import os

# Slack
SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
SLACK_SIGNING_SECRET = os.environ["SLACK_SIGNING_SECRET"]
PROMO_CHANNEL_ID = os.environ.get("PROMO_CHANNEL_ID", "")

# Google Sheets
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
PROMO_SHEET_ID = os.environ["PROMO_SHEET_ID"]
PL_SHEET_ID = os.environ["PL_SHEET_ID"]
SELLOUT_SHEET_ID = os.environ["SELLOUT_SHEET_ID"]

# Metabase
METABASE_URL = os.environ.get("METABASE_URL", "https://metabase.example.com")
METABASE_API_KEY = os.environ.get("METABASE_API_KEY", "")

# Anthropic (optional LLM fallback)
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# HTTPS enforcement
# HTTPSRedirectMiddleware is intentionally NOT added here — the app runs behind
# Render's TLS-terminating reverse proxy, which handles HTTPS at the edge.
# REQUIREMENT: this app MUST NOT be exposed directly on HTTP without a TLS proxy
# in front of it. Verify with: curl -v http://<host>/health — must NOT be reachable.
ENFORCE_HTTPS_PROXY = True  # documents the deployment requirement; checked at startup

# Debug endpoint token — must be set to enable /debug/* and other internal endpoints.
# If unset, all debug routes return 403. Generate with: python3 -c "import secrets; print(secrets.token_hex(32))"
DEBUG_TOKEN = os.environ.get("DEBUG_TOKEN", "")

# Primary notification user — used for @mentions in Slack messages
APPROVER_USER_ID = os.environ.get("APPROVER_USER_ID", "")

# Regional retail POCs — used by reminder fan-out per app/regional_pocs.py.
# These are the people who own retailer relationships in their geos.
PERSONA_USER_ID = "U0REDACT001"        # PersonA
PERSONB_USER_ID = "U0REDACT002"       # PersonB
PERSONC_USER_ID = "U0REDACT003"          # PersonC
PERSOND_USER_ID = "U0REDACT004"       # PersonD
PERSONE_USER_ID = "U0REDACT005"       # PersonE
PERSONF_USER_ID = "U0REDACT006"           # PersonF
PERSONG_USER_ID = "U0REDACT007"       # PersonG
PERSONH_USER_ID = "U0REDACT008"       # PersonH
PERSONI_USER_ID = "U0REDACT009"        # PersonI

# Approved Slack user IDs — comma-separated, stored in env (not hardcoded)
# e.g. APPROVER_USER_IDS="U0REDACT010,U123456789"
_approver_ids_raw = os.environ.get("APPROVER_USER_IDS", "")
APPROVER_USER_IDS: set[str] = {
    uid.strip() for uid in _approver_ids_raw.split(",") if uid.strip()
}

# Approver approvals channel — bot monitors threads here for approval signals
APPROVER_APPROVALS_CHANNEL_ID = os.environ.get("APPROVER_APPROVALS_CHANNEL_ID", "")

# Slack workspace team ID — used to build app_redirect URLs that open the
# desktop app instead of the browser. Find via: WebClient.auth_test()['team_id']
SLACK_TEAM_ID = os.environ.get("SLACK_TEAM_ID", "T00000000")

# Days before a Pending promo is auto-marked "No Response"
NO_RESPONSE_DAYS_THRESHOLD = 7

# ---------------------------------------------------------------------------
# Universal constants
# ---------------------------------------------------------------------------
RETAIL_PRICE_USD = 1            # consumer-facing price (all markets, converted to USD)  # REDACTED
COGS_PER_RING_USD = 0            # Base Ring / regular Ring COGS  # REDACTED
COGS_RING_PRO_USD = 0             # Pro Ring COGS (use when message references "Pro Ring" / "RP")  # REDACTED


def cogs_for_request(message_text: str = "") -> int:
    """Return [REDACTED] for Pro-model requests, [REDACTED] otherwise.

    Detection: any of "pro ring", "proring", "pro-ring" SKU prefix, "pro launch".
    Conservative: only flips on explicit Pro signal; default stays at the
    legacy base-ring economics value.
    """
    if not message_text:
        return COGS_PER_RING_USD
    t = message_text.lower()
    if "pro ring" in t or "proring" in t or "pro-ring" in t or "pro launch" in t:
        return COGS_RING_PRO_USD
    # SKU pattern: SKU-PRO-* indicates the Pro model
    import re as _re
    if _re.search(r"\bsku[-_]?pro\b", t):
        return COGS_RING_PRO_USD
    return COGS_PER_RING_USD
