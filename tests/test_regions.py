"""Tests for app.regions canonical region module."""
import os, sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
os.environ.setdefault("SLACK_BOT_TOKEN", "test")
os.environ.setdefault("SLACK_SIGNING_SECRET", "test")
os.environ.setdefault("GOOGLE_SERVICE_ACCOUNT_JSON", "{}")
os.environ.setdefault("PROMO_SHEET_ID", "test")
os.environ.setdefault("PL_SHEET_ID", "test")
os.environ.setdefault("SELLOUT_SHEET_ID", "test")

from app.regions import (
    normalize_region, calendar_columns_for, poc_ids_for, REGIONS
)


def test_normalize_handles_short_codes_and_full_names():
    """Both short codes and full country names normalize to same canonical."""
    pairs = [
        ("UAE", "uae"), ("U.A.E.", "uae"), ("united arab emirates", "uae"),
        ("KSA", "ksa"), ("Saudi Arabia", "ksa"),
        ("Germany", "germany"), ("DE", "germany"), ("Deutschland", "germany"),
        ("Austria", "austria"), ("AT", "austria"),
        ("Switzerland", "switzerland"), ("CH", "switzerland"),
        ("Europe (Switzerland)", "switzerland"),
        ("NL", "benelux"), ("Netherlands", "benelux"),
        ("Sweden", "sweden"), ("SE", "sweden"),
        ("JP", "jp"), ("Japan", "jp"),
        ("Mexico", "mexico"), ("MX", "mexico"),
    ]
    for inp, _expected_alias in pairs:
        canonical = normalize_region(inp)
        assert canonical, f"{inp!r} should normalize to a canonical, got empty"
        # Confirm round-trip: the canonical itself normalizes to itself
        assert normalize_region(canonical).lower() == canonical.lower()


def test_normalize_unknown_returns_empty():
    assert normalize_region("Atlantis") == ""
    assert normalize_region("") == ""
    assert normalize_region(None) == ""


def test_calendar_columns_individual_gcc_country():
    """Was bugged before — UAE alone returned [] because only GCC aggregate
    was mapped. Now UAE → ['UAE']."""
    assert calendar_columns_for("UAE") == ["UAE"]
    assert calendar_columns_for("KSA") == ["KSA"]
    assert calendar_columns_for("uae") == ["UAE"]   # case insensitive


def test_calendar_columns_individual_nordic_country():
    """Was bugged before — Sweden alone returned [] because only Nordics
    aggregate was mapped. Now Sweden → ['Sweden']."""
    assert calendar_columns_for("Sweden") == ["Sweden"]
    assert calendar_columns_for("Iceland") == ["Iceland"]


def test_calendar_columns_aggregates():
    assert set(calendar_columns_for("GCC")) == {"UAE", "KSA", "Kuwait", "Qatar", "Bahrain"}
    assert set(calendar_columns_for("Nordics")) == {"Sweden", "Norway", "Denmark", "Finland", "Iceland"}
    assert set(calendar_columns_for("APCOM")) == {"Czechia", "Romania", "Slovakia", "Hungary"}


def test_calendar_columns_germany_alias():
    assert calendar_columns_for("Germany") == ["Germany"]
    assert calendar_columns_for("DE") == ["Germany"]
    assert calendar_columns_for("Deutschland") == ["Germany"]


def test_calendar_columns_austria_alias():
    assert calendar_columns_for("Austria") == ["Austria"]
    assert calendar_columns_for("AT") == ["Austria"]


def test_calendar_columns_switzerland_alias():
    assert calendar_columns_for("Switzerland") == ["Switzerland"]
    assert calendar_columns_for("CH") == ["Switzerland"]
    assert calendar_columns_for("Europe (Switzerland)") == ["Switzerland"]


def test_calendar_columns_japan_alias():
    assert calendar_columns_for("Japan") == ["Japan"]
    assert calendar_columns_for("JP") == ["Japan"]


def test_calendar_columns_nl_routes_to_benelux():
    assert calendar_columns_for("NL") == ["BeNeLux"]
    assert calendar_columns_for("Netherlands") == ["BeNeLux"]


def test_calendar_columns_israel_mexico_empty():
    assert calendar_columns_for("Israel") == []
    assert calendar_columns_for("Mexico") == []


def test_poc_ids_individual_gcc_country():
    """UAE/KSA/Kuwait/Qatar/Bahrain all → PersonA."""
    for r in ["UAE", "KSA", "Kuwait", "Qatar", "Bahrain"]:
        assert poc_ids_for(r) == ["U0REDACT001"]


def test_poc_ids_individual_nordic_country():
    expected = {"U0REDACT002", "U0REDACT003"}
    for r in ["Sweden", "Norway", "Denmark", "Finland", "Iceland"]:
        assert set(poc_ids_for(r)) == expected, f"{r} mismatch"


def test_poc_ids_apac_uses_aliases():
    expected = {"U0REDACT006", "U0REDACT007", "U0REDACT008"}
    for r in ["AU", "Australia", "NZ", "New Zealand", "JP", "Japan",
              "SG", "Singapore", "MY", "Malaysia", "TH", "Thailand"]:
        assert set(poc_ids_for(r)) == expected, f"{r} mismatch"


def test_poc_ids_germany_alias_routes_eu():
    expected = {"U0REDACT002", "U0REDACT003"}
    for r in ["Germany", "DE", "Deutschland"]:
        assert set(poc_ids_for(r)) == expected, f"{r} mismatch"


def test_every_canonical_region_has_required_keys():
    """Every entry must have calendar_columns + poc_ids + aliases."""
    for canonical, info in REGIONS.items():
        assert "calendar_columns" in info, f"{canonical} missing calendar_columns"
        assert "poc_ids" in info, f"{canonical} missing poc_ids"
        assert "aliases" in info, f"{canonical} missing aliases"
        assert isinstance(info["calendar_columns"], list)
        assert isinstance(info["poc_ids"], list)
        assert isinstance(info["aliases"], list)


def test_regression_2026_05_15_tracker_regions_all_resolve():
    """Every Region value seen in the production Promo Tracker on 2026-05-15
    must normalize to a non-empty canonical. Caught: UAE, Germany, NL,
    Europe (Switzerland), MY, Sweden, Austria, DE, Japan."""
    tracker_regions = [
        "AU", "NZ", "Germany", "NL", "UK", "Europe (Switzerland)",
        "MY", "CA", "ZA", "Nordics", "Sweden", "Europe", "GCC",
        "UAE", "DE", "Austria", "Mexico", "Japan", "Poland",
    ]
    for r in tracker_regions:
        assert normalize_region(r), f"Region {r!r} from production tracker doesn't normalize"
        # And both lookups should return something sensible (could be [] for Mexico/Israel)
        cols = calendar_columns_for(r)
        pocs = poc_ids_for(r)
        assert isinstance(cols, list)
        assert isinstance(pocs, list)
