"""Tests for app.slack_form structured field parser."""
import os, sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
os.environ.setdefault("SLACK_BOT_TOKEN", "test")
os.environ.setdefault("SLACK_SIGNING_SECRET", "test")
os.environ.setdefault("GOOGLE_SERVICE_ACCOUNT_JSON", "{}")
os.environ.setdefault("PROMO_SHEET_ID", "test"); os.environ.setdefault("PL_SHEET_ID", "test"); os.environ.setdefault("SELLOUT_SHEET_ID", "test")

from app.slack_form import parse_slack_form, has_form_fields


ALPENFOTO_MSG = """Marketing Scope
Retailer
*Region*
Europe
*Retailer*
Alpenfoto
*Marketing Type*
Stock Clearance Promo - Retailer Category Discontinuation
*Marketing investment*
NA
*Start Date*
2026-04-25
*End date*
2026-05-31
*Discount %*
0%
*SPIV per unit*
NA
*Additional Context*
Alpenfoto has stock in 18 store (Most stock >1year old)
- Retailer discontinuing category due to low sell out
- Pending stock will come back to Alderon which would be recirculated
- Stock > 1 year so runs the risk of Battery issue"""


def test_parse_alpenfoto_extracts_all_fields():
    f = parse_slack_form(ALPENFOTO_MSG)
    assert f["region"] == "Europe"
    assert f["retailer"] == "Alpenfoto"
    assert f["marketing_type"] == "Stock Clearance Promo - Retailer Category Discontinuation"
    assert f["start_date"] == "2026-04-25"
    assert f["end_date"] == "2026-05-31"
    assert f["discount_pct"] == "0%"  # REDACTED
    assert f["marketing_spend"] == ""   # was "NA" — normalized to empty
    assert f["spiv"] == ""              # NA → empty
    assert "stock in 18 store" in f["additional_context"]


def test_has_form_fields_detects_form():
    assert has_form_fields(ALPENFOTO_MSG) is True
    assert has_form_fields("Hi Approver, requesting approval for...") is False
    assert has_form_fields("") is False
    assert has_form_fields(None) is False


def test_parse_returns_empty_for_freeform():
    """Free-form messages without bold labels return empty dict."""
    assert parse_slack_form("Hi Approver, please approve the X promo at 20% off") == {}


def test_na_and_dash_values_normalize_to_empty():
    msg = "*Marketing investment*\nNA\n*SPIV per unit*\n-"
    f = parse_slack_form(msg)
    assert f.get("marketing_spend") == ""
    assert f.get("spiv") == ""


def test_label_aliases_work():
    """Form may use 'Type' instead of 'Marketing Type', etc."""
    msg = "*Type*\nDiscount\n*Channel*\nMegamart\n*Geography*\nUAE"
    f = parse_slack_form(msg)
    assert f["marketing_type"] == "Discount"
    assert f["channel"] == "Megamart"
    assert f["region"] == "UAE"


def test_multiline_value_preserved():
    """Additional Context can span multiple lines."""
    msg = "*Additional Context*\nLine 1\nLine 2\nLine 3"
    f = parse_slack_form(msg)
    assert "Line 1" in f["additional_context"]
    assert "Line 2" in f["additional_context"]
    assert "Line 3" in f["additional_context"]


def test_case_insensitive_label_match():
    msg = "*MARKETING TYPE*\nDiscount"
    f = parse_slack_form(msg)
    assert f["marketing_type"] == "Discount"
