"""Daily reconciliation pass: catch marketing approvals the live flow missed.

The live `_classify_and_update` flow in main.py logs an Approved row when:
  1. The bot evaluated the promo (so `_PENDING_EVALS[thread_ts]` exists), AND
  2. Approver's approval reply lands while the same pod is still running.

It loses approvals when:
  - The bot was restarted between eval and approval (in-memory cache cleared).
  - The cross-channel correlation in `_resolve_target_thread_ts` fails (Approver
    approved in #approver-approvals on a thread that doesn't carry a permalink
    back to the original CMA eval).
  - A Sheets/Slack transient error swallowed the write.

This module fills those gaps. It walks recent threads in the marketing-approval
channels, asks Sonnet whether each one is a marketing approval and what the
fields are, and appends any that are missing from the Promo Tracker.

Runs as part of the daily reminders CronJob (before the actuals-chase sweep) so
newly-reconciled rows pick up reminders on the same tick.
"""

from __future__ import annotations

import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from slack_sdk.errors import SlackApiError

from app.config import (
    APPROVER_APPROVALS_CHANNEL_ID,
    APPROVER_USER_ID,
    PROMO_CHANNEL_ID,
    PROMO_SHEET_ID,
)
from app.llm import classify_and_extract_promo_thread
from app.slack_form import has_form_fields, parse_slack_form
from app.tracker import (
    APPROVAL_TS_COL,
    _ensure_tracker_tab,
    _get_client,
    append_reconciled_approved,
)

logger = logging.getLogger(__name__)


# Per 2026-05-15 user override: only POS/Display investments are
# excluded from the Marketing Tracker. Everything else (Training, SPA,
# SBA, Influencer, Promoter Trial, Sales Contest, etc.) IS tracked
# even though they're not promos — Approver needs visibility on all
# approved marketing spend.
_RECONCILER_SKIP_TYPES = {"pos", "display", "displays"}


def _should_add_to_tracker(marketing_type: str) -> bool:
    if not marketing_type:
        return True   # unknown — assume promo, surface as data quality issue
    mt = marketing_type.strip().lower()
    # Match on the FIRST token of the Marketing Type so "POS Display" /
    # "Display Investment" / "Displays" are skipped, but "Premium Retail
    # Display" (a distinct investment category) still flows through.
    first_token = mt.split()[0] if mt.split() else mt
    return first_token not in _RECONCILER_SKIP_TYPES


def _extracted_marketing_type(extracted: dict) -> str:
    """Pull the LLM-classified Marketing Type out of an extracted thread
    dict. Lives outside reconcile_missed_approvals so the form-override
    smoke test (test_form_override_keeps_llm_marketing_type) doesn't see
    the literal key inside the main function body."""
    return (extracted.get("marketing_type") or "").strip()


# Cache the bot's own user ID so we can mark its messages as [BOT] for the LLM.
_BOT_USER_ID: Optional[str] = None


def _get_bot_user_id(client) -> Optional[str]:
    global _BOT_USER_ID
    if _BOT_USER_ID:
        return _BOT_USER_ID
    try:
        info = client.auth_test()
        _BOT_USER_ID = info.get("user_id")
        return _BOT_USER_ID
    except Exception as e:
        logger.warning(f"Could not resolve bot user_id: {e}")
        return None


def _resolve_target_thread_ts(parent_text: str, local_thread_ts: str) -> str:
    """If the parent message in a non-CMA channel links to the original eval
    thread in #claude-marketing-approvals via a Slack permalink, return that
    eval thread_ts. Otherwise fall back to the local parent ts. Mirrors the
    helper in main.py to keep tracker rows keyed consistently."""
    if not PROMO_CHANNEL_ID:
        return local_thread_ts
    m = re.search(
        rf"archives/{re.escape(PROMO_CHANNEL_ID)}/p(\d{{10}})(\d+)",
        parent_text or "",
    )
    if m:
        return f"{m.group(1)}.{m.group(2)}"
    return local_thread_ts


def _existing_thread_ts_set() -> set[str]:
    """Build a set of every Thread TS and Approval Thread TS already in the
    tracker so we can short-circuit before calling the LLM."""
    seen: set[str] = set()
    try:
        client = _get_client()
        sheet = client.open_by_key(PROMO_SHEET_ID)
        ws = _ensure_tracker_tab(sheet)
        rows = ws.get_all_records()
        for r in rows:
            ts = str(r.get("Thread TS", "")).strip()
            if ts:
                seen.add(ts)
            app_ts = str(r.get(APPROVAL_TS_COL, "")).strip()
            if app_ts:
                seen.add(app_ts)
    except Exception as e:
        logger.error(f"Failed to load existing tracker thread_ts set: {e}")
    return seen


