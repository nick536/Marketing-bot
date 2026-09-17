import os, sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
os.environ.setdefault("SLACK_BOT_TOKEN", "test")
os.environ.setdefault("SLACK_SIGNING_SECRET", "test")
os.environ.setdefault("GOOGLE_SERVICE_ACCOUNT_JSON", "{}")
os.environ.setdefault("PROMO_SHEET_ID", "test")
os.environ.setdefault("PL_SHEET_ID", "test")
os.environ.setdefault("SELLOUT_SHEET_ID", "test")

import app.influencer_history as IH


def test_get_influencer_bau_averages_weeks(monkeypatch):
    IH.get_influencer_bau.cache_clear()
    monkeypatch.setattr(IH, "DATA_API_TOKEN", "tok")
    monkeypatch.setattr(IH, "_execute_sql", lambda sql: [
        {"WK": "2026-03-02", "UNITS": 0},  # REDACTED
        {"WK": "2026-03-09", "UNITS": 0},  # REDACTED
        {"WK": "2026-03-16", "UNITS": 0},  # REDACTED
    ])
    upw, n, src = IH.get_influencer_bau()
    assert upw == 0.0  # REDACTED
    assert n == 3
    assert src == "live"


def test_get_influencer_bau_no_token(monkeypatch):
    IH.get_influencer_bau.cache_clear()
    monkeypatch.setattr(IH, "DATA_API_TOKEN", "")
    upw, n, src = IH.get_influencer_bau()
    assert (upw, n, src) == (0.0, 0, "unavailable")


def test_get_influencer_bau_query_error(monkeypatch):
    IH.get_influencer_bau.cache_clear()
    monkeypatch.setattr(IH, "DATA_API_TOKEN", "tok")
    def _boom(sql):
        raise RuntimeError("data warehouse down")
    monkeypatch.setattr(IH, "_execute_sql", _boom)
    assert IH.get_influencer_bau() == (0.0, 0, "unavailable")
