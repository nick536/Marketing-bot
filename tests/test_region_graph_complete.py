import os
for k in ("SLACK_BOT_TOKEN","SLACK_SIGNING_SECRET","PROMO_SHEET_ID","PL_SHEET_ID","SELLOUT_SHEET_ID"):
    os.environ.setdefault(k,"x")
os.environ.setdefault("GOOGLE_SERVICE_ACCOUNT_JSON","{}")
from app.config import REGION_TAB_KEYWORDS

def test_individual_country_tabs_routed():
    import re
    # Mirror of real Promo Sheet tab titles — update if tabs are renamed in the sheet.
    real_titles = [
        "Bulkclub Spain","Germany_Alderon Promo Performance","France_Galerie Promo Performance",
        "France_Flashvente Promo Performance","Alpencomp Promo Performance","ALPENHAUS Flyer Performance",
        "Digitex AG Promo Performance","BeNeLux_Lowlands NL Promo Performance",
    ]
    for r in ("France","Spain","Germany","Switzerland","BeNeLux"):
        pats = REGION_TAB_KEYWORDS.get(r)
        assert pats, f"{r} has no tab keyword"
        assert any(re.search(p, t, re.I) for p in pats for t in real_titles), \
            f"{r} keyword matches no real tab"


# ---------------------------------------------------------------------------
# C1 completeness tests — canonical fallback graph + NEW_MARKETS sentinel
# ---------------------------------------------------------------------------
from app.regions import REGIONS, SIMILAR_MARKETS, NEW_MARKETS, normalize_region


def test_every_region_has_fallback_or_is_new():
    for r in REGIONS:
        if r in NEW_MARKETS:
            assert not SIMILAR_MARKETS.get(r), f"{r} is NEW_MARKET but also has fallbacks"
            continue
        fbs = SIMILAR_MARKETS.get(r)
        assert fbs, f"{r} has no fallback and is not a NEW_MARKET"
        for f in fbs:
            assert f in REGIONS, f"{r}->{f} not a canonical REGIONS key"
            assert normalize_region(f) == f, f"{r}->{f} not canonical-normalized"
            assert f != r, f"{r} self-loop"


def test_new_markets_are_canonical_regions():
    for m in NEW_MARKETS:
        assert m in REGIONS, f"NEW_MARKET {m} not a canonical region"


def test_similar_markets_keys_are_canonical():
    for r in SIMILAR_MARKETS:
        assert r in REGIONS, f"SIMILAR_MARKETS key {r!r} is not a canonical region"


def test_fallback_targets_resolve_single_hop():
    # Resolution in get_promo_history_with_fallback is single-hop (it does not
    # recurse), so mutual pairs (Europe<->Poland, CA<->US, AU<->NZ, GCC<->India,
    # TH/PH/SG) are intentional and fine. We only assert every target is itself
    # a key that either has fallbacks or is a NEW_MARKET (no dangling targets).
    for r, fbs in SIMILAR_MARKETS.items():
        for f in fbs:
            assert f in SIMILAR_MARKETS or f in NEW_MARKETS, \
                f"fallback target {f} (from {r}) is itself unmapped"


# ---------------------------------------------------------------------------
# C2: NEW_MARKETS guard in get_promo_history_with_fallback
# ---------------------------------------------------------------------------

def test_new_market_skips_enrichment(monkeypatch):
    import app.sheets as s
    from app.models import PromoHistory
    monkeypatch.setattr(s, "get_promo_history", lambda r, reg: PromoHistory())
    monkeypatch.setattr(s, "get_sales_baseline", lambda reg, r="": None)
    h = s.get_promo_history_with_fallback("", "Mexico")
    assert not h.fallback_source            # no cross-region borrow
    assert h.sample_count == 0


# ---------------------------------------------------------------------------
# C2: evaluator emits NEW_MARKET vs NO_HISTORY based on region membership
# ---------------------------------------------------------------------------

def _minimal_pl():
    from app.models import PLImpact
    return PLImpact()


def _minimal_velocity():
    from app.models import VelocityCheck
    return VelocityCheck()


