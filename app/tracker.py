"""Promo Tracker — logs evaluations, approvals, and actuals to a Google Sheet tab."""

import json
import logging
import re
from datetime import date, datetime, timedelta
from typing import Optional

import gspread
from google.oauth2.service_account import Credentials

from app.config import GOOGLE_SERVICE_ACCOUNT_JSON, NO_RESPONSE_DAYS_THRESHOLD, PROMO_SHEET_ID

logger = logging.getLogger(__name__)

SLACK_WORKSPACE_DOMAIN = "example.slack.com"

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
# Renamed from "Promo Tracker" 2026-05-15 — tab now tracks all marketing approvals (promos + investments), not just promos.
TRACKER_TAB = "Marketing Tracker"
HEADERS = [
    "Thread TS",       # 1
    "Channel",         # 2
    "Submitter",       # 3
    "Retailer",        # 4
    "Region",          # 5
    "Marketing Type",  # 6
    "Discount %",      # 7
    "Marketing Spend", # 8
    "Start Date",      # 9
    "End Date",        # 10
    "Predicted Units", # 11
    "Predicted CM3",   # 12
    "Grade",           # 13
    "Status",          # 14
    "Actual Units",    # 15
    "Actual CM3",      # 16
    "Accuracy %",      # 17
    "Decision At",     # 18
    "Posted At",       # 19
]

# Column numbers (1-indexed) — update here if HEADERS ever change
_COL_STATUS = 14
_COL_ACTUAL_UNITS = 15
_COL_ACTUAL_CM3 = 16
_COL_ACCURACY = 17
_COL_DECISION_AT = 18


def _to_iso_date(value) -> str:
    """Coerce a date value to ISO 'YYYY-MM-DD'. Returns '' if unparseable
    or out of the 2020–2100 sanity range (rejects junk like '1901-03-02').

    Accepts datetime/date objects, ISO strings, and short forms ('Apr 24',
    'May 22'). Drops things we can't pin to a real date ('TBD', 'Q2 2026',
    'launch', 'TBD (10+ weeks)') so the reminder cron and downstream
    consumers never have to second-guess.

    Date inference: ambiguous dates without a year (e.g. 'Apr 24', 'May 22')
    always assume the current calendar year — never walk backward. We anchor
    dateparser to Jan 1 of the current year and force-bump if the parser
    still picks a prior year. Same fix as _parse_input_date — both paths
    must agree so a 'Jun 7' end date stored mid-year doesn't get stamped
    as last year and trip the actuals chase ("ended 341 days ago").
    """
    if value is None or value == "":
        return ""
    if isinstance(value, datetime):
        y = value.year
        return value.strftime("%Y-%m-%d") if 2020 <= y <= 2100 else ""
    s = str(value).strip()
    if not s or s == "-":
        return ""
    # Already ISO? Validate the year before passing through.
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return s if 2020 <= int(m.group(1)) <= 2100 else ""
    try:
        import dateparser
    except ImportError:
        return ""
    from datetime import datetime as _dt
    today = date.today()
    current_year = today.year
    relative_base = _dt.combine(date(current_year, 1, 1), _dt.min.time())
    parsed = dateparser.parse(
        s,
        settings={
            "DATE_ORDER": "DMY",
            "STRICT_PARSING": False,
            "PREFER_DATES_FROM": "current_period",
            "RELATIVE_BASE": relative_base,
        },
    )
    if not parsed:
        return ""
    if parsed.year < 2020 or parsed.year > 2100:
        return ""
    # If the input has no explicit 4-digit year and we landed in the past,
    # force-bump to current year (consistency with _parse_input_date).
    has_explicit_year = bool(re.search(r"\b(19|20)\d{2}\b", s))
    if not has_explicit_year and parsed.year < current_year:
        try:
            parsed = parsed.replace(year=current_year)
        except ValueError:
            parsed = parsed.replace(year=current_year, day=28)
    return parsed.strftime("%Y-%m-%d")


def _to_pct_str(value) -> str:
    """Coerce a CM3 / percentage value to clean 'XX.X%' string. Returns ''.

    Strips '$', commas, parens, trailing 'CM3', etc. If the input is plainly
    a dollar absolute (e.g. '[REDACTED]'), returns '' — we don't keep absolute
    dollars in the % column. Numbers > 1 are treated as already-percent
    (34 → '34.0%'); fractions ≤ 1 are scaled (0.34 → '34.0%').
    """
    if value is None or value == "":
        return ""
    if isinstance(value, (int, float)):
        n = float(value)
        if abs(n) <= 1:
            n *= 100
        return f"{round(n, 1)}%"
    s = str(value).strip()
    if not s or s == "-":
        return ""
    # Pull the first number we see; if string starts with '$' and has no '%',
    # it's an absolute dollar — discard.
    has_pct = "%" in s
    has_dollar = "$" in s and not has_pct
    if has_dollar and "(" not in s:
        return ""
    # If we have something like '[REDACTED]', extract the % portion.
    pct_match = re.search(r"-?\d+(?:\.\d+)?\s*%", s)
    if pct_match:
        n = float(pct_match.group().replace("%", "").strip())
        return f"{round(n, 1)}%"
    # Bare number — coerce.
    num_match = re.search(r"-?\d+(?:\.\d+)?", s)
    if not num_match:
        return ""
    n = float(num_match.group())
    if abs(n) <= 1:
        n *= 100
    return f"{round(n, 1)}%"


def _fmt_spend(value, currency: str = "USD") -> str:
    """Format a marketing-spend value as a text-y string for Sheets.

    Why: Sheets with value_input_option=USER_ENTERED coerces bare numbers
    against the column format. The Marketing Spend column is occasionally
    formatted as date in some sheet variants, which turns 391 into
    "1901-03-02". Always emit a string with a currency prefix so Sheets
    cannot coerce.
    """
    if value is None or value == "":
        return ""
    if isinstance(value, (int, float)):
        n = float(value)
        if n == 0:
            return ""
        return f"{currency} {n:,.2f}"
    s = str(value).strip()
    if not s or s in ("-", "0", "0.0", "0.00"):
        return ""
    # If already prefixed with a currency or symbol, pass through.
    if re.search(r"^[A-Z]{3}\s|\$|€|£|EUR|USD|GBP|AED|CAD", s):
        return s
    # Try to parse as bare number and wrap.
    try:
        n = float(s.replace(",", ""))
        if n == 0:
            return ""
        return f"{currency} {n:,.2f}"
    except ValueError:
        return s  # text we don't recognise — keep verbatim


def _to_discount_str(value) -> str:
    """Coerce a discount value to '20%' or '-'. Empty string → '-'."""
    if value is None or value == "":
        return "-"
    if isinstance(value, (int, float)):
        n = float(value)
        if abs(n) <= 1:
            n *= 100
        return f"{int(round(n))}%"
    s = str(value).strip()
    if not s or s == "-":
        return "-"
    # Take first number out of the string.
    num_match = re.search(r"-?\d+(?:\.\d+)?", s)
    if not num_match:
        return "-"
    n = float(num_match.group())
    if abs(n) <= 1:
        n *= 100
    return f"{int(round(n))}%"


def _thread_ts_link(thread_ts: str, channel_id: str = "") -> str:
    """Wrap a Slack thread_ts as a clickable Sheets HYPERLINK.

    Display text stays the raw ts so existing _find_row_by_thread lookups
    keep working (gspread's col_values returns rendered text, not the
    formula). When the channel is unknown we fall back to PROMO_CHANNEL_ID;
    if even that is missing, return the bare ts so the cell still has a
    matchable string.
    """
    ts = (thread_ts or "").strip()
    if not ts or ts.startswith("WA-"):
        return ts
    chan = (channel_id or "").strip()
    if not chan:
        try:
            from app.config import PROMO_CHANNEL_ID as _pcid
            chan = (_pcid or "").strip()
        except Exception:
            chan = ""
    if not chan:
        return ts
    # Use Slack's app_redirect URL — opens the desktop app reliably from
    # Google Sheets clicks. Falls back to the legacy archive permalink if
    # SLACK_TEAM_ID is unset (which would be a misconfiguration).
    from app.config import SLACK_TEAM_ID
    if SLACK_TEAM_ID:
        url = (
            f"https://slack.com/app_redirect?team={SLACK_TEAM_ID}"
            f"&channel={chan}&message_ts={ts}"
        )
    else:
        ts_no_dot = ts.replace(".", "")
        url = f"https://{SLACK_WORKSPACE_DOMAIN}/archives/{chan}/p{ts_no_dot}"
    # Escape any double quotes in the display text (paranoid — Slack ts is
    # always digits + one dot, but be safe for the formula).
    safe_ts = ts.replace('"', '""')
    return f'=HYPERLINK("{url}","{safe_ts}")'


def _get_client() -> gspread.Client:
    raw = GOOGLE_SERVICE_ACCOUNT_JSON
    if not raw or raw == "{}":
        raise ValueError("GOOGLE_SERVICE_ACCOUNT_JSON not set")
    creds = Credentials.from_service_account_info(json.loads(raw), scopes=SCOPES)
    return gspread.authorize(creds)


def _ensure_tracker_tab(sheet: gspread.Spreadsheet) -> gspread.Worksheet:
    """Get or create the Promo Tracker tab with headers."""
    try:
        ws = sheet.worksheet(TRACKER_TAB)
        _ensure_headers_current(ws)
    except gspread.WorksheetNotFound:
        ws = sheet.add_worksheet(title=TRACKER_TAB, rows=500, cols=len(HEADERS))
        ws.append_row(HEADERS, value_input_option="RAW")
        logger.info("Created Promo Tracker tab with headers")
    return ws


def _ensure_headers_current(ws: gspread.Worksheet) -> None:
    """Migrate existing sheet to add Submitter and Marketing Spend columns if missing."""
    current_headers = ws.row_values(1)
    if current_headers == HEADERS:
        return

    if "Submitter" not in current_headers:
        ws.insert_cols([[""]], col=3)
        ws.update_cell(1, 3, "Submitter")
        logger.info("Inserted Submitter column at col 3")
        current_headers = ws.row_values(1)

    if "Marketing Spend" not in current_headers:
        # After Submitter insert, Discount % is col 7, Start Date is col 8
        ws.insert_cols([[""]], col=8)
        ws.update_cell(1, 8, "Marketing Spend")
        logger.info("Inserted Marketing Spend column at col 8")
        current_headers = ws.row_values(1)

    if "Approved At" in current_headers:
        col = current_headers.index("Approved At") + 1
        ws.update_cell(1, col, "Decision At")
        logger.info("Renamed Approved At → Decision At")


def _find_row_by_thread(ws: gspread.Worksheet, thread_ts: str) -> Optional[int]:
    """Find the row number (1-indexed) for a given thread_ts. Returns None if not found."""
    col_values = ws.col_values(1)  # Thread TS is column 1
    for i, val in enumerate(col_values):
        if val == thread_ts:
            return i + 1  # gspread is 1-indexed
    return None


# In-memory cache of recent evaluations, keyed by CMA eval thread_ts.
# Populated by log_pending_promo (now a stash, not a sheet write) and drained
# on approval to append the full row with Status=Approved. Policy: only
# Approver-approved promos land in the Promo Tracker sheet — pending / rejected
# evaluations never create sheet rows.
_PENDING_EVALS: dict[str, dict] = {}
_PENDING_EVALS_MAX = 2000  # cap to bound memory; oldest entries pruned FIFO


# Per 2026-05-15 user override: only POS/Display investments are excluded
# from the Marketing Tracker. Everything else (Training, SPA, SBA, Influencer,
# Promoter Trial, Sales Contest, etc.) IS tracked even though they're not
# promos — Approver needs visibility on all approved marketing spend. Mirror
# of the filter in app/reconcile.py so both write paths agree.
_TRACKER_SKIP_TYPES = {"pos", "display", "displays"}


def _should_track_marketing_type(marketing_type: str) -> bool:
    if not marketing_type:
        return True   # unknown — assume promo, surface as data quality issue
    mt = marketing_type.strip().lower()
    # Match on the FIRST token so "POS Display" / "Display Investment" /
    # "Displays" are skipped, but "Premium Retail Display" still tracks.
    first_token = mt.split()[0] if mt.split() else mt
    return first_token not in _TRACKER_SKIP_TYPES


def log_pending_promo(
    thread_ts: str,
    channel: str,
    promo,  # PromoRequest from parser
    evaluation,  # PromoEvaluation
    submitter: str = "",
    marketing_spend: Optional[float] = None,
) -> None:
    """Stash a fresh evaluation in memory so mark_approved can later append
    it to the Promo Tracker as an Approved row. No sheet write happens here.
    Pending / un-approved promos never clutter the tracker."""
    try:
        dates = promo.dates or []
        channel_label = "WhatsApp" if channel == "WhatsApp" else "Slack"
        marketing_type = (promo.promo_type or "promo").replace("_", " ").title()
        # 2026-05-15 user override: POS/Display investments don't go in the
        # Marketing Tracker. Skip stashing so mark_approved later finds no
        # cached eval and won't write a row either.
        if not _should_track_marketing_type(marketing_type):
            logger.info(
                f"Live flow: skipping tracker stash for thread {thread_ts} — "
                f"Marketing Type {marketing_type!r} is a non-tracked investment type"
            )
            return
        has_discount = promo.discount_pct and promo.discount_pct > 0
        discount_display = promo.discount_pct if has_discount else "-"
        promo_scope = getattr(promo, "promo_scope", "retailer")
        retailer_display = promo.retailer or ""
        if promo_scope == "distributor":
            dist_name = promo.distributor or promo.region or ""
            retailer_display = f"ALL {dist_name}" if dist_name else "ALL"

        if marketing_spend is None:
            try:
                spend_val = evaluation.pl_impact.total_promo_investment
                marketing_spend = round(spend_val, 2) if spend_val else ""
            except AttributeError:
                marketing_spend = ""

        _PENDING_EVALS[thread_ts] = {
            "Thread TS": thread_ts,
            "Channel": channel_label,
            "Submitter": submitter,
            "Retailer": retailer_display,
            "Region": promo.region or "",
            "Marketing Type": marketing_type,
            "Discount %": _to_discount_str(discount_display),
            "Marketing Spend": _fmt_spend(marketing_spend),
            "Start Date": _to_iso_date(dates[0]) if len(dates) > 0 else "",
            "End Date": _to_iso_date(dates[1]) if len(dates) > 1 else "",
            "Predicted Units": evaluation.pl_impact.incremental_units,
            "Predicted CM3": _to_pct_str(evaluation.pl_impact.cm3_cash_pct),
            "Grade": evaluation.grade,
            "Posted At": datetime.utcnow().strftime("%Y-%m-%d %H:%M"),
        }
        # FIFO prune when cap exceeded.
        if len(_PENDING_EVALS) > _PENDING_EVALS_MAX:
            drop = len(_PENDING_EVALS) - _PENDING_EVALS_MAX
            for k in list(_PENDING_EVALS.keys())[:drop]:
                _PENDING_EVALS.pop(k, None)
        logger.info(f"Cached eval for thread {thread_ts} (not yet written to sheet)")
    except Exception as e:
        logger.error(f"Failed to cache pending promo: {e}")


