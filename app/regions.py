"""Canonical region definitions for promo-bot.

Single source of truth for:
  - Calendar column header(s) per region
  - POC Slack user IDs per region
  - Alias normalization (any free-form region value -> canonical)

Tracker submitters use inconsistent region values (UAE / GCC, Germany / DE,
Austria / AT, Japan / JP, etc.). normalize_region() collapses all variants
to a canonical key. Both calendar verifier and POC tagging key off canonical.
"""
from __future__ import annotations

from app.config import (
    PERSONA_USER_ID, PERSONB_USER_ID, PERSONC_USER_ID,
    PERSOND_USER_ID, PERSONE_USER_ID,
    PERSONF_USER_ID, PERSONG_USER_ID, PERSONH_USER_ID,
    PERSONI_USER_ID,
)

# APAC = PersonF + PersonG + PersonH (excluding India which is PersonI)
_APAC_POCS = [PERSONF_USER_ID, PERSONG_USER_ID, PERSONH_USER_ID]
# EU (excluding Poland) = PersonB + PersonC
_EU_POCS = [PERSONB_USER_ID, PERSONC_USER_ID]
# NA = PersonD + PersonE
_NA_POCS = [PERSOND_USER_ID, PERSONE_USER_ID]


# Canonical region -> metadata. The KEY is the canonical name we use
# internally; aliases below map free-form input to these keys.
#
# `calendar_columns`: which columns in Promotions Calendar 2026 Summary
#                     row 1 to scan. Empty list = not on calendar (auto-exempt).
# `poc_ids`:          Slack user IDs to tag for this region.
# `aliases`:          alternate spellings / abbreviations (case-insensitive).
REGIONS: dict[str, dict] = {
    # --- GCC: individual countries + aggregate ---
    "UAE": {
        "calendar_columns": ["UAE"],
        "poc_ids": [PERSONA_USER_ID],
        "aliases": ["uae", "u.a.e.", "united arab emirates", "dubai"],
    },
    "KSA": {
        "calendar_columns": ["KSA"],
        "poc_ids": [PERSONA_USER_ID],
        "aliases": ["ksa", "saudi", "saudi arabia", "sa"],
    },
    "Kuwait": {
        "calendar_columns": ["Kuwait"],
        "poc_ids": [PERSONA_USER_ID],
        "aliases": ["kuwait", "kw"],
    },
    "Qatar": {
        "calendar_columns": ["Qatar"],
        "poc_ids": [PERSONA_USER_ID],
        "aliases": ["qatar", "qa"],
    },
    "Bahrain": {
        "calendar_columns": ["Bahrain"],
        "poc_ids": [PERSONA_USER_ID],
        "aliases": ["bahrain", "bh"],
    },
    "GCC": {
        "calendar_columns": ["UAE", "KSA", "Kuwait", "Qatar", "Bahrain"],
        "poc_ids": [PERSONA_USER_ID],
        "aliases": ["gcc", "gulf", "middle east", "me", "mena", "oman"],
    },

    # --- North America ---
    "US": {
        "calendar_columns": ["USA"],
        "poc_ids": _NA_POCS,
        "aliases": ["us", "usa", "america", "united states", "u.s.", "u.s.a."],
    },
    "CA": {
        "calendar_columns": ["Canada"],
        "poc_ids": _NA_POCS,
        "aliases": ["ca", "canada"],
    },
    "Mexico": {
        "calendar_columns": [],   # not in calendar
        "poc_ids": _NA_POCS,
        "aliases": ["mexico", "mx"],
    },

    # --- UK ---
    "UK": {
        "calendar_columns": ["UK"],
        "poc_ids": _EU_POCS,
        "aliases": ["uk", "united kingdom", "u.k.", "england", "britain", "great britain", "gb"],
    },

    # --- EU individual countries ---
    "Germany": {
        "calendar_columns": ["Germany"],
        "poc_ids": _EU_POCS,
        "aliases": ["germany", "de", "deutschland", "ger"],
    },
    "Austria": {
        "calendar_columns": ["Austria"],
        "poc_ids": _EU_POCS,
        "aliases": ["austria", "at"],
    },
    "Switzerland": {
        "calendar_columns": ["Switzerland"],
        "poc_ids": _EU_POCS,
        "aliases": ["switzerland", "ch", "suisse", "europe (switzerland)"],
    },
    "France": {
        "calendar_columns": ["France"],
        "poc_ids": _EU_POCS,
        "aliases": ["france", "fr"],
    },
    "Italy": {
        "calendar_columns": ["Italy"],
        "poc_ids": _EU_POCS,
        "aliases": ["italy", "it", "italia"],
    },
    "Spain": {
        "calendar_columns": ["Spain", "Spain (Bulkclub ES/FR)"],
        "poc_ids": _EU_POCS,
        "aliases": ["spain", "es", "españa"],
    },
    "BeNeLux": {
        "calendar_columns": ["BeNeLux"],
        "poc_ids": _EU_POCS,
        "aliases": ["benelux", "nl", "netherlands", "holland", "be", "belgium", "lu", "luxembourg"],
    },
    "Poland": {
        "calendar_columns": ["Poland"],
        "poc_ids": [PERSONA_USER_ID],
        "aliases": ["poland", "pl", "polska"],
    },

    # --- Nordics: individual + aggregate ---
    "Sweden": {
        "calendar_columns": ["Sweden"],
        "poc_ids": _EU_POCS,
        "aliases": ["sweden", "se", "sverige"],
    },
    "Norway": {
        "calendar_columns": ["Norway"],
        "poc_ids": _EU_POCS,
        "aliases": ["norway", "no", "norge"],
    },
    "Denmark": {
        "calendar_columns": ["Denmark"],
        "poc_ids": _EU_POCS,
        "aliases": ["denmark", "dk", "danmark"],
    },
    "Finland": {
        "calendar_columns": ["Finland"],
        "poc_ids": _EU_POCS,
        "aliases": ["finland", "fi", "suomi"],
    },
    "Iceland": {
        "calendar_columns": ["Iceland"],
        "poc_ids": _EU_POCS,
        "aliases": ["iceland", "is"],
    },
    "Nordics": {
        "calendar_columns": ["Sweden", "Norway", "Denmark", "Finland", "Iceland"],
        "poc_ids": _EU_POCS,
        "aliases": ["nordics", "scandinavia", "scandinavian"],
    },

    # --- Europe aggregate (PersonB+PersonC region for EU promos that span) ---
    "Europe": {
        "calendar_columns": ["Germany", "Italy", "Austria", "Spain", "France",
                             "UK", "BeNeLux", "Switzerland"],
        "poc_ids": _EU_POCS,
        "aliases": ["europe", "eu", "eea", "european union", "western europe", "we"],
    },

    # --- APCOM (Central + Eastern Europe) ---
    "Czechia": {
        "calendar_columns": ["Czechia"],
        "poc_ids": _EU_POCS,
        "aliases": ["czechia", "cz", "czech republic", "czech"],
    },
    "Romania": {
        "calendar_columns": ["Romania"],
        "poc_ids": _EU_POCS,
        "aliases": ["romania", "ro"],
    },
    "Slovakia": {
        "calendar_columns": ["Slovakia"],
        "poc_ids": _EU_POCS,
        "aliases": ["slovakia", "sk"],
    },
    "Hungary": {
        "calendar_columns": ["Hungary"],
        "poc_ids": _EU_POCS,
        "aliases": ["hungary", "hu"],
    },
    "APCOM": {
        "calendar_columns": ["Czechia", "Romania", "Slovakia", "Hungary"],
        "poc_ids": _EU_POCS,
        "aliases": ["apcom", "cee", "central eastern europe"],
    },

    # --- South Africa ---
    "ZA": {
        "calendar_columns": ["South Africa"],
        "poc_ids": [PERSONB_USER_ID],
        "aliases": ["za", "south africa", "rsa"],
    },

    # --- India ---
    "India": {
        "calendar_columns": ["India"],
        "poc_ids": [PERSONI_USER_ID],
        "aliases": ["india", "in", "ind", "bharat"],
    },

    # --- Israel ---
    "Israel": {
        "calendar_columns": [],   # not in calendar
        "poc_ids": [PERSONA_USER_ID],
        "aliases": ["israel", "il"],
    },

    # --- Australia / New Zealand ---
    "AU": {
        "calendar_columns": ["Australia"],
        "poc_ids": _APAC_POCS,
        "aliases": ["au", "aus", "australia", "auz"],
    },
    "NZ": {
        "calendar_columns": ["New Zealand"],
        "poc_ids": _APAC_POCS,
        "aliases": ["nz", "new zealand", "newzealand"],
    },

    # --- APAC individual countries ---
    "JP": {
        "calendar_columns": ["Japan"],
        "poc_ids": _APAC_POCS,
        "aliases": ["jp", "japan", "nihon", "nippon"],
    },
    "Hong Kong": {
        "calendar_columns": ["Hong Kong"],
        "poc_ids": _APAC_POCS,
        "aliases": ["hong kong", "hk", "hongkong"],
    },
    "Taiwan": {
        "calendar_columns": ["Taiwan"],
        "poc_ids": _APAC_POCS,
        "aliases": ["taiwan", "tw"],
    },
    "TH": {
        "calendar_columns": ["Thailand"],
        "poc_ids": _APAC_POCS,
        "aliases": ["th", "thailand"],
    },
    "SG": {
        "calendar_columns": ["Singapore"],
        "poc_ids": _APAC_POCS,
        "aliases": ["sg", "singapore"],
    },
    "MY": {
        "calendar_columns": ["Malaysia"],
        "poc_ids": _APAC_POCS,
        "aliases": ["my", "malaysia"],
    },
    "PH": {
        "calendar_columns": ["Philippines"],
        "poc_ids": _APAC_POCS,
        "aliases": ["ph", "philippines", "pi"],
    },

    # --- Global aggregates (used by influencer / D2C channel-level promos) ---
    "Global": {
        "calendar_columns": [],
        "poc_ids": [],
        "aliases": ["global", "worldwide", "ww", "all regions"],
    },
    "Global (Excl USA)": {
        "calendar_columns": [],
        "poc_ids": [],
        "aliases": ["global (excl usa)", "global excl usa", "global ex usa",
                    "global ex-usa", "global excluding usa",
                    "global (ex usa)", "global excl us"],
    },
}


