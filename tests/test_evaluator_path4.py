"""Regression tests for the retailer evaluator's Path 4 + duration-default fix.

Bugs fixed:
  1. sheets.py: missing end-date no longer silently defaults duration_days to 7.
     It now writes 0 (sentinel for "missing") and the evaluator skips that comp.
  2. evaluator.py Path 4: cross-region scaling now applies the comp's LIFT RATIO
     to the target baseline, instead of treating comp.units_sold as if it were
     net-of-baseline incremental volume.
"""
import os
import sys

# Make `import app.*` work when running pytest from repo root or this dir.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# app.config requires several env vars to be present at import time. The
# evaluator never *uses* them in this test, but the import chain demands them.
os.environ.setdefault("SLACK_BOT_TOKEN", "test")
os.environ.setdefault("SLACK_SIGNING_SECRET", "test")
os.environ.setdefault("GOOGLE_SERVICE_ACCOUNT_JSON", "{}")
os.environ.setdefault("PROMO_SHEET_ID", "test")
os.environ.setdefault("PL_SHEET_ID", "test")
os.environ.setdefault("SELLOUT_SHEET_ID", "test")

from app.evaluator import estimate_incremental_units
from app.models import PromoHistory, PromoRecord
from app.parser import PromoRequest


def _bulkclub_uk_request():
    return PromoRequest(
        retailer="Bulkclub UK",
        region="UK",
        promo_type="promo",
        discount_pct=1.0,  # REDACTED
    )


def _catalogmart_uk_comp(duration_days=13, baseline_weekly=1.0):  # REDACTED
    """Catalogmart UK comp: [REDACTED] units in 13d at [REDACTED] off, baseline [REDACTED]/wk."""
    return PromoRecord(
        market="UK",
        retailer="Catalogmart UK",
        promo_name="Catalogmart Spring Sale",
        promo_type="promo",
        promo_start="2026-03-01",
        promo_end="2026-03-13",
        duration_days=duration_days,
        discount_pct=1.0,  # REDACTED
        units_sold=0,  # REDACTED
        baseline_units_weekly=baseline_weekly,
        notes="(cross-region: UK)",
        month=3,
    )


# ---------------------------------------------------------------------------
# Path 4: cross-region lift-ratio scaling (the headline regression test)
# ---------------------------------------------------------------------------

def test_path4_uses_lift_ratio_not_absolute_volume():
    """Bulkclub UK [REDACTED]% x 27d, with Catalogmart UK comp ([REDACTED]u/13d/[REDACTED]% off, baseline [REDACTED]/wk).

    Pre-fix behaviour: returned an inflated incremental figure (treated comp units as
    net-incremental and scaled to 27d, ignoring Bulkclub's much smaller baseline).

    Post-fix expectation: lift ratio is applied to the target baseline instead of the
    comp's absolute volume. [REDACTED]
    """
    history = PromoHistory(
        records=[_catalogmart_uk_comp()],
        baseline_units_weekly=1.0,           # REDACTED — Bulkclub UK baseline
        sample_count=1,
        retailer_matched="Bulkclub UK",
        fallback_source="enriched with UK data",  # triggers Path 4 (cross-region)
    )
    request = _bulkclub_uk_request()
    promo_weeks = 27 / 7

    result = estimate_incremental_units(history, request, promo_weeks=promo_weeks)

    assert 0 <= result <= 0, (  # REDACTED
        f"Path 4 returned {result}; expected a small incremental figure for "
        f"Bulkclub UK x 27d with Catalogmart UK comp. The pre-fix bug returned an inflated figure."
    )
    # Hard ceiling: must be well below the comp's own absolute volume.
    assert result < 0, (  # REDACTED
        f"Path 4 returned {result}, which is >= comp.units_sold. "
        f"That implies we're still scaling absolute volume rather than lift ratio."
    )


def test_path4_skips_comp_with_missing_duration():
    """If the comp has duration_days <= 0, we should not fabricate a 1-week window.

    With no other usable signal (no comp + cross-region fallback bypasses
    baseline-dependent paths), the evaluator should fall through to Path 5
    (conservative lift on adjusted_baseline) or return 0.
    """
    bad_comp = _catalogmart_uk_comp(duration_days=0)  # missing/unparseable end date
    history = PromoHistory(
        records=[bad_comp],
        baseline_units_weekly=1.0,  # REDACTED
        sample_count=1,
        retailer_matched="Bulkclub UK",
        fallback_source="enriched with UK data",
    )
    request = _bulkclub_uk_request()
    promo_weeks = 27 / 7

    result = estimate_incremental_units(history, request, promo_weeks=promo_weeks)

    # Pre-fix: would have produced an inflated number from a fabricated 7-day comp.
    # Post-fix: comp is dropped; Path 4 inactive; Path 5 (or 0) takes over.
    # Either way the answer is small, not [REDACTED].
    assert result < 0, (  # REDACTED
        f"Got {result}; expected a small number (Path 5 conservative lift) or 0 "
        f"once the duration-less comp is correctly skipped."
    )


def test_path4_fallback_subtracts_target_baseline():
    """When comp has no baseline but target does, we still must return INCREMENTAL,
    not total promo volume."""
    no_baseline_comp = PromoRecord(
        market="UK",
        retailer="Catalogmart UK",
        promo_name="Catalogmart Spring Sale",
        promo_type="promo",
        promo_start="2026-03-01",
        promo_end="2026-03-13",
        duration_days=13,
        discount_pct=1.0,  # REDACTED
        units_sold=0,  # REDACTED
        baseline_units_weekly=None,  # no comp baseline
        notes="(cross-region: UK)",
        month=3,
    )
    history = PromoHistory(
        records=[no_baseline_comp],
        baseline_units_weekly=1.0,  # REDACTED
        sample_count=1,
        retailer_matched="Bulkclub UK",
        fallback_source="enriched with UK data",
    )
    request = _bulkclub_uk_request()
    promo_weeks = 27 / 7

    result = estimate_incremental_units(history, request, promo_weeks=promo_weeks)

    # [REDACTED] — comp weekly rate scaled by discount ratio, minus target
    # baseline. Still high (no comp baseline means we can't compute lift
    # ratio), but at least it's net of the target baseline.
    assert result < 0, f"Got {result}, expected to subtract target baseline from total."  # REDACTED


if __name__ == "__main__":
    # Allow running without pytest installed.
    test_path4_uses_lift_ratio_not_absolute_volume()
    test_path4_skips_comp_with_missing_duration()
    test_path4_fallback_subtracts_target_baseline()
    print("All Path 4 regression tests passed.")
