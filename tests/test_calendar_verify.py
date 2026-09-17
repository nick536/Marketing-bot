"""Tests for app/tracker.py calendar-verification logic."""
import os
import sys
from datetime import date

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
os.environ.setdefault("SLACK_BOT_TOKEN", "test")
os.environ.setdefault("SLACK_SIGNING_SECRET", "test")
os.environ.setdefault("GOOGLE_SERVICE_ACCOUNT_JSON", "{}")
os.environ.setdefault("PROMO_SHEET_ID", "test")
os.environ.setdefault("PL_SHEET_ID", "test")
os.environ.setdefault("SELLOUT_SHEET_ID", "test")

from app.tracker import _CALENDAR_REGION_TO_COUNTRY_HEADERS, _parse_calendar_date_range


# Every column header that appears in the Promotions Calendar 2026 Summary
# tab row 1. If you add a country to that calendar, add it here too.
CALENDAR_HEADERS = [
    "USA", "Canada", "Czechia", "Romania", "Slovakia", "Hungary",
    "Italy", "Germany", "Austria", "Poland", "UK", "BeNeLux",
    "Switzerland", "Spain", "Spain (Bulkclub ES/FR)", "France",
    "Sweden", "Norway", "Denmark", "Finland", "Iceland", "South Africa",
    "UAE", "KSA", "Kuwait", "Qatar", "Bahrain", "India",
    "Hong Kong", "Taiwan", "Thailand", "Singapore", "Malaysia",
    "Japan", "Philippines", "Australia", "New Zealand",
]


def test_every_calendar_column_routed_by_at_least_one_region():
    """For every country column in the calendar, at least one Region value
    in the map should target it. Otherwise that column is unreachable.
    Prevents future calendar additions from being silently invisible."""
    all_targeted = set()
    for headers in _CALENDAR_REGION_TO_COUNTRY_HEADERS.values():
        all_targeted.update(headers)
    missing = [h for h in CALENDAR_HEADERS if h not in all_targeted]
    assert not missing, f"Calendar columns with no region routing: {missing}"


def test_israel_and_mexico_are_explicitly_empty():
    """Calendar doesn't track Israel or Mexico. These regions must map to
    [] so the bot auto-exempts them rather than silently scanning nothing."""
    assert _CALENDAR_REGION_TO_COUNTRY_HEADERS.get("Israel") == []
    assert _CALENDAR_REGION_TO_COUNTRY_HEADERS.get("Mexico") == []


# --- Multi-format date parser regression coverage ---

def test_parse_ordinal_dates():
    assert _parse_calendar_date_range("30th April - 10th May", 2026) == (date(2026, 4, 30), date(2026, 5, 10))


def test_parse_dash_only_one_month():
    assert _parse_calendar_date_range("1 - 8 May", 2026) == (date(2026, 5, 1), date(2026, 5, 8))


def test_parse_em_dash():
    assert _parse_calendar_date_range("May 1–12", 2026) == (date(2026, 5, 1), date(2026, 5, 12))


def test_parse_returns_none_for_unparseable():
    assert _parse_calendar_date_range("TBD", 2026) is None
    assert _parse_calendar_date_range("", 2026) is None
    assert _parse_calendar_date_range("Q2 2026", 2026) is None


def test_months_in_range_covers_single_month():
    from app.tracker import _months_in_range
    assert _months_in_range(date(2026, 5, 1), date(2026, 5, 15)) == [(2026, 5)]


def test_months_in_range_covers_two_months():
    from app.tracker import _months_in_range
    assert _months_in_range(date(2026, 4, 30), date(2026, 5, 10)) == [(2026, 4), (2026, 5)]


def test_months_in_range_covers_three_months():
    from app.tracker import _months_in_range
    assert _months_in_range(date(2026, 4, 28), date(2026, 6, 5)) == [
        (2026, 4), (2026, 5), (2026, 6)
    ]