_audit_logger = logging.getLogger("audit")


def _emit_audit(action: str, thread_ts: str, actor_id: str = "") -> None:
    """Emit a structured, append-only audit log line.

    Format: AUDIT action=<action> thread=<ts> actor=<slack_user_id> at=<iso_utc>
    These lines go to the same log stream as other output and should be
    forwarded to an immutable sink (e.g. Render log drain, CloudWatch).
    """
    _audit_logger.info(
        "AUDIT action=%s thread=%s actor=%s at=%s",
        action, thread_ts, actor_id or "unknown",
        datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
    )


def mark_approved(
    thread_ts: str,
    actor_id: str = "",
    approval_channel_id: str = "",
    approval_thread_ts: str = "",
) -> bool:
    """Log an Approved promo to the tracker. Returns True on success.

    Two paths:
      1. Row already exists (e.g. historical / manually seeded): flip Status
         to Approved, stamp Decision At, backfill approval location cols.
      2. No row yet: append a fresh Approved row from the in-memory eval
         cache (_PENDING_EVALS). If cache is empty for this thread (e.g.
         bot restarted between eval and approval), we can't reconstruct —
         log a warning and return False.
    """
    now_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M")
    try:
        client = _get_client()
        sheet = client.open_by_key(PROMO_SHEET_ID)
        ws = _ensure_tracker_tab(sheet)

        row_num = _find_row_by_thread(ws, thread_ts)
        if row_num:
            ws.update_cell(row_num, _COL_STATUS, "Approved")
            ws.update_cell(row_num, _COL_DECISION_AT, now_str)
            # Best-effort backfill of approval-location columns when present.
            try:
                headers = ws.row_values(1)
                if APPROVAL_CHAN_COL in headers and approval_channel_id:
                    ws.update_cell(row_num, headers.index(APPROVAL_CHAN_COL) + 1, approval_channel_id)
                if APPROVAL_TS_COL in headers and approval_thread_ts:
                    ws.update_cell(
                        row_num,
                        headers.index(APPROVAL_TS_COL) + 1,
                        _thread_ts_link(approval_thread_ts, approval_channel_id),
                        value_input_option="USER_ENTERED",
                    )
            except Exception:
                logger.exception("Failed to backfill approval-location cols")
            _emit_audit("approved", thread_ts, actor_id)
            return True

        cached = _PENDING_EVALS.pop(thread_ts, None)
        if not cached:
            logger.warning(
                f"mark_approved: no tracker row and no cached eval for thread {thread_ts} "
                "(bot restarted between eval and approval?) — cannot log."
            )
            return False

        # Build + append a fresh Approved row using the sheet's current header order.
        headers = ws.row_values(1)
        # Ensure extension cols exist so we can write approval location on insert.
        for col in (APPROVAL_CHAN_COL, APPROVAL_TS_COL, CALENDAR_FYI_COL,
                    LOGGED_IN_CAL_COL, T7_PINGED_AT_COL, T7_TAGGED_COL,
                    ACTUALS_SNOOZE_COL):
            if col not in headers:
                _ensure_col(ws, col)
                headers.append(col)

        row_data = {**cached}
        row_data["Status"] = "Approved"
        row_data["Decision At"] = now_str
        # Wrap Thread TS as a clickable hyperlink. Channel for the eval thread
        # is PROMO_CHANNEL_ID (where the bot ran); approval row gets a separate
        # hyperlink keyed off approval_channel_id below.
        row_data["Thread TS"] = _thread_ts_link(thread_ts)
        if approval_channel_id:
            row_data[APPROVAL_CHAN_COL] = approval_channel_id
        if approval_thread_ts:
            row_data[APPROVAL_TS_COL] = _thread_ts_link(
                approval_thread_ts, approval_channel_id
            )

        new_row = [row_data.get(h, "") for h in headers]
        ws.append_row(new_row, value_input_option="USER_ENTERED")
        _emit_audit("approved", thread_ts, actor_id)
        logger.info(f"Appended Approved row for thread {thread_ts} (retailer={row_data.get('Retailer')})")
        return True
    except Exception as e:
        logger.error(f"Failed to mark approved: {e}")
        return False


def append_reconciled_approved(
    thread_ts: str,
    channel_label: str,
    approval_channel_id: str,
    approval_thread_ts: str,
    decision_at: str,
    fields: dict,
) -> bool:
    """Append an Approved row reconstructed from a Slack thread the live flow
    missed (e.g. bot restart between eval and approval). Idempotent: if a row
    with this Thread TS already exists, returns False without writing.

    `fields` matches the LLM extractor schema (retailer, region, marketing_type,
    discount_pct, marketing_spend, start_date, end_date, predicted_units,
    predicted_cm3_pct, grade, submitter).
    """
    try:
        client = _get_client()
        sheet = client.open_by_key(PROMO_SHEET_ID)
        ws = _ensure_tracker_tab(sheet)

        if _find_row_by_thread(ws, thread_ts):
            return False  # already logged
        # Also guard against duplicate rows keyed on Approval Thread TS — when
        # the row exists with a different Thread TS but the same approval ts.
        if approval_thread_ts:
            try:
                headers = ws.row_values(1)
                if APPROVAL_TS_COL in headers:
                    col_idx = headers.index(APPROVAL_TS_COL) + 1
                    for v in ws.col_values(col_idx)[1:]:
                        if v and v.strip() == approval_thread_ts:
                            return False
            except Exception:
                pass

        headers = ws.row_values(1)
        for col in (APPROVAL_CHAN_COL, APPROVAL_TS_COL, CALENDAR_FYI_COL,
                    LOGGED_IN_CAL_COL, T7_PINGED_AT_COL, T7_TAGGED_COL,
                    ACTUALS_SNOOZE_COL):
            if col not in headers:
                _ensure_col(ws, col)
                headers.append(col)

        def _fmt_num(v):
            if v is None or v == "":
                return ""
            return v

        row_data = {
            "Thread TS": _thread_ts_link(thread_ts),
            "Channel": channel_label or "Slack",
            "Submitter": (fields.get("submitter") or "").strip(),
            "Retailer": (fields.get("retailer") or "").strip(),
            "Region": (fields.get("region") or "").strip(),
            "Marketing Type": (fields.get("marketing_type") or "Unknown (reconciled)").strip(),
            "Discount %": _to_discount_str(fields.get("discount_pct")),
            "Marketing Spend": _fmt_spend(fields.get("marketing_spend")),
            "Start Date": _to_iso_date(fields.get("start_date")),
            "End Date": _to_iso_date(fields.get("end_date")),
            "Predicted Units": _fmt_num(fields.get("predicted_units")) or "",
            "Predicted CM3": _to_pct_str(fields.get("predicted_cm3_pct")),
            "Grade": (fields.get("grade") or "").strip(),
            "Status": "Approved",
            "Decision At": decision_at,
            "Posted At": datetime.utcnow().strftime("%Y-%m-%d %H:%M"),
            APPROVAL_CHAN_COL: approval_channel_id or "",
            APPROVAL_TS_COL: _thread_ts_link(approval_thread_ts, approval_channel_id),
        }

        new_row = [row_data.get(h, "") for h in headers]
        ws.append_row(new_row, value_input_option="USER_ENTERED")
        _emit_audit("approved_reconciled", thread_ts, "")
        logger.info(
            f"Reconciled: appended Approved row for {row_data['Retailer']} "
            f"({row_data['Region']}) thread={thread_ts}"
        )
        return True
    except Exception as e:
        logger.error(f"Failed to append reconciled approved row: {e}")
        return False


def mark_rejected(thread_ts: str, actor_id: str = "") -> bool:
    """Drop the cached eval on rejection — rejected promos are not logged.
    If a row already exists in the sheet (backward-compat / historical), flip
    its Status to Rejected so the record is preserved; otherwise no-op."""
    try:
        _PENDING_EVALS.pop(thread_ts, None)
        client = _get_client()
        sheet = client.open_by_key(PROMO_SHEET_ID)
        ws = _ensure_tracker_tab(sheet)
        row_num = _find_row_by_thread(ws, thread_ts)
        if not row_num:
            _emit_audit("rejected_no_row", thread_ts, actor_id)
            return True  # cache cleared is enough
        ws.update_cell(row_num, _COL_STATUS, "Rejected")
        ws.update_cell(row_num, _COL_DECISION_AT, datetime.utcnow().strftime("%Y-%m-%d %H:%M"))
        _emit_audit("rejected", thread_ts, actor_id)
        return True
    except Exception as e:
        logger.error(f"Failed to mark rejected: {e}")
        return False


def mark_no_response(days_threshold: int = NO_RESPONSE_DAYS_THRESHOLD) -> int:
    """Flip Pending promos older than days_threshold to 'No Response'. Returns count updated."""
    try:
        client = _get_client()
        sheet = client.open_by_key(PROMO_SHEET_ID)
        ws = _ensure_tracker_tab(sheet)

        rows = ws.get_all_records()
        cutoff = datetime.utcnow() - timedelta(days=days_threshold)
        updated = 0

        for i, row in enumerate(rows):
            if row.get("Status") != "Pending":
                continue
            posted_raw = row.get("Posted At", "")
            if not posted_raw:
                continue
            try:
                posted_dt = datetime.strptime(str(posted_raw), "%Y-%m-%d %H:%M")
            except ValueError:
                continue
            if posted_dt < cutoff:
                row_num = i + 2  # +1 for header, +1 for 1-indexed
                ws.update_cell(row_num, _COL_STATUS, "No Response")
                updated += 1
                logger.info(f"Marked thread {row.get('Thread TS')} as No Response ({days_threshold}d old)")

        return updated
    except Exception as e:
        logger.error(f"Failed to mark no-response promos: {e}")
        return 0


def log_actuals(thread_ts: str, actual_units: int, actual_cm3: Optional[float] = None) -> Optional[dict]:
    """Log actual results and compute accuracy. Returns summary dict or None."""
    try:
        client = _get_client()
        sheet = client.open_by_key(PROMO_SHEET_ID)
        ws = _ensure_tracker_tab(sheet)

        row_num = _find_row_by_thread(ws, thread_ts)
        if not row_num:
            logger.warning(f"No tracker row for thread {thread_ts}")
            return None

        row = ws.row_values(row_num)
        # Predicted Units = col 11 (index 10), Predicted CM3% = col 12 (index 11)
        predicted_units = int(row[10]) if len(row) > 10 and row[10] else 0
        raw_cm3 = str(row[11]).replace("%", "") if len(row) > 11 and row[11] else ""
        predicted_cm3 = float(raw_cm3) if raw_cm3 else 0.0

        # Variance: signed % deviation of actual vs predicted. Negative =
        # under-delivered, positive = over-delivered. Bug 2026-05-15:
        # previously gated on `actual_units > 0` which silently skipped
        # the accuracy column for `actuals 0` (Welltech UK case).
        # `actuals 0` should record -100% — that IS the signal.
        accuracy = None
        if predicted_units > 0:
            accuracy = round(
                ((actual_units - predicted_units) / predicted_units) * 100, 1
            )

        # Bug 2026-05-15 dual-review: write Actual Units (and the
        # derivative columns) BEFORE Status="Actuals Received". If the
        # second-write fails (network/rate-limit/transient sheet error),
        # we want the row in {Actual Units present, Status stale} — the
        # chase gate `if actual_raw: continue` then skips re-pinging on
        # the next cron, so data is preserved. Previous order (Status
        # first) left the row in {Status="Actuals Received", Actual
        # Units empty} on a partial failure — silent data loss.
        ws.update_cell(row_num, _COL_ACTUAL_UNITS, actual_units)
        if actual_cm3 is not None:
            ws.update_cell(row_num, _COL_ACTUAL_CM3, round(actual_cm3, 2))
        if accuracy is not None:
            ws.update_cell(row_num, _COL_ACCURACY, accuracy)
        ws.update_cell(row_num, _COL_STATUS, "Actuals Received")

        logger.info(f"Logged actuals for thread {thread_ts}: {actual_units} units, accuracy={accuracy}%")
        return {
            "predicted_units": predicted_units,
            "actual_units": actual_units,
            "predicted_cm3": predicted_cm3,
            "actual_cm3": actual_cm3,
            "accuracy_pct": accuracy,
        }
    except Exception as e:
        logger.error(f"Failed to log actuals: {e}")
        return None


