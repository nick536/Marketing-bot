#!/usr/bin/env python3
"""Explain, per Approved row, whether the post-end ACTUALS CHASE reminder
(check_and_send_reminders, app/tracker.py ~L2570-2697) would fire RIGHT NOW
— and if not, the exact gate that filtered it out.

This mirrors the bot's gate sequence exactly using the bot's own helpers.
Read-only. Posts nothing.

Usage:
  python3 scripts/dry_run/why_actuals_chase.py
"""
import os
import sys
from pathlib import Path
from datetime import date

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ.setdefault("SLACK_BOT_TOKEN", "stub")
os.environ.setdefault("SLACK_SIGNING_SECRET", "stub")
os.environ.setdefault("GOOGLE_SERVICE_ACCOUNT_JSON", "{}")
os.environ.setdefault("PROMO_SHEET_ID", "stub")
os.environ.setdefault("PL_SHEET_ID", "stub")
os.environ.setdefault("SELLOUT_SHEET_ID", "stub")

from app.tracker import _parse_input_date, _should_chase_actuals  # noqa: E402
from app.regional_pocs import get_regional_poc_ids  # noqa: E402

PROMO_SHEET = "REDACTED_SHEET_ID_2"
TRACKER_TAB = "Marketing Tracker"


def load_tracker() -> list[dict]:
    from google.auth.transport.requests import Request as GReq
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    token_file = Path.home() / ".gmail-mcp" / "sheets_token.json"
    creds = Credentials.from_authorized_user_file(
        str(token_file), ["https://www.googleapis.com/auth/spreadsheets"])
    if creds.expired and creds.refresh_token:
        creds.refresh(GReq())
    svc = build("sheets", "v4", credentials=creds)
    rows = svc.spreadsheets().values().get(
        spreadsheetId=PROMO_SHEET, range=f"'{TRACKER_TAB}'").execute().get("values", [])
    if not rows:
        return []
    hdr = rows[0]
    return [{h: (r[i] if i < len(r) else "") for i, h in enumerate(hdr)} for r in rows[1:]]


def verdict(p: dict, today: date):
    """Return (fires: bool, reason: str) mirroring check_and_send_reminders."""
    status = (p.get("Status") or "").strip()
    end_raw = (p.get("End Date") or "").strip()
    start_raw = (p.get("Start Date") or "").strip()

    # get_promos_needing_reminders filter
    if status != "Approved":
        return False, f"Status={status!r} (not Approved)"
    if not end_raw:
        return False, "no End Date"

    # _post viability (is_wa / thread)
    thread_ts = str(p.get("Thread TS") or "").strip()
    chan = (p.get("Channel") or "").strip()
    appr_ts = str(p.get("Approval Thread TS") or "").strip()
    has_thread = bool(appr_ts or (thread_ts and not thread_ts.startswith("WA-")))
    is_wa = chan == "WhatsApp" or not has_thread
    if is_wa:
        return False, "WhatsApp-sourced / no Slack thread (_post skips)"

    # actuals already filled
    actual_raw = str(p.get("Actual Units") or "").strip()
    if actual_raw and actual_raw != "-":
        return False, f"Actual Units already filled ({actual_raw!r})"

    # marketing-type gate
    mt = str(p.get("Marketing Type") or "").strip()
    if not _should_chase_actuals(mt):
        return False, f"Marketing Type {mt!r} not unit-tracked (POS/Display)"

    # PRIMARY GATE: Is it completed?
    is_completed = str(
        p.get("Is it completed?", p.get("Is it completed", ""))).strip().upper()
    if is_completed != "Y":
        return False, f"'Is it completed?'={is_completed!r} (not Y)"

    end_d = _parse_input_date(end_raw, end_of_month=True)
    if not end_d:
        return False, f"End Date {end_raw!r} won't parse"
    start_d = _parse_input_date(start_raw)
    if start_d and end_d < start_d:
        return False, f"End {end_raw!r} before Start {start_raw!r} (form typo)"
    dse = (today - end_d).days
    if dse < 0:
        return False, f"End Date resolves to future ({end_d})"
    if dse < 7:
        return False, f"ended only {dse}d ago (<7, too soon)"

    snooze = str(p.get("Actuals Snooze Until") or "").strip()
    if snooze:
        sd = _parse_input_date(snooze)
        if sd and sd > today:
            return False, f"snoozed until {snooze}"

    pocs = list(get_regional_poc_ids((p.get("Region") or "").strip()))
    if not pocs:
        return False, f"no regional POC for Region {p.get('Region')!r}"

    return True, f"FIRES — ended {dse}d ago, tags {len(pocs)} POC(s)"


def main():
    today = date.today()
    rows = load_tracker()
    print(f"\nACTUALS-CHASE DRY RUN · {today} · {len(rows)} tracker rows\n" + "=" * 78)
    fires, skips = [], []
    for p in rows:
        if (p.get("Status") or "").strip() != "Approved":
            continue
        f, why = verdict(p, today)
        label = f"{p.get('Retailer','?')} [{p.get('Region','?')}] " \
                f"end={p.get('End Date','')!r} MT={p.get('Marketing Type','')!r}"
        (fires if f else skips).append((label, why))
    print(f"\n🔥 WOULD FIRE ({len(fires)}):")
    for lbl, why in fires:
        print(f"  • {lbl}\n      {why}")
    print(f"\n⏭  SKIPPED ({len(skips)}):")
    for lbl, why in skips:
        print(f"  • {lbl}\n      → {why}")


if __name__ == "__main__":
    main()
