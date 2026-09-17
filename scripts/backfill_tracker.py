#!/usr/bin/env python3
"""
One-time backfill script: reads all threads from #claude-marketing-approvals,
migrates the sheet to new headers (Submitter + Marketing Spend + Decision At),
and writes any missing promo rows to Promo Tracker.

Usage:
    cd ~/marketing-approval-bot
    SLACK_BOT_TOKEN=xoxb-... \
    GOOGLE_SERVICE_ACCOUNT_JSON='{"type":"service_account",...}' \
    PROMO_SHEET_ID=REDACTED_SHEET_ID_2 \
    python scripts/backfill_tracker.py

Or with a .env file:
    cp .env.example .env  # fill in values
    python -m dotenv run -- python scripts/backfill_tracker.py
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timezone

# ── Inject project root into path so app.* imports work ──
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

# ── Config ──
SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
APPROVER_USER_ID = "U0REDACT010"
PROMO_CHANNEL_ID = os.environ.get("PROMO_CHANNEL_ID", "C0REDACT003")

# Approval / rejection keyword sets (mirrors main.py)
APPROVAL_PATTERNS = ["approved", "approve", "go ahead", "lgtm", "looks good"]
REJECTION_PATTERNS = ["reject", "rejected", "not approved", "don't approve",
                      "dont approve", "pass on this", "no go"]


# ── Slack helpers ──

def get_channel_history(client: WebClient, channel: str) -> list[dict]:
    """Paginate through all messages in channel (newest first)."""
    messages = []
    cursor = None
    while True:
        try:
            kwargs = {"channel": channel, "limit": 200}
            if cursor:
                kwargs["cursor"] = cursor
            resp = client.conversations_history(**kwargs)
            messages.extend(resp["messages"])
            if not resp.get("has_more"):
                break
            cursor = resp["response_metadata"]["next_cursor"]
            time.sleep(0.5)  # rate limit
        except SlackApiError as e:
            print(f"[ERROR] conversations_history: {e}")
            break
    return messages


def get_thread_replies(client: WebClient, channel: str, thread_ts: str) -> list[dict]:
    """Get all replies in a thread (excludes the parent message)."""
    try:
        resp = client.conversations_replies(channel=channel, ts=thread_ts, limit=200)
        # First message is the parent; skip it
        return resp.get("messages", [])[1:]
    except SlackApiError as e:
        print(f"[WARN] conversations_replies({thread_ts}): {e}")
        return []


# ── Block / text parsers ──

def _text_from_blocks(blocks: list[dict]) -> str:
    """Flatten all block text into a single string for regex scanning."""
    parts = []
    for b in blocks or []:
        t = b.get("text", {})
        if isinstance(t, dict):
            parts.append(t.get("text", ""))
        elif isinstance(t, str):
            parts.append(t)
        for field in b.get("fields", []):
            if isinstance(field, dict):
                parts.append(field.get("text", ""))
    return "\n".join(parts)


def extract_grade(blocks: list[dict], fallback_text: str) -> str | None:
    """Extract grade (A/B/C/REJECT) from bot evaluation blocks or fallback text."""
    combined = _text_from_blocks(blocks) + "\n" + (fallback_text or "")
    m = re.search(r"Grade\s+(A|B|C|REJECT)\b", combined)
    return m.group(1) if m else None


def extract_predicted_units(blocks: list[dict]) -> int | None:
    combined = _text_from_blocks(blocks)
    m = re.search(r"[Ii]ncremental units[:\s]*\*?(\d+)\*?", combined)
    return int(m.group(1)) if m else None


def extract_predicted_cm3(blocks: list[dict]) -> float | None:
    combined = _text_from_blocks(blocks)
    # Matches: `*CM3-Cash             $[REDACTED]*` or `CM3-Cash: $[REDACTED]`
    m = re.search(r"CM3-Cash\s+\$?([\d,]+)", combined)
    if m:
        return float(m.group(1).replace(",", ""))
    return None


def extract_marketing_spend(blocks: list[dict], text: str) -> float | None:
    combined = _text_from_blocks(blocks) + "\n" + (text or "")
    # "Marketing spend: $[REDACTED] USD" or "USD [REDACTED] spend"
    m = re.search(r"[Mm]arketing spend[:\s]+\$?([\d,]+)", combined)
    if m:
        return float(m.group(1).replace(",", ""))
    return None


def extract_promo_info(original_text: str, blocks: list[dict]) -> dict:
    """Best-effort extraction of promo fields from original message + bot blocks."""
    block_text = _text_from_blocks(blocks)
    combined = block_text + "\n" + (original_text or "")
    orig = original_text or ""

    retailer = None
    region = None
    discount_pct = None
    marketing_type = "Promo"
    dates = []

    # --- Retailer / Region extraction (priority order) ---

    # 1. Bot eval section 2: "Retailer: X | Region: Y"
    m = re.search(r"Retailer[:\s]+([^\|\n]+)\|?\s*Region[:\s]+([^\n]+)", block_text)
    if m:
        retailer = m.group(1).strip().rstrip("|").strip()
        region = m.group(2).strip()

    # 2. Bot eval section 1 header: "*Retailer* (Region) | 15% discount"
    if not retailer:
        m = re.search(r"\*([^*\n]+)\*\s+\(([^)]+)\)\s+\|", block_text)
        if m:
            retailer = m.group(1).strip()
            region = m.group(2).strip()

    # 3. Original message bracket format: [Retailer] [Region] or [Retailer] [Region] [Sub]
    if not retailer:
        brackets = re.findall(r"\[([^\]]+)\]", orig)
        if len(brackets) >= 2:
            retailer = brackets[0].strip()
            region = brackets[1].strip()
        elif len(brackets) == 1:
            retailer = brackets[0].strip()

    # 4. Natural language patterns in original message
    if not retailer:
        # "SPIV Evaluation: Soundstore (AU) —"
        m = re.search(r"(?:Evaluation|Eval)[:\s]+([^(—\n]+?)\s+\(([A-Z]{2,})\)", orig)
        if m:
            retailer = m.group(1).strip()
            region = m.group(2).strip()

    if not retailer:
        # "Evaluate X% ... for Bug Israel" or "Evaluate ... for Retailer Region"
        m = re.search(r"[Ee]valuate\s+.+?\s+for\s+([A-Za-z0-9 ]+?)(?:\s*[-–]\s|\s*,|\s*\.|\s*$)", orig)
        if m:
            candidate = m.group(1).strip()
            # Split last word as region if it looks like a country
            words = candidate.split()
            retailer = " ".join(words[:-1]) if len(words) > 1 else candidate
            region = words[-1] if len(words) > 1 else None

    if not retailer:
        # "spend $X for/with/on Retailer Region"
        m = re.search(r"(?:spend|budget).+?(?:for|with|on)\s+([A-Za-z0-9 &]+?)(?:\s+(?:launch|from|in\s+\w+\s+to|\()|\s*$)", orig, re.IGNORECASE)
        if m:
            retailer = m.group(1).strip()

    if not retailer:
        # "X will contribute ... toward a Y% retail promotion"
        m = re.search(r"^([A-Za-z0-9 &]+?)\s+will\s+contribute", orig)
        if m:
            retailer = m.group(1).strip()

    if not retailer:
        # "in Philippines with Isleco" / "with X from"
        m = re.search(r"with\s+([A-Za-z0-9 &]+?)(?:\s+from\s|\s+in\s|\s*$)", orig, re.IGNORECASE)
        if m:
            retailer = m.group(1).strip()

    # --- Discount % ---
    m = re.search(r"[Dd]iscount[:\s]+(\d+(?:\.\d+)?)\s*%", combined)
    if not m:
        m = re.search(r"(\d+(?:\.\d+)?)\s*%\s*(?:off|discount|retail)", combined)
    if m:
        discount_pct = float(m.group(1))

    # --- Marketing type ---
    m = re.search(r"[Tt]ype[:\s]+([^\n\|]+)", block_text)
    if m:
        marketing_type = m.group(1).strip()
    elif re.search(r"\bSPIV\b", combined, re.IGNORECASE):
        marketing_type = "Spiv"
    elif re.search(r"\b(?:emailer|newsletter|email campaign)\b", combined, re.IGNORECASE):
        marketing_type = "Emailer"
    elif re.search(r"\b(?:SPA|SBA|ecom|digital ads|online)\b", combined, re.IGNORECASE):
        marketing_type = "Ecom Campaign"
    elif re.search(r"\bmarketing\b.*\b(?:budget|investment|spend)\b", combined, re.IGNORECASE):
        marketing_type = "Marketing Investment"

    # --- Dates ---
    m = re.search(r"[Dd]ates?[:\s]+([^\n]+)", combined)
    if m:
        raw_dates = m.group(1).strip()
        parts = re.split(r",\s*|\s+[-–]\s+", raw_dates)
        dates = [p.strip() for p in parts if p.strip()][:2]
    if not dates:
        # "Apr 1st - April 8th" or "20 Mar to Apr 5" in original
        m = re.search(r"(\w+ \d+(?:st|nd|rd|th)?)\s*(?:to|-|–)\s*(\w+ \d+(?:st|nd|rd|th)?)", orig, re.IGNORECASE)
        if m:
            dates = [m.group(1).strip(), m.group(2).strip()]

    # Clean up region — strip trailing punctuation
    if region:
        region = region.strip().rstrip(".,;:")

    return {
        "retailer": retailer,
        "region": region,
        "discount_pct": discount_pct,
        "marketing_type": marketing_type,
        "dates": dates,
    }


def determine_status(replies: list[dict]) -> tuple[str, str]:
    """
    Scan thread replies for Approver's approval/rejection.
    Returns (status, decision_at_str).
    """
    for reply in replies:
        if reply.get("user") != APPROVER_USER_ID:
            continue
        text_lower = (reply.get("text") or "").lower()
        ts = reply.get("ts", "")
        dt_str = ""
        if ts:
            try:
                dt = datetime.fromtimestamp(float(ts), tz=timezone.utc)
                dt_str = dt.strftime("%Y-%m-%d %H:%M")
            except (ValueError, TypeError):
                pass

        if any(p in text_lower for p in APPROVAL_PATTERNS):
            return "Approved", dt_str
        if any(p in text_lower for p in REJECTION_PATTERNS):
            return "Rejected", dt_str

    return "Pending", ""


def ts_to_datetime(ts: str) -> str:
    """Convert Slack timestamp to UTC datetime string."""
    try:
        dt = datetime.fromtimestamp(float(ts), tz=timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return ""


def is_bot_eval_message(msg: dict) -> bool:
    """True if this message is a bot evaluation (contains Grade block or text)."""
    text = msg.get("text", "") or ""
    blocks = msg.get("blocks", []) or []
    if re.search(r"Grade\s+[A-Z]+", text):
        return True
    combined = _text_from_blocks(blocks)
    return bool(re.search(r"Grade\s+(A|B|C|REJECT)\b", combined))


# ── Main backfill ──

def run_backfill():
    from app.tracker import _get_client, _ensure_tracker_tab, PROMO_SHEET_ID, HEADERS, _ensure_headers_current

    print("=== Promo Tracker Backfill ===\n")

    # Step 1: Connect to sheet and migrate headers
    print("[1/4] Connecting to Google Sheets and migrating headers...")
    gc = _get_client()
    sheet = gc.open_by_key(PROMO_SHEET_ID)
    ws = _ensure_tracker_tab(sheet)  # triggers _ensure_headers_current
    print(f"      Sheet headers: {ws.row_values(1)}\n")

    # Step 2: Load existing thread_ts values to avoid duplicates
    print("[2/4] Loading existing tracker rows...")
    existing_rows = ws.get_all_records()
    existing_ts = {str(r.get("Thread TS", "")).strip() for r in existing_rows}
    print(f"      {len(existing_ts)} existing rows found.\n")

    # Step 3: Pull Slack channel history
    print(f"[3/4] Fetching channel history from #{PROMO_CHANNEL_ID}...")
    client = WebClient(token=SLACK_BOT_TOKEN)
    messages = get_channel_history(client, PROMO_CHANNEL_ID)
    print(f"      {len(messages)} total messages fetched.\n")

    # Step 4: Process each thread
    print("[4/4] Processing threads...\n")
    added = 0
    skipped_existing = 0
    skipped_no_eval = 0

    # Sort oldest first so tracker rows are in chronological order
    messages_sorted = sorted(messages, key=lambda m: float(m.get("ts", 0)))

    for msg in messages_sorted:
        ts = msg.get("ts", "")
        text = msg.get("text", "") or ""
        user = msg.get("user", "") or msg.get("bot_id", "")

        # Skip thread replies
        if msg.get("thread_ts") and msg["thread_ts"] != ts:
            continue

        # Skip deleted messages (tombstone subtype)
        if msg.get("subtype") == "tombstone":
            continue

        # Already tracked
        if ts in existing_ts:
            skipped_existing += 1
            continue

        # Only process messages that have thread replies (promos that got evaluated)
        reply_count = msg.get("reply_count", 0)
        if not reply_count:
            continue

        # Get thread replies
        replies = get_thread_replies(client, PROMO_CHANNEL_ID, ts)
        time.sleep(0.3)  # rate limit

        # Find the bot's evaluation message in replies
        eval_msg = None
        for reply in replies:
            if is_bot_eval_message(reply):
                eval_msg = reply
                break

        if not eval_msg:
            skipped_no_eval += 1
            continue

        # Extract grade and P&L from bot's evaluation
        eval_blocks = eval_msg.get("blocks", []) or []
        eval_text = eval_msg.get("text", "") or ""

        grade = extract_grade(eval_blocks, eval_text)
        if not grade:
            skipped_no_eval += 1
            continue  # Can't write a row without a grade

        predicted_units = extract_predicted_units(eval_blocks) or ""
        predicted_cm3 = extract_predicted_cm3(eval_blocks) or ""
        marketing_spend = extract_marketing_spend(eval_blocks, text) or ""

        # Extract promo info from original message + bot blocks
        info = extract_promo_info(text, eval_blocks)

        # Determine status from Approver's replies
        status, decision_at = determine_status(replies)

        # Channel label: bot posts on behalf of user or workflow
        channel_label = "WhatsApp" if "WhatsApp" in text else "Slack"
        submitter = user if (user and not user.startswith("B")) else ""  # skip bot IDs

        # Discount display
        discount_display = info["discount_pct"] if info["discount_pct"] else "-"

        # Posted At from message timestamp
        posted_at = ts_to_datetime(ts)

        row = [
            ts,
            channel_label,
            submitter,
            info["retailer"] or "",
            info["region"] or "",
            info["marketing_type"] or "Promo",
            discount_display,
            marketing_spend,
            info["dates"][0] if len(info["dates"]) > 0 else "",
            info["dates"][1] if len(info["dates"]) > 1 else "",
            predicted_units,
            predicted_cm3,
            grade,
            status,
            "",  # actual_units
            "",  # actual_cm3
            "",  # accuracy_pct
            decision_at,
            posted_at,
        ]

        ws.append_row(row, value_input_option="USER_ENTERED")
        existing_ts.add(ts)
        added += 1

        retailer_label = info["retailer"] or "?"
        print(f"  + Added: {posted_at} | {retailer_label} ({info['region']}) | Grade {grade} | {status}")
        time.sleep(0.5)  # avoid sheet rate limit

    print(f"\n=== Done ===")
    print(f"  Added:              {added}")
    print(f"  Skipped (existing): {skipped_existing}")
    print(f"  Skipped (no eval):  {skipped_no_eval}")
    print(f"  Total messages:     {len(messages)}")


if __name__ == "__main__":
    run_backfill()
