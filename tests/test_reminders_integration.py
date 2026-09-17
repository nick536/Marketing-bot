"""End-to-end cron simulation — mocks sheets + Slack, asserts the right
reminders fire for a hand-crafted set of approved promos."""
import os
import sys
from datetime import date, timedelta
from unittest.mock import MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
os.environ.setdefault("SLACK_BOT_TOKEN", "test")
os.environ.setdefault("SLACK_SIGNING_SECRET", "test")
os.environ.setdefault("GOOGLE_SERVICE_ACCOUNT_JSON", "{}")
os.environ.setdefault("PROMO_SHEET_ID", "test")
os.environ.setdefault("PL_SHEET_ID", "test")
os.environ.setdefault("SELLOUT_SHEET_ID", "test")


def _make_calendar_values():
    """Synthetic calendar with one entry: Kiwilink NZ in May 2026.

    Layout mirrors the real Promotions Calendar 2026 Summary tab:
    - Row 1: column headers (Product, Australia, New Zealand, …)
    - Sentinel "May 2026" row marks the start of the month section.
    - Inside the section, a "Date" row plus follow-up Day/Name/Retail rows
      give the calendar hit + a non-empty follow-up so the bot trusts it.
    """
    return [
        ["", "Product", "Australia", "New Zealand"],
        ["April 2026", "", "", ""],
        ["May 2026", "", "", ""],
        ["Date", "", "", "30th April - 10th May"],
        ["Day", "", "", "Thursday - Sunday"],
        ["Name", "", "", "Mothers Day"],
        ["Retail", "", "", "0% off"],  # REDACTED
    ]


def _make_promo(thread_ts, retailer, region, start, end, status="Approved",
                actual_units="", logged_in_cal_at="", marketing_type="Discount",
                predicted_units=0, submitter="PersonF", channel="Slack",  # REDACTED
                is_completed=None):
    """Build a Promo Tracker row dict for tests.

    is_completed defaults to "Y" when end date is a past ISO string, "N"
    when future. Pass explicit value to override (incl. "" to test the
    not-completed gate). This mirrors the user-maintained sheet formula
    =IF(B="","",IF(K<TODAY(),"Y","N")).
    """
    if is_completed is None:
        from datetime import date as _date
        try:
            ed = _date.fromisoformat(end[:10]) if end else None
            is_completed = "Y" if (ed and ed < _date.today()) else "N"
        except (ValueError, TypeError):
            is_completed = ""
    return {
        "Thread TS": thread_ts,
        "Retailer": retailer,
        "Region": region,
        "Start Date": start,
        "End Date": end,
        "Status": status,
        "Actual Units": actual_units,
        "Logged In Calendar At": logged_in_cal_at,
        "Marketing Type": marketing_type,
        "Predicted Units": predicted_units,
        "Submitter": submitter,
        "Channel": channel,
        "Approval Channel ID": "C0_TEST",
        "Approval Thread TS": thread_ts,
        # Sheet header is "Is it completed?" with the ?. Test fixture
        # writes BOTH spellings so the test stays robust if the lookup
        # tolerates either form (Bug 2026-05-15: code was reading the
        # no-? spelling and the gate silently no-op'd every row).
        "Is it completed?": is_completed,
        "Is it completed": is_completed,
    }


def _patch_promo_loader(monkeypatch, tracker, promos):
    """Monkeypatch the loader used by check_and_send_reminders.

    tracker.get_promos_needing_reminders() is the entry point; the
    calendar-reminder helper also calls _mark_logged_in_calendar which
    writes to the sheet — stub that out too so tests don't try to touch
    a real worksheet.
    """
    monkeypatch.setattr(tracker, "get_promos_needing_reminders",
                        lambda *a, **kw: promos)
    monkeypatch.setattr(tracker, "_mark_logged_in_calendar",
                        lambda thread_ts, source="...": True)
    return "get_promos_needing_reminders"


