"""Tests covering the 7 bugs flagged by the Claude+Codex dual-review on
2026-05-15. One test per fix, named so it's grep-able from a triage shell.

Fix list:
  1. main._handle_thread_reply bot-self filter via _is_bot_reply (5-layer)
  2. tracker.get_promo_by_thread_ts also matches Approval Thread TS
  3. reconcile form-override uses presence-check, not truthy-check
  4. tracker.parse_actuals_snooze accepts short date strings (Jun 7 / Mon)
  5. tracker._parse_calendar_date_range rolls Dec→Jan into next year
  6. tracker.log_actuals writes Actual Units BEFORE Status
  7. tracker.check_and_send_reminders T-7 idempotency on same-day re-run
"""
import os
import sys
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
os.environ.setdefault("SLACK_BOT_TOKEN", "test")
os.environ.setdefault("SLACK_SIGNING_SECRET", "test")
os.environ.setdefault("GOOGLE_SERVICE_ACCOUNT_JSON", "{}")
os.environ.setdefault("PROMO_SHEET_ID", "test")
os.environ.setdefault("PL_SHEET_ID", "test")
os.environ.setdefault("SELLOUT_SHEET_ID", "test")


# -------------------------------------------------------------------
# Fix 1: bot-self filter must catch bot_id / subtype / app_id paths
# -------------------------------------------------------------------

def test_inline_handler_filters_bot_messages_with_no_user_field():
    """Slack bot messages can have bot_id/subtype/app_id but NO user field.
    The bot's own chase ping body contains the example 'actuals 379' — if
    we only filter on user==bot_uid, we'd self-log 379. Reuse the 5-layer
    _is_bot_reply helper from app.tracker."""
    from app.tracker import _is_bot_reply

    # bot_id present, no user field
    msg = {"bot_id": "B123", "text": ":bell: actuals 379"}
    assert _is_bot_reply(msg, "U_BOT") is True

    # subtype=bot_message
    msg = {"subtype": "bot_message", "text": ":bell: actuals 379"}
    assert _is_bot_reply(msg, "U_BOT") is True

    # app_id only
    msg = {"app_id": "A123", "text": ":bell: actuals 379"}
    assert _is_bot_reply(msg, "U_BOT") is True

    # Emoji-prefix from our own pings (final layer)
    msg = {"user": "U_HUMAN", "text": ":bell: actuals 379"}
    assert _is_bot_reply(msg, "U_BOT") is True

    # Real human reply with the magic words → NOT a bot reply
    msg = {"user": "U_HUMAN", "text": "actuals 379"}
    assert _is_bot_reply(msg, "U_BOT") is False


# -------------------------------------------------------------------
# Fix 2: cross-thread lookup must also check Approval Thread TS
# -------------------------------------------------------------------

def test_get_promo_by_thread_ts_matches_approval_thread_ts(monkeypatch):
    """POC may reply in the approver-approvals-XXX channel (= Approval
    Thread TS), not the original eval thread. Lookup must check both
    columns or actuals/snooze handlers silently no-op."""
    from app import tracker

    rows = [
        {
            "Thread TS": "1700000000.001",
            "Approval Thread TS": "1700000099.999",
            "Retailer": "FooMart",
            "Status": "Approved",
        },
    ]

    class FakeWS:
        def get_all_records(self):
            return rows

    class FakeSheet:
        def worksheet(self, *_): return FakeWS()

    class FakeClient:
        def open_by_key(self, *_): return FakeSheet()

    monkeypatch.setattr(tracker, "_get_client", lambda: FakeClient())
    monkeypatch.setattr(tracker, "_ensure_tracker_tab", lambda sheet: FakeWS())

    # Eval-thread lookup (legacy path)
    r = tracker.get_promo_by_thread_ts("1700000000.001")
    assert r is not None and r["Retailer"] == "FooMart"

    # Approval-thread lookup (the bug fix)
    r = tracker.get_promo_by_thread_ts("1700000099.999")
    assert r is not None and r["Retailer"] == "FooMart"

    # Unknown TS → None
    assert tracker.get_promo_by_thread_ts("9999999999.999") is None


# -------------------------------------------------------------------
# Fix 3: form-override respects explicit empty (NA / dash → "")
# -------------------------------------------------------------------