def test_months_in_range_handles_year_boundary():
    from app.tracker import _months_in_range
    assert _months_in_range(date(2026, 12, 28), date(2027, 1, 5)) == [
        (2026, 12), (2027, 1)
    ]


def test_is_logged_in_calendar_finds_kiwilink_across_apr_may(monkeypatch):
    """Kiwilink (NZ) Apr 30 - May 10 should be detected in the calendar even
    though the date range spans two months. Pre-fix this returned False
    because only the start-month was scanned."""
    from app import tracker

    fake_calendar = [
        # Header row
        ["", "Product", "USA", "Australia", "New Zealand"],
        # noise
        ["Retail Price", "", "$0", "$0", "$0"],  # REDACTED
        # April 2026 section
        ["April 2026", "", "", "", ""],
        ["Date",   "", "", "", ""],
        ["Day",    "", "", "", ""],
        ["Name",   "", "", "", ""],
        ["Retail", "", "", "", ""],
        # May 2026 section — Kiwilink entry IS here
        ["May 2026", "", "", "", ""],
        ["Date",   "", "", "", "30th April - 10th May"],
        ["Day",    "", "", "", "Thursday - Sunday"],
        ["Name",   "", "", "", "Mothers Day"],
        ["Retail", "", "", "", "20% off"],
    ]

    class FakeWS:
        def get_all_values(self):
            return fake_calendar

    class FakeSheet:
        def worksheet(self, _name):
            return FakeWS()

    class FakeClient:
        def open_by_key(self, _key):
            return FakeSheet()

    monkeypatch.setattr(tracker, "_get_client", lambda: FakeClient())

    # Call the verifier with the Kiwilink dates in the production ISO format
    # (the tracker stores Start Date/End Date as ISO YYYY-MM-DD via
    # _to_iso_date). This exercises both the multi-month section walk and
    # the ISO date-parsing path used in production.
    result = tracker._is_logged_in_calendar(
        retailer="Kiwilink",
        region="NZ",
        start_str="2026-04-30",
        end_str="2026-05-10",
    )
    assert result is True, "Kiwilink (NZ) Apr 30-May 10 should be found in the May 2026 section"


def test_is_logged_in_calendar_accepts_iso_dates(monkeypatch):
    """The tracker stores dates as ISO YYYY-MM-DD (see _to_iso_date).
    The verifier MUST accept that format, not silently misparse it.
    Regression test for DMY-vs-ISO bug found 2026-05-13."""
    from app import tracker

    fake_calendar = [
        ["", "Product", "USA", "Australia", "New Zealand"],
        ["Retail Price", "", "$0", "$0", "$0"],  # REDACTED
        ["May 2026", "", "", "", ""],
        ["Date",   "", "", "", "30th April - 10th May"],
        ["Day",    "", "", "", "Thursday - Sunday"],
        ["Name",   "", "", "", "Mothers Day"],
        ["Retail", "", "", "", "20% off"],
    ]

    class FakeWS:
        def get_all_values(self):
            return fake_calendar

    class FakeSheet:
        def worksheet(self, _name):
            return FakeWS()

    class FakeClient:
        def open_by_key(self, _key):
            return FakeSheet()

    monkeypatch.setattr(tracker, "_get_client", lambda: FakeClient())

    # Pass ISO format — this is what the tracker actually stores.
    result = tracker._is_logged_in_calendar(
        retailer="Kiwilink",
        region="NZ",
        start_str="2026-04-30",
        end_str="2026-05-10",
    )
    assert result is True, (
        "ISO YYYY-MM-DD must parse correctly; bug was DMY parse "
        "made '2026-04-30' return None and '2026-05-10' parse as Oct 5."
    )