def test_kiwilink_in_calendar_does_not_get_calendar_ping(monkeypatch):
    from app import tracker

    promos = [
        _make_promo(
            thread_ts="1700000000.000001",
            retailer="Kiwilink", region="NZ",
            start="2026-04-30", end="2026-05-10",
        ),
    ]
    _patch_promo_loader(monkeypatch, tracker, promos)

    posted = []
    slack = MagicMock()
    slack.chat_postMessage.side_effect = lambda **kwargs: posted.append(kwargs.get("text", ""))

    cal_values = _make_calendar_values()
    tracker.check_and_send_reminders(slack, calendar_values=cal_values)

    calendar_pings = [m for m in posted if "still not showing up" in m.lower()
                      or "calendar" in m.lower() and "log" in m.lower()]
    assert calendar_pings == [], (
        f"Kiwilink should NOT have received a calendar ping; got: {calendar_pings}"
    )


def test_pos_row_skipped_for_actuals(monkeypatch):
    from app import tracker

    today = date.today()
    promos = [
        _make_promo(
            thread_ts="1700000001.000002",
            retailer="SOUNDSTORE", region="AU",
            start=str(today - timedelta(days=30)),
            end=str(today - timedelta(days=10)),
            marketing_type="POS Investment",
            logged_in_cal_at="(manual)",
        ),
    ]
    _patch_promo_loader(monkeypatch, tracker, promos)

    posted = []
    slack = MagicMock()
    slack.chat_postMessage.side_effect = lambda **kwargs: posted.append(kwargs.get("text", ""))

    tracker.check_and_send_reminders(slack, calendar_values=[])

    # _build_actuals_message produces messages mentioning "promo ended"
    # or "days post-end" — anything that looks like the actuals chase.
    actuals_pings = [m for m in posted if "promo ended" in m.lower()
                     or "actual units" in m.lower()
                     or "post-end" in m.lower()]
    assert actuals_pings == [], (
        f"POS row should not get an actuals ping; got: {actuals_pings}"
    )


def test_actuals_t14_does_not_tag_approver_per_user_override(monkeypatch):
    """2026-05-15 user override: 'Let's not tag Approver till we get the
    logic right.' The T+14/T+15 Approver escalation is disabled — the
    actuals chase tag list contains ONLY the regional POC at every
    T+ value. Inverts the prior test_actuals_t14_adds_approver_to_tag_list.
    """
    # APPROVER_USER_ID is read from app.config at function-call time via
    # `from app.config import APPROVER_USER_ID as _APPROVER_UID`, so the env var
    # must be set BEFORE the module is imported / before the function runs.
    # The cleanest path is to patch the attribute directly on app.config.
    from app import config as _config
    monkeypatch.setattr(_config, "APPROVER_USER_ID", "U_APPROVER_TEST", raising=False)

    from app import tracker

    today = date.today()
    promos = [
        _make_promo(
            thread_ts="1700000002.000003",
            retailer="Kiwilink", region="NZ",
            start=str(today - timedelta(days=40)),
            end=str(today - timedelta(days=15)),  # T+15 used to escalate
            marketing_type="Discount",
            logged_in_cal_at="2026-05-01T00:00Z (manual)",  # skip calendar reminder
        ),
    ]
    _patch_promo_loader(monkeypatch, tracker, promos)

    posted = []
    slack = MagicMock()
    slack.chat_postMessage.side_effect = lambda **kwargs: posted.append(kwargs.get("text", ""))

    tracker.check_and_send_reminders(slack, calendar_values=[])

    # The actuals chase ping MUST still fire (gate is open at T+15).
    actuals_pings = [m for m in posted if "promo ended" in m.lower()]
    assert actuals_pings, (
        f"Expected an actuals chase ping at T+15; got: {posted}"
    )
    # …and Approver MUST NOT appear in any of them.
    assert not any("<@U_APPROVER_TEST>" in m for m in posted), (
        f"Approver tagging is disabled per 2026-05-15 user override; got: {posted}"
    )


