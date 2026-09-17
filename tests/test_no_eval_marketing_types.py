import os
for k in ("SLACK_BOT_TOKEN","SLACK_SIGNING_SECRET","PROMO_SHEET_ID","PL_SHEET_ID","SELLOUT_SHEET_ID"):
    os.environ.setdefault(k,"x")
os.environ.setdefault("GOOGLE_SERVICE_ACCOUNT_JSON","{}")

from app.sheets import _infer_promo_type

def test_sales_contest_maps_to_no_eval():
    assert _infer_promo_type("Sales Contest", "", "") == "no_eval"

def test_pos_maps_to_no_eval():
    assert _infer_promo_type("POS", "", "") == "no_eval"

def test_pos_is_whole_word_only():
    assert _infer_promo_type("position paper", "", "") != "no_eval"
    assert _infer_promo_type("posters in stores", "", "") != "no_eval"

def test_existing_types_unchanged():
    assert _infer_promo_type("Discount", "", "") == "promo"
    assert _infer_promo_type("SPIV", "", "") == "spiv"
    assert _infer_promo_type("Ecom Campaign", "", "") == "ecom_campaign"
    assert _infer_promo_type("Emailer", "", "") == "emailer"


from app.parser import PromoRequest
from app.evaluator import evaluate_promo
from app.models import PromoHistory, MarketEconomics, VelocityCheck


def test_no_eval_short_circuits_evaluator():
    req = PromoRequest(retailer="Soundstore", region="AU",
                       promo_type="no_eval", channel="Retail")
    res = evaluate_promo(req, PromoHistory(), MarketEconomics(), VelocityCheck())
    assert res is not None
    assert res.verdict == "LOGGED"
    assert "not evaluated" in res.commentary.lower()


def test_sales_contest_request_short_circuits():
    req = PromoRequest(retailer="Soundstore", region="AU",
                       promo_type="sales_contest", channel="Retail")
    res = evaluate_promo(req, PromoHistory(), MarketEconomics(), VelocityCheck())
    assert res.verdict == "LOGGED"
    assert "not evaluated" in res.commentary.lower()


def test_pos_request_short_circuits():
    req = PromoRequest(retailer="Soundstore", region="AU",
                       promo_type="pos", channel="Retail")
    res = evaluate_promo(req, PromoHistory(), MarketEconomics(), VelocityCheck())
    assert res.verdict == "LOGGED"
    assert "not evaluated" in res.commentary.lower()