def test_calendar_reminder_tag_list_includes_regional_poc_not_approver():
    """The reminder must tag PersonN/PersonO/PersonP + the regional POC for
    the row's Region + the submitter. NOT Approver."""
    from app.tracker import _build_calendar_reminder_message

    msg = _build_calendar_reminder_message(
        retailer="Kiwilink",
        region="NZ",
        submitter_uid="U_SUBMITTER",
    )
    # Calendar keepers always
    assert "<@U0REDACT015>" in msg  # PersonN
    assert "<@U0REDACT016>" in msg  # PersonO
    assert "<@U0REDACT017>" in msg  # PersonP
    # NZ → PersonF, PersonG, PersonH
    assert "<@U0REDACT006>" in msg  # PersonF
    assert "<@U0REDACT007>" in msg  # PersonG
    assert "<@U0REDACT008>" in msg  # PersonH
    # Submitter
    assert "<@U_SUBMITTER>" in msg
    # Approver must NOT be tagged
    from app.config import APPROVER_USER_ID
    if APPROVER_USER_ID:
        assert f"<@{APPROVER_USER_ID}>" not in msg
    # New simpler text
    assert "Please log it" in msg
    # Old "Logged In Calendar At" mention dropped
    assert "Logged In Calendar At" not in msg


def test_t7_message_format():
    from app.tracker import _build_t7_message
    msg = _build_t7_message(
        retailer="Kiwilink",
        start_str="Apr 30",
        end_str="May 10",
        mention_uids=["U_KEEPER_A", "U_KEEPER_B", "U_REGIONAL_POC", "U_SUBMITTER"],
    )
    assert "<@U_KEEPER_A>" in msg
    assert "<@U_REGIONAL_POC>" in msg
    assert "starts in 7 days" in msg
    assert "Apr 30" in msg and "May 10" in msg
    assert "scheduled for your channel" in msg
    # Drop the old "(Region)" suffix
    assert "(NZ)" not in msg


def test_t1_skip_when_all_replied(monkeypatch):
    """If everyone tagged at T-7 replied in thread, T-1 should not fire."""
    from app import tracker

    t7_ts = "1700000000.000000"
    tagged = ["U_A", "U_B", "U_C"]

    repliers = {"U_A", "U_B", "U_C"}
    monkeypatch.setattr(tracker, "_get_thread_repliers_since",
                        lambda *a, **kw: repliers)

    silent = tracker._compute_t1_tag_list(tagged, t7_ts, "C0_TEST")
    assert silent == []


def test_t1_fires_for_silent_subset(monkeypatch):
    """Only tag users who didn't reply at T-7."""
    from app import tracker

    t7_ts = "1700000000.000000"
    tagged = ["U_A", "U_B", "U_C", "U_D"]
    repliers = {"U_B", "U_D"}
    monkeypatch.setattr(tracker, "_get_thread_repliers_since",
                        lambda *a, **kw: repliers)

    silent = tracker._compute_t1_tag_list(tagged, t7_ts, "C0_TEST")
    assert sorted(silent) == ["U_A", "U_C"]


def test_t1_message_format():
    from app.tracker import _build_t1_message
    msg = _build_t1_message(
        retailer="Kiwilink",
        start_str="Apr 30",
        end_str="May 10",
        mention_uids=["U_A"],
    )
    assert "starts tomorrow" in msg
    assert "<@U_A>" in msg
    assert "Apr 30" in msg and "May 10" in msg


def test_actuals_message_tags_regional_poc_not_submitter():
    from app.tracker import _build_actuals_message
    msg = _build_actuals_message(
        retailer="Kiwilink", region="NZ", days_since_end=8,
        predicted_units=0, mention_uids=["U_PERSONF", "U_PERSONG", "U_PERSONH"],  # REDACTED
    )
    assert "<@U_PERSONF>" in msg
    assert "<@U_PERSONG>" in msg
    assert "<@U_PERSONH>" in msg
    assert "ended 8 days ago" in msg
    assert "actuals 379" in msg or "`actuals" in msg
    assert "Predicted: 0 units" in msg  # REDACTED


