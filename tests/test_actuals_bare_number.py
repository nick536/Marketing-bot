"""Tests for the bare-count actuals reply form added 2026-07-23.

Motivation: on the ALPENHAUS chase, PersonC replied "80" and PersonA replied
"80 units". The strict `actuals N`-only parser ignored both, so the daily
actuals chase re-pinged forever. `parse_actuals_from_reply` now accepts a
bare whole-message count in addition to inline `actuals N`.

The bare form is ANCHORED (^...$): prose numbers must NOT match, or we
regress the 2026-05-15 bug (broad patterns caught "70 units at bulkclub",
"we sold 30 last week", "236 units to breakeven", "around 50").
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
os.environ.setdefault("SLACK_BOT_TOKEN", "test")
os.environ.setdefault("SLACK_SIGNING_SECRET", "test")
os.environ.setdefault("GOOGLE_SERVICE_ACCOUNT_JSON", "{}")
os.environ.setdefault("PROMO_SHEET_ID", "test")
os.environ.setdefault("PL_SHEET_ID", "test")
os.environ.setdefault("SELLOUT_SHEET_ID", "test")


def test_bare_number_forms_parse():
    """The exact replies that were being ignored on ALPENHAUS must now parse."""
    from app.tracker import parse_actuals_from_reply

    assert parse_actuals_from_reply("80") == 80
    assert parse_actuals_from_reply("80 units") == 80
    assert parse_actuals_from_reply("80 unit") == 80
    assert parse_actuals_from_reply("80 rings") == 80
    assert parse_actuals_from_reply("80 pcs") == 80
    assert parse_actuals_from_reply("80 pieces") == 80
    # Whitespace / newlines around the count are fine.
    assert parse_actuals_from_reply("  379  ") == 379
    assert parse_actuals_from_reply("\n80 units\n") == 80
    # Zero is a valid actuals value ("went live, sold nothing").
    assert parse_actuals_from_reply("0") == 0
    assert parse_actuals_from_reply("0 units") == 0


def test_inline_actuals_still_parses():
    """Back-compat: the original `actuals N` form is untouched."""
    from app.tracker import parse_actuals_from_reply

    assert parse_actuals_from_reply("actuals 379") == 379
    assert parse_actuals_from_reply("actuals: 25") == 25
    assert parse_actuals_from_reply("actual 100") == 100
    assert parse_actuals_from_reply("we did actuals 80 last week") == 80


def test_prose_numbers_do_not_parse():
    """The anchored bare form must NOT catch numbers embedded in prose —
    these are exactly the false positives from the 2026-05-15 bug."""
    from app.tracker import parse_actuals_from_reply

    assert parse_actuals_from_reply("70 units at bulkclub") is None
    assert parse_actuals_from_reply("we sold 30 last week") is None
    assert parse_actuals_from_reply("236 units to breakeven") is None
    assert parse_actuals_from_reply("around 50") is None
    assert parse_actuals_from_reply("~50") is None
    assert parse_actuals_from_reply("22% off") is None
    assert parse_actuals_from_reply("80 units sold and 20 returned") is None
    assert parse_actuals_from_reply("") is None
    assert parse_actuals_from_reply("no idea yet") is None


def test_bot_chase_ping_body_does_not_bare_match():
    """The bot's own chase ping contains "actuals 379" inline (still matched
    by design) but is filtered by _is_bot_reply upstream. Here we only assert
    the multi-line ping body is NOT treated as a bare whole-message count."""
    from app.tracker import _ACTUALS_BARE_RE

    ping = "Two ways to log actuals (pick one):\n1. Reply: actuals 379\n"
    assert _ACTUALS_BARE_RE.match(ping) is None
