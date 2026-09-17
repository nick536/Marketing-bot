#!/usr/bin/env python3
"""One-shot cleanup of the Promo Tracker sheet.

What it does:
- Deletes rows that should never have landed in the sheet:
    * Status == "Pending" (per policy: only Approved promos belong here).
    * End Date that parses to a year < 2020 or > 2100 (LLM hallucinations
      like "1901-03-02" from the Proshop SDA row).
- For every remaining row, normalizes:
    * Thread TS          → =HYPERLINK(slack_url, ts)
    * Approval Thread TS → =HYPERLINK(slack_url, ts) using Approval Channel ID
    * Start Date / End Date → ISO YYYY-MM-DD (blank if unparseable)
    * Discount %         → "20%" or "-"
    * Predicted CM3      → "X.X%"

Default is dry-run. Pass --apply to actually mutate the sheet.

Usage:
    cd ~/marketing-approval-bot
    source .env.sh    # loads GOOGLE_SERVICE_ACCOUNT_JSON, PROMO_SHEET_ID, PROMO_CHANNEL_ID
    python scripts/cleanup_tracker.py            # dry run
    python scripts/cleanup_tracker.py --apply    # write changes
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.tracker import (  # noqa: E402
    APPROVAL_CHAN_COL,
    APPROVAL_TS_COL,
    PROMO_SHEET_ID,
    _ensure_tracker_tab,
    _get_client,
    _to_discount_str,
    _to_iso_date,
    _to_pct_str,
    _thread_ts_link,
)


def _is_link_already(value: str) -> bool:
    return isinstance(value, str) and value.strip().startswith("=HYPERLINK(")


def _looks_pending(status: str) -> bool:
    return (status or "").strip().lower() == "pending"


def _bad_end_date(end_raw) -> bool:
    """True iff end_raw is set but resolves to a year < 2020 / > 2100.

    Blank / unparseable end dates are NOT a delete reason — those rows still
    have value (e.g., "TBD" promos waiting for a date). We only nuke rows
    where the date is plainly wrong (1901-03-02 was a real bug).

    Numeric (serial-date) end values are LEFT ALONE — those are valid dates
    Sheets stores natively. Only inspect strings.
    """
    if isinstance(end_raw, (int, float)):
        return False
    s = (str(end_raw) if end_raw is not None else "").strip()
    if not s:
        return False
    try:
        import dateparser
    except ImportError:
        return False
    parsed = dateparser.parse(s, settings={"PREFER_DATES_FROM": "future"})
    if not parsed:
        return False
    return parsed.year < 2020 or parsed.year > 2100


def _is_native(value) -> bool:
    """True if the cell value is a native number (Sheets serial date / native
    int) that already represents valid data. Skip normalization on these so
    we don't blow them away by str-coercing them through dateparser."""
    return isinstance(value, (int, float))