def find_promo_by_thread(thread_ts: str) -> Optional[dict]:
    """Look up a tracked promo by thread_ts. Returns dict of row values or None."""
    try:
        client = _get_client()
        sheet = client.open_by_key(PROMO_SHEET_ID)
        ws = _ensure_tracker_tab(sheet)

        row_num = _find_row_by_thread(ws, thread_ts)
        if not row_num:
            return None

        row = ws.row_values(row_num)
        return dict(zip(HEADERS, row + [""] * (len(HEADERS) - len(row))))
    except Exception as e:
        logger.error(f"Failed to find promo: {e}")
        return None


def get_all_promos() -> list[dict]:
    """Return all rows from Promo Tracker as list of dicts (keyed by header)."""
    try:
        client = _get_client()
        sheet = client.open_by_key(PROMO_SHEET_ID)
        ws = _ensure_tracker_tab(sheet)
        return ws.get_all_records()
    except Exception as e:
        logger.error(f"Failed to get all promos: {e}")
        return []


def get_promos_needing_reminders() -> list[dict]:
    """Return all Approved promos with end dates."""
    try:
        client = _get_client()
        sheet = client.open_by_key(PROMO_SHEET_ID)
        ws = _ensure_tracker_tab(sheet)

        rows = ws.get_all_records()
        return [r for r in rows if r.get("Status") == "Approved" and r.get("End Date")]
    except Exception as e:
        logger.error(f"Failed to get reminder promos: {e}")
        return []


# Submitter display name → Slack user ID. Used to @-mention POCs in reminders.
# Extend as new POCs start submitting promos.
SUBMITTER_SLACK_IDS = {
    "PersonF": "U0REDACT006",
    "PersonJ": "U0REDACT011",
    "PersonK": "U0REDACT012",
    "PersonL": "U0REDACT013",
    "PersonM": "U0REDACT014",
    "PersonB": "U0REDACT002",
    "PersonH": "U0REDACT008",
    "PersonA": "U0REDACT001",
    "PersonI": "U0REDACT009",
    "PersonD": "U0REDACT004",
}

# Always cc'd on actuals reminders so they have visibility into stale rows.
PERSONA_SLACK_ID = "U0REDACT001"

# Marketing Types where reminders chase a units-based actual.
# POS/Display/Training are investments tracked by months-to-breakeven or
# capability-building, not units sold, so reminding the submitter to fill
# an Actual Units column makes no sense.
#
# Substring tokens (case-insensitive). Matched via `token in mt`, so
# "promoter" catches "Promoter Trial", "Promoter Program", etc. Likewise
# "premium retail" catches "Premium Retail Display" + "Premium Retail
# Activation".
_ACTUALS_REMINDER_SKIP_TYPES = {
    # Per 2026-05-15 user FINAL override: chase actuals for everything
    # except pure point-of-sale display investments. Even SPA+SBA / SOA /
    # Training / Sales Contest / Product Launch / Influencer / KOL chase
    # — POC clarifies in thread if no consumer attribution exists. The
    # chase ping is cheap; silence on actuals is the costly failure mode.
    # PersonL's 2026-05-15 examples (Rheinshop SPA+SBA, Byteport launch) all
    # fall into chase.
    "pos", "display", "displays",
}


# Whitelist that beats skip-list substring match. Premium Retail Display
# contains "display" but is a unit-tracked retail demo, not a POS investment.
_ACTUALS_CHASE_OVERRIDES = (
    "premium retail",
)


def _should_chase_actuals(marketing_type: str) -> bool:
    """Return True if this row should get the post-end actuals reminder.

    Order:
      1. Empty Marketing Type → chase (assume promo, surfaces data quality).
      2. Matches an _ACTUALS_CHASE_OVERRIDES phrase → chase (whitelist wins;
         e.g. 'Premium Retail Display' contains 'display' but is unit-tracked).
      3. Matches any _ACTUALS_REMINDER_SKIP_TYPES substring → skip.
      4. Otherwise → chase.
    """
    if not marketing_type:
        return True   # unspecified → assume promo, chase actuals
    mt = marketing_type.strip().lower()
    if any(ov in mt for ov in _ACTUALS_CHASE_OVERRIDES):
        return True
    return not any(skip in mt for skip in _ACTUALS_REMINDER_SKIP_TYPES)


# The Promo Calendar tracks consumer-facing promos / discounts only.
# Per 2026-05-14 user rule: "On promo calendar -> it is a promo. On
# approvals group -> need not be in promo calendar." Events, trainings,
# POS displays, allowances, contests, influencer outreach, etc. are in
# the approvals group but never go on the calendar.
#
# We use a POSITIVE list (whitelist) of promo-ish keywords. Default is
# SKIP for unknown Marketing Types — submitter can add to the whitelist
# if a new genuine-promo type emerges. Empty Marketing Type defaults to
# True (assume promo, ping) so blank rows surface as data-quality issues
# rather than silently skipping.
#
# Substring matching, case-insensitive. "Discount + SOA" → matches
# "discount" → KEEP (was being wrongly skipped under the old negative
# list because of the "soa" substring).
# Per 2026-05-15 user clarification: Flyer / MVM (Mailer) are
# distribution channels, not market-wide consumer pricing changes. The
# discount itself is happening, but it's targeted at flyer recipients /
# mailer subscribers — not a market-wide pricing event the calendar
# tracks.
_CALENDAR_PROMO_KEYWORDS = (
    "discount", "disc",      # "disc" catches abbreviations like "Pre-summer Disc"
    "coupon", "promo",
    "birthday", "stock clearance", "slow moving", "sale",
)
# Phrases that contain a positive keyword but ARE NOT promos. Checked
# before the positive list. "Event Coupon" contains "coupon" but is an
# event ticket, not a discount. "Promoter Trial" contains "promo" but
# is staffing. "Flyer Discount" / "MVM (Mailer)" contain "discount"
# but are distribution channels, not market-wide pricing events
# (per 2026-05-15 user clarification).
_CALENDAR_PROMO_EXCLUSIONS = (
    "event coupon", "event",
    "promoter",
    "sales contest",                 # contains "sale" but is a contest
    "flyer", "mailer", "mvm",        # distribution channels, not market-wide promos
)


def _should_send_calendar_reminder(
    marketing_type: str,
    discount_pct: "str | int | float" = 0,
) -> bool:
    """Return True if this row belongs on the Promo Calendar.

    Logic (per 2026-05-14/15 user rules):
      0. discount_pct > 0 → True regardless of Marketing Type. Per
         2026-05-15 user clarification: "Any discount should be on promo
         even if it doesn't affect market." Catches Bulkclub UK MVM (Mailer)
         with Discount %=10 — the MT alone would skip it under the
         exclusion list, but a real discount is firing so it belongs on
         the calendar.
      1. Empty/missing Marketing Type → True (ping; surfaces data quality).
      2. Matches any _CALENDAR_PROMO_EXCLUSIONS substring → False (skip).
         Catches "Event Coupon", "Promoter Trial", "Sales Contest",
         "Flyer Discount", "MVM (Mailer)" — but only when discount_pct=0.
      3. Matches any _CALENDAR_PROMO_KEYWORDS substring → True (ping).
         Catches "Discount", "Discount + SOA", "Coupon Code", "Birthday
         Campaign", "Slow Moving Coll", "Stock Clearance".
      4. Otherwise → False (default skip — non-promo type).

    discount_pct accepts str ("10%", "10", "NA", ""), int, or float. Any
    parse failure / non-numeric → treated as 0.
    """
    # 0. Explicit discount overrides MT-only logic. Strip %, $, currency,
    # whitespace; tolerate "NA" / "" / None as zero.
    try:
        if isinstance(discount_pct, (int, float)):
            pct = float(discount_pct)
        else:
            s = str(discount_pct or "").strip().rstrip("%").strip()
            pct = float(s) if s else 0.0
    except (ValueError, TypeError):
        pct = 0.0
    if pct > 0:
        return True

    # 1-4. Existing MT-based logic.
    if not marketing_type:
        return True
    mt = marketing_type.strip().lower()
    if any(excl in mt for excl in _CALENDAR_PROMO_EXCLUSIONS):
        return False
    return any(kw in mt for kw in _CALENDAR_PROMO_KEYWORDS)

# Promo calendar keepers — tagged on approval (FYI) and on T-7 upcoming-promo
# reminders. Order: PersonN (Marketplaces), PersonO, PersonP.
CALENDAR_KEEPER_IDS = ["U0REDACT015", "U0REDACT016", "U0REDACT017"]


# Region calendar mapping now lives in app/regions.py — single source of
# truth for both calendar verifier and POC fan-out, with full alias
# normalization (UAE/GCC, Germany/DE, Austria/AT, Sweden/Nordics, etc.).
from app.regions import REGIONS, calendar_columns_for as _calendar_columns_for

# Deprecated: use app.regions.calendar_columns_for(). Kept as a thin
# backward-compat view for tests that import this dict directly.
_CALENDAR_REGION_TO_COUNTRY_HEADERS: dict[str, list[str]] = {
    canonical: info["calendar_columns"]
    for canonical, info in REGIONS.items()
}

_CALENDAR_SHEET_ID = "REDACTED_SHEET_ID_1"
_CALENDAR_TAB = "2026 Summary"


# Retailer name aliases - calendar entries often use shorter forms.
# When the verifier scans calendar cells, it tries every alias for the
# row's Retailer field. Keys are case-insensitive Tracker values; values
# are the strings to search for (case-insensitive substring).
_RETAILER_ALIASES: dict[str, list[str]] = {
    # Placeholder examples - populate with your own retailers.
    "savannamart":       ["savannamart", "svm"],
    "techbuy":           ["techbuy", "tby"],
    "soundstore":        ["soundstore"],
}


def _retailer_aliases(retailer: str) -> list[str]:
    """Return list of substring tokens to try when matching retailer name
    against calendar cell text. Falls back to [retailer.lower()] when no
    alias entry exists."""
    if not retailer:
        return []
    key = retailer.strip().lower()
    return _RETAILER_ALIASES.get(key, [key])

# Promotions Calendar sheet — 2026 Summary tab
PROMO_CALENDAR_URL = (
    "https://docs.google.com/spreadsheets/d/"
    "REDACTED_SHEET_ID_1/edit?gid=0"
)

# Tracker column that marks when the calendar-keepers FYI was posted.
# Populated = "we've already pinged about this promo" → backfill + on-approval
# both skip the row. Column is auto-added to the sheet on first use.
CALENDAR_FYI_COL = "Calendar FYI Sent At"
APPROVAL_CHAN_COL = "Approval Channel ID"
APPROVAL_TS_COL = "Approval Thread TS"
LOGGED_IN_CAL_COL = "Logged In Calendar At"
T7_PINGED_AT_COL = "T-7 Pinged At"
T7_TAGGED_COL = "T-7 Tagged"   # space-separated Slack user IDs
ACTUALS_SNOOZE_COL = "Actuals Snooze Until"
ACTUALS_CHASE_COL = "Actuals Chase Sent At"   # ISO timestamp of last chase


def _ensure_col(ws, name: str) -> int:
    """Return 1-indexed column for `name`, appending the header if missing."""
    headers = ws.row_values(1)
    if name in headers:
        return headers.index(name) + 1
    col_idx = len(headers) + 1
    ws.update_cell(1, col_idx, name)
    return col_idx


def _resolve_fyi_target(row: dict):
    """Return (channel_id, thread_ts_or_None, is_wa) for posting the calendar
    FYI. Priority: explicit Approval Channel/Thread cols > Thread TS with
    PROMO_CHANNEL_ID fallback."""
    from app.config import PROMO_CHANNEL_ID

    # gspread parses Slack thread TS values like "1773556482.011949" as floats,
    # not strings. Wrap in str() so .strip() doesn't crash on numeric cells.
    approval_chan = str(row.get(APPROVAL_CHAN_COL) or "").strip()
    approval_ts = str(row.get(APPROVAL_TS_COL) or "").strip()

    if approval_chan and approval_ts:
        return approval_chan, approval_ts, False

    thread_ts = str(row.get("Thread TS", "")).strip()
    channel_label = row.get("Channel", "")
    is_wa = (
        channel_label == "WhatsApp"
        or not thread_ts
        or thread_ts.startswith("WA-")
    )
    if is_wa:
        return PROMO_CHANNEL_ID, None, True
    return PROMO_CHANNEL_ID, thread_ts, False


def send_calendar_fyi(slack_client, row: dict, row_index: int, ws=None) -> bool:
    """Post the calendar-keepers FYI in the thread Approver approved and mark
    the Calendar FYI Sent At column. Idempotent: skips if already marked.

    row_index is 1-indexed including header (row 2 = first data row).
    ws is the gspread worksheet handle; re-opened if not passed in.
    """
    from app.config import APPROVER_USER_ID

    if (row.get(CALENDAR_FYI_COL) or "").strip():
        return False

    retailer = row.get("Retailer", "")
    region = row.get("Region", "")
    submitter = (row.get("Submitter") or "").strip()
    submitter_uid = SUBMITTER_SLACK_IDS.get(submitter)

    channel_id, thread_ts, is_wa = _resolve_fyi_target(row)
    if not channel_id or not thread_ts or is_wa:
        # No Slack approval thread to reply in (WhatsApp-sourced or missing TS).
        # Policy: only post as thread replies, never channel top-level — skip.
        return False

    mentions = [f"<@{uid}>" for uid in CALENDAR_KEEPER_IDS]
    if APPROVER_USER_ID and APPROVER_USER_ID not in CALENDAR_KEEPER_IDS:
        mentions.append(f"<@{APPROVER_USER_ID}>")
    if submitter_uid and submitter_uid not in CALENDAR_KEEPER_IDS and submitter_uid != APPROVER_USER_ID:
        mentions.append(f"<@{submitter_uid}>")

    msg = (
        f":calendar: {' '.join(mentions)} — *{retailer} ({region})* promo "
        f"was approved. Please update the <{PROMO_CALENDAR_URL}|Promotions "
        f"Calendar> (2026 Summary tab) for this one and confirm it is/will "
        f"be live on the retailer side. Once logged, paste the date into the "
        f"*{LOGGED_IN_CAL_COL}* column of the Marketing Tracker so we stop pinging."
    )

    try:
        slack_client.chat_postMessage(
            channel=channel_id, thread_ts=thread_ts, text=msg,
        )
    except Exception as e:
        logger.error(f"Calendar FYI post failed for {retailer} ({region}): {e}")
        return False

    # Mark row as sent
    try:
        if ws is None:
            client = _get_client()
            ws = _ensure_tracker_tab(client.open_by_key(PROMO_SHEET_ID))
        col_idx = _ensure_col(ws, CALENDAR_FYI_COL)
        ws.update_cell(row_index, col_idx, datetime.utcnow().isoformat(timespec="minutes"))
    except Exception:
        logger.exception(f"Failed to mark {CALENDAR_FYI_COL} for row {row_index}")

    logger.info(f"Calendar FYI sent: {retailer} ({region}), channel={channel_id}, wa={is_wa}")
    return True


