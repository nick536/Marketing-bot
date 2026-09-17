import os, sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
os.environ.setdefault("SLACK_BOT_TOKEN", "test")
os.environ.setdefault("SLACK_SIGNING_SECRET", "test")
os.environ.setdefault("GOOGLE_SERVICE_ACCOUNT_JSON", "{}")
os.environ.setdefault("PROMO_SHEET_ID", "test")
os.environ.setdefault("PL_SHEET_ID", "test")
os.environ.setdefault("SELLOUT_SHEET_ID", "test")

from app.models import InfluencerCommercials, InfluencerPL
from app.parser import PromoRequest
from app.slack_form import parse_slack_form
from app.evaluator import model_influencer_pl, evaluate_promo
from app.sheets import _parse_influencer_commercials, _parse_influencer_volume


def test_influencer_commercials_defaults():
    c = InfluencerCommercials()
    assert c.retail_price_usd == 0.0  # REDACTED
    assert c.cogs_usd == 0.0  # REDACTED
    assert c.return_rate_pct == 0.0  # REDACTED
    assert c.commission_pct == 0.0  # REDACTED
    assert c.warranty_pct == 0.0  # REDACTED
    assert c.marketing_pct == 0.0  # REDACTED
    assert c.source == "default"


def test_influencer_pl_defaults():
    pl = InfluencerPL()
    assert pl.gross == 0.0
    assert pl.trade_discount == 0.0
    assert pl.channel_margin == 0.0
    assert pl.marketplace_commission == 0.0
    assert pl.returns == 0.0
    assert pl.tax == 0.0
    assert pl.net_revenue == 0.0
    assert pl.cogs == 0.0
    assert pl.commission == 0.0
    assert pl.marketing == 0.0
    assert pl.warranty == 0.0
    assert pl.payment_gateway == 0.0
    assert pl.total_sm == 0.0
    assert pl.total_warranty == 0.0
    assert pl.cm3_per_unit == 0.0
    assert pl.cm3_pct == 0.0
    assert pl.cm3_cash == 0.0
    assert pl.estimated_units == 0.0
    assert pl.grade == ""


def test_promo_request_has_commission_pct():
    assert PromoRequest().commission_pct == 0.0
    assert PromoRequest(channel="Influencer", commission_pct=0.0).commission_pct == 0.0  # REDACTED


def test_parse_influencer_commercials_from_rows():
    raw = [{
        "Retail price ($USD)": "$0", "COGS": "$0", "Return Rate": "0%",  # REDACTED
        "Commission": "0%", "Warranty": "0%", "Marketing": "0%",  # REDACTED
    }]
    c = _parse_influencer_commercials(raw)
    assert c.retail_price_usd == 0.0  # REDACTED
    assert c.cogs_usd == 0.0  # REDACTED
    assert c.return_rate_pct == 0.0  # REDACTED
    assert c.commission_pct == 0.0  # REDACTED
    assert c.warranty_pct == 0.0  # REDACTED
    assert c.marketing_pct == 0.0  # REDACTED
    assert c.source == "sheet"


def test_parse_influencer_commercials_empty_returns_default():
    c = _parse_influencer_commercials([])
    assert c.source == "default"
    assert c.retail_price_usd == 0.0  # REDACTED


def test_parse_influencer_volume_averages_units():
    raw = [
        {"Discount": "0%", "Units Sold": "0"},  # REDACTED
        {"Discount": "0%", "Units Sold": "0"},  # REDACTED
    ]
    avg, n = _parse_influencer_volume(raw)
    assert n == 2
    assert avg == 0.0  # REDACTED


def test_parse_influencer_volume_empty():
    avg, n = _parse_influencer_volume([])
    assert avg == 0.0
    assert n == 0


def test_parse_influencer_volume_skips_blank_units():
    raw = [
        {"Discount": "0%", "Units Sold": "0"},  # REDACTED
        {"Discount": "0%", "Units Sold": ""},  # REDACTED
    ]
    avg, n = _parse_influencer_volume(raw)
    assert n == 1
    assert avg == 0.0  # REDACTED