TRADE_DISCOUNT_BRAND_DEFAULT = 0.0  # default brand share of discount  # REDACTED
DEFAULT_RETURN_RATE = 0.0       # blended default  # REDACTED
DEFAULT_BUY_PRICE_USD = 0.0     # default buy price when sheet unavailable  # REDACTED

# ---------------------------------------------------------------------------
# Grading thresholds: CM3-Cash % of Net Revenue (SOP v0.4 aligned)
# ---------------------------------------------------------------------------
GRADE_A_FLOOR = 0.0        # >= [REDACTED]% = A
GRADE_B_FLOOR = 0.0        # [REDACTED]-[REDACTED]% = B  # REDACTED
GRADE_C_FLOOR = 0.0        # [REDACTED]-[REDACTED]% = C; below = REJECT. PROVISIONAL — pending Approver/Finance sign-off (SOP v0.4)  # REDACTED

# ---------------------------------------------------------------------------
# Retailer → region mapping (FALLBACK — prefer app.lookups.get_retailer_region_map())
# ---------------------------------------------------------------------------
_FALLBACK_RETAILER_REGION_MAP = {
    # Placeholder examples only. The real mapping lives in the lookups sheet,
    # loaded via app.lookups.get_retailer_region_map().
    "example retailer us": "US",
    "example retailer europe": "Europe",
    "example retailer india": "India",
    "example retailer gcc": "GCC",
}

# Direct region keywords (FALLBACK — prefer app.lookups.get_region_keywords())
_FALLBACK_REGION_KEYWORDS = {
    "gcc": "GCC",
    "uae": "GCC",
    "dubai": "GCC",
    "saudi": "GCC",
    "kuwait": "GCC",
    "qatar": "GCC",
    "bahrain": "GCC",
    "oman": "GCC",
    "poland": "Poland",
    "canada": "CA",
    "india": "India",
    "us": "US",
    "usa": "US",
    "united states": "US",
    "europe": "Europe",
    "eu": "Europe",
    "dach": "Europe",
    "germany": "Europe",
    "netherlands": "Europe",
    "austria": "Europe",
    "italy": "Europe",
    "switzerland": "Europe",
    "uk": "UK",
    "united kingdom": "UK",
    "england": "UK",
    "britain": "UK",
    "apcom": "APCOM",
    "thailand": "TH",
    "philippines": "PH",
    "australia": "AU",
    "new zealand": "NZ",
    "japan": "JP",
    "singapore": "SG",
    "south africa": "ZA",
    "za": "ZA",
    "malaysia": "MY",
    "my": "MY",
}

# ---------------------------------------------------------------------------
# Dynamic tab discovery: keywords to match sheet tab names to regions
# Each region has a list of keywords (checked against tab name, case-insensitive)
# First match wins; order matters for shared tabs (Asia)
# ---------------------------------------------------------------------------
REGION_TAB_KEYWORDS = {
    # Placeholder patterns - match these to your own sheet tab names.
    "GCC": ["gcc.*promo"],
    "Poland": ["poland.*promo"],
    "CA": ["canada.*promo", "ca.*promo"],
    "US": ["us.*perf", "us.*promo"],
    "India": ["india.*promo", "india.*perf"],
    "Europe": ["europe.*promo"],
    "UK": ["uk.*promo"],
    "APCOM": ["apcom.*promo"],
    "TH": ["th.*promo", "thailand.*promo"],
    "PH": ["ph.*promo", "philippines.*promo"],
    "SG": ["asia.*promo", "sg.*promo", "singapore.*promo"],
    "AU": ["anz.*promo", "au.*promo", "australia.*promo"],
    "NZ": ["anz.*promo", "nz.*promo", "new zealand.*promo"],
    "JP": ["asia.*promo", "jp.*promo", "japan.*promo"],
    "ZA": ["south.africa.*promo"],
    "MY": ["malaysia.*promo", "malaysia.*perf"],
    "Nordics": ["nordics.*promo"],
    # Individual-country tabs (Phase A fallback graph)
    # Austria/Italy intentionally omitted: no dedicated tab; served by Europe aggregate fallback.
    "France":      ["france.*galerie.*promo", "france.*flashvente.*promo", "france.*promo"],
    "Spain":       ["bulkclub spain"],                        # NOTE: title has no "promo" word
    "Germany":     ["germany.*alderon.*promo", "germany.*promo"],
    "Switzerland": ["alpencomp.*promo", "alpenhaus.*flyer.*perf", "digitex ag.*promo"],
    "BeNeLux":     ["benelux.*lowlands nl.*promo", "benelux.*promo"],
}