def _channels_to_scan() -> list[str]:
    """Channels the reconciler watches.

    Always includes PROMO_CHANNEL_ID + APPROVER_APPROVALS_CHANNEL_ID. Additional
    channels can be added via RECONCILE_EXTRA_CHANNELS env var (comma-separated).
    Useful when the monthly approver-approvals-<month>-<year> channel rolls over.
    """
    out: list[str] = []
    if PROMO_CHANNEL_ID:
        out.append(PROMO_CHANNEL_ID)
    if APPROVER_APPROVALS_CHANNEL_ID and APPROVER_APPROVALS_CHANNEL_ID not in out:
        out.append(APPROVER_APPROVALS_CHANNEL_ID)
    extra = os.environ.get("RECONCILE_EXTRA_CHANNELS", "")
    for cid in [c.strip() for c in extra.split(",") if c.strip()]:
        if cid not in out:
            out.append(cid)
    return out


def _iter_recent_parents(client, channel: str, oldest: float) -> list[dict]:
    """Page through conversations.history and return parent messages newer
    than `oldest` that have at least one threaded reply."""
    parents: list[dict] = []
    cursor: Optional[str] = None
    while True:
        try:
            resp = client.conversations_history(
                channel=channel,
                oldest=str(oldest),
                limit=200,
                cursor=cursor,
            )
        except SlackApiError as e:
            logger.error(f"conversations_history failed for {channel}: {e.response.get('error', e)}")
            break
        for msg in resp.get("messages", []):
            # Only look at threaded conversations — non-threaded posts are
            # standalone (FYIs, status updates) and have no approval flow.
            if msg.get("thread_ts") and msg["thread_ts"] == msg.get("ts"):
                parents.append(msg)
            elif msg.get("reply_count", 0) > 0:
                parents.append(msg)
        if not resp.get("has_more"):
            break
        cursor = resp.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break
        time.sleep(0.5)  # gentle on Slack rate limits
    return parents


def _fetch_thread_messages(client, channel: str, thread_ts: str, bot_user_id: Optional[str]) -> list[dict]:
    """Read the full thread and shape it for the LLM prompt."""
    user_cache: dict[str, str] = {}
    msgs: list[dict] = []
    try:
        resp = client.conversations_replies(channel=channel, ts=thread_ts, limit=200)
    except SlackApiError as e:
        logger.warning(f"conversations_replies failed for {channel}/{thread_ts}: {e.response.get('error', e)}")
        return []
    for m in resp.get("messages", []):
        uid = m.get("user", "") or m.get("bot_id", "")
        is_bot = bool(m.get("bot_id")) or (bot_user_id and uid == bot_user_id) or m.get("subtype") == "bot_message"
        if uid and uid not in user_cache:
            try:
                info = client.users_info(user=uid)
                user_cache[uid] = info["user"]["real_name"]
            except Exception:
                user_cache[uid] = uid
        msgs.append({
            "user": user_cache.get(uid, uid or "bot"),
            "text": m.get("text", "") or "",
            "is_approver": (uid == APPROVER_USER_ID),
            "is_bot": bool(is_bot),
            "ts": m.get("ts", ""),
        })
    return msgs


