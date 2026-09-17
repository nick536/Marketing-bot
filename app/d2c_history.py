"""Live D2C historical lookups against the purchase-history table via the internal data API.

Replaces the static lift table with a per-request query so as new D2C sales
accumulate, BAU rates and lift factors update automatically — no quarterly
refresh.

Two public lookups:
  - `get_bau_per_day(country, month)` → daily $0-discount sales rate
  - `get_lift_estimate(country, discount_pct, month)` → multiplicative lift
    factor over BAU at the requested discount tier, with confidence + source

Both go through `get_country_history(country)` which runs ONE query covering
all months and tiers for that country, then caches the result in-process.
Multi-leg evals across 6 countries → 6 queries total (one per country),
not 36 (one per leg-month-tier).

Falls back to the static `app/lift_table_d2c.py` defaults when:
  - DATA_API_TOKEN is unset (dev)
  - the data warehouse returns an error
  - The country has no history at the requested tier (uses peer / global fallback)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import lru_cache
from typing import Optional

from app.ad_metrics import _execute_sql
from app.config import DATA_API_TOKEN
from app.lift_table_d2c import (
    LIFT_CAP,
    LIFT_FLOOR,
    PEER_MAP,
    LiftEstimate,
    _GLOBAL_LIFT_DEFAULTS,
    _classify_tier,
    _adjust_lift_for_discount,
)
from app.lift_table_d2c import get_bau_per_day as _static_bau
from app.lift_table_d2c import get_lift as _static_lift

logger = logging.getLogger(__name__)


# Tier centers (% discount) for cross-tier lift extrapolation when the
# country has data at a different band than what the request asks.
_TIER_CENTERS = {"t15": 15.0, "t20": 20.0, "t25": 25.0}

# Excluded windows: Black Friday + New Year (seasonal anomaly weeks).
_BF_START_DATES = ("2024-11-04", "2025-11-03")
_BF_END_DATES = ("2025-02-03", "2026-02-02")


def _country_history_sql() -> str:
    """ONE query returning two row types for a country:
      - row_type='bau': per-month strict BAU rate. Filter is order-level
        DISCOUNT_REVENUE = 0 (per PersonA's spec: "BAU = 0% discount" — pure
        full-price customers). avg_units_per_week is rate × 7 / active_days.
      - row_type='tier': per-(month, tier) weekly aggregates classified by
        blended weekly discount %. Used as the lift NUMERATOR; the lift
        DENOMINATOR comes from row_type='bau'.

    Channel whitelist (D2C consumer-grade orders only) — explicitly excludes
    `Retail` (=retailer bulk shipments bundled into purchase_history), `Marketplace`
    (=Amazon etc), `Partnerships` (=B2B deals), `offline`, and `Replacement`
    (=warranty). `Inside Sales` IS included — high-touch sales-team-driven
    consumer purchases are still consumer demand.

    No QTY filter — multi-unit orders within consumer channels (gift packs,
    family buys) are real demand. Bulk B2B is excluded by the channel filter,
    not the QTY filter.

    Lookback: trailing 15 months. Tier rows exclude BF/NY seasonal windows;
    BAU rows do not (BAU is by-definition non-promo).
    """
    return f"""
    WITH order_qty AS (
      SELECT
        "ORDER_ID",
        DATE("PURCHASE_DATE") AS pdate,
        EXTRACT(MONTH FROM "PURCHASE_DATE") AS mon,
        SUM("QTY") AS units,
        SUM("AMOUNT_USD") AS gross,
        SUM("DISCOUNT_REVENUE") AS discount
      FROM "purchase_history"
      WHERE "COUNTRY" = %s
        AND "PURCHASE_DATE" >= DATEADD(month, -15, CURRENT_DATE)
        AND "PRODUCT_TYPE" = 'smart_ring'
        AND "PURCHASE_CHANNEL" IN (
          'others',
          'Influencer Marketing',
          'Performance Marketing',
          'Product in-app Referrals',
          'Product in-app buy buttons',
          'Inside Sales',
          'Blog',
          'Social',
          'Reengagement Communication'
        )
      GROUP BY 1, 2, 3
    ),
    bau_per_month AS (
      -- Strict BAU: orders with $0 discount only (no storewide code, no
      -- individual code). Aggregates to per-day rate using distinct days.
      SELECT
        mon,
        COUNT(DISTINCT pdate) AS active_days,
        SUM(units) AS bau_units
      FROM order_qty
      WHERE discount = 0
      GROUP BY 1
    ),
    weekly AS (
      SELECT
        mon,
        DATE_TRUNC('week', pdate) AS wk,
        SUM(units) AS units,
        SUM(gross) AS gross,
        SUM(discount) AS discount,
        SUM(discount) / NULLIF(SUM(gross), 0) AS disc_pct
      FROM order_qty
      GROUP BY 1, 2
    ),
    classified AS (
      SELECT *,
        CASE
          WHEN disc_pct < 0.03 THEN 'bau_week'
          WHEN disc_pct < 0.13 THEN 'mild'
          WHEN disc_pct < 0.18 THEN 't15'
          WHEN disc_pct < 0.22 THEN 't20'
          ELSE 'deep'
        END AS tier
      FROM weekly
    )
    SELECT
      'bau' AS row_type,
      mon::INTEGER AS month_num,
      'bau_strict' AS tier,
      active_days::INTEGER AS n,
      (bau_units::FLOAT * 7.0 / NULLIF(active_days, 0)) AS avg_units_per_week
    FROM bau_per_month
    UNION ALL
    SELECT
      'tier' AS row_type,
      mon::INTEGER AS month_num,
      tier,
      COUNT(*) AS n,
      AVG(units) AS avg_units_per_week
    FROM classified
    WHERE wk NOT BETWEEN '{_BF_START_DATES[0]}' AND '{_BF_END_DATES[0]}'
      AND wk NOT BETWEEN '{_BF_START_DATES[1]}' AND '{_BF_END_DATES[1]}'
    GROUP BY 1, 2, 3
    """


@dataclass
class CountryHistory:
    """Per-country aggregate from the data warehouse. Two parallel views:
      - bau_by_month: strict order-level $0-discount BAU rate.
      - tier_by_month_tier: weekly aggregates segmented by blended discount tier.
        Used as lift NUMERATOR only; BAU denominator is from `bau_by_month`.
    Both have cells = (sample_n, units_per_day).
    """
    bau_by_month: dict[int, tuple[int, float]]                # month -> (active_days, u/day)
    tier_by_month_tier: dict[tuple[int, str], tuple[int, float]]  # (month, tier) -> (n_weeks, u/day)
    fetched_ok: bool


@lru_cache(maxsize=128)
def get_country_history(country: str) -> CountryHistory:
    """Run ONE the data warehouse query for the country (returns BAU rows + tier rows)
    and cache the result. Cached for the pod lifetime; restart refreshes."""
    if not DATA_API_TOKEN:
        logger.info(f"D2C history: DATA_API_TOKEN not set; using static fallback for {country}")
        return CountryHistory(bau_by_month={}, tier_by_month_tier={}, fetched_ok=False)

    try:
        rows = _execute_sql(_country_history_sql(), params=(country,))
    except Exception as e:
        logger.error(f"D2C history query failed for {country}: {e}")
        return CountryHistory(bau_by_month={}, tier_by_month_tier={}, fetched_ok=False)

    bau: dict[int, tuple[int, float]] = {}
    tiers: dict[tuple[int, str], tuple[int, float]] = {}
    for r in rows:
        try:
            row_type = (r.get("ROW_TYPE") or r.get("row_type") or "").strip()
            mon = int(r.get("MONTH_NUM") or r.get("month_num"))
            tier = (r.get("TIER") or r.get("tier") or "").strip()
            n = int(r.get("N") or r.get("n") or 0)
            upw = float(r.get("AVG_UNITS_PER_WEEK") or r.get("avg_units_per_week") or 0.0)
            if row_type == "bau":
                bau[mon] = (n, upw / 7.0)
            elif row_type == "tier":
                tiers[(mon, tier)] = (n, upw / 7.0)
        except (TypeError, ValueError):
            continue

    logger.info(
        f"D2C history: {country} → {len(bau)} BAU months, "
        f"{len(tiers)} (month, tier) tier cells"
    )
    return CountryHistory(bau_by_month=bau, tier_by_month_tier=tiers, fetched_ok=True)


def _annual_avg_bau(history: CountryHistory) -> Optional[float]:
    """Weighted average BAU u/day across months we have strict-BAU sample for."""
    if not history.bau_by_month:
        return None
    weighted_total = sum(n * upd for n, upd in history.bau_by_month.values())
    weighted_n = sum(n for n, _upd in history.bau_by_month.values())
    return weighted_total / weighted_n if weighted_n > 0 else None


def get_bau_per_day(country: str, month: int) -> tuple[float, str]:
    """Return (BAU rate u/day, source string).

    BAU is strict: orders with DISCOUNT_REVENUE = 0 (per PersonA's spec).
    Falls back: live month-specific → live annual avg → static table.
    """
    history = get_country_history(country)
    if history.fetched_ok:
        cell = history.bau_by_month.get(month)
        if cell and cell[0] > 0:
            return cell[1], f"live-strict:{country}-month-{month}"
        annual = _annual_avg_bau(history)
        if annual is not None:
            return annual, f"live-strict:{country}-annual-avg"
    # Fallback to static table (which is also strict $0 by construction).
    bau, src = _static_bau(country, month)
    return bau, f"static:{src}"


def _country_lift_at_tier(history: CountryHistory, tier: str) -> Optional[tuple[float, int]]:
    """Average lift across months we have tier data for this country.
    Lift = tier u/day ÷ strict BAU u/day (same month preferred, else annual avg).
    Returns (lift, n_weeks_total) or None.
    """
    if not history.fetched_ok:
        return None

    tier_cells = [(m, n, upd) for (m, t), (n, upd) in history.tier_by_month_tier.items()
                  if t == tier and n > 0]
    if not tier_cells:
        return None

    annual_bau = _annual_avg_bau(history)
    if annual_bau is None or annual_bau <= 0:
        return None

    weighted_lift_sum = 0.0
    total_weeks = 0
    for m, n, upd in tier_cells:
        # Prefer same-month strict BAU; fall back to annual avg.
        bau_cell = history.bau_by_month.get(m)
        bau_upd = bau_cell[1] if (bau_cell and bau_cell[0] > 0) else annual_bau
        if bau_upd <= 0:
            continue
        lift = upd / bau_upd
        weighted_lift_sum += lift * n
        total_weeks += n
    if total_weeks == 0:
        return None
    return weighted_lift_sum / total_weeks, total_weeks


def get_lift_estimate(country: str, discount_pct: float, month: int) -> LiftEstimate:
    """Return a LiftEstimate for the (country, discount, month) request.

    Resolution order:
      1. Live: country has data at the target tier in the target month or any month
      2. Live + cross-tier: country has data at adjacent tier; scale by
         (target_disc / band_center)^0.7
      3. Live: peer-country lift at target tier
      4. Static fallback (`app/lift_table_d2c.py`)
    """
    tier = _classify_tier(discount_pct)
    bau, bau_src = get_bau_per_day(country, month)

    if not tier:
        return LiftEstimate(1.0, bau, 0, "no-discount", "n/a")

    history = get_country_history(country)

    # Tier 1: country direct hit at requested tier.
    direct = _country_lift_at_tier(history, tier)
    if direct is not None:
        lift, n = direct
        lift = max(LIFT_FLOOR, min(LIFT_CAP, lift))
        return LiftEstimate(
            lift=lift, bau_per_day=bau, sample_n=n,
            source=f"live:{country}-{tier}",
            confidence=_confidence_for(n),
        )

    # Tier 2: scale from adjacent country tier we DO have.
    if history.fetched_ok:
        for adj_tier in ("t15", "t20", "t25"):
            if adj_tier == tier:
                continue
            adj = _country_lift_at_tier(history, adj_tier)
            if adj is None:
                continue
            adj_lift, n = adj
            scaled = _adjust_lift_for_discount(adj_lift, _TIER_CENTERS[adj_tier], discount_pct)
            scaled = max(LIFT_FLOOR, min(LIFT_CAP, scaled))
            return LiftEstimate(
                lift=scaled, bau_per_day=bau, sample_n=n,
                source=f"live:{country}-{adj_tier}-scaled",
                confidence="med-low",
            )

    # Tier 3: peer country at requested tier (live).
    peer = PEER_MAP.get(country)
    if peer:
        peer_history = get_country_history(peer)
        peer_lift = _country_lift_at_tier(peer_history, tier)
        if peer_lift is not None:
            lift, n = peer_lift
            lift = max(LIFT_FLOOR, min(LIFT_CAP, lift))
            return LiftEstimate(
                lift=lift, bau_per_day=bau, sample_n=n,
                source=f"live:peer-{peer}-{tier}",
                confidence="med-low",
            )

    # Tier 4: static-table fallback (pre-baked defaults).
    static = _static_lift(country, discount_pct)
    static.bau_per_day = bau  # use live BAU even if lift came from static
    static.source = f"static-fallback:{static.source}"
    return static


def _confidence_for(n_weeks: int) -> str:
    """Heuristic confidence label from sample size (weeks)."""
    if n_weeks >= 8:
        return "high"
    if n_weeks >= 4:
        return "med-high"
    if n_weeks >= 2:
        return "medium"
    if n_weeks >= 1:
        return "med-low"
    return "low"


def clear_cache() -> None:
    """Wipe the in-process country-history cache. Useful when refreshing the
    lift table without bouncing the pod (e.g. via an admin endpoint)."""
    get_country_history.cache_clear()