def test_influencer_pl_has_reference_sheet_fields():
    from app.models import InfluencerPL
    pl = InfluencerPL()
    for f in ("gross", "trade_discount", "channel_margin",
              "marketplace_commission", "returns", "tax", "net_revenue",
              "cogs", "commission", "marketing", "warranty",
              "payment_gateway", "total_sm", "total_warranty",
              "cm3_per_unit", "cm3_pct", "estimated_units", "cm3_cash",
              "grade"):
        assert hasattr(pl, f), f


def test_model_influencer_pl_22pct_worked_example():
    """[REDACTED] — reference-sheet retail/COGS/return/discount/commission/
    warranty/marketing worked example."""
    import pytest
    from app.evaluator import model_influencer_pl
    from app.models import InfluencerCommercials
    comm = InfluencerCommercials(
        retail_price_usd=1.0, cogs_usd=1.0, return_rate_pct=1.0,  # REDACTED
        commission_pct=1.0, warranty_pct=1.0, marketing_pct=1.0)  # REDACTED
    pl = model_influencer_pl(discount_pct=1.0, commission_pct=1.0,  # REDACTED
                             commercials=comm, estimated_units=1.0)  # REDACTED
    assert pl.gross == pytest.approx(0.0)  # REDACTED
    assert pl.trade_discount == pytest.approx(0.0)  # REDACTED
    assert pl.returns == pytest.approx(0.0)          # REDACTED
    assert pl.net_revenue == pytest.approx(0.0)  # REDACTED
    assert pl.commission == pytest.approx(0.0)      # REDACTED
    assert pl.marketing == pytest.approx(0.0)         # REDACTED
    assert pl.warranty == pytest.approx(0.0)       # REDACTED
    assert pl.cm3_per_unit == pytest.approx(0.0)  # REDACTED
    assert pl.cm3_pct == pytest.approx(0.0, abs=0.01)  # REDACTED
    assert pl.cm3_cash == pytest.approx(0.0)       # REDACTED
    assert pl.grade == "B"


def test_model_influencer_pl_deep_discount_rejects():
    """[REDACTED] — deep-discount worked example that results in a REJECT grade."""
    c = InfluencerCommercials(return_rate_pct=1.0, source="sheet")  # REDACTED
    pl = model_influencer_pl(discount_pct=1.0, commission_pct=1.0,  # REDACTED
                             commercials=c, estimated_units=0.0)
    assert pl.cm3_pct < 0.0  # REDACTED
    assert pl.grade == "REJECT"


def test_model_influencer_pl_grade_is_volume_invariant():
    """Same discount/commission/economics → same grade regardless of volume."""
    c = InfluencerCommercials(source="sheet")
    g_lo = model_influencer_pl(1.0, 1.0, c, estimated_units=0.0).grade  # REDACTED
    g_hi = model_influencer_pl(1.0, 1.0, c, estimated_units=0.0).grade  # REDACTED
    g_zero = model_influencer_pl(1.0, 1.0, c, estimated_units=0.0).grade  # REDACTED
    assert g_lo == g_hi == g_zero


def test_evaluate_promo_routes_influencer(monkeypatch):
    import app.evaluator as E
    from app.models import PromoHistory, MarketEconomics
    monkeypatch.setattr(E, "get_influencer_commercials",
                        lambda: InfluencerCommercials(source="sheet"), raising=False)
    monkeypatch.setattr(E, "get_influencer_volume_estimate",
                        lambda: (0.0, 1), raising=False)  # REDACTED
    req = PromoRequest(channel="Influencer", region="Global",
                       discount_pct=0.0, commission_pct=0.0)  # REDACTED
    res = evaluate_promo(req, PromoHistory(), MarketEconomics(), None)
    assert res.influencer_pl is not None
    assert res.influencer_pl.cm3_pct > 0.0
    assert res.verdict in ("APPROVE", "CONDITIONAL", "REJECT")
    assert res.grade == res.influencer_pl.grade
    assert "NO_HISTORY" not in [f.flag for f in (res.risk_flags or [])]


def test_evaluate_promo_influencer_commercials_default_flag(monkeypatch):
    import app.evaluator as E
    from app.models import PromoHistory, MarketEconomics
    monkeypatch.setattr(E, "get_influencer_commercials",
                        lambda: InfluencerCommercials(source="default"), raising=False)
    monkeypatch.setattr(E, "get_influencer_volume_estimate",
                        lambda: (0.0, 0), raising=False)
    req = PromoRequest(channel="Influencer", region="Global",
                       discount_pct=0.0, commission_pct=0.0)  # REDACTED
    res = evaluate_promo(req, PromoHistory(), MarketEconomics(), None)
    names = [f.flag for f in (res.risk_flags or [])]
    assert "COMMERCIALS_FROM_DEFAULT" in names
    assert "NO_PAST_PROMOS" in names