SALES_TAB_KEYWORDS = {
    # Placeholder patterns - match these to your own sheet tab names.
    "GCC": ["gcc.*sale"],
    "Poland": ["poland.*sale"],
    "Europe": ["europe.*sale"],
    "CA": ["canada.*sale"],
    "US": ["us.*sale"],
    "AU": ["au.*sale", "australia.*sale"],
    "NZ": ["nz.*sale", "new zealand.*sale"],
    "UK": ["uk.*sale"],
    "India": ["india.*sale"],
    "TH": ["th.*sale", "thailand.*sale"],
    "SG": ["sg.*sale", "singapore.*sale"],
    "JP": ["jp.*sale", "japan.*sale"],
    # ZA: "^savannamart$" is anchored so it matches the sales tab exactly
    # but NOT "Savannamart Promo Performance".
    "ZA": ["^savannamart$", "savannamart.*sell.*out", "south.africa.*sale"],
    "MY": ["malaysia.*sale"],
    "APCOM": ["apcom.*sale"],
}

# Fallback: hardcoded map used ONLY if dynamic discovery fails
REGION_TAB_MAP_FALLBACK = {}
REGION_SALES_TAB_MAP_FALLBACK = {}

# Region → P&L tab name (for CM3-Cash benchmark)
REGION_PL_TAB_MAP = {
    "CA": "DIST CA",
}

# Regions where promos are at distributor level (all retailers in region)
# For these regions, always aggregate across all retailers, even if a
# specific retailer is mentioned in the request.
# GCC is distributor-level (Gulfshore); its member countries collapse there too.
DISTRIBUTOR_LEVEL_REGIONS = {"GCC", "UAE", "KSA", "Kuwait", "Qatar", "Bahrain", "Poland"}

# Region → distributor (FALLBACK — prefer app.lookups.get_region_distributor_map())
# Placeholder values - replace with your own distributor mapping.
_FALLBACK_REGION_DISTRIBUTOR_MAP = {
    "CA": "Mapleline",
    "US": "Mapleline",
    "Poland": "Vistula",
    "GCC": "Gulfshore",
    "India": "Voltmart",
    "Europe": "Alderon",
    "APCOM": "APCOM",
    "TH": "Siamline",
    "PH": "Isleco",
    "MY": "Meridian",
    "AU": "Ozlink",
    "NZ": "Kiwilink",
    "JP": "Sakura",
    "SG": "Straitline",
    "Nordics": "Nordica AB",
    "UK": "Albion UK",
    "NL": "Lowlands NL",
    "BeNeLux": "Lowlands NL",
    "CH": "Alpencomp",
    "AT": "Alpencomp",
    "Mexico": "Novarep",
    "ZA": "Veldcorp",
    "Israel": "Carmel",
    "France": "Galerie",
    "Germany": "Alderon",
}