def test_parse_input_date_assumes_current_year_for_ambiguous():
    """Ambiguous dates with no year (e.g. 'Jun 7', '7-Jun') must assume
    current year (2026), not walk back to 2025. Bug: actuals chase pinged
    'promo ended 341 days ago' on Savannamart ZA whose end date stored without year."""
    from app.tracker import _parse_input_date
    from datetime import date
    today = date.today()
    current_year = today.year

    # Ambiguous dates without year — should always be current year
    for s in ["Jun 7", "7-Jun", "7 Jun", "June 7", "07 Jun"]:
        result = _parse_input_date(s)
        assert result is not None, f"{s!r} should parse"
        assert result.year == current_year, (
            f"{s!r} parsed to {result} — should default to current year {current_year}"
        )


def test_thread_ts_link_uses_app_redirect_when_team_id_set(monkeypatch):
    """Thread links must use Slack's app_redirect URL so clicks from Google
    Sheets open the desktop app instead of the browser. Bug: legacy
    /archives/ permalinks bounced through the browser unreliably."""
    from app import tracker
    monkeypatch.setattr("app.config.SLACK_TEAM_ID", "T00000000")
    # Force re-read of SLACK_TEAM_ID inside the function by reimporting if needed
    result = tracker._thread_ts_link("1773701297.512019", channel_id="C0REDACT003")
    assert "slack.com/app_redirect" in result, f"expected app_redirect URL, got: {result}"
    assert "team=T00000000" in result
    assert "channel=C0REDACT003" in result
    assert "message_ts=1773701297.512019" in result
    # Should still be wrapped as HYPERLINK with the raw ts as display text
    assert result.startswith("=HYPERLINK(")
    assert '"1773701297.512019"' in result


def test_thread_ts_link_handles_empty_or_wa_prefix():
    from app.tracker import _thread_ts_link
    assert _thread_ts_link("") == ""
    assert _thread_ts_link("WA-12345") == "WA-12345"


# --- _should_chase_actuals skip list (2026-05-14 expansion) ---

def test_should_chase_actuals_chases_promoter_trial():
    """2026-05-15 user override: Promoter Trial DOES have unit attribution
    (sales during the trial window). Now CHASED. Inverts the prior
    test_should_chase_actuals_skips_promoter_trial."""
    from app.tracker import _should_chase_actuals
    assert _should_chase_actuals("Promoter Trial") is True
    assert _should_chase_actuals("Promoter Program") is True
    assert _should_chase_actuals("PROMOTER trial") is True  # case-insensitive
    # Sanity: real promo still chased
    assert _should_chase_actuals("Discount") is True


def test_should_chase_actuals_chases_premium_retail_display():
    """2026-05-15 user override: Premium Retail Display/Activation generate
    measurable unit lift via demos. Now CHASED via the
    _ACTUALS_CHASE_OVERRIDES whitelist (which beats the bare 'display'
    substring in the skip list). Inverts the prior
    test_should_chase_actuals_skips_premium_retail_display."""
    from app.tracker import _should_chase_actuals
    assert _should_chase_actuals("Premium Retail Display") is True
    assert _should_chase_actuals("Premium Retail Activation") is True
    # Sanity
    assert _should_chase_actuals("Discount") is True
    # Static POS/Display investments still skip — only the "premium
    # retail" prefix unlocks chase.
    assert _should_chase_actuals("POS Display") is False
    assert _should_chase_actuals("Display Investment") is False


