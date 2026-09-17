import logging
import re
from dataclasses import dataclass, field
from typing import Optional

from app.lookups import get_retailer_region_map, get_region_keywords
from app.regions import normalize_region

# --- Regex patterns ---
# Discount: prefer "X% off/discount/brg" over bare "X%"
DISCOUNT_PCT_EXPLICIT_RE = re.compile(
    r'(\d{1,3}(?:\.\d+)?)\s*%\s*(?:off|discount|brg|promo|promotion|retail|sale)',
    re.IGNORECASE,
)
DISCOUNT_PCT_RE = re.compile(r'(\d{1,3})\s*%')
DISCOUNT_AMOUNT_RE = re.compile(
    r'(?:USD|EUR|€|£|\$)\s*([\d,]+(?:\.\d{1,2})?)\s*([kKmM](?=\b|\s|$))?',
    re.IGNORECASE,
)
# Keep backward-compat alias
DISCOUNT_DOLLAR_RE = DISCOUNT_AMOUNT_RE
SOA_PER_UNIT_RE = re.compile(
    # "EUR 2.50 per unit SOA" / "€3/unit SOA" / "$2.50 per ring" / "USD 34.5 per ring"
    r'(?:USD|EUR|€|£|\$)\s*([\d,]+(?:\.\d{1,2})?)\s*(?:/\s*unit|(?:per|for\s+each)\s+(?:unit|ring|piece)(?:\s+sold)?)\s*(?:SOA)?'
    # "SOA of EUR 2.50 per unit" / "SOA of EUR 2.50"
    r'|SOA\s*(?:of\s+)?(?:USD|EUR|€|£|\$)\s*([\d,]+(?:\.\d{1,2})?)\s*(?:/\s*unit|(?:per|for\s+each)\s+(?:unit|ring|piece)(?:\s+sold)?)?'
    # "EUR 2.50 SOA for each unit sold" / "EUR 2.50 additional SOA for each unit"
    r'|(?:USD|EUR|€|£|\$)\s*([\d,]+(?:\.\d{1,2})?)\s+(?:additional\s+)?SOA\s+(?:per|for\s+each)\s+(?:unit|ring|piece)(?:\s+sold)?',
    re.IGNORECASE,
)
DATE_RE = re.compile(
    r'(\d{1,2}[/\-\.]\d{1,2}[/\-\.]\d{2,4})'
    # "Mar 23", "April 6, 2026"
    r'|(\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*\s+\d{1,2}(?:\s*[,\-]\s*\d{2,4})?)'
    # "23 Mar", "6 April" (European day-before-month)
    r'|(\b\d{1,2}\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*(?:\s+\d{2,4})?)',
    re.IGNORECASE,
)
DURATION_RE = re.compile(r'(\d+)\s*(?:days?|weeks?|months?)', re.IGNORECASE)
# "brand funding 12.5%" or "the brand bearing 50%" or "brand share 12.5%"
BRAND_FUNDING_RE = re.compile(
    r'the brand\s+(?:funding|bearing|share|pays?|covers?|funds?)\s+(?:the\s+)?(?:remaining\s+)?(\d+(?:\.\d+)?)\s*%',
    re.IGNORECASE,
)
# "the brand bears: USD 34.5 per ring" / "the brand bears USD 34.5 per unit" — dollar-based TD per unit
BRAND_BEARS_DOLLAR_RE = re.compile(
    r'the brand\s+bears?[:\s]+(?:USD|EUR|€|£|\$)\s*([\d,]+(?:\.\d{1,2})?)\s*(?:/\s*(?:unit|ring)|(?:per|for\s+each)\s+(?:unit|ring|piece))',
    re.IGNORECASE,
)
# "50% contribution" or "X% the brand contribution/share/split"
CONTRIBUTION_RE = re.compile(
    r'(\d{1,3}(?:\.\d+)?)\s*%\s*(?:contribution|brand\s+share|brand\s+split|brand\s+contribution)',
    re.IGNORECASE,
)
# Rebate percentage: "5% rebate", "3% rebate on buy price", "Rebate: 5%", "Extra Rebate: 5%"
REBATE_PCT_RE = re.compile(
    r'(\d{1,3}(?:\.\d+)?)\s*%\s*rebate'
    r'|(?:extra\s+)?rebate[:\s]+(\d{1,3}(?:\.\d+)?)\s*%',
    re.IGNORECASE,
)
# Non-discount % contexts to skip (keyword after %)
NON_DISCOUNT_PCT_RE = re.compile(
    r'(\d{1,3}(?:\.\d+)?)\s*%\s*(?:contribution|brand|share|split|return|margin|cm3|roas|tacos|rebate)',
    re.IGNORECASE,
)
# Non-discount % contexts: keyword before % (e.g. "bears 100%", "funds 50%")
NON_DISCOUNT_BEFORE_RE = re.compile(
    r'(?:bears?|bearing|funds?|funding|covers?|covering|pays?|paying|supports?)\s+(?:the\s+)?(?:remaining\s+)?(\d{1,3}(?:\.\d+)?)\s*%',
    re.IGNORECASE,
)
# SPIV per-unit cost: "$17.50 per unit", "EUR 15/unit SPIV"
SPIV_PER_UNIT_RE = re.compile(
    r'(?:USD|EUR|€|£|\$)\s*([\d,]+(?:\.\d{1,2})?)\s*(?:/\s*unit|(?:per|for\s+each)\s+(?:unit|ring|piece)(?:\s+sold)?)\s*(?:SPIV)?'
    r'|SPIV\s*(?:of\s+)?(?:USD|EUR|€|£|\$)\s*([\d,]+(?:\.\d{1,2})?)\s*(?:/\s*unit|(?:per|for\s+each)\s+(?:unit|ring|piece)(?:\s+sold)?)?',
    re.IGNORECASE,
)
# Promo type keywords in message text
_PROMO_TYPE_PATTERNS = [
    (re.compile(r'\bPOS\b|\bPOS\s+(?:display|investment|unit)\b|point[\s-]of[\s-]sale|display\s+(?:unit|investment)', re.IGNORECASE), "pos"),
    (re.compile(r'\bSPIV\b', re.IGNORECASE), "spiv"),
    # Newsletter SPONSORSHIP = third-party list buy (cold audience → D2C reach funnel).
    # Must come before the generic \bnewsletter\b pattern so "newsletter sponsorship"
    # isn't first matched as plain "newsletter".
    (re.compile(r'\bnewsletter\s+sponsorship\b|\bsponsored\s+newsletter\b|\bemail\s+(?:blast|sponsorship)\b|\bthird[\s-]party\s+(?:newsletter|email)\b|\b(?:audienceserv|email\s+placement)\b', re.IGNORECASE), "newsletter_sponsorship"),
    (re.compile(r'\bnewsletter\b', re.IGNORECASE), "newsletter"),
    (re.compile(r'\bemailer\b', re.IGNORECASE), "emailer"),
    (re.compile(r'\bemail\s+campaign\b', re.IGNORECASE), "emailer"),
    (re.compile(r'\be-?com(?:merce)?\s+campaign\b', re.IGNORECASE), "ecom_campaign"),
    (re.compile(r'\bonline\s+(?:\w+\s+)?campaign\b', re.IGNORECASE), "ecom_campaign"),
    (re.compile(r'\bmarketing\s+(?:campaign|spend|investment)\b', re.IGNORECASE), "ecom_campaign"),
    (re.compile(r'\bdigital\s+(?:ads?|marketing|campaign)\b', re.IGNORECASE), "ecom_campaign"),
    (re.compile(r'\bhomepage\s+(?:placement|banner|feature)\b', re.IGNORECASE), "ecom_campaign"),
    (re.compile(r'\bSPA\b'), "ecom_campaign"),                    # Sponsored Products Ads
    (re.compile(r'\bSBA\b'), "ecom_campaign"),                    # Sponsored Brands Ads
    (re.compile(r'\bSDA\b'), "ecom_campaign"),                    # Sponsored Display Ads
    (re.compile(r'\bsponsored\s+(?:products?|brands?|display)\b', re.IGNORECASE), "ecom_campaign"),
    (re.compile(r'\bin[\s-]store\b', re.IGNORECASE), "in_store"),
    (re.compile(r'\bBRG\b', re.IGNORECASE), "promo"),
]

