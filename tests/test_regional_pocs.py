"""Tests for app.regional_pocs — region → POC ID mapping."""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
os.environ.setdefault("SLACK_BOT_TOKEN", "test")
os.environ.setdefault("SLACK_SIGNING_SECRET", "test")
os.environ.setdefault("GOOGLE_SERVICE_ACCOUNT_JSON", "{}")
os.environ.setdefault("PROMO_SHEET_ID", "test")
os.environ.setdefault("PL_SHEET_ID", "test")
os.environ.setdefault("SELLOUT_SHEET_ID", "test")

from app.regional_pocs import get_regional_poc_ids, REGIONAL_POC_MAP


def test_apac_regions_route_to_personf_persong_personh():
    expected = {"U0REDACT006", "U0REDACT007", "U0REDACT008"}
    for region in ["AU", "NZ", "JP", "SG", "MY", "TH", "PH", "Hong Kong", "Taiwan"]:
        assert set(get_regional_poc_ids(region)) == expected, f"{region} mismatch"


def test_eu_excluding_poland_routes_to_personb_personc():
    expected = {"U0REDACT002", "U0REDACT003"}
    for region in ["Europe", "BeNeLux", "Nordics", "France", "Germany",
                   "Italy", "Spain", "CH", "AT", "UK", "APCOM"]:
        assert set(get_regional_poc_ids(region)) == expected, f"{region} mismatch"


def test_poland_routes_to_persona_only():
    assert get_regional_poc_ids("Poland") == ["U0REDACT001"]


def test_gcc_israel_route_to_persona():
    assert get_regional_poc_ids("GCC") == ["U0REDACT001"]
    assert get_regional_poc_ids("Israel") == ["U0REDACT001"]


def test_na_routes_to_persond_persone():
    expected = {"U0REDACT004", "U0REDACT005"}
    for region in ["US", "CA", "Mexico"]:
        assert set(get_regional_poc_ids(region)) == expected, f"{region} mismatch"


def test_india_routes_to_personi():
    assert get_regional_poc_ids("India") == ["U0REDACT009"]


def test_za_routes_to_personb():
    assert get_regional_poc_ids("ZA") == ["U0REDACT002"]


def test_individual_nordic_countries_route_to_personb_personc():
    expected = {"U0REDACT002", "U0REDACT003"}
    for region in ["Sweden", "Norway", "Denmark", "Finland", "Iceland"]:
        assert set(get_regional_poc_ids(region)) == expected, f"{region} mismatch"


def test_unknown_region_returns_empty_list():
    assert get_regional_poc_ids("Atlantis") == []
    assert get_regional_poc_ids("") == []
    assert get_regional_poc_ids(None) == []