def _parse_input_date(s: str, end_of_month: bool = False):
    """Parse a date string for the calendar verifier. Prefers ISO
    YYYY-MM-DD (the tracker's storage format via _to_iso_date) and falls
    back to dateparser DMY for free-text edge cases. Returns a date or None.

    For ambiguous dates without a year (e.g. 'Jun 7', '7-Jun', 'June 7'),
    we ALWAYS assume the current calendar year — never walk backward to a
    prior year. Dateparser's default with no anchor would pick the closest
    occurrence, which can land in last year if the date is months away;
    that broke the actuals chase ("Savannamart ZA promo ended 341 days ago"
    on 2026-05-14 because 'Jun 7' was parsed as 2025-06-07).

    `end_of_month` — when True AND the input is month+year only (e.g.,
    'Mar 2026', 'March 2026'), bump the day to the last day of that month.
    Use for end_date inputs so 'Mar 2026' means "ended on Mar 31" not
    "ended on Mar 1" (Byteport bug 2026-05-14: bot said "ended 74 days
    ago" computed from Mar 1; user expected ~44 from Mar 31).

    Anchor strategy: RELATIVE_BASE = Jan 1 of the current year, with
    PREFER_DATES_FROM=current_period. After the parse, if the input has
    no 4-digit year and the result year is in the past, force-bump it.
    """
    if not s:
        return None
    s = s.strip()
    # Fast path: ISO YYYY-MM-DD (tracker's storage format).
    try:
        return date.fromisoformat(s[:10])
    except (ValueError, TypeError):
        pass
    # Fallback: free-text dates (rare for this code path).
    import dateparser as _dp
    import calendar as _cal
    from datetime import datetime as _dt
    today = date.today()
    current_year = today.year
    relative_base = _dt.combine(date(current_year, 1, 1), _dt.min.time())
    parsed = _dp.parse(
        s,
        settings={
            "DATE_ORDER": "DMY",
            "PREFER_DATES_FROM": "current_period",
            "RELATIVE_BASE": relative_base,
        },
    )
    if not parsed:
        return None
    result = parsed.date()
    # If the input has no explicit 4-digit year and the parser still
    # walked us into the past, force-bump to the current year. Defensive
    # belt to RELATIVE_BASE: some dateparser versions still pick "nearest"
    # past occurrence when the relative base sits before the date itself.
    has_explicit_year = bool(re.search(r"\b(19|20)\d{2}\b", s))
    if not has_explicit_year and result.year < current_year:
        try:
            result = result.replace(year=current_year)
        except ValueError:
            # Feb 29 in a non-leap year — fall back to Feb 28 of current year.
            result = result.replace(year=current_year, day=28)
    # End-of-month bump for month+year-only inputs. Detect via simple
    # regex: input is "<MonthName> <Year>" with nothing else (e.g.,
    # "Mar 2026", "March 2026"). Bump day to last day of month so end-date
    # math reflects "promo ran through end of March", not "ended Mar 1".
    if end_of_month:
        if re.match(r"^[A-Za-z]+\s+\d{4}$", s):
            last_day = _cal.monthrange(result.year, result.month)[1]
            result = result.replace(day=last_day)
    return result


DEFAULT_SNOOZE_DAYS = 3

# Vague-acknowledgment phrases that imply "I'm aware, give me time" but
# don't carry a specific date. Caller can opt to default-snooze on these.
_VAGUE_ACK_KEYWORDS = (
    "working on it", "looking into it", "will check", "let me check",
    "soon", "asap", "in progress", "asking team", "checking with",
    "need to check", "give me time",
)


def _date_to_datetime(d):
    """dateparser RELATIVE_BASE wants datetime, not date. Convert."""
    from datetime import datetime as _dt
    return _dt.combine(d, _dt.min.time())


def parse_actuals_snooze(
    text: "str | None",
    today: "date | None" = None,
    default_if_vague: bool = False,
) -> "date | None":
    """Parse a thread-reply text into a snooze-until date.

    Order:
      1. Regex match for "in N days/weeks/months" or "give me N ..." → date math.
      2. If text contains a date signal (weekday / month / year / tomorrow /
         next week|month / "by N") → run dateparser. Accepts short replies
         like "Jun 7", "Mon", "Friday".
      3. If text is a vague-ack and default_if_vague → today + DEFAULT_SNOOZE_DAYS.
      4. Otherwise None.

    Bug 2026-05-15 dual-review: the previous len(text)<6 guard outright
    rejected valid short snoozes ("Jun 7", "Mon", "Friday"). Replaced with
    the date-signal regex check — if the input contains a date keyword,
    let dateparser handle it regardless of length. Vague-acks are still
    filtered out via _VAGUE_ACK_KEYWORDS.
    """
    from datetime import date as _date, timedelta as _td
    import re as _re
    import dateparser as _dp
    if not text:
        return None
    today = today or _date.today()
    t = text.lower().strip()
    if not t:
        return None

    # If the text is a vague-ack ("working on it", "looking into it"), skip
    # the dateparser pass — those phrases trip false positives (e.g. "on"
    # parses to a date). Either default-snooze or return None.
    is_vague = any(kw in t for kw in _VAGUE_ACK_KEYWORDS)
    if is_vague:
        if default_if_vague:
            return today + _td(days=DEFAULT_SNOOZE_DAYS)
        return None

    m = _re.search(r"\bin\s+(\d+)\s+(day|days|week|weeks|month|months)\b", t)
    if m:
        n = int(m.group(1))
        unit = m.group(2).rstrip("s")
        delta = {"day": _td(days=n), "week": _td(weeks=n),
                 "month": _td(days=n * 30)}[unit]
        return today + delta

    m = _re.search(r"\bgive\s+me\s+(\d+)\s+(day|days|week|weeks|month|months)\b", t)
    if m:
        n = int(m.group(1))
        unit = m.group(2).rstrip("s")
        delta = {"day": _td(days=n), "week": _td(weeks=n),
                 "month": _td(days=n * 30)}[unit]
        return today + delta

    # Date-signal gate: only invoke dateparser when the input contains a
    # recognizable date keyword (weekday / month / year / tomorrow /
    # next week|month / "by N"). Without this, dateparser tries absurdly
    # hard to coerce noise like "on it", "thanks", "checking" into a date.
    # WITH this, short valid inputs ("Jun 7", "Mon", "Friday") are still
    # accepted because they hit a weekday/month token.
    has_date_signal = bool(_re.search(
        r"\b(mon|tue|wed|thu|fri|sat|sun|monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
        r"jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|"
        r"january|february|march|april|june|july|august|september|october|november|december|"
        r"20\d{2}|next\s+week|next\s+month|tomorrow|by\s+\d)",
        t,
    ))
    if not has_date_signal:
        if default_if_vague and any(kw in t for kw in _VAGUE_ACK_KEYWORDS):
            return today + _td(days=DEFAULT_SNOOZE_DAYS)
        return None

    # search_dates() finds dates inside surrounding text ("data on 2026-06-01",
    # "data by Friday"). Plain .parse() rejects those — it requires the whole
    # string be a date. We still fall back to .parse() if search_dates returns
    # nothing.
    parsed = None
    try:
        from dateparser.search import search_dates as _search_dates
        hits = _search_dates(
            text,
            settings={
                "PREFER_DATES_FROM": "future",
                "RELATIVE_BASE": _date_to_datetime(today),
            },
        )
        if hits:
            # Pick the first hit whose date is strictly in the future
            # within 180d. Skip junk matches like "on" → today's date.
            for _, dt in hits:
                d = dt.date()
                if today < d < today + _td(days=180):
                    parsed = dt
                    break
    except Exception:
        parsed = None
    if parsed is None:
        parsed = _dp.parse(
            text,
            settings={
                "PREFER_DATES_FROM": "future",
                "RELATIVE_BASE": _date_to_datetime(today),
            },
        )
    if parsed:
        d = parsed.date()
        if d > today and d < today + _td(days=180):
            return d

    if default_if_vague and any(kw in t for kw in _VAGUE_ACK_KEYWORDS):
        return today + _td(days=DEFAULT_SNOOZE_DAYS)

    return None


def _months_in_range(start: "date", end: "date") -> list[tuple[int, int]]:
    """Return [(year, month), ...] for every month the [start, end] range
    touches, inclusive of both endpoints. Used by the calendar verifier
    so a promo spanning April–May checks BOTH month sections.
    """
    if end < start:
        return []
    out: list[tuple[int, int]] = []
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        out.append((y, m))
        m += 1
        if m > 12:
            m = 1
            y += 1
    return out


def _parse_calendar_date_range(cell: str, year: int, default_month: int | None = None):
    """Parse a calendar Date cell into a (start_date, end_date) tuple.

    Handles a wide variety of human-typed formats — the calendar is keyed
    in by hand so spacing/punctuation is loose:

    Standard:
      "30th April - 10th May"   "1 - 8 May"   "May 1–12"   "5 - 21 June"

    Loose hyphenation (2026-05-15 audit):
      "27-April - 24-May"       (ALPENHAUS CH style — hyphen between day & month)
      "21- May - 2-June"        (Catalogmart UK style — extra hyphen + spacing)

    Day-only ranges (require `default_month` from the calling section):
      "1st - 8th"               (Kiwilink NZ Easter style — month from header)

    Returns None if unparseable. Assumes both endpoints in the given year
    unless the second endpoint's month is numerically earlier than the
    first, in which case the second endpoint rolls into year+1 (e.g.
    "21 Dec - 5 Jan" → Jan = next year). default_month is used only when
    BOTH endpoints lack a month name.
    """
    import re as _re
    import dateparser as _dp
    s = (cell or "").strip()
    if not s:
        return None
    # Strip ordinal suffixes (1st, 2nd, 3rd, 4th-31st)
    s = _re.sub(r'(\d+)(st|nd|rd|th)', r'\1', s, flags=_re.IGNORECASE)
    # Normalize hyphen-as-day-month-glue: "27-April" → "27 April",
    # "21- May" → "21 May". Done BEFORE dash normalization so the
    # day-month hyphen doesn't get mistaken for a range separator.
    # Pattern: digit(s) followed by optional spaces then '-' then optional
    # spaces then a month name → replace with "digits month".
    _MONTHS = r'(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*'
    s = _re.sub(
        rf'(\d+)\s*-\s*({_MONTHS})\b',
        r'\1 \2',
        s,
        flags=_re.IGNORECASE,
    )
    # Normalize various dashes/separators to a single hyphen
    s = _re.sub(r'\s*[-–—]\s*', ' - ', s)
    parts = s.split(' - ')
    if len(parts) != 2:
        return None
    left = parts[0].strip()
    right = parts[1].strip()
    # Determine which side has a month name
    month_re = _re.compile(rf'\b{_MONTHS}\b', _re.I)
    left_has = bool(month_re.search(left))
    right_has = bool(month_re.search(right))
    if not left_has and right_has:
        # "1 - 8 May" → left needs month from right
        m = month_re.search(right)
        left = f"{left} {m.group(0)}"
    elif left_has and not right_has:
        # "30 April - 10" → right needs month from left
        m = month_re.search(left)
        right = f"{right} {m.group(0)}"
    elif not left_has and not right_has:
        # Both endpoints bare — use the calendar section's month as fallback.
        # Caller (e.g. _find_calendar_match) tracks the active "May 2026"
        # section and passes default_month=5. Without this, "1st - 8th"
        # (Kiwilink NZ Easter style) is unparseable.
        if default_month is None:
            return None
        from datetime import date as _date
        month_name = _date(year, default_month, 1).strftime("%B")
        left = f"{left} {month_name}"
        right = f"{right} {month_name}"
    # Append year to both
    d1 = _dp.parse(f"{left} {year}", settings={"DATE_ORDER": "DMY"})
    d2 = _dp.parse(f"{right} {year}", settings={"DATE_ORDER": "DMY"})
    if not d1 or not d2:
        return None
    start_date = d1.date()
    end_date = d2.date()
    # Year rollover: docstpro ringmises ranges like "21 Dec - 5 Jan" roll
    # the end into year+1. Without this, both endpoints get the same year
    # (Dec 21, 2026 → Jan 5, 2026 → end<start) and the verifier's overlap
    # check silently fails. Bug 2026-05-15 dual-review.
    if end_date < start_date:
        try:
            end_date = end_date.replace(year=end_date.year + 1)
        except ValueError:
            # Feb 29 on non-leap rollover — extremely unlikely for this
            # use case, but bail safely instead of crashing.
            return None
    return start_date, end_date


def load_calendar_values() -> list[list[str]] | None:
    """Read the Promotions Calendar 2026 Summary tab once. Used by the
    cron orchestrator to pass cached values to per-row functions."""
    try:
        client = _get_client()
        cal_sheet = client.open_by_key(_CALENDAR_SHEET_ID)
        ws = cal_sheet.worksheet(_CALENDAR_TAB)
        return ws.get_all_values()
    except Exception as e:
        logger.warning(f"load_calendar_values failed: {e}")
        return None