# Amazon ad channel keywords — used to identify SPA/SBA campaigns for ROAS-based estimation
_AD_CHANNEL_PATTERNS = [
    (re.compile(r'\bSPA\b'), "SponsoredProducts"),
    (re.compile(r'\bsponsored\s+products?\s+ads?\b', re.IGNORECASE), "SponsoredProducts"),
    (re.compile(r'\bSBA\b'), "SponsoredBrands"),
    (re.compile(r'\bsponsored\s+brands?\s+ads?\b', re.IGNORECASE), "SponsoredBrands"),
    (re.compile(r'\bSDA\b'), "SponsoredDisplay"),
    (re.compile(r'\bsponsored\s+display\s+ads?\b', re.IGNORECASE), "SponsoredDisplay"),
]


@dataclass
class PromoRequest:
    retailer: Optional[str] = None
    region: Optional[str] = None
    promo_type: str = ""  # promo, spiv, newsletter, emailer, ecom_campaign, etc.
    promo_scope: str = "retailer"  # "retailer" | "distributor" — controls BAU aggregation
    discount_pct: Optional[float] = None
    discount_amount: Optional[float] = None
    discount_currency: str = "USD"  # currency of discount_amount: USD, EUR, GBP
    brand_funding_pct: Optional[float] = None  # the brand's share of the discount (e.g. 12.5 means the brand pays 12.5% of retail)
    brand_bears_per_unit: Optional[float] = None  # the brand's flat $ TD per unit (e.g. "the brand bears USD 34.5 per ring")
    soa_per_unit: Optional[float] = None    # Sell-Out Activation cost per unit (flat $)
    rebate_pct: Optional[float] = None     # Rebate % of buy price per unit (e.g. 5 = 5%)
    spiv_per_unit: Optional[float] = None   # SPIV cost per unit (paid on ALL units, not just incremental)
    addon_cogs_per_unit: Optional[float] = None  # Additional per-unit cost (e.g. free BAND giveaway)
    addon_cogs_label: str = ""  # Label for the addon (e.g. "Free Case")
    ad_channels: list = field(default_factory=list)  # ["SponsoredProducts", "SponsoredBrands"] for SPA/SBA
    dates: list = field(default_factory=list)
    duration: Optional[str] = None
    distributor: Optional[str] = None  # optional override — used as fallback if region lookup fails
    store_name: Optional[str] = None  # specific store name for POS display eval (e.g. "Yas Mall")
    # Slack workflow form routing fields (added 2026-05). `channel` is the
    # primary routing key — D2C / Retail / Marketplace. `marketing_scope_label`
    # is a secondary alias that the form may use instead.
    channel: Optional[str] = None
    marketing_scope_label: Optional[str] = None
    # Influencer channel-level eval (2026-05-21). 0-100 convention.
    # 0.0 = not supplied by the submitter; the influencer evaluator falls
    # back to InfluencerCommercials.commission_pct (the [REDACTED] sheet default).
    commission_pct: float = 0.0
    raw_text: str = ""
    parse_confidence: str = "high"  # high | low