# Build reverse alias index (lowercase -> canonical) at module load.
_ALIAS_INDEX: dict[str, str] = {}
for canonical, info in REGIONS.items():
    _ALIAS_INDEX[canonical.lower()] = canonical
    for alias in info.get("aliases", []):
        _ALIAS_INDEX[alias.lower()] = canonical


def register_aliases(mapping: dict[str, str]) -> None:
    """Merge runtime (sheet-supplied) aliases into the resolver. Canonical
    code aliases are authoritative and never overwritten. Entries whose
    canonical value does not resolve to a known region are silently ignored."""
    # Not thread-safe; call once at startup.
    for alias, canonical in (mapping or {}).items():
        if not alias or not canonical:
            continue
        alias_key = str(alias).strip().lower()
        if not alias_key:
            continue
        canon = _ALIAS_INDEX.get(str(canonical).strip().lower())
        if not canon:
            continue
        _ALIAS_INDEX.setdefault(alias_key, canon)


def normalize_region(s: str | None) -> str:
    """Map any region variant (case-insensitive) to its canonical key.
    Returns "" if the input is empty or unrecognized.
    """
    if not s:
        return ""
    return _ALIAS_INDEX.get(s.strip().lower(), "")


def calendar_columns_for(region: str | None) -> list[str]:
    """Return the calendar column header(s) for this region. Empty list
    means either unmapped (caller should treat as 'no calendar entry
    expected', i.e., skip calendar reminder)."""
    canonical = normalize_region(region)
    if not canonical:
        return []
    return REGIONS.get(canonical, {}).get("calendar_columns", [])


