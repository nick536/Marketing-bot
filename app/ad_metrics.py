import logging

import requests

from app.config import (
    DATA_API_URL, DATA_API_TOKEN,
    REGION_TO_MARKETPLACE_KEY, AD_HEAVY_SPEND_THRESHOLD,
)
from app.regions import SIMILAR_MARKETS
from app.models import AdMetrics, MonthlyAdPerformance, PromoLiftData

logger = logging.getLogger(__name__)

_EXECUTE_SQL_URL = f"{DATA_API_URL}/api/v1/data/execute-sql"


def _execute_sql(sql: str, params: tuple = (), max_rows: int = 100) -> list[dict]:
    """Execute SQL via the the brand data API and return a list of row dicts.

    params are internal config values only (marketplace keys, month integers) —
    never user input. Substituted directly into the SQL since the HTTP API
    does not support parameterized queries.
    """
    if not DATA_API_TOKEN:
        raise ValueError("DATA_API_TOKEN not configured")

    formatted_sql = _bind_params(sql, params)
    resp = requests.post(
        _EXECUTE_SQL_URL,
        headers={
            "Authorization": f"Bearer {DATA_API_TOKEN}",
            "Content-Type": "application/json",
        },
        json={"sql": formatted_sql, "max_rows": max_rows},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()

    # Handle common response shapes
    if isinstance(data, list):
        return data
    if "rows" in data:
        return data["rows"]
    if "data" in data:
        d = data["data"]
        if isinstance(d, list):
            return d
        # Metabase-style: {"data": {"cols": [...], "rows": [[...]]}}
        if isinstance(d, dict) and "rows" in d and "cols" in d:
            cols = [c["name"] for c in d["cols"]]
            return [dict(zip(cols, row)) for row in d["rows"]]
    return []


def _bind_params(sql: str, params: tuple) -> str:
    """Replace %s placeholders with SQL-safe literals.

    Only safe because all callers pass values from internal config maps
    (REGION_TO_MARKETPLACE_KEY, month integers) — never user-supplied strings.
    """
    result = sql
    for p in params:
        if isinstance(p, int):
            result = result.replace("%s", str(p), 1)
        else:
            escaped = str(p).replace("'", "''")
            result = result.replace("%s", f"'{escaped}'", 1)
    return result


def get_ad_metrics(region: str) -> AdMetrics:
    """Fetch 30-day Amazon ad performance for a region as market context."""
    if not DATA_API_TOKEN:
        return AdMetrics(error="DATA_API_TOKEN not configured")

    mapping = REGION_TO_MARKETPLACE_KEY.get(region)
    if not mapping:
        try:
            promo_lift = _fetch_promo_lift_with_fallback(region, [])
        except Exception:
            promo_lift = PromoLiftData()
        return AdMetrics(
            marketplace_key=region,
            promo_lift=promo_lift,
            available=promo_lift.available,
            error=f"No direct marketplace for {region!r}" if not promo_lift.available else None,
        )

    keys = [mapping] if isinstance(mapping, str) else mapping
    marketplace_label = keys[0] if len(keys) == 1 else f"{region} ({len(keys)} marketplaces)"
    in_placeholders = ", ".join(["%s"] * len(keys))
    keys_params = tuple(keys)

    try:
        current_rows = _execute_sql(f"""
            SELECT
                "SPONSORED_TYPE",
                SUM("COSTS") AS "TOTAL_SPEND",
                SUM("SALES") AS "TOTAL_SALES",
                SUM("ORDERS") AS "TOTAL_ORDERS",
                SUM("CLICKS") AS "TOTAL_CLICKS",
                SUM("IMPRESSIONS") AS "TOTAL_IMPRESSIONS"
            FROM "dh_advertising_campaign_metrics"
            WHERE "MARKETPLACE_KEY" IN ({in_placeholders})
              AND "DATE_DAY" >= DATEADD('day', -30, CURRENT_DATE())
              AND "DATE_DAY" < CURRENT_DATE()
            GROUP BY "SPONSORED_TYPE"
        """, params=keys_params)

        prior_rows = _execute_sql(f"""
            SELECT
                SUM("COSTS") AS "TOTAL_SPEND",
                SUM("SALES") AS "TOTAL_SALES"
            FROM "dh_advertising_campaign_metrics"
            WHERE "MARKETPLACE_KEY" IN ({in_placeholders})
              AND "DATE_DAY" >= DATEADD('day', -60, CURRENT_DATE())
              AND "DATE_DAY" < DATEADD('day', -30, CURRENT_DATE())
        """, params=keys_params)

        total_spend = 0.0
        total_sales = 0.0
        total_orders = 0
        total_clicks = 0
        total_impressions = 0
        roas_by_type = {}
        spend_by_type = {}

        for row in current_rows:
            s_type = row.get("SPONSORED_TYPE", "Unknown")
            spend = float(row.get("TOTAL_SPEND") or 0)
            sales = float(row.get("TOTAL_SALES") or 0)
            orders = int(float(row.get("TOTAL_ORDERS") or 0))
            clicks = int(float(row.get("TOTAL_CLICKS") or 0))
            impressions = int(float(row.get("TOTAL_IMPRESSIONS") or 0))

            total_spend += spend
            total_sales += sales
            total_orders += orders
            total_clicks += clicks
            total_impressions += impressions
            spend_by_type[s_type] = spend
            roas_by_type[s_type] = round(sales / spend, 1) if spend > 0 else 0.0

        if total_spend == 0:
            return AdMetrics(marketplace_key=marketplace_label, error="No ad spend data in last 30 days")

        roas = round(total_sales / total_spend, 1) if total_spend > 0 else None
        ctr = round(total_clicks / total_impressions * 100, 2) if total_impressions > 0 else None

        roas_prior = None
        roas_trend = "unknown"
        if prior_rows:
            prior_spend = float(prior_rows[0].get("TOTAL_SPEND") or 0)
            prior_sales = float(prior_rows[0].get("TOTAL_SALES") or 0)
            roas_prior = round(prior_sales / prior_spend, 1) if prior_spend > 0 else None
            if roas and roas_prior:
                pct = (roas - roas_prior) / roas_prior * 100
                roas_trend = "up" if pct > 10 else ("down" if pct < -10 else "flat")

        promo_lift = PromoLiftData()
        try:
            promo_lift = _fetch_promo_lift(keys)
            if not promo_lift.available:
                promo_lift = _fetch_promo_lift_with_fallback(region, keys)
        except Exception as e:
            logger.warning(f"ad_metrics: promo lift fetch failed for {marketplace_label}: {e}")

        return AdMetrics(
            marketplace_key=marketplace_label,
            period_days=30,
            total_spend=round(total_spend, 0),
            total_sales=round(total_sales, 0),
            total_orders=total_orders,
            total_impressions=total_impressions,
            total_clicks=total_clicks,
            roas=roas,
            ctr=ctr,
            roas_by_type=roas_by_type,
            spend_by_type=spend_by_type,
            roas_prior_30d=roas_prior,
            roas_trend=roas_trend,
            is_heavy_ad_period=total_spend > AD_HEAVY_SPEND_THRESHOLD,
            promo_lift=promo_lift,
            available=True,
        )

    except Exception as e:
        logger.warning(f"ad_metrics: error for {marketplace_label}: {e}")
        return AdMetrics(marketplace_key=marketplace_label, error=str(e))


def _fetch_promo_lift_with_fallback(region: str, primary_keys: list[str]) -> PromoLiftData:
    similar = SIMILAR_MARKETS.get(region, [])
    for sim_region in similar:
        sim_mapping = REGION_TO_MARKETPLACE_KEY.get(sim_region)
        if not sim_mapping:
            continue
        sim_keys = [sim_mapping] if isinstance(sim_mapping, str) else sim_mapping
        try:
            lift = _fetch_promo_lift(sim_keys)
            if lift.available:
                lift.fallback_source = f"similar: {sim_region}"
                logger.info(f"promo_lift: using similar market {sim_region} for {region}")
                return lift
        except Exception:
            continue
    return PromoLiftData()


def _fetch_promo_lift(keys: list[str]) -> PromoLiftData:
    in_placeholders = ", ".join(["%s"] * len(keys))
    rows = _execute_sql(f"""
        WITH weekly AS (
            SELECT
                DATE_TRUNC('week', "PURCHASE_DATE") AS week,
                SUM("UNITS_SOLD") AS total_units,
                SUM(CASE WHEN "ITEM_PROMOTION_DISCOUNT" > 1 THEN "UNITS_SOLD" ELSE 0 END) AS promo_units,
                SUM(CASE WHEN "ITEM_PROMOTION_DISCOUNT" > 1
                    THEN "ITEM_PROMOTION_DISCOUNT" / NULLIF("SALES" + "ITEM_PROMOTION_DISCOUNT", 0) * 100 * "UNITS_SOLD"
                    ELSE 0 END)
                / NULLIF(SUM(CASE WHEN "ITEM_PROMOTION_DISCOUNT" > 1 THEN "UNITS_SOLD" ELSE 0 END), 0) AS wtd_disc_pct
            FROM "MAIN_REV_AMAZON"
            WHERE "PURCHASE_DATE" >= DATEADD('month', -12, CURRENT_DATE())
              AND "PRODUCT_TYPE" IN ('AIR-SSK', 'smart_ring', 'AIR-W', 'AIRSK-NEW')
              AND "ORDER_STATUS" != 'Cancelled'
              AND "MARKETPLACE_KEY" IN ({in_placeholders})
            GROUP BY week
        ),
        classified AS (
            SELECT *,
                promo_units * 1.0 / NULLIF(total_units, 0) AS promo_share,
                CASE
                    WHEN promo_units * 1.0 / NULLIF(total_units, 0) < 0.05 THEN 'baseline'
                    WHEN wtd_disc_pct <= 15 THEN '5-15'
                    WHEN wtd_disc_pct <= 25 THEN '15-25'
                    ELSE '25+'
                END AS week_type
            FROM weekly
        )
        SELECT
            week_type,
            COUNT(*) AS weeks,
            ROUND(AVG(total_units), 1) AS avg_units_wk
        FROM classified
        GROUP BY week_type
        HAVING COUNT(*) >= 3
        ORDER BY week_type
    """, params=tuple(keys), max_rows=10)

    if not rows:
        return PromoLiftData()

    baseline = 0.0
    units_by_bracket = {}
    weeks_by_bracket = {}

    for r in rows:
        wt = r["WEEK_TYPE"]
        avg = float(r["AVG_UNITS_WK"] or 0)
        wks = int(r["WEEKS"])
        if wt == "baseline":
            baseline = avg
        else:
            units_by_bracket[wt] = avg
            weeks_by_bracket[wt] = wks

    if baseline <= 0:
        return PromoLiftData()

    lift_by_bracket = {
        bracket: round(units / baseline, 1)
        for bracket, units in units_by_bracket.items()
        if units / baseline >= 1.0
    }

    return PromoLiftData(
        baseline_weekly=baseline,
        lift_by_bracket=lift_by_bracket,
        weeks_by_bracket=weeks_by_bracket,
        available=bool(lift_by_bracket),
    )


def get_monthly_ad_roas(region: str, target_month: int = 0) -> MonthlyAdPerformance:
    if not DATA_API_TOKEN:
        return MonthlyAdPerformance(error="DATA_API_TOKEN not configured")

    mapping = REGION_TO_MARKETPLACE_KEY.get(region)
    if not mapping:
        return MonthlyAdPerformance(error=f"No marketplace for {region!r}")

    keys = [mapping] if isinstance(mapping, str) else mapping
    in_placeholders = ", ".join(["%s"] * len(keys))

    try:
        rows = _execute_sql(f"""
            SELECT
                MONTH("DATE_DAY") AS "MONTH",
                "SPONSORED_TYPE",
                SUM("COSTS") AS "SPEND",
                SUM("SALES") AS "SALES",
                SUM("ORDERS") AS "ORDERS"
            FROM "dh_advertising_campaign_metrics"
            WHERE "MARKETPLACE_KEY" IN ({in_placeholders})
              AND "DATE_DAY" >= DATEADD('month', -12, CURRENT_DATE())
              AND "DATE_DAY" < CURRENT_DATE()
            GROUP BY MONTH("DATE_DAY"), "SPONSORED_TYPE"
            ORDER BY "MONTH", "SPONSORED_TYPE"
        """, params=tuple(keys), max_rows=200)

        if not rows:
            return MonthlyAdPerformance(error="No monthly ad data")

        monthly = {}
        total_spend = 0.0
        total_sales = 0.0
        for r in rows:
            m = int(r.get("MONTH", 0))
            s_type = r.get("SPONSORED_TYPE", "Unknown")
            spend = float(r.get("SPEND") or 0)
            sales = float(r.get("SALES") or 0)
            total_spend += spend
            total_sales += sales

            if m not in monthly:
                monthly[m] = {"spend": 0, "sales": 0, "by_type": {}}
            monthly[m]["spend"] += spend
            monthly[m]["sales"] += sales
            if spend > 0:
                monthly[m]["by_type"][s_type] = round(sales / spend, 1)

        for m in monthly:
            s = monthly[m]["spend"]
            monthly[m]["roas"] = round(monthly[m]["sales"] / s, 1) if s > 0 else 0

        target_roas = None
        target_by_type = {}
        if target_month in monthly:
            target_roas = monthly[target_month].get("roas")
            target_by_type = monthly[target_month].get("by_type", {})

        annual_avg = round(total_sales / total_spend, 1) if total_spend > 0 else None

        return MonthlyAdPerformance(
            monthly_roas=monthly,
            target_month=target_month,
            target_month_roas=target_roas,
            target_month_roas_by_type=target_by_type,
            annual_avg_roas=annual_avg,
            available=True,
        )

    except Exception as e:
        logger.warning(f"monthly_ad_roas: error for {region}: {e}")
        return MonthlyAdPerformance(error=str(e))


def get_seasonal_lift(region: str, target_month: int) -> PromoLiftData:
    if not DATA_API_TOKEN:
        return PromoLiftData()

    mapping = REGION_TO_MARKETPLACE_KEY.get(region)
    if not mapping:
        return _fetch_seasonal_lift_with_fallback(region, target_month)

    keys = [mapping] if isinstance(mapping, str) else mapping
    in_placeholders = ", ".join(["%s"] * len(keys))

    months = [(target_month - 1 - 1) % 12 + 1,
              target_month,
              target_month % 12 + 1]
    month_placeholders = ", ".join(["%s"] * len(months))
    quarter_label = _quarter_label(target_month)
    seasonal_params = tuple(months) + tuple(keys)

    try:
        rows = _execute_sql(f"""
            WITH weekly AS (
                SELECT
                    DATE_TRUNC('week', "PURCHASE_DATE") AS week,
                    SUM("UNITS_SOLD") AS total_units,
                    SUM(CASE WHEN "ITEM_PROMOTION_DISCOUNT" > 1 THEN "UNITS_SOLD" ELSE 0 END) AS promo_units,
                    SUM(CASE WHEN "ITEM_PROMOTION_DISCOUNT" > 1
                        THEN "ITEM_PROMOTION_DISCOUNT" / NULLIF("SALES" + "ITEM_PROMOTION_DISCOUNT", 0) * 100 * "UNITS_SOLD"
                        ELSE 0 END)
                    / NULLIF(SUM(CASE WHEN "ITEM_PROMOTION_DISCOUNT" > 1 THEN "UNITS_SOLD" ELSE 0 END), 0) AS wtd_disc_pct
                FROM "MAIN_REV_AMAZON"
                WHERE "PURCHASE_DATE" >= DATEADD('month', -12, CURRENT_DATE())
                  AND MONTH("PURCHASE_DATE") IN ({month_placeholders})
                  AND "PRODUCT_TYPE" IN ('AIR-SSK', 'smart_ring', 'AIR-W', 'AIRSK-NEW')
                  AND "ORDER_STATUS" != 'Cancelled'
                  AND "MARKETPLACE_KEY" IN ({in_placeholders})
                GROUP BY week
            ),
            classified AS (
                SELECT *,
                    promo_units * 1.0 / NULLIF(total_units, 0) AS promo_share,
                    CASE
                        WHEN promo_units * 1.0 / NULLIF(total_units, 0) < 0.05 THEN 'baseline'
                        WHEN wtd_disc_pct <= 15 THEN '5-15'
                        WHEN wtd_disc_pct <= 25 THEN '15-25'
                        ELSE '25+'
                    END AS week_type
                FROM weekly
            )
            SELECT
                week_type,
                COUNT(*) AS weeks,
                ROUND(AVG(total_units), 1) AS avg_units_wk
            FROM classified
            GROUP BY week_type
            HAVING COUNT(*) >= 2
            ORDER BY week_type
        """, params=seasonal_params, max_rows=10)

        if not rows:
            return PromoLiftData()

        baseline = 0.0
        units_by_bracket = {}
        weeks_by_bracket = {}
        for r in rows:
            wt = r["WEEK_TYPE"]
            avg = float(r["AVG_UNITS_WK"] or 0)
            wks = int(r["WEEKS"])
            if wt == "baseline":
                baseline = avg
            else:
                units_by_bracket[wt] = avg
                weeks_by_bracket[wt] = wks

        if baseline <= 0:
            return PromoLiftData()

        lift = {
            bracket: round(units / baseline, 1)
            for bracket, units in units_by_bracket.items()
            if units / baseline >= 1.0
        }

        return PromoLiftData(
            seasonal_lift_by_bracket=lift,
            seasonal_baseline_weekly=baseline,
            seasonal_source=quarter_label,
            available=bool(lift),
        )

    except Exception as e:
        logger.warning(f"seasonal_lift: error for {region} month {target_month}: {e}")
        return PromoLiftData()


def _fetch_seasonal_lift_with_fallback(region: str, target_month: int) -> PromoLiftData:
    similar = SIMILAR_MARKETS.get(region, [])
    for sim_region in similar:
        lift = get_seasonal_lift(sim_region, target_month)
        if lift.available:
            lift.fallback_source = f"similar: {sim_region}"
            return lift
    return PromoLiftData()


def _quarter_label(month: int) -> str:
    MONTH_NAMES = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
                   "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    prev = (month - 2) % 12 + 1
    next_m = month % 12 + 1
    return f"{MONTH_NAMES[prev]}-{MONTH_NAMES[next_m]}"