def test_anomaly_end_before_start_skips_actuals(monkeypatch):
    """Rheinshop Europe row 21 had form End=2026-04-14 BEFORE Start=2026-05-06
    (submitter typo). Bot must skip rather than ping 'promo ended 31 days
    ago' off the wrong endpoint."""
    from app import tracker
    today = date.today()
    promos = [_make_promo(
        thread_ts="1700000333.444444",
        retailer="Rheinshop", region="Europe",
        start=str(today + timedelta(days=10)),     # future start
        end=str(today - timedelta(days=20)),       # End BEFORE Start — typo
        marketing_type="Discount",
        is_completed="Y",                          # force past gate
        logged_in_cal_at="(manual)",               # skip calendar reminder
    )]
    _patch_promo_loader(monkeypatch, tracker, promos)
    posted = []
    slack = MagicMock()
    slack.chat_postMessage.side_effect = lambda **kw: posted.append(kw.get("text", ""))
    tracker.check_and_send_reminders(slack, calendar_values=[])
    actuals = [m for m in posted if "promo ended" in m.lower()]
    assert actuals == [], (
        f"end<start anomaly must skip actuals chase: {actuals}"
    )


def test_future_promo_does_not_get_actuals_ping(monkeypatch):
    """A promo that hasn't ended yet must NOT receive actuals chase, even if
    the date parser misreads the year. Guards against the 'Nordica AB - 339 days
    ago' bug from 2026-05-14."""
    from app import tracker
    today = date.today()
    promos = [
        _make_promo(
            thread_ts="1700000099.999999",
            retailer="Nordica AB", region="Nordics",
            start=str(today + timedelta(days=15)),  # future start
            end=str(today + timedelta(days=24)),    # future end
            marketing_type="Discount",
            logged_in_cal_at="(manual)",
        ),
    ]
    _patch_promo_loader(monkeypatch, tracker, promos)
    posted = []
    slack = MagicMock()
    slack.chat_postMessage.side_effect = lambda **kw: posted.append(kw.get("text", ""))
    tracker.check_and_send_reminders(slack, calendar_values=[])
    actuals = [m for m in posted if "promo ended" in m.lower()]
    assert actuals == [], f"Future promo should not get actuals ping: {actuals}"


def test_snooze_until_future_skips_ping(monkeypatch):
    from app import tracker

    today = date.today()
    snooze_until = today + timedelta(days=5)
    promos = [
        _make_promo(
            thread_ts="1700000003.000004",
            retailer="Kiwilink", region="NZ",
            start=str(today - timedelta(days=30)),
            end=str(today - timedelta(days=10)),
            marketing_type="Discount",
            logged_in_cal_at="(manual)",
        ),
    ]
    promos[0]["Actuals Snooze Until"] = snooze_until.isoformat()

    _patch_promo_loader(monkeypatch, tracker, promos)

    posted = []
    slack = MagicMock()
    slack.chat_postMessage.side_effect = lambda **kwargs: posted.append(kwargs.get("text", ""))

    tracker.check_and_send_reminders(slack, calendar_values=[])

    actuals_pings = [m for m in posted if "promo ended" in m.lower()
                     or "actual units" in m.lower()
                     or "post-end" in m.lower()]
    assert actuals_pings == [], (
        f"Snoozed row should not get a ping; got: {actuals_pings}"
    )