_WORKFLOW_FIELDS = {
    "channel", "marketing scope", "distributor",
    "retailer", "region", "marketing type", "discount %", "discount",
    "marketing investment", "spiv per unit", "soa per unit",
    "start date", "end date", "additional context", "context",
    "store name",
    "commission %", "commission", "commission percent",
}


# Retailer-field values that mean "this is a D2C storewide promo, not a
# retailer/distributor promo" — route to the D2C evaluator.
_D2C_RETAILER_TOKENS = {
    "d2c", "d2c website", "website", "online", "storewide",
    "brand.com", "brand website", "brand direct",
    "own site", "own website",
}


def is_d2c_request(promo) -> bool:
    """True if `promo` describes a D2C storewide promo rather than a
    retailer/distributor promo. Used to route between the two evaluators.

    Matches on (in priority order):
      1. Workflow `Channel` field set to D2C / Website / Storewide
      2. Workflow `Marketing scope` set to D2C / Website / Storewide
      3. Retailer field set to a known D2C alias
      4. Marketing type explicitly D2C / Storewide / Website
      5. Last-resort: text mentions "D2C" with no retailer set
    """
    if not promo:
        return False

    # 1. Slack workflow form's "Channel" field — the new explicit routing key.
    channel_label = getattr(promo, "channel", "") or ""
    if channel_label.strip().lower() in _D2C_RETAILER_TOKENS:
        return True

    # 2. Workflow form's "Marketing scope" — alternate routing key.
    scope_label = getattr(promo, "marketing_scope_label", "") or ""
    if scope_label.strip().lower() in _D2C_RETAILER_TOKENS:
        return True

    # 3. Retailer field (legacy fallback for forms that don't use Channel).
    retailer = (promo.retailer or "").strip().lower()
    if retailer in _D2C_RETAILER_TOKENS:
        return True

    # 4. Marketing type contains D2C/storewide markers.
    promo_type = (promo.promo_type or "").strip().lower()
    if any(tok in promo_type for tok in ("d2c", "storewide", "website")):
        return True

    # 5. Last resort: explicit D2C mention in raw text without a retailer.
    raw = (promo.raw_text or "").lower()
    if not retailer and "d2c" in raw:
        return True
    return False


