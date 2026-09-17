"""Tests for the calendar + actuals reply classifiers."""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
os.environ.setdefault("SLACK_BOT_TOKEN", "test")
os.environ.setdefault("SLACK_SIGNING_SECRET", "test")
os.environ.setdefault("GOOGLE_SERVICE_ACCOUNT_JSON", "{}")
os.environ.setdefault("PROMO_SHEET_ID", "test")
os.environ.setdefault("PL_SHEET_ID", "test")
os.environ.setdefault("SELLOUT_SHEET_ID", "test")

from app.tracker import classify_calendar_reply


def test_done_phrases_route_to_bucket_a():
    for text in [
        "done",
        "logged",
        "added it",
        "posted",
        "updated the calendar",
        "it's there now",
        "live now",
        "confirmed in calendar",
        "synced",
    ]:
        assert classify_calendar_reply(text) == "A", f"{text!r} should be A"


def test_exempt_phrases_route_to_bucket_b():
    for text in [
        "won't be on calendar",
        "not applicable",
        "n/a for this one",
        "skip — internal only",
        "not for calendar",
        "internal only",
        "one-off promo",
        "BAU, no calendar",
        "regular promo not for calendar",
        "not tracking this",
        "exempt",
        "no need to log",
        "this is a B2B event, not a retailer",
        "Welltech isn't a Retailer btw, it was a B2B event",
        "event sponsorship — not a retailer",
        "b2b only",
        # Nordmed (Sweden) on 2026-04-23 — PersonL's reply
        "This is only one influencer directing audience to the retailer so need not be updated on promo calendar as it does not impact pricing",
        "need not be tracked",
        "not needed for calendar",
        "doesn't impact pricing",
        # 2026-05-14 — PersonL's Rheinshop/Alderon reply phrasings
        "This is only marketing so does not impact promos",
        "only marketing — not on calendar",
        "marketing only, no promo",
        "does not impact promos",
        "doesn't impact promos this round",
        "no impact on promos",
    ]:
        assert classify_calendar_reply(text) == "B", f"{text!r} should be B"


def test_unrelated_replies_route_to_bucket_c():
    for text in [
        "looking into it",
        "tomorrow",
        "let me check",
        "who owns this?",
        "@john can you take this?",
        "thanks",
    ]:
        assert classify_calendar_reply(text) == "C", f"{text!r} should be C"


def test_empty_reply_routes_to_c():
    assert classify_calendar_reply("") == "C"
    assert classify_calendar_reply(None) == "C"


from datetime import date, timedelta


def test_snooze_parses_explicit_date():
    from app.tracker import parse_actuals_snooze
    today = date(2026, 5, 13)
    assert parse_actuals_snooze("data on 2026-06-01", today=today) == date(2026, 6, 1)


def test_snooze_parses_relative_duration_weeks():
    from app.tracker import parse_actuals_snooze
    today = date(2026, 5, 13)
    assert parse_actuals_snooze("give me 2 weeks", today=today) == today + timedelta(weeks=2)


def test_snooze_parses_relative_duration_days():
    from app.tracker import parse_actuals_snooze
    today = date(2026, 5, 13)
    assert parse_actuals_snooze("in 5 days", today=today) == today + timedelta(days=5)


def test_snooze_parses_weekday():
    from app.tracker import parse_actuals_snooze
    # 2026-05-13 is a Wednesday. "by Friday" → 2026-05-15.
    today = date(2026, 5, 13)
    result = parse_actuals_snooze("data by Friday", today=today)
    assert result is not None and result.weekday() == 4   # Friday
    assert (result - today).days <= 7


def test_snooze_returns_none_for_unrelated_text():
    from app.tracker import parse_actuals_snooze
    today = date(2026, 5, 13)
    assert parse_actuals_snooze("looks great", today=today) is None
    assert parse_actuals_snooze("thanks", today=today) is None


def test_snooze_default_for_vague_acknowledgment():
    """Vague replies like 'working on it' don't parse to a date but should
    return a sentinel (today + 3 days) so caller can default-snooze."""
    from app.tracker import parse_actuals_snooze, DEFAULT_SNOOZE_DAYS
    today = date(2026, 5, 13)
    assert parse_actuals_snooze("working on it", today=today, default_if_vague=True) \
        == today + timedelta(days=DEFAULT_SNOOZE_DAYS)


def test_soundstore_false_positive_no_longer_exempt():
    """Regression: SOUNDSTORE eval discussion ('the event is an exhibition style event')
    must NOT auto-exempt. Bug found in dry-run on 2026-05-14."""
    from app.tracker import classify_calendar_reply
    text = (
        "Format - the event is an exhibition style event with multiple other "
        "brands attending. 3x3m / 3x6m kiosk (shared with Polaroid)."
    )
    assert classify_calendar_reply(text) == "C", (
        f"SOUNDSTORE eval discussion must be C (noop), not B. Got: "
        f"{classify_calendar_reply(text)}"
    )


def test_skip_alone_no_longer_exempt():
    """'skip' as a verb in normal speech must not exempt."""
    from app.tracker import classify_calendar_reply
    for text in [
        "skip the discount this round",
        "let's skip ahead to the numbers",
        "we'd skip Friday delivery",
    ]:
        assert classify_calendar_reply(text) == "C", (
            f"{text!r} should be C, got {classify_calendar_reply(text)}"
        )


def test_b2b_alone_no_longer_exempt():
    """'b2b' alone is too broad — must require anchor phrase."""
    from app.tracker import classify_calendar_reply
    # Plain 'b2b' alone shouldn't trigger; specific phrasing should
    assert classify_calendar_reply("interesting b2b play here") == "C"
    # Anchored phrases still work
    assert classify_calendar_reply("this is b2b only") == "B"
    assert classify_calendar_reply("Welltech is a b2b event") == "B"


def test_event_alone_no_longer_exempt():
    """'event' alone is too broad — must require anchor phrase."""
    from app.tracker import classify_calendar_reply
    assert classify_calendar_reply("did you go to the event?") == "C"
    assert classify_calendar_reply("the event is an exhibition") == "C"
    # Anchored phrases still work
    assert classify_calendar_reply("this is an event sponsorship") == "B"
    assert classify_calendar_reply("it was an event, not a retailer push") == "B"


def test_bau_alone_no_longer_exempt():
    """'bau' (Business As Usual) appears in eval baselines like 'Feb as BAU'.
    Regression: SOUNDSTORE dry-run on 2026-05-14 wrongly auto-exempted because
    of 'assuming Feb as BAU' in a sizing reply."""
    from app.tracker import classify_calendar_reply
    for text in [
        "236 units are needed to breakeven - assuming Feb as BAU",
        "comparing against BAU baseline",
        "BAU sales were 50/week",
        "let's use Q1 as BAU for the projection",
    ]:
        assert classify_calendar_reply(text) == "C", (
            f"{text!r} should be C (eval discussion), got {classify_calendar_reply(text)}"
        )
    # Anchored phrases still mark exempt
    assert classify_calendar_reply("This is BAU, no calendar entry needed") == "B"
    assert classify_calendar_reply("this is bau") == "B"
