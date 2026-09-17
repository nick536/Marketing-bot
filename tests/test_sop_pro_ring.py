import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

os.environ.setdefault("SLACK_BOT_TOKEN", "test")
os.environ.setdefault("SLACK_SIGNING_SECRET", "test")
os.environ.setdefault("GOOGLE_SERVICE_ACCOUNT_JSON", "{}")
os.environ.setdefault("PROMO_SHEET_ID", "test")
os.environ.setdefault("PL_SHEET_ID", "test")
os.environ.setdefault("SELLOUT_SHEET_ID", "test")

import pytest
from app.evaluator import _grade_from_pct, _generate_verdict
from app.parser import PromoRequest
from types import SimpleNamespace


@pytest.mark.parametrize("pct,cm3,expected", [
    (0.0, 0.0, "REJECT"),  # REDACTED
    (0.0, 0.0, "REJECT"),  # REDACTED
    (0.0, 0.0, "REJECT"),  # REDACTED
    (0.0, 0.0, "C"),  # REDACTED
    (0.0, 0.0, "C"),  # REDACTED
    (0.0, 0.0, "B"),  # REDACTED
    (0.0, 0.0, "B"),  # REDACTED
    (0.0, 0.0, "A"),  # REDACTED
    (0.0, 0.0, "A"),  # REDACTED
    (0.0, 0.0, "REJECT"),  # REDACTED
    (None, 0.0, "C"),  # REDACTED
])
def test_grade_band_sop(pct, cm3, expected):
    assert _grade_from_pct(pct, cm3) == expected


def test_reject_below_15_says_cbo_exception():
    pl = SimpleNamespace(cm3_cash=0.0, cm3_cash_pct=0.0, total_promo_investment=0.0)  # REDACTED
    v = _generate_verdict(PromoRequest(), pl, "REJECT", 0.0)
    assert "requires explicit CBO exception" in v
    assert "0%" in v  # REDACTED


def test_reject_negative_keeps_loses_money_wording():
    pl = SimpleNamespace(cm3_cash=0.0, cm3_cash_pct=0.0, total_promo_investment=0.0)  # REDACTED
    v = _generate_verdict(PromoRequest(), pl, "REJECT", 0.0)
    assert "loses money" in v
    assert "CBO exception" not in v


from app.evaluator import _sop_advisories


def test_discount_over_25_flags_cbo():
    req = PromoRequest(discount_pct=0.0)  # REDACTED
    advs = _sop_advisories(req)
    assert any("0%" in a and "CBO exception" in a for a in advs)  # REDACTED


def test_discount_25_or_below_no_flag():
    assert _sop_advisories(PromoRequest(discount_pct=0.0)) == []  # REDACTED
    assert _sop_advisories(PromoRequest(discount_pct=0.0)) == []  # REDACTED
    assert _sop_advisories(PromoRequest(discount_pct=None)) == []


from datetime import date, timedelta


def _start_in(days: int) -> str:
    return (date.today() + timedelta(days=days)).strftime("%Y-%m-%d")


def test_retail_inside_t4_flags():
    req = PromoRequest(channel="Retail", dates=[_start_in(20), _start_in(34)])
    assert any("lead time" in a for a in _sop_advisories(req))


def test_retail_outside_t4_no_flag():
    req = PromoRequest(channel="Retail", dates=[_start_in(40), _start_in(54)])
    assert not any("lead time" in a for a in _sop_advisories(req))


def test_d2c_uses_t2_window():
    # 20 days out: inside T-4 (retail) but outside T-2 (D2C) → no flag
    req = PromoRequest(channel="D2C", dates=[_start_in(20)])
    assert not any("lead time" in a for a in _sop_advisories(req))
    req2 = PromoRequest(channel="D2C", dates=[_start_in(10)])
    assert any("lead time" in a for a in _sop_advisories(req2))


def test_influencer_uses_t1_window():
    req = PromoRequest(channel="Influencer", dates=[_start_in(5)])
    assert any("lead time" in a for a in _sop_advisories(req))


def test_no_dates_no_leadtime_flag():
    assert _sop_advisories(PromoRequest(channel="Retail")) == []


def test_influencer_outside_t1_no_flag():
    # 10 days out vs influencer window 7 → 10 < 7 is False → no flag
    req = PromoRequest(channel="Influencer", dates=[_start_in(10)])
    assert not any("lead time" in a for a in _sop_advisories(req))


def test_influencer_signal_via_marketing_type_not_channel():
    # Realistic production: influencer arrives as marketing type / free text,
    # NOT as a Channel token. 5d out vs T-1 (7d) → flag.
    req = PromoRequest(promo_type="influencer", dates=[_start_in(5)])
    assert any("lead time" in a for a in _sop_advisories(req))
    req2 = PromoRequest(channel="D2C", raw_text="influencer code drop", dates=[_start_in(5)])
    assert any("lead time" in a for a in _sop_advisories(req2))


def test_channelless_retail_uses_t4():
    # Retailer named but no Channel field set → must still use retail T-4,
    # not default T-2. 20d out: inside T-4 (28) → flag; would NOT flag at T-2.
    req = PromoRequest(retailer="Megamart", dates=[_start_in(20)])
    assert any("lead time" in a for a in _sop_advisories(req))


def test_d2c_with_retailer_field_stays_t2():
    # D2C marker present alongside a retailer string → must NOT be treated as
    # retail T-4. 20d out vs T-2 (14) → no flag.
    req = PromoRequest(channel="D2C", retailer="Shopify", dates=[_start_in(20)])
    assert not any("lead time" in a for a in _sop_advisories(req))


def test_channelless_amazon_retailer_stays_t2():
    # retailer="Amazon" with no channel/raw text must resolve to T-2, not
    # retail T-4 (the d2c/amazon guard inspects retailer too). 20d out vs
    # T-2 (14) → no flag; would falsely flag if treated as retail T-4.
    req = PromoRequest(retailer="Amazon", dates=[_start_in(20)])
    assert not any("lead time" in a for a in _sop_advisories(req))
