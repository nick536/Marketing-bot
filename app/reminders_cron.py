"""CronJob entry point — runs the daily reminder sweep and exits.

Replaces the in-process threading.Timer scheduler that died on every pod
restart. Invoked by k8s CronJob (see helm/marketing-bot/templates/cronjob.yaml)
on the schedule defined in values-*.yaml.
"""

import logging
import os
import sys

from slack_sdk import WebClient

from app.config import SLACK_BOT_TOKEN
from app.reconcile import reconcile_missed_approvals
from app.tracker import (
    backfill_actuals_reply_scan,
    backfill_calendar_fyis,
    backfill_calendar_reply_scan,
    check_and_send_reminders,
    load_calendar_values,
    mark_no_response,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("reminders_cron")


def main() -> int:
    client = WebClient(token=SLACK_BOT_TOKEN)

    # One-shot Slack-link backfill — gated by env var so it only runs when
    # explicitly requested. Set BACKFILL_SLACK_LINKS=1 in the Jenkins job
    # for a single run, then unset. Idempotent (already-migrated rows skip),
    # but writes to the sheet so we don't want it firing every cron.
    if os.environ.get("BACKFILL_SLACK_LINKS", "").strip() in ("1", "true", "yes"):
        try:
            from scripts.backfill_slack_links import main as _backfill_links_main
            logger.info("BACKFILL_SLACK_LINKS=1 — running one-shot link backfill (LIVE)")
            # Force --live by injecting argv before invoking script main.
            _orig_argv = sys.argv[:]
            sys.argv = ["backfill_slack_links", "--live"]
            try:
                _backfill_links_main()
            finally:
                sys.argv = _orig_argv
        except Exception:
            logger.exception("Slack-link backfill failed (continuing with cron)")

    # Reconcile first so any approvals the live flow missed get into the
    # tracker before the reminder/backfill sweeps see the rows.
    try:
        days_back = int(os.environ.get("RECONCILE_DAYS_BACK", "14"))
        summary = reconcile_missed_approvals(client, days_back=days_back)
        logger.info(f"Reconcile sweep: {summary}")
    except Exception:
        logger.exception("Reconcile sweep failed")

    try:
        backfilled = backfill_calendar_fyis(client)
        logger.info(f"Calendar FYI backfill: {backfilled} pings sent")
    except Exception:
        logger.exception("Calendar FYI backfill failed")

    try:
        cal_cache = load_calendar_values()

        # Backfill historical replies BEFORE the daily reminder sweep so
        # bucket-B replies that landed before the classifier shipped get
        # honored on this cron tick (no more re-pinging exempt rows).
        try:
            backfill = backfill_calendar_reply_scan(
                client, calendar_values=cal_cache,
            )
            logger.info(f"Backfill: {backfill}")
        except Exception:
            logger.exception("Calendar reply backfill failed")

        # Backfill historical `actuals N` replies BEFORE the chase fires —
        # otherwise a row with a missed actuals reply gets re-chased on the
        # same cron tick. Caught 2026-05-13: PersonJ replied "actuals 0" on
        # Welltech (UK) thread before main.py was patched, bot pinged
        # again next morning.
        try:
            actuals_backfill = backfill_actuals_reply_scan(client)
            logger.info(f"Actuals backfill: {actuals_backfill}")
        except Exception:
            logger.exception("Actuals reply backfill failed")

        sent = check_and_send_reminders(client, calendar_values=cal_cache)
        logger.info(f"Reminder sweep: {sent} reminders sent (calendar cached={'yes' if cal_cache else 'no'})")
    except Exception:
        logger.exception("Reminder sweep failed")
        return 1

    # Data-hygiene scan — counts (not pings) rows where End Date parses to
    # the wrong year. Surfaces the row count + first 20 examples so the
    # user knows to clean the sheet. Pairs with the actuals-chase >90d
    # guard and the calendar-verify wrong-year guard, both of which
    # silently skip these rows from pings.
    try:
        from app.tracker import get_promos_needing_reminders, _parse_input_date
        from datetime import date
        rows = get_promos_needing_reminders()
        current_year = date.today().year
        bad_year = []
        for r in rows:
            if str(r.get("Status", "")).strip() != "Approved":
                continue
            end_raw = str(r.get("End Date", "")).strip()
            ed = _parse_input_date(end_raw)
            if ed and ed.year != current_year:
                bad_year.append(f"{r.get('Retailer','?')} ({r.get('Region','?')}): End={end_raw} -> {ed}")
        if bad_year:
            logger.warning(
                f"DATA HYGIENE: {len(bad_year)} approved rows have End Date "
                f"parsing to wrong year. These are skipped from cron pings:\n  - "
                + "\n  - ".join(bad_year[:20])
            )
    except Exception:
        logger.exception("data hygiene scan failed (continuing)")

    try:
        expired = mark_no_response()
        logger.info(f"No-response sweep: marked {expired} promos")
    except Exception:
        logger.exception("No-response sweep failed")

    return 0


if __name__ == "__main__":
    sys.exit(main())
