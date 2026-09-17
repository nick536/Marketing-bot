import json
import logging
import re
from typing import Optional
from collections import defaultdict

import gspread
from google.oauth2.service_account import Credentials

from app.config import (
    GOOGLE_SERVICE_ACCOUNT_JSON, PROMO_SHEET_ID, SELLOUT_SHEET_ID,
    REGION_TAB_KEYWORDS, SALES_TAB_KEYWORDS,
    REGION_TAB_MAP_FALLBACK, REGION_SALES_TAB_MAP_FALLBACK,
    DEFAULT_RETURN_RATE,
    MARKET_DEFAULTS, RETAIL_PRICE_USD, TRADE_DISCOUNT_BRAND_DEFAULT,
)
from app.lookups import get_region_aliases, get_region_distributor_map, read_input_variables
from app.models import PromoRecord, PromoHistory, MarketEconomics, InfluencerCommercials
from app.regions import region_accepts, REGION_TAB_GROUPS, SIMILAR_MARKETS, NEW_MARKETS, normalize_region

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


def _get_client() -> gspread.Client:
    raw = GOOGLE_SERVICE_ACCOUNT_JSON
    if not raw or raw == "{}":
        raise ValueError("GOOGLE_SERVICE_ACCOUNT_JSON env var is empty or not set")
    creds_dict = json.loads(raw)
    logger.info(f"Authenticating as service account: {creds_dict.get('client_email', 'UNKNOWN')}")
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    return gspread.authorize(creds)


def _discover_tab(sheet, region: str, keyword_map: dict, fallback_map: dict,
                  tab_titles=None) -> Optional[str]:
    """Dynamically find the best matching tab for a region by scanning actual sheet tabs.

    Reads all worksheet titles from the sheet, matches against keyword patterns.
    Falls back to hardcoded map only if discovery finds nothing.

    tab_titles: optional pre-fetched list of worksheet title strings. When provided,
    sheet.worksheets() is skipped (avoids redundant API round trips in aggregate loops).
    """
    keywords = keyword_map.get(region, [])
    if not keywords:
        return fallback_map.get(region)

    if tab_titles is None:
        try:
            tab_titles = [ws.title for ws in sheet.worksheets()]
        except Exception as e:
            logger.error(f"Failed to list worksheets: {e}")
            return fallback_map.get(region)

    # Try each keyword pattern against all tab titles
    for pattern in keywords:
        for title in tab_titles:
            if re.search(pattern, title, re.IGNORECASE):
                logger.info(f"Tab discovery: {region} → '{title}' (matched '{pattern}')")
                return title

    # No match found
    fallback = fallback_map.get(region)
    if fallback:
        logger.warning(f"Tab discovery failed for {region}, using fallback: {fallback}")
    else:
        logger.warning(f"Tab discovery failed for {region}, no fallback available")
    return fallback


def _fuzzy_match(needle: str, haystack: str) -> bool:
    n = needle.lower().strip()
    h = haystack.lower().strip()
    if n in h or h in n:
        return True
    # Match on first word (e.g. "bulkclub canada" matches "bulkclub ca")
    n_first = n.split()[0] if n else ""
    h_first = h.split()[0] if h else ""
    return n_first and h_first and n_first == h_first


def _parse_number(val) -> Optional[float]:
    if val is None or val == "" or val == "-" or val == "—":
        return None
    s = str(val).replace(",", "").replace("$", "").replace("x", "").replace("X", "")
    s = s.replace("€", "").replace("£", "").replace("%", "").replace("~", "").strip()
    try:
        return float(s)
    except (ValueError, TypeError):
        # Handle ranges like "20-40" → midpoint 30
        range_match = re.match(r'^([\d.]+)\s*-\s*([\d.]+)$', s)
        if range_match:
            return (float(range_match.group(1)) + float(range_match.group(2))) / 2
        return None


def _parse_int(val) -> int:
    n = _parse_number(val)
    return int(n) if n is not None else 0


def _find_header_row(all_values: list[list], max_scan: int = 10) -> int:
    """Find the header row index by looking for rows with multiple populated cells
    and known header keywords like 'Retailer', 'Partner', 'Week Ending'."""
    header_keywords = {"retailer", "partner", "week ending", "promo start", "promo end",
                       "promo name", "country", "distributor", "discount", "units sold",
                       "revenue", "mdf cost", "sell-through", "retail price", "commission"}
    for i, row in enumerate(all_values[:max_scan]):
        non_empty = sum(1 for c in row if str(c).strip())
        if non_empty < 3:
            continue
        row_lower = " ".join(str(c).strip().lower() for c in row)
        if any(kw in row_lower for kw in header_keywords):
            return i
    return 0


def _extract_discount_pct(text: str) -> Optional[float]:
    m = re.search(r'(\d{1,3})\s*%', str(text))
    return float(m.group(1)) if m else None