def test_should_chase_actuals_chases_everything_except_pos_display():
    """2026-05-15 FINAL user override: actuals chase skip list narrowed to
    pos/display/displays only. Training, SPA, SBA, SOA, Sales Contest,
    Promoter Trial, Premium Retail, Influencer, KOL, Event Coupon,
    Product Launch — ALL chase. Reasoning: chase ping is cheap; silence
    on actuals is the costly failure mode. POC clarifies in thread if
    no consumer attribution exists."""
    from app.tracker import _should_chase_actuals
    # Skip — pure POS / Display investments
    assert _should_chase_actuals("POS Display") is False
    assert _should_chase_actuals("POS Investment") is False
    assert _should_chase_actuals("Display Investment") is False
    assert _should_chase_actuals("Displays") is False
    # CHASE — everything else (per 2026-05-15 final override)
    assert _should_chase_actuals("Training") is True
    assert _should_chase_actuals("Staff Training") is True
    assert _should_chase_actuals("SPA Campaign") is True
    assert _should_chase_actuals("SPA+SBA Campaign") is True
    assert _should_chase_actuals("SBA Claim") is True
    assert _should_chase_actuals("SOA") is True
    assert _should_chase_actuals("Sales Contest") is True
    assert _should_chase_actuals("Product Launch") is True
    assert _should_chase_actuals("Promoter Trial") is True
    assert _should_chase_actuals("Premium Retail Display") is True
    assert _should_chase_actuals("Influencer Code") is True
    assert _should_chase_actuals("Instagram KOL") is True
    assert _should_chase_actuals("Event Coupon") is True
    assert _should_chase_actuals("Discount") is True


# --- Retailer aliases (fix for Savannamart ZA / SVM mismatch) ---

def test_retailer_aliases_savannamart():
    """Savannamart ZA promo on calendar shows as 'SVM Birthday Promo'.
    Tracker stores 'Savannamart'. Alias map must let SVM match."""
    from app.tracker import _retailer_aliases
    aliases = _retailer_aliases("Savannamart")
    assert "savannamart" in aliases
    assert "svm" in aliases


def test_retailer_aliases_unknown_falls_back_to_lowercase():
    """Retailers not in the alias map fall back to a single lowercase
    token equal to themselves — so existing matching behavior is unchanged."""
    from app.tracker import _retailer_aliases
    assert _retailer_aliases("RandomRetailer") == ["randomretailer"]


def test_should_send_calendar_reminder_positive_list():
    """Calendar reminder uses a POSITIVE whitelist of promo keywords.
    Per 2026-05-14 user rule: 'Promo calendar includes ONLY promos/
    discounts. On approvals group → need not be in promo calendar.'
    Default for non-promo types is SKIP (no ping)."""
    from app.tracker import _should_send_calendar_reminder
    # --- Real promos — must ping ---
    assert _should_send_calendar_reminder("Discount") is True
    assert _should_send_calendar_reminder("Discount + SOA") is True   # WAS wrongly skipped under negative list
    assert _should_send_calendar_reminder("Coupon Code") is True
    assert _should_send_calendar_reminder("Birthday Campaign") is True
    assert _should_send_calendar_reminder("Pre-summer Disc") is True
    assert _should_send_calendar_reminder("Slow Moving Coll") is True
    assert _should_send_calendar_reminder("Stock Clearance") is True
    assert _should_send_calendar_reminder("Promo") is True
    # --- Exclusions — contains a positive keyword but NOT a promo ---
    assert _should_send_calendar_reminder("Event Coupon") is False    # event ticket, not discount
    assert _should_send_calendar_reminder("Promoter Trial") is False  # contains "promo" but staffing
    assert _should_send_calendar_reminder("Sales Contest") is False   # contains "sale" but contest
    # Flyer / Mailer / MVM — distribution channels, not market-wide promos
    assert _should_send_calendar_reminder("Flyer Discount") is False
    assert _should_send_calendar_reminder("MVM (Mailer)") is False
    assert _should_send_calendar_reminder("Mailer") is False
    # --- Pure non-promos — must skip ---
    assert _should_send_calendar_reminder("POS Display") is False
    assert _should_send_calendar_reminder("Training") is False
    assert _should_send_calendar_reminder("Premium Retail Display") is False
    assert _should_send_calendar_reminder("SOA") is False
    assert _should_send_calendar_reminder("SPA+SBA Camp") is False
    assert _should_send_calendar_reminder("Influencer Code") is False
    assert _should_send_calendar_reminder("Instagram KOL") is False
    assert _should_send_calendar_reminder("Product Launch") is False
    # --- Empty → ping (data-quality signal); unknown → skip (default) ---
    assert _should_send_calendar_reminder("") is True
    assert _should_send_calendar_reminder("Unknown") is False


