"""One-shot historical reconciliation.

Walks the marketing-approval channels for a wider window than the daily cron
(default 180 days, configurable via --days), runs the same reconcile pass, and
reports a summary. Use this once after the reconcile feature ships to backfill
every approval the live flow missed since the tracker was introduced.

Usage:
  RECONCILE_DAYS_BACK=180 python -m scripts.historical_reconcile
or:
  python -m scripts.historical_reconcile --days 365

Safe to re-run — append_reconciled_approved is idempotent (Thread TS unique
constraint and approval-ts dedup).
"""

import argparse
import logging
import sys

from slack_sdk import WebClient

from app.config import SLACK_BOT_TOKEN
from app.reconcile import reconcile_missed_approvals

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("historical_reconcile")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--days", type=int, default=180,
        help="How many days back to scan (default: 180).",
    )
    args = parser.parse_args()

    client = WebClient(token=SLACK_BOT_TOKEN)
    logger.info(f"Starting historical reconcile, window={args.days}d")
    summary = reconcile_missed_approvals(client, days_back=args.days)
    logger.info(f"Done. Summary: {summary}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
