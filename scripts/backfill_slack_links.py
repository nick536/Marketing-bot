"""One-shot: rewrite legacy Slack /archives/ permalinks in the Promo Tracker
to the new app_redirect format so they open the desktop app from Sheets.

Reads every row, parses the existing HYPERLINK in the Thread TS column,
extracts channel + ts, writes back the new HYPERLINK with the same ts as
display text.

Idempotent: rows already using app_redirect URLs are skipped.

Usage (DRY RUN — prints what would change, makes no writes):
    GOOGLE_SERVICE_ACCOUNT_JSON='...json...' \
    PROMO_SHEET_ID=... \
    PL_SHEET_ID=x SELLOUT_SHEET_ID=x SLACK_BOT_TOKEN=x SLACK_SIGNING_SECRET=x \
    SLACK_TEAM_ID=T00000000 \
    python3 scripts/backfill_slack_links.py --dry-run

LIVE (writes back):
    ... same env ... python3 scripts/backfill_slack_links.py --live
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.config import PROMO_SHEET_ID, SLACK_TEAM_ID
from app.tracker import _get_client, _ensure_tracker_tab

# Match the legacy permalink format inside a HYPERLINK formula:
# =HYPERLINK("https://example.slack.com/archives/<CHAN>/p<TS_NO_DOT>","<TS>")
LEGACY_RE = re.compile(
    r'=HYPERLINK\("https://[^/]+/archives/([A-Z0-9]+)/p(\d+)","([^"]+)"\)',
    re.IGNORECASE,
)
# Already-migrated form to skip:
APP_REDIRECT_RE = re.compile(r"slack\.com/app_redirect", re.IGNORECASE)


def _new_url(team_id: str, chan: str, ts: str) -> str:
    return (
        f"https://slack.com/app_redirect?team={team_id}"
        f"&channel={chan}&message_ts={ts}"
    )


def _new_formula(team_id: str, chan: str, ts: str) -> str:
    safe_ts = ts.replace('"', '""')
    return f'=HYPERLINK("{_new_url(team_id, chan, ts)}","{safe_ts}")'


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true", default=True,
                   help="Default: print only, no writes.")
    p.add_argument("--live", action="store_true",
                   help="Override --dry-run and actually write.")
    p.add_argument("--col", default="Thread TS",
                   help="Header name of the column to rewrite. Default 'Thread TS'.")
    args = p.parse_args()

    live = args.live
    if not live:
        print("== DRY RUN — no writes. Pass --live to commit changes.")

    if not SLACK_TEAM_ID:
        print("!! SLACK_TEAM_ID not set; cannot build app_redirect URLs.")
        sys.exit(1)
    print(f"== team={SLACK_TEAM_ID}  sheet={PROMO_SHEET_ID}")

    client = _get_client()
    sheet = client.open_by_key(PROMO_SHEET_ID)
    ws = _ensure_tracker_tab(sheet)

    # We need raw formula text, not rendered values.
    # gspread's `get_all_values(value_render_option='FORMULA')` returns formulas as text.
    formulas = ws.get_all_values(value_render_option="FORMULA")
    if not formulas:
        print("!! sheet empty"); return

    headers = formulas[0]
    try:
        col_idx_0 = headers.index(args.col)
    except ValueError:
        print(f"!! header {args.col!r} not found. Headers: {headers}")
        sys.exit(1)
    col_idx_1 = col_idx_0 + 1   # 1-based for update_cell

    rewrites = []
    skipped_already = 0
    skipped_empty = 0
    skipped_unparseable = 0

    for row_idx, row in enumerate(formulas[1:], start=2):  # 1-based row number; +1 for header
        if col_idx_0 >= len(row):
            skipped_empty += 1; continue
        cell = (row[col_idx_0] or "").strip()
        if not cell:
            skipped_empty += 1; continue
        if APP_REDIRECT_RE.search(cell):
            skipped_already += 1; continue
        m = LEGACY_RE.match(cell)
        if not m:
            # Could be a bare ts (no HYPERLINK), a WA-prefix one, or something else.
            # Skip unless it looks like a parseable ts.
            skipped_unparseable += 1
            continue
        chan, _ts_no_dot, ts = m.group(1), m.group(2), m.group(3)
        new = _new_formula(SLACK_TEAM_ID, chan, ts)
        if new == cell:
            skipped_already += 1; continue
        rewrites.append((row_idx, ts, chan, new))

    print(f"\n== Rows to rewrite: {len(rewrites)}")
    print(f"== Skipped (already app_redirect): {skipped_already}")
    print(f"== Skipped (empty cell):           {skipped_empty}")
    print(f"== Skipped (unparseable):          {skipped_unparseable}")

    # Sample first 5 rewrites
    print("\n-- sample rewrites --")
    for row_idx, ts, chan, new in rewrites[:5]:
        print(f"  row {row_idx}  ts={ts}  chan={chan}")
        print(f"    new: {new}")

    if not live:
        print("\n== Dry run complete. Re-run with --live to apply.")
        return

    print("\n== Writing changes…")
    for i, (row_idx, ts, chan, new) in enumerate(rewrites, start=1):
        try:
            ws.update_cell(row_idx, col_idx_1, new)
        except Exception as e:
            print(f"!! row {row_idx} write failed: {e}")
            continue
        if i % 20 == 0:
            print(f"  …{i}/{len(rewrites)}")
        time.sleep(1.1)  # gspread rate limit: ~60 writes/min
    print(f"\n== Done. Rewrote {len(rewrites)} rows.")


if __name__ == "__main__":
    main()
