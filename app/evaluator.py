import logging
from typing import Optional

from app.config import (
    RETAIL_PRICE_USD, COGS_PER_RING_USD,
    GRADE_A_FLOOR, GRADE_B_FLOOR, GRADE_C_FLOOR,
)
from app.models import (
    MarketEconomics, PromoHistory, PromoRecord,
    PLImpact, SensitivityRow, ScenarioRow, VelocityCheck, RiskFlag, PromoEvaluation,
    InfluencerCommercials, InfluencerPL,
)
from app.parser import PromoRequest
from app.regions import NEW_MARKETS, normalize_region
from app.sheets import get_influencer_commercials, get_influencer_volume_estimate
from app.influencer_history import get_influencer_bau

logger = logging.getLogger(__name__)

# Marketing Types the bot does not financially evaluate.
# Submissions with these promo_type values are logged to the Tracker but
# return a "LOGGED" verdict with no P&L / verdict / risk flags.
_NO_EVAL_PROMO_TYPES = frozenset({"no_eval", "sales_contest", "sales contest", "pos"})


def _is_no_eval(request: "PromoRequest") -> bool:
    """Return True if this submission should be logged without financial evaluation.

    Covers:
      - Exact matches in _NO_EVAL_PROMO_TYPES ("no_eval", "sales_contest", "pos", …)
      - Any promo_type starting with "pos" ("pos investment", "pos display", …)
      - "display" / "displays" variants
    """
    pt = (request.promo_type or "").strip().lower()
    return (
        pt in _NO_EVAL_PROMO_TYPES
        or pt.startswith("pos")
        or pt in ("display", "displays")
    )


# ---------------------------------------------------------------------------
# 1. Incremental unit estimation from historical promo data
# ---------------------------------------------------------------------------

def estimate_incremental_units(
    history: PromoHistory,
    request: PromoRequest,
    promo_weeks: float = 1.0,
    target_month: int = 0,
    ad_metrics=None,
    seasonal_lift=None,
) -> int:
    """Estimate incremental units using historical promo performance.

    Priority:
    1. Same retailer, same discount level → use actual incremental units
    2. Same retailer, nearest discount level → interpolate
    3. Cross-region fallback (already merged into history if needed)
    4. Conservative default

    For non-discount promos (SPIV, newsletter, etc.), skips discount-based
    logic and uses comparable promo type matching instead.
    """
    target_disc = request.discount_pct or 0
    promo_type = request.promo_type or "promo"

    # For discount promos, require a discount %
    if promo_type in ("promo", "") and target_disc == 0:
        return 0

    baseline = history.baseline_units_weekly or 0

    # Apply seasonal adjustment if we have an index for the target month
    seasonal_mult = 1.0
    if target_month and history.seasonal_index:
        seasonal_mult = history.seasonal_index.get(target_month, 1.0)

    adjusted_baseline = baseline * seasonal_mult if baseline > 0 else 0

    # Find comparable promos — promo_type is now a key scoring factor
    target_duration_days = int(round(promo_weeks * 7))
    comp = _find_best_comparable(
        history.records, target_disc, target_month, target_duration_days, promo_type
    )

    # Guard: if the comp has no parseable duration, drop it. Previously the
    # evaluator would silently treat a duration-less comp as a 1-week comp
    # (because sheets.py defaulted to 7 days), which inflated cross-region
    # volume estimates 2-7×. Better to return 0/no estimate than a fabricated one.
    if comp is not None and comp.duration_days <= 0:
        logger.info(
            f"Skipping comp '{comp.promo_name}' ({comp.market}/{comp.retailer}): "
            f"missing/unparseable duration_days. Falling through to later paths."
        )
        comp = None

    # When history was enriched with cross-region data, the baseline may not represent
    # the actual retailer (e.g. Poland baseline ≠ Elecunion). Skip baseline-dependent
    # paths and go straight to comp's own units_sold (Path 4).
    use_baseline_paths = not history.fallback_source

    # --- SPIV/non-discount path: use conservative baseline lift ---
    # SPIV comps often overlap with concurrent discounts (e.g. Dec SPIV + 30% Christmas).
    # Using raw comp volume would wildly overestimate standalone SPIV impact.
    # Instead, use baseline + conservative lift (10-25% for SPIV, 5-15% for newsletter).
    if promo_type not in ("promo", "") and adjusted_baseline > 0:
        lift = _non_discount_lift(promo_type, comp, adjusted_baseline, ad_metrics=ad_metrics)
        incremental = int(round(adjusted_baseline * (lift - 1.0) * promo_weeks))
        logger.info(f"Non-discount promo ({promo_type}): baseline={adjusted_baseline:.0f}/wk, "
                     f"lift={lift:.2f}x → {incremental} incremental in {promo_weeks:.1f}w")
        return max(1, incremental)

    # --- Path 1: comparable has known incremental units + we have baseline ---
    if use_baseline_paths and comp and comp.incremental_units > 0 and baseline > 0:
        comp_weeks = comp.duration_days / 7 if comp.duration_days > 0 else 1
        weekly_incremental = comp.incremental_units / comp_weeks
        # Adjust for discount difference (ratio^0.7 gives diminishing returns)
        comp_disc = comp.discount_pct or target_disc
        if comp_disc > 0 and comp_disc != target_disc:
            discount_ratio = target_disc / comp_disc
            weekly_incremental *= (discount_ratio ** 0.7)
        return max(1, int(round(weekly_incremental * promo_weeks * seasonal_mult)))

    # --- Path 2: comparable has units + baseline → compute lift ---
    if use_baseline_paths and comp and comp.units_sold > 0 and comp.baseline_units_weekly and comp.baseline_units_weekly > 0 and adjusted_baseline > 0:
        comp_weeks = comp.duration_days / 7 if comp.duration_days > 0 else 1
        lift = comp.units_sold / (comp.baseline_units_weekly * comp_weeks)
        if lift > 1.0:
            total_promo = adjusted_baseline * lift * promo_weeks
            return max(1, int(round(total_promo - adjusted_baseline * promo_weeks)))

    # --- Path 3: interpolate lift from all records ---
    if use_baseline_paths and adjusted_baseline > 0:
        lift = _interpolate_lift_from_records(history.records, target_disc, baseline)
        if lift and lift > 1.0:
            total_promo = adjusted_baseline * lift * promo_weeks
            return max(1, int(round(total_promo - adjusted_baseline * promo_weeks)))

    # --- Path 4: comparable has units_sold → use LIFT RATIO, not absolute volume ---
    # Primary path when cross-region enrichment is present (baseline may not match
    # the target retailer, so baseline-dependent Paths 1-3 are bypassed).
    #
    # Bug fix: previous version scaled comp.units_sold (TOTAL volume during comp)
    # as if it were already net-of-baseline incremental. It isn't. For a 13d Catalogmart
    # UK comp at 15% off with 100 units sold, that returned ~108 incremental for a
    # Bulkclub UK target — even though Bulkclub UK's own baseline implies < 5/wk.
    #
    # Corrected: when the comp has its own baseline, derive a LIFT RATIO
    # (comp_total / comp_baseline_total) and apply it to the TARGET region's
    # baseline. When the comp has no baseline, fall back to the old volume-scaling
    # behaviour but subtract target_baseline × promo_weeks at the end so we return
    # an *incremental* figure rather than total promo volume.
    if comp and comp.units_sold > 0 and comp.duration_days > 0:
        comp_weeks = comp.duration_days / 7
        comp_disc = comp.discount_pct or target_disc

        # Preferred sub-path: comp has its own baseline → lift-ratio scaling
        if (
            comp.baseline_units_weekly
            and comp.baseline_units_weekly > 0
            and adjusted_baseline > 0
        ):
            comp_total = comp.units_sold
            comp_baseline_total = comp.baseline_units_weekly * comp_weeks
            if comp_baseline_total > 0 and comp_total > comp_baseline_total:
                lift = comp_total / comp_baseline_total
                target_baseline_total = adjusted_baseline * promo_weeks
                target_total_promo = target_baseline_total * lift
                incremental = target_total_promo - target_baseline_total
                # Diminishing-returns adjustment for discount delta
                if comp_disc > 0 and comp_disc != target_disc:
                    incremental *= (target_disc / comp_disc) ** 0.7
                result = max(1, int(round(incremental * seasonal_mult)))
                logger.info(
                    f"Path 4 eval (lift-ratio): comp.duration_days={comp.duration_days}, "
                    f"comp.baseline_units_weekly={comp.baseline_units_weekly}, "
                    f"comp.units_sold={comp.units_sold}, lift={lift:.2f}x, "
                    f"target_baseline={adjusted_baseline:.2f}/wk, promo_weeks={promo_weeks:.1f}, "
                    f"comp_disc={comp_disc:.0f}%, target_disc={target_disc:.0f}%, "
                    f"predicted_incremental={result}"
                )
                return result

        # Fallback sub-path: comp has no usable baseline → scale comp's weekly
        # rate to the target promo period, then subtract target's own baseline so
        # we return incremental (not total) volume.
        comp_weekly_rate = comp.units_sold / comp_weeks
        if comp_disc > 0 and comp_disc != target_disc:
            comp_weekly_rate *= (target_disc / comp_disc) ** 0.7
        target_total = comp_weekly_rate * promo_weeks
        target_baseline_total = adjusted_baseline * promo_weeks if adjusted_baseline > 0 else 0
        result = max(1, int(round((target_total - target_baseline_total) * seasonal_mult)))
        logger.info(
            f"Path 4 eval (volume-fallback): comp.duration_days={comp.duration_days}, "
            f"comp.baseline_units_weekly={comp.baseline_units_weekly}, "
            f"comp.units_sold={comp.units_sold}, target_baseline={adjusted_baseline:.2f}/wk, "
            f"promo_weeks={promo_weeks:.1f}, comp_disc={comp_disc:.0f}%, "
            f"target_disc={target_disc:.0f}%, predicted_incremental={result}"
        )
        return result

    # --- Path 5: conservative lift on baseline ---
    # Prefer seasonal lift (same quarter) over annual average when available
    if adjusted_baseline > 0:
        seasonal_used = False
        if seasonal_lift and seasonal_lift.available and seasonal_lift.seasonal_lift_by_bracket:
            bracket = _discount_to_bracket(target_disc)
            lift = seasonal_lift.seasonal_lift_by_bracket.get(bracket)
            if lift and lift > 1.0:
                total_promo = adjusted_baseline * lift * promo_weeks
                incremental = int(round(total_promo - adjusted_baseline * promo_weeks))
                logger.info(f"Seasonal lift ({seasonal_lift.seasonal_source}): {lift:.1f}x "
                             f"at {target_disc}% → {incremental} incremental units")
                seasonal_used = True
                return max(1, incremental)

        if not seasonal_used:
            conservative_lift = _conservative_lift(target_disc, ad_metrics=ad_metrics)
            total_promo = adjusted_baseline * conservative_lift * promo_weeks
            incremental = int(round(total_promo - adjusted_baseline * promo_weeks))
            logger.info(f"Conservative lift (annual): {conservative_lift:.1f}x at {target_disc}% "
                         f"→ {incremental} incremental units")
            return max(1, incremental)

    return 0