def _find_calendar_match(
    retailer: str,
    region: str,
    start_str: str,
    end_str: str,
    calendar_values: list[list[str]] | None = None,
) -> str | None:
    """Like _is_logged_in_calendar, but returns a human-readable string
    describing the matched calendar cell (or None on no match).

    Format examples:
      "29 May - 7 Jun → Pre-Summer Promo (Sweden)"
      "TAL Birthday Promo (South Africa, alias)"

    Used by callers (e.g. _mark_logged_in_calendar) so the
    `Logged In Calendar At` cell records which calendar entry matched.
    Visible in the sheet — if the bot mis-matched a 2025 section because
    End Date was wrong-year, the user spots it by eye.

    Wrong-year guard (2026-05-14): if both target_start.year and
    target_end.year differ from the current calendar year, return None.
    Prevents the verifier from silently scanning non-existent past-year
    sections of the calendar — those rows should be flagged via the
    cron startup data-hygiene log, not silently re-pinged forever.
    Caught with Nordica AB (Nordics) End Date "Jun 7" parsing to 2025.
    """
    target_start = _parse_input_date(start_str)
    target_end = _parse_input_date(end_str, end_of_month=True)
    if not target_start or not target_end:
        return None

    # 2026-05-15 audit: Rheinshop Europe row had End=2026-04-14 BEFORE
    # Start=2026-05-06 (submitter form typo). Bail rather than scan a
    # nonsensical inverted range — the actuals chase block has a parallel
    # guard so the row is fully quiet until the human fixes column K.
    if target_end < target_start:
        logger.warning(
            f"calendar verify: {retailer} ({region}) has End Date "
            f"{end_str!r} BEFORE Start Date {start_str!r} — submitter form "
            f"typo. Skipping calendar verify to avoid garbage matches."
        )
        return None

    from datetime import date as _date
    current_year = _date.today().year
    if target_start.year != current_year and target_end.year != current_year:
        logger.warning(
            f"calendar verify: {retailer} ({region}) dates parsed as "
            f"{target_start}–{target_end} (year != {current_year}). "
            f"Skipping — End/Start Date column likely has wrong year."
        )
        return None

    year = target_start.year

    country_headers = _calendar_columns_for(region)
    if not country_headers:
        logger.info(f"Calendar check skipped — region {region!r} not mapped to any calendar column")
        return None

    if calendar_values is None:
        try:
            client = _get_client()
            cal_sheet = client.open_by_key(_CALENDAR_SHEET_ID)
            ws = cal_sheet.worksheet(_CALENDAR_TAB)
            calendar_values = ws.get_all_values()
        except Exception as e:
            logger.warning(f"Calendar check failed to read sheet: {e}")
            return None

    all_values = calendar_values
    if not all_values:
        return None
    header = all_values[0]
    col_indices: list[int] = []
    for country in country_headers:
        for idx, h in enumerate(header):
            if h.strip().lower() == country.lower():
                col_indices.append(idx)
                break
    if not col_indices:
        return None

    import re as _re
    months = _months_in_range(target_start, target_end)
    target_labels = {date(y, m, 1).strftime("%B %Y") for (y, m) in months}
    # Build label -> month-int map so we can pass default_month to the
    # date parser when a Date cell is bare-day ("1st - 8th"). The cell's
    # month then comes from the calendar section header.
    target_label_to_month = {
        date(y, m, 1).strftime("%B %Y"): m for (y, m) in months
    }
    month_header_re = _re.compile(r'^[A-Za-z]+\s+\d{4}$')
    in_target_section = False
    current_section_month: int | None = None

    # Per-retailer aliases for the cell-text fallback path. Lets "Savannamart"
    # match a "TAL Birthday Promo" cell (2026-05-14 fix).
    aliases = _retailer_aliases(retailer)

    for row_idx, row in enumerate(all_values):
        if not row:
            continue
        label = (row[0] or "").strip()
        if label in target_labels:
            in_target_section = True
            current_section_month = target_label_to_month.get(label)
            continue
        if month_header_re.match(label):
            in_target_section = False
            current_section_month = None
            continue
        if not in_target_section:
            continue
        # Alias fallback: any cell in the country columns of this section
        # whose text contains a retailer alias is treated as a hit. Catches
        # entries where the calendar has a name but no parseable Date cell,
        # or where the Date cell's range is slightly off but the entry is
        # clearly the right retailer for the right country + month.
        if aliases:
            for col_idx in col_indices:
                if col_idx >= len(row):
                    continue
                cell_text = (row[col_idx] or "").strip()
                cell_lower = cell_text.lower()
                if not cell_lower:
                    continue
                if any(a in cell_lower for a in aliases):
                    desc = f"{cell_text} ({header[col_idx]}, alias)"
                    logger.info(
                        f"Calendar HIT (alias): {retailer} ({region}) "
                        f"{start_str}–{end_str} matched cell {cell_text!r} "
                        f"in col {header[col_idx]!r} via alias"
                    )
                    return desc
        if label != "Date":
            continue
        # Found a Date row in the target month. Check our country columns.
        for col_idx in col_indices:
            if col_idx >= len(row):
                continue
            cell = (row[col_idx] or "").strip()
            if not cell:
                continue
            parsed = _parse_calendar_date_range(
                cell, year, default_month=current_section_month,
            )
            if not parsed:
                continue
            cell_start, cell_end = parsed
            if cell_start > target_end or cell_end < target_start:
                continue  # no overlap
            # Overlap. Confirm corresponding Name/Retail row has content
            # (cheap sanity that this isn't a stale Date stub). Capture the
            # follow-up Name (offset 2) for the match description so the
            # user can eyeball what the bot matched.
            has_followup = False
            name_text = ""
            for follow_offset in (1, 2, 3):  # Day, Name, Retail
                follow_idx = row_idx + follow_offset
                if follow_idx >= len(all_values):
                    break
                follow_row = all_values[follow_idx]
                if col_idx < len(follow_row) and (follow_row[col_idx] or "").strip():
                    has_followup = True
                    if follow_offset == 2 and not name_text:
                        name_text = (follow_row[col_idx] or "").strip()
            if has_followup:
                country_label = header[col_idx] if col_idx < len(header) else ""
                if name_text:
                    desc = f"{cell} -> {name_text} ({country_label})"
                else:
                    desc = f"{cell} ({country_label})"
                logger.info(
                    f"Calendar HIT: {retailer} ({region}) {start_str}–{end_str} "
                    f"matched cell '{cell}' in col {header[col_idx]!r}"
                )
                return desc
    return None


def _is_logged_in_calendar(
    retailer: str,
    region: str,
    start_str: str,
    end_str: str,
    calendar_values: list[list[str]] | None = None,
) -> bool:
    """Boolean wrapper around _find_calendar_match for callers that only
    need the True/False answer. See _find_calendar_match for behavior +
    wrong-year guard rules."""
    return _find_calendar_match(
        retailer, region, start_str, end_str,
        calendar_values=calendar_values,
    ) is not None


def _mark_logged_in_calendar(thread_ts: str, source: str = "auto-detected in calendar") -> bool:
    """Auto-fill Logged In Calendar At on the Promo Tracker row matching
    `thread_ts`, so subsequent reminder runs skip it. Returns True on success."""
    if not thread_ts:
        return False
    try:
        from datetime import datetime as _dt
        client = _get_client()
        sheet = client.open_by_key(PROMO_SHEET_ID)
        ws = _ensure_tracker_tab(sheet)
        all_rows = ws.get_all_records()
        for i, r in enumerate(all_rows):
            if str(r.get("Thread TS", "")).strip() == thread_ts:
                col_idx = _ensure_col(ws, LOGGED_IN_CAL_COL)
                stamp = f"{_dt.utcnow().strftime('%Y-%m-%dT%H:%MZ')} ({source})"
                ws.update_cell(i + 2, col_idx, stamp)  # +2: skip header + 0-index
                return True
        return False
    except Exception as e:
        logger.warning(f"Failed to mark Logged In Calendar At: {e}")
        return False


def _build_calendar_reminder_message(
    retailer: str,
    region: str,
    submitter_uid: str | None,
) -> str:
    """Compose the calendar-not-logged reminder message body.

    Tags: PersonN + PersonO + PersonP (always) + regional retail POC for the
    promo's Region + the submitter. Drops Approver (per 2026-05-13 spec).
    Drops the "fill Logged In Calendar At" instruction — bot auto-fills
    the column once it detects the entry on the next scan.
    """
    from app.regional_pocs import get_regional_poc_ids

    mention_ids: list[str] = list(CALENDAR_KEEPER_IDS)
    for uid in get_regional_poc_ids(region):
        if uid and uid not in mention_ids:
            mention_ids.append(uid)
    if submitter_uid and submitter_uid not in mention_ids:
        mention_ids.append(submitter_uid)

    mentions = " ".join(f"<@{uid}>" for uid in mention_ids)
    return (
        f":spiral_calendar_pad: {mentions} — *{retailer}* still not showing "
        f"up in the <{PROMO_CALENDAR_URL}|Promotions Calendar> "
        f"(2026 Summary tab). Please log it."
    )


def _build_t7_message(
    retailer: str,
    start_str: str,
    end_str: str,
    mention_uids: list[str],
) -> str:
    """T-7 ping body — drops the (Region) suffix, includes date range."""
    mentions = " ".join(f"<@{uid}>" for uid in mention_uids)
    return (
        f":stopwatch: {mentions} — *{retailer}* promo starts in 7 days "
        f"({start_str} – {end_str}). Please confirm it's scheduled for "
        f"your channel."
    )


def _record_t7_pinged(promo: dict, mention_uids: list[str]) -> None:
    """Persist when T-7 fired + who got tagged, so T-1 can subtract repliers."""
    from datetime import datetime as _dt
    thread_ts = str(promo.get("Thread TS", "")).strip()
    if not thread_ts:
        return
    try:
        client = _get_client()
        sheet = client.open_by_key(PROMO_SHEET_ID)
        ws = _ensure_tracker_tab(sheet)
        all_rows = ws.get_all_records()
        for i, r in enumerate(all_rows):
            if str(r.get("Thread TS", "")).strip() == thread_ts:
                ts_col = _ensure_col(ws, T7_PINGED_AT_COL)
                tagged_col = _ensure_col(ws, T7_TAGGED_COL)
                ws.update_cell(i + 2, ts_col, _dt.utcnow().isoformat() + "Z")
                ws.update_cell(i + 2, tagged_col, " ".join(mention_uids))
                return
    except Exception as e:
        logger.warning(f"_record_t7_pinged failed: {e}")


def _record_actuals_chased(promo: dict) -> None:
    """Stamp the current UTC time in the Actuals Chase Sent At column.

    The actuals chase has no fixed cadence — it re-fires every sweep from
    day 7 until Actual Units is filled — so this is NOT a one-shot 'done'
    marker. It records WHEN the chase last fired so the same-day guard in
    check_and_send_reminders can skip a duplicate fire if the sweep runs
    twice in one day (duplicate CronJob tick, manual /reminders/trigger,
    k8s job retry). Mirrors _record_t7_pinged.
    """
    from datetime import datetime as _dt
    thread_ts = str(promo.get("Thread TS", "")).strip()
    if not thread_ts:
        return
    try:
        client = _get_client()
        sheet = client.open_by_key(PROMO_SHEET_ID)
        ws = _ensure_tracker_tab(sheet)
        all_rows = ws.get_all_records()
        for i, r in enumerate(all_rows):
            if str(r.get("Thread TS", "")).strip() == thread_ts:
                col = _ensure_col(ws, ACTUALS_CHASE_COL)
                ws.update_cell(i + 2, col, _dt.utcnow().isoformat() + "Z")
                return
    except Exception as e:
        logger.warning(f"_record_actuals_chased failed: {e}")


def _get_thread_repliers_since(
    slack_client, channel_id: str, thread_ts: str, since_ts: str,
) -> set[str]:
    """Return set of Slack user IDs who posted any reply in the thread
    after `since_ts`. Used to compute the silent subset for T-1."""
    if not (channel_id and thread_ts and since_ts):
        return set()
    try:
        resp = slack_client.conversations_replies(
            channel=channel_id, ts=thread_ts, oldest=since_ts,
        )
        repliers = set()
        for msg in resp.get("messages", []) or []:
            uid = msg.get("user")
            if uid and msg.get("ts", "0") > since_ts:
                repliers.add(uid)
        return repliers
    except Exception as e:
        logger.warning(f"_get_thread_repliers_since failed: {e}")
        return set()


def _compute_t1_tag_list(
    t7_tagged_uids: list[str],
    t7_ping_ts: str,
    channel_id: str,
    slack_client=None,
    thread_ts: str = "",
) -> list[str]:
    """T-1 tag list = T-7 list minus anyone who replied since T-7."""
    repliers = _get_thread_repliers_since(
        slack_client, channel_id, thread_ts, t7_ping_ts,
    )
    return [uid for uid in t7_tagged_uids if uid not in repliers]


def _build_t1_message(
    retailer: str, start_str: str, end_str: str, mention_uids: list[str],
) -> str:
    """T-1 ping body — 'starts tomorrow' phrasing."""
    mentions = " ".join(f"<@{uid}>" for uid in mention_uids)
    return (
        f":alarm_clock: {mentions} — *{retailer}* promo starts tomorrow "
        f"({start_str} – {end_str}). Please confirm it's scheduled for "
        f"your channel."
    )