def test_is_logged_in_calendar_skips_wrong_year_dates(monkeypatch):
    """Defensive: if End Date parses to non-current year (e.g. 2025 in
    a 2026 cron run), skip the calendar verify entirely. Otherwise the
    verifier looks for non-existent past-year sections and returns False,
    causing forever-pinging on stale rows. Caught 2026-05-14 with Nordica AB."""
    from app import tracker
    # mock _get_client so verifier doesn't actually try to open Google Sheets
    monkeypatch.setattr(tracker, "_get_client", lambda: None)
    result = tracker._is_logged_in_calendar(
        retailer="Nordica AB", region="Nordics",
        start_str="2025-05-29", end_str="2025-06-07",
    )
    assert result is False, "wrong-year dates must short-circuit to False"


def test_parse_input_date_month_year_only_end_of_month():
    """For end-date inputs, 'Mar 2026' (month + year, no day) should bump
    to last day of the month, not be treated as Mar 1. Caught 2026-05-14:
    Byteport End Date 'Mar 2026' was parsed as Mar 1, making the bot
    say 'ended 74 days ago' instead of the user-intuitive ~44 days."""
    from app.tracker import _parse_input_date
    # End-of-month bump
    assert _parse_input_date("Mar 2026", end_of_month=True) == date(2026, 3, 31)
    assert _parse_input_date("March 2026", end_of_month=True) == date(2026, 3, 31)
    assert _parse_input_date("Feb 2026", end_of_month=True) == date(2026, 2, 28)  # non-leap
    assert _parse_input_date("April 2026", end_of_month=True) == date(2026, 4, 30)


def test_parse_input_date_month_year_only_default_first_day():
    """Default (end_of_month=False) keeps the legacy behavior — month-only
    inputs land on the 1st (start of period). Used for start-date parsing."""
    from app.tracker import _parse_input_date
    assert _parse_input_date("Mar 2026") == date(2026, 3, 1)
    assert _parse_input_date("Mar 2026", end_of_month=False) == date(2026, 3, 1)


def test_parse_input_date_explicit_day_not_bumped():
    """'Mar 7 2026' has an explicit day; end_of_month must not bump it."""
    from app.tracker import _parse_input_date
    assert _parse_input_date("Mar 7 2026", end_of_month=True) == date(2026, 3, 7)
    assert _parse_input_date("7 Mar 2026", end_of_month=True) == date(2026, 3, 7)


def test_parse_input_date_iso_not_bumped():
    """ISO YYYY-MM-DD takes the fast path — never bumped."""
    from app.tracker import _parse_input_date
    assert _parse_input_date("2026-03-15", end_of_month=True) == date(2026, 3, 15)


# --- 2026-05-15 audit: calendar parser + discount % + End<Start + form-MT-LLM ---

# Fix 1: Calendar Date parser tolerates loose hyphenation + day-only ranges
def test_parse_loose_hyphenation():
    """The calendar Date row is hand-typed and submitters use loose
    hyphenation. Parser must tolerate:
      - hyphen between day and month name (ALPENHAUS CH '27-April - 24-May')
      - extra hyphen between day and month (Catalogmart UK '21- May - 2-June')
      - day-only ranges where month comes from section header (Kiwilink NZ
        Easter '1st - 8th')
    """
    from app.tracker import _parse_calendar_date_range
    # ALPENHAUS CH style — hyphen as day-month glue on both endpoints
    assert _parse_calendar_date_range("27-April - 24-May", 2026) == (
        date(2026, 4, 27), date(2026, 5, 24)
    )
    # Catalogmart UK style — extra hyphen + space between day and month name
    assert _parse_calendar_date_range("21- May - 2-June", 2026) == (
        date(2026, 5, 21), date(2026, 6, 2)
    )
    # Kiwilink NZ Easter style — both endpoints bare, month from caller context
    assert _parse_calendar_date_range("1st - 8th", 2026, default_month=4) == (
        date(2026, 4, 1), date(2026, 4, 8)
    )
    # Bare-day range without default_month is still unparseable
    assert _parse_calendar_date_range("1st - 8th", 2026) is None