def parse_workflow_message(text: str) -> "PromoRequest | None":
    """Parse a structured workflow message (key-value pairs, one per line).

    Returns PromoRequest if the message looks like a workflow form submission,
    None if it doesn't match the expected format.
    """
    cleaned = _clean_slack_formatting(text)
    lines = [l.strip() for l in cleaned.split("\n") if l.strip()]

    # Detect workflow format: look for known field labels
    fields = {}
    i = 0
    while i < len(lines):
        label = lines[i].lower().rstrip(":")
        if label in _WORKFLOW_FIELDS and i + 1 < len(lines):
            value = lines[i + 1].strip()
            # Skip if next line is also a known field (means this field was empty)
            if value.lower().rstrip(":") in _WORKFLOW_FIELDS:
                fields[label] = ""
                i += 1
            else:
                fields[label] = value
                i += 2
        else:
            i += 1

    # Workflow messages must have at least one identifying field. Channel +
    # Region is the new D2C-friendly minimum (no retailer required for D2C);
    # retailer + one other field still works for retailer submissions.
    has_retailer = "retailer" in fields
    has_channel_d2c_form = "channel" in fields and "region" in fields
    if not (has_retailer or has_channel_d2c_form) or len(fields) < 2:
        return None

    retailer = fields.get("retailer", "")
    region = fields.get("region", "")
    marketing_type = fields.get("marketing type", "promo")
    discount_str = fields.get("discount %", fields.get("discount", ""))
    investment_raw = fields.get("marketing investment", "")
    spiv_str = fields.get("spiv per unit", "")
    start_date = fields.get("start date", "")
    end_date = fields.get("end date", "")
    context = fields.get("additional context", fields.get("context", ""))
    store_name = fields.get("store name", "").strip() or None
    channel_label = fields.get("channel", "").strip() or None
    marketing_scope_label = fields.get("marketing scope", "").strip() or None
    distributor_label = fields.get("distributor", "").strip() or None
    commission_str = fields.get("commission %",
                                fields.get("commission percent",
                                           fields.get("commission", ""))).strip()
    try:
        commission_pct_val = float(commission_str.rstrip("%").strip()) if commission_str else 0.0
    except (ValueError, TypeError):
        commission_pct_val = 0.0

    if not commission_pct_val and context:
        commission_pct_val = _extract_commission_from_context(context)

    def _safe_float(s):
        s = s.replace(",", "").replace("$", "").replace("€", "").replace("£", "").replace("%", "").strip()
        try:
            return float(s) if s else None
        except (ValueError, TypeError):
            return None

    # Parse marketing investment: "USD 20000" or "20000"
    spend_currency = "USD"
    spend_amount = None
    if investment_raw:
        parts = investment_raw.split()
        if len(parts) >= 2 and parts[0].isalpha():
            spend_currency = parts[0].upper()
            spend_amount = _safe_float(" ".join(parts[1:]))
        else:
            spend_amount = _safe_float(investment_raw)

    # Parse dates
    import dateparser
    dates = []
    for d in [start_date, end_date]:
        if d:
            parsed = dateparser.parse(d, settings={"PREFER_DATES_FROM": "future"})
            if parsed:
                dates.append(parsed.strftime("%Y-%m-%d"))
            else:
                dates.append(d)

    duration = None
    if len(dates) == 2:
        from datetime import datetime
        try:
            d1 = datetime.strptime(dates[0], "%Y-%m-%d")
            d2 = datetime.strptime(dates[1], "%Y-%m-%d")
            days = (d2 - d1).days
            if days > 0:
                duration = f"{days} days"
        except ValueError:
            pass

    # Map marketing type text to promo_type code
    type_lower = marketing_type.lower().strip()
    promo_type = "promo"
    # Explicit POS check first: catches "POS", "POS Investment", "pos investment"
    if type_lower.startswith("pos") or type_lower in ("display", "display investment", "displays"):
        promo_type = "pos"
    else:
        for pattern, ptype in _PROMO_TYPE_PATTERNS:
            if pattern.search(type_lower):
                promo_type = ptype
                break

    raw_parts = [f"{retailer} ({region}) — {marketing_type}"]
    if discount_str:
        raw_parts.append(f"{discount_str}% discount")
    if spend_amount:
        raw_parts.append(f"{spend_currency} {spend_amount} spend")
    if spiv_str:
        raw_parts.append(f"SPIV {spiv_str}/unit")
    if dates:
        raw_parts.append(f"Dates: {' to '.join(dates)}")
    if context:
        raw_parts.append(f"Context: {context}")

    return PromoRequest(
        retailer=retailer or None,
        region=region or None,
        promo_type=promo_type,
        discount_pct=_safe_float(discount_str),
        discount_amount=spend_amount,
        discount_currency=spend_currency,
        spiv_per_unit=_safe_float(spiv_str) if spiv_str else None,
        store_name=store_name,
        distributor=distributor_label,
        channel=channel_label,
        marketing_scope_label=marketing_scope_label,
        commission_pct=commission_pct_val,
        dates=dates,
        duration=duration,
        raw_text=" | ".join(raw_parts),
        parse_confidence="high",
    )