# ---------------------------------------------------------------------------
# Market economics defaults (used if Market Economics tab not yet populated)
# Override per-market as data becomes available
# ---------------------------------------------------------------------------
# REDACTED: commercial values removed (buy_price_local, retail_price_usd, trade_discount_brand_pct)
MARKET_DEFAULTS = {
    "CA": {
        "currency": "CAD",
        "fx_rate_to_usd": 0.72,
        "buy_price_local": 0.0,
        "retail_price_usd": 1.0,
        "trade_discount_brand_pct": 0.0,
        "distributor": "Mapleline",
    },
    "Poland": {
        "currency": "EUR",
        "fx_rate_to_usd": 1.08,
        "buy_price_local": 0.0,  # placeholder — needs real data
        "retail_price_usd": 1.0,
        "trade_discount_brand_pct": 0.0,
        "distributor": "Vistula",
    },
    "GCC": {
        "currency": "AED",
        "fx_rate_to_usd": 0.27,
        "buy_price_local": 0.0,  # placeholder
        "retail_price_usd": 1.0,
        "trade_discount_brand_pct": 0.0,
        "distributor": "Gulfshore",
    },
    "US": {
        "currency": "USD",
        "fx_rate_to_usd": 1.0,
        "buy_price_local": 0.0,
        "retail_price_usd": 1.0,
        "trade_discount_brand_pct": 0.0,
        "distributor": "Mapleline",
    },
    "India": {
        "currency": "INR",
        "fx_rate_to_usd": 0.012,
        "buy_price_local": 0.0,
        "retail_price_usd": 1.0,
        "trade_discount_brand_pct": 0.0,
        "distributor": "Voltmart",
    },
    "Europe": {
        "currency": "EUR",
        "fx_rate_to_usd": 1.08,
        "buy_price_local": 0.0,
        "retail_price_usd": 1.0,
        "trade_discount_brand_pct": 0.0,
        "distributor": "Alderon",
    },
    "APCOM": {
        "currency": "USD",
        "fx_rate_to_usd": 1.0,
        "buy_price_local": 0.0,
        "retail_price_usd": 1.0,
        "trade_discount_brand_pct": 0.0,
        "distributor": "APCOM",
    },
    "TH": {
        "currency": "USD",
        "fx_rate_to_usd": 1.0,
        "buy_price_local": 0.0,
        "retail_price_usd": 1.0,
        "trade_discount_brand_pct": 0.0,
        "distributor": "Alderon",
    },
    "PH": {
        "currency": "USD",
        "fx_rate_to_usd": 1.0,
        "buy_price_local": 0.0,
        "retail_price_usd": 1.0,
        "trade_discount_brand_pct": 0.0,
        "distributor": "Alderon",
    },
    "SG": {
        "currency": "USD",
        "fx_rate_to_usd": 1.0,
        "buy_price_local": 0.0,
        "retail_price_usd": 1.0,
        "trade_discount_brand_pct": 0.0,
        "distributor": "Straitline",
    },
    "AU": {
        "currency": "USD",
        "fx_rate_to_usd": 1.0,
        "buy_price_local": 0.0,
        "retail_price_usd": 1.0,
        "trade_discount_brand_pct": 0.0,
        "distributor": "Ozlink",
    },
    "NZ": {
        "currency": "USD",
        "fx_rate_to_usd": 1.0,
        "buy_price_local": 0.0,
        "retail_price_usd": 1.0,
        "trade_discount_brand_pct": 0.0,
        "distributor": "Kiwilink",
    },
    "JP": {
        "currency": "USD",
        "fx_rate_to_usd": 1.0,
        "buy_price_local": 0.0,
        "retail_price_usd": 1.0,
        "trade_discount_brand_pct": 0.0,
        "distributor": "Sakura",
    },
}

# ---------------------------------------------------------------------------
# Metabase IDs
# ---------------------------------------------------------------------------
SELL_OUT_TABLE_ID = 0            # placeholder - set your own Metabase table ID
RETURN_DATA_TABLE_ID = 0         # placeholder - set your own Metabase table ID
RINGS_SHIPPED_CARD_ID = 0        # placeholder - set your own Metabase card ID
REPLACEMENT_RINGS_CARD_ID = 0    # placeholder - set your own Metabase card ID

# ---------------------------------------------------------------------------
# Live sales and advertising data
# ---------------------------------------------------------------------------
# Internal data API (SQL execution over the company data warehouse)
DATA_API_URL = os.environ.get("DATA_API_URL", "https://datacli.internal.example.com")
DATA_API_TOKEN = os.environ.get("DATA_API_TOKEN", "")

# Map region keys to Amazon MARKETPLACE_KEY values in the data warehouse
# Single string = one marketplace. List = aggregate across multiple marketplaces.
REGION_TO_MARKETPLACE_KEY = {
    "India": "Amazon-IN",
    "GCC": ["Amazon-AE", "Amazon-SA"],
    "US": "Amazon-US",
    "Europe": ["Amazon-DE", "Amazon-FR", "Amazon-IT", "Amazon-ES", "Amazon-NL", "Amazon-SE", "Amazon-BE", "Amazon-GB"],
    "AU": "Amazon-AU",
    "CA": "Amazon-CA",
    "JP": "Amazon-JP",
    "SG": "Amazon-SG",
    "Poland": "Amazon-PL",
}

# Ad spend threshold for "heavy ad period" cannibalization flag (local currency)
AD_HEAVY_SPEND_THRESHOLD = 0  # REDACTED