def test_attach_commentary_skips_influencer_eval():
    """_attach_commentary must not LLM-overwrite an influencer eval's
    deterministic commentary."""
    import sys
    import unittest.mock as mock
    # app.main does `bolt_app = App(token=..., ...)` at module level which calls
    # Slack's auth.test — stub it out so we can import the module in tests.
    with mock.patch("slack_bolt.App", return_value=mock.MagicMock()), \
         mock.patch("slack_bolt.adapter.fastapi.SlackRequestHandler",
                    return_value=mock.MagicMock()):
        # Remove cached module so the patched import is fresh (idempotent if
        # already loaded by a prior test run in the same process).
        sys.modules.pop("app.main", None)
        import app.main as M

    from app.models import PromoEvaluation, InfluencerPL
    ev = PromoEvaluation(verdict="APPROVE", grade="A",
                         commentary="DETERMINISTIC-INFLUENCER-TEXT",
                         influencer_pl=InfluencerPL(cm3_pct=0.0, grade="A"))  # REDACTED
    # _attach_commentary(promo, evaluation) — promo is only used inside the
    # LLM path; the early-return fires before any promo access, so pass None.
    M._attach_commentary(None, ev)
    assert ev.commentary == "DETERMINISTIC-INFLUENCER-TEXT"


def test_evaluate_promo_influencer_form_commission_overrides_default(monkeypatch):
    import app.evaluator as E
    from app.models import PromoHistory, MarketEconomics
    monkeypatch.setattr(E, "get_influencer_commercials",
                        lambda: InfluencerCommercials(commission_pct=0.0, source="sheet"),  # REDACTED
                        raising=False)
    monkeypatch.setattr(E, "get_influencer_volume_estimate",
                        lambda: (0.0, 2), raising=False)  # REDACTED
    req = PromoRequest(channel="Influencer", region="Global",
                       discount_pct=0.0, commission_pct=0.0)  # REDACTED
    res = evaluate_promo(req, PromoHistory(), MarketEconomics(), None)
    # form's commission used → commission cost higher than the default case. [REDACTED]
    assert res.influencer_pl.commission > 0.0  # REDACTED


def test_form_parser_recognizes_commission():
    msg = ("*Channel*\nInfluencer\n*Region*\nGlobal\n"
           "*Discount %*\n0\n*Commission %*\n0\n")  # REDACTED
    fields = parse_slack_form(msg)
    assert fields.get("channel") == "Influencer"
    assert str(fields.get("discount_pct")) == "0"  # REDACTED
    assert str(fields.get("commission_pct")) == "0"  # REDACTED


def test_workflow_message_wires_commission_to_request():
    """Live workflow path: parse_workflow_message must propagate Commission %
    into PromoRequest.commission_pct (the per-campaign lever)."""
    from app.parser import parse_workflow_message
    msg = ("*Channel*\nInfluencer\n*Region*\nGlobal\n"
           "*Marketing Type*\nDiscount\n"
           "*Discount %*\n0\n*Commission %*\n0\n"  # REDACTED
           "*Start Date*\n2026-06-01\n*End Date*\n2026-06-07\n")
    promo = parse_workflow_message(msg)
    assert promo.channel == "Influencer"
    assert promo.commission_pct == 0.0  # REDACTED


def test_workflow_message_commission_absent_defaults_zero():
    """No Commission % field → commission_pct 0.0 (evaluator falls back to tab)."""
    from app.parser import parse_workflow_message
    msg = ("*Channel*\nRetail\n*Region*\nAU\n*Retailer*\nSoundstore\n"
           "*Marketing Type*\nDiscount\n*Discount %*\n0\n"  # REDACTED
           "*Start Date*\n2026-06-01\n*End Date*\n2026-06-07\n")
    promo = parse_workflow_message(msg)
    assert promo.commission_pct == 0.0