def _build_actuals_message(
    retailer: str,
    region: str,
    days_since_end: int,
    predicted_units: int,
    mention_uids: list[str],
) -> str:
    """Actuals chase ping body. Tags the mention_uids passed in (caller
    decides who: regional POC at T+7, +Approver from T+14)."""
    mentions = " ".join(f"<@{uid}>" for uid in mention_uids)
    sheet_link = (
        "https://docs.google.com/spreadsheets/d/"
        "REDACTED_SHEET_ID_2/edit#gid=0"
    )
    pred_line = (
        f"\n\n_Predicted: {predicted_units} units._"
        if predicted_units else ""
    )
    return (
        f":bell: {mentions} — *{retailer}* promo ended "
        f"{days_since_end} days ago.\n"
        f"\nTwo ways to log actuals (pick one):\n"
        f"  1. Reply in this thread:  `actuals 379`  (just the unit count)\n"
        f"  2. Or fill the row in the <{sheet_link}|Marketing Tracker>"
        f"{pred_line}"
    )


# Calendar reply classification keywords. Seeded list — tune in v2 once
# Slack MCP is back and we can sample real replies.
_CALENDAR_DONE_KEYWORDS = (
    "done", "logged", "added", "posted", "updated",
    "it's there", "it is there", "live now", "confirmed", "synced",
    "in calendar",
)
# Listed in priority order — longer/more specific phrases first so they
# beat single words during substring scan.
_CALENDAR_EXEMPT_KEYWORDS = (
    "won't be on calendar", "not for calendar", "regular promo",
    "not applicable", "internal only", "one-off", "not tracking",
    "no need to log", "no need", "exempt", "n/a",
    # Anchored exempt phrases — must be specific enough to avoid
    # matching evaluation discussion. "event" / "b2b" / "skip" / "bau"
    # alone were too broad. "bau" matches "Feb as BAU" baseline notation
    # in eval replies (caught in dry-run on 2026-05-14 — SOUNDSTORE FP).
    "not a retailer", "is b2b", "b2b only", "b2b event", "purely b2b",
    "is an event", "was an event", "event sponsorship", "this event isn",
    "skip this", "skip it", "skip for now",
    "bau, no calendar", "is bau", "this is bau",
    # Single-influencer / no-pricing-impact promos — PersonL's reply on
    # Nordmed (Sweden) on 2026-04-23 ("only one influencer ... need not be
    # updated on promo calendar as it does not impact pricing").
    "need not", "not needed", "doesn't impact pricing",
    "does not impact pricing", "only one influencer", "no impact on pricing",
    # 2026-05-14 fix — PersonL's Rheinshop/Alderon reply phrasings
    "only marketing", "marketing only", "just marketing",
    "does not impact promos", "doesn't impact promos",
    "no impact on promos", "no impact",
)


def classify_calendar_reply(text: str | None) -> str:
    """Bucket a thread reply into A (done) / B (reason-exempt) / C (other).

    Substring match, case-insensitive. Bucket B is checked before A to
    catch phrases like "won't be on calendar" (which contains "calendar"
    but is NOT a done signal).
    """
    if not text:
        return "C"
    t = text.lower()
    if any(kw in t for kw in _CALENDAR_EXEMPT_KEYWORDS):
        return "B"
    if any(kw in t for kw in _CALENDAR_DONE_KEYWORDS):
        return "A"
    return "C"


def handle_calendar_reply(
    slack_client,
    thread_ts: str,
    channel_id: str,
    reply_text: str,
    promo_row: dict,
) -> str:
    """Process a thread reply for the calendar reminder.

    Returns one of: "confirmed", "pushed_back", "exempted", "noop".
    """
    bucket = classify_calendar_reply(reply_text)
    if bucket == "C":
        return "noop"

    retailer = promo_row.get("Retailer", "")
    region = promo_row.get("Region", "")

    if bucket == "B":
        snippet = (reply_text or "")[:120].replace("\n", " ").strip()
        _mark_logged_in_calendar(thread_ts, source=f"NA - reason: {snippet}")
        try:
            slack_client.chat_postMessage(
                channel=channel_id,
                thread_ts=thread_ts,
                text=(
                    f":no_entry_sign: Got it — marking *{retailer} ({region})* "
                    f"exempt from calendar tracking. Reply 'log it' to override."
                ),
            )
        except Exception as e:
            logger.warning(f"calendar reply confirm-exempt post failed: {e}")
        return "exempted"

    # Bucket A: re-verify the calendar before trusting "done"
    start_str = promo_row.get("Start Date", "")
    end_str = promo_row.get("End Date", "")
    match_desc = _find_calendar_match(retailer, region, start_str, end_str)
    if match_desc:
        # Source string includes the matched cell text so it shows up in
        # `Logged In Calendar At` — user can eyeball wrong-year matches.
        _mark_logged_in_calendar(
            thread_ts, source=f"auto after reply: {match_desc}",
        )
        try:
            slack_client.chat_postMessage(
                channel=channel_id,
                thread_ts=thread_ts,
                text=(
                    f":white_check_mark: Confirmed in calendar — thanks. "
                    f"Future pings stopped."
                ),
            )
        except Exception as e:
            logger.warning(f"calendar reply confirm post failed: {e}")
        return "confirmed"

    try:
        slack_client.chat_postMessage(
            channel=channel_id,
            thread_ts=thread_ts,
            text=(
                f":warning: Still not showing up for *{retailer} ({region})* — "
                f"can you double-check the country column / month section?"
            ),
        )
    except Exception as e:
        logger.warning(f"calendar reply push-back post failed: {e}")
    return "pushed_back"


# BOT_EMOJI_PREFIXES — bot posts always start with one of these.
# Belt-and-braces filter on top of bot_id/subtype/user/app_id checks.
_BOT_REPLY_EMOJI_PREFIXES = (
    ":calendar:", ":bell:", ":stopwatch:", ":alarm_clock:",
    ":no_entry_sign:", ":white_check_mark:", ":warning:", ":zzz:",
    ":spiral_calendar_pad:",
)


def _is_bot_reply(msg: dict, bot_uid: str | None) -> bool:
    """Return True if msg is a bot post (us or any other bot). Robust to
    Slack's inconsistent bot/app field setting across post types."""
    if not msg:
        return True
    if bot_uid and msg.get("user") == bot_uid:
        return True
    if msg.get("subtype") == "bot_message":
        return True
    if msg.get("bot_id"):
        return True
    if msg.get("app_id"):
        return True
    text = (msg.get("text") or "").lstrip()
    if any(text.startswith(p) for p in _BOT_REPLY_EMOJI_PREFIXES):
        return True
    return False


def backfill_calendar_reply_scan(slack_client, calendar_values=None) -> dict:
    """One-shot scan of historical replies on open Approved promo threads.
    Catches bucket B / bucket A signals from replies that landed before the
    classifier shipped. Returns {"exempted": N, "confirmed": N, "noop": N}.

    For every Approved promo with `Logged In Calendar At` blank:
      1. Resolve the approval channel + thread_ts (via _resolve_fyi_target).
      2. Pull the thread's replies (skip bot's own + the original parent).
      3. Run classify_calendar_reply on each.
      4. If ANY reply is bucket B → mark exempt + post a one-time confirm.
      5. Else if ANY reply is bucket A → re-verify via _is_logged_in_calendar
         and mark logged on hit.
      6. Otherwise noop.

    Idempotent: rows already marked (Logged In Calendar At populated) are
    skipped entirely, so the confirmation reply is never re-posted.
    Slack failures (network, auth, missing scope) are logged and the row
    is skipped — never crashes the cron.
    """
    summary = {"exempted": 0, "confirmed": 0, "noop": 0}

    try:
        promos = get_promos_needing_reminders()
    except Exception:
        logger.exception("backfill_calendar_reply_scan: failed to load promos")
        return summary

    # Resolve bot user id once so we can drop the bot's own replies from
    # the scan (otherwise the bot's exempt-confirm would feed itself).
    bot_user_id = None
    try:
        info = slack_client.auth_test()
        bot_user_id = info.get("user_id")
    except Exception as e:
        logger.warning(f"backfill_calendar_reply_scan: auth_test failed: {e}")

    for promo in promos:
        # Idempotency: if already marked, skip entirely. Don't re-classify,
        # don't re-post any confirmation.
        if (promo.get(LOGGED_IN_CAL_COL) or "").strip():
            continue

        retailer = promo.get("Retailer", "")
        region = promo.get("Region", "")
        thread_ts = str(promo.get("Thread TS", "")).strip()

        channel_id, post_thread_ts, is_wa = _resolve_fyi_target(promo)
        if is_wa or not channel_id or not post_thread_ts:
            # Thread-only policy: WhatsApp-sourced rows have no Slack thread
            # to scan, and rows missing approval channel/ts can't be reached.
            continue

        try:
            resp = slack_client.conversations_replies(
                channel=channel_id, ts=post_thread_ts,
            )
            messages = resp.get("messages", []) or []
        except Exception as e:
            # Network / auth / missing channels:history scope — log and move on.
            logger.warning(
                f"backfill_calendar_reply_scan: conversations_replies failed for "
                f"{retailer} ({region}) channel={channel_id} ts={post_thread_ts}: {e}"
            )
            continue

        # Skip the parent message (the eval/approval post). We only want
        # *replies*, and only from humans (not the bot's own pings).
        replies: list[str] = []
        for msg in messages:
            msg_ts = msg.get("ts", "")
            if msg_ts == post_thread_ts:
                continue  # original eval/approval post
            if _is_bot_reply(msg, bot_user_id):
                continue
            text = msg.get("text", "") or ""
            if not text.strip():
                continue
            replies.append(text)

        if not replies:
            summary["noop"] += 1
            continue

        # Bucket B wins outright — exempt the row.
        bucket_b_text = next(
            (t for t in replies if classify_calendar_reply(t) == "B"),
            None,
        )
        if bucket_b_text is not None:
            snippet = bucket_b_text[:120].replace("\n", " ").strip()
            _mark_logged_in_calendar(thread_ts, source=f"NA - reason: {snippet}")
            # Silent: bucket-B historical reply already exempts the row in
            # the sheet — posting another "Backfill — marking exempt" message
            # is just noise (the human has already moved on, the row won't
            # ping again, and "Reply 'log it' to override" invites churn).
            # Real-time handle_calendar_reply still posts a confirmation
            # because that's a fresh interaction worth acknowledging.
            summary["exempted"] += 1
            logger.info(f"Backfill exempted (silent): {retailer} ({region}) — {snippet!r}")
            continue

        # Bucket A — re-verify the calendar before trusting "done".
        bucket_a_text = next(
            (t for t in replies if classify_calendar_reply(t) == "A"),
            None,
        )
        if bucket_a_text is not None:
            start_str = promo.get("Start Date", "")
            end_str = promo.get("End Date", "")
            match_desc = _find_calendar_match(
                retailer, region, start_str, end_str,
                calendar_values=calendar_values,
            )
            if match_desc:
                _mark_logged_in_calendar(
                    thread_ts,
                    source=f"auto after reply (backfill): {match_desc}",
                )
                summary["confirmed"] += 1
                logger.info(f"Backfill confirmed: {retailer} ({region})")
                continue

        summary["noop"] += 1

    logger.info(f"Backfill calendar reply scan complete: {summary}")
    return summary


# Inline `actuals N` regex — matches "actuals 0", "actuals: 25", "actual 379".
# Must accept 0 (POC sometimes posts "actuals 0" to mean "promo went live but
# sold zero" — caught 2026-05-13 with PersonJ on Welltech UK).
_ACTUALS_INLINE_RE = re.compile(r"\bactuals?[:\s]+(\d+)\b", re.IGNORECASE)

# Bare-count form — the ENTIRE trimmed message is a number, optionally
# followed by a unit word ("80", "80 units", "80 rings", "80 pcs", "80 pieces").
# Anchored with ^...$ on purpose: prose numbers must NOT match — "70 units at
# bulkclub", "we sold 30 last week", "236 units to breakeven", "around 50",
# "22% off" all carry extra text and fall through. Only a message that is
# *nothing but* the count qualifies.
# Added 2026-07-23: POCs (and PersonC/PersonA on the ALPENHAUS chase) kept replying
# "80 units" and the strict `actuals N`-only parser ignored it, so the daily
# chase re-pinged forever. The 4 guards in main.py (bot-self / row exists /
# Status==Approved / idempotency) still gate every write.
_ACTUALS_BARE_RE = re.compile(
    r"^\s*(\d+)\s*(?:units?|rings?|pcs\.?|pieces?)?\s*$", re.IGNORECASE
)


def parse_actuals_from_reply(text: str) -> "int | None":
    """Extract an actuals unit count from a thread reply.

    Two accepted forms:
      1. Inline `actuals N` anywhere in the text ("actuals 379", "actual: 25").
      2. A bare count that is the WHOLE message ("80", "80 units", "80 rings").

    Returns the int, or None if neither form matches. Shared by the live inline
    handler (app/main.py) and the backfill scanner below so both honor the same
    reply shapes. Callers still apply the bot-self / Approved / idempotency
    guards before writing anything.
    """
    if not text:
        return None
    for pattern, method in ((_ACTUALS_INLINE_RE, "search"), (_ACTUALS_BARE_RE, "match")):
        m = getattr(pattern, method)(text)
        if m:
            try:
                return int(m.group(1))
            except (ValueError, TypeError):
                return None
    return None


def _build_actuals_confirmation(units: int, predicted: int) -> str:
    """Confirmation reply for actuals logged. Shows prediction vs actual
    + variance % so the thread is self-documenting (Approver's ask
    2026-05-15: "human readable on slack").

    Used by both the inline handler in app/main.py AND the backfill
    scanner here in app/tracker.py so the confirmation surface is
    identical across paths.
    """
    if predicted and predicted > 0:
        variance = ((units - predicted) / predicted) * 100
        return (
            f":bar_chart: Actuals logged: *{units}* units.\n"
            f"   Predicted: {predicted} → Variance: {variance:+.0f}%"
        )
    return f":bar_chart: Actuals logged: *{units}* units."