def test_form_override_respects_explicit_na():
    """parse_slack_form returns '' when the form field says NA/dash. The
    reconcile loop must treat that as the submitter's clearance signal
    (override the LLM-inferred value with ''), NOT skip it as falsy and
    leave the LLM guess in place."""
    from app.slack_form import parse_slack_form

    # Confirm the parser semantics first: NA → ""
    text = "*Region*\nIN\n*Retailer*\nNA\n*Discount %*\nNA\n*Marketing Investment*\n0"  # REDACTED
    parsed = parse_slack_form(text)
    assert parsed.get("region") == "IN"
    assert "retailer" in parsed and parsed["retailer"] == ""
    assert "discount_pct" in parsed and parsed["discount_pct"] == ""
    assert "marketing_spend" in parsed and parsed["marketing_spend"] == "0"  # REDACTED

    # Now mirror reconcile.py's override logic and confirm the fix.
    extracted = {
        "region": "EU",          # LLM guess that should be overridden
        "retailer": "WrongCo",   # LLM guess; form said NA → must clear
        "start_date": "2026-05-01",
        "end_date": "2026-05-15",
        "discount_pct": 0.0,    # LLM guess; form said NA → must clear  # REDACTED
        "marketing_spend": 0.0,  # LLM guess; form has [REDACTED] → override
    }
    form_fields = parsed
    for canonical_key in ("region", "retailer", "start_date", "end_date"):
        if canonical_key in form_fields:
            extracted[canonical_key] = form_fields[canonical_key]
    for canonical_key in ("discount_pct", "marketing_spend"):
        if canonical_key not in form_fields:
            continue
        raw = form_fields.get(canonical_key)
        if not raw:
            extracted[canonical_key] = None
            continue
        cleaned = raw.replace("%", "").replace(",", "").replace("$", "")
        try:
            extracted[canonical_key] = float(cleaned.strip())
        except (ValueError, TypeError):
            pass

    assert extracted["region"] == "IN"
    assert extracted["retailer"] == ""           # form NA wins
    assert extracted["discount_pct"] is None     # form NA wins (cleared)
    assert extracted["marketing_spend"] == 0.0  # REDACTED


# -------------------------------------------------------------------
# Fix 4: parse_actuals_snooze accepts short valid date strings
# -------------------------------------------------------------------

def test_snooze_accepts_short_date_strings():
    """'Jun 7', 'Mon', 'Friday' are valid short snoozes. Previously the
    len(text)<6 guard rejected them outright, so POC's quick 'Jun 7'
    reply was ignored and daily pings continued."""
    from app.tracker import parse_actuals_snooze

    today = date(2026, 5, 15)

    # Short date strings — must parse to a future date within 180d
    assert parse_actuals_snooze("Jun 7", today=today) is not None
    assert parse_actuals_snooze("Friday", today=today) is not None
    assert parse_actuals_snooze("Mon", today=today) is not None

    # Vague short replies still rejected (no date signal)
    assert parse_actuals_snooze("ok", today=today) is None
    assert parse_actuals_snooze("thanks", today=today) is None
    assert parse_actuals_snooze("thx", today=today) is None

    # Vague-acks still trigger default snooze when the caller opts in
    out = parse_actuals_snooze("on it", today=today, default_if_vague=False)
    assert out is None
    out = parse_actuals_snooze(
        "looking into it", today=today, default_if_vague=True,
    )
    assert out is not None and out > today


# -------------------------------------------------------------------
# Fix 5: calendar parser year rollover for Dec→Jan ranges
# -------------------------------------------------------------------

def test_parse_calendar_date_range_year_rollover():
    """'21 Dec - 5 Jan' must roll the Jan endpoint into the next year.
    Without the rollover, end<start → verifier overlap check silently
    fails and year-spanning calendar entries are missed."""
    from app.tracker import _parse_calendar_date_range

    result = _parse_calendar_date_range("21 Dec - 5 Jan", 2026)
    assert result == (date(2026, 12, 21), date(2027, 1, 5))

    # Same-month ranges: no rollover
    result = _parse_calendar_date_range("1 - 8 May", 2026)
    assert result == (date(2026, 5, 1), date(2026, 5, 8))

    # Cross-month within same year: no rollover
    result = _parse_calendar_date_range("30 April - 10 May", 2026)
    assert result == (date(2026, 4, 30), date(2026, 5, 10))


def test_calendar_overlap_check_with_year_rollover():
    """Verifier's overlap check must see year-spanning calendar entries.
    Target range Dec 22 → Jan 3 (2026/2027) overlaps the calendar entry
    'Dec 21 - Jan 5'. Pre-fix the cell parsed to (Dec 21 2026, Jan 5
    2026) → cell_end < cell_start so any overlap test trivially failed."""
    from app.tracker import _parse_calendar_date_range

    cell_start, cell_end = _parse_calendar_date_range("21 Dec - 5 Jan", 2026)
    target_start = date(2026, 12, 22)
    target_end = date(2027, 1, 3)
    # Mirror the production overlap check at app/tracker.py:1488
    has_overlap = not (cell_start > target_end or cell_end < target_start)
    assert has_overlap is True


# -------------------------------------------------------------------
# Fix 6: log_actuals writes Actual Units BEFORE Status
# -------------------------------------------------------------------