from app.responder import build_influencer_pl_block
from app.models import InfluencerPL


def test_influencer_pl_block_renders():
    from app.responder import build_influencer_pl_block
    from app.models import InfluencerPL
    pl = InfluencerPL(
        gross=0.0, trade_discount=0.0, returns=0.0,  # REDACTED
        net_revenue=0.0, cogs=0.0, commission=0.0,  # REDACTED
        marketing=0.0, warranty=0.0, cm3_per_unit=0.0,  # REDACTED
        cm3_pct=0.0, estimated_units=0.0, cm3_cash=0.0,  # REDACTED
        grade="B")
    txt = build_influencer_pl_block(pl)
    assert "Net Revenue" in txt
    assert "0" in txt                 # net revenue shown  # REDACTED
    assert "0.0%" in txt               # CM3% of net  # REDACTED
    assert "BAU run-rate" in txt        # cm3_cash labelled as a floor
    # Zero line items are omitted:
    assert "Channel Margin" not in txt
    assert "Marketplace Commission" not in txt


def test_influencer_pl_block_shows_nonzero_marketplace_line():
    from app.responder import build_influencer_pl_block
    from app.models import InfluencerPL
    pl = InfluencerPL(gross=0.0, trade_discount=0.0,  # REDACTED
                      marketplace_commission=0.0, returns=0.0,  # REDACTED
                      net_revenue=0.0, cogs=0.0, commission=0.0,  # REDACTED
                      marketing=0.0, warranty=0.0, cm3_per_unit=0.0,  # REDACTED
                      cm3_pct=0.0, estimated_units=0.0, cm3_cash=0.0,  # REDACTED
                      grade="C")
    txt = build_influencer_pl_block(pl)
    assert "Marketplace Commission" in txt   # non-zero -> shown


def test_influencer_pl_block_none_returns_empty():
    assert build_influencer_pl_block(None) == ""


def test_influencer_eval_response_has_no_retail_pl_section():
    """An influencer PromoEvaluation must render the influencer CM3 block and
    NOT the retail 'Could not estimate volume' / empty P&L waterfall."""
    from app.responder import build_evaluation_blocks
    from app.models import PromoEvaluation, InfluencerPL
    from app.parser import PromoRequest as PR
    ev = PromoEvaluation(
        verdict="APPROVE", grade="A",
        commentary="Influencer promo — [REDACTED]% discount, [REDACTED]% commission.",
        influencer_pl=InfluencerPL(gross=0.0, cogs=0.0,  # REDACTED
                                   net_revenue=0.0, cm3_per_unit=0.0,  # REDACTED
                                   cm3_pct=0.0, estimated_units=0.0,  # REDACTED
                                   cm3_cash=0.0, grade="A"),  # REDACTED
    )
    promo = PR(channel="Influencer", region="Global", discount_pct=0.0)  # REDACTED
    blocks = build_evaluation_blocks(promo, ev)
    text = str(blocks).lower()
    assert "could not estimate volume" not in text
    assert "cm3" in text and "0.0" in text  # REDACTED


def test_influencer_risk_flags_surface_in_blocks():
    """Spec F: COMMERCIALS_FROM_DEFAULT flag detail must appear in Slack output."""
    from app.responder import build_evaluation_blocks
    from app.models import PromoEvaluation, InfluencerPL, RiskFlag
    from app.parser import PromoRequest as PR

    flag_detail = "Influencer economics are placeholder defaults — populate the 'Influencer commercials' tab for an accurate read."
    ev = PromoEvaluation(
        verdict="CONDITIONAL", grade="B",
        commentary="Influencer promo — [REDACTED]% discount.",
        influencer_pl=InfluencerPL(gross=0.0, cogs=0.0,  # REDACTED
                                   net_revenue=0.0, cm3_per_unit=0.0,  # REDACTED
                                   cm3_pct=0.0, estimated_units=0.0,
                                   cm3_cash=0.0, grade="B"),
        risk_flags=[RiskFlag("COMMERCIALS_FROM_DEFAULT", flag_detail)],
    )
    promo = PR(channel="Influencer", region="Global", discount_pct=0.0)  # REDACTED
    blocks = build_evaluation_blocks(promo, ev)
    full_text = str(blocks)
    assert "placeholder defaults" in full_text