def poc_ids_for(region: str | None) -> list[str]:
    """Return the regional POC Slack user IDs for this region. Empty list
    means caller should fall back to keepers + submitter only."""
    canonical = normalize_region(region)
    if not canonical:
        return []
    return REGIONS.get(canonical, {}).get("poc_ids", [])


# ---------------------------------------------------------------------------
# Aggregate-region membership — used to filter shared multi-country sheet tabs
# (e.g. "Asia Promo Performance" holds AU/NZ/JP/SG; "Gulfshore Promo
# Performance (GCC)" holds UAE/KSA/...). Keys + values are canonical region
# names. A request for an aggregate accepts any row whose country is a member.
# ---------------------------------------------------------------------------
REGION_MEMBERS: dict[str, set[str]] = {
    "GCC": {"UAE", "KSA", "Kuwait", "Qatar", "Bahrain"},
    "Europe": {"Germany", "Italy", "Austria", "Spain", "France",
               "UK", "BeNeLux", "Switzerland"},
    "APCOM": {"Czechia", "Romania", "Slovakia", "Hungary"},
    "Nordics": {"Sweden", "Norway", "Denmark", "Finland", "Iceland"},
}


# ---------------------------------------------------------------------------
# Tab groups — defines which constituent country tabs to read when the
# requested region is an aggregate. Keys match canonical region names.
# Order is preserved; duplicates are deduped in the caller via seen_tabs.
# ---------------------------------------------------------------------------
REGION_TAB_GROUPS: dict[str, list[str]] = {
    "Europe":  ["Germany", "Austria", "Switzerland", "France", "Italy", "Spain", "BeNeLux", "UK"],
    "GCC":     ["UAE", "KSA", "Kuwait", "Qatar", "Bahrain"],
    "APCOM":   ["Czechia", "Romania", "Slovakia", "Hungary"],
    "Nordics": ["Sweden", "Norway", "Denmark", "Finland", "Iceland"],
}