def _discount_to_bracket(discount_pct: float) -> str:
    """Map a discount percentage to the lift bracket key."""
    if discount_pct <= 15:
        return "5-15"
    elif discount_pct <= 25:
        return "15-25"
    else:
        return "25+"


def _non_discount_lift(
    promo_type: str,
    comp: Optional[PromoRecord],
    baseline: float,
    ad_metrics=None,
) -> float:
    """Estimate lift for non-discount promos (SPIV, newsletter, emailer, etc.).

    SPIV comps are unreliable for direct volume scaling because they often ran
    concurrently with heavy discounts (e.g. Dec 2025 AU SPIV + [REDACTED] Christmas).
    Using raw comp volume would wildly overestimate standalone SPIV impact.

    Instead, use conservative industry-standard lift ranges:
    - SPIV: [REDACTED] lift (staff incentive alone, no consumer discount)
    - Newsletter/emailer: [REDACTED] lift
    - ecom_campaign: [REDACTED] lift

    For display/push marketing types (emailer, ecom_campaign, social), Amazon
    SponsoredDisplay ROAS in the same geo indicates how well banner/push
    advertising converts in that market. We use this to calibrate within the
    lift range: strong display performance → upper end, weak → lower end.
    """
    # Default conservative lifts by promo type
    # REDACTED: commercial lift-range values removed
    lift_ranges = {
        "spiv": (0.0, 0.0),
        "newsletter": (0.0, 0.0),
        "emailer": (0.0, 0.0),
        "ecom_campaign": (0.0, 0.0),
        "in_store": (0.0, 0.0),
        "social": (0.0, 0.0),
    }
    low, high = lift_ranges.get(promo_type, (0.0, 0.0))  # REDACTED

    # Promo types where display/push ad performance is a relevant signal
    DISPLAY_ANALOGOUS = ("emailer", "newsletter", "ecom_campaign", "social")

    # Calibrate display-type promos using Amazon SponsoredDisplay ROAS in the geo
    # Compares display ROAS to overall ROAS — tells us if banner/push marketing
    # over- or under-performs in this market relative to other ad types
    if (ad_metrics and ad_metrics.available and promo_type in DISPLAY_ANALOGOUS
            and ad_metrics.roas and ad_metrics.roas > 0):
        display_roas = ad_metrics.roas_by_type.get("SponsoredDisplay")
        if display_roas is not None:
            ratio = display_roas / ad_metrics.roas
            if ratio >= 0.8:
                # Display works well in this market → upper quartile
                calibrated = low + (high - low) * 0.75
                logger.info(f"Display strong in geo (ratio {ratio:.2f}) -> lift {calibrated:.3f}x for {promo_type}")
                return calibrated
            elif ratio < 0.4:
                # Display weak in this market → lower quartile
                calibrated = low + (high - low) * 0.25
                logger.info(f"Display weak in geo (ratio {ratio:.2f}) -> lift {calibrated:.3f}x for {promo_type}")
                return calibrated
            # else: moderate (0.4-0.8) → fall through to comp/midpoint logic below

    # If comp has baseline data, try to extract actual lift (but cap it)
    if comp and comp.baseline_units_weekly and comp.baseline_units_weekly > 0 and comp.units_sold > 0:
        comp_weeks = comp.duration_days / 7 if comp.duration_days > 0 else 1
        comp_lift = comp.units_sold / (comp.baseline_units_weekly * comp_weeks)
        # Cap at the high end of the range — comp likely had concurrent discount
        capped_lift = min(comp_lift, high)
        logger.info(f"Non-discount comp lift: raw={comp_lift:.2f}x, capped to {capped_lift:.2f}x "
                     f"(range {low:.2f}-{high:.2f} for {promo_type})")
        return max(low, capped_lift)

    # Default: midpoint of range
    mid = (low + high) / 2
    logger.info(f"No usable comp for {promo_type}, using midpoint lift: {mid:.2f}x")
    return mid


def _conservative_lift(discount_pct: float, ad_metrics=None) -> float:
    """Conservative lift multiplier based on discount level.

    Uses Amazon promo lift curves for the same geo when available — real
    market-level price elasticity data. Falls back to hardcoded defaults
    when no Amazon data exists for this region.
    """
    # Try Amazon promo lift data for this geo (with nearest-bracket fallback)
    if ad_metrics and ad_metrics.available and ad_metrics.promo_lift and ad_metrics.promo_lift.available:
        lift_map = ad_metrics.promo_lift.lift_by_bracket
        # Map discount_pct to bracket, fall back to nearest available
        if discount_pct >= 25:
            bracket_order = ["25+", "15-25", "5-15"]
        elif discount_pct >= 15:
            bracket_order = ["15-25", "5-15", "25+"]
        elif discount_pct >= 5:
            bracket_order = ["5-15", "15-25", "25+"]
        else:
            bracket_order = []
        amz_lift = None
        for b in bracket_order:
            if b in lift_map:
                amz_lift = lift_map[b]
                break

        if amz_lift is not None:
            # Use Amazon lift but apply a conservative discount ([REDACTED]x)
            # because Amazon conversion may be higher than retail partners
            conservative = max(0.0, amz_lift * 0.0)  # REDACTED
            logger.info(f"Amazon-calibrated lift: {amz_lift:.1f}x raw → {conservative:.1f}x "
                        f"conservative at {discount_pct:.0f}% discount")
            return conservative

    # Hardcoded fallback
    # REDACTED: commercial lift thresholds/multipliers removed
    if discount_pct >= 0:
        return 0.0
    if discount_pct >= 0:
        return 0.0
    if discount_pct >= 0:
        return 0.0
    return 0.0


def _find_best_comparable(
    records: list[PromoRecord],
    target_disc: float,
    target_month: int = 0,
    target_duration_days: int = 0,
    target_promo_type: str = "",
) -> Optional[PromoRecord]:
    """Find closest promo by discount level, prefer same season, duration, and promo type.

    Scoring (lower = better):
    - Promo type match: -8 bonus (strongest signal — SPIV comps for SPIV, etc.)
    - Promo type mismatch: +15 penalty (comparing SPIV to discount promo is unreliable)
    - Discount difference is a primary factor (for discount-based promos)
    - Same month: -3 bonus
    - Same quarter (e.g. OND for Dec target): -2 bonus
    - Has incremental units: -2 bonus
    - Direct region (not cross-region): -1 bonus
    - Duration mismatch penalty: +5 if ratio > 3x, +2 if ratio > 2x
    """
    # GWP / gift-with-purchase is not a price discount and doesn't model
    # discount elasticity — never a discount comp (kept only if the target
    # itself is a GWP).
    if target_promo_type != "gwp":
        records = [r for r in records if (r.promo_type or "") != "gwp"]

    # For non-discount promos (SPIV, newsletter), include records without discount_pct
    if target_promo_type and target_promo_type != "promo":
        candidates = [r for r in records if r.units_sold > 0]
    else:
        candidates = [r for r in records if r.discount_pct is not None and r.units_sold > 0]
    if not candidates:
        return None

    # Define quarters for seasonal matching
    quarter_months = {
        1: {1, 2, 3}, 2: {1, 2, 3}, 3: {1, 2, 3},
        4: {4, 5, 6}, 5: {4, 5, 6}, 6: {4, 5, 6},
        7: {7, 8, 9}, 8: {7, 8, 9}, 9: {7, 8, 9},
        10: {10, 11, 12}, 11: {10, 11, 12}, 12: {10, 11, 12},
    }
    target_quarter = quarter_months.get(target_month, set())
    # Quarter number (1-4) for cross-season penalty
    quarter_num = {1: 1, 2: 1, 3: 1, 4: 2, 5: 2, 6: 2, 7: 3, 8: 3, 9: 3, 10: 4, 11: 4, 12: 4}

    def score(rec: PromoRecord) -> float:
        """Higher = better comp. All signals add (+) for match, subtract (-) for mismatch."""
        s = 0.0

        # Promo type match (+8 same, -15 different)
        if target_promo_type:
            rec_type = rec.promo_type or "promo"
            s += 8 if rec_type == target_promo_type else -15

        # Discount proximity: exact=+8, within 5%=+4, within 10%=+2, further=-disc_diff
        if target_disc and target_disc > 0:
            disc_diff = abs((rec.discount_pct or 0) - target_disc)
            if disc_diff == 0:
                s += 8
            elif disc_diff <= 5:
                s += 4
            elif disc_diff <= 10:
                s += 2
            else:
                s -= disc_diff
        # else non-discount promo — discount is irrelevant, no signal

        # Volume: +3 if reliable (≥5 units), -15 if too few to trust
        if rec.units_sold < 5:
            s -= 15
        else:
            s += min(3, rec.units_sold / 10)

        # Season: same month=+6, same quarter=+4, adjacent quarter=0, opposite half=-8
        if target_month:
            if rec.month == target_month:
                s += 6
            elif rec.month in target_quarter:
                s += 4
            elif rec.month:
                tq = quarter_num.get(target_month, 0)
                rq = quarter_num.get(rec.month, 0)
                qdiff = min(abs(tq - rq), 4 - abs(tq - rq))
                if qdiff >= 2:
                    s -= 8

        # Incremental data available
        if rec.incremental_units > 0:
            s += 2

        # Region: direct=+3, cross-region=-10
        if "(cross-region" in (rec.notes or ""):
            s -= 10
        else:
            s += 3

        # Duration proximity: close=+2, 2x off=0, 3x+ off=-2 or -4
        if target_duration_days > 0 and rec.duration_days > 0:
            ratio = max(target_duration_days, rec.duration_days) / max(min(target_duration_days, rec.duration_days), 1)
            if ratio <= 2:
                s += 2
            elif ratio <= 3:
                s -= 2
            else:
                s -= 4

        return s

    # Stable tie-breaking: primary = score (higher = better),
    # secondary = units_sold (prefer more data), tertiary = promo_name (alphabetical stability).
    # Without stable tiebreaking, equal-scoring comps resolve by list order which varies
    # across Google Sheets reads, causing different comps to be selected for identical inputs.
    return max(candidates, key=lambda r: (score(r), r.units_sold, r.promo_name or ""))