def _clean_slack_formatting(text: str) -> str:
    text = re.sub(r'<@[A-Z0-9]+>', '', text)  # remove user mentions
    text = re.sub(r'<#[A-Z0-9]+\|([^>]+)>', r'#\1', text)  # channel refs
    text = re.sub(r'[*_~`]', '', text)  # bold/italic/strike/code
    text = re.sub(r'<(https?://[^|>]+)(?:\|[^>]+)?>', r'\1', text)  # links
    return text.strip()


def _find_retailer(text: str) -> tuple[Optional[str], Optional[str]]:
    text_lower = text.lower()
    retailer_map = get_retailer_region_map()
    # Try longest match first
    sorted_retailers = sorted(retailer_map.keys(), key=len, reverse=True)
    for name in sorted_retailers:
        if name in text_lower:
            return name.title(), retailer_map[name]
    return None, None


def _find_discount(text: str) -> tuple[Optional[float], Optional[float], str]:
    # 1. Try explicit discount pattern first: "22% off", "20% discount", "12% BRG"
    explicit_match = DISCOUNT_PCT_EXPLICIT_RE.search(text)
    if explicit_match:
        pct = float(explicit_match.group(1))
    else:
        # 2. Fall back to bare "X%" but skip non-discount contexts
        non_discount_positions = {m.start() for m in NON_DISCOUNT_PCT_RE.finditer(text)}
        # Also skip "bears 100%", "funds 50%" etc. where keyword precedes the %
        for m in NON_DISCOUNT_BEFORE_RE.finditer(text):
            # Find the position of the digit group within the match
            digit_start = m.start(1)
            non_discount_positions.add(digit_start)
        pct = None
        for m in DISCOUNT_PCT_RE.finditer(text):
            if m.start() not in non_discount_positions:
                pct = float(m.group(1))
                break

    # Find marketing spend amount, but skip per-unit amounts (SPIV/SOA)
    amt = None
    currency = "USD"
    for dollar_match in DISCOUNT_AMOUNT_RE.finditer(text):
        # Skip if followed by /unit, per unit, per ring (these are SPIV/SOA costs)
        end_pos = dollar_match.end()
        after = text[end_pos:end_pos + 30].strip().lower()
        if after.startswith(('/unit', 'per unit', 'per ring', '/ring', 'per piece')):
            continue
        # Skip amounts that are addon COGS (brand addon cost, giveaway, etc.)
        if re.search(r'^(?:as\s+(?:part\s+of\s+)?)?(?:brand\w*|cogs|giveaway|addon)', after):
            continue
        before = text[max(0, dollar_match.start() - 30):dollar_match.start()].lower()
        if re.search(r'(?:cogs|free\s+brand\w*|brand\w+\s+cost)', before):
            continue
        raw_amt = float(dollar_match.group(1).replace(',', ''))
        suffix = (dollar_match.group(2) or '').lower()
        if suffix == 'k':
            raw_amt *= 1000
        elif suffix == 'm':
            raw_amt *= 1000000
        amt = raw_amt
        # Detect currency from the matched prefix
        matched_text = dollar_match.group(0)
        if matched_text.startswith(('EUR', '€', 'eur')):
            currency = "EUR"
        elif matched_text.startswith('£'):
            currency = "GBP"
        # $ = USD (no FX conversion needed)
        break
    return pct, amt, currency


def _find_dates(text: str) -> list[str]:
    return [m.group(0) for m in DATE_RE.finditer(text)]


def _find_duration(text: str) -> Optional[str]:
    m = DURATION_RE.search(text)
    return m.group(0) if m else None


def _find_brand_funding(text: str, discount_pct: Optional[float] = None) -> Optional[float]:
    """Extract the brand's funding from text.

    Handles two patterns:
    1. "brand funding 12.5%" → the brand pays 12.5% of retail price (absolute)
    2. "50% contribution" → the brand pays 50% of the discount (TD share)
       Converted to absolute: 50% × discount_pct = the brand's % of retail
    """
    # Pattern 1: "brand funding/bearing/share X%"
    m = BRAND_FUNDING_RE.search(text)
    if m:
        return float(m.group(1))

    # Pattern 2: "X% contribution" → TD share, convert to absolute brand funding
    m = CONTRIBUTION_RE.search(text)
    if m and discount_pct:
        td_share = float(m.group(1)) / 100.0  # e.g. 50% → 0.5
        return round(discount_pct * td_share, 2)  # e.g. 22% × 50% = 11%

    return None


