"""Aggregates Promo Tracker data for the Notion expenditure dashboard."""

from collections import defaultdict
from datetime import datetime
from typing import Any


def aggregate_tracker_data(rows: list[dict]) -> dict[str, Any]:
    """
    Compute summary statistics from Promo Tracker rows (as returned by get_all_promos).
    Returns a structured dict ready to be rendered into a Notion page.
    """
    if not rows:
        return _empty_summary()

    # --- Counters ---
    status_counts: dict[str, int] = defaultdict(int)
    total_approved_spend = 0.0
    total_approved_cm3 = 0.0
    total_pending_cm3 = 0.0
    pending_spend = 0.0

    by_region: dict[str, dict] = defaultdict(lambda: {"count": 0, "spend": 0.0, "cm3": 0.0, "grades": defaultdict(int)})
    by_type: dict[str, dict] = defaultdict(lambda: {"count": 0, "spend": 0.0, "cm3": 0.0, "approved": 0, "total": 0})
    grade_dist: dict[str, int] = defaultdict(int)
    grade_cm3: dict[str, float] = defaultdict(float)

    decision_days: list[float] = []
    pending_rows: list[dict] = []
    all_rows_with_dates: list[dict] = []

    now = datetime.utcnow()

    for row in rows:
        status = row.get("Status", "").strip()
        region = row.get("Region", "Unknown").strip() or "Unknown"
        mtype = row.get("Marketing Type", "Unknown").strip() or "Unknown"
        grade = row.get("Grade", "").strip()
        spend = _safe_float(row.get("Marketing Spend", ""))
        cm3 = _safe_float(row.get("Predicted CM3", ""))
        posted_raw = row.get("Posted At", "")
        decision_raw = row.get("Decision At", "")

        status_counts[status] += 1

        if grade:
            grade_dist[grade] += 1
            grade_cm3[grade] += cm3 or 0.0

        by_type[mtype]["total"] += 1
        by_type[mtype]["spend"] += spend or 0.0
        by_type[mtype]["cm3"] += cm3 or 0.0
        if status == "Approved":
            by_type[mtype]["approved"] += 1

        if status == "Approved":
            total_approved_spend += spend or 0.0
            total_approved_cm3 += cm3 or 0.0
            by_region[region]["count"] += 1
            by_region[region]["spend"] += spend or 0.0
            by_region[region]["cm3"] += cm3 or 0.0
            if grade:
                by_region[region]["grades"][grade] += 1

        if status == "Pending":
            total_pending_cm3 += cm3 or 0.0
            pending_spend += spend or 0.0
            days_pending = None
            if posted_raw:
                try:
                    posted_dt = datetime.strptime(str(posted_raw), "%Y-%m-%d %H:%M")
                    days_pending = (now - posted_dt).days
                except ValueError:
                    pass
            pending_rows.append({
                "retailer": row.get("Retailer", ""),
                "region": region,
                "type": mtype,
                "grade": grade,
                "predicted_cm3": cm3,
                "days_pending": days_pending,
                "thread_ts": row.get("Thread TS", ""),
            })

        # Avg days to decision
        if decision_raw and posted_raw:
            try:
                d1 = datetime.strptime(str(posted_raw), "%Y-%m-%d %H:%M")
                d2 = datetime.strptime(str(decision_raw), "%Y-%m-%d %H:%M")
                decision_days.append((d2 - d1).total_seconds() / 86400)
            except ValueError:
                pass

        all_rows_with_dates.append({
            "date": posted_raw,
            "retailer": row.get("Retailer", ""),
            "region": region,
            "type": mtype,
            "grade": grade,
            "status": status,
            "spend": spend,
            "cm3": cm3,
        })

    # Recent 15 (sorted by Posted At descending)
    sorted_rows = sorted(
        all_rows_with_dates,
        key=lambda r: r["date"] or "",
        reverse=True,
    )
    recent = sorted_rows[:15]

    # Approval rate per type
    type_table = []
    for t, d in sorted(by_type.items(), key=lambda x: -x[1]["spend"]):
        total = d["total"]
        approval_rate = round(d["approved"] / total * 100, 1) if total else 0
        avg_cm3 = round(d["cm3"] / total, 0) if total else 0
        type_table.append({
            "type": t,
            "count": total,
            "spend": round(d["spend"], 0),
            "avg_cm3": avg_cm3,
            "approval_rate_pct": approval_rate,
        })

    # Region table
    region_table = []
    for r, d in sorted(by_region.items(), key=lambda x: -x[1]["spend"]):
        count = d["count"]
        avg_cm3_pct = round(d["cm3"] / count, 0) if count else 0
        top_grade = max(d["grades"], key=d["grades"].get) if d["grades"] else "-"
        region_table.append({
            "region": r,
            "approved": count,
            "spend": round(d["spend"], 0),
            "avg_cm3": avg_cm3_pct,
            "top_grade": top_grade,
        })

    # Grade distribution
    grade_table = [
        {
            "grade": g,
            "count": grade_dist[g],
            "pct_of_total": round(grade_dist[g] / len(rows) * 100, 1),
            "avg_cm3": round(grade_cm3[g] / grade_dist[g], 0) if grade_dist[g] else 0,
        }
        for g in sorted(grade_dist.keys())
    ]

    # Status breakdown
    avg_days_to_decision = round(sum(decision_days) / len(decision_days), 1) if decision_days else None
    status_table = [
        {
            "status": s,
            "count": c,
        }
        for s, c in sorted(status_counts.items(), key=lambda x: -x[1])
    ]

    return {
        "generated_at": now.strftime("%Y-%m-%d %H:%M UTC"),
        "summary": {
            "total_promos": len(rows),
            "approved_count": status_counts.get("Approved", 0),
            "pending_count": status_counts.get("Pending", 0),
            "rejected_count": status_counts.get("Rejected", 0),
            "no_response_count": status_counts.get("No Response", 0),
            "actuals_received_count": status_counts.get("Actuals Received", 0),
            "total_approved_spend": round(total_approved_spend, 0),
            "total_approved_cm3": round(total_approved_cm3, 0),
            "pending_pipeline_cm3": round(total_pending_cm3, 0),
            "pending_pipeline_spend": round(pending_spend, 0),
            "avg_days_to_decision": avg_days_to_decision,
        },
        "by_region": region_table,
        "by_type": type_table,
        "grade_distribution": grade_table,
        "status_breakdown": status_table,
        "recent_activity": recent,
        "pending_pipeline": sorted(pending_rows, key=lambda r: -(r["days_pending"] or 0)),
    }


def _safe_float(val) -> float | None:
    try:
        return float(str(val).replace(",", "")) if val not in ("", None, "-") else None
    except (ValueError, TypeError):
        return None


def _empty_summary() -> dict:
    return {
        "generated_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
        "summary": {
            "total_promos": 0,
            "approved_count": 0,
            "pending_count": 0,
            "rejected_count": 0,
            "no_response_count": 0,
            "actuals_received_count": 0,
            "total_approved_spend": 0,
            "total_approved_cm3": 0,
            "pending_pipeline_cm3": 0,
            "pending_pipeline_spend": 0,
            "avg_days_to_decision": None,
        },
        "by_region": [],
        "by_type": [],
        "grade_distribution": [],
        "status_breakdown": [],
        "recent_activity": [],
        "pending_pipeline": [],
    }