def test_actuals_chase_skips_same_day_rerun(monkeypatch):
    """The actuals chase has no fixed cadence — it fires every sweep from
    day 7 until Actual Units is filled. If check_and_send_reminders runs
    twice in one day (duplicate CronJob tick, manual /reminders/trigger,
    k8s job retry) an un-guarded chase re-pings every open promo with a
    byte-identical message. Guard: skip when Actuals Chase Sent At already
    carries today's date."""
    from datetime import datetime

    from app import tracker

    today = date.today()
    promos = [
        _make_promo(
            thread_ts="1700000777.000077",
            retailer="Nordmed", region="NZ",
            start=str(today - timedelta(days=25)),
            end=str(today - timedelta(days=11)),
            marketing_type="Discount",
            logged_in_cal_at="(manual)",   # skip calendar reminder
        ),
    ]
    # Simulate a prior same-day chase already recorded on this row.
    promos[0][tracker.ACTUALS_CHASE_COL] = datetime.utcnow().isoformat() + "Z"

    _patch_promo_loader(monkeypatch, tracker, promos)
    monkeypatch.setattr(tracker, "_record_actuals_chased", lambda *a, **kw: None)
    monkeypatch.setattr("app.regional_pocs.get_regional_poc_ids",
                        lambda region: ["U_POC_TEST"])

    posted = []
    slack = MagicMock()
    slack.chat_postMessage.side_effect = lambda **kw: posted.append(kw.get("text", ""))

    tracker.check_and_send_reminders(slack, calendar_values=[])

    actuals_pings = [m for m in posted if "promo ended" in m.lower()]
    assert actuals_pings == [], (
        f"actuals chase fired again on same-day re-run: {actuals_pings}"
    )


def test_actuals_chase_fires_when_not_chased_today(monkeypatch):
    """Sanity: a blank Actuals Chase Sent At column means the chase MUST
    still fire on this sweep. Confirms the same-day idempotency guard did
    not accidentally suppress the daily ping."""
    from app import tracker

    today = date.today()
    promos = [
        _make_promo(
            thread_ts="1700000778.000078",
            retailer="Nordmed", region="NZ",
            start=str(today - timedelta(days=25)),
            end=str(today - timedelta(days=11)),
            marketing_type="Discount",
            logged_in_cal_at="(manual)",
        ),
    ]
    promos[0][tracker.ACTUALS_CHASE_COL] = ""   # never chased

    _patch_promo_loader(monkeypatch, tracker, promos)
    monkeypatch.setattr(tracker, "_record_actuals_chased", lambda *a, **kw: None)
    monkeypatch.setattr("app.regional_pocs.get_regional_poc_ids",
                        lambda region: ["U_POC_TEST"])

    posted = []
    slack = MagicMock()
    slack.chat_postMessage.side_effect = lambda **kw: posted.append(kw.get("text", ""))

    tracker.check_and_send_reminders(slack, calendar_values=[])

    actuals_pings = [m for m in posted if "promo ended" in m.lower()]
    assert actuals_pings, (
        f"actuals chase suppressed despite blank chase column: {posted}"
    )


# ---------- backfill_calendar_reply_scan ----------

def _make_slack_with_replies(reply_texts, bot_user_id="U_BOT", parent_ts="1700000050.000001"):
    """Build a MagicMock Slack client whose conversations_replies returns
    the given human reply texts (one msg per text) plus a parent placeholder.

    chat_postMessage records text into a list returned alongside the client.
    """
    posted = []
    slack = MagicMock()
    slack.auth_test.return_value = {"user_id": bot_user_id}
    msgs = [{"ts": parent_ts, "user": "U_HUMAN", "text": "original eval post"}]
    for i, t in enumerate(reply_texts):
        msgs.append({"ts": f"1700000060.00000{i}", "user": "U_HUMAN", "text": t})
    slack.conversations_replies.return_value = {"messages": msgs}
    slack.chat_postMessage.side_effect = lambda **kw: posted.append(kw.get("text", ""))
    return slack, posted