# Fix 2: discount_pct > 0 fires calendar reminder regardless of MT
def test_should_send_calendar_reminder_fires_on_discount_pct():
    """Per 2026-05-15 user rule: any non-zero discount fires calendar
    even if MT isn't in the promo whitelist. Bulkclub UK MVM (Mailer) at
    Discount %=10 was being skipped under the MT-only check; now it fires."""
    from app.tracker import _should_send_calendar_reminder
    # MVM (Mailer) is in EXCLUSIONS, but a real 10% discount overrides.
    assert _should_send_calendar_reminder("MVM (Mailer)", "10%") is True
    assert _should_send_calendar_reminder("MVM (Mailer)", "10") is True
    assert _should_send_calendar_reminder("MVM (Mailer)", 10) is True
    assert _should_send_calendar_reminder("MVM (Mailer)", 10.0) is True
    # No discount → MT exclusion still wins → skip
    assert _should_send_calendar_reminder("MVM (Mailer)", "0") is False
    assert _should_send_calendar_reminder("MVM (Mailer)", "") is False
    assert _should_send_calendar_reminder("MVM (Mailer)", "NA") is False
    assert _should_send_calendar_reminder("MVM (Mailer)", 0) is False
    # Default arg (no discount provided) still works
    assert _should_send_calendar_reminder("MVM (Mailer)") is False
    # Whitelisted MT with explicit discount: still True (no regression)
    assert _should_send_calendar_reminder("Discount", "20%") is True
    # Garbage discount string falls back to MT-only logic
    assert _should_send_calendar_reminder("MVM (Mailer)", "abc") is False
    assert _should_send_calendar_reminder("Discount", "abc") is True


# Fix 3: End < Start anomaly skips calendar verify
def test_is_logged_in_calendar_skips_end_before_start(monkeypatch):
    """Rheinshop Europe row had form End=2026-04-14 BEFORE Start=2026-05-06.
    Calendar verifier must bail rather than scan an inverted range."""
    from app import tracker
    # Mock client so we don't try to read Sheets even if we got that far.
    monkeypatch.setattr(tracker, "_get_client", lambda: None)
    result = tracker._is_logged_in_calendar(
        retailer="Rheinshop", region="Europe",
        start_str="2026-05-06", end_str="2026-04-14",
    )
    assert result is False, "End<Start must short-circuit to False"


# Fix 4: PR #86 refinement — form-override block must not touch marketing_type
def test_form_override_keeps_llm_marketing_type():
    """LLM enriches MT from Additional Context (e.g. 'Influencer Code'
    over form's generic 'Discount'). The form-override block in
    reconcile.py must NOT clobber LLM's marketing_type.

    Source-level smoke check — the override block must not reference the
    form's marketing_type field."""
    from app import reconcile
    import inspect
    src = inspect.getsource(reconcile.reconcile_missed_approvals)
    # The override loop iterates over a tuple of canonical keys. None of
    # them should be marketing_type.
    # Crude: look for the literal "marketing_type" inside the override block.
    # The override block is fenced by the comment we wrote.
    # If marketing_type appears anywhere in this function it's a regression.
    assert '"marketing_type"' not in src, (
        "reconcile_missed_approvals must NOT override LLM's marketing_type "
        "from the form — LLM enrichment is more accurate (Nordmed case)."
    )
    assert "'marketing_type'" not in src, (
        "reconcile_missed_approvals must NOT override LLM's marketing_type "
        "from the form."
    )