def _interpolate_lift_from_records(
    records: list[PromoRecord],
    target_disc: float,
    baseline: float,
) -> Optional[float]:
    """Compute lift multiplier by interpolating from historical records."""
    points = []  # (discount_pct, lift)
    for r in records:
        if (r.promo_type or "") == "gwp":
            continue  # GWP is not a price discount — excludes from elasticity
        if r.discount_pct is not None and r.units_sold > 0 and r.duration_days > 0:
            weekly_units = r.units_sold / max(r.duration_days / 7, 1)
            rec_baseline = r.baseline_units_weekly or baseline
            if rec_baseline > 0:
                points.append((r.discount_pct, weekly_units / rec_baseline))

    if not points:
        return None

    # Exact or near match
    for disc, lift in points:
        if abs(disc - target_disc) < 2.0:
            return lift

    # Bracket interpolation
    below = [(d, l) for d, l in points if d < target_disc]
    above = [(d, l) for d, l in points if d > target_disc]

    if below and above:
        lo_d, lo_l = max(below, key=lambda x: x[0])
        hi_d, hi_l = min(above, key=lambda x: x[0])
        ratio = (target_disc - lo_d) / (hi_d - lo_d)
        return lo_l + ratio * (hi_l - lo_l)

    # Extrapolate from nearest
    nearest_d, nearest_l = min(points, key=lambda x: abs(x[0] - target_disc))
    if nearest_d > 0:
        return max(1.1, nearest_l * (target_disc / nearest_d))

    return None


# ---------------------------------------------------------------------------
# 2. Seasonality index from monthly sell-through
# ---------------------------------------------------------------------------

def build_seasonal_index(records: list[PromoRecord]) -> dict:
    """Build {month: index} where 1.0 = average month.

    Uses baseline (non-promo) periods where available, falls back to all data.
    """
    monthly_units = {}  # {month: [units_per_week]}
    for r in records:
        if r.month and r.baseline_units_weekly and r.baseline_units_weekly > 0:
            monthly_units.setdefault(r.month, []).append(r.baseline_units_weekly)

    if len(monthly_units) < 3:
        # Not enough data for seasonal index
        return {}

    monthly_avg = {m: sum(vals) / len(vals) for m, vals in monthly_units.items()}
    overall_avg = sum(monthly_avg.values()) / len(monthly_avg) if monthly_avg else 1
    if overall_avg == 0:
        return {}

    return {m: round(avg / overall_avg, 2) for m, avg in monthly_avg.items()}


# ---------------------------------------------------------------------------
# 3. P&L waterfall
# ---------------------------------------------------------------------------

def model_pl_impact(
    incremental_units: int,
    request: PromoRequest,
    econ: MarketEconomics,
    marketing_spend: float = 0.0,
    soa_per_unit: float = 0.0,
    volume_confidence: str = "high",
    addon_cogs_per_unit: float = 0.0,
    bau_weekly: float = 0.0,
    promo_weeks: float = 2.0,
) -> PLImpact:
    """Build P&L waterfall for incremental promo volume.

    Always computes per-unit economics (buy price, TD, returns, COGS, margin).
    If incremental_units > 0, also computes totals and CM3-Cash.
    If incremental_units == 0, shows per-unit P&L + breakeven units.
    """
    units = incremental_units
    discount_pct = request.discount_pct or 0
    buy_price = econ.buy_price_usd
    retail_price = econ.retail_price_usd
    return_rate = econ.return_rate

    # --- Per-unit economics (always computed) ---
    # When "the brand bears $X per ring" is specified, use that directly as trade discount
    brand_bears = request.brand_bears_per_unit or 0.0
    if brand_bears > 0:
        trade_disc_per_unit = brand_bears
    else:
        discount_per_unit_absolute = retail_price * (discount_pct / 100.0)
        trade_disc_per_unit = discount_per_unit_absolute * econ.trade_discount_brand_pct
    # Rebate is separate from SOA
    rebate_per_unit = buy_price * (request.rebate_pct / 100.0) if request.rebate_pct else 0.0
    returns_per_unit = return_rate * buy_price
    net_rev_per_unit = buy_price - trade_disc_per_unit - returns_per_unit
    warranty_per_unit = 0.0 * net_rev_per_unit  # REDACTED: warranty rate
    margin_per_unit = net_rev_per_unit - COGS_PER_RING_USD - addon_cogs_per_unit - warranty_per_unit

    # Breakeven: how many units to cover marketing spend + SOA + rebate
    effective_margin_per_unit = margin_per_unit - soa_per_unit - rebate_per_unit
    breakeven = None
    if effective_margin_per_unit > 0 and marketing_spend > 0:
        breakeven = int(marketing_spend / effective_margin_per_unit) + 1

    if units <= 0:
        # No volume estimate — return per-unit economics + breakeven + scenario table
        pl_no_vol = PLImpact(
            incremental_units=0,
            return_rate_used=return_rate,
            trade_discount_per_unit=round(trade_disc_per_unit, 2),
            brand_bears_per_unit=brand_bears,
            returns_per_unit=round(returns_per_unit, 2),
            net_revenue_per_unit=round(net_rev_per_unit, 2),
            margin_per_unit=round(margin_per_unit, 2),
            breakeven_units=breakeven,
            marketing_spend=round(marketing_spend, 2),
            soa_per_unit=soa_per_unit,
            rebate_per_unit=round(rebate_per_unit, 2),
            addon_cogs_per_unit=addon_cogs_per_unit,
            addon_cogs_label=request.addon_cogs_label or "",
            volume_confidence=volume_confidence,
            grade="C",  # can't grade without volume
        )
        # Scenario anchor: breakeven > BAU-scaled > default  # REDACTED
        anchor = breakeven or (int(bau_weekly * promo_weeks) if bau_weekly > 0 else 1)  # REDACTED
        if anchor and anchor > 0:
            pl_no_vol.scenarios = _build_scenarios(
                anchor, request, econ, marketing_spend, soa_per_unit,
                addon_cogs_per_unit, bau_weekly=bau_weekly, promo_weeks=promo_weeks,
            )
        return pl_no_vol

    # --- Totals (when we have volume) ---
    gross_rev_incl = units * retail_price
    gross_rev_excl = units * buy_price
    channel_margin = gross_rev_incl - gross_rev_excl
    trade_disc_total = units * trade_disc_per_unit
    returns_total = units * returns_per_unit
    net_revenue = gross_rev_excl - trade_disc_total - returns_total
    cogs_total = units * COGS_PER_RING_USD
    addon_cogs_total = units * addon_cogs_per_unit
    gross_margin = net_revenue - cogs_total - addon_cogs_total
    warranty_cost = 0.0 * net_revenue  # REDACTED: warranty rate
    soa_total = units * soa_per_unit
    rebate_total = units * rebate_per_unit
    cm3_cash = gross_margin - marketing_spend - soa_total - rebate_total - warranty_cost
    cm3_cash_pct = (cm3_cash / net_revenue * 100) if net_revenue > 0 else None
    grade = _grade_from_pct(cm3_cash_pct, cm3_cash)

    # ROAS: gross revenue (incl channel) / total promo investment
    total_investment = marketing_spend + trade_disc_total + soa_total + rebate_total
    roas = gross_rev_incl / total_investment if total_investment > 0 else None

    pl = PLImpact(
        incremental_units=units,
        gross_revenue_incl_channel=round(gross_rev_incl, 2),
        gross_revenue_excl_channel=round(gross_rev_excl, 2),
        channel_margin=round(channel_margin, 2),
        trade_discount_total=round(trade_disc_total, 2),
        returns_cost=round(returns_total, 2),
        return_rate_used=return_rate,
        net_revenue=round(net_revenue, 2),
        cogs_total=round(cogs_total, 2),
        gross_margin=round(gross_margin, 2),
        warranty_cost=round(warranty_cost, 2),
        marketing_spend=round(marketing_spend, 2),
        soa_per_unit=soa_per_unit,
        soa_total=round(soa_total, 2),
        rebate_per_unit=round(rebate_per_unit, 2),
        rebate_total=round(rebate_total, 2),
        addon_cogs_per_unit=addon_cogs_per_unit,
        addon_cogs_total=round(addon_cogs_total, 2),
        addon_cogs_label=request.addon_cogs_label or "",
        cm3_cash=round(cm3_cash, 2),
        cm3_cash_pct=round(cm3_cash_pct, 1) if cm3_cash_pct is not None else None,
        grade=grade,
        total_promo_investment=round(total_investment, 2),
        roas=round(roas, 1) if roas else None,
        trade_discount_per_unit=round(trade_disc_per_unit, 2),
        brand_bears_per_unit=brand_bears,
        returns_per_unit=round(returns_per_unit, 2),
        net_revenue_per_unit=round(net_rev_per_unit, 2),
        margin_per_unit=round(margin_per_unit, 2),
        breakeven_units=breakeven,
        volume_confidence=volume_confidence,
    )

    # Build sensitivity analysis
    pl.sensitivity = _build_sensitivity(units, request, econ, marketing_spend, pl, soa_per_unit, addon_cogs_per_unit)

    # Always build Bear/Base/Bull/StrongBull scenarios
    # Anchor: breakeven > estimated units > BAU-scaled > default  # REDACTED
    anchor = breakeven or units or (int(bau_weekly * promo_weeks) if bau_weekly > 0 else 1)  # REDACTED
    if anchor and anchor > 0:
        pl.scenarios = _build_scenarios(
            anchor, request, econ, marketing_spend, soa_per_unit,
            addon_cogs_per_unit, bau_weekly=bau_weekly, promo_weeks=promo_weeks,
        )

    return pl


