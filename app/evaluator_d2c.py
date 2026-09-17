"""D2C storewide promo evaluator.

Mirrors the retailer evaluator's structure (incremental-units → P&L waterfall)
but uses D2C-specific economics:
  - sell price [REDACTED] (no retailer markup; we are the channel)
  - [REDACTED] brand-funded trade discount (no retailer co-fund)
  - [REDACTED] return rate (vs retailer-flow which uses sheet-driven rates)
  - [REDACTED] COGS, [REDACTED] warranty on net revenue (same as retailer)

Incremental units are derived from the country×month BAU rate × the lift
factor at the requested discount level (see app/lift_table_d2c.py). The
retailer flow uses historical comp's `incremental_units` directly; D2C uses
BAU × (lift − 1) × days because there's no per-promo historical record we
can pluck from a sheet.

Slack output mirrors the retailer P&L waterfall format so reviewers see the
same shape regardless of promo type.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

from app.config import COGS_PER_RING_USD, GRADE_A_FLOOR, GRADE_B_FLOOR, DATA_API_TOKEN
from app.evaluator import _grade_from_pct, model_pl_impact
from app.lift_table_d2c import LiftEstimate
from app.lift_table_d2c import get_lift_estimate as _static_lift_estimate
from app.models import MarketEconomics, PLImpact
from app.parser import PromoRequest


def get_lift_estimate(country: str, discount_pct: float, month: int) -> LiftEstimate:
    """Resolve a lift estimate, preferring live the data warehouse history when the
    bot has internal data credentials; falls through to the baked-in lift table
    for dev environments / outages."""
    if DATA_API_TOKEN:
        try:
            from app.d2c_history import get_lift_estimate as _live
            return _live(country, discount_pct, month)
        except Exception:
            logger.exception("Live D2C history lookup failed; falling back to static")
    return _static_lift_estimate(country, discount_pct, month)

logger = logging.getLogger(__name__)


D2C_SELL_PRICE_USD = 0.0  # REDACTED
D2C_RETURN_RATE = 0.0  # REDACTED
D2C_TD_UH_PCT = 0.0   # REDACTED (brand-funded share of trade discount)


@dataclass
class D2CEvalResult:
    """One D2C promo evaluation. Per-country if a multi-country submission is
    decomposed into legs; one of these per leg."""
    country: str
    discount_pct: float
    days: int
    bau_per_day: float
    lift: float
    lift_source: str
    lift_confidence: str
    bau_units: int
    total_units: int
    incremental_units: int
    pl_impact: PLImpact            # waterfall on incremental units only
    counterfactual_cm3: float      # BAU CM3 at full price (no-promo state)
    counterfactual_net_rev: float  # BAU net rev at full price
    promo_cm3_total: float         # total CM3 at discounted price for ALL units (BAU + incr)
    promo_net_rev_total: float     # total net rev at discounted price for ALL units
    incremental_cm3: float         # promo_cm3_total − counterfactual_cm3 (Δ value created)
    incremental_net_rev: float     # promo_net_rev_total − counterfactual_net_rev
    incremental_cm3_pct: Optional[float]  # Δ CM3 / Δ Net Rev — primary verdict metric
    incremental_roas: Optional[float]     # Δ CM3 / discount paid (efficiency view, secondary)
    grade: str
    notes: list[str] = field(default_factory=list)


@dataclass
class D2CMultiLegResult:
    """Aggregate result when a single submission spans multiple country×% legs."""
    legs: list[D2CEvalResult] = field(default_factory=list)
    total_incremental_units: int = 0
    total_discount_cost: float = 0.0
    total_incremental_cm3: float = 0.0
    total_incremental_net_rev: float = 0.0
    aggregate_incremental_cm3_pct: Optional[float] = None  # primary verdict
    aggregate_incremental_roas: Optional[float] = None     # secondary
    aggregate_grade: str = ""


def _d2c_economics() -> MarketEconomics:
    """Standard D2C MarketEconomics. We sell direct at [REDACTED], no channel
    markup, [REDACTED] brand burden on trade discount, [REDACTED] returns."""
    return MarketEconomics(
        market="D2C",
        currency="USD",
        fx_rate_to_usd=1.0,
        buy_price_local=D2C_SELL_PRICE_USD,
        buy_price_usd=D2C_SELL_PRICE_USD,
        retail_price_usd=D2C_SELL_PRICE_USD,
        channel_margin_usd=0.0,
        trade_discount_brand_pct=D2C_TD_UH_PCT,
        return_rate=D2C_RETURN_RATE,
        distributor="Brand-D2C",
    )


def _full_price_per_unit() -> tuple[float, float]:
    """Return (CM3-cash, net_rev) per unit at $0 discount (no-promo counterfactual)."""
    returns_per_unit = D2C_RETURN_RATE * D2C_SELL_PRICE_USD
    net_rev = D2C_SELL_PRICE_USD - 0.0 - returns_per_unit
    warranty = 0.0 * net_rev  # REDACTED (warranty % of net revenue)
    cm3 = net_rev - COGS_PER_RING_USD - warranty
    return cm3, net_rev


def _promo_per_unit(discount_pct: float) -> tuple[float, float]:
    """Return (CM3-cash, net_rev) per unit at the given discount %."""
    trade_disc = D2C_SELL_PRICE_USD * (discount_pct / 100.0) * D2C_TD_UH_PCT
    returns_per_unit = D2C_RETURN_RATE * D2C_SELL_PRICE_USD
    net_rev = D2C_SELL_PRICE_USD - trade_disc - returns_per_unit
    warranty = 0.0 * net_rev  # REDACTED (warranty % of net revenue)
    cm3 = net_rev - COGS_PER_RING_USD - warranty
    return cm3, net_rev


def _grade_from_incremental_pct(inc_cm3: float, inc_pct: Optional[float]) -> str:
    """Grade D2C promo on Δ CM3 as % of Δ net revenue (mirrors retailer
    eval thresholds). REJECT if Δ CM3 negative — promo destroys margin.

    Thresholds (from app/config.py — same as retailer):
      A ≥ [REDACTED], B [REDACTED], C < [REDACTED], REJECT if Δ CM3 < 0
    """
    if inc_cm3 < 0:
        return "REJECT"
    if inc_pct is None:
        return "C"
    if inc_pct >= GRADE_A_FLOOR:
        return "A"
    if inc_pct >= GRADE_B_FLOOR:
        return "B"
    return "C"


def evaluate_d2c_leg(
    country: str,
    discount_pct: float,
    start_date: date,
    end_date: date,
    submitter: str = "",
) -> D2CEvalResult:
    """Evaluate one country×discount leg of a D2C storewide proposal.

    `discount_pct` is a number, not a fraction — pass 16 for 16%, not 0.16.
    `country` accepts both ISO codes ("IN", "AU") and full names ("India",
    "Australia") — normalized via lift_table_d2c.normalize_country().
    """
    days = max(1, (end_date - start_date).days + 1)
    target_month = start_date.month

    # 1. BAU + lift lookup.
    le: LiftEstimate = get_lift_estimate(country, discount_pct, target_month)

    # Fail loudly if the country couldn't be resolved. Previously a name like
    # "Middle East" silently fell to global defaults (BAU=1.0/day, lift=1.5×)
    # and produced a meaningless grade.
    if le.confidence == "cannot_evaluate":
        from app.lift_table_d2c import SUPPORTED_D2C_COUNTRIES
        supported = ", ".join(sorted(SUPPORTED_D2C_COUNTRIES))
        return D2CEvalResult(
            country=country,
            discount_pct=discount_pct,
            days=days,
            bau_per_day=0.0,
            lift=0.0,
            lift_source=le.source,
            lift_confidence="cannot_evaluate",
            bau_units=0,
            total_units=0,
            incremental_units=0,
            pl_impact=PLImpact(),
            counterfactual_cm3=0.0,
            counterfactual_net_rev=0.0,
            promo_cm3_total=0.0,
            promo_net_rev_total=0.0,
            incremental_cm3=0.0,
            incremental_net_rev=0.0,
            incremental_cm3_pct=None,
            incremental_roas=None,
            grade="CANNOT_EVALUATE",
            notes=[
                f"Country '{country}' not recognized. Supported D2C markets: {supported}. "
                f"Either pass an ISO code (IN, AU, US, UK, CA, EU, PL, GCC, JP, SG, MY, NZ) "
                f"or one of the common full names (India, Australia, etc.)."
            ],
        )

    bau_per_day = le.bau_per_day
    lift = le.lift

    bau_units = int(round(bau_per_day * days))
    total_units = int(round(bau_per_day * lift * days))
    incremental_units = max(0, total_units - bau_units)

    # 2. Build the request + economics for the existing P&L model.
    request = PromoRequest(
        retailer=f"D2C-{country}",
        region=country,
        promo_type="d2c_storewide",
        promo_scope="d2c",
        discount_pct=discount_pct,
    )
    econ = _d2c_economics()

    # 3. Run the existing waterfall on incremental units. This produces the
    #    promo-state CM3-cash for the lift portion only (matches retailer
    #    semantics: CM3 from the units we wouldn't have sold without the promo).
    pl: PLImpact = model_pl_impact(
        incremental_units=incremental_units,
        request=request,
        econ=econ,
        marketing_spend=0.0,
        soa_per_unit=0.0,
        volume_confidence=("low" if le.confidence in {"low", "med-low"} else "high"),
    )

    # 4. Compute the two states (no-promo counterfactual vs promo-on) at the
    #    per-unit level, then total. Δ between them is the value the promo
    #    creates — both Δ CM3-cash and Δ Net Revenue. The bot grades on Δ
    #    CM3 as % of Δ Net Rev (mirrors retailer eval thresholds).
    cm3_per_unit_full, net_rev_per_unit_full = _full_price_per_unit()
    cm3_per_unit_promo, net_rev_per_unit_promo = _promo_per_unit(discount_pct)

    counterfactual_cm3 = bau_units * cm3_per_unit_full
    counterfactual_net_rev = bau_units * net_rev_per_unit_full
    promo_cm3_total = total_units * cm3_per_unit_promo
    promo_net_rev_total = total_units * net_rev_per_unit_promo

    incremental_cm3 = promo_cm3_total - counterfactual_cm3
    incremental_net_rev = promo_net_rev_total - counterfactual_net_rev

    incremental_cm3_pct = (
        (incremental_cm3 / incremental_net_rev * 100.0)
        if incremental_net_rev > 0
        else None
    )

    # Secondary metric: discount-efficiency view (kept for reviewers who want it).
    discount_per_unit = D2C_SELL_PRICE_USD * (discount_pct / 100.0) * D2C_TD_UH_PCT
    discount_cost_total = total_units * discount_per_unit
    inc_roas = (incremental_cm3 / discount_cost_total) if discount_cost_total > 0 else None

    grade = _grade_from_incremental_pct(incremental_cm3, incremental_cm3_pct)

    notes = []
    if le.confidence in {"low", "med-low"}:
        notes.append(f"Lift confidence {le.confidence} (n={le.sample_n}, source={le.source}). Treat with caution.")
    if le.sample_n == 0:
        notes.append("Lift derived from peer-country fallback or global default — refresh lift table when this country has more data.")

    # TOPLINE_SHRINKAGE flag: promo created margin (ΔCM3 > 0) but shrunk net revenue (ΔNetRev < 0).
    # Rare but real — heavy baseline cannibalization at a discount that adds modest incremental
    # margin. Reviewer should know the promo was value-positive but topline-negative.
    if incremental_cm3 > 0 and incremental_net_rev < 0:
        notes.append(
            f"⚠️ TOPLINE_SHRINKAGE: Δ CM3 +${incremental_cm3:,.0f} but Δ Net Rev "
            f"${incremental_net_rev:,.0f}. Promo created margin but shrunk topline — "
            f"baseline cannibalization at the discount outweighs incremental revenue. "
            f"Review whether topline growth matters for this funding cycle."
        )

    return D2CEvalResult(
        country=country,
        discount_pct=discount_pct,
        days=days,
        bau_per_day=bau_per_day,
        lift=lift,
        lift_source=le.source,
        lift_confidence=le.confidence,
        bau_units=bau_units,
        total_units=total_units,
        incremental_units=incremental_units,
        pl_impact=pl,
        counterfactual_cm3=round(counterfactual_cm3, 2),
        counterfactual_net_rev=round(counterfactual_net_rev, 2),
        promo_cm3_total=round(promo_cm3_total, 2),
        promo_net_rev_total=round(promo_net_rev_total, 2),
        incremental_cm3=round(incremental_cm3, 2),
        incremental_net_rev=round(incremental_net_rev, 2),
        incremental_cm3_pct=round(incremental_cm3_pct, 1) if incremental_cm3_pct is not None else None,
        incremental_roas=round(inc_roas, 2) if inc_roas is not None else None,
        grade=grade,
        notes=notes,
    )


def evaluate_d2c_multi(
    legs: list[tuple[str, float]],
    start_date: date,
    end_date: date,
    submitter: str = "",
) -> D2CMultiLegResult:
    """Evaluate a multi-country D2C proposal. `legs` is a list of (country,
    discount_pct) pairs. Returns per-leg results plus the aggregate grade,
    which uses the *aggregate* incremental ROAS (sum of incremental CM3 ÷
    sum of discount cost across all legs)."""
    out = D2CMultiLegResult()
    total_disc_cost = 0.0
    for country, discount_pct in legs:
        leg = evaluate_d2c_leg(country, discount_pct, start_date, end_date, submitter=submitter)
        out.legs.append(leg)
        out.total_incremental_units += leg.incremental_units
        out.total_incremental_cm3 += leg.incremental_cm3
        out.total_incremental_net_rev += leg.incremental_net_rev
        leg_disc_cost = leg.total_units * (D2C_SELL_PRICE_USD * (leg.discount_pct / 100.0) * D2C_TD_UH_PCT)
        total_disc_cost += leg_disc_cost
    out.total_discount_cost = round(total_disc_cost, 2)
    if out.total_incremental_net_rev > 0:
        out.aggregate_incremental_cm3_pct = round(
            out.total_incremental_cm3 / out.total_incremental_net_rev * 100.0, 1
        )
    if total_disc_cost > 0:
        out.aggregate_incremental_roas = round(out.total_incremental_cm3 / total_disc_cost, 2)
    out.aggregate_grade = _grade_from_incremental_pct(
        out.total_incremental_cm3, out.aggregate_incremental_cm3_pct
    )
    return out


def format_d2c_eval_for_slack(result: D2CMultiLegResult, dates_label: str = "") -> str:
    """Render a multi-leg D2C eval as a Slack mrkdwn block. Mirrors the
    retailer eval's waterfall-style format for consistency."""
    lines = []
    grade_emoji = {
        "A": ":large_green_circle:",
        "B": ":large_yellow_circle:",
        "C": ":red_circle:",
        "REJECT": ":x:",
    }.get(result.aggregate_grade, ":white_circle:")
    lines.append(
        f"*Grade {result.aggregate_grade}* {grade_emoji}  D2C storewide proposal"
        + (f" ({dates_label})" if dates_label else "")
    )
    lines.append("")

    # Per-leg summary table.
    lines.append("*Per-leg summary*")
    lines.append("```")
    lines.append(
        f"{'Country':<6} {'Disc':>5} {'Lift':>5} {'BAU':>5} {'Total':>6} {'Incr':>5} "
        f"{'ΔCM3':>8} {'ΔNetRev':>9} {'ΔCM3%':>7} {'Grade':>6}"
    )
    for leg in result.legs:
        pct_str = f"{leg.incremental_cm3_pct:.1f}%" if leg.incremental_cm3_pct is not None else "—"
        lines.append(
            f"{leg.country:<6} {int(leg.discount_pct):>3}%  "
            f"{leg.lift:>4.2f}x "
            f"{leg.bau_units:>5} {leg.total_units:>6} {leg.incremental_units:>5} "
            f"${leg.incremental_cm3:>6,.0f} ${leg.incremental_net_rev:>7,.0f} "
            f"{pct_str:>7} {leg.grade:>6}"
        )
    lines.append("```")
    lines.append("")

    # Full waterfall per leg — counterfactual vs promo state, with COGS + warranty.
    lines.append("*Full P&L waterfall (per leg)*")
    lines.append("```")
    for leg in result.legs:
        cm3_full_pu, nr_full_pu = _full_price_per_unit()
        cm3_promo_pu, nr_promo_pu = _promo_per_unit(leg.discount_pct)
        # Recompute waterfall components from per-unit × units.
        full_gross = leg.bau_units * D2C_SELL_PRICE_USD
        full_returns = leg.bau_units * D2C_RETURN_RATE * D2C_SELL_PRICE_USD
        full_net_rev = leg.bau_units * nr_full_pu
        full_cogs = leg.bau_units * COGS_PER_RING_USD
        full_warranty = 0.0 * full_net_rev  # REDACTED (warranty % of net revenue)
        full_cm3 = full_net_rev - full_cogs - full_warranty

        promo_gross = leg.total_units * D2C_SELL_PRICE_USD
        promo_disc = leg.total_units * D2C_SELL_PRICE_USD * (leg.discount_pct / 100.0) * D2C_TD_UH_PCT
        promo_returns = leg.total_units * D2C_RETURN_RATE * D2C_SELL_PRICE_USD
        promo_net_rev = leg.total_units * nr_promo_pu
        promo_cogs = leg.total_units * COGS_PER_RING_USD
        promo_warranty = 0.0 * promo_net_rev  # REDACTED (warranty % of net revenue)
        promo_cm3 = promo_net_rev - promo_cogs - promo_warranty

        lines.append(f"  {leg.country} @ {int(leg.discount_pct)}% ({leg.days}d, lift {leg.lift:.2f}×)")
        lines.append(f"    {'':<22}{'No promo':>12}{'With promo':>13}{'Δ':>11}")
        lines.append(f"    {'Units':<22}{leg.bau_units:>12,d}{leg.total_units:>13,d}{leg.incremental_units:>11,d}")
        lines.append(f"    {'Gross revenue':<22}${full_gross:>11,.0f}${promo_gross:>12,.0f}${promo_gross-full_gross:>10,.0f}")
        lines.append(f"    {'(−) Trade discount':<22}{'$0':>12}${promo_disc:>12,.0f}${promo_disc:>10,.0f}")
        lines.append(f"    {'(−) Returns':<22}${full_returns:>11,.0f}${promo_returns:>12,.0f}${promo_returns-full_returns:>10,.0f}")
        lines.append(f"    {'= Net revenue':<22}${full_net_rev:>11,.0f}${promo_net_rev:>12,.0f}${promo_net_rev-full_net_rev:>10,.0f}")
        lines.append(f"    {'(−) COGS ($[REDACTED]/u)':<22}${full_cogs:>11,.0f}${promo_cogs:>12,.0f}${promo_cogs-full_cogs:>10,.0f}")
        lines.append(f"    {'(−) Warranty ([REDACTED]%)':<22}${full_warranty:>11,.0f}${promo_warranty:>12,.0f}${promo_warranty-full_warranty:>10,.0f}")
        lines.append(f"    {'= CM3-cash':<22}${full_cm3:>11,.0f}${promo_cm3:>12,.0f}${leg.incremental_cm3:>10,.0f}")
        lines.append(f"    Δ CM3% of Δ NetRev: {leg.incremental_cm3_pct:.1f}% → Grade {leg.grade}")
        lines.append("")
    lines.append("```")
    lines.append("")

    # Aggregate.
    agg_pct_str = (
        f"{result.aggregate_incremental_cm3_pct:.1f}%"
        if result.aggregate_incremental_cm3_pct is not None
        else "—"
    )
    agg_roas_str = (
        f"{result.aggregate_incremental_roas:.2f}x"
        if result.aggregate_incremental_roas is not None
        else "—"
    )
    lines.append(
        f"*Aggregate*: {result.total_incremental_units} incremental units · "
        f"Δ Net Rev ${result.total_incremental_net_rev:,.0f} · "
        f"Δ CM3-cash ${result.total_incremental_cm3:,.0f} · "
        f"*Δ CM3% of Δ Net Rev: {agg_pct_str}* "
        f"_(efficiency view: Δ CM3 ÷ discount paid = {agg_roas_str})_"
    )
    lines.append("")

    # Notes per leg.
    notes_lines = []
    for leg in result.legs:
        for note in leg.notes:
            notes_lines.append(f"• [{leg.country}] {note}")
    if notes_lines:
        lines.append("*Confidence notes*")
        lines.extend(notes_lines)

    return "\n".join(lines)
