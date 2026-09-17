"""D2C lift table — BAU baseline rates + lift factors per (country, month, discount band).

Data derived from the purchase-history warehouse table, base ring
SKU only (PRODUCT_TYPE='smart_ring'), with PURCHASE_CHANNEL whitelist:
  others / Influencer Marketing / Performance Marketing /
  Product in-app Referrals / Product in-app buy buttons /
  Inside Sales / Blog / Social / Reengagement Communication.

Excludes Retail (=retailer bulk), Marketplace (=Amazon), Partnerships (=B2B),
offline, Replacement (=warranty). No QTY filter — channel filter is the B2B
discriminator. BF/NY seasonal windows excluded for tier rows only.

BAU = orders with $0 discount applied, computed at country×month grain.
Lift = (units/day during a storewide-level discount tier) ÷ (BAU units/day for
the same country, same month if available, else any-month average).

Refresh cadence: rerun `scripts/refresh_d2c_lift_table.py` quarterly. As more
non-seasonal sales accumulate, the n grows and confidence improves.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from app.regions import normalize_region as _canon


@dataclass
class LiftEstimate:
    """Result of a lift table lookup."""
    lift: float                # multiplicative factor over BAU (>= 1.0)
    bau_per_day: float         # baseline u/day for that country×month
    sample_n: int              # weeks of data behind the lift number
    source: str                # "country-month-tier" / "country-tier" / "peer:AU" / "global"
    confidence: str            # "high" / "med-high" / "medium" / "med-low" / "low" / "cannot_evaluate"
    normalized_country: str = ""  # what we resolved the input country to (or "" if unresolved)


# Canonical D2C country codes used by BAU_RATES_D2C / LIFT_TABLE_D2C.
# Aggregate regions (GCC, EU): D2C lookup rolls up across constituent countries
# (the data warehouse pull already sums them at the regional grain).
SUPPORTED_D2C_COUNTRIES = {"IN", "AU", "JP", "MY", "NZ", "SG",
                          "US", "UK", "CA", "EU", "PL", "GCC"}

# Only D2C-graded regions appear here; non-D2C canonical regions (TH, ZA, Israel, Mexico, …) intentionally absent → "".
_CANON_TO_D2C: dict[str, str] = {
    "India": "IN", "AU": "AU", "JP": "JP", "MY": "MY", "NZ": "NZ", "SG": "SG",
    "US": "US", "UK": "UK", "CA": "CA", "Poland": "PL", "GCC": "GCC",
    "Europe": "EU", "Germany": "EU", "Austria": "EU", "Switzerland": "EU",
    "France": "EU", "Italy": "EU", "Spain": "EU", "BeNeLux": "EU",
    "UAE": "GCC", "KSA": "GCC", "Kuwait": "GCC", "Qatar": "GCC", "Bahrain": "GCC",
}


def normalize_country(raw: str) -> str:
    """Resolve a user-supplied region/country string to a canonical D2C code.

    1. Delegates to ``app.regions.normalize_region`` to get the canonical key.
    2. Maps that key to a D2C table code via ``_CANON_TO_D2C``.
    3. Falls back to a bare upper-case match against ``SUPPORTED_D2C_COUNTRIES``
       (handles the case where the input is already a valid D2C code like "IN").

    Returns "" if not recognized — callers must fail loudly (don't silently
    fall back to global default, that's what produces wrong grades).
    """
    if not raw:
        return ""
    canon = _canon(raw)
    if canon in _CANON_TO_D2C:
        return _CANON_TO_D2C[canon]
    up = raw.strip().upper()
    return up if up in SUPPORTED_D2C_COUNTRIES else ""


# ---------------------------------------------------------------------------
# BAU rates: country -> month -> u/day (orders at $0 discount, base ring only)
# Derived from May 2025 - May 2026 historical data.
# ---------------------------------------------------------------------------
# REDACTED: commercial values removed (units/day run-rates)
BAU_RATES_D2C: dict[str, dict[int, float]] = {
    # Refreshed 2026-05-08 — channel whitelist applied (D2C consumer channels
    # only), no QTY filter. Excludes Retail / Marketplace / Partnerships.
    "IN": {1: 0.0, 2: 0.0, 3: 0.0, 4: 0.0, 5: 0.0, 6: 0.0, 7: 0.0,
           8: 0.0, 9: 0.0, 10: 0.0, 11: 0.0, 12: 0.0},
    "AU": {1: 0.0, 2: 0.0, 3: 0.0, 4: 0.0, 5: 0.0, 6: 0.0, 7: 0.0,
           8: 0.0, 9: 0.0, 10: 0.0, 11: 0.0, 12: 0.0},
    "JP": {1: 0.0, 2: 0.0, 3: 0.0, 4: 0.0, 5: 0.0, 6: 0.0, 7: 0.0,
           8: 0.0, 9: 0.0, 10: 0.0, 12: 0.0},
    "MY": {1: 0.0, 2: 0.0, 3: 0.0, 4: 0.0, 5: 0.0, 6: 0.0, 7: 0.0,
           8: 0.0, 10: 0.0, 11: 0.0, 12: 0.0},
    "NZ": {1: 0.0, 2: 0.0, 3: 0.0, 4: 0.0},  # sparse
    "SG": {3: 0.0, 4: 0.0, 6: 0.0, 11: 0.0},  # sparse
    # Below: backfilled 2026-05-12 from Oct 2025 - Jan 2026 the data warehouse data.
    # Strict $0-discount BAU u/day; annual average shown (monthly grain too
    # noisy on small denominators outside US).
    "US":  {1: 0.0, 10: 0.0, 11: 0.0, 12: 0.0},
    "UK":  {1: 0.0, 10: 0.0, 11: 0.0, 12: 0.0},
    "CA":  {1: 0.0, 10: 0.0, 11: 0.0, 12: 0.0},
    "EU":  {1: 0.0, 10: 0.0, 11: 0.0, 12: 0.0},
    "PL":  {1: 0.0, 10: 0.0, 11: 0.0, 12: 0.0},
    "GCC": {1: 0.0, 10: 0.0, 11: 0.0, 12: 0.0},
}

# REDACTED: commercial values removed (units/day run-rates)
# Annual-average BAU when a specific month isn't in the table.
_BAU_ANNUAL_AVG: dict[str, float] = {
    "IN": 0.0, "AU": 0.0, "JP": 0.0, "MY": 0.0, "NZ": 0.0, "SG": 0.0,
    # 2026-05-12 backfill — Oct'25–Jan'26 the data warehouse observed BAU.
    "US": 0.0, "UK": 0.0, "CA": 0.0, "EU": 0.0, "PL": 0.0, "GCC": 0.0,
}

# Last-resort BAU when a country isn't tabulated at all.
_BAU_GLOBAL_DEFAULT = 0.0  # REDACTED


# ---------------------------------------------------------------------------
# Lift table: country -> discount tier -> lift factor.
#
# Tiers:
#   "t15"  → 13–17% discount band
#   "t20"  → 18–22% discount band
#   "t25"  → 23–28% discount band (placeholder, low data)
#
# Lifts computed as (sale-period total u/day) ÷ (same-month BAU u/day).
# ---------------------------------------------------------------------------
# REDACTED: commercial lift values removed (sample_n / confidence structure kept)
LIFT_TABLE_D2C: dict[str, dict[str, tuple[float, int, str]]] = {
    # country: {tier: (lift, sample_n_weeks, confidence)}
    # Refreshed 2026-05-12 from Oct 2025 - Jan 2026 the data warehouse `purchase_history`,
    # base ring only, channel whitelist applied. BF/NY weeks scrubbed.
    # [REDACTED] capped values mean raw lift exceeded LIFT_CAP — anchor business
    # decisions to t20 numbers, not t25 (pent-up holiday demand inflates).
    "US":  {"t15": (1.0, 1, "low"),      "t20": (1.0, 3, "med")},                                  # t25 BF-scrubbed
    "UK":  {"t15": (1.0, 3, "high"),     "t20": (1.0, 4, "high"),     "t25": (1.0, 2, "med")},
    "CA":  {"t15": (1.0, 5, "high"),     "t20": (1.0, 3, "high"),     "t25": (1.0, 4, "med")},
    "EU":  {"t15": (1.0, 4, "med-high"), "t20": (1.0, 2, "med-high"), "t25": (1.0, 2, "med")},
    "PL":  {"t15": (1.0, 2, "med"),      "t20": (1.0, 2, "med"),      "t25": (1.0, 3, "med")},
    "GCC": {"t15": (1.0, 2, "med-low"),  "t20": (1.0, 1, "low"),      "t25": (1.0, 2, "med-low")},
    "AU":  {"t15": (1.0, 2, "high"),     "t20": (1.0, 2, "high"),     "t25": (1.0, 5, "high")},
    "IN":  {"t15": (1.0, 2, "high"),     "t20": (1.0, 2, "high"),     "t25": (1.0, 3, "high")},
    "JP":  {"t15": (1.0, 1, "low"),      "t20": (1.0, 3, "low"),      "t25": (1.0, 2, "low")},    # JP discount-resistant
    "SG":  {"t15": (1.0, 4, "med"),                                  "t25": (1.0, 3, "med")},
    "MY":  {"t15": (1.0, 2, "low"),                                  "t25": (1.0, 3, "low")},
    "NZ":  {"t15": (1.0, 2, "low"),      "t20": (1.0, 1, "low"),      "t25": (1.0, 6, "low")},
}

# Peer countries for fallback when country has no data at requested tier.
PEER_MAP: dict[str, str] = {
    "NZ": "AU",
    "MY": "SG",
    "SG": "MY",  # bidirectional
}

# Lifts can never go below the floor (running a sale doesn't decrease sales) or
# above the cap ([REDACTED]; small-denominator markets produce noisy ratios).
LIFT_FLOOR = 1.0  # REDACTED
LIFT_CAP = 1.0  # REDACTED

# REDACTED: commercial values removed
# Global lift defaults used as last-resort fallback (median of Oct'25-Jan'26
# large-market data: US/UK/CA/EU/AU/IN — capped tiers conservatively included).
_GLOBAL_LIFT_DEFAULTS: dict[str, float] = {
    "t15": 1.0,
    "t20": 1.0,
    "t25": 1.0,   # PRIOR BUG: missing t25 → callers fell through and silently used a hardcoded [REDACTED] default in _classify_tier callers
}


def _classify_tier(discount_pct: float) -> str:
    """Bucket a discount % into the lift table's tier key."""
    if discount_pct <= 0:
        return ""
    if discount_pct < 13:
        return "t15"  # below-band promos lean on t15 lift
    if discount_pct <= 17:
        return "t15"
    if discount_pct <= 22:
        return "t20"
    return "t25"


def _adjust_lift_for_discount(base_lift: float, base_disc: float, target_disc: float) -> float:
    """Scale a lift factor when the historical comp's discount differs from
    the target. Mirrors the retailer evaluator's diminishing-returns curve
    (^0.7) so behavior is consistent across retailer + D2C paths.
    """
    if base_disc <= 0 or target_disc <= 0 or base_disc == target_disc:
        return base_lift
    ratio = target_disc / base_disc
    # Anchor the lift's "incremental portion" to the discount ratio.
    # base_lift = 1 + incr → incr scales with ratio^0.7.
    incr = max(0.0, base_lift - 1.0)
    return 1.0 + incr * (ratio ** 0.7)


def get_bau_per_day(country: str, month: int) -> tuple[float, str]:
    """Look up BAU rate. Falls back: exact → country annual avg → global.

    `country` is normalized via `normalize_country()` first. Unrecognized
    countries return (0.0, "unresolved:<input>") so callers can fail loudly
    rather than silently using global default 1.0/day.
    """
    canonical = normalize_country(country)
    if not canonical:
        return 0.0, f"unresolved:{country}"
    monthly = BAU_RATES_D2C.get(canonical, {})
    if month in monthly:
        return monthly[month], f"{canonical}-month-{month}"
    if canonical in _BAU_ANNUAL_AVG:
        return _BAU_ANNUAL_AVG[canonical], f"{canonical}-annual-avg"
    return _BAU_GLOBAL_DEFAULT, "global-default"


def get_lift(country: str, discount_pct: float) -> LiftEstimate:
    """Look up a lift factor for (country, discount %).

    Fallback chain:
      1. Direct hit: country has data at the target tier.
      2. Country has data at adjacent tier — scale by discount ratio^0.7.
      3. Peer country at target tier (AU↔NZ, SG↔MY).
      4. Global default for the tier.

    Always floors at 1.0 and caps at 5.0.

    Unrecognized country → LiftEstimate with confidence="cannot_evaluate" and
    source listing supported countries; caller is responsible for surfacing.
    """
    canonical = normalize_country(country)
    if not canonical:
        supported = ", ".join(sorted(SUPPORTED_D2C_COUNTRIES))
        return LiftEstimate(
            lift=0.0,
            bau_per_day=0.0,
            sample_n=0,
            source=f"unresolved:{country}",
            confidence="cannot_evaluate",
            normalized_country="",
        )

    tier = _classify_tier(discount_pct)
    if not tier:
        return LiftEstimate(1.0, 0.0, 0, "no-discount", "n/a", canonical)

    # Tier 1: direct hit.
    country_table = LIFT_TABLE_D2C.get(canonical, {})
    if tier in country_table:
        lift, n, conf = country_table[tier]
        # Adjust if the country's tier is centered at e.g. 20% but request is 18%.
        # We don't store sub-band centers, so this is approximate.
        lift = max(LIFT_FLOOR, min(LIFT_CAP, lift))
        return LiftEstimate(lift, 0.0, n, f"country:{canonical}-{tier}", conf, canonical)

    # Tier 2: country has adjacent tier, scale.
    if country_table:
        # Pick the closest tier we have data for and scale.
        tier_centers = {"t15": 15.0, "t20": 20.0, "t25": 25.0}
        target_center = tier_centers.get(tier, discount_pct)
        # Find closest tier present.
        adj_tier = min(country_table.keys(), key=lambda t: abs(tier_centers.get(t, 0) - target_center))
        adj_lift, n, _ = country_table[adj_tier]
        scaled = _adjust_lift_for_discount(adj_lift, tier_centers.get(adj_tier, 0), discount_pct)
        scaled = max(LIFT_FLOOR, min(LIFT_CAP, scaled))
        return LiftEstimate(scaled, 0.0, n, f"country:{canonical}-{adj_tier}-scaled", "med-low", canonical)

    # Tier 3: peer fallback.
    peer = PEER_MAP.get(canonical)
    if peer:
        peer_table = LIFT_TABLE_D2C.get(peer, {})
        if tier in peer_table:
            lift, n, _ = peer_table[tier]
            lift = max(LIFT_FLOOR, min(LIFT_CAP, lift))
            return LiftEstimate(lift, 0.0, n, f"peer:{peer}-{tier}", "med-low", canonical)

    # Tier 4: global default.
    g = _GLOBAL_LIFT_DEFAULTS.get(tier, 1.0)  # REDACTED
    g = max(LIFT_FLOOR, min(LIFT_CAP, g))
    return LiftEstimate(g, 0.0, 0, f"global-default-{tier}", "low", canonical)


def get_lift_estimate(country: str, discount_pct: float, month: int) -> LiftEstimate:
    """Convenience: combine BAU + lift lookup into a single LiftEstimate
    populated with bau_per_day for that month."""
    lift_est = get_lift(country, discount_pct)
    bau, _bau_src = get_bau_per_day(country, month)
    lift_est.bau_per_day = bau
    return lift_est