def model_spiv_pl_impact(
    incremental_units: int,
    request: PromoRequest,
    econ: MarketEconomics,
    marketing_spend: float = 0.0,
    spiv_per_unit: float = 0.0,
    promo_weeks: float = 1.0,
    volume_confidence: str = "high",
    baseline_weekly: float = 0.0,
) -> PLImpact:
    """Build P&L for SPIV campaigns.

    Key difference from standard promos: SPIV cost applies to ALL units
    during the campaign (baseline + incremental), not just incremental.
    This makes baseline volume a critical cost driver.

    Args:
        baseline_weekly: Weekly baseline sell-through (from history or velocity).
            Critical for computing total SPIV cost.
    """
    units = incremental_units
    buy_price = econ.buy_price_usd
    retail_price = econ.retail_price_usd
    return_rate = econ.return_rate
    discount_pct = request.discount_pct or 0  # may be 0 for pure SPIV

    # --- Per-unit economics ---
    brand_bears = request.brand_bears_per_unit or 0.0
    if brand_bears > 0:
        trade_disc_per_unit = brand_bears
    else:
        discount_per_unit = retail_price * (discount_pct / 100.0)
        trade_disc_per_unit = discount_per_unit * econ.trade_discount_brand_pct
    returns_per_unit = return_rate * buy_price
    net_rev_per_unit = buy_price - trade_disc_per_unit - returns_per_unit
    warranty_per_unit = 0.0 * net_rev_per_unit  # REDACTED: warranty rate
    margin_per_unit = net_rev_per_unit - COGS_PER_RING_USD - warranty_per_unit

    # SPIV breakeven: must cover SPIV cost on baseline units
    # Net gain per incremental unit = margin - SPIV (on that unit)
    # Net cost from baseline = baseline_units * SPIV_per_unit
    # Breakeven: baseline_cost / (margin - SPIV) = incremental units needed
    total_baseline_units = int(round(baseline_weekly * promo_weeks)) if baseline_weekly > 0 else 0
    total_all_units = total_baseline_units + units
    spiv_total = total_all_units * spiv_per_unit

    effective_margin = margin_per_unit - spiv_per_unit
    breakeven = None
    if effective_margin > 0:
        baseline_spiv_cost = total_baseline_units * spiv_per_unit + marketing_spend
        breakeven = int(baseline_spiv_cost / effective_margin) + 1 if baseline_spiv_cost > 0 else 1

    if units <= 0:
        return PLImpact(
            incremental_units=0,
            return_rate_used=return_rate,
            trade_discount_per_unit=round(trade_disc_per_unit, 2),
            returns_per_unit=round(returns_per_unit, 2),
            net_revenue_per_unit=round(net_rev_per_unit, 2),
            margin_per_unit=round(margin_per_unit, 2),
            breakeven_units=breakeven,
            marketing_spend=round(marketing_spend, 2),
            soa_per_unit=spiv_per_unit,  # reuse soa fields for SPIV display
            soa_total=round(spiv_total, 2),
            volume_confidence=volume_confidence,
            grade="C",
        )

    # --- Totals (incremental units only for revenue, ALL units for SPIV cost) ---
    gross_rev_incl = units * retail_price
    gross_rev_excl = units * buy_price
    channel_margin = gross_rev_incl - gross_rev_excl
    trade_disc_total = units * trade_disc_per_unit
    returns_total = units * returns_per_unit
    net_revenue = gross_rev_excl - trade_disc_total - returns_total
    cogs_total = units * COGS_PER_RING_USD
    gross_margin = net_revenue - cogs_total

    warranty_cost = 0.0 * net_revenue  # REDACTED: warranty rate
    # SPIV cost on ALL units (baseline + incremental) — this is the key difference
    cm3_cash = gross_margin - marketing_spend - spiv_total - warranty_cost
    cm3_cash_pct = (cm3_cash / net_revenue * 100) if net_revenue > 0 else None
    grade = _grade_from_pct(cm3_cash_pct, cm3_cash)

    total_investment = marketing_spend + trade_disc_total + spiv_total
    roas = gross_rev_incl / total_investment if total_investment > 0 else None

    pl = PLImpact(
        incremental_units=units,
        gross_revenue_incl_channel=round(gross_rev_incl, 2),
        gross_revenue_excl_channel=round(gross_rev_excl, 2),
        channel_margin=round(channel_margin, 2),
        trade_discount_total=round(trade_disc_total, 2),
        returns_cost=round(returns_total, 2),
        return_rate_used=return_rate,
        net_revenue=round(net_revenue, 2),
        cogs_total=round(cogs_total, 2),
        gross_margin=round(gross_margin, 2),
        warranty_cost=round(warranty_cost, 2),
        marketing_spend=round(marketing_spend, 2),
        soa_per_unit=spiv_per_unit,
        soa_total=round(spiv_total, 2),
        cm3_cash=round(cm3_cash, 2),
        cm3_cash_pct=round(cm3_cash_pct, 1) if cm3_cash_pct is not None else None,
        grade=grade,
        total_promo_investment=round(total_investment, 2),
        roas=round(roas, 1) if roas else None,
        trade_discount_per_unit=round(trade_disc_per_unit, 2),
        returns_per_unit=round(returns_per_unit, 2),
        net_revenue_per_unit=round(net_rev_per_unit, 2),
        margin_per_unit=round(margin_per_unit, 2),
        breakeven_units=breakeven,
        volume_confidence=volume_confidence,
    )

    # Always build Bear/Base/Bull/StrongBull scenarios anchored to breakeven incremental
    if breakeven and breakeven > 0:
        pl.scenarios = _build_spiv_scenarios(
            breakeven, total_baseline_units, request, econ,
            marketing_spend, spiv_per_unit,
            bau_weekly=baseline_weekly, promo_weeks=promo_weeks,
        )

    return pl


def _build_spiv_scenarios(
    breakeven_incremental: int,
    baseline_units: int,
    request: PromoRequest,
    econ: MarketEconomics,
    marketing_spend: float,
    spiv_per_unit: float,
    bau_weekly: float = 0.0,
    promo_weeks: float = 2.0,
) -> list[ScenarioRow]:
    """Build SPIV scenario table (Bear/Base/Bull/StrongBull) anchored to breakeven incremental units."""
    scenarios = []
    buy_price = econ.buy_price_usd
    retail_price = econ.retail_price_usd
    return_rate = econ.return_rate
    discount_pct = request.discount_pct or 0

    brand_bears = request.brand_bears_per_unit or 0.0

    for label, mult in [("Bear", 0.5), ("Base", 1.0), ("Bull", 1.5), ("Strong Bull", 2.0)]:
        inc_units = max(1, int(round(breakeven_incremental * mult)))
        total_units = baseline_units + inc_units
        # Revenue from incremental units only
        if brand_bears > 0:
            trade_disc = inc_units * brand_bears
        else:
            trade_disc = inc_units * retail_price * (discount_pct / 100.0) * econ.trade_discount_brand_pct
        returns = inc_units * return_rate * buy_price
        nr = inc_units * buy_price - trade_disc - returns
        cogs = inc_units * COGS_PER_RING_USD
        gm = nr - cogs
        warranty = 0.0 * nr  # REDACTED: warranty rate
        # SPIV cost on ALL units
        spiv_total = total_units * spiv_per_unit
        cm3 = gm - marketing_spend - spiv_total - warranty
        pct = (cm3 / nr * 100) if nr > 0 else None
        grade = _grade_from_pct(pct, cm3)
        uplift_pct = None
        if bau_weekly > 0 and promo_weeks > 0:
            uplift_pct = round((inc_units / promo_weeks / bau_weekly) * 100, 0)
        scenarios.append(ScenarioRow(
            label=label,
            units=inc_units,
            cm3_cash=round(cm3, 2),
            cm3_cash_pct=round(pct, 1) if pct is not None else None,
            grade=grade,
            uplift_pct=uplift_pct,
        ))

    return scenarios


def _build_scenarios(
    breakeven_units: int,
    request: PromoRequest,
    econ: MarketEconomics,
    marketing_spend: float,
    soa_per_unit: float,
    addon_cogs_per_unit: float = 0.0,
    bau_weekly: float = 0.0,
    promo_weeks: float = 2.0,
) -> list[ScenarioRow]:
    """Build Bear/Base/Bull/StrongBull scenario table anchored to breakeven units."""
    scenarios = []
    discount_pct = request.discount_pct or 0
    buy_price = econ.buy_price_usd
    retail_price = econ.retail_price_usd
    return_rate = econ.return_rate

    brand_bears = request.brand_bears_per_unit or 0.0
    rebate_pu = buy_price * (request.rebate_pct / 100.0) if request.rebate_pct else 0.0

    for label, mult in [("Bear", 0.5), ("Base", 1.0), ("Bull", 1.5), ("Strong Bull", 2.0)]:
        units = max(1, int(round(breakeven_units * mult)))
        if brand_bears > 0:
            trade_disc = units * brand_bears
        else:
            trade_disc = units * retail_price * (discount_pct / 100.0) * econ.trade_discount_brand_pct
        returns = units * return_rate * buy_price
        nr = units * buy_price - trade_disc - returns
        cogs = units * (COGS_PER_RING_USD + addon_cogs_per_unit)
        gm = nr - cogs
        warranty = 0.0 * nr  # REDACTED: warranty rate
        soa = units * soa_per_unit
        rebate = units * rebate_pu
        cm3 = gm - marketing_spend - soa - rebate - warranty
        pct = (cm3 / nr * 100) if nr > 0 else None
        grade = _grade_from_pct(pct, cm3)
        # BAU uplift: what weekly lift over BAU is needed for this scenario
        uplift_pct = None
        if bau_weekly > 0 and promo_weeks > 0:
            weekly_incr = units / promo_weeks
            uplift_pct = round((weekly_incr / bau_weekly) * 100, 0)
        scenarios.append(ScenarioRow(
            label=label,
            units=units,
            cm3_cash=round(cm3, 2),
            cm3_cash_pct=round(pct, 1) if pct is not None else None,
            grade=grade,
            uplift_pct=uplift_pct,
        ))

    return scenarios


def _grade_from_pct(cm3_cash_pct: Optional[float], cm3_cash: float) -> str:
    if cm3_cash < 0:
        return "REJECT"
    if cm3_cash_pct is None:
        return "C"
    if cm3_cash_pct < GRADE_C_FLOOR:
        return "REJECT"
    if cm3_cash_pct >= GRADE_A_FLOOR:
        return "A"
    if cm3_cash_pct >= GRADE_B_FLOOR:
        return "B"
    return "C"


def model_influencer_pl(
    discount_pct: float,
    commission_pct: float,
    commercials: InfluencerCommercials,
    estimated_units: float = 0.0,
) -> InfluencerPL:
    """Per-unit influencer CM3 waterfall — matches the reference P&L sheet.

    Net Revenue = Gross − Trade Discount − Channel Margin − Marketplace
    Commission − Returns − Tax. Returns are a % of (Gross − Trade Discount).
    Commission & Warranty are a % of Net Revenue; Marketing, Channel Margin,
    Marketplace Commission, Tax and Payment Gateway are a % of Gross.
    CM3% is a % of Net Revenue — volume-invariant — and drives the grade.
    """
    gross = commercials.retail_price_usd
    cogs = commercials.cogs_usd
    trade_discount = (discount_pct / 100.0) * gross
    channel_margin = (commercials.channel_margin_pct / 100.0) * gross
    marketplace_commission = (commercials.marketplace_commission_pct / 100.0) * gross
    returns = (commercials.return_rate_pct / 100.0) * (gross - trade_discount)
    tax = (commercials.tax_pct / 100.0) * gross
    net_revenue = (gross - trade_discount - channel_margin
                   - marketplace_commission - returns - tax)
    commission = (commission_pct / 100.0) * net_revenue
    marketing = (commercials.marketing_pct / 100.0) * gross
    warranty = (commercials.warranty_pct / 100.0) * net_revenue
    payment_gateway = (commercials.payment_gateway_pct / 100.0) * gross
    total_sm = commission + marketing
    total_warranty = warranty
    cm3_per_unit = (net_revenue - cogs - total_sm - total_warranty
                    - payment_gateway)
    cm3_pct = (cm3_per_unit / net_revenue * 100.0) if net_revenue > 0 else 0.0
    cm3_cash = cm3_per_unit * estimated_units
    return InfluencerPL(
        gross=gross, trade_discount=trade_discount,
        channel_margin=channel_margin,
        marketplace_commission=marketplace_commission,
        returns=returns, tax=tax, net_revenue=net_revenue, cogs=cogs,
        commission=commission, marketing=marketing, warranty=warranty,
        payment_gateway=payment_gateway,
        total_sm=total_sm, total_warranty=total_warranty,
        cm3_per_unit=cm3_per_unit, cm3_pct=cm3_pct,
        estimated_units=estimated_units, cm3_cash=cm3_cash,
        # Grade on cm3_pct (volume-invariant) with cm3_per_unit for the
        # <0 reject guard — both are per-unit so the grade never depends
        # on volume.
        grade=_grade_from_pct(cm3_pct, cm3_per_unit),
    )