def _find_soa_per_unit(text: str) -> Optional[float]:
    """Extract SOA per-unit cost from text.

    Matches patterns like: EUR 2.50 per unit SOA, €3/unit SOA, $2.50 per ring, USD 34.5 per ring
    Skips amounts preceded by "the brand bears/support/cost/share" (those are TD, not SOA).
    """
    # If "the brand bears $X per ring" is present, that's TD — don't also treat it as SOA
    if BRAND_BEARS_DOLLAR_RE.search(text):
        return None
    m = SOA_PER_UNIT_RE.search(text)
    if m:
        # Skip if preceded by "the brand support", "the brand cost", "brand share" — that's TD, not SOA
        before = text[:m.start()].strip().lower()
        if before.endswith(('brand support:', 'brand cost:', 'brand share:', 'support:',
                           'brand bears:', 'brand bears', 'bears:', 'bears')):
            return None
        # Groups: (1) amount before SOA, (2) amount after "SOA of", (3) amount before "SOA" suffix
        raw = m.group(1) or m.group(2) or m.group(3)
        if raw:
            return float(raw.replace(',', ''))
    return None


def _find_rebate_pct(text: str) -> Optional[float]:
    """Extract rebate percentage from text.

    Matches: '5% rebate', '3% rebate on buy price', 'Rebate: 5%', 'Extra Rebate: 5%'
    Returns the percentage as a number (e.g. 5.0 for 5%).
    """
    m = REBATE_PCT_RE.search(text)
    if m:
        raw = m.group(1) or m.group(2)
        return float(raw) if raw else None
    return None


def _find_promo_type(text: str) -> str:
    """Detect promo type from message text."""
    for pattern, ptype in _PROMO_TYPE_PATTERNS:
        if pattern.search(text):
            return ptype
    return "promo"  # default


def _find_spiv_per_unit(text: str) -> Optional[float]:
    """Extract SPIV per-unit cost from text.

    Matches: $17.50 per unit, EUR 15/unit SPIV, SPIV of $17.50/unit
    """
    m = SPIV_PER_UNIT_RE.search(text)
    if m:
        raw = m.group(1) or m.group(2)
        if raw:
            return float(raw.replace(',', ''))
    # Also check for "SPIV" near a currency amount without "per unit"
    # e.g. "$17.50 SPIV" or "SPIV $17.50"
    if re.search(r'\bSPIV\b', text, re.IGNORECASE):
        m = re.search(
            r'(?:EUR|€|£|\$)\s*([\d,]+(?:\.\d{1,2})?)\s*(?:SPIV|per\s+unit)',
            text, re.IGNORECASE,
        )
        if m:
            return float(m.group(1).replace(',', ''))
    return None


def _find_region(text: str) -> Optional[str]:
    """Fallback region detection from keywords when retailer match didn't provide one."""
    text_lower = text.lower()
    keywords = get_region_keywords()
    # Try longest match first to avoid "us" matching inside other words
    for keyword in sorted(keywords.keys(), key=len, reverse=True):
        # Use word boundary check to avoid partial matches (e.g. "us" in "discuss")
        pattern = r'\b' + re.escape(keyword) + r'\b'
        if re.search(pattern, text_lower):
            return normalize_region(keywords[keyword]) or keywords[keyword]
    return None


ADDON_COGS_RE = re.compile(
    # Pattern 1: "include in COGS another $54 as part of BAND cost" / "include in COGS $54 BAND"
    r'(?:include\s+in\s+COGS\s+(?:another\s+)?)'
    r'\$?\s*([\d,]+(?:\.\d{1,2})?)\s*(?:as\s+(?:part\s+of\s+)?)?'
    r'(BAND[A-Z]*|[A-Za-z]+)?\s*(?:cost|cogs|giveaway)?'
    # Pattern 2: "free BAND ($54)" / "free BAND $54" / "include BAND cost $54"
    r'|(?:free|include|add)\s+(BAND[A-Z]*)\s*'
    r'(?:(?:as\s+)?(?:a\s+)?(?:cost|cogs|giveaway))?\s*'
    r'(?:\(?\s*\$?\s*([\d,]+(?:\.\d{1,2})?)\s*\)?)?'
    # Pattern 3: "$54 as BAND cost" / "include $54 as BAND cost"
    r'|(?:include\s+|add\s+)?\$\s*([\d,]+(?:\.\d{1,2})?)\s*(?:as\s+(?:part\s+of\s+)?)?'
    r'(BAND[A-Z]*)\s*(?:cost|cogs|giveaway)'
    # Pattern 4: "BAND retails at $54" / "BAND costs $54" / "BAND $54"
    r'|(BAND[A-Z]*)\s+(?:retails?\s+(?:at\s+)?|costs?\s+)?\$?\s*([\d,]+(?:\.\d{1,2})?)',
    re.IGNORECASE,
)


