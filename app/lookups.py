"""Sheet-driven retailer/region lookups with cached reads.

Reads the 'Bot Input Variables' tab once, builds all derived mappings,
and exposes them via function calls. 10-minute TTL cache with
stale-while-revalidate on failure.
"""
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

import gspread
from google.oauth2.service_account import Credentials

from app.config import (
    GOOGLE_SERVICE_ACCOUNT_JSON,
    PROMO_SHEET_ID,
    # Fallbacks — only used if sheet read fails AND no stale cache
    _FALLBACK_RETAILER_REGION_MAP,
    _FALLBACK_REGION_KEYWORDS,
    _FALLBACK_REGION_DISTRIBUTOR_MAP,
)

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
_CACHE_TTL_SECONDS = 600  # 10 minutes

# ---------------------------------------------------------------------------
# Country/keyword -> region code (the only hardcoded piece, ~15 entries)
# Changes maybe 2x/year when a new market launches.
# ---------------------------------------------------------------------------
_COUNTRY_TO_CODE = {
    "germany": "Europe",
    "austria": "Europe",
    "italy": "Europe",
    "netherlands": "Europe",
    "switzerland": "Europe",
    "spain": "Europe",
    "france": "Europe",
    "europe": "Europe",
    "eu": "Europe",
    "dach": "Europe",
    "uk": "UK",
    "united kingdom": "UK",
    "england": "UK",
    "britain": "UK",
    "mexico": "Mexico",
    "mx": "Mexico",
    "uae": "GCC",
    "saudi": "GCC",
    "gcc": "GCC",
    "kuwait": "GCC",
    "qatar": "GCC",
    "bahrain": "GCC",
    "oman": "GCC",
    "dubai": "GCC",
    "middle east": "GCC",
    "poland": "Poland",
    "canada": "CA",
    "ca": "CA",
    "us": "US",
    "usa": "US",
    "united states": "US",
    "india": "India",
    "thailand": "TH",
    "th": "TH",
    "philippines": "PH",
    "ph": "PH",
    "australia": "AU",
    "au": "AU",
    "new zealand": "NZ",
    "nz": "NZ",
    "japan": "JP",
    "jp": "JP",
    "singapore": "SG",
    "sg": "SG",
    "apcom": "APCOM",
    "israel": "Israel",
    "south africa": "ZA",
    "za": "ZA",
    "malaysia": "MY",
    "my": "MY",
    "nordics": "Nordics",
    "sweden": "Nordics",
    "norway": "Nordics",
    "denmark": "Nordics",
    "finland": "Nordics",
    "nordica": "Nordics",
}

# Reverse: region_code -> list of aliases (for _find_market_row / _find_partner_row)
_FALLBACK_REGION_ALIASES = {
    "TH": ["thailand", "th"],
    "PH": ["philippines", "ph"],
    "AU": ["australia", "au"],
    "NZ": ["new zealand", "nz"],
    "JP": ["japan", "jp", "sakura"],
    "SG": ["singapore", "sg", "straitline"],
    "Europe": ["europe", "eu", "germany", "austria", "italy", "dach", "alderon"],
    "UK": ["uk", "united kingdom", "england", "britain", "albion uk"],
    "APCOM": ["apcom"],
    "GCC": ["gcc", "uae", "middle east"],
    "CA": ["ca", "canada"],
    "US": ["us", "usa", "united states"],
    "Poland": ["poland"],
    "India": ["india"],
    "Israel": ["israel", "carmel"],
    "Mexico": ["mexico", "mx", "novarep", "novarep"],
    "ZA": ["south africa", "za", "savannamart"],
    "MY": ["malaysia", "my", "meridian"],
    "Nordics": ["nordics", "sweden", "norway", "denmark", "finland", "nordica"],
}


# ---------------------------------------------------------------------------
# Cached mapping dataclass
# ---------------------------------------------------------------------------
@dataclass
class MappingCache:
    retailer_region_map: dict = field(default_factory=dict)
    region_keywords: dict = field(default_factory=dict)
    region_aliases: dict = field(default_factory=dict)
    region_distributor_map: dict = field(default_factory=dict)
    built_at: float = 0.0


