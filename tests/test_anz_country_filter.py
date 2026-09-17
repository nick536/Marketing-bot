"""ANZ tab split + Country-aware promo-history filter (2026-05-19).

Covers: region_accepts truth table, _parse_promo_records country filtering
on shared tabs (Asia / GCC), Country-less tab regression, GWP comp exclusion.
"""
import os, sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
os.environ.setdefault("SLACK_BOT_TOKEN", "test")
os.environ.setdefault("SLACK_SIGNING_SECRET", "test")
os.environ.setdefault("GOOGLE_SERVICE_ACCOUNT_JSON", "{}")
os.environ.setdefault("PROMO_SHEET_ID", "test")
os.environ.setdefault("PL_SHEET_ID", "test")
os.environ.setdefault("SELLOUT_SHEET_ID", "test")

from app.regions import region_accepts
from app.sheets import _parse_promo_records, _infer_promo_type
from app.evaluator import _find_best_comparable, _interpolate_lift_from_records


def test_aggregate_groups_defined():
    from app.regions import REGION_TAB_GROUPS
    assert set(REGION_TAB_GROUPS["Europe"]) >= {"Germany", "France", "Spain", "BeNeLux"}
    assert set(REGION_TAB_GROUPS["GCC"]) >= {"UAE", "KSA"}


def test_region_accepts_truth_table():
    # single-country requests partition the shared Asia tab
    assert region_accepts("AU", "Australia") is True
    assert region_accepts("AU", "Singapore") is False
    assert region_accepts("AU", "Japan") is False
    assert region_accepts("NZ", "New Zealand") is True
    assert region_accepts("NZ", "Japan") is False
    assert region_accepts("JP", "Japan") is True
    assert region_accepts("SG", "Japan") is False
    # aggregate requests keep constituents
    assert region_accepts("GCC", "UAE") is True
    assert region_accepts("GCC", "Saudi Arabia") is True
    assert region_accepts("GCC", "Germany") is False
    assert region_accepts("APCOM", "Czechia") is True
    assert region_accepts("Europe", "Germany") is True
    assert region_accepts("Nordics", "Sweden") is True
    # fail-open: blank / unresolvable / unknown-request → keep
    assert region_accepts("UK", "") is True
    assert region_accepts("AU", "Narnia") is True
    assert region_accepts("", "Japan") is True
    assert region_accepts(None, "Japan") is True


def _asia_rows():
    """Mirrors live 'Asia Promo Performance' columns."""
    return [
        {"Retailer": "Mattressco", "Country": "Singapore", "Promo Name": "Mattressco GWP",
         "Discount": "0%", "Units Sold": "0", "Revenue (USD)": "0",  # REDACTED
         "Promo Start": "01-May-25", "Promo End": "30-Jun-25", "ROAS": "0.0x"},  # REDACTED
        {"Retailer": "Sakura", "Country": "Japan", "Promo Name": "Spring Sale",
         "Discount": "0%", "Units Sold": "0", "Revenue (USD)": "0",  # REDACTED
         "Promo Start": "04-Mar-25", "Promo End": "26-Mar-25", "ROAS": "0.0x"},  # REDACTED
        {"Retailer": "Soundstore", "Country": "Australia", "Promo Name": "Father's Day",
         "Discount": "0%", "Units Sold": "0", "Revenue (USD)": "0",  # REDACTED
         "Promo Start": "01-Sep-25", "Promo End": "08-Sep-25", "ROAS": "0.0x"},  # REDACTED
        {"Retailer": "Noel Leeming", "Country": "New Zealand", "Promo Name": "NZ Spring",
         "Discount": "0%", "Units Sold": "0", "Revenue (USD)": "0",  # REDACTED
         "Promo Start": "01-Sep-25", "Promo End": "14-Sep-25", "ROAS": "0.0x"},  # REDACTED
    ]


