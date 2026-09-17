#!/usr/bin/env python3
"""Verify what backfill_actuals_reply_scan would do RIGHT NOW.

Reads the Marketing Tracker, finds every Approved row with empty Actual
Units + parseable past End Date (same gate the bot's scanner uses), then
calls Slack's conversations.replies for each candidate thread and looks
for `actuals N` patterns from human (non-bot) users.

If a hit is found, the bot would:
  - log_actuals(thread_ts, N) -> writes Actual Units + recalcs Accuracy %
  - post a :bar_chart: confirmation in the thread

This script does NEITHER. It only reports what the bot would do.

Usage:
  export SLACK_TOKEN=xoxp-... or xoxb-...   # user or bot token
  python3 scripts/dry_run/verify_actuals_threads.py
"""
import os
import re
import sys
import json
import time
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError

# Reuse the bot's logic
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ.setdefault("SLACK_BOT_TOKEN", "stub")
os.environ.setdefault("SLACK_SIGNING_SECRET", "stub")
os.environ.setdefault("GOOGLE_SERVICE_ACCOUNT_JSON", "{}")
os.environ.setdefault("PROMO_SHEET_ID", "stub")
os.environ.setdefault("PL_SHEET_ID", "stub")
os.environ.setdefault("SELLOUT_SHEET_ID", "stub")

from app.tracker import _parse_input_date, _ACTUALS_INLINE_RE  # noqa: E402
from datetime import date  # noqa: E402

SLACK_TOKEN = os.environ.get("SLACK_TOKEN") or os.environ.get("SLACK_BOT_TOKEN_USER") or os.environ.get("SLACK_BOT_TOKEN")
if not SLACK_TOKEN or SLACK_TOKEN == "stub":
    print("ERROR: set SLACK_TOKEN (xoxp-user or xoxb-bot) in env first", file=sys.stderr)
    sys.exit(1)

PROMO_SHEET = "REDACTED_SHEET_ID_2"
TRACKER_TAB = "Marketing Tracker"


def slack_api(method: str, params: dict) -> dict:
    """Hit Slack Web API; raise on transport errors, return JSON."""
    qs = "&".join(f"{k}={v}" for k, v in params.items())
    url = f"https://slack.com/api/{method}?{qs}"
    req = Request(url, headers={"Authorization": f"Bearer {SLACK_TOKEN}"})
    with urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def load_marketing_tracker() -> list[dict]:
    """Pull Marketing Tracker via the same gsheets CLI auth path."""
    from google.auth.transport.requests import Request as GReq
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    token_file = Path.home() / ".gmail-mcp" / "sheets_token.json"
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_authorized_user_file(str(token_file), scopes)
    if creds.expired and creds.refresh_token:
        creds.refresh(GReq())
    svc = build("sheets", "v4", credentials=creds)
    resp = svc.spreadsheets().values().get(
        spreadsheetId=PROMO_SHEET, range=f"'{TRACKER_TAB}'",
    ).execute()
    rows = resp.get("values", [])
    if not rows:
        return []
    headers = rows[0]
    return [
        {h: (r[i] if i < len(r) else "") for i, h in enumerate(headers)}
        for r in rows[1:]
    ]


def resolve_post_target(row: dict) -> tuple[str, str]:
    """Mirror app.tracker._resolve_fyi_target priority for actuals scan."""
    approval_ch = (row.get("Approval Channel ID") or "").strip()
    approval_ts = (row.get("Approval Thread TS") or "").strip()
    if approval_ch and approval_ts:
        return approval_ch, approval_ts
    thread_ts = str(row.get("Thread TS") or "").strip()
    return "", thread_ts  # would fall back to PROMO_CHANNEL_ID env


def is_bot_message(msg: dict, bot_user_id: str) -> bool:
    """Mirror the bot's 5-layer _is_bot_reply check."""
    if msg.get("subtype") == "bot_message":
        return True
    if msg.get("bot_id"):
        return True
    if msg.get("app_id"):
        return True
    if bot_user_id and msg.get("user") == bot_user_id:
        return True
    if msg.get("bot_profile"):
        return True
    return False