def main(apply: bool) -> int:
    gc = _get_client()
    sheet = gc.open_by_key(PROMO_SHEET_ID)
    ws = _ensure_tracker_tab(sheet)

    headers = ws.row_values(1)
    # Read raw values (FORMULA) so we don't re-wrap rows that already are HYPERLINKs.
    raw = ws.get_all_values(value_render_option="FORMULA")
    if not raw:
        print("Sheet is empty. Nothing to do.")
        return 0
    data_rows = raw[1:]
    n = len(data_rows)
    print(f"Read {n} data rows. Headers: {headers}\n")

    def col(name: str) -> int:
        return headers.index(name) if name in headers else -1

    c_thread = col("Thread TS")
    c_status = col("Status")
    c_start = col("Start Date")
    c_end = col("End Date")
    c_discount = col("Discount %")
    c_cm3 = col("Predicted CM3")
    c_approval_chan = col(APPROVAL_CHAN_COL)
    c_approval_ts = col(APPROVAL_TS_COL)
    c_channel = col("Channel")

    delete_rows: list[tuple[int, str]] = []  # (sheet_row_num, reason)
    cell_updates: list[dict] = []  # {"range": "B2", "values": [[v]]}

    def a1(col_index_zero: int, row_num: int) -> str:
        # 0-indexed column → A1 letter
        n_letters = ""
        idx = col_index_zero
        while True:
            n_letters = chr(ord("A") + (idx % 26)) + n_letters
            idx = idx // 26 - 1
            if idx < 0:
                break
        return f"{n_letters}{row_num}"

    for i, row in enumerate(data_rows):
        sheet_row_num = i + 2  # 1-indexed sheet, +1 for header
        # Pad row to header length so col indexing never IndexErrors.
        if len(row) < len(headers):
            row = row + [""] * (len(headers) - len(row))

        status = row[c_status] if c_status >= 0 else ""
        end_raw = row[c_end] if c_end >= 0 else ""
        retailer = row[col("Retailer")] if col("Retailer") >= 0 else ""

        if _looks_pending(status):
            delete_rows.append((sheet_row_num, f"Pending — {retailer}"))
            continue
        if _bad_end_date(end_raw):
            delete_rows.append((sheet_row_num, f"Bad End Date '{end_raw}' — {retailer}"))
            continue

        # ---- Normalizations ----
        approval_chan = row[c_approval_chan].strip() if c_approval_chan >= 0 else ""

        # Thread TS hyperlink. Skip if already a HYPERLINK formula (idempotent).
        if c_thread >= 0:
            curr = row[c_thread]
            if not _is_link_already(curr):
                # Strip any quoting noise; live ts has the form 1234567890.123456.
                bare = curr.strip()
                # Eval thread channel = PROMO_CHANNEL_ID (env), not approval_chan.
                new_val = _thread_ts_link(bare)
                if new_val and new_val != bare:
                    cell_updates.append({
                        "range": a1(c_thread, sheet_row_num),
                        "values": [[new_val]],
                    })

        # Approval Thread TS hyperlink (using approval_chan)
        if c_approval_ts >= 0:
            curr = row[c_approval_ts]
            if curr and not _is_link_already(curr):
                bare = curr.strip()
                new_val = _thread_ts_link(bare, approval_chan)
                if new_val and new_val != bare:
                    cell_updates.append({
                        "range": a1(c_approval_ts, sheet_row_num),
                        "values": [[new_val]],
                    })

        # Start / End → ISO. Skip native serial dates — those are already
        # valid; coercing them through str() would yield "46155" and
        # _to_iso_date would reject as out-of-range, blanking real dates.
        for c_idx in (c_start, c_end):
            if c_idx < 0:
                continue
            curr = row[c_idx]
            if _is_native(curr):
                continue
            iso = _to_iso_date(curr)
            if iso != (str(curr) if curr is not None else "").strip():
                cell_updates.append({
                    "range": a1(c_idx, sheet_row_num),
                    "values": [[iso]],
                })

        # Discount %. Skip native numbers — Sheets renders those as e.g. 0.15
        # which the user can format as a %.
        if c_discount >= 0:
            curr = row[c_discount]
            if not _is_native(curr):
                new_val = _to_discount_str(curr)
                if new_val != (str(curr) if curr is not None else "").strip():
                    cell_updates.append({
                        "range": a1(c_discount, sheet_row_num),
                        "values": [[new_val]],
                    })

        # Predicted CM3. Skip native numbers (already a number, can format as %).
        if c_cm3 >= 0:
            curr = row[c_cm3]
            if not _is_native(curr):
                new_val = _to_pct_str(curr)
                if new_val != (str(curr) if curr is not None else "").strip():
                    cell_updates.append({
                        "range": a1(c_cm3, sheet_row_num),
                        "values": [[new_val]],
                    })

    # ---- Report ----
    print(f"Rows to DELETE: {len(delete_rows)}")
    for rn, why in delete_rows:
        print(f"  row {rn}: {why}")
    print(f"\nCell updates: {len(cell_updates)}")
    for u in cell_updates[:30]:
        print(f"  {u['range']}: {u['values'][0][0]!r}")
    if len(cell_updates) > 30:
        print(f"  ... and {len(cell_updates) - 30} more")

    if not apply:
        print("\nDRY RUN — pass --apply to write changes.")
        return 0

    print("\nApplying changes...")

    # Delete rows from the brheinshopm up so row numbers stay stable.
    for rn, why in sorted(delete_rows, key=lambda x: -x[0]):
        ws.delete_rows(rn)
        print(f"  deleted row {rn}: {why}")
        time.sleep(0.3)

    # Batch updates in chunks of 50 to stay under quota (60 writes/min/user).
    chunk = 50
    for i in range(0, len(cell_updates), chunk):
        ws.batch_update(cell_updates[i : i + chunk], value_input_option="USER_ENTERED")
        print(f"  wrote {min(i + chunk, len(cell_updates))} / {len(cell_updates)} cells")
        time.sleep(1.5)

    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main(apply="--apply" in sys.argv))