def _find_addon_cogs(text: str) -> tuple[Optional[float], str]:
    """Parse additional per-unit COGS like free product giveaways."""
    m = ADDON_COGS_RE.search(text)
    if not m:
        return None, ""
    # Extract amount and label from whichever pattern matched
    # Pattern 1: groups 1,2 - "include in COGS $54 BAND"
    # Pattern 2: groups 3,4 - "free BAND ($54)"
    # Pattern 3: groups 5,6 - "$54 as BAND cost"
    # Pattern 4: groups 7,8 - "BAND retails at $54"
    if m.group(1):  # Pattern 1
        amount = m.group(1)
        label = (m.group(2) or "addon").strip()
    elif m.group(3):  # Pattern 2
        label = m.group(3).strip()
        amount = m.group(4)
    elif m.group(5):  # Pattern 3
        amount = m.group(5)
        label = (m.group(6) or "addon").strip()
    elif m.group(7):  # Pattern 4
        label = m.group(7).strip()
        amount = m.group(8)
    else:
        return None, ""
    if amount:
        return float(amount.replace(",", "")), f"Free {label.upper()}"
    return None, ""


def _find_brand_bears_per_unit(text: str) -> Optional[float]:
    """Extract the brand's flat dollar TD per unit from text.

    Matches: 'the brand bears: USD 34.5 per ring', 'the brand bears $34.50/unit'
    This REPLACES the formula-based trade discount (retail × discount% × TD_share%).
    """
    m = BRAND_BEARS_DOLLAR_RE.search(text)
    if m:
        return float(m.group(1).replace(',', ''))
    return None


def _find_ad_channels(text: str) -> list[str]:
    """Detect Amazon ad channels (SPA, SBA, SDA) from message text."""
    channels = []
    seen = set()
    for pattern, channel in _AD_CHANNEL_PATTERNS:
        if pattern.search(text) and channel not in seen:
            channels.append(channel)
            seen.add(channel)
    return channels


# Two separate patterns for the two word orders so re.finditer never skips
# an overlapping match due to alternation ordering. \b before comm prevents
# matching inside "recommend", "accommodate", "ecommerce", etc.
_COMMISSION_KW_FIRST_RE = re.compile(
    r'\bcomm(?:ission)?[^\d\n]{0,20}?(\d{1,2}(?:\.\d{1,2})?)\s*%?',
    re.IGNORECASE,
)
_COMMISSION_NUM_FIRST_RE = re.compile(
    r'(\d{1,2}(?:\.\d{1,2})?)\s*%?[^\d\n]{0,20}?\bcomm(?:ission)?',
    re.IGNORECASE,
)


def _extract_commission_from_context(text: str) -> float:
    """Pull a commission percentage out of free-text context.

    Matches 'commission 15%', '15% commission', 'commission: 12',
    '12% comm...'. Returns the number NEAREST the word 'commission'
    so a stray number elsewhere in the text is never mistaken for commission.
    Returns 0.0 when nothing matches.
    """
    if not text:
        return 0.0

    candidates: list[tuple[int, float]] = []  # (separator_length, value)

    # keyword-first: "commission 15%" — separator = chars between kw end and number start
    for m in _COMMISSION_KW_FIRST_RE.finditer(text):
        val_str = m.group(1)
        if not val_str:
            continue
        try:
            f_val = float(val_str)
        except (TypeError, ValueError):
            continue
        full = m.group(0)
        kw_m = re.search(r'\bcomm(?:ission)?', full, re.IGNORECASE)
        kw_end = kw_m.end() if kw_m else 0
        num_start = full.rfind(val_str)
        candidates.append((num_start - kw_end, f_val))

    # number-first: "15% commission" — separator = chars between number end and kw start
    for m in _COMMISSION_NUM_FIRST_RE.finditer(text):
        val_str = m.group(1)
        if not val_str:
            continue
        try:
            f_val = float(val_str)
        except (TypeError, ValueError):
            continue
        full = m.group(0)
        num_end = full.find(val_str) + len(val_str)
        kw_m = re.search(r'\bcomm(?:ission)?', full, re.IGNORECASE)
        kw_start = kw_m.start() if kw_m else len(full)
        candidates.append((kw_start - num_end, f_val))

    if not candidates:
        return 0.0
    # Return value with the smallest separator; on tie, first wins (stable min)
    return min(candidates, key=lambda t: t[0])[1]