def main():
    print(f"\n{'='*72}")
    print(f"BACKFILL ACTUALS REPLY SCAN — DRY RUN  ·  {date.today()}")
    print(f"{'='*72}\n")

    # auth_test → bot user id (the bot's, not us)
    auth = slack_api("auth.test", {})
    if not auth.get("ok"):
        print(f"auth.test failed: {auth}", file=sys.stderr)
        sys.exit(1)
    my_user = auth.get("user_id")
    is_bot_token = SLACK_TOKEN.startswith("xoxb-")
    print(f"Token type: {'bot' if is_bot_token else 'user'}  ·  user_id={my_user}")
    if not is_bot_token:
        # For user tokens the bot's identity differs. The bot's user_id is what
        # we want to filter on. Hardcoded from production logs.
        print("NOTE: using user token; bot identity inferred from message metadata only")
    print()

    promos = load_marketing_tracker()
    print(f"Loaded {len(promos)} tracker rows\n")

    today = date.today()
    candidates = []
    for p in promos:
        if (p.get("Status") or "").strip() != "Approved":
            continue
        actual_raw = str(p.get("Actual Units") or "").strip()
        if actual_raw and actual_raw != "-":
            continue
        end_raw = (p.get("End Date") or "").strip()
        end_d = _parse_input_date(end_raw, end_of_month=True)
        if not end_d or end_d >= today:
            continue
        ch, ts = resolve_post_target(p)
        if not ch or not ts or ts.startswith("WA-"):
            continue
        candidates.append({
            "retailer": p.get("Retailer", ""), "region": p.get("Region", ""),
            "mt": p.get("Marketing Type", ""), "end": end_raw,
            "channel": ch, "thread_ts": ts,
            "snooze": (p.get("Actuals Snooze Until") or "").strip(),
        })

    print(f"Candidate threads to scan: {len(candidates)}\n")
    for c in candidates:
        print(f"  • {c['retailer']} [{c['region']}] | MT={c['mt']} | end={c['end']} | ts={c['thread_ts']}")
        if c["snooze"]:
            print(f"    ⚠ Has Actuals Snooze Until={c['snooze']} (NOTE: scan ignores snooze!)")
    print()

    print("─" * 72)
    print("READING THREADS")
    print("─" * 72)

    would_fire = []
    no_match = []
    errors = []

    for c in candidates:
        print(f"\n• {c['retailer']} [{c['region']}]  ({c['channel']}/{c['thread_ts']})")
        try:
            resp = slack_api("conversations.replies", {
                "channel": c["channel"],
                "ts": c["thread_ts"],
                "limit": 200,
            })
        except HTTPError as e:
            print(f"    HTTP {e.code}: {e.read()[:200]!r}")
            errors.append((c, f"HTTP {e.code}"))
            continue
        except Exception as e:
            print(f"    ERROR: {e}")
            errors.append((c, str(e)))
            continue

        if not resp.get("ok"):
            err = resp.get("error", "unknown")
            print(f"    Slack error: {err}")
            errors.append((c, err))
            continue

        messages = resp.get("messages", [])
        parent_ts = c["thread_ts"]
        human_replies = []
        for msg in messages:
            if msg.get("ts") == parent_ts:
                continue  # parent
            if is_bot_message(msg, bot_user_id=""):
                continue
            text = msg.get("text", "") or ""
            if not text.strip():
                continue
            human_replies.append(msg)

        print(f"    {len(messages)} total msgs · {len(human_replies)} human replies")

        # Find LAST actuals N in human replies (bot's scanner picks the latest)
        match_units = None
        match_msg = None
        for msg in human_replies:
            m = _ACTUALS_INLINE_RE.search(msg.get("text", ""))
            if m:
                try:
                    match_units = int(m.group(1))
                    match_msg = msg
                except (ValueError, TypeError):
                    continue

        if match_units is not None:
            user_id = match_msg.get("user", "?")
            ts = match_msg.get("ts", "")
            snippet = match_msg.get("text", "")[:120].replace("\n", " ")
            print(f"    🔥 MATCH: 'actuals {match_units}' from <@{user_id}> at ts={ts}")
            print(f"       text: {snippet!r}")
            print(f"       → bot WOULD log {match_units} + post :bar_chart: confirmation")
            would_fire.append((c, match_units, user_id, snippet))
        else:
            print(f"    ✓ no 'actuals N' in human replies — no scan ping fires")
            no_match.append(c)

        time.sleep(0.3)  # gentle on Slack API

    # Summary
    print(f"\n{'='*72}")
    print(f"SUMMARY")
    print(f"{'='*72}")
    print(f"\n🔥 Threads where backfill_actuals_reply_scan WOULD fire (post :bar_chart:):")
    if not would_fire:
        print("   (none)")
    for c, units, uid, snippet in would_fire:
        print(f"   • {c['retailer']} [{c['region']}] — units={units} from <@{uid}>")
        if c["snooze"]:
            print(f"     ⚠ This row also has Actuals Snooze Until={c['snooze']} — scan ignores snooze!")

    print(f"\n✓ Threads with no actuals reply (silent):")
    for c in no_match:
        print(f"   • {c['retailer']} [{c['region']}]")

    if errors:
        print(f"\n⚠ Errors:")
        for c, e in errors:
            print(f"   • {c['retailer']} [{c['region']}]: {e}")

    print(f"\nFinal verdict: {len(would_fire)} unexpected :bar_chart: pings would fire from this scan.")


if __name__ == "__main__":
    main()
