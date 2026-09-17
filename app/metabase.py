import logging
from datetime import datetime, timedelta
from typing import Optional

import requests

from app.config import (
    METABASE_URL, METABASE_API_KEY,
    SELL_OUT_TABLE_ID, REPLACEMENT_RINGS_CARD_ID,
)
from app.models import VelocityCheck

logger = logging.getLogger(__name__)


# Legacy dataclass kept for backward compat
class TrendData:
    def __init__(self, current_units=0, prior_units=0, pct_change=None, direction="unknown"):
        self.current_units = current_units
        self.prior_units = prior_units
        self.pct_change = pct_change
        self.direction = direction


def _query_dataset(filter_clause: list) -> list[dict]:
    url = f"{METABASE_URL}/api/dataset"
    headers = {
        "x-api-key": METABASE_API_KEY,
        "Content-Type": "application/json",
    }
    payload = {
        "database": 2,
        "type": "query",
        "query": {
            "source-table": SELL_OUT_TABLE_ID,
            "filter": filter_clause,
            "aggregation": [["sum", ["field", "SOLD_UNITS", {"base-type": "type/Integer"}]]],
        },
    }
    resp = requests.post(url, headers=headers, json=payload, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    cols = [c["name"] for c in data["data"]["cols"]]
    return [dict(zip(cols, row)) for row in data["data"]["rows"]]


def _run_card(card_id: int) -> list[dict]:
    """Execute a saved Metabase card/question and return rows."""
    url = f"{METABASE_URL}/api/card/{card_id}/query"
    headers = {
        "x-api-key": METABASE_API_KEY,
        "Content-Type": "application/json",
    }
    resp = requests.post(url, headers=headers, json={}, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    cols = [c["name"] for c in data["data"]["cols"]]
    return [dict(zip(cols, row)) for row in data["data"]["rows"]]


def _classify_direction(pct_change: Optional[float]) -> str:
    if pct_change is None:
        return "unknown"
    if pct_change > 10:
        return "up"
    if pct_change < -10:
        return "down"
    return "flat"


# ---------------------------------------------------------------------------
# Sell-through velocity (weekly run rate + trend)
# ---------------------------------------------------------------------------

def get_velocity(retailer: str) -> VelocityCheck:
    """Get current weekly run-rate and month-over-month trend for a retailer."""
    if not METABASE_API_KEY:
        logger.warning("No Metabase API key configured")
        return VelocityCheck()

    now = datetime.utcnow().date()
    current_start = now - timedelta(days=28)   # 4 weeks
    prior_start = now - timedelta(days=56)     # prior 4 weeks

    retailer_filter = [
        "contains",
        ["field", "RETAILER", {"base-type": "type/Text"}],
        retailer,
        {"case-sensitive": False},
    ]

    # Only count positive sell-out values (negatives are returns of prior purchases)
    positive_filter = [
        ">",
        ["field", "SOLD_UNITS", {"base-type": "type/Integer"}],
        0,
    ]

    try:
        # Current 4 weeks
        current_filter = [
            "and", retailer_filter, positive_filter,
            [">=", ["field", "SALE_DATE", {"base-type": "type/Date"}], str(current_start)],
            ["<", ["field", "SALE_DATE", {"base-type": "type/Date"}], str(now)],
        ]
        current_rows = _query_dataset(current_filter)
        current_units = int(current_rows[0].get("sum", 0)) if current_rows else 0

        # Prior 4 weeks
        prior_filter = [
            "and", retailer_filter, positive_filter,
            [">=", ["field", "SALE_DATE", {"base-type": "type/Date"}], str(prior_start)],
            ["<", ["field", "SALE_DATE", {"base-type": "type/Date"}], str(current_start)],
        ]
        prior_rows = _query_dataset(prior_filter)
        prior_units = int(prior_rows[0].get("sum", 0)) if prior_rows else 0

    except Exception as e:
        logger.error(f"Metabase velocity query failed: {e}")
        return VelocityCheck()

    weekly_run_rate = current_units // 4 if current_units > 0 else 0
    pct_change = None
    if prior_units > 0:
        pct_change = round(((current_units - prior_units) / prior_units) * 100, 1)

    return VelocityCheck(
        weekly_run_rate=weekly_run_rate,
        monthly_units=current_units,
        prior_monthly_units=prior_units,
        pct_change=pct_change,
        direction=_classify_direction(pct_change),
    )


# ---------------------------------------------------------------------------
# Legacy: get_activation_trend (wraps get_velocity)
# ---------------------------------------------------------------------------

def get_activation_trend(retailer: str) -> TrendData:
    if not METABASE_API_KEY:
        return TrendData()
    vel = get_velocity(retailer)
    return TrendData(
        current_units=vel.monthly_units,
        prior_units=vel.prior_monthly_units,
        pct_change=vel.pct_change,
        direction=vel.direction,
    )