def test_influencer_grade_b_header_matches_verdict():
    """Grade B influencer eval = APPROVE; the header must not read 'CONDITIONAL'
    (the retail grade label) and contradict the verdict line."""
    from app.responder import build_evaluation_blocks
    from app.models import PromoEvaluation, InfluencerPL
    from app.parser import PromoRequest as PR

    ev = PromoEvaluation(
        verdict="APPROVE", grade="B",
        commentary="Influencer promo — [REDACTED]% discount, [REDACTED]% commission.",
        influencer_pl=InfluencerPL(gross=0.0, cogs=0.0,  # REDACTED
                                   net_revenue=0.0, cm3_per_unit=0.0,  # REDACTED
                                   cm3_pct=0.0, estimated_units=0.0,  # REDACTED
                                   cm3_cash=0.0, grade="B"),  # REDACTED
    )
    promo = PR(channel="Influencer", region="Global", discount_pct=0.0)  # REDACTED
    blocks = build_evaluation_blocks(promo, ev)
    header = next(b for b in blocks if b.get("type") == "header")
    assert header["text"]["text"] == "Grade B: APPROVE"
    assert "CONDITIONAL" not in header["text"]["text"]


def test_influencer_no_risk_flags_no_warning():
    """When risk_flags=[], no warning flag detail appears in the blocks."""
    from app.responder import build_evaluation_blocks
    from app.models import PromoEvaluation, InfluencerPL
    from app.parser import PromoRequest as PR

    ev = PromoEvaluation(
        verdict="APPROVE", grade="A",
        commentary="Influencer promo — [REDACTED]% discount, [REDACTED]% commission.",
        influencer_pl=InfluencerPL(gross=0.0, cogs=0.0,  # REDACTED
                                   net_revenue=0.0, cm3_per_unit=0.0,  # REDACTED
                                   cm3_pct=0.0, estimated_units=0.0,  # REDACTED
                                   cm3_cash=0.0, grade="A"),  # REDACTED
        risk_flags=[],
    )
    promo = PR(channel="Influencer", region="Global", discount_pct=0.0)  # REDACTED
    blocks = build_evaluation_blocks(promo, ev)
    full_text = str(blocks)
    # The flag detail text must not appear when there are no flags
    assert "placeholder defaults" not in full_text
    assert "populate the" not in full_text


def test_parse_influencer_commercials_reads_new_columns():
    from app.sheets import _parse_influencer_commercials
    rows = [{"Retail price ($USD)": "$0", "COGS": "$0",  # REDACTED
             "Return Rate": "0%", "Commission": "0%", "Warranty": "0%",  # REDACTED
             "Marketing": "0%", "Channel Margin": "0%",  # REDACTED
             "Marketplace Commission": "0%", "Tax": "0%",  # REDACTED
             "Payment Gateway": "0%"}]  # REDACTED
    c = _parse_influencer_commercials(rows)
    assert c.channel_margin_pct == 0.0  # REDACTED
    assert c.marketplace_commission_pct == 0.0  # REDACTED
    assert c.tax_pct == 0.0  # REDACTED
    assert c.payment_gateway_pct == 0.0  # REDACTED


def test_parse_influencer_commercials_new_columns_default_zero():
    from app.sheets import _parse_influencer_commercials
    rows = [{"Retail price ($USD)": "$0", "COGS": "$0",  # REDACTED
             "Return Rate": "0%", "Commission": "0%", "Warranty": "0%",  # REDACTED
             "Marketing": "0%"}]  # REDACTED
    c = _parse_influencer_commercials(rows)
    assert c.channel_margin_pct == 0.0
    assert c.marketplace_commission_pct == 0.0
    assert c.tax_pct == 0.0
    assert c.payment_gateway_pct == 0.0


def test_extract_commission_from_context():
    from app.parser import _extract_commission_from_context
    assert _extract_commission_from_context("commission 15%") == 15.0
    assert _extract_commission_from_context("15% commission") == 15.0
    assert _extract_commission_from_context("commission: 12%") == 12.0
    assert _extract_commission_from_context("commission of 12") == 12.0
    assert _extract_commission_from_context("12% comm on this one") == 12.0
    assert _extract_commission_from_context("just a normal note") == 0.0
    assert _extract_commission_from_context("") == 0.0
    # A discount % in the same text must NOT be picked up as commission:
    assert _extract_commission_from_context(
        "30% off storewide, influencer commission 18%") == 18.0
    # comm must be a whole word, not a substring of recommend/accommodate
    assert _extract_commission_from_context("recommended 15% payout") == 0.0
    assert _extract_commission_from_context("accommodate a 12% bump") == 0.0
    # nearest-match: a stray number BEFORE the keyword must not win
    assert _extract_commission_from_context("30 units, commission 15%") == 15.0