def backfill_actuals_reply_scan(slack_client) -> dict:
    """One-shot scan of historical replies on Approved promos with empty
    Actual Units and a past End Date. Catches `actuals N` replies that
    landed BEFORE the inline-actuals path shipped (or before a particular
    deploy) so the next reminder cron honors them instead of re-pinging.

    Symptom this fixes (2026-05-13): PersonJ replied `actuals 0` on the Wired
    Health (UK) approval thread at 4:47 PM. Bot was running OLD code at
    that moment so log_actuals was never called. Today's cron pings the
    same thread the next morning. With this scanner the cron back-fills
    PersonJ's reply on the next tick and stops the chase.

    For every Approved row where:
      - Actual Units is empty AND
      - End Date parses + is in the past
    we walk thread replies (skipping bot's own), match via
    `parse_actuals_from_reply` (inline `actuals N` OR a bare "80" / "80 units"
    whole-message count), and pick the MOST RECENT human match. log_actuals()
    handles the write + accuracy calc.

    Idempotent: rows where Actual Units is already filled are skipped
    entirely. Slack failures are logged + skipped — never crash the cron.

    Returns {"logged": N, "scanned": N, "no_match": N}.
    """
    summary = {"logged": 0, "scanned": 0, "no_match": 0}

    try:
        promos = get_promos_needing_reminders()
    except Exception:
        logger.exception("backfill_actuals_reply_scan: failed to load promos")
        return summary

    bot_user_id = None
    try:
        info = slack_client.auth_test()
        bot_user_id = info.get("user_id")
    except Exception as e:
        logger.warning(f"backfill_actuals_reply_scan: auth_test failed: {e}")

    today = date.today()

    for promo in promos:
        # Idempotency: actuals already filled → done.
        actual_raw = str(promo.get("Actual Units", "")).strip()
        if actual_raw and actual_raw != "-":
            continue

        # End date must parse + be in the past (no point chasing a future
        # promo's actuals reply).
        end_raw = promo.get("End Date", "")
        end_d = _parse_input_date(end_raw, end_of_month=True)
        if not end_d or end_d >= today:
            continue

        retailer = promo.get("Retailer", "")
        region = promo.get("Region", "")
        thread_ts = str(promo.get("Thread TS", "")).strip()

        channel_id, post_thread_ts, is_wa = _resolve_fyi_target(promo)
        if is_wa or not channel_id or not post_thread_ts:
            continue

        summary["scanned"] += 1

        try:
            resp = slack_client.conversations_replies(
                channel=channel_id, ts=post_thread_ts,
            )
            messages = resp.get("messages", []) or []
        except Exception as e:
            logger.warning(
                f"backfill_actuals_reply_scan: conversations_replies failed for "
                f"{retailer} ({region}) channel={channel_id} ts={post_thread_ts}: {e}"
            )
            continue

        # Walk replies newest-last (Slack returns oldest-first), skip bot
        # posts + the parent. Pick the LAST (most recent) `actuals N` hit
        # so a follow-up correction wins over the original number.
        match_units = None
        for msg in messages:
            msg_ts = msg.get("ts", "")
            if msg_ts == post_thread_ts:
                continue
            if _is_bot_reply(msg, bot_user_id):
                continue
            text = msg.get("text", "") or ""
            parsed = parse_actuals_from_reply(text)
            if parsed is not None:
                match_units = parsed

        if match_units is None:
            summary["no_match"] += 1
            continue

        # Hand off to log_actuals — same write path the live inline handler uses.
        try:
            result = log_actuals(thread_ts, match_units)
        except Exception as e:
            logger.warning(
                f"backfill_actuals_reply_scan: log_actuals failed for "
                f"{retailer} ({region}) thread={thread_ts} units={match_units}: {e}"
            )
            continue
        if result is None:
            logger.warning(
                f"backfill_actuals_reply_scan: log_actuals returned None for "
                f"{retailer} ({region}) thread={thread_ts}"
            )
            continue
        summary["logged"] += 1
        logger.info(
            f"Backfill actuals logged: {retailer} ({region}) units={match_units} "
            f"thread={thread_ts}"
        )

        # Post a :bar_chart: confirmation reply matching the inline handler's
        # surface (Approver 2026-05-15: "human readable on slack"). Failures
        # are logged but never stop the scan — the sheet write succeeded.
        try:
            confirm_msg = _build_actuals_confirmation(
                units=match_units,
                predicted=int(result.get("predicted_units") or 0),
            )
            slack_client.chat_postMessage(
                channel=channel_id,
                thread_ts=post_thread_ts,
                text=confirm_msg,
            )
        except Exception as e:
            logger.warning(
                f"backfill_actuals_reply_scan: confirmation post failed for "
                f"{retailer} ({region}) thread={thread_ts}: {e}"
            )

    logger.info(f"Backfill actuals reply scan complete: {summary}")
    return summary


def get_promo_by_thread_ts(thread_ts: str) -> dict | None:
    """Lookup a Promo Tracker row by Slack Thread TS. Tries both Thread TS
    (eval thread) and Approval Thread TS (where Approver approved). Returns
    None if not found in either column.

    Bug 2026-05-15 dual-review: previously only matched Thread TS, so a
    POC reply landing in the approver-approvals-XXX channel (where the
    Approval Thread TS lives) silently no-op'd actuals/snooze/calendar
    handling.
    """
    if not thread_ts:
        return None
    try:
        client = _get_client()
        sheet = client.open_by_key(PROMO_SHEET_ID)
        ws = _ensure_tracker_tab(sheet)
        ts_str = str(thread_ts).strip()
        for r in ws.get_all_records():
            if str(r.get("Thread TS", "")).strip() == ts_str:
                return r
            if str(r.get("Approval Thread TS", "")).strip() == ts_str:
                return r
        return None
    except Exception as e:
        logger.warning(f"get_promo_by_thread_ts failed: {e}")
        return None


def _date_today():
    from datetime import date as _date
    return _date.today()


def _set_actuals_snooze(thread_ts: str, snooze_until) -> bool:
    if not thread_ts:
        return False
    try:
        client = _get_client()
        sheet = client.open_by_key(PROMO_SHEET_ID)
        ws = _ensure_tracker_tab(sheet)
        all_rows = ws.get_all_records()
        for i, r in enumerate(all_rows):
            if str(r.get("Thread TS", "")).strip() == thread_ts:
                col_idx = _ensure_col(ws, ACTUALS_SNOOZE_COL)
                ws.update_cell(i + 2, col_idx, snooze_until.isoformat())
                return True
        return False
    except Exception as e:
        logger.warning(f"_set_actuals_snooze failed: {e}")
        return False


def handle_actuals_reply(
    slack_client,
    thread_ts: str,
    channel_id: str,
    reply_text: str,
    promo_row: dict,
) -> str:
    """Process a thread reply for the actuals chase.

    Order:
      1. Inline actuals (`actuals 379`) is handled in main.py BEFORE this is called.
      2. Try to parse a snooze date from the text.
      3. If found → write to ACTUALS_SNOOZE_COL, ack with date.
      4. If not found but vague-ack → default snooze (3 days), ack.
      5. Otherwise no-op.

    Returns: "snoozed_specific" / "snoozed_default" / "noop".
    """
    today = _date_today()
    snooze_until = parse_actuals_snooze(reply_text, today=today)
    if snooze_until:
        outcome = "snoozed_specific"
    else:
        snooze_until = parse_actuals_snooze(
            reply_text, today=today, default_if_vague=True,
        )
        outcome = "snoozed_default" if snooze_until else "noop"

    if not snooze_until:
        return "noop"

    _set_actuals_snooze(thread_ts, snooze_until)
    retailer = promo_row.get("Retailer", "")
    region = promo_row.get("Region", "")
    try:
        slack_client.chat_postMessage(
            channel=channel_id, thread_ts=thread_ts,
            text=(
                f":zzz: Got it — snoozing actuals reminders for "
                f"*{retailer} ({region})* until {snooze_until.isoformat()}. "
                f"Will re-ping then if actuals still not logged."
            ),
        )
    except Exception as e:
        logger.warning(f"snooze ack post failed: {e}")
    return outcome


def send_calendar_logged_reminder(
    slack_client,
    row: dict,
    calendar_values: list[list[str]] | None = None,
) -> bool:
    """Daily reminder: if Status=Approved and Logged In Calendar At is blank,
    AND the Promotions Calendar sheet has no overlapping entry, ping calendar
    keepers + regional POC + submitter in the approval thread to log the promo.

    NEW (2026-05-13): before pinging, the bot reads the Promotions Calendar
    directly. If an entry is already there (overlapping date range, populated
    Name/Retail in the right country column), the bot auto-fills Logged In
    Calendar At and SKIPS the ping. This prevents the bot from lying
    ("not showing up") when humans have already updated the calendar.

    Tag list (per 2026-05-13 spec): calendar keepers (PersonN/PersonO/PersonP) +
    regional retail POC for the row's Region + submitter. Approver is dropped
    here — calendar logging is owned by the keepers + regional POC, not him.

    `calendar_values` is an optional pre-fetched copy of the Promotions
    Calendar tab so a cron sweep can avoid re-reading the sheet for every
    Approved row.

    Returns True if posted."""
    if (row.get(LOGGED_IN_CAL_COL) or "").strip():
        return False

    # Marketing Type gate — calendar tracks consumer promos/discounts only.
    # Per 2026-05-14 user rule: "Promo calendar includes ONLY promos/discounts."
    # SPA+SBA Camp, SOA, Sales Contest, Influencer Code, Promoter Trial, etc.
    # don't belong here and shouldn't get calendar pings.
    marketing_type = (row.get("Marketing Type") or "").strip()
    discount_pct_raw = row.get("Discount %", "")
    if not _should_send_calendar_reminder(marketing_type, discount_pct_raw):
        retailer_skip = row.get("Retailer", "")
        region_skip = row.get("Region", "")
        logger.info(
            f"Calendar reminder skipped for {retailer_skip} ({region_skip}) — "
            f"Marketing Type {marketing_type!r} (discount_pct={discount_pct_raw!r}) "
            f"doesn't belong on Promo Calendar"
        )
        return False

    # Check the actual Promotions Calendar before pinging.
    retailer_check = row.get("Retailer", "")
    region_check = row.get("Region", "")
    start_check = row.get("Start Date", "")
    end_check = row.get("End Date", "")
    if retailer_check and region_check and start_check and end_check:
        match_desc = _find_calendar_match(
            retailer_check, region_check, start_check, end_check,
            calendar_values=calendar_values,
        )
        if match_desc:
            thread_ts_check = str(row.get("Thread TS", "")).strip()
            # Source string includes the matched cell text so it shows up
            # in `Logged In Calendar At`. Wrong-year mis-matches are then
            # spottable by eye in the sheet.
            if _mark_logged_in_calendar(
                thread_ts_check, source=f"auto: {match_desc}",
            ):
                logger.info(
                    f"Auto-marked Logged In Calendar At for {retailer_check} ({region_check}) — "
                    f"matched {match_desc!r}, skipping reminder"
                )
            return False

    retailer = row.get("Retailer", "")
    region = row.get("Region", "")
    submitter = (row.get("Submitter") or "").strip()
    submitter_uid = SUBMITTER_SLACK_IDS.get(submitter)

    channel_id, thread_ts, is_wa = _resolve_fyi_target(row)
    if not channel_id or not thread_ts or is_wa:
        return False  # thread-only policy

    msg = _build_calendar_reminder_message(retailer, region, submitter_uid)
    try:
        slack_client.chat_postMessage(
            channel=channel_id, thread_ts=thread_ts, text=msg,
        )
        return True
    except Exception as e:
        logger.error(f"Calendar-logged reminder failed for {retailer}: {e}")
        return False


def backfill_calendar_fyis(slack_client) -> int:
    """One-shot at bot startup: post the calendar-keepers FYI for every
    Approved promo that hasn't been pinged yet. The Calendar FYI Sent At
    column prevents re-fires across restarts."""
    sent = 0
    try:
        client = _get_client()
        sheet = client.open_by_key(PROMO_SHEET_ID)
        ws = _ensure_tracker_tab(sheet)
        _ensure_col(ws, CALENDAR_FYI_COL)
        _ensure_col(ws, APPROVAL_CHAN_COL)
        _ensure_col(ws, APPROVAL_TS_COL)
        _ensure_col(ws, LOGGED_IN_CAL_COL)
        _ensure_col(ws, T7_PINGED_AT_COL)
        _ensure_col(ws, T7_TAGGED_COL)
        _ensure_col(ws, ACTUALS_SNOOZE_COL)
        rows = ws.get_all_records()
        for i, row in enumerate(rows, start=2):
            if row.get("Status") != "Approved":
                continue
            if send_calendar_fyi(slack_client, row, i, ws=ws):
                sent += 1
    except Exception:
        logger.exception("backfill_calendar_fyis failed")
    logger.info(f"Calendar FYI backfill complete: {sent} pings sent")
    return sent