_cache: Optional[MappingCache] = None
_cache_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Sheet reading (moved from sheets.py to avoid circular imports)
# ---------------------------------------------------------------------------

def _get_sheets_client() -> gspread.Client:
    raw = GOOGLE_SERVICE_ACCOUNT_JSON
    if not raw or raw == "{}":
        raise ValueError("GOOGLE_SERVICE_ACCOUNT_JSON env var is empty or not set")
    creds_dict = json.loads(raw)
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    return gspread.authorize(creds)


def read_input_variables() -> tuple[list[dict], list[dict]]:
    """Read 'Bot Input Variables' tab from the promo sheet.

    Returns (market_rows, partner_rows).
    Public so sheets.py can also call it without circular imports.
    """
    try:
        client = _get_sheets_client()
        sheet = client.open_by_key(PROMO_SHEET_ID)
        ws = sheet.worksheet("Bot Input Variables")
        all_values = ws.get_all_values()
    except Exception as e:
        logger.error(f"Failed to read Bot Input Variables tab: {e}")
        return [], []

    market_rows = []
    partner_rows = []
    section = None
    market_header = []
    partner_header = []

    for row in all_values:
        first_cell = str(row[0]).strip() if row else ""

        if first_cell.startswith("MARKET-LEVEL"):
            section = "market_header"
            continue
        if first_cell.startswith("PARTNER-LEVEL"):
            section = "partner_header"
            continue

        if section == "market_header":
            market_header = [str(c).strip() for c in row]
            section = "market"
            continue
        if section == "partner_header":
            partner_header = [str(c).strip() for c in row]
            section = "partner"
            continue

        if not first_cell:
            continue

        if section == "market":
            row_dict = {market_header[i]: row[i] for i in range(min(len(market_header), len(row)))}
            market_rows.append(row_dict)
        elif section == "partner":
            row_dict = {partner_header[i]: row[i] for i in range(min(len(partner_header), len(row)))}
            partner_rows.append(row_dict)

    return market_rows, partner_rows


# ---------------------------------------------------------------------------
# Build mappings from sheet data
# ---------------------------------------------------------------------------

def _build_mappings_from_sheet() -> MappingCache:
    """Read Bot Input Variables and derive all lookup maps."""
    market_rows, partner_rows = read_input_variables()
    if not market_rows and not partner_rows:
        raise RuntimeError("No data from Bot Input Variables sheet")

    retailer_region_map: dict[str, str] = {}
    region_keywords: dict[str, str] = {}
    region_aliases: dict[str, list[str]] = {}
    region_distributor_map: dict[str, str] = {}

    all_rows = market_rows + partner_rows

    for row in all_rows:
        market_cell = str(row.get("Market", "")).strip()
        retailer_cell = str(row.get("Retailer", "")).strip()
        distributor_cell = str(row.get("Distributor", "")).strip()

        if not market_cell:
            continue

        # Split comma-separated market names: "Germany, Austria, Italy"
        segments = [s.strip() for s in market_cell.split(",") if s.strip()]

        # Determine region code from segments
        region_code = None
        for seg in segments:
            code = _COUNTRY_TO_CODE.get(seg.lower())
            if code:
                region_code = code
                break

        if not region_code:
            # Try the whole cell as-is (might be "GCC", "Poland", etc.)
            region_code = _COUNTRY_TO_CODE.get(market_cell.lower(), market_cell)

        # Build region_aliases: region_code -> set of lowercase names from the sheet
        if region_code not in region_aliases:
            region_aliases[region_code] = []
        for seg in segments:
            seg_lower = seg.lower()
            if seg_lower not in region_aliases[region_code]:
                region_aliases[region_code].append(seg_lower)
        # Always include the code itself
        code_lower = region_code.lower()
        if code_lower not in region_aliases[region_code]:
            region_aliases[region_code].append(code_lower)

        # Build region_keywords from each segment
        for seg in segments:
            seg_lower = seg.lower()
            if seg_lower not in region_keywords:
                region_keywords[seg_lower] = region_code

        # Build retailer -> region (skip "All" entries)
        if retailer_cell and retailer_cell.lower() not in ("all", "", "-"):
            retailer_region_map[retailer_cell.lower()] = region_code

        # Build region -> distributor
        if distributor_cell and region_code and region_code not in region_distributor_map:
            region_distributor_map[region_code] = distributor_cell

    # Also add _COUNTRY_TO_CODE entries to region_keywords so keyword-based
    # region detection works for countries not explicitly in the sheet
    for keyword, code in _COUNTRY_TO_CODE.items():
        if keyword not in region_keywords:
            region_keywords[keyword] = code

    logger.info(
        f"Built lookups from sheet: {len(retailer_region_map)} retailers, "
        f"{len(region_keywords)} keywords, {len(region_aliases)} regions, "
        f"{len(region_distributor_map)} distributors"
    )

    return MappingCache(
        retailer_region_map=retailer_region_map,
        region_keywords=region_keywords,
        region_aliases=region_aliases,
        region_distributor_map=region_distributor_map,
        built_at=time.time(),
    )