def test_workflow_message_commission_from_context():
    from app.parser import parse_workflow_message
    msg = ("Channel\nInfluencer\nRegion\nGlobal\nDiscount %\n0\n"  # REDACTED
           "Additional Context\nhigher payout this campaign, commission 0%")  # REDACTED
    req = parse_workflow_message(msg)
    assert req is not None
    assert req.commission_pct == 0.0  # REDACTED


def test_evaluate_promo_influencer_uses_bau_volume(monkeypatch):
    import app.evaluator as E
    from app.parser import PromoRequest
    from app.models import PromoHistory, MarketEconomics, InfluencerCommercials
    monkeypatch.setattr(E, "get_influencer_commercials",
                        lambda: InfluencerCommercials(source="sheet"))
    monkeypatch.setattr(E, "get_influencer_bau", lambda: (0.0, 12, "live"))  # REDACTED
    monkeypatch.setattr(E, "get_influencer_volume_estimate", lambda: (0.0, 3))  # REDACTED
    req = PromoRequest(channel="Influencer", region="Global",
                       discount_pct=0.0, commission_pct=0.0,  # REDACTED
                       dates=["2026-06-01", "2026-06-15"])  # 2-week window
    res = E.evaluate_promo(req, PromoHistory(), MarketEconomics(market="Global"), None)
    # BAU weekly rate x 2 weeks — from BAU, not the past-promos value  # REDACTED
    assert res.influencer_pl.estimated_units == 0.0  # REDACTED
    assert "past promo" not in res.commentary
    assert "BAU" in res.commentary


def test_evaluate_promo_influencer_bau_unavailable_falls_back(monkeypatch):
    import app.evaluator as E
    from app.parser import PromoRequest
    from app.models import PromoHistory, MarketEconomics, InfluencerCommercials
    monkeypatch.setattr(E, "get_influencer_commercials",
                        lambda: InfluencerCommercials(source="sheet"))
    monkeypatch.setattr(E, "get_influencer_bau", lambda: (0.0, 0, "unavailable"))
    monkeypatch.setattr(E, "get_influencer_volume_estimate", lambda: (0.0, 3))  # REDACTED
    req = PromoRequest(channel="Influencer", region="Global",
                       discount_pct=0.0, commission_pct=0.0)  # REDACTED
    res = E.evaluate_promo(req, PromoHistory(), MarketEconomics(market="Global"), None)
    assert res.influencer_pl.estimated_units == 0.0  # REDACTED — past-promos fallback
    assert "past promo" in res.commentary


def test_global_regions_normalize():
    from app.regions import normalize_region
    assert normalize_region("Global") == "Global"
    assert normalize_region("global") == "Global"
    assert normalize_region("Global (Excl USA)") == "Global (Excl USA)"
    assert normalize_region("global excl usa") == "Global (Excl USA)"
    assert normalize_region("global ex-usa") == "Global (Excl USA)"


def test_influencer_routes_regardless_of_marketing_scope(monkeypatch):
    import app.evaluator as E
    from app.parser import PromoRequest
    from app.models import PromoHistory, MarketEconomics, InfluencerCommercials
    monkeypatch.setattr(E, "get_influencer_commercials",
                        lambda: InfluencerCommercials(source="sheet"))
    monkeypatch.setattr(E, "get_influencer_bau", lambda: (0.0, 0, "unavailable"))
    monkeypatch.setattr(E, "get_influencer_volume_estimate", lambda: (0.0, 0))
    req = PromoRequest(channel="Influencer", region="Global",
                       discount_pct=0.0, marketing_scope_label="Retail")  # REDACTED
    res = E.evaluate_promo(req, PromoHistory(), MarketEconomics(market="Global"), None)
    assert res.influencer_pl is not None   # routed to influencer despite scope