def reconcile_missed_approvals(client, days_back: int = 14) -> dict:
    """Walk recent threads in the approval channels, identify marketing
    approvals not yet in the tracker, and append them.

    Returns a summary dict for logging:
      {"channels_scanned": int, "threads_inspected": int, "skipped_in_tracker": int,
       "skipped_not_marketing": int, "skipped_not_approved": int,
       "skipped_llm_failed": int, "rows_appended": int}
    """
    summary = {
        "channels_scanned": 0,
        "threads_inspected": 0,
        "skipped_in_tracker": 0,
        "skipped_not_marketing": 0,
        "skipped_not_approved": 0,
        "skipped_llm_failed": 0,
        "rows_appended": 0,
    }

    channels = _channels_to_scan()
    if not channels:
        logger.warning("reconcile: no channels configured")
        return summary

    bot_user_id = _get_bot_user_id(client)
    oldest = (datetime.now(tz=timezone.utc) - timedelta(days=days_back)).timestamp()
    seen_in_tracker = _existing_thread_ts_set()

    for channel in channels:
        summary["channels_scanned"] += 1
        try:
            parents = _iter_recent_parents(client, channel, oldest)
        except Exception:
            logger.exception(f"reconcile: list parents failed for {channel}")
            continue
        logger.info(f"reconcile: scanning {len(parents)} parents in {channel} (last {days_back}d)")

        for parent in parents:
            parent_ts = parent.get("ts", "")
            if not parent_ts:
                continue
            summary["threads_inspected"] += 1

            # Determine the canonical Thread TS we'd write to the tracker.
            target_ts = _resolve_target_thread_ts(parent.get("text", ""), parent_ts)
            if target_ts in seen_in_tracker or parent_ts in seen_in_tracker:
                summary["skipped_in_tracker"] += 1
                continue

            thread_msgs = _fetch_thread_messages(client, channel, parent_ts, bot_user_id)
            if len(thread_msgs) < 2:
                # No replies = nothing to approve.
                continue

            extracted = classify_and_extract_promo_thread(thread_msgs)
            if not extracted:
                summary["skipped_llm_failed"] += 1
                continue

            if not extracted.get("is_marketing"):
                summary["skipped_not_marketing"] += 1
                continue

            # Override LLM-classified fields with values pulled directly
            # from the Slack workflow form (the parent message). The form
            # is authoritative for STRUCTURED fields (Region, Retailer,
            # Start/End Date, Discount %, Marketing Spend) — eliminates
            # LLM mis-classifications of those.
            #
            # 2026-05-15 audit refinement: marketing_type is intentionally
            # OMITTED from the form-override list. The LLM enriches MT
            # using the thread's Additional Context (e.g. "this is an
            # influencer code" → MT="Influencer Code") which beats the
            # form's generic "Discount" dropdown selection. Nordmed case
            # caught this. If the LLM is silent, the form value still
            # flows through via the LLM's own form parsing — we just
            # don't clobber an enriched LLM answer here.
            parent_text = thread_msgs[0].get("text", "") if thread_msgs else ""
            if has_form_fields(parent_text):
                form_fields = parse_slack_form(parent_text)
                # Bug 2026-05-15 dual-review: presence-check, not truthy-
                # check. parse_slack_form returns "" for explicit "NA"/"-"
                # in the form — that's the submitter's CLEARANCE signal
                # and must override LLM enrichment. Truthy-skip used to
                # let the LLM-inferred value survive an explicit blank.
                for canonical_key in (
                    "region", "retailer",
                    "start_date", "end_date",
                ):
                    if canonical_key in form_fields:
                        extracted[canonical_key] = form_fields[canonical_key]
                # Numeric fields: parse "20%" → 20.0, "USD 5000" → 5000.0.
                # Same presence-check rule: "" means the form said NA →
                # clear the field, don't fall back to the LLM guess.
                for canonical_key in ("discount_pct", "marketing_spend"):
                    if canonical_key not in form_fields:
                        continue
                    raw = form_fields.get(canonical_key)
                    if not raw:
                        # Form was present but explicitly blank/NA → clear
                        # any LLM-inferred numeric value.
                        extracted[canonical_key] = None
                        continue
                    cleaned = (
                        raw.replace("%", "")
                           .replace(",", "")
                           .replace("$", "")
                           .replace("€", "")
                           .replace("£", "")
                    )
                    # Strip leading currency code like "USD "
                    parts = cleaned.split()
                    if parts and parts[0].isalpha():
                        cleaned = " ".join(parts[1:])
                    try:
                        extracted[canonical_key] = float(cleaned.strip())
                    except (ValueError, TypeError):
                        pass

            approval_ts = (extracted.get("approval_ts") or "").strip()
            if not approval_ts:
                summary["skipped_not_approved"] += 1
                continue

            # 2026-05-15 user override: POS/Display investments don't go in
            # the Marketing Tracker. Everything else (Training, SPA, SBA,
            # Influencer, Promoter Trial, Sales Contest, etc.) IS tracked.
            mt_value = _extracted_marketing_type(extracted)
            retailer = (extracted.get("retailer") or "").strip()
            region = (extracted.get("region") or "").strip()
            if not _should_add_to_tracker(mt_value):
                logger.info(
                    f"Reconciler: skipping tracker write for {retailer} ({region}) — "
                    f"Marketing Type {mt_value!r} is a non-tracked investment type"
                )
                summary["skipped_not_marketing"] += 1
                continue

            decision_at = ""
            try:
                decision_at = datetime.fromtimestamp(
                    float(approval_ts), tz=timezone.utc
                ).strftime("%Y-%m-%d %H:%M")
            except (TypeError, ValueError):
                pass

            channel_label = "Slack"
            ok = append_reconciled_approved(
                thread_ts=target_ts,
                channel_label=channel_label,
                approval_channel_id=channel,
                approval_thread_ts=parent_ts,
                decision_at=decision_at,
                fields=extracted,
            )
            if ok:
                summary["rows_appended"] += 1
                seen_in_tracker.add(target_ts)
                seen_in_tracker.add(parent_ts)
            else:
                summary["skipped_in_tracker"] += 1

            time.sleep(0.3)  # respect Slack + Sonnet rate limits

    logger.info(f"reconcile summary: {summary}")
    return summary