# ---------------------------------------------------------------------------
# Cache management (thread-safe, stale-while-revalidate)
# ---------------------------------------------------------------------------

def _get_cache() -> MappingCache:
    """Return current cache, refreshing if stale. Thread-safe.

    Fast path captures _cache in a local variable (one atomic GIL read) so
    the subsequent .built_at access can't race with a concurrent cache replace.
    """
    global _cache

    # Fast path: snapshot the reference once so built_at access is on same object
    c = _cache
    if c is not None and (time.time() - c.built_at) < _CACHE_TTL_SECONDS:
        return c

    with _cache_lock:
        # Double-check under lock before rebuilding
        c = _cache
        if c is not None and (time.time() - c.built_at) < _CACHE_TTL_SECONDS:
            return c

        try:
            new_cache = _build_mappings_from_sheet()
            _cache = new_cache
            return _cache
        except Exception as e:
            logger.error(f"Failed to refresh lookups cache: {e}")
            # Stale-while-revalidate: serve old cache if available
            if _cache:
                logger.warning("Serving stale lookups cache")
                return _cache
            # No cache at all: build from fallback dicts
            logger.warning("No cache available, using hardcoded fallbacks")
            return MappingCache(
                retailer_region_map=dict(_FALLBACK_RETAILER_REGION_MAP),
                region_keywords=dict(_FALLBACK_REGION_KEYWORDS),
                region_aliases=dict(_FALLBACK_REGION_ALIASES),
                region_distributor_map=dict(_FALLBACK_REGION_DISTRIBUTOR_MAP),
                built_at=time.time(),
            )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_retailer_region_map() -> dict[str, str]:
    """Retailer name (lowercase) -> region code. Replaces config.RETAILER_REGION_MAP."""
    return _get_cache().retailer_region_map


def get_region_keywords() -> dict[str, str]:
    """Keyword (lowercase) -> region code. Replaces config.REGION_KEYWORDS."""
    return _get_cache().region_keywords


def get_region_aliases() -> dict[str, list[str]]:
    """Region code -> list of lowercase aliases. Replaces sheets._REGION_ALIASES."""
    return _get_cache().region_aliases


def get_region_distributor_map() -> dict[str, str]:
    """Region code -> distributor name. Replaces config.REGION_DISTRIBUTOR_MAP."""
    return _get_cache().region_distributor_map


def get_debug_info() -> dict:
    """Return full cache state for the /debug/lookups endpoint."""
    cache = _get_cache()
    return {
        "built_at": cache.built_at,
        "age_seconds": round(time.time() - cache.built_at, 1),
        "retailer_region_map": cache.retailer_region_map,
        "retailer_count": len(cache.retailer_region_map),
        "region_keywords": cache.region_keywords,
        "keyword_count": len(cache.region_keywords),
        "region_aliases": cache.region_aliases,
        "region_distributor_map": cache.region_distributor_map,
    }