def test_backfill_exempts_bucket_b_reply(monkeypatch):
    """An old reply like 'need not be updated on promo calendar as it does
    not impact pricing' must mark the row exempt SILENTLY — no Slack post.
    Mirrors the Nordmed (Sweden) 2026-04-23 production symptom; the noisy
    confirmation post was removed 2026-05-14 (see
    test_backfill_calendar_bucket_b_does_not_post_confirmation)."""
    from app import tracker

    today = date.today()
    promos = [
        _make_promo(
            thread_ts="1700000050.000001",
            retailer="Nordmed", region="Sweden",
            start=str(today - timedelta(days=20)),
            end=str(today - timedelta(days=10)),
            logged_in_cal_at="",  # never marked
        ),
    ]
    monkeypatch.setattr(tracker, "get_promos_needing_reminders",
                        lambda *a, **kw: promos)
    marks = []
    monkeypatch.setattr(
        tracker, "_mark_logged_in_calendar",
        lambda thread_ts, source="...": marks.append((thread_ts, source)) or True,
    )

    slack, posted = _make_slack_with_replies(
        ["This is only one influencer directing audience to the retailer "
         "so need not be updated on promo calendar as it does not impact pricing"],
        parent_ts="1700000050.000001",
    )

    summary = tracker.backfill_calendar_reply_scan(slack, calendar_values=[])

    assert summary["exempted"] == 1, summary
    # Silent: no Slack post for backfill bucket-B.
    assert posted == [], f"Backfill must be silent; got posts: {posted}"
    assert marks and marks[0][0] == "1700000050.000001"
    assert "NA - reason:" in marks[0][1]


def test_actuals_reply_backfill_logs_personj_actuals_zero(monkeypatch):
    """Regression: PersonJ replied 'actuals 0' on Welltech thread before
    main.py was patched. Today's cron must backfill that into the sheet
    via the new actuals-reply backfill scanner."""
    from app import tracker
    promos = [_make_promo(
        thread_ts="1700000666.000001",
        retailer="Welltech", region="UK",
        start="2026-04-15", end="2026-04-19",
        marketing_type="Discount",
        actual_units="",  # NOT yet logged
        is_completed="Y",
        logged_in_cal_at="(manual)",
    )]
    _patch_promo_loader(monkeypatch, tracker, promos)

    fake_replies = {
        "messages": [
            {"user": "U_BOT", "text": ":bell: ping...", "ts": "1700000666.000001"},
            {"user": "U_PERSONJ", "text": "actuals 0", "ts": "1700000666.000003"},
        ]
    }
    slack = MagicMock()
    slack.conversations_replies.return_value = fake_replies
    slack.auth_test.return_value = {"user_id": "U_BOT"}

    logged = []
    monkeypatch.setattr(tracker, "log_actuals",
                        lambda thread_ts, units, *args, **kw: (
                            logged.append((thread_ts, units))
                            or {"predicted_units": 0, "actual_units": units,  # REDACTED
                                "predicted_cm3": 0, "actual_cm3": None,
                                "accuracy_pct": None}
                        ))

    summary = tracker.backfill_actuals_reply_scan(slack)
    assert logged, f"PersonJ's 'actuals 0' must be logged; logged={logged}"
    # Specifically the value 0
    assert any(int(u) == 0 for _, u in logged), f"Must log 0 as valid value; got: {logged}"
    assert summary["logged"] == 1, summary


def test_actuals_reply_backfill_skips_already_filled(monkeypatch):
    """Idempotency: rows where Actual Units is already populated MUST be
    skipped entirely — no Slack call, no re-log."""
    from app import tracker
    promos = [_make_promo(
        thread_ts="1700000666.000099",
        retailer="Welltech", region="UK",
        start="2026-04-15", end="2026-04-19",
        marketing_type="Discount",
        actual_units="0",  # already filled  # REDACTED
        is_completed="Y",
        logged_in_cal_at="(manual)",
    )]
    _patch_promo_loader(monkeypatch, tracker, promos)

    slack = MagicMock()
    slack.auth_test.return_value = {"user_id": "U_BOT"}
    logged = []
    monkeypatch.setattr(tracker, "log_actuals",
                        lambda *a, **kw: logged.append(a) or {"actual_units": 0})

    summary = tracker.backfill_actuals_reply_scan(slack)
    assert summary == {"logged": 0, "scanned": 0, "no_match": 0}, summary
    slack.conversations_replies.assert_not_called()
    assert logged == []


