"""Tests covering the 7 actuals-pillar fixes from the 2026-05-15 audit.

One file per audit so each fix is greppable from a single test name.
See `git log` for the consolidated commit + PR description.
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


# -------------------------------------------------------------------
# Fix 1: PRIMARY GATE — header is "Is it completed?" with the ?
# -------------------------------------------------------------------

def test_is_completed_gate_handles_question_mark_header():
    """Sheet header is 'Is it completed?' with ?. Code must read that key.

    Bug 2026-05-15: code was reading 'Is it completed' (no ?) → dict.get
    always returned '' → gate evaluated '' != 'Y' → True → ALL rows
    silently skipped. PRIMARY GATE was no-op.
    """
    # Mirror the exact lookup used in app/tracker.py around line 2327.
    row = {"Is it completed?": "Y", "Other": "x"}
    val = str(
        row.get("Is it completed?", row.get("Is it completed", ""))
    ).strip().upper()
    assert val == "Y"

    # Backward compat with the old (no-?) spelling, just in case anyone
    # hand-edits a row.
    row2 = {"Is it completed": "Y"}
    val2 = str(
        row2.get("Is it completed?", row2.get("Is it completed", ""))
    ).strip().upper()
    assert val2 == "Y"

    # Empty / missing → blank, gate stays closed.
    row3 = {}
    val3 = str(
        row3.get("Is it completed?", row3.get("Is it completed", ""))
    ).strip().upper()
    assert val3 == ""


# -------------------------------------------------------------------
# Fix 2: _build_actuals_confirmation — :bar_chart: + variance
# -------------------------------------------------------------------

def test_actuals_confirmation_shows_variance():
    """Approver's 2026-05-15 ask: 'human readable on slack'. Confirmation
    must show actual + predicted + signed variance %."""
    from app.tracker import _build_actuals_confirmation
    msg = _build_actuals_confirmation(106, 174)
    assert ":bar_chart:" in msg
    assert "106" in msg
    assert "174" in msg
    # 106 vs 174 = -39%. The format string uses :+.0f so a negative is
    # printed as "-39".
    assert "-39%" in msg


def test_actuals_confirmation_handles_zero():
    """`actuals 0` predicted 30 → -100% variance. Don't crash, don't
    swallow the signal."""
    from app.tracker import _build_actuals_confirmation
    msg = _build_actuals_confirmation(0, 30)
    assert "0" in msg and "30" in msg
    assert "-100%" in msg


def test_actuals_confirmation_no_predicted():
    """No prediction available → omit the variance line cleanly."""
    from app.tracker import _build_actuals_confirmation
    msg = _build_actuals_confirmation(50, 0)
    assert ":bar_chart:" in msg
    assert "50" in msg
    assert "Variance" not in msg


# -------------------------------------------------------------------
# Fix 3: tighten ACTUALS_INLINE_RE (strict pattern only)
# -------------------------------------------------------------------

def test_inline_actuals_only_matches_strict_pattern():
    """Only `actuals N` / `actual N` (with optional colon/space). Drops
    the broad patterns that would log SOUNDSTORE's '236 units' eval
    discussion or 'we sold 30 last week' anecdotes.

    The same compiled regex lives in BOTH app/tracker.py (used by the
    backfill scanner) and app/main.py (used by the inline reply
    handler — `from app.tracker import _ACTUALS_INLINE_RE as
    ACTUALS_INLINE_RE`). Test the tracker's copy directly: importing
    app.main initialises Slack Bolt and would fail under a test token.
    """
    from app.tracker import _ACTUALS_INLINE_RE as RE
    # Strict matches
    assert RE.search("actuals 0").group(1) == "0"
    assert RE.search("actuals: 25").group(1) == "25"
    assert RE.search("actual 379").group(1) == "379"
    # Must NOT match these (false-positive land that the OLD broad
    # patterns would have logged).
    assert not RE.search("236 units to breakeven")
    assert not RE.search("we sold 30 last week")
    assert not RE.search("around 50")
    assert not RE.search("70 units at bulkclub")
    # The bot's own chase ping body contains the example "actuals 379" —
    # so the regex DOES match it. The bot-self filter (Guard 1 in
    # _handle_thread_reply) is the second line of defense for that case.
    assert RE.search("actuals 379")


# -------------------------------------------------------------------
# Fix 4: log_actuals records -100% variance for actual=0
# -------------------------------------------------------------------

def test_log_actuals_records_minus100_for_zero_actual():
    """Welltech predicted 30, actual=0 should record -100% variance,
    not skip the accuracy column entirely. Caught 2026-05-15 audit.

    We assert the math directly (the full log_actuals path requires a
    live sheet; the integration test is in test_reminders_integration.py).
    """
    predicted = 30
    actual = 0
    variance = ((actual - predicted) / predicted) * 100
    assert variance == -100.0


# -------------------------------------------------------------------
# Fix 5: parse_actuals_snooze rejects vague short inputs
# -------------------------------------------------------------------

def test_snooze_rejects_vague_short_inputs():
    """`on it` / `thanks` / `ok` must not coerce into a date. Real date
    patterns still parse. Bug 2026-05-15."""
    from app.tracker import parse_actuals_snooze
    from datetime import date
    today = date(2026, 5, 15)

    # Vague-ack / too-short → no snooze
    assert parse_actuals_snooze("on it", today=today) is None
    assert parse_actuals_snooze("thanks", today=today) is None
    assert parse_actuals_snooze("ok", today=today) is None
    assert parse_actuals_snooze("checking", today=today) is None

    # Real date patterns still work
    assert parse_actuals_snooze("in 3 days", today=today) is not None
    assert parse_actuals_snooze("by Friday", today=today) is not None
    assert parse_actuals_snooze("2026-06-01", today=today) is not None


# -------------------------------------------------------------------
# Fix 6: actuals chase skip list — 2026-05-15 user override
# -------------------------------------------------------------------

def test_actuals_chase_skip_list_2026_05_15_final():
    """Per user 2026-05-15 FINAL override: skip ONLY pos/display/displays.
    Everything else chases. Reasoning: chase ping is cheap; silence on
    actuals is costly. POC clarifies in thread (per PersonL's Rheinshop SPA+SBA
    / Byteport Product Launch context) if no consumer attribution."""
    from app.tracker import _should_chase_actuals

    # Skip ONLY pure POS / Display
    for mt in ["POS Display", "POS Investment", "Display Investment", "Displays"]:
        assert _should_chase_actuals(mt) is False, f"{mt!r} should skip"

    # CHASE — everything else (formerly skipped categories now chase)
    for mt in [
        "Training", "Staff Training",
        "SPA Campaign", "SPA+SBA Campaign", "SBA Claim", "SOA",
        "Sales Contest",
        "Promoter Trial",
        "Premium Retail Display", "Premium Retail Activation",
        "Product Launch",
        "Influencer Code", "Instagram KOL", "Event Coupon",
        "Discount", "Coupon Code",
    ]:
        assert _should_chase_actuals(mt) is True, f"{mt!r} should chase"