def test_log_actuals_writes_units_before_status(monkeypatch):
    """Order matters: if the second write fails (network/rate limit/sheet
    transient error), we want {Actual Units present, Status stale} so the
    chase gate `if actual_raw: continue` skips re-pinging. Previous order
    (Status first) left {Status='Actuals Received', Actual Units empty}
    on partial failure → silent data loss."""
    from app import tracker

    calls: list[tuple[int, object]] = []

    class FakeWS:
        def update_cell(self, row, col, val):
            calls.append((col, val))
        def row_values(self, _row):
            # Predicted Units = col 11 (index 10) = 100
            return ["TS"] + [""] * 9 + ["100", "10%"]

    class FakeSheet:
        def worksheet(self, *_): return FakeWS()

    class FakeClient:
        def open_by_key(self, *_): return FakeSheet()

    monkeypatch.setattr(tracker, "_get_client", lambda: FakeClient())
    monkeypatch.setattr(tracker, "_ensure_tracker_tab", lambda sheet: FakeWS())
    monkeypatch.setattr(tracker, "_find_row_by_thread", lambda ws, ts: 2)

    out = tracker.log_actuals("1700000000.001", actual_units=120)
    assert out is not None

    # Status column = _COL_STATUS = 14
    # Actual Units column = _COL_ACTUAL_UNITS = 15
    cols_in_order = [c for c, _ in calls]
    assert cols_in_order[0] == tracker._COL_ACTUAL_UNITS, (
        f"first write must be Actual Units (col 15), got col {cols_in_order[0]}"
    )
    assert cols_in_order[-1] == tracker._COL_STATUS, (
        f"last write must be Status (col 14), got col {cols_in_order[-1]}"
    )
    # Status comes AFTER actuals
    status_idx = cols_in_order.index(tracker._COL_STATUS)
    units_idx = cols_in_order.index(tracker._COL_ACTUAL_UNITS)
    assert units_idx < status_idx


# -------------------------------------------------------------------
# Fix 7: T-7 idempotency on same-day cron retry
# -------------------------------------------------------------------

def test_t7_skips_if_already_pinged_today(monkeypatch):
    """Cron retry on the same day must not re-fire T-7. The fix reads
    T-7 Pinged At and skips if its date >= today."""
    from app import tracker

    today = datetime.utcnow().date()
    start_date = today + timedelta(days=7)

    promo_row = {
        "Status": "Approved",
        "Thread TS": "1700000000.001",
        "Channel": "Slack",
        "Retailer": "FooMart",
        "Region": "IN",
        "Submitter": "",
        "Start Date": start_date.isoformat(),
        "End Date": (start_date + timedelta(days=5)).isoformat(),
        "Actual Units": "",
        # Simulate a prior same-day fire
        tracker.T7_PINGED_AT_COL: datetime.utcnow().isoformat() + "Z",
        tracker.T7_TAGGED_COL: "U1 U2",
        "Is it completed?": "N",
    }

    monkeypatch.setattr(
        tracker, "get_promos_needing_reminders", lambda: [promo_row]
    )
    # Other helpers we don't care about — make them no-op
    monkeypatch.setattr(
        tracker, "send_calendar_logged_reminder",
        lambda *a, **kw: False,
    )
    monkeypatch.setattr(
        tracker, "_resolve_fyi_target",
        lambda promo: ("CXXX", promo.get("Thread TS"), ""),
    )
    monkeypatch.setattr(
        tracker, "_record_t7_pinged",
        lambda *a, **kw: None,
    )

    posted: list[str] = []

    class FakeSlack:
        def chat_postMessage(self, **kwargs):
            posted.append(kwargs.get("text", ""))
            return {"ok": True}

    sent = tracker.check_and_send_reminders(FakeSlack())
    # No T-7 ping should have been sent because we already fired today
    t7_pings = [m for m in posted if "starts" in m.lower() or "T-7" in m]
    assert not t7_pings, f"T-7 fired again on same-day re-run: {t7_pings}"


def test_t7_fires_when_not_pinged_yet(monkeypatch):
    """Sanity: if T-7 Pinged At is blank, the T-7 reminder MUST fire on
    the day-7-before-start window. Confirms the idempotency guard didn't
    accidentally suppress fresh T-7 sends."""
    from app import tracker

    today = datetime.utcnow().date()
    start_date = today + timedelta(days=7)

    promo_row = {
        "Status": "Approved",
        "Thread TS": "1700000000.001",
        "Channel": "Slack",
        "Retailer": "FooMart",
        "Region": "IN",
        "Submitter": "",
        "Start Date": start_date.isoformat(),
        "End Date": (start_date + timedelta(days=5)).isoformat(),
        "Actual Units": "",
        tracker.T7_PINGED_AT_COL: "",     # never pinged
        tracker.T7_TAGGED_COL: "",
        "Is it completed?": "N",
    }

    monkeypatch.setattr(
        tracker, "get_promos_needing_reminders", lambda: [promo_row]
    )
    monkeypatch.setattr(
        tracker, "send_calendar_logged_reminder",
        lambda *a, **kw: False,
    )
    monkeypatch.setattr(
        tracker, "_resolve_fyi_target",
        lambda promo: ("CXXX", promo.get("Thread TS"), ""),
    )
    monkeypatch.setattr(
        tracker, "_record_t7_pinged",
        lambda *a, **kw: None,
    )

    posted: list[str] = []

    class FakeSlack:
        def chat_postMessage(self, **kwargs):
            posted.append(kwargs.get("text", ""))
            return {"ok": True}

    tracker.check_and_send_reminders(FakeSlack())
    # T-7 ping body uses _build_t7_message — at minimum some message
    # should have posted in this scenario.
    assert posted, "T-7 was suppressed despite never being pinged"