# ---------------------------------------------------------------------------
# 4. Sensitivity analysis
# ---------------------------------------------------------------------------

_RETURN_RATE_FLOOR = 0.0     # returns can't realistically go below [REDACTED]  # REDACTED
_TD_SHARE_FLOOR = 0.0        # negotiate TD share down to [REDACTED]  # REDACTED


def _build_sensitivity(
    units: int,
    request: PromoRequest,
    econ: MarketEconomics,
    marketing_spend: float,
    base_pl: PLImpact,
    soa_per_unit: float = 0.0,
    addon_cogs_per_unit: float = 0.0,
) -> list[SensitivityRow]:
    """Show realistic levers to improve CM3-Cash, with a combined recommendation."""
    rows = []
    discount_pct = request.discount_pct or 0
    buy_price = econ.buy_price_usd
    retail_price = econ.retail_price_usd
    td_share = econ.trade_discount_brand_pct
    ret_rate = econ.return_rate
    brand_bears = request.brand_bears_per_unit or 0.0
    rebate_pu = buy_price * (request.rebate_pct / 100.0) if request.rebate_pct else 0.0

    def _calc(disc_pct, ret, td_pct, mktg, label_from, label_to, lever):
        if brand_bears > 0:
            td_cost = units * brand_bears
        else:
            td_cost = units * retail_price * (disc_pct / 100) * td_pct
        ret_cost = units * ret * buy_price
        nr = units * buy_price - td_cost - ret_cost
        gm = nr - units * (COGS_PER_RING_USD + addon_cogs_per_unit)
        # Warranty: [REDACTED] of net revenue. Mirrors model_pl_impact:595,
        # model_spiv_pl_impact:732, _build_spiv_scenarios:812, _build_scenarios:863.
        # Was missing here — see commit 7bdb107.
        warranty = 0.0 * nr  # REDACTED: warranty rate
        soa = units * soa_per_unit
        rebate = units * rebate_pu
        cm3 = gm - mktg - soa - rebate - warranty
        pct = (cm3 / nr * 100) if nr > 0 else 0
        return SensitivityRow(
            lever=lever, value_from=label_from, value_to=label_to,
            cm3_cash=round(cm3, 2), cm3_cash_pct=round(pct, 1),
            grade=_grade_from_pct(pct, cm3), delta=round(cm3 - base_pl.cm3_cash, 2),
        )

    # Track which levers are actionable for the combined scenario
    combo_disc = discount_pct
    combo_ret = ret_rate
    combo_td = td_share
    combo_mktg = marketing_spend

    # --- Individual levers ---

    # 1. Reduce discount by [REDACTED]pp (skip when the brand bears is a fixed $ — discount % is not the lever)
    if discount_pct > 0 and not brand_bears:  # REDACTED
        lower_disc = discount_pct - 0  # REDACTED
        rows.append(_calc(lower_disc, ret_rate, td_share, marketing_spend,
                          f"{discount_pct:.0f}%", f"{lower_disc:.0f}%", "Reduce discount"))
        combo_disc = lower_disc

    # 2. Reduce returns to floor (only if above 15%)
    if ret_rate > _RETURN_RATE_FLOOR:
        target_ret = _RETURN_RATE_FLOOR
        rows.append(_calc(discount_pct, target_ret, td_share, marketing_spend,
                          f"{ret_rate*100:.0f}%", f"{target_ret*100:.0f}%", "Reduce returns"))
        combo_ret = target_ret

    # 3. Negotiate TD share to [REDACTED] (only if above [REDACTED] AND not using fixed bears)
    if td_share > _TD_SHARE_FLOOR and not brand_bears:
        rows.append(_calc(discount_pct, ret_rate, _TD_SHARE_FLOOR, marketing_spend,
                          f"{td_share*100:.0f}%", f"{_TD_SHARE_FLOOR*100:.0f}%",
                          "Negotiate TD share"))  # REDACTED
        combo_td = _TD_SHARE_FLOOR

    # 4. Cut marketing spend by 50%
    if marketing_spend > 0:
        half_mktg = marketing_spend * 0.5
        rows.append(_calc(discount_pct, ret_rate, td_share, half_mktg,
                          f"${marketing_spend:,.0f}", f"${half_mktg:,.0f}",
                          "Cut marketing 50%"))
        combo_mktg = half_mktg

    # --- Recommended combo: all actionable levers combined ---
    # Only include if at least 2 levers changed
    changes = []
    if combo_disc != discount_pct:
        changes.append(f"disc {combo_disc:.0f}%")
    if combo_ret != ret_rate:
        changes.append(f"returns {combo_ret*100:.0f}%")
    if combo_td != td_share:
        changes.append(f"TD {combo_td*100:.0f}%")
    if combo_mktg != marketing_spend:
        changes.append(f"mktg ${combo_mktg:,.0f}")

    if len(changes) >= 2:
        rows.append(_calc(combo_disc, combo_ret, combo_td, combo_mktg,
                          "current", " + ".join(changes),
                          "Recommended combo"))

    return rows


# ---------------------------------------------------------------------------
# 5. Risk flags
# ---------------------------------------------------------------------------

def assess_risk_flags(
    request: PromoRequest,
    history: PromoHistory,
    velocity: VelocityCheck,
    pl: PLImpact,
    ad_metrics=None,
    econ=None,
) -> list[RiskFlag]:
    flags = []

    if request.discount_pct and request.discount_pct >= 0:  # REDACTED
        flags.append(RiskFlag("HIGH_DISCOUNT",
                              f"Discount {request.discount_pct}% — margin erosion"))

    if history.sample_count == 0:
        if normalize_region(request.region or "") in NEW_MARKETS:
            flags.append(RiskFlag("NEW_MARKET",
                                  "New market — no promo history yet; estimate uses sell-out baseline only"))
        else:
            flags.append(RiskFlag("NO_HISTORY",
                                  "No promo history — lift estimate unreliable"))

    # Buy price came from MARKET_DEFAULTS hardcoded placeholder, not the sheet.
    # Reviewer should know the economics may be stale.
    if econ is not None and getattr(econ, "buy_price_source", "missing") == "default":
        flags.append(RiskFlag(
            "BUY_PRICE_FROM_DEFAULT",
            f"Buy price ${econ.buy_price_usd:.0f} is from MARKET_DEFAULTS fallback — "
            f"not the Bot Input Variables sheet. May be stale. Populate the sheet row "
            f"for {econ.market}/{request.retailer or 'all'} to get a sharper read."
        ))

    # SPIV cost per unit exceeds margin per unit — the promo is structurally impossible to break even on.
    spiv_pu = request.spiv_per_unit or 0.0
    if request.promo_type == "spiv" and spiv_pu > 0 and pl.margin_per_unit and spiv_pu > pl.margin_per_unit:
        flags.append(RiskFlag(
            "SPIV_EXCEEDS_MARGIN",
            f"SPIV ${spiv_pu:.2f}/unit > margin ${pl.margin_per_unit:.2f}/unit — "
            f"every ring sold loses ${spiv_pu - pl.margin_per_unit:.2f}. "
            f"No volume can recover this. Renegotiate or reject."
        ))

    if history.fallback_source:
        flags.append(RiskFlag("CROSS_REGION_FALLBACK",
                              f"Using cross-region data: {history.fallback_source}"))

    if velocity.direction == "down":
        flags.append(RiskFlag("DECLINING_VELOCITY",
                              f"Sell-through declining ({velocity.pct_change:+.1f}%)"))

    if pl.marketing_spend > 0 and history.sample_count < 3:  # REDACTED
        flags.append(RiskFlag("HIGH_SPEND_LOW_DATA",
                              f"${pl.marketing_spend:,.0f} spend with limited promo track record"))

    # Ad cannibalization: only relevant when the promo IS on Amazon
    retailer_lower = (request.retailer or "").lower().strip()
    if (ad_metrics and ad_metrics.available and ad_metrics.is_heavy_ad_period
            and retailer_lower.startswith("amazon")):
        flags.append(RiskFlag(
            "AD_CANNIBALIZATION",
            f"Heavy Amazon ad spend (ROAS {ad_metrics.roas}x) — promo may cannibalize paid traffic"
        ))

    # Market demand signal: if Amazon ROAS is declining in this geo, flag softening demand
    if (ad_metrics and ad_metrics.available and ad_metrics.roas_trend == "down"
            and ad_metrics.roas_prior_30d and ad_metrics.roas):
        flags.append(RiskFlag(
            "MARKET_DEMAND_SOFTENING",
            f"Amazon ad ROAS in this market declining ({ad_metrics.roas_prior_30d}x → {ad_metrics.roas}x) — suggests softening demand"
        ))

    return flags


# ---------------------------------------------------------------------------
# 6. Conditions
# ---------------------------------------------------------------------------