def test_au_request_excludes_japan_and_singapore():
    h = _parse_promo_records(_asia_rows(), retailer="", region="AU")
    countries = {r.retailer for r in h.records}
    assert countries == {"Soundstore"}  # only the Australia row
    assert all("Sakura" != r.retailer and "Mattressco" != r.retailer
               for r in h.records)


def test_nz_request_excludes_sakura():
    h = _parse_promo_records(_asia_rows(), retailer="", region="NZ")
    assert [r.retailer for r in h.records] == ["Noel Leeming"]


def test_gcc_aggregate_keeps_constituents():
    rows = [
        {"Retailer": "Megamart", "Country": "UAE", "Promo Name": "Ramadan",
         "Discount": "0%", "Units Sold": "0", "Revenue (USD)": "0",  # REDACTED
         "Promo Start": "01-Mar-25", "Promo End": "10-Mar-25"},
        {"Retailer": "Bookmart", "Country": "Saudi Arabia", "Promo Name": "KSA Promo",
         "Discount": "0%", "Units Sold": "0", "Revenue (USD)": "0",  # REDACTED
         "Promo Start": "01-Apr-25", "Promo End": "10-Apr-25"},
        {"Retailer": "Voltmart", "Country": "India", "Promo Name": "Diwali",
         "Discount": "0%", "Units Sold": "0", "Revenue (USD)": "0",  # REDACTED
         "Promo Start": "01-Nov-25", "Promo End": "10-Nov-25"},
    ]
    h = _parse_promo_records(rows, retailer="", region="GCC")
    got = {r.retailer for r in h.records}
    assert got == {"Megamart", "Bookmart"}  # India excluded, UAE+KSA kept


def test_country_less_tab_no_regression():
    """Canada/Mapleline-shaped rows (no Country column) are all retained."""
    rows = [
        {"Retailer": "Techbuy", "Promo Name": "BF", "Discount": "0%",  # REDACTED
         "Units Sold": "0", "Revenue (USD)": "0",  # REDACTED
         "Promo Start": "24-Nov-25", "Promo End": "01-Dec-25"},
        {"Retailer": "Bulkclub", "Promo Name": "Holiday", "Discount": "0%",  # REDACTED
         "Units Sold": "0", "Revenue (USD)": "0",  # REDACTED
         "Promo Start": "01-Dec-25", "Promo End": "15-Dec-25"},
    ]
    h = _parse_promo_records(rows, retailer="", region="CA")
    assert len(h.records) == 2


def test_gwp_inferred_and_excluded_from_discount_comps():
    assert _infer_promo_type("", "Mattressco GWP", "") == "gwp"
    assert _infer_promo_type("", "Father's Day 10% off", "") == "promo"

    h = _parse_promo_records(_asia_rows(), retailer="", region="SG")
    # SG keeps only the Mattressco GWP row; it must NOT be a discount comp.
    comp = _find_best_comparable(h.records, target_disc=10.0,
                                 target_promo_type="promo")
    assert comp is None
    lift = _interpolate_lift_from_records(h.records, target_disc=10.0,
                                          baseline=5.0)
    assert lift is None