def test_evaluator_new_market_flag_for_new_market_region():
    """Empty history for a NEW_MARKET region → NEW_MARKET flag, not NO_HISTORY."""
    from app.evaluator import assess_risk_flags
    from app.models import PromoHistory
    from app.parser import PromoRequest

    request = PromoRequest(retailer="First Rep", region="Mexico")
    history = PromoHistory()  # sample_count=0
    flags = assess_risk_flags(request, history, _minimal_velocity(), _minimal_pl())

    flag_codes = [f.flag for f in flags]
    assert "NEW_MARKET" in flag_codes, f"Expected NEW_MARKET flag, got: {flag_codes}"
    assert "NO_HISTORY" not in flag_codes, f"Expected no NO_HISTORY flag, got: {flag_codes}"


def test_evaluator_no_history_flag_for_non_new_market_region():
    """Empty history for a non-NEW_MARKET region → NO_HISTORY flag, not NEW_MARKET."""
    from app.evaluator import assess_risk_flags
    from app.models import PromoHistory
    from app.parser import PromoRequest

    request = PromoRequest(retailer="Bulkclub", region="US")
    history = PromoHistory()  # sample_count=0
    flags = assess_risk_flags(request, history, _minimal_velocity(), _minimal_pl())

    flag_codes = [f.flag for f in flags]
    assert "NO_HISTORY" in flag_codes, f"Expected NO_HISTORY flag, got: {flag_codes}"
    assert "NEW_MARKET" not in flag_codes, f"Expected no NEW_MARKET flag, got: {flag_codes}"


# ---------------------------------------------------------------------------
# C2 Fix 3: baseline injection for new-market path
# ---------------------------------------------------------------------------

def test_new_market_injects_sales_baseline(monkeypatch):
    import app.sheets as s
    from app.models import PromoHistory
    monkeypatch.setattr(s, "get_promo_history", lambda r, reg: PromoHistory())
    monkeypatch.setattr(s, "get_sales_baseline", lambda reg, r="": 0.0)  # REDACTED
    h = s.get_promo_history_with_fallback("", "Mexico")
    assert h.baseline_units_weekly == 0.0  # REDACTED
    assert not h.fallback_source
    assert h.sample_count == 0


# ---------------------------------------------------------------------------
# C2 Fix 1 proof: NEW_MARKET flag → human-readable condition line
# ---------------------------------------------------------------------------

def test_new_market_flag_produces_condition():
    """NEW_MARKET risk flag → generate_conditions emits the pilot condition string."""
    from app.evaluator import generate_conditions
    from app.models import RiskFlag, PromoHistory, PLImpact, VelocityCheck
    from app.parser import PromoRequest

    request = PromoRequest(retailer="First Rep", region="Mexico")
    flags = [RiskFlag("NEW_MARKET", "new market")]
    history = PromoHistory()
    conditions = generate_conditions(request, flags, history, VelocityCheck(), PLImpact())
    assert any("New market" in c for c in conditions), f"Expected NEW_MARKET condition, got: {conditions}"


# ---------------------------------------------------------------------------
# C3: SALES_TAB_KEYWORDS entries for ZA / MY / APCOM
# ---------------------------------------------------------------------------

from app.config import SALES_TAB_KEYWORDS


def test_sales_coverage_for_unmapped_markets():
    import re
    # Real sales-section tab titles in the Promo Sheet.
    sales_titles = [
        "Carmel Sales","Savannamart","Influencer sales","ANZ Sales","Canada Sell-Out by SKU",
        "Nedermart Sales_Oct 25 to Mar 26","Alderon Sales_LTD","Gulfshore sales",
        "Vistula Sales","Sellout by Distributor - India Retail",
    ]
    # All three have keywords (MY/APCOM future-proof by design).
    for r in ("ZA","MY","APCOM"):
        assert SALES_TAB_KEYWORDS.get(r), f"{r} missing sales-tab keyword"
    za = SALES_TAB_KEYWORDS["ZA"]
    # ZA must resolve to the real Savannamart sales tab...
    assert any(re.search(p, "Savannamart", re.I) for p in za), "ZA does not match the Savannamart sales tab"
    # ...and must NOT match the Savannamart PROMO tab (checked over ALL ZA patterns,
    # so removing the anchor in future cannot make this vacuously pass).
    assert not any(re.search(p, "Savannamart Promo Performance", re.I) for p in za), \
        "a ZA sales pattern wrongly matches the Savannamart promo tab"
    # No ZA pattern may match any OTHER real sales tab (guards over-broad patterns).
    for t in sales_titles:
        if t == "Savannamart":
            continue
        assert not any(re.search(p, t, re.I) for p in za), \
            f"ZA sales pattern over-matches unrelated sales tab {t!r}"