def validate_promo_request(req: "PromoRequest") -> "PromoRequest":
    """Clamp or nullify out-of-range numeric fields. Logs a warning for each rejected value.

    Rules:
      discount_pct      — must be 0 < x <= 100 (percentage)
      discount_amount   — must be >= 0 (no negative spend)
      brand_funding_pct    — must be 0 < x <= 100
      brand_bears_per_unit — must be >= 0
      soa_per_unit      — must be >= 0
      spiv_per_unit     — must be >= 0
      rebate_pct        — must be 0 <= x <= 100
      addon_cogs_per_unit — must be >= 0
    """
    _log = logging.getLogger(__name__)

    def _check_pct(value, name, allow_zero=False):
        if value is None:
            return None
        lo = 0.0 if allow_zero else 0.0
        exclusive_lo = not allow_zero
        if exclusive_lo and value <= lo:
            _log.warning("Validation: %s=%.4g rejected (must be > 0)", name, value)
            return None
        if value > 100:
            _log.warning("Validation: %s=%.4g rejected (must be <= 100)", name, value)
            return None
        return value

    def _check_non_negative(value, name):
        if value is None:
            return None
        if value < 0:
            _log.warning("Validation: %s=%.4g rejected (must be >= 0)", name, value)
            return None
        return value

    return PromoRequest(
        retailer=req.retailer,
        region=req.region,
        promo_type=req.promo_type,
        promo_scope=req.promo_scope,
        discount_pct=_check_pct(req.discount_pct, "discount_pct"),
        discount_amount=_check_non_negative(req.discount_amount, "discount_amount"),
        discount_currency=req.discount_currency,
        brand_funding_pct=_check_pct(req.brand_funding_pct, "brand_funding_pct"),
        brand_bears_per_unit=_check_non_negative(req.brand_bears_per_unit, "brand_bears_per_unit"),
        soa_per_unit=_check_non_negative(req.soa_per_unit, "soa_per_unit"),
        rebate_pct=_check_pct(req.rebate_pct, "rebate_pct", allow_zero=True),
        spiv_per_unit=_check_non_negative(req.spiv_per_unit, "spiv_per_unit"),
        addon_cogs_per_unit=_check_non_negative(req.addon_cogs_per_unit, "addon_cogs_per_unit"),
        addon_cogs_label=req.addon_cogs_label,
        ad_channels=req.ad_channels,
        dates=req.dates,
        duration=req.duration,
        distributor=req.distributor,
        store_name=req.store_name,
        raw_text=req.raw_text,
        parse_confidence=req.parse_confidence,
    )


_MAX_INPUT_BYTES = 10_240  # 10 KB hard cap — prevents runaway LLM calls and memory pressure


def parse_promo_message(text: str) -> PromoRequest:
    """Parse a freeform Slack promo message using Sonnet API.

    Replaces the old regex pipeline entirely. Sonnet extracts all structured
    fields from natural language. Falls back to a minimal PromoRequest (low
    confidence) only if the API call fails.
    """
    import logging as _log
    from app.llm import parse_with_llm
    cleaned = _clean_slack_formatting(text)

    if len(cleaned.encode("utf-8")) > _MAX_INPUT_BYTES:
        _log.getLogger(__name__).warning(
            "parse_promo_message: input truncated from %d bytes to %d bytes",
            len(cleaned.encode("utf-8")), _MAX_INPUT_BYTES,
        )
        cleaned = cleaned.encode("utf-8")[:_MAX_INPUT_BYTES].decode("utf-8", errors="ignore")

    fields = parse_with_llm(cleaned)

    if not fields:
        # API unavailable or failed — return minimal request so the bot can
        # at least post an error rather than crashing silently
        import logging
        logging.getLogger(__name__).warning("parse_with_llm returned None — API may be unavailable")
        return PromoRequest(raw_text=cleaned, parse_confidence="low")

    def _f(key, default=None):
        v = fields.get(key)
        return v if v is not None else default

    def _float(key):
        v = fields.get(key)
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    # Duration from dates if not explicitly returned
    start_date = _f("start_date")
    end_date = _f("end_date")
    duration = _f("duration")
    dates = [d for d in [start_date, end_date] if d]
    if not duration and start_date and end_date:
        from datetime import datetime
        try:
            days = (datetime.strptime(end_date, "%Y-%m-%d") -
                    datetime.strptime(start_date, "%Y-%m-%d")).days
            if days > 0:
                duration = f"{days} days"
        except ValueError:
            pass

    req = PromoRequest(
        retailer=_f("retailer"),
        region=_f("region"),
        promo_type=_f("promo_type", "promo"),
        discount_pct=_float("discount_pct"),
        discount_amount=_float("discount_amount"),
        discount_currency=_f("discount_currency", "USD"),
        brand_funding_pct=_float("brand_funding_pct"),
        brand_bears_per_unit=_float("brand_bears_per_unit"),
        soa_per_unit=_float("soa_per_unit"),
        rebate_pct=_float("rebate_pct"),
        spiv_per_unit=_float("spiv_per_unit"),
        addon_cogs_per_unit=_float("addon_cogs_per_unit"),
        addon_cogs_label=_f("addon_cogs_label", ""),
        ad_channels=_f("ad_channels", []),
        dates=dates,
        duration=duration,
        raw_text=cleaned,
        parse_confidence="high",
    )
    return validate_promo_request(req)