def test_europe_aggregate_concatenates_constituent_tabs(monkeypatch):
    """Structural test: get_promo_history("", "Europe") iterates REGION_TAB_GROUPS,
    dedupes tabs, concatenates records, populates ROAS stats, sets retailer_matched —
    no network call. Also asserts _discover_tab receives tab_titles (single-fetch proof)."""
    import app.sheets as S
    from app.models import PromoHistory, PromoRecord

    # Fake sheet object — worksheets() should NOT be called directly by _discover_tab
    # because the aggregate path pre-fetches and passes tab_titles kwarg.
    # We track worksheets() calls on the fake sheet to confirm single-fetch.
    class _FakeSheet:
        def worksheets(self):
            # Return fake worksheet stubs
            class _WS:
                def __init__(self, t):
                    self.title = t
            return [_WS("Germany_tab"), _WS("France_tab")]

    class _FakeClient:
        def open_by_key(self, key):
            return _FakeSheet()

    monkeypatch.setattr(S, "_get_client", lambda: _FakeClient())

    # No retailer-specific tab for any name
    monkeypatch.setattr(S, "_find_retailer_tab", lambda sheet, retailer: None)

    # Patch _discover_tab with a recorder that captures tab_titles kwarg
    discover_calls = []

    def fake_discover_tab(sheet, reg, kw_map, fb_map, tab_titles=None):
        discover_calls.append({"reg": reg, "tab_titles": tab_titles})
        return {
            "Germany": "Germany_tab",
            "France":  "France_tab",
            "Italy":   "Germany_tab",   # duplicate → should be read only once
        }.get(reg, None)

    monkeypatch.setattr(S, "_discover_tab", fake_discover_tab)

    # Track which tabs _read_one_tab was called with
    calls = []

    def fake_read_one_tab(sheet, tab_name, retailer, region):
        calls.append((tab_name, region))
        h = PromoHistory()
        # Attach a roas to each record so ROAS aggregation can be asserted
        rec = PromoRecord(market=region, retailer=f"r-{tab_name}", promo_name="x")
        rec.roas = 5.0 if tab_name == "Germany_tab" else 3.0
        h.records = [rec]
        h.sample_count = 1
        return h

    monkeypatch.setattr(S, "_read_one_tab", fake_read_one_tab)

    h = S.get_promo_history("", "Europe")

    # Two unique tabs found (Germany_tab and France_tab); Italy_tab deduped
    assert set(tab for tab, _ in calls) == {"Germany_tab", "France_tab"}
    # Each call must pass the AGGREGATE region, not the sub-country
    assert all(region == "Europe" for _, region in calls), \
        f"Expected all calls to use 'Europe' but got: {calls}"
    # Two records concatenated
    assert len(h.records) == 2
    assert h.sample_count == 2
    # Dedupe: Germany_tab appears only once despite Italy also mapping to it
    assert calls.count(("Germany_tab", "Europe")) == 1

    # Fix 1: ROAS stats must be populated from merged records (5.0 and 3.0)
    assert h.avg_roas == 4.0, f"Expected avg_roas=4.0, got {h.avg_roas}"
    assert h.best_roas == 5.0, f"Expected best_roas=5.0, got {h.best_roas}"
    assert h.worst_roas == 3.0, f"Expected worst_roas=3.0, got {h.worst_roas}"
    # Fix 1: retailer_matched must be set to aggregate label
    assert h.retailer_matched == "Europe (aggregate)", \
        f"Expected 'Europe (aggregate)', got {h.retailer_matched!r}"

    # Fix 3: every _discover_tab call must have received tab_titles (not None),
    # proving the aggregate path pre-fetched once and passed it through.
    assert all(c["tab_titles"] is not None for c in discover_calls), \
        f"Some _discover_tab calls had tab_titles=None: {discover_calls}"


def test_discover_tab_uses_provided_tab_titles_without_calling_worksheets():
    """_discover_tab must use the supplied tab_titles list and skip sheet.worksheets()."""
    import app.sheets as S

    class _BrokenSheet:
        """A sheet whose worksheets() raises — proves _discover_tab never calls it."""
        def worksheets(self):
            raise RuntimeError("worksheets() must not be called when tab_titles is supplied")

    from app.config import REGION_TAB_KEYWORDS, REGION_TAB_MAP_FALLBACK

    # Provide tab_titles directly — Germany has a pattern in REGION_TAB_KEYWORDS
    # that should match "Germany Promo Performance"
    provided_titles = ["Germany Promo Performance", "France Promo Performance"]

    result = S._discover_tab(
        _BrokenSheet(), "Germany",
        REGION_TAB_KEYWORDS, REGION_TAB_MAP_FALLBACK,
        tab_titles=provided_titles,
    )

    # Should have matched without touching .worksheets()
    assert result == "Germany Promo Performance", \
        f"Expected 'Germany Promo Performance', got {result!r}"