def generate_conditions(
    request: PromoRequest,
    risk_flags: list[RiskFlag],
    history: PromoHistory,
    velocity: VelocityCheck,
    pl: PLImpact,
) -> list[str]:
    conditions = []

    for flag in risk_flags:
        if flag.flag == "HIGH_DISCOUNT":
            conditions.append(f"Discount is aggressive ({request.discount_pct}%) — confirm margin floor acceptable")
        elif flag.flag == "NEW_MARKET":
            conditions.append("New market — no historical comp; treat as pilot, set clear success metrics before approving full spend")
        elif flag.flag == "NO_HISTORY":
            conditions.append("No historical data — treat as pilot with clear success metrics")
        elif flag.flag == "CROSS_REGION_FALLBACK":
            conditions.append(f"Lift estimate from cross-region comp ({history.fallback_source}) — lower confidence")
        elif flag.flag == "DECLINING_VELOCITY":
            conditions.append("Sell-through declining — promo may mask structural demand issue")
        elif flag.flag == "HIGH_SPEND_LOW_DATA":
            conditions.append("Large spend with thin track record — consider smaller test first")
        elif flag.flag == "AD_CANNIBALIZATION":
            conditions.append(
                "Active heavy Amazon ad spend — confirm promo targets new customers, "
                "not existing intent already captured by ads"
            )
        elif flag.flag == "MARKET_DEMAND_SOFTENING":
            conditions.append(
                "Amazon ad efficiency declining in this market — consumer demand may be "
                "softening across channels. Factor into volume expectations."
            )

    # Seasonality warning
    event_text = request.raw_text.lower()
    event_keywords = {
        "mother's day": "Mother's Day", "father's day": "Father's Day",
        "valentine": "Valentine's Day", "christmas": "Christmas",
        "black friday": "Black Friday", "bfcm": "BFCM",
        "new year": "New Year", "prime day": "Prime Day",
        "easter": "Easter", "spring": "Spring",
    }
    for kw, display_name in event_keywords.items():
        if kw in event_text:
            has_comp = any(kw in (r.season_event or "").lower() for r in history.records)
            if not has_comp:
                conditions.append(f"{display_name} is unproven for {request.retailer or 'this retailer'} — no historical comp")
            break

    # Monitoring threshold
    if velocity.weekly_run_rate > 0:
        daily_min = max(0, velocity.weekly_run_rate // 7)  # REDACTED
        conditions.append(f"Monitor first 3 days; if < {daily_min} units/day, consider reallocating")

    return conditions


# ---------------------------------------------------------------------------
# 6b. Volume confidence assessment
# ---------------------------------------------------------------------------

def _assess_volume_confidence(history: PromoHistory, request: PromoRequest) -> str:
    """Determine if volume estimate is reliable enough to skip scenario table.

    Returns "low" when:
    - Few historical records (< 3)
    - Cross-region fallback in use
    - Big discount gap between target and closest comp (> 10pp)
    - Unproven seasonal event (no comp with matching event keyword)
    - Non-discount promo type (SPIV, newsletter) with no same-type comp
    """
    if history.sample_count < 3:
        return "low"
    if history.fallback_source:
        return "low"

    promo_type = request.promo_type or "promo"

    # For non-discount promos, check if we have same-type comps
    if promo_type not in ("promo", ""):
        same_type_comps = [r for r in history.records if r.promo_type == promo_type]
        if len(same_type_comps) < 2:
            return "low"

    target_disc = request.discount_pct or 0
    if target_disc > 0 and history.comparable_promo and history.comparable_promo.discount_pct is not None:
        gap = abs(target_disc - history.comparable_promo.discount_pct)
        if gap > 10:
            return "low"
    # Check for unproven seasonal event
    event_keywords = ["easter", "mother's day", "father's day", "valentine",
                      "christmas", "black friday", "bfcm", "prime day", "spring"]
    text_lower = request.raw_text.lower()
    for kw in event_keywords:
        if kw in text_lower:
            has_comp = any(kw in (r.season_event or "").lower() for r in history.records)
            if not has_comp:
                return "low"
            break
    return "high"


# ---------------------------------------------------------------------------
# 6c. Recommendations and verdict
# ---------------------------------------------------------------------------

def _sop_advisories(request: PromoRequest) -> list[str]:
    """SOP v0.4 governance advisories — output-only, never affect the grade."""
    from datetime import date, datetime
    out: list[str] = []

    # Discount-band guardrail: SOP peak public maximum = [REDACTED]
    disc = request.discount_pct or 0
    if disc > 0:  # REDACTED
        out.append(
            f":triangular_flag_on_post: Discount {disc:g}% exceeds the [REDACTED] peak public "
            f"maximum — CBO exception only (SOP v0.4 discount guardrails)."
        )

    # Approval lead-time: T-4 retail / T-2 D2C+Amazon+marketplace / T-1 influencer
    # Submit-date proxy: bot evaluates within seconds of submission, so date.today() ≈ submit date (advisory-only, never affects grade).
    start = None
    if request.dates:
        try:
            start = datetime.strptime(request.dates[0], "%Y-%m-%d").date()
        except (ValueError, TypeError):
            start = None
    if start is not None:
        # Influencer/retail signals are not channel-only: influencer often
        # arrives as a marketing type or in free text, and retail submissions
        # may name a retailer without setting the Channel field. Inspect all
        # of them. Order matters: influencer before retail.
        ch = " ".join((
            request.channel or "",
            request.marketing_scope_label or "",
            request.promo_type or "",
            request.raw_text or "",
        )).lower()
        _retailer_l = (request.retailer or "").lower()
        _has_d2c_amzn = any(
            t in ch or t in _retailer_l for t in ("d2c", "amazon", "marketplace")
        )
        _is_retailish = ("retail" in ch) or (bool(request.retailer) and not _has_d2c_amzn)
        if "influencer" in ch:
            window_days, window_label = 7, "T-1 week (influencer)"
        elif _is_retailish:
            window_days, window_label = 28, "T-4 weeks (retail)"
        # Default: D2C / Amazon / unknown channel → T-2 weeks.
        else:
            window_days, window_label = 14, "T-2 weeks (D2C/Amazon/marketplace)"
        lead_days = (start - date.today()).days
        if lead_days < window_days:
            out.append(
                f":triangular_flag_on_post: Submitted {lead_days}d before start — inside the "
                f"{window_label} lead time. Requires CBO exception (SOP v0.4 approval lead times)."
            )

    return out


def _generate_recommendations(
    request: PromoRequest,
    pl: PLImpact,
    econ: MarketEconomics,
    history: PromoHistory,
    velocity: VelocityCheck,
    grade: str,
    ad_metrics=None,
) -> list[str]:
    """Generate actionable recommendations based on the evaluation."""
    recs = []

    # If grade is not A, suggest the best sensitivity lever
    if pl.sensitivity and grade != "A":
        combo = next((s for s in pl.sensitivity if s.lever == "Recommended combo"), None)
        if combo and combo.grade in ("A", "B"):
            recs.append(f"Negotiate to {combo.value_to} → Grade {combo.grade} (CM3 +${combo.delta:,.0f})")

        # Single best lever
        best = None
        for s in pl.sensitivity:
            if s.lever != "Recommended combo" and (best is None or s.cm3_cash > best.cm3_cash):
                best = s
        if best and best.grade in ("A", "B") and (not combo or best.lever != combo.lever):
            recs.append(f"Alternatively: {best.lever} {best.value_from} → {best.value_to} → Grade {best.grade}")

    # Declining velocity warning
    if velocity.direction == "down" and velocity.pct_change and velocity.pct_change < 0:  # REDACTED
        recs.append("Velocity declining — consider shorter duration or smaller test first")

    # High return rate
    if econ.return_rate > 0.0:  # REDACTED
        recs.append(f"Return rate is {econ.return_rate*100:.0f}% — negotiate return caps or tighter activation tracking")

    # Cross-region data
    if history.fallback_source:
        recs.append(f"Using cross-region data ({history.fallback_source}) — validate with local team before committing")

    # Thin history
    if history.sample_count < 3:
        recs.append("Limited promo history — run as pilot with clear KPIs")

    # Marketing spend efficiency
    if pl.marketing_spend > 0 and pl.roas and pl.roas < 0:  # REDACTED
        recs.append(f"ROAS {pl.roas}x is low — consider reducing spend or spreading over longer period")

    # Market context from Amazon ad data
    if ad_metrics and ad_metrics.available:
        # Corroborate velocity decline with market-wide signal
        if velocity.direction == "down" and ad_metrics.roas_trend == "down":
            recs.append(
                f"Both retailer velocity and Amazon ad ROAS ({ad_metrics.roas_prior_30d}x → {ad_metrics.roas}x) "
                f"are declining — likely market-wide softness, not retailer-specific"
            )
        # Strong market but weak retailer → retailer-specific issue
        elif velocity.direction == "down" and ad_metrics.roas_trend in ("up", "flat") and ad_metrics.roas and ad_metrics.roas >= 0.0:  # REDACTED
            recs.append(
                f"Amazon ads healthy in this market ({ad_metrics.roas}x ROAS) but retailer velocity declining — "
                f"investigate retailer-specific issues before investing"
            )

    return recs


def _generate_verdict(
    request: PromoRequest,
    pl: PLImpact,
    grade: str,
    marketing_spend: float,
) -> str:
    """Generate a one-line verdict with rationale."""
    grade_labels = {"A": "APPROVE", "B": "CONDITIONAL APPROVE", "C": "REVIEW NEEDED", "REJECT": "DO NOT APPROVE"}
    label = grade_labels.get(grade, grade)

    if grade == "A":
        return f"{label}. CM3-Cash ${pl.cm3_cash:,.0f} ({pl.cm3_cash_pct}%) on ${pl.total_promo_investment:,.0f} investment."
    elif grade == "B":
        return f"{label}. CM3-Cash ${pl.cm3_cash:,.0f} ({pl.cm3_cash_pct}%) — viable with conditions above."
    elif grade == "REJECT":
        if pl.cm3_cash < 0:
            return f"{label}. Negative CM3-Cash ${pl.cm3_cash:,.0f} — every incremental unit loses money."
        return (
            f"REJECT — requires explicit CBO exception. "
            f"CM3-Cash ${pl.cm3_cash:,.0f} ({pl.cm3_cash_pct}%) is below the {GRADE_C_FLOOR:g}% floor (SOP v0.4)."
        )
    else:  # C
        if pl.incremental_units == 0:
            return f"{label}. Insufficient data to estimate volume — fill in promo history."
        return f"{label}. CM3-Cash ${pl.cm3_cash:,.0f} ({pl.cm3_cash_pct}%) — needs better terms to justify."


def _get_amazon_benchmark(discount_pct: float, ad_metrics) -> tuple:
    """Get Amazon promo lift for the given discount level as a benchmark.

    Returns (lift_multiplier, source_label) or (None, "").
    """
    if not discount_pct or not ad_metrics or not ad_metrics.available:
        return None, ""
    pl = ad_metrics.promo_lift
    if not pl or not pl.available:
        return None, ""

    # Map discount to bracket, with fallback to nearest available
    if discount_pct >= 25:
        brackets = ["25+", "15-25", "5-15"]
    elif discount_pct >= 15:
        brackets = ["15-25", "5-15", "25+"]
    elif discount_pct >= 5:
        brackets = ["5-15", "15-25", "25+"]
    else:
        return None, ""

    lift = None
    for b in brackets:
        lift = pl.lift_by_bracket.get(b)
        if lift is not None:
            break
    if lift is None:
        return None, ""

    source = "Amazon (similar geo)" if pl.fallback_source else "Amazon (direct geo)"
    return lift, source


# ---------------------------------------------------------------------------
# 7. Master evaluation
# ---------------------------------------------------------------------------

def _estimate_units_from_roas(
    marketing_spend_usd: float,
    retail_price: float,
    ad_campaign=None,
    ad_metrics=None,
    ad_channels: list = None,
    monthly_ad=None,
    target_month: int = 0,
) -> tuple[int, str, str]:
    """Estimate units from ROAS for marketing investment promos (SPA/SBA/SDA).

    Fallback chain (most contextual first):
    1. Sheet SPA/SBA tab (same retailer, same channel)
    2. Amazon monthly ROAS by ad type for target month (month × type granularity)
    3. Amazon monthly blended ROAS for target month
    4. Amazon trailing 30-day ROAS by ad type
    5. Amazon trailing 30-day blended ROAS

    Returns (units, confidence, source_label).
    """
    MONTH_NAMES = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
                   "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    if marketing_spend_usd <= 0 or retail_price <= 0:
        return 0, "low", ""

    # --- Source 1: Historical sheet tab (most reliable — same retailer, same channel) ---
    if ad_campaign and ad_campaign.available and ad_campaign.roas and ad_campaign.roas > 0:
        roas = ad_campaign.roas * 0.0  # REDACTED: conservative factor
        expected_sales = marketing_spend_usd * roas
        units = max(1, int(round(expected_sales / retail_price)))
        logger.info(f"ROAS-based volume (sheet tab '{ad_campaign.tab_name}'): "
                     f"ROAS={ad_campaign.roas:.1f}x → conservative {roas:.1f}x, "
                     f"${marketing_spend_usd:.0f} spend → ${expected_sales:.0f} sales → {units} units")
        return units, "medium", f"Sheet: {ad_campaign.tab_name} (ROAS {ad_campaign.roas:.1f}x)"

    # --- Source 2: Monthly Amazon ROAS by ad type for target month ---
    # Most contextual Amazon source: same month + same ad type from last year
    if (monthly_ad and monthly_ad.available and target_month
            and monthly_ad.target_month_roas_by_type and ad_channels):
        channel_roas = []
        for ch in (ad_channels or []):
            r = monthly_ad.target_month_roas_by_type.get(ch)
            if r and r > 0:
                channel_roas.append(r)
        if channel_roas:
            avg_roas = sum(channel_roas) / len(channel_roas)
            roas = avg_roas * 0.0  # Amazon → retailer conservative factor  # REDACTED
            expected_sales = marketing_spend_usd * roas
            units = max(1, int(round(expected_sales / retail_price)))
            month_name = MONTH_NAMES[target_month] if target_month <= 12 else "?"
            ch_labels = ", ".join(ad_channels)
            logger.info(f"ROAS-based volume ({month_name} Amazon {ch_labels}): "
                         f"monthly ROAS {avg_roas:.1f}x → conservative {roas:.1f}x → {units} units")
            return units, "medium", f"Amazon {month_name}: {ch_labels} (ROAS {avg_roas:.1f}x)"

    # --- Source 3: Monthly Amazon blended ROAS for target month ---
    if (monthly_ad and monthly_ad.available and target_month
            and monthly_ad.target_month_roas and monthly_ad.target_month_roas > 0):
        roas = monthly_ad.target_month_roas * 0.0  # REDACTED: conservative factor
        expected_sales = marketing_spend_usd * roas
        units = max(1, int(round(expected_sales / retail_price)))
        month_name = MONTH_NAMES[target_month] if target_month <= 12 else "?"
        logger.info(f"ROAS-based volume ({month_name} Amazon blended): "
                     f"ROAS {monthly_ad.target_month_roas:.1f}x → conservative {roas:.1f}x → {units} units")
        return units, "low", f"Amazon {month_name} blended (ROAS {monthly_ad.target_month_roas:.1f}x)"

    # --- Source 4: Trailing 30-day Amazon ROAS by ad type ---
    if ad_metrics and ad_metrics.available and ad_metrics.roas_by_type and ad_channels:
        channel_roas = []
        channel_spend = []
        for ch in (ad_channels or []):
            r = ad_metrics.roas_by_type.get(ch)
            s = ad_metrics.spend_by_type.get(ch, 0)
            if r is not None and r > 0:
                channel_roas.append(r)
                channel_spend.append(s)

        if channel_roas:
            if sum(channel_spend) > 0:
                roas = sum(r * s for r, s in zip(channel_roas, channel_spend)) / sum(channel_spend)
            else:
                roas = sum(channel_roas) / len(channel_roas)
            roas *= 0.0  # REDACTED: conservative factor
            expected_sales = marketing_spend_usd * roas
            units = max(1, int(round(expected_sales / retail_price)))
            ch_labels = ", ".join(ad_channels)
            logger.info(f"ROAS-based volume (Amazon 30d {ch_labels}): "
                         f"avg ROAS → conservative {roas:.1f}x → {units} units")
            return units, "low", f"Amazon 30d: {ch_labels} (ROAS ~{roas:.1f}x)"

    # --- Source 5: Trailing 30-day Amazon blended ROAS ---
    if ad_metrics and ad_metrics.available and ad_metrics.roas and ad_metrics.roas > 0:
        roas = ad_metrics.roas * 0.0  # REDACTED: conservative factor
        expected_sales = marketing_spend_usd * roas
        units = max(1, int(round(expected_sales / retail_price)))
        logger.info(f"ROAS-based volume (Amazon 30d blended): "
                     f"blended ROAS {ad_metrics.roas:.1f}x → conservative {roas:.1f}x → {units} units")
        return units, "low", f"Amazon 30d blended (ROAS ~{roas:.1f}x)"

    return 0, "low", ""


def _combined_diminishing_factor(request: PromoRequest) -> float:
    """Diminishing returns factor when both a consumer discount and a fixed marketing spend are active.

    The consumer who buys because of the discount is partly the same person the ad reaches —
    showing them both signals doesn't double the conversion lift.

    [REDACTED] — same channel: retailer discount + retailer co-op SPA at the same store.
             Heavy audience overlap; the ad just reminds a consumer already primed by the price.
    [REDACTED] — cross channel: physical retail discount + Amazon DSP / D2C spend, or vice versa.
             Lighter overlap; the ad reaches a distinct segment that hadn't encountered the discount.
    """
    retailer = (request.retailer or "").lower()
    ad_channels = request.ad_channels or []

    # Amazon-side spend: request specifies Amazon ad types (SponsoredProducts, etc.)
    amazon_spend = any("sponsored" in ch.lower() for ch in ad_channels)
    # Physical retailer: the discount is at a brick-and-mortar / non-Amazon store
    physical_retailer = bool(retailer) and "amazon" not in retailer and "online" not in retailer

    if amazon_spend and physical_retailer:
        # Spend on Amazon, discount at physical retail → cross-channel → lighter overlap
        return 0.0  # REDACTED
    # Default: same channel (both at retailer, or both on Amazon, or unclear)
    return 0.0  # REDACTED


def _influencer_promo_weeks(request) -> float:
    """Promo-window length in weeks from request.dates; default 2.0 when
    dates are absent or unparseable."""
    from datetime import datetime
    dates = getattr(request, "dates", None) or []
    if len(dates) >= 2:
        try:
            d1 = datetime.strptime(dates[0], "%Y-%m-%d")
            d2 = datetime.strptime(dates[1], "%Y-%m-%d")
            days = (d2 - d1).days
            if days > 0:
                return days / 7.0
        except (ValueError, TypeError):
            pass
    return 2.0


def evaluate_promo(
    request: PromoRequest,
    history: PromoHistory,
    econ: MarketEconomics,
    velocity: VelocityCheck,
    promo_weeks: float = 1.0,
    target_month: int = 0,
    ad_metrics=None,
    ad_campaign=None,
    monthly_ad=None,
    seasonal_lift=None,
) -> PromoEvaluation:
    """Run the full evaluation pipeline."""

    # 2026-05-20: Log-only Marketing Types (Sales Contest, POS). Skip
    # financial evaluation; main.py still writes the submission to the
    # Marketing Tracker. POS was previously routed to a display-investment
    # ROI eval; per the 2026-05-20 spec it is now log-only (no display-investment grade).
    if _is_no_eval(request):
        return PromoEvaluation(
            verdict="LOGGED",
            grade="LOGGED",
            commentary=(
                "Logged to Tracker. This Marketing Type is not "
                "evaluated by the bot."
            ),
        )

    # 2026-05-21: Channel=Influencer — channel-level CM3 eval. Deterministic
    # per-unit P&L from the Influencer commercials tab; volume from past promos
    # scales cm3_cash only (the grade is volume-invariant).
    if (request.channel or "").lower() == "influencer":
        commercials = get_influencer_commercials()
        bau_per_week, n_bau_weeks, _ = get_influencer_bau()
        if bau_per_week > 0:
            window_weeks = _influencer_promo_weeks(request)
            avg_units = bau_per_week * window_weeks
            n_past = n_bau_weeks
            volume_source = "bau"
        else:
            avg_units, n_past = get_influencer_volume_estimate()
            volume_source = "past_promos"
        commission = request.commission_pct or commercials.commission_pct
        pl = model_influencer_pl(
            discount_pct=request.discount_pct or 0.0,
            commission_pct=commission,
            commercials=commercials,
            estimated_units=avg_units,
        )
        flags = []
        if commercials.source == "default":
            flags.append(RiskFlag(
                "COMMERCIALS_FROM_DEFAULT",
                "Influencer economics are placeholder defaults — populate the "
                "'Influencer commercials' tab for an accurate read."))
        if n_past == 0:
            flags.append(RiskFlag(
                "NO_PAST_PROMOS",
                "No past influencer promos logged — CM3-cash uses 0 units; "
                "the CM3% grade is unaffected (volume-invariant)."))
        # A and B both → APPROVE (SOP v0.4 bands: A ≥[REDACTED], B ≥[REDACTED] CM3% — both
        # clear the bar); C → CONDITIONAL; REJECT → REJECT.
        verdict = {"A": "APPROVE", "B": "APPROVE", "C": "CONDITIONAL",
                   "REJECT": "REJECT"}.get(pl.grade, pl.grade)
        if volume_source == "bau":
            volume_phrase = (f"Volume ref: {avg_units:.0f} units "
                             f"(BAU run-rate × {window_weeks:.0f}-week window).")
        else:
            volume_phrase = (f"Volume ref: {avg_units:.0f} units "
                             f"from {n_past} past promo(s).")
        return PromoEvaluation(
            verdict=verdict,
            grade=pl.grade,
            commentary=(f"Influencer promo — {request.discount_pct or 0:.0f}% discount, "
                        f"{commission:.0f}% commission → CM3 ${pl.cm3_per_unit:.2f}/unit "
                        f"({pl.cm3_pct:.1f}%). {volume_phrase}"),
            influencer_pl=pl,
            risk_flags=flags,
            conditions=[],
        )

    # Check if buy price is available — can't evaluate without it
    promo_type = request.promo_type or "promo"
    missing_data = []
    if econ.buy_price_usd == 0:
        missing_data.append("buy price")
    # Only require discount % for discount-based promos (not SPIV, newsletter, etc.)
    if promo_type in ("promo", "") and not request.discount_pct:
        missing_data.append("discount %")

    if missing_data:
        return PromoEvaluation(
            grade="CANNOT_EVALUATE",
            grade_emoji=":warning:",
            promo_history=history,
            velocity=velocity,
            market_economics=econ,
            ad_campaign=ad_campaign,
            conditions=[
                f"Cannot evaluate — missing required data: {', '.join(missing_data)}.",
                "Fix: populate the Bot Input Variables sheet for this retailer/region.",
            ],
            verdict=(
                f"Cannot evaluate. Missing: {', '.join(missing_data)}. "
                f"No grade assigned because the bot has no economics to model against."
            ),
            data_quality=f"missing: {', '.join(missing_data)}",
        )

    # NOTE: POS / display / display-investment variants are handled by _is_no_eval()
    # at the top of this function and never reach here.

    # Build seasonal index
    history.seasonal_index = build_seasonal_index(history.records)

    # Find comparable — promo_type is a key scoring factor
    target_duration_days = int(round(promo_weeks * 7))
    history.comparable_promo = _find_best_comparable(
        history.records, request.discount_pct or 0, target_month,
        target_duration_days, promo_type,
    )

    # Override TD share if the request specifies the brand's funding %
    if request.brand_funding_pct and request.discount_pct and request.discount_pct > 0:
        override_td = request.brand_funding_pct / request.discount_pct
        logger.info(f"TD override from request: the brand funds {request.brand_funding_pct}% of "
                     f"{request.discount_pct}% discount = {override_td*100:.1f}% TD share "
                     f"(was {econ.trade_discount_brand_pct*100:.0f}%)")
        econ.trade_discount_brand_pct = override_td

    # Note: brand_bears_per_unit is handled directly in model_pl_impact —
    # no TD% conversion needed. The flat $ amount IS the trade discount.
    if request.brand_bears_per_unit:
        logger.info(f"the brand bears ${request.brand_bears_per_unit}/unit — using as direct TD, "
                     f"bypassing sheet TD share ({econ.trade_discount_brand_pct*100:.0f}%)")

    # Estimate incremental units
    # Convert marketing spend from local currency to USD only if non-USD
    # $ = USD (no conversion), EUR/€/£ = local currency (apply FX)
    fx = econ.fx_rate_to_usd if econ.fx_rate_to_usd > 0 else 1.0
    amount_fx = fx if request.discount_currency != "USD" else 1.0
    marketing_spend = (request.discount_amount or 0) * amount_fx
    # SOA/SPIV per-unit costs: use same currency as the amount in the message
    # Rebate is handled separately in model_pl_impact (not mixed with SOA)
    soa_per_unit = (request.soa_per_unit or 0.0) * amount_fx
    spiv_per_unit = (request.spiv_per_unit or 0.0) * amount_fx

    # For training promos: event duration is 2-3 days but staff training impact
    # persists for ~8 weeks as trained staff proactively pitch the brand. Use the impact
    # window for unit estimation, not the event duration — otherwise we get near-zero
    # incremental (0.29 weeks × [REDACTED] lift × baseline ≈ nothing).
    impact_weeks = promo_weeks
    if promo_type == "training" and promo_weeks < 2.0:
        impact_weeks = 8.0
        logger.info(
            f"Training promo: estimation window {promo_weeks:.2f}w (event) → "
            f"{impact_weeks}w (expected staff impact period)"
        )

    # --- Volume estimation: ROAS-based for marketing investments, lift-based otherwise ---
    roas_source = ""
    if promo_type == "ecom_campaign" and marketing_spend > 0:
        # Marketing investment: estimate volume from ROAS (sheet tab → monthly Amazon → blended)
        incremental, volume_confidence, roas_source = _estimate_units_from_roas(
            marketing_spend, econ.retail_price_usd,
            ad_campaign=ad_campaign, ad_metrics=ad_metrics,
            ad_channels=request.ad_channels,
            monthly_ad=monthly_ad, target_month=target_month,
        )
        if incremental == 0:
            # ROAS estimation failed — fall back to standard lift-based estimation
            incremental = estimate_incremental_units(
                history, request, impact_weeks, target_month,
                ad_metrics=ad_metrics, seasonal_lift=seasonal_lift,
            )
    elif promo_type in ("promo", "") and marketing_spend > 0 and (request.discount_pct or 0) > 0:
        # Combined discount + fixed spend (e.g. [REDACTED]% off + $[REDACTED] retailer SPA).
        # Estimate units from both sources, then apply a diminishing returns factor:
        # the consumer captured by the discount is partly the same one the ad reaches.
        #
        # Diminishing factor:
        #   0.65 = same channel (retailer discount + retailer co-op SPA — heavy overlap)
        #   0.75 = cross channel (physical discount + Amazon DSP/D2C spend — lighter overlap)
        disc_incremental = estimate_incremental_units(
            history, request, impact_weeks, target_month,
            ad_metrics=ad_metrics, seasonal_lift=seasonal_lift,
        )
        spend_incremental, _, roas_source = _estimate_units_from_roas(
            marketing_spend, econ.retail_price_usd,
            ad_campaign=ad_campaign, ad_metrics=ad_metrics,
            ad_channels=request.ad_channels,
            monthly_ad=monthly_ad, target_month=target_month,
        )
        if spend_incremental > 0:
            diminishing = _combined_diminishing_factor(request)
            incremental = disc_incremental + int(round(spend_incremental * diminishing))
            volume_confidence = "medium"
            logger.info(
                f"Combined discount+spend: disc={disc_incremental}u + "
                f"spend={spend_incremental}u × {diminishing} (diminishing) = {incremental}u total"
            )
        else:
            # No ROAS data — fall back to discount-only estimation; spend is still a P&L cost
            incremental = disc_incremental
            roas_source = ""
    else:
        incremental = estimate_incremental_units(
            history, request, impact_weeks, target_month,
            ad_metrics=ad_metrics, seasonal_lift=seasonal_lift,
        )

    # Determine volume confidence (override if ROAS-based estimation set it)
    if not roas_source:
        volume_confidence = _assess_volume_confidence(history, request)

    # P&L model — use SPIV-aware model for SPIV promos
    if promo_type == "spiv" and spiv_per_unit > 0:
        # Use best available baseline: velocity > history > 0
        baseline_weekly = velocity.weekly_run_rate or history.baseline_units_weekly or 0
        pl = model_spiv_pl_impact(
            incremental, request, econ, marketing_spend,
            spiv_per_unit, promo_weeks, volume_confidence, baseline_weekly,
        )
    else:
        addon_cogs = request.addon_cogs_per_unit or 0.0
        bau_weekly_val = velocity.weekly_run_rate or history.baseline_units_weekly or 0.0
        pl = model_pl_impact(
            incremental, request, econ, marketing_spend, soa_per_unit, volume_confidence,
            addon_cogs, bau_weekly=bau_weekly_val, promo_weeks=impact_weeks,
        )

    # If no incremental units could be estimated, refuse to grade rather than
    # silently downgrading. Distinguish "data missing" vs "unit economics broken":
    #   - margin_per_unit < 0  → REJECT (every ring would lose money regardless of volume)
    #   - else                 → CANNOT_EVALUATE with explicit reason
    if incremental == 0:
        if pl.margin_per_unit is not None and pl.margin_per_unit < 0:
            pl.grade = "REJECT"
            pl.verdict = (
                f"Reject on unit economics: margin/unit ${pl.margin_per_unit:.2f} is negative. "
                f"Every ring loses money — no volume can recover this. "
                f"Discount too deep / the brand bearing too much / returns too high."
            )
        else:
            pl.grade = "CANNOT_EVALUATE"
            reasons = []
            if history.sample_count == 0:
                reasons.append("no historical promo records for this retailer/region")
            if not (ad_metrics and ad_metrics.available and ad_metrics.roas):
                reasons.append("no Amazon ROAS data for the spend channel")
            if velocity.weekly_run_rate == 0 and history.baseline_units_weekly == 0:
                reasons.append("no baseline sell-out data")
            if not reasons:
                reasons.append("comp-history lift estimator returned 0 — see logs")
            pl.verdict = (
                "Cannot evaluate — insufficient data to estimate incremental volume. "
                f"Reasons: {'; '.join(reasons)}."
            )

    # Risk flags
    flags = assess_risk_flags(request, history, velocity, pl, ad_metrics=ad_metrics, econ=econ)

    # Grade is purely CM3-based — risk flags inform commentary and conditions only
    final_grade = pl.grade

    grade_emoji_map = {
        "A": ":large_green_circle:",
        "B": ":large_yellow_circle:",
        "C": ":red_circle:",
        "REJECT": ":no_entry:",
        "CANNOT_EVALUATE": ":warning:",
    }

    # Conditions
    conditions = generate_conditions(request, flags, history, velocity, pl)

    # Seasonality note
    season_note = ""
    if target_month and history.seasonal_index:
        idx_val = history.seasonal_index.get(target_month)
        if idx_val is not None:
            if idx_val < 0.8:
                season_note = f"Month {target_month} is a low season ({idx_val:.1f}x avg) — baseline adjusted down"
            elif idx_val > 1.2:
                season_note = f"Month {target_month} is a high season ({idx_val:.1f}x avg) — baseline adjusted up"

    # Data quality flag
    data_quality = "direct match"
    if history.fallback_source:
        data_quality = f"cross-region fallback: {history.fallback_source}"
    elif history.sample_count == 0:
        data_quality = "insufficient data"

    # Amazon benchmark lift for this discount level
    amz_benchmark_lift, amz_benchmark_source = _get_amazon_benchmark(request.discount_pct, ad_metrics)

    # Recommendations
    recommendations = _generate_recommendations(request, pl, econ, history, velocity, final_grade, ad_metrics=ad_metrics)
    recommendations = _sop_advisories(request) + recommendations

    # Verdict
    verdict = _generate_verdict(request, pl, final_grade, marketing_spend)

    # Override data quality for ROAS-based estimates
    if roas_source:
        data_quality = f"ROAS-based: {roas_source}"

    return PromoEvaluation(
        grade=final_grade,
        grade_emoji=grade_emoji_map.get(final_grade, ":white_circle:"),
        promo_history=history,
        pl_impact=pl,
        velocity=velocity,
        market_economics=econ,
        risk_flags=flags,
        conditions=conditions,
        recommendations=recommendations,
        verdict=verdict,
        seasonality_note=season_note,
        data_quality=data_quality,
        ad_metrics=ad_metrics,
        ad_campaign=ad_campaign,
        monthly_ad=monthly_ad,
        seasonal_lift=seasonal_lift,
        amazon_benchmark_lift=amz_benchmark_lift,
        amazon_benchmark_source=amz_benchmark_source,
    )