def test_backfill_calendar_bucket_b_does_not_post_confirmation(monkeypatch):
    """Backfill scan must mark exempt SILENTLY — no Slack post for bucket-B
    historical replies. Only bucket-A confirms (positive signal). Caught
    on 2026-05-14 — PersonL's Nordmed reply got a noisy 'Backfill' post."""
    from app import tracker

    promos = [_make_promo(
        thread_ts="1700000777.000001",
        retailer="Nordmed", region="Sweden",
        start="2026-04-25", end="2026-05-11",
        marketing_type="Influencer Code",
        is_completed="Y",
    )]
    _patch_promo_loader(monkeypatch, tracker, promos)

    # Mock conversations.replies to return PersonL's exempt phrasing
    fake_replies = {
        "messages": [
            {"user": "U_BOT", "text": ":calendar: ... approved", "ts": "1700000777.000001"},
            {"user": "U_PERSONL", "text": "need not be updated on promo calendar",
             "ts": "1700000777.000003"},
        ]
    }
    posted = []
    slack = MagicMock()
    slack.conversations_replies.return_value = fake_replies
    slack.auth_test.return_value = {"user_id": "U_BOT"}
    slack.chat_postMessage.side_effect = lambda **kw: posted.append(kw.get("text", ""))

    marks = []
    monkeypatch.setattr(tracker, "_mark_logged_in_calendar",
                        lambda thread_ts, source="...": marks.append((thread_ts, source)) or True)

    tracker.backfill_calendar_reply_scan(slack)

    # Must have marked exempt
    assert any("NA - reason" in src for _, src in marks), \
        f"backfill should mark exempt; marks={marks}"
    # Must NOT have posted any backfill confirmation
    backfill_posts = [m for m in posted if "Backfill" in m or "exempt from calendar" in m]
    assert backfill_posts == [], f"backfill should be silent; got: {backfill_posts}"


def test_backfill_skips_already_marked_rows(monkeypatch):
    """Idempotency: if Logged In Calendar At is already populated, the
    backfill must skip entirely — no Slack call, no re-post."""
    from app import tracker

    today = date.today()
    promos = [
        _make_promo(
            thread_ts="1700000051.000002",
            retailer="Nordmed", region="Sweden",
            start=str(today - timedelta(days=20)),
            end=str(today - timedelta(days=10)),
            logged_in_cal_at="2026-04-24T10:00Z (NA - reason: foo)",
        ),
    ]
    monkeypatch.setattr(tracker, "get_promos_needing_reminders",
                        lambda *a, **kw: promos)
    monkeypatch.setattr(tracker, "_mark_logged_in_calendar",
                        lambda thread_ts, source="...": True)

    slack, posted = _make_slack_with_replies(["need not be updated"])
    summary = tracker.backfill_calendar_reply_scan(slack, calendar_values=[])

    assert summary == {"exempted": 0, "confirmed": 0, "noop": 0}, summary
    assert posted == [], f"Already-marked row must not get re-posted: {posted}"
    slack.conversations_replies.assert_not_called()


def test_backfill_swallows_slack_errors(monkeypatch):
    """conversations_replies failures (network, missing scope) must NOT
    crash the cron — log + skip the row."""
    from app import tracker
    from slack_sdk.errors import SlackApiError

    today = date.today()
    promos = [
        _make_promo(
            thread_ts="1700000052.000003",
            retailer="Nordmed", region="Sweden",
            start=str(today - timedelta(days=20)),
            end=str(today - timedelta(days=10)),
        ),
    ]
    monkeypatch.setattr(tracker, "get_promos_needing_reminders",
                        lambda *a, **kw: promos)
    monkeypatch.setattr(tracker, "_mark_logged_in_calendar",
                        lambda thread_ts, source="...": True)

    slack = MagicMock()
    slack.auth_test.return_value = {"user_id": "U_BOT"}
    slack.conversations_replies.side_effect = SlackApiError(
        "missing_scope", {"error": "missing_scope"},
    )

    # Must not raise.
    summary = tracker.backfill_calendar_reply_scan(slack, calendar_values=[])
    assert summary == {"exempted": 0, "confirmed": 0, "noop": 0}, summary