# ---------------------------------------------------------------------------
# Fallback graph — authoritative, single source of truth (2026-05-19).
# Every canonical region is either a key here (with >=1 ranked fallback) or
# in NEW_MARKETS. Mutual pairs are intentional; runtime resolution is
# single-hop so there is no infinite-loop risk.
# ---------------------------------------------------------------------------
NEW_MARKETS: set[str] = {"Mexico", "Israel", "ZA", "Global", "Global (Excl USA)"}

SIMILAR_MARKETS: dict[str, list[str]] = {
    "CA": ["US"], "US": ["CA"], "Poland": ["Europe"], "Europe": ["Poland"],
    "GCC": ["India"], "India": ["GCC"], "Nordics": ["Europe"],
    "TH": ["PH", "SG"], "PH": ["TH", "SG"], "SG": ["TH", "PH"],
    "AU": ["NZ"], "NZ": ["AU"],
    "UAE": ["GCC"], "KSA": ["GCC"], "Kuwait": ["GCC"],
    "Qatar": ["GCC"], "Bahrain": ["GCC"],
    "UK": ["Europe"], "Germany": ["Europe"], "Austria": ["Europe"],
    "Switzerland": ["Europe"], "France": ["Europe"], "Italy": ["Europe"],
    "Spain": ["Europe"], "BeNeLux": ["Europe"], "Sweden": ["Europe"],
    "Norway": ["Europe"], "Denmark": ["Europe"], "Finland": ["Europe"],
    "Iceland": ["Europe"], "Czechia": ["Europe"], "Romania": ["Europe"],
    "Slovakia": ["Europe"], "Hungary": ["Europe"], "APCOM": ["Europe"],
    "Hong Kong": ["TH", "PH", "SG"], "Taiwan": ["TH", "PH", "SG"],
    "MY": ["TH", "PH", "SG"], "JP": ["SG", "TH", "PH"],
}


def fallbacks_for(region: str | None) -> list[str]:
    """Ranked cross-region fallbacks for a region (canonical). [] if the
    region is a NEW_MARKET or unknown.
    Accepts raw or canonical input (normalizes internally; idempotent)."""
    return SIMILAR_MARKETS.get(normalize_region(region), [])


def region_accepts(requested: str | None, row_country: str | None) -> bool:
    """True if a sheet row tagged `row_country` belongs to the `requested`
    region. Fail-open: a blank or unresolvable country is kept (so tabs
    without a usable Country column, e.g. Canada/Mapleline, are unaffected and
    dirty data is not silently dropped). Returns False only when the row's
    country resolves cleanly to a region that is not the request and not a
    member of the request.
    """
    req = normalize_region(requested)
    if not req:
        return True  # unknown request → don't filter
    if not row_country or not str(row_country).strip():
        return True  # no country tag → keep (fail-open)
    rc = normalize_region(row_country)
    if not rc:
        return True  # unresolvable spelling → keep, don't lose data
    if rc == req:
        return True
    return rc in REGION_MEMBERS.get(req, set())