def _extract_month(date_str: str) -> int:
    """Extract month number (1-12) from a date string."""
    # Try YYYY-MM-DD
    m = re.search(r'(\d{4})-(\d{2})', str(date_str))
    if m:
        return int(m.group(2))
    # Try Mon-YY or Month name
    month_names = {
        "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
        "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    }
    for name, num in month_names.items():
        if name in str(date_str).lower():
            return num
    return 0


def _duration_days(start: str, end: str) -> int:
    """Estimate duration in days from start/end strings."""
    try:
        from datetime import datetime
        fmts = (
            "%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y",
            "%d-%m-%Y", "%m-%d-%Y",
            "%d-%B-%Y", "%B-%d-%Y",
            "%d-%b-%Y", "%b-%d-%Y",
            "%d %b %Y", "%b %d, %Y", "%d %B %Y", "%B %d, %Y",
            "%d/%m/%y", "%m/%d/%y",
        )
        for fmt in fmts:
            try:
                d1 = datetime.strptime(start.strip(), fmt)
                d2 = datetime.strptime(end.strip(), fmt)
                return max((d2 - d1).days, 1)
            except ValueError:
                continue
    except Exception:
        pass
    return 7  # default 1 week


# ---------------------------------------------------------------------------
# Promo type inference
# ---------------------------------------------------------------------------

_PROMO_TYPE_KEYWORDS = {
    "spiv": "spiv",
    "newsletter": "newsletter",
    "emailer": "emailer",
    "email blast": "emailer",
    "email campaign": "emailer",
    "e-com": "ecom_campaign",
    "ecom": "ecom_campaign",
    "ecommerce": "ecom_campaign",
    "online campaign": "ecom_campaign",
    "in-store": "in_store",
    "in store": "in_store",
    "social media": "social",
    "instagram": "social",
    "facebook": "social",
    "brg": "promo",
    "discount": "promo",
    "sale": "promo",
    # Gift-with-purchase / bundles are NOT price discounts — they must not be
    # selected as discount comps. Longest-keyword-first ordering in
    # _infer_promo_type ensures "gift with purchase" wins over "gwp".
    "gift with purchase": "gwp",
    "free gift": "gwp",
    "bundle": "gwp",
    "gwp": "gwp",
    # 2026-05-20: Marketing Types the bot does not evaluate. Sentinel routes
    # to a log-only short-circuit in evaluator.evaluate_promo.
    "sales contest": "no_eval",
}


def _infer_promo_type(explicit_type: str, promo_name: str, notes: str) -> str:
    """Infer promo type from explicit column, promo name, or notes.

    Returns: spiv, promo, newsletter, emailer, ecom_campaign, in_store,
    social, gwp, no_eval, or ''.
    """
    # Whole-word "POS" → no_eval. Done BEFORE the substring loop so
    # "position"/"posters" don't false-match "pos".
    if re.search(r"\bpos\b", f"{explicit_type} {promo_name} {notes}", re.IGNORECASE):
        return "no_eval"

    # 1. Explicit column value (longest keyword first so "sales contest" beats "sale")
    if explicit_type.strip():
        val = explicit_type.strip().lower()
        for keyword, ptype in sorted(_PROMO_TYPE_KEYWORDS.items(), key=lambda x: -len(x[0])):
            if keyword in val:
                return ptype
        return val  # use whatever was in the column

    # 2. Infer from promo name + notes
    combined = f"{promo_name} {notes}".lower()
    for keyword, ptype in sorted(_PROMO_TYPE_KEYWORDS.items(), key=lambda x: -len(x[0])):
        if keyword in combined:
            return ptype

    # 3. Default: if there's a discount mentioned, it's a promo
    if any(c == '%' for c in promo_name):
        return "promo"

    return ""


# ---------------------------------------------------------------------------
# Ad Campaign History (SPA/SBA/SDA tabs)
# ---------------------------------------------------------------------------

def get_ad_campaign_data(retailer: str, region: str) -> "AdCampaignData":
    """Read SPA/SBA campaign performance from dedicated sheet tabs.

    Looks for tabs matching patterns like 'Alderon_Rheinshop_SPA', 'Nedermart_SPA',
    '{distributor}_{retailer}_SPA'. Returns aggregated totals from the summary row.
    """
    from app.models import AdCampaignData

    try:
        client = _get_client()
        sheet = client.open_by_key(PROMO_SHEET_ID)
    except Exception as e:
        logger.error(f"ad_campaign_data: auth/open failed: {e}")
        return AdCampaignData(error=str(e))

    try:
        tab_titles = [ws.title for ws in sheet.worksheets()]
    except Exception as e:
        logger.error(f"ad_campaign_data: failed to list tabs: {e}")
        return AdCampaignData(error=str(e))

    # Build search patterns: {distributor}_{retailer}_SPA, {retailer}_SPA, {distributor}_*_SPA
    distributor = get_region_distributor_map().get(region, "")
    patterns = []
    retailer_clean = (retailer or "").strip()
    # Skip distributor name as "retailer" — the actual retailer is usually different
    is_distributor = retailer_clean.lower() == distributor.lower() if distributor else False
    if distributor and retailer_clean and not is_distributor:
        # Exact: Alderon_Rheinshop_SPA
        patterns.append(re.compile(
            rf'{re.escape(distributor)}[_\s]+{re.escape(retailer_clean)}[_\s]+SPA',
            re.IGNORECASE,
        ))
        # Without distributor prefix: Rheinshop_SPA
        patterns.append(re.compile(rf'{re.escape(retailer_clean)}[_\s]+SPA', re.IGNORECASE))
    # Broad: any tab with distributor + SPA (e.g. Alderon_Rheinshop_SPA, Alderon_MediaMarkt_SPA)
    if distributor:
        patterns.append(re.compile(rf'{re.escape(distributor)}[_\s]+\w+[_\s]+SPA', re.IGNORECASE))
    # Distributor-level SPA tab (e.g. Alderon_SPA)
    if distributor:
        patterns.append(re.compile(rf'{re.escape(distributor)}[_\s]+SPA\b', re.IGNORECASE))
    # Retailer-only (e.g. Rheinshop_SPA)
    if retailer_clean and not is_distributor:
        patterns.append(re.compile(rf'{re.escape(retailer_clean)}[_\s]+SPA', re.IGNORECASE))

    # Find matching tab
    matched_tab = None
    for pattern in patterns:
        for title in tab_titles:
            if pattern.search(title):
                matched_tab = title
                break
        if matched_tab:
            break

    if not matched_tab:
        logger.info(f"ad_campaign_data: no SPA/SBA tab found for {retailer}/{region}")
        return AdCampaignData()

    logger.info(f"ad_campaign_data: found tab '{matched_tab}' for {retailer}/{region}")

    try:
        ws = sheet.worksheet(matched_tab)
        all_values = ws.get_all_values()
        if len(all_values) < 2:
            return AdCampaignData(tab_name=matched_tab, error="Tab is empty")

        # Find header row — look for known ad campaign headers
        header_idx = 0
        ad_headers = {"spend", "clicks", "impressions", "roas", "orders", "units sold", "cpc", "ctr", "cvr", "total sales"}
        for i, row in enumerate(all_values[:10]):
            row_lower = " ".join(str(c).strip().lower() for c in row)
            if sum(1 for kw in ad_headers if kw in row_lower) >= 3:
                header_idx = i
                break

        headers = [str(h).strip().lower() for h in all_values[header_idx]]

        # Parse summary row (first data row after header — usually has totals)
        # Look for the first row with numeric spend data
        total_spend = 0.0
        total_sales = 0.0
        total_orders = 0
        total_units = 0
        total_clicks = 0
        total_impressions = 0

        def _col_idx(names: list[str]) -> int:
            for name in names:
                for i, h in enumerate(headers):
                    if name in h:
                        return i
            return -1

        spend_col = _col_idx(["spend"])
        sales_col = _col_idx(["total sales", "sales"])
        orders_col = _col_idx(["orders"])
        units_col = _col_idx(["units sold", "units"])
        clicks_col = _col_idx(["clicks"])
        impressions_col = _col_idx(["impressions"])
        roas_col = _col_idx(["roas"])
        ctr_col = _col_idx(["ctr"])
        cvr_col = _col_idx(["cvr"])

        # Sum across all data rows (per-product rows)
        for row in all_values[header_idx + 1:]:
            if spend_col >= 0 and spend_col < len(row):
                val = _parse_number(row[spend_col])
                if val and val > 0:
                    total_spend += val
            if sales_col >= 0 and sales_col < len(row):
                val = _parse_number(row[sales_col])
                if val:
                    total_sales += val
            if orders_col >= 0 and orders_col < len(row):
                val = _parse_number(row[orders_col])
                if val:
                    total_orders += int(val)
            if units_col >= 0 and units_col < len(row):
                val = _parse_number(row[units_col])
                if val:
                    total_units += int(val)
            if clicks_col >= 0 and clicks_col < len(row):
                val = _parse_number(row[clicks_col])
                if val:
                    total_clicks += int(val)
            if impressions_col >= 0 and impressions_col < len(row):
                val = _parse_number(row[impressions_col])
                if val:
                    total_impressions += int(val)

        if total_spend <= 0:
            return AdCampaignData(tab_name=matched_tab, error="No spend data found")

        roas = round(total_sales / total_spend, 2) if total_spend > 0 else None
        ctr = round(total_clicks / total_impressions * 100, 2) if total_impressions > 0 else None
        cvr = round(total_orders / total_clicks * 100, 2) if total_clicks > 0 else None

        logger.info(f"ad_campaign_data: {matched_tab} → spend={total_spend:.0f}, "
                     f"sales={total_sales:.0f}, orders={total_orders}, units={total_units}, ROAS={roas}")

        return AdCampaignData(
            tab_name=matched_tab,
            total_spend=total_spend,
            total_sales=total_sales,
            total_orders=total_orders,
            total_units=total_units,
            total_clicks=total_clicks,
            total_impressions=total_impressions,
            roas=roas,
            ctr=ctr,
            cvr=cvr,
            available=True,
        )

    except Exception as e:
        logger.error(f"ad_campaign_data: failed to read tab '{matched_tab}': {e}")
        return AdCampaignData(tab_name=matched_tab, error=str(e))


# ---------------------------------------------------------------------------
# Influencer tab shared helper
# ---------------------------------------------------------------------------

def _read_influencer_tab(tab_name: str) -> list[dict]:
    """Open an influencer tab by exact name, return its data rows as a list
    of dicts keyed by header strings. Returns [] on missing tab / empty tab /
    any read error. Blank rows are dropped."""
    try:
        client = _get_client()
        sheet = client.open_by_key(PROMO_SHEET_ID)
        try:
            ws = sheet.worksheet(tab_name)
        except gspread.exceptions.WorksheetNotFound:
            logger.warning(f"{tab_name} tab not found")
            return []
        all_values = ws.get_all_values()
        if len(all_values) < 2:
            return []
        header_idx = _find_header_row(all_values)
        headers = [str(h).strip() for h in all_values[header_idx]]
        raw = []
        for row in all_values[header_idx + 1:]:
            d = {headers[i]: str(row[i]).strip()
                 for i in range(min(len(headers), len(row)))}
            if any(d.values()):
                raw.append(d)
        return raw
    except Exception as e:
        logger.error(f"_read_influencer_tab({tab_name}) failed: {e}")
        return []


# ---------------------------------------------------------------------------
# Influencer Commercials
# ---------------------------------------------------------------------------

def _parse_influencer_commercials(raw: list[dict]) -> InfluencerCommercials:
    """Build InfluencerCommercials from the first data row of the
    'Influencer commercials' tab. Empty input → defaults (source='default').
    Percentage cells like '[REDACTED]' and currency like '[REDACTED]' are stripped by
    _parse_number."""
    if not raw:
        return InfluencerCommercials(source="default")
    r = raw[0]

    def _val(key, default):
        v = _parse_number(r.get(key))
        return v if v is not None else default

    return InfluencerCommercials(
        retail_price_usd=_val("Retail price ($USD)", 0.0),  # REDACTED
        cogs_usd=_val("COGS", 0.0),  # REDACTED
        return_rate_pct=_val("Return Rate", 0.0),  # REDACTED
        commission_pct=_val("Commission", 0.0),  # REDACTED
        warranty_pct=_val("Warranty", 0.0),  # REDACTED
        marketing_pct=_val("Marketing", 0.0),  # REDACTED
        channel_margin_pct=_val("Channel Margin", 0.0),
        marketplace_commission_pct=_val("Marketplace Commission", 0.0),
        tax_pct=_val("Tax", 0.0),
        payment_gateway_pct=_val("Payment Gateway", 0.0),
        source="sheet",
    )


def get_influencer_commercials() -> InfluencerCommercials:
    """Read the 'Influencer commercials' tab. Missing/empty/error → defaults."""
    raw = _read_influencer_tab("Influencer commercials")
    if not raw:
        return InfluencerCommercials(source="default")
    return _parse_influencer_commercials(raw)


# ---------------------------------------------------------------------------
# Influencer Volume Estimate
# ---------------------------------------------------------------------------

def _parse_influencer_volume(raw: list[dict]) -> tuple[float, int]:
    """Average the 'Units Sold' column across 'Influencer past promos' rows.
    Returns (avg_units, n_rows). Rows with no parseable positive Units Sold
    are skipped. Volume is a display-only estimate; never affects the grade."""
    units = []
    for r in raw:
        v = _parse_number(r.get("Units Sold"))
        if v is not None and v > 0:
            units.append(v)
    if not units:
        return 0.0, 0
    return sum(units) / len(units), len(units)


def get_influencer_volume_estimate() -> tuple[float, int]:
    """Read 'Influencer past promos', return (avg_units, n_promos).
    (0.0, 0) on missing/empty tab or any error."""
    raw = _read_influencer_tab("Influencer past promos")
    return _parse_influencer_volume(raw)


# ---------------------------------------------------------------------------
# Promo History
# ---------------------------------------------------------------------------

def _find_retailer_tab(sheet, retailer: str) -> Optional[str]:
    """Look for a tab whose title matches the retailer name directly.

    Used for retailers that have their own dedicated promo history tab
    (e.g. an "Catalogmart" tab for Catalogmart UK history).
    """
    if not retailer:
        return None
    try:
        tab_titles = [ws.title for ws in sheet.worksheets()]
    except Exception:
        return None
    retailer_clean = retailer.strip().lower()
    for title in tab_titles:
        title_lower = title.strip().lower()
        if title_lower == retailer_clean or retailer_clean in title_lower:
            logger.info(f"Retailer tab found: '{title}' for retailer '{retailer}'")
            return title
    return None


def _read_one_tab(sheet, tab_name: str, retailer: str, region: str) -> PromoHistory:
    """Open one worksheet, parse it into PromoHistory (Country filter applied
    via region in _parse_promo_records). Returns empty PromoHistory on
    WorksheetNotFound / read error."""
    try:
        worksheet = sheet.worksheet(tab_name)
    except gspread.exceptions.WorksheetNotFound:
        logger.warning(f"Tab not found: {tab_name}")
        return PromoHistory()

    try:
        all_values = worksheet.get_all_values()
        if len(all_values) < 2:
            return PromoHistory()
        # Find the actual header row (skip title/banner rows)
        # Header rows have multiple populated cells; title rows typically have just 1
        header_idx = _find_header_row(all_values)
        headers = [str(h).strip() for h in all_values[header_idx]]
        logger.info(f"Using header row {header_idx}: {headers[:8]}")
        raw = []
        for row in all_values[header_idx + 1:]:
            row_dict = {headers[i]: str(row[i]).strip() for i in range(min(len(headers), len(row)))}
            raw.append(row_dict)
        logger.info(f"Raw rows: {len(raw)}, first non-empty: {next((r for r in raw if r.get('Retailer') or r.get('retailer')), 'NONE')}")
    except Exception as e:
        logger.error(f"Failed to read sheet: {e}")
        return PromoHistory()

    return _parse_promo_records(raw, retailer, region)


def get_promo_history(retailer: str, region: str) -> PromoHistory:
    """Pull all historical promo records for a retailer/region."""
    try:
        client = _get_client()
        sheet = client.open_by_key(PROMO_SHEET_ID)
    except Exception as e:
        logger.error(f"Sheets auth/open failed: {e}")
        return PromoHistory()

    # Try retailer-specific tab first (e.g. "Catalogmart" tab for Catalogmart UK)
    tab_name = _find_retailer_tab(sheet, retailer)

    # Aggregate path: for aggregate regions (Europe/GCC/APCOM/Nordics) with no
    # retailer-specific tab, read each constituent country's tab and concatenate.
    # The Country filter in _parse_promo_records (via region_accepts) partitions
    # rows correctly because region_accepts("Europe","Germany") is True etc.
    # Pass region=AGGREGATE (not sub) so the Country filter keeps all members.
    if not tab_name:
        group = REGION_TAB_GROUPS.get(region)
        if group:
            merged = PromoHistory()
            seen_tabs: set[str] = set()
            # Pre-fetch worksheet list once to avoid N API round trips (one per constituent).
            try:
                agg_titles = [ws.title for ws in sheet.worksheets()]
            except Exception as e:
                logger.error(f"Failed to list worksheets for aggregate {region}: {e}")
                agg_titles = None
            for sub in group:
                tab = _discover_tab(sheet, sub, REGION_TAB_KEYWORDS, REGION_TAB_MAP_FALLBACK,
                                    tab_titles=agg_titles)
                if not tab or tab in seen_tabs:
                    continue
                seen_tabs.add(tab)
                h = _read_one_tab(sheet, tab, retailer, region)  # region=AGGREGATE for Country filter
                merged.records.extend(h.records)
                if h.baseline_units_weekly and not merged.baseline_units_weekly:
                    merged.baseline_units_weekly = h.baseline_units_weekly
            merged.sample_count = len(merged.records)
            all_roas = [r.roas for r in merged.records if r.roas and r.roas > 0]
            if all_roas:
                merged.avg_roas = round(sum(all_roas) / len(all_roas), 2)
                merged.best_roas = round(max(all_roas), 2)
                merged.worst_roas = round(min(all_roas), 2)
            merged.retailer_matched = f"{region} (aggregate)"
            if merged.records:
                return merged
            # else fall through to existing single-tab discovery (e.g. Gulfshore GCC tab)

    # Fall back to regional tab discovery
    if not tab_name:
        tab_name = _discover_tab(sheet, region, REGION_TAB_KEYWORDS, REGION_TAB_MAP_FALLBACK)
    if not tab_name:
        logger.warning(f"No tab found for region: {region}, retailer: {retailer}")
        return PromoHistory()

    return _read_one_tab(sheet, tab_name, retailer, region)


def _aggregate_by_promo_period(records: list[PromoRecord]) -> list[PromoRecord]:
    """Aggregate per-retailer records into per-promo-period totals.

    Groups by (promo_name, promo_start, promo_end) and sums units/revenue/costs.
    Used for distributor-level queries where we want total impact, not per-retailer.
    """
    groups = defaultdict(list)
    for rec in records:
        key = (rec.promo_name, rec.promo_start, rec.promo_end)
        groups[key].append(rec)

    aggregated = []
    for (promo_name, start, end), group in groups.items():
        total_units = sum(r.units_sold for r in group)
        total_revenue = sum(r.revenue_usd for r in group)
        total_mdf = sum(r.mdf_cost_usd for r in group)
        retailers = list(set(r.retailer for r in group if r.retailer))

        agg = PromoRecord(
            market=group[0].market,
            retailer=f"{len(retailers)} retailers",
            promo_name=promo_name,
            promo_type=group[0].promo_type,
            promo_start=start,
            promo_end=end,
            duration_days=group[0].duration_days,
            units_sold=total_units,
            revenue_usd=total_revenue,
            mdf_cost_usd=total_mdf,
            season_event=group[0].season_event,
            month=group[0].month,
        )
        # Discount: use the most common non-None value
        discounts = [r.discount_pct for r in group if r.discount_pct is not None]
        agg.discount_pct = discounts[0] if discounts else None

        # ROAS: total revenue / total MDF
        if total_mdf > 0:
            agg.roas = round(total_revenue / total_mdf, 2)

        aggregated.append(agg)

    logger.info(f"Aggregated {len(records)} per-retailer records into {len(aggregated)} promo periods")
    return aggregated


def _is_distributor(name: str) -> bool:
    """Check if a name matches a known distributor."""
    distributors = set(v.lower() for v in get_region_distributor_map().values())
    return name.lower().strip() in distributors


def _parse_promo_records(raw: list[dict], retailer: str, region: str) -> PromoHistory:
    """Parse raw sheet rows into PromoHistory."""
    records = []
    roas_values = []
    matched_name = None
    baseline_units = []
    is_distributor_query = _is_distributor(retailer) if retailer else False
    skipped_empty = 0
    skipped_filter = 0
    skipped_total = 0

    for row in raw:
        row_retailer = str(
            row.get("Retailer", row.get("retailer", row.get("Partner", "")))
        ).strip()
        row_distributor = str(row.get("Distributor", "")).strip()
        if not row_retailer and not row_distributor:
            skipped_empty += 1
            continue

        # Match logic:
        # 1. If query is a distributor name → match on Distributor column (take all retailers)
        # 2. If query is empty → take all rows (market-wide fallback)
        # 3. Otherwise → match on Retailer column
        if retailer:
            if is_distributor_query:
                # Match distributor column, or include all if no distributor column
                if row_distributor and not _fuzzy_match(retailer, row_distributor):
                    continue
                elif not row_distributor and not _fuzzy_match(retailer, row_retailer):
                    continue
            else:
                if not _fuzzy_match(retailer, row_retailer) and "total" not in row_retailer.lower():
                    continue

        if "total" in row_retailer.lower():
            skipped_total += 1
            continue  # skip summary rows

        # Country filter: shared tabs (e.g. "Asia Promo Performance" holds
        # AU/NZ/JP/SG) must not bleed across countries. Fail-open: blank or
        # unresolvable Country is kept, so Country-less tabs are unaffected.
        row_country = str(row.get("Country", row.get("country", ""))).strip()
        if region and not region_accepts(region, row_country):
            skipped_filter += 1
            continue

        if is_distributor_query:
            matched_name = f"{retailer.title()} (all retailers)"
        elif not retailer:
            matched_name = f"{region} (all retailers)"
        else:
            matched_name = row_retailer

        start = str(row.get("Promo Start", row.get("Week Ending", "")))
        end = str(row.get("Promo End", row.get("End Date", row.get("Promo End Date", row.get("End", row.get("Promotion End", ""))))))
        if not end:
            known_keys = set(row.keys())
            logger.debug(f"No end date found for row (retailer={row_retailer}). Available columns: {known_keys}")

        promo_name = str(row.get("Promo Name", row.get("Active Promo", "")))
        promo_type = _infer_promo_type(
            str(row.get("Promo Type", row.get("Type", ""))),
            promo_name,
            str(row.get("Notes", "")),
        )

        rec = PromoRecord(
            market=region,
            retailer=row_retailer,
            promo_name=promo_name,
            promo_type=promo_type,
            promo_start=start,
            promo_end=end,
            # NOTE: 0 = "missing/unparseable" sentinel, distinct from "1-week comp".
            # The evaluator skips comps with duration_days <= 0 rather than fabricating
            # a 1-week window (which silently 2-7×'d cross-region volume estimates).
            duration_days=_duration_days(start, end) if end else 0,
            units_sold=_parse_int(row.get("Units Sold", row.get("Sell-Through (Units)", row.get("Units (Sell-in)", row.get("Sales", 0))))),
            revenue_usd=_parse_number(row.get("Revenue (USD)", row.get("Ad Sales (CAD)", 0))) or 0,
            mdf_cost_usd=_parse_number(row.get("the brand MDF Cost (USD)",
                                                row.get("Non-Criteo Spend (CAD)",
                                                        row.get("Total Spend (CAD)", 0)))) or 0,
            season_event=str(row.get("Promo Name", row.get("Seasonality/Event", ""))),
            channel=str(row.get("Non-Criteo Placements", "")),
            notes=str(row.get("Notes", "")),
            month=_extract_month(start),
        )

        # Discount %
        disc = _extract_discount_pct(str(row.get("Discount %", row.get("Discount", ""))))
        rec.discount_pct = disc

        # ROAS
        roas_val = _parse_number(row.get("ROAS", row.get("roas", None)))
        if roas_val and roas_val > 0:
            rec.roas = round(roas_val, 2)
            roas_values.append(roas_val)

        records.append(rec)

        # Classify baseline vs promo
        has_promo = bool(rec.promo_name and rec.promo_name.lower() not in ("", "no", "none", "-"))
        if not has_promo and rec.units_sold > 0:
            weeks = rec.duration_days / 7 if rec.duration_days > 0 else 1
            baseline_units.append(rec.units_sold / weeks)

    # If distributor-level query, aggregate records by promo period
    # so that "Christmas Promotion" sums all retailers' units/revenue/costs
    if is_distributor_query and records:
        records = _aggregate_by_promo_period(records)
        # Recalculate ROAS values from aggregated records
        roas_values = [r.roas for r in records if r.roas and r.roas > 0]

    # Compute baseline
    avg_baseline = None
    if baseline_units:
        avg_baseline = sum(baseline_units) / len(baseline_units)

    # Backfill incremental units on records
    if avg_baseline and avg_baseline > 0:
        for rec in records:
            rec.baseline_units_weekly = avg_baseline
            weeks = rec.duration_days / 7 if rec.duration_days > 0 else 1
            expected_baseline_units = avg_baseline * weeks
            rec.incremental_units = max(0, rec.units_sold - int(expected_baseline_units))

    logger.info(f"Parse results for {region}/{retailer}: {len(records)} records, "
                 f"skipped {skipped_empty} empty, {skipped_total} totals, "
                 f"{skipped_filter} country-filtered, "
                 f"{len(raw) - len(records) - skipped_empty - skipped_total - skipped_filter} retailer-filtered")

    return PromoHistory(
        records=records,
        baseline_units_weekly=round(avg_baseline, 1) if avg_baseline else None,
        avg_roas=round(sum(roas_values) / len(roas_values), 2) if roas_values else None,
        best_roas=round(max(roas_values), 2) if roas_values else None,
        worst_roas=round(min(roas_values), 2) if roas_values else None,
        sample_count=len(records),
        retailer_matched=matched_name,
    )


# ---------------------------------------------------------------------------
# Cross-region fallback
# ---------------------------------------------------------------------------

MIN_PROMOS_FOR_CONFIDENCE = 5  # enrich with cross-region data if below this


def get_promo_history_with_fallback(retailer: str, region: str) -> PromoHistory:
    """Pull promo history, enriching with cross-region data when thin.

    Strategy:
    1. Pull direct match for this retailer/region
    2. If zero promos → try similar markets entirely
    3. If few promos (< 5) → ALSO pull similar-market records and merge them
       as supplementary comps (tagged so the bot can note cross-region source)
    4. Inject sales-tab baseline when promo tab has no non-promo rows
    """
    history = get_promo_history(retailer, region)

    if normalize_region(region) in NEW_MARKETS:
        # New market: no comparable region by design — skip cross-region
        # enrichment. A sell-out baseline (if any) is still useful.
        # records are empty by definition for a brand-new market (sample_count==0),
        # so the per-record baseline back-fill done later in this function is a
        # no-op here and is intentionally skipped by this early return.
        if history.baseline_units_weekly is None:
            sb = get_sales_baseline(region, retailer)
            if sb and sb > 0:
                history.baseline_units_weekly = sb
        history.fallback_source = None
        return history

    if history.sample_count == 0:
        # No data at all → try similar markets as primary source
        similar = SIMILAR_MARKETS.get(region, [])
        for fallback_region in similar:
            fb_history = get_promo_history(retailer, fallback_region)
            if fb_history.sample_count > 0:
                fb_history.fallback_source = f"{fallback_region} (similar market)"
                history = fb_history
                break

    if history.sample_count == 0:
        # Still nothing → try same region, any retailer
        any_retailer_history = get_promo_history("", region)
        if any_retailer_history.sample_count > 0:
            any_retailer_history.fallback_source = f"{region} market average (different retailers)"
            history = any_retailer_history

    # Enrich thin history with cross-region records for better comps
    if 0 < history.sample_count < MIN_PROMOS_FOR_CONFIDENCE:
        similar = SIMILAR_MARKETS.get(region, [])
        cross_region_records = []
        for fallback_region in similar:
            fb_history = get_promo_history("", fallback_region)
            for rec in fb_history.records:
                rec.notes = f"(cross-region: {fallback_region})"
                cross_region_records.append(rec)

        if cross_region_records:
            history.records.extend(cross_region_records)
            # Recalculate ROAS stats with enriched pool
            all_roas = [r.roas for r in history.records if r.roas and r.roas > 0]
            if all_roas:
                history.avg_roas = round(sum(all_roas) / len(all_roas), 2)
                history.best_roas = round(max(all_roas), 2)
                history.worst_roas = round(min(all_roas), 2)
                history.sample_count = len(all_roas)
            if not history.fallback_source:
                history.fallback_source = f"enriched with {', '.join(similar)} data"
            logger.info(f"Enriched {region} history with {len(cross_region_records)} "
                        f"cross-region records from {similar}")

    # Always try to get sell-out baseline — even if no promo records exist.
    # This allows volume estimation for new retailers with no promo history.
    if history.baseline_units_weekly is None:
        sales_baseline = get_sales_baseline(region, retailer)
        if sales_baseline and sales_baseline > 0:
            history.baseline_units_weekly = sales_baseline
            for rec in history.records:
                if not rec.baseline_units_weekly:
                    rec.baseline_units_weekly = sales_baseline
                    weeks = rec.duration_days / 7 if rec.duration_days > 0 else 1
                    expected = sales_baseline * weeks
                    rec.incremental_units = max(0, rec.units_sold - int(expected))
            logger.info(f"Injected sell-out baseline: {sales_baseline} units/week")

    return history


# ---------------------------------------------------------------------------
# Sales baseline (from regional sales tabs like "Gulfshore sales")
# ---------------------------------------------------------------------------

def _is_month_column(header: str) -> bool:
    """Check if a column header looks like a month (e.g. 'Jan-25', 'February 2025', 'Nov')."""
    header_lower = header.lower().strip()
    month_names = {"jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"}
    # Skip aggregate/summary columns
    skip_keywords = {"total", "average", "avg", "grand", "sum", "ytd", "cumulative"}
    if any(kw in header_lower for kw in skip_keywords):
        return False
    # Check if header starts with or contains a month name
    for m in month_names:
        if m in header_lower:
            return True
    return False


def _get_sellout_sheet_data(region: str, retailer: str = "") -> Optional[tuple[float, dict]]:
    """Internal: read sell-out sheet and return (avg_weekly, monthly_data_dict) or None.

    monthly_data_dict = last 3 non-zero months, e.g. {"Jan-26": [REDACTED], "Feb-26": [REDACTED], "Mar-26": [REDACTED]}
    avg_weekly = mean of those 3 months / 4.33
    """
    try:
        client = _get_client()
        sheet = client.open_by_key(SELLOUT_SHEET_ID)
        ws = sheet.worksheet("Sell-out (Monthly)")
        all_values = ws.get_all_values()
    except Exception as e:
        logger.error(f"Failed to read sell-out sheet: {e}")
        return None

    if len(all_values) < 2:
        return None

    headers = all_values[0]
    col_map = {}
    month_cols = []
    for i, h in enumerate(headers):
        hl = str(h).strip().lower()
        if hl == "region":
            col_map["region"] = i
        elif hl == "country":
            col_map["country"] = i
        elif hl == "distributor":
            col_map["distributor"] = i
        elif hl == "retailer":
            col_map["retailer"] = i
        elif _is_month_column(str(h).strip()):
            month_cols.append((i, str(h).strip()))

    if "retailer" not in col_map or not month_cols:
        logger.warning("Sell-out sheet missing retailer column or month columns")
        return None

    from app.lookups import get_region_distributor_map
    dist_map = get_region_distributor_map()
    region_distributor = dist_map.get(region, "").lower()

    region_aliases = get_region_aliases()
    region_names = {region.lower()}
    for alias, reg in region_aliases.items():
        if reg == region:
            region_names.add(alias.lower())

    retailer_lower = retailer.lower().strip() if retailer else ""

    monthly_totals = {}
    matched_rows = 0
    for row in all_values[1:]:
        if not row or len(row) <= col_map.get("retailer", 0):
            continue

        row_retailer = str(row[col_map["retailer"]]).strip().lower()
        row_distributor = str(row[col_map.get("distributor", 0)]).strip().lower() if "distributor" in col_map else ""

        match = False
        if retailer_lower and retailer_lower in row_retailer:
            match = True
        elif retailer_lower and row_retailer in retailer_lower:
            match = True
        elif region_distributor and region_distributor in row_distributor:
            match = True
        elif "country" in col_map:
            row_country = str(row[col_map["country"]]).strip().lower()
            if row_country in region_names:
                match = True

        if not match:
            continue

        matched_rows += 1
        for ci, label in month_cols:
            if ci < len(row):
                val = _parse_int(row[ci])
                if val > 0:
                    monthly_totals[label] = monthly_totals.get(label, 0) + val

    if not monthly_totals:
        logger.info(f"Sell-out sheet: no data found for region={region}, retailer={retailer}")
        return None

    # Take last 3 non-zero months
    month_keys = list(monthly_totals.keys())
    recent_keys = [k for k in month_keys[-6:] if monthly_totals[k] > 0][-3:]
    if not recent_keys:
        return None

    recent_dict = {k: monthly_totals[k] for k in recent_keys}
    avg_monthly = sum(recent_dict.values()) / len(recent_dict)
    avg_weekly = avg_monthly / 4.33

    logger.info(f"Sell-out sheet baseline for {region}/{retailer}: {avg_weekly:.1f} units/week "
                f"(from {len(recent_keys)} months, {matched_rows} matched rows)")
    return round(avg_weekly, 1), recent_dict


def _get_sellout_sheet_baseline(region: str, retailer: str = "") -> Optional[float]:
    """Primary BAU source: returns avg units/week or None."""
    result = _get_sellout_sheet_data(region, retailer)
    return result[0] if result else None


def get_sales_baseline(region: str, retailer: str = "") -> Optional[float]:
    """Read monthly sell-through from sell-out sheet, return avg units/week."""
    return _get_sellout_sheet_baseline(region, retailer)


def get_distributor_bau_breakdown(region: str) -> dict[str, float]:
    """Return per-retailer weekly BAU for all retailers under the region's distributor.

    Returns {retailer_name: avg_weekly_units} using the last 3 non-zero months
    per retailer. Used for distributor-level promo evaluations.
    """
    try:
        client = _get_client()
        sheet = client.open_by_key(SELLOUT_SHEET_ID)
        ws = sheet.worksheet("Sell-out (Monthly)")
        all_values = ws.get_all_values()
    except Exception as e:
        logger.error(f"get_distributor_bau_breakdown: failed to read sell-out sheet: {e}")
        return {}

    if len(all_values) < 2:
        return {}

    headers = all_values[0]
    col_map = {}
    month_cols = []
    for i, h in enumerate(headers):
        hl = str(h).strip().lower()
        if hl == "region":
            col_map["region"] = i
        elif hl == "country":
            col_map["country"] = i
        elif hl == "distributor":
            col_map["distributor"] = i
        elif hl == "retailer":
            col_map["retailer"] = i
        elif _is_month_column(str(h).strip()):
            month_cols.append((i, str(h).strip()))

    if "retailer" not in col_map or "distributor" not in col_map or not month_cols:
        return {}

    from app.lookups import get_region_distributor_map
    region_distributor = get_region_distributor_map().get(region, "").lower()
    if not region_distributor:
        return {}

    # Collect monthly data per retailer
    retailer_monthly: dict[str, dict[str, int]] = {}
    for row in all_values[1:]:
        if not row or len(row) <= col_map.get("retailer", 0):
            continue
        row_distributor = str(row[col_map["distributor"]]).strip().lower()
        if region_distributor not in row_distributor:
            continue
        row_retailer = str(row[col_map["retailer"]]).strip()
        if not row_retailer:
            continue
        if row_retailer not in retailer_monthly:
            retailer_monthly[row_retailer] = {}
        for ci, label in month_cols:
            if ci < len(row):
                val = _parse_int(row[ci])
                if val > 0:
                    retailer_monthly[row_retailer][label] = retailer_monthly[row_retailer].get(label, 0) + val

    # Compute avg weekly for each retailer (last 3 non-zero months)
    breakdown: dict[str, float] = {}
    for retailer_name, monthly_totals in retailer_monthly.items():
        if not monthly_totals:
            continue
        month_keys = list(monthly_totals.keys())
        recent_keys = [k for k in month_keys[-6:] if monthly_totals[k] > 0][-3:]
        if not recent_keys:
            continue
        avg_monthly = sum(monthly_totals[k] for k in recent_keys) / len(recent_keys)
        avg_weekly = round(avg_monthly / 4.33, 1)
        if avg_weekly > 0:
            breakdown[retailer_name] = avg_weekly

    logger.info(f"Distributor BAU breakdown for {region}: {breakdown}")
    return breakdown


def get_sales_velocity(region: str) -> dict:
    """Get velocity data from sales tab. Returns {weekly_run_rate, direction, pct_change}."""
    try:
        client = _get_client()
        sheet = client.open_by_key(PROMO_SHEET_ID)
    except Exception as e:
        logger.error(f"Sheets auth/open failed: {e}")
        return {}

    tab_name = _discover_tab(sheet, region, SALES_TAB_KEYWORDS, REGION_SALES_TAB_MAP_FALLBACK)
    if not tab_name:
        return {}

    try:
        ws = sheet.worksheet(tab_name)
        all_values = ws.get_all_values()
    except Exception as e:
        logger.error(f"Failed to read sales tab {tab_name}: {e}")
        return {}

    if len(all_values) < 3:
        return {}

    header_idx = _find_header_row(all_values)
    headers = all_values[header_idx]

    # Non-pro ringduct keywords to exclude (sizing kits, chargers, accessories)
    _SKIP_PRODUCTS = {"sizing kit", "charger", "strap", "cable", "adapter", "accessory",
                      "accessories", "band", "membership"}

    # Sum all retailers by month (only month columns, skip Total/Average/etc.)
    # Filter: rings only (exclude sizing kits, chargers), positive values only
    monthly_totals = {}
    for row in all_values[header_idx + 1:]:
        if not row or not str(row[0]).strip():
            continue
        row_label = str(row[0]).strip().lower()
        if any(kw in row_label for kw in ("total", "grand", "sum", "average")):
            continue  # skip summary rows
        # Skip non-pro ringducts (sizing kits, chargers, accessories)
        if any(kw in row_label for kw in _SKIP_PRODUCTS):
            continue
        for i in range(1, min(len(headers), len(row))):
            month_label = str(headers[i]).strip()
            if not month_label or not _is_month_column(month_label):
                continue
            val = _parse_int(row[i])
            if val <= 0:
                continue  # skip negative values (returns) and zeros
            monthly_totals[month_label] = monthly_totals.get(month_label, 0) + val

    if not monthly_totals:
        return {}

    month_keys = list(monthly_totals.keys())
    recent = [(k, monthly_totals[k]) for k in month_keys[-4:] if monthly_totals[k] > 0]
    if len(recent) < 2:
        return {}

    latest_label, latest = recent[-1]
    prev_label, previous = recent[-2]
    weekly_rate = latest / 4.33

    logger.info(f"Velocity (rings only, positive): {latest_label}={latest}, {prev_label}={previous}")
    pct_change = ((latest - previous) / previous * 100) if previous > 0 else 0
    if pct_change > 10:
        direction = "up"
    elif pct_change < -10:
        direction = "down"
    else:
        direction = "flat"

    return {
        "weekly_run_rate": round(weekly_rate, 0),
        "direction": direction,
        "pct_change": round(pct_change, 1),
        "latest_month": recent[-1][0],
        "latest_units": latest,
    }


# ---------------------------------------------------------------------------
# Market Economics — read from Input Variables sheet
# ---------------------------------------------------------------------------

def _read_input_variables():
    """Thin wrapper — delegates to lookups.read_input_variables()."""
    return read_input_variables()


def _find_market_row(market_rows: list[dict], region: str) -> dict:
    """Find the row matching this region in Market Economics tab.

    Handles alias mapping: region code 'TH' matches sheet value 'Thailand'.
    Also handles comma-separated Market cells like 'Germany, Austria, Italy'.
    """
    all_aliases = get_region_aliases()
    aliases = set(all_aliases.get(region, [region.lower()]))
    aliases.add(region.lower())  # always include the code itself

    for row in market_rows:
        market_cell = str(row.get("Market", "")).strip().lower()
        # Split comma-separated markets and check each segment
        cell_markets = {m.strip() for m in market_cell.split(",")}
        if aliases & cell_markets:
            return row
    return {}


def _find_partner_row(partner_rows: list[dict], retailer: str, region: str) -> dict:
    """Find a partner override row for this retailer + region."""
    all_aliases = get_region_aliases()
    aliases = set(all_aliases.get(region, [region.lower()]))
    aliases.add(region.lower())

    for row in partner_rows:
        row_market = str(row.get("Market", "")).strip().lower()
        row_retailer = str(row.get("Retailer", "")).strip()
        cell_markets = {m.strip() for m in row_market.split(",")}
        if (aliases & cell_markets) and _fuzzy_match(retailer, row_retailer):
            return row
    return {}


def _find_row_by_distributor(market_rows: list[dict], distributor: str) -> dict:
    """Fallback: find a sheet row by matching the Distributor column.

    Used when region resolution fails (buy_price = 0) but the user
    provided a distributor name. Matches case-insensitively on common
    abbreviations (e.g. 'Albion UK', 'albion uk', 'albionuk' all resolve).
    """
    dist_lower = distributor.lower().replace(" ", "").replace("-", "")
    for row in market_rows:
        cell = str(row.get("Distributor", "")).strip().lower().replace(" ", "").replace("-", "")
        if cell and (cell == dist_lower or cell.startswith(dist_lower) or dist_lower.startswith(cell)):
            return row
    return {}


def get_market_economics(region: str, retailer: str = "", distributor: str = "") -> MarketEconomics:
    """Build market economics from Input Variables sheet.

    Priority: exact retailer+region match > any region match > config defaults.
    The Bot Input Variables tab is a flat table with (Market, Retailer, Distributor, ...) per row.
    """
    market_rows, partner_rows = _read_input_variables()

    # Try exact match: region + retailer
    best_row = {}
    if retailer:
        best_row = _find_partner_row(market_rows, retailer, region)
        if not best_row and partner_rows:
            best_row = _find_partner_row(partner_rows, retailer, region)

    # Fall back to any row for this region
    market_data = _find_market_row(market_rows, region)
    if not best_row:
        best_row = market_data

    # Distributor fallback: if region lookup found nothing, try matching by Distributor column
    if not best_row and distributor:
        best_row = _find_row_by_distributor(market_rows, distributor)
        if best_row:
            logger.info(f"Market economics: region '{region}' resolved via distributor '{distributor}' "
                        f"→ Market={best_row.get('Market', '?')}")

    logger.info(f"Market economics for {region}/{retailer}: "
                f"found row with Market={best_row.get('Market', '?')}, "
                f"Retailer={best_row.get('Retailer', '?')}")

    # --- Buy price (USD) ---
    buy_usd = _parse_number(best_row.get("Buy Price (USD)"))
    buy_price_source = "sheet" if buy_usd is not None else "missing"
    if buy_usd is None:
        # Try market-level fallback if best_row was a specific retailer
        buy_usd = _parse_number(market_data.get("Buy Price (USD)"))
        if buy_usd is not None:
            buy_price_source = "sheet"
    if buy_usd is None:
        defaults = MARKET_DEFAULTS.get(region, {})
        buy_usd = defaults.get("buy_price_local")
        if buy_usd is not None:
            buy_price_source = "default"
    buy_usd = buy_usd or 0

    # --- Trade discount brand share % ---
    # Check both possible column names
    td_pct = (
        _parse_number(best_row.get("Trade Discount the brand Share %"))
        or _parse_number(best_row.get("TD the brand Share %"))
        or _parse_number(market_data.get("Trade Discount the brand Share %"))
        or _parse_number(market_data.get("TD the brand Share %"))
    )
    if td_pct is not None:
        # Sheet stores as whole number (e.g. 50 for 50%)
        if td_pct > 1:
            td_pct = td_pct / 100.0
    else:
        # Try region-specific default before global fallback
        defaults = MARKET_DEFAULTS.get(region, {})
        td_pct = defaults.get("trade_discount_brand_pct", TRADE_DISCOUNT_BRAND_DEFAULT)

    # --- Return rate (from Bot Input Variables tab only) ---
    return_rate_override = (
        _parse_number(best_row.get("Return Rate %"))
        or _parse_number(market_data.get("Return Rate %"))
    )
    if return_rate_override is not None:
        return_rate = return_rate_override / 100.0 if return_rate_override > 1 else return_rate_override
    else:
        return_rate = DEFAULT_RETURN_RATE

    # --- FX rate to USD ---
    fx_rate = (
        _parse_number(best_row.get("FX Rate to USD"))
        or _parse_number(market_data.get("FX Rate to USD"))
    )
    if fx_rate is None:
        defaults = MARKET_DEFAULTS.get(region, {})
        fx_rate = defaults.get("fx_rate_to_usd", 1.0)

    # --- Distributor ---
    distributor = (
        str(best_row.get("Distributor", "")).strip()
        or str(market_data.get("Distributor", "")).strip()
        or get_region_distributor_map().get(region, "")
    )

    retail_usd = (
        _parse_number(best_row.get("Retail Price (USD)"))
        or _parse_number(best_row.get("Retail Price"))
        or _parse_number(market_data.get("Retail Price (USD)"))
        or _parse_number(market_data.get("Retail Price"))
        or RETAIL_PRICE_USD
    )

    econ = MarketEconomics(
        market=region,
        currency="USD",
        fx_rate_to_usd=fx_rate,
        buy_price_local=buy_usd,
        buy_price_usd=buy_usd,
        retail_price_usd=retail_usd,
        channel_margin_usd=retail_usd - buy_usd if buy_usd > 0 else 0,
        trade_discount_brand_pct=td_pct,
        return_rate=return_rate,
        distributor=distributor,
        buy_price_source=buy_price_source,
    )

    if buy_usd == 0:
        logger.warning(f"Buy price missing for {region}/{retailer} — evaluation will be unreliable")
    elif buy_price_source == "default":
        logger.warning(
            f"Buy price for {region}/{retailer} fell back to MARKET_DEFAULTS "
            f"(${buy_usd}). May be stale — populate Bot Input Variables sheet."
        )

    return econ


# ---------------------------------------------------------------------------
# Legacy compatibility
# ---------------------------------------------------------------------------

class ROASData:
    def __init__(self, avg_roas=None, best_roas=None, worst_roas=None,
                 sample_count=0, retailer_matched=None):
        self.avg_roas = avg_roas
        self.best_roas = best_roas
        self.worst_roas = worst_roas
        self.sample_count = sample_count
        self.retailer_matched = retailer_matched


def lookup_roas(retailer: str, region: str) -> ROASData:
    history = get_promo_history(retailer, region)
    return ROASData(
        avg_roas=history.avg_roas,
        best_roas=history.best_roas,
        worst_roas=history.worst_roas,
        sample_count=history.sample_count,
        retailer_matched=history.retailer_matched,
    )