def test_backfill_ignores_bot_replies(monkeypatch):
    """If the only replies in the thread are bot's own pings, classify
    nothing — bucket B must NOT trigger from the bot reading itself."""
    from app import tracker

    today = date.today()
    promos = [
        _make_promo(
            thread_ts="1700000053.000004",
            retailer="Nordmed", region="Sweden",
            start=str(today - timedelta(days=20)),
            end=str(today - timedelta(days=10)),
        ),
    ]
    monkeypatch.setattr(tracker, "get_promos_needing_reminders",
                        lambda *a, **kw: promos)
    monkeypatch.setattr(tracker, "_mark_logged_in_calendar",
                        lambda thread_ts, source="...": True)

    parent_ts = "1700000053.000004"
    slack = MagicMock()
    slack.auth_test.return_value = {"user_id": "U_BOT"}
    slack.conversations_replies.return_value = {"messages": [
        {"ts": parent_ts, "user": "U_HUMAN", "text": "original"},
        {"ts": "1700000054.000005", "user": "U_BOT",
         "text": "no need to log — internal", "bot_id": "B_BOT"},
    ]}
    posted = []
    slack.chat_postMessage.side_effect = lambda **kw: posted.append(kw.get("text", ""))

    summary = tracker.backfill_calendar_reply_scan(slack, calendar_values=[])
    assert summary == {"exempted": 0, "confirmed": 0, "noop": 1}, summary
    assert posted == []


def test_backfill_ignores_bot_calendar_reply(monkeypatch):
    """Backfill scan must not classify bot's own ':calendar:' reply as a
    human bucket-A signal. Caused CAL_VERIFY false positives in dry-run."""
    from app import tracker
    bot_replies = [
        {"user": "U_BOT", "text": ":calendar: <@U..> — _Kiwilink (NZ) [REDACTED]_ promo was approved (Apr 30–May 10)"},
        {"user": "U_BOT", "text": ":bell: <@U..> — Savannamart promo ended 5 days ago"},
        {"app_id": "A123", "text": ":stopwatch: T-7 ping"},
    ]
    bot_uid = "U_BOT"
    for msg in bot_replies:
        assert tracker._is_bot_reply(msg, bot_uid), (
            f"Bot reply not detected: {msg}"
        )
    # Real human reply should NOT be filtered
    human = {"user": "U_PERSONA", "text": "looks good, approved on calendar"}
    assert not tracker._is_bot_reply(human, bot_uid)


def test_actuals_with_no_year_end_date_does_not_misfire(monkeypatch):
    """End date stored as 'Jun 7' (no year) must be interpreted as current
    year — not walked back to 2025 with 339-day-old result."""
    from app import tracker
    today = date.today()
    promos = [
        _make_promo(
            thread_ts="1700000999.999998",
            retailer="Savannamart", region="ZA",
            start="May 29",
            end="Jun 7",  # no year! should assume current year
            marketing_type="Discount",
            logged_in_cal_at="(manual)",
        ),
    ]
    _patch_promo_loader(monkeypatch, tracker, promos)
    posted = []
    slack = MagicMock()
    slack.chat_postMessage.side_effect = lambda **kw: posted.append(kw.get("text", ""))
    tracker.check_and_send_reminders(slack, calendar_values=[])
    actuals = [m for m in posted if "promo ended" in m.lower()]
    assert actuals == [], f"Future-this-year promo should not get actuals ping: {actuals}"