def check_and_send_reminders(
    slack_client,
    calendar_values: list[list[str]] | None = None,
) -> int:
    """Daily scan: for every Approved promo whose end date is ≥7 days ago and
    Actual Units is still blank, ping the *submitter* (only) for actuals.
    Approver is intentionally NOT mentioned on this reminder — actuals are the
    submitter's responsibility, not the approver's.

    - Slack-source rows: thread reply on the original eval thread_ts.
    - WhatsApp-source rows: skipped (no Slack thread to reply in).
    - Skips row once Actual Units is populated (POC filling the sheet = ACK).
    - Reminds EVERY daily scan from day 7 onward until actuals arrive — no
      fixed 0/7/14 cadence.
    """
    from app.config import PROMO_CHANNEL_ID

    sent = 0
    today = datetime.utcnow().date()

    promos = get_promos_needing_reminders()
    logger.info(f"Reminder check: {len(promos)} approved promos with end dates")

    # Guard against calendar-verify fail-open. When load_calendar_values()
    # returns None (sheet not shared with bot's service account, gspread auth
    # failure, network), the calendar matcher returns None for EVERY row,
    # which silently turns into a "still not showing up" ping for every
    # approved promo-MT row. Skip the calendar pillar entirely on this tick
    # and log once instead of spamming keepers. Fix the root cause (sheet
    # share) before re-enabling.
    calendar_available = bool(calendar_values)
    if not calendar_available:
        logger.warning(
            "Calendar values unavailable (sheet load failed). Skipping all "
            "calendar-verify pings this tick to avoid false-positive 'still "
            "not showing up' spam. Fix: share Promotions Calendar with the "
            "bot's GCP service account (Viewer role)."
        )

    for promo in promos:
        end_raw = promo.get("End Date", "")
        start_raw = promo.get("Start Date", "")
        thread_ts = str(promo.get("Thread TS", ""))
        channel_label = promo.get("Channel", "")
        retailer = promo.get("Retailer", "")
        region = promo.get("Region", "")
        submitter_name = promo.get("Submitter", "")

        submitter_uid = SUBMITTER_SLACK_IDS.get(submitter_name.strip())
        is_wa = (
            channel_label == "WhatsApp"
            or not thread_ts
            or thread_ts.startswith("WA-")
        )

        # Resolve which channel the eval thread actually lives in. Some rows
        # were raised directly in the approver-approvals channel rather than
        # #claude-marketing-approvals, so PROMO_CHANNEL_ID isn't always the
        # right post target. _resolve_fyi_target prefers the row's Approval
        # Channel + Thread TS when set, falls back to PROMO_CHANNEL_ID.
        post_channel, post_thread_ts, _ = _resolve_fyi_target(promo)
        if not post_thread_ts:
            post_thread_ts = thread_ts  # honor original Thread TS if helper had none

        def _post(msg_text: str) -> bool:
            # Thread-only policy: WhatsApp-sourced rows have no Slack thread,
            # so skip them entirely rather than posting top-level in the channel.
            if is_wa or not post_thread_ts or not post_channel:
                return False
            try:
                slack_client.chat_postMessage(
                    channel=post_channel,
                    thread_ts=post_thread_ts,
                    text=msg_text,
                )
                return True
            except Exception as e:
                logger.error(
                    f"Failed to send reminder for {retailer} "
                    f"(channel={post_channel}, thread_ts={post_thread_ts}): {e}"
                )
                return False

        # ---- Calendar-logged reminder (daily until POC fills Logged In Calendar At) ----
        # Gated by calendar_available — see top-of-function guard. Prevents
        # bot from firing "still not showing up" on every row when the
        # calendar sheet itself isn't readable.
        if calendar_available and send_calendar_logged_reminder(
            slack_client, promo, calendar_values=calendar_values,
        ):
            sent += 1
            logger.info(f"Calendar-logged reminder: {retailer} ({region})")

        # ---- T-7 upcoming-promo reminder (fires exactly 7 days before start) ----
        # Use _parse_input_date — it anchors ambiguous dates to current year
        # so 'Jun 7' resolves to 2026-06-07, not 2025-06-07. T-7 was the only
        # remaining naked dateparser caller in this loop.
        start_date_obj = _parse_input_date(start_raw)
        if start_date_obj:
            days_until_start = (start_date_obj - today).days
            if days_until_start == 7:
                # Idempotency guard (Bug 2026-05-15 dual-review): if
                # T-7 Pinged At already carries today's date, this is
                # a same-day cron retry — skip to avoid duplicate ping.
                # _record_t7_pinged writes ISO with a "Z" suffix.
                t7_ping_ts = str(promo.get(T7_PINGED_AT_COL, "")).strip()
                already_pinged_today = False
                if t7_ping_ts:
                    try:
                        from datetime import datetime as _dt
                        t7_dt = _dt.fromisoformat(t7_ping_ts.rstrip("Z")).date()
                        if t7_dt >= today:
                            already_pinged_today = True
                    except Exception:
                        pass
                if already_pinged_today:
                    logger.info(
                        f"T-7 already pinged for {retailer} ({region}) at "
                        f"{t7_ping_ts} — skipping duplicate same-day fire"
                    )
                else:
                    from app.regional_pocs import get_regional_poc_ids
                    mention_uids: list[str] = list(CALENDAR_KEEPER_IDS)
                    for uid in get_regional_poc_ids(region):
                        if uid and uid not in mention_uids:
                            mention_uids.append(uid)
                    if submitter_uid and submitter_uid not in mention_uids:
                        mention_uids.append(submitter_uid)
                    msg = _build_t7_message(
                        retailer=retailer,
                        start_str=start_raw,
                        end_str=end_raw,
                        mention_uids=mention_uids,
                    )
                    if _post(msg):
                        sent += 1
                        _record_t7_pinged(promo, mention_uids)
                        logger.info(
                            f"T-7 reminder: {retailer} ({region}), starts {start_raw}, "
                            f"tagged={len(mention_uids)} ids"
                        )

        # ---- T-1 reminder (fires once when start_date - today == 1) ----
        if start_date_obj and (start_date_obj - today).days == 1:
            t7_ping_ts = str(promo.get(T7_PINGED_AT_COL, "")).strip()
            t7_tagged_str = str(promo.get(T7_TAGGED_COL, "")).strip()
            if t7_ping_ts and t7_tagged_str:
                t7_tagged = t7_tagged_str.split()
                from datetime import datetime as _dt
                try:
                    t7_dt = _dt.fromisoformat(t7_ping_ts.rstrip("Z"))
                    t7_slack_ts = f"{t7_dt.timestamp():.6f}"
                except Exception:
                    t7_slack_ts = ""
                silent = _compute_t1_tag_list(
                    t7_tagged, t7_slack_ts, post_channel,
                    slack_client=slack_client,
                    thread_ts=str(promo.get("Thread TS", "")).strip(),
                )
                if silent:
                    msg = _build_t1_message(
                        retailer=retailer, start_str=start_raw,
                        end_str=end_raw, mention_uids=silent,
                    )
                    if _post(msg):
                        sent += 1
                        logger.info(
                            f"T-1 reminder: {retailer} ({region}), "
                            f"silent_subset={len(silent)}/{len(t7_tagged)}"
                        )
                else:
                    logger.info(
                        f"T-1 skipped for {retailer} ({region}) — "
                        f"all {len(t7_tagged)} T-7 taggees replied"
                    )

        # ---- Post-end actuals chase (daily from day 7 until Actual Units filled) ----
        actual_raw = str(promo.get("Actual Units", "")).strip()
        if actual_raw and actual_raw != "-":
            continue  # POC already filled actuals — done.

        # Skip promo types that don't get units-based actuals (POS, Display).
        # These have different lifecycles (months-to-breakeven, etc.) and pinging
        # for "Actual Units" makes no sense. Training IS chased — see
        # _should_chase_actuals docstring.
        marketing_type = str(promo.get("Marketing Type", "")).strip()
        if not _should_chase_actuals(marketing_type):
            logger.info(
                f"Skipping actuals chase for {retailer} ({region}) — Marketing Type "
                f"{marketing_type!r} is not a unit-tracked promo."
            )
            continue

        # PRIMARY GATE: read the user-maintained "Is it completed?" column.
        # Formula: =IF(B="","",IF(K<TODAY(),"Y","N")). When this is "Y" the
        # promo has ended and we should chase. Anything else (N, blank, or
        # missing column) → skip. This replaces brittle dateparser-based
        # date logic for the gating decision; we still parse end_date below
        # to compute T+7 / T+14 thresholds and ages.
        # Tolerate both header spellings (with/without ?). Sheet uses
        # "Is it completed?". Bug 2026-05-15: code was reading the no-?
        # spelling, so dict.get() returned "" for every row → gate was
        # silently no-op and ALL rows were skipped.
        is_completed = str(
            promo.get("Is it completed?", promo.get("Is it completed", ""))
        ).strip().upper()
        if is_completed != "Y":
            continue

        # End date still needed for T+7 / T+14 escalation thresholds.
        # _parse_input_date prefers ISO YYYY-MM-DD (the tracker's storage
        # format) so we don't get burned by dateparser's PREFER_DATES_FROM
        # walking "2026-06-07" back to 2025. Same fix as _is_logged_in_calendar.
        # end_of_month=True so "Mar 2026" → Mar 31 (not Mar 1) — Byteport
        # bug 2026-05-14: bot said "ended 74 days ago" instead of ~44.
        end_date_obj = _parse_input_date(end_raw, end_of_month=True)
        if not end_date_obj:
            # Completed=Y but end date didn't parse — sheet has a text-only
            # date like "May 22" or "Jun 7" without year. Don't fire a
            # garbage ping; log so submitter can fix the cell.
            logger.warning(
                f"actuals chase: {retailer} ({region}) marked completed=Y but "
                f"End Date {end_raw!r} won't parse — please fix to ISO YYYY-MM-DD"
            )
            continue
        # 2026-05-15 audit: Rheinshop Europe row 21 had form End=2026-04-14
        # before Start=2026-05-06 (submitter typo). Skip the chase rather
        # than ping "promo ended 31 days ago" off the wrong endpoint.
        start_date_obj = _parse_input_date(start_raw)
        if start_date_obj and end_date_obj < start_date_obj:
            logger.warning(
                f"actuals chase: {retailer} ({region}) has End Date "
                f"{end_raw!r} BEFORE Start Date {start_raw!r} — submitter "
                f"form typo. Skipping to avoid garbage post-end ping."
            )
            continue
        days_since_end = (today - end_date_obj).days
        if days_since_end < 0:
            # Completed=Y but end date is in future — formula likely tripped
            # by text-vs-number comparison (text > number is always TRUE in
            # Sheets). Skip rather than ping garbage.
            logger.warning(
                f"actuals chase: {retailer} ({region}) marked completed=Y but "
                f"End Date {end_raw!r} resolves to future ({end_date_obj}) — "
                f"likely text-format date in column K, please fix"
            )
            continue
        if days_since_end < 7:
            continue  # too soon — wait until at least 7 days post-end

        # Snooze check — Task 9 will add the column-write logic; for now we just
        # read whatever is in the column and skip if it's a future date. The
        # column may not exist yet in the sheet, in which case .get() returns "".
        snooze_until = str(promo.get("Actuals Snooze Until", "")).strip()
        if snooze_until:
            try:
                # Use _parse_input_date so 'Jun 7' is current-year, not last-year.
                snooze_d = _parse_input_date(snooze_until)
                if snooze_d and snooze_d > today:
                    logger.info(
                        f"Skipping actuals — {retailer} ({region}) snoozed until {snooze_until}"
                    )
                    continue
            except Exception:
                pass

        # Same-day idempotency guard. The actuals chase has no fixed
        # cadence — it fires every sweep from day 7 until Actual Units is
        # filled — so it relies on the sweep running once per day. If a
        # second run happens the same day (duplicate CronJob tick, manual
        # /reminders/trigger, k8s job retry) an un-guarded chase re-pings
        # every open promo with a byte-identical message. Skip when
        # Actuals Chase Sent At already carries today's date. The stamp is
        # written by _record_actuals_chased after a successful post.
        chase_ts = str(promo.get(ACTUALS_CHASE_COL, "")).strip()
        if chase_ts:
            try:
                from datetime import datetime as _dt
                if _dt.fromisoformat(chase_ts.rstrip("Z")).date() >= today:
                    logger.info(
                        f"actuals chase already sent today for {retailer} "
                        f"({region}) at {chase_ts} — skipping duplicate fire"
                    )
                    continue
            except Exception:
                pass

        # Tag list: regional POC for the Region only.
        # 2026-05-15 user override: "Let's not tag Approver till we get the
        # logic right." The T+14 Approver escalation is disabled here —
        # regional POC stays the sole tagged owner at every T+ value.
        # Reinstate by re-adding the APPROVER_USER_ID branch below once the
        # escalation logic is finalized.
        from app.regional_pocs import get_regional_poc_ids
        mention_uids = list(get_regional_poc_ids(region))
        # --- TEMPORARILY DISABLED 2026-05-15 ---
        # from app.config import APPROVER_USER_ID as _APPROVER_UID
        # if days_since_end >= 14 and _APPROVER_UID and _APPROVER_UID not in mention_uids:
        #     mention_uids.append(_APPROVER_UID)
        # ----------------------------------------
        if not mention_uids:
            logger.warning(
                f"No regional POC for {retailer} ({region}) — actuals chase has nobody to tag"
            )
            continue

        try:
            predicted_units = int(str(promo.get("Predicted Units", "0")).strip() or "0")
        except ValueError:
            predicted_units = 0

        msg = _build_actuals_message(
            retailer=retailer, region=region,
            days_since_end=days_since_end,
            predicted_units=predicted_units,
            mention_uids=mention_uids,
        )
        if _post(msg):
            sent += 1
            _record_actuals_chased(promo)
            logger.info(
                f"actuals reminder: {retailer} ({region}), {days_since_end}d post-end, "
                f"tags={len(mention_uids)} approver_added=False (disabled 2026-05-15)"
            )

    return sent


def start_reminder_scheduler(slack_client):
    """Boot-time calendar FYI backfill — fires once ~30s after pod start so
    newly-approved retail promos get pinged without waiting for the daily
    cron tick. The daily reminder sweep itself is now driven by a k8s
    CronJob (app.reminders_cron) — see helm/marketing-bot/templates/cronjob.yaml.
    The previous threading.Timer scheduler died on every pod restart and
    repeatedly missed reminder ticks; cron is the durable home.
    """
    import threading

    def _run_backfill():
        try:
            backfill_calendar_fyis(slack_client)
        except Exception:
            logger.exception("Calendar FYI backfill tick failed")

    backfill_timer = threading.Timer(30, _run_backfill)
    backfill_timer.daemon = True
    backfill_timer.start()
    logger.info("Calendar FYI backfill scheduled (T+30s); daily reminders run via CronJob")
