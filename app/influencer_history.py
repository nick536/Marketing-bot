"""Live influencer-channel BAU from the data warehouse `purchase_history`.

The influencer eval's CM3-cash figure scales with a volume estimate. This
module supplies that estimate as the trailing-12-week run-rate of the
`Influencer Marketing` purchase channel — real demand, not a hand-typed
sheet average. Falls back to (0.0, 0, "unavailable") when DATA_API_TOKEN is
unset (dev) or the query errors; the caller then uses the past-promos tab.
"""
from __future__ import annotations

import logging
from functools import lru_cache

from app.ad_metrics import _execute_sql
from app.config import DATA_API_TOKEN

logger = logging.getLogger(__name__)


def _influencer_bau_sql() -> str:
    """Sum smart_ring QTY per ISO week for the Influencer Marketing channel,
    trailing 12 complete weeks (current partial week excluded)."""
    return """
    SELECT DATE_TRUNC('week', "PURCHASE_DATE") AS wk,
           SUM("QTY") AS units
    FROM "purchase_history"
    WHERE "PURCHASE_CHANNEL" = 'Influencer Marketing'
      AND "PRODUCT_TYPE" = 'smart_ring'
      AND "PURCHASE_DATE" >= DATEADD(week, -12, DATE_TRUNC('week', CURRENT_DATE))
      AND "PURCHASE_DATE" < DATE_TRUNC('week', CURRENT_DATE)
    GROUP BY 1
    ORDER BY 1
    """


@lru_cache(maxsize=1)
def get_influencer_bau() -> tuple[float, int, str]:
    """Return (avg_units_per_week, n_weeks, source).

    source is "live" on a successful query, "unavailable" when the token is
    unset or the query fails. Cached for the pod lifetime; restart refreshes.
    """
    if not DATA_API_TOKEN:
        logger.info("Influencer BAU: DATA_API_TOKEN not set; unavailable")
        return (0.0, 0, "unavailable")
    try:
        rows = _execute_sql(_influencer_bau_sql())
    except Exception as e:
        logger.error(f"Influencer BAU query failed: {e}")
        return (0.0, 0, "unavailable")

    weekly: list[float] = []
    for r in rows:
        u = r.get("UNITS") if "UNITS" in r else r.get("units")
        if u is None:
            continue
        try:
            weekly.append(float(u))
        except (TypeError, ValueError):
            continue
    if not weekly:
        return (0.0, 0, "unavailable")
    return (sum(weekly) / len(weekly), len(weekly), "live")


def clear_cache() -> None:
    """Wipe the cached BAU result (admin refresh without a pod bounce)."""
    get_influencer_bau.cache_clear()