def test_actuals_skipped_when_is_completed_is_N(monkeypatch):
    """Sheet column 'Is it completed' = N → skip actuals chase entirely.
    User-maintained formula: =IF(B="","",IF(K<TODAY(),"Y","N"))."""
    from app import tracker
    today = date.today()
    promos = [
        _make_promo(
            thread_ts="1700000888.111111",
            retailer="Nedermart", region="NL",
            start=str(today - timedelta(days=20)),
            end=str(today + timedelta(days=10)),  # future end
            marketing_type="Discount",
            logged_in_cal_at="(manual)",
            is_completed="N",
        ),
    ]
    _patch_promo_loader(monkeypatch, tracker, promos)
    posted = []
    slack = MagicMock()
    slack.chat_postMessage.side_effect = lambda **kw: posted.append(kw.get("text", ""))
    tracker.check_and_send_reminders(slack, calendar_values=[])
    actuals = [m for m in posted if "promo ended" in m.lower()]
    assert actuals == [], f"is_completed=N must skip actuals: {actuals}"


def test_actuals_skipped_when_is_completed_is_blank(monkeypatch):
    """Blank 'Is it completed' (formula returns "" because Retailer empty)
    must NOT trigger actuals chase."""
    from app import tracker
    today = date.today()
    promos = [
        _make_promo(
            thread_ts="1700000888.222222",
            retailer="Kiwilink", region="NZ",
            start=str(today - timedelta(days=30)),
            end=str(today - timedelta(days=10)),
            marketing_type="Discount",
            logged_in_cal_at="(manual)",
            is_completed="",
        ),
    ]
    _patch_promo_loader(monkeypatch, tracker, promos)
    posted = []
    slack = MagicMock()
    slack.chat_postMessage.side_effect = lambda **kw: posted.append(kw.get("text", ""))
    tracker.check_and_send_reminders(slack, calendar_values=[])
    actuals = [m for m in posted if "promo ended" in m.lower()]
    assert actuals == [], f"blank is_completed must skip actuals: {actuals}"


def test_actuals_fires_when_is_completed_Y_and_past_t7(monkeypatch):
    """Sanity: when column says Y and end is past T+7, ping fires."""
    from app import tracker
    today = date.today()
    promos = [
        _make_promo(
            thread_ts="1700000888.333333",
            retailer="Kiwilink", region="NZ",
            start=str(today - timedelta(days=30)),
            end=str(today - timedelta(days=10)),  # T+10
            marketing_type="Discount",
            logged_in_cal_at="(manual)",
            is_completed="Y",
        ),
    ]
    _patch_promo_loader(monkeypatch, tracker, promos)
    posted = []
    slack = MagicMock()
    slack.chat_postMessage.side_effect = lambda **kw: posted.append(kw.get("text", ""))
    tracker.check_and_send_reminders(slack, calendar_values=[])
    actuals = [m for m in posted if "promo ended" in m.lower()]
    assert len(actuals) == 1, f"is_completed=Y past T+7 should ping once: {actuals}"


def test_actuals_skipped_when_completed_Y_but_end_unparseable(monkeypatch):
    """Defensive: if formula returns Y but End Date is text/garbage that
    won't parse, log warning and SKIP — don't ping with bogus 'X days ago'.
    Caught a real production class of bug (text-format dates in column K)."""
    from app import tracker
    promos = [
        _make_promo(
            thread_ts="1700000888.444444",
            retailer="ALPENHAUS", region="Switzerland",
            start="Apr 24",
            end="TBD or whatever",  # will not parse
            marketing_type="Discount",
            logged_in_cal_at="(manual)",
            is_completed="Y",
        ),
    ]
    _patch_promo_loader(monkeypatch, tracker, promos)
    posted = []
    slack = MagicMock()
    slack.chat_postMessage.side_effect = lambda **kw: posted.append(kw.get("text", ""))
    tracker.check_and_send_reminders(slack, calendar_values=[])
    actuals = [m for m in posted if "promo ended" in m.lower()]
    assert actuals == [], (
        f"unparseable end date must skip even when is_completed=Y: {actuals}"
    )
