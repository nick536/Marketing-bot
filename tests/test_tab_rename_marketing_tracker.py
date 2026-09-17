"""Tests for the 2026-05-15 tab rename + reconciler POS/Display skip.

CRITICAL: User renamed the Promo Tracker tab → 'Marketing Tracker' (capital T)
on 2026-05-15. Bot's TRACKER_TAB constant must match exactly or
_ensure_tracker_tab will silently CREATE a new empty 'Promo Tracker' tab on
next cron and orphan all 25 existing rows.
"""


def test_tracker_tab_constant_renamed():
    """Sheet tab was renamed from 'Promo Tracker' to 'Marketing Tracker'
    on 2026-05-15. Constant must match or _ensure_tracker_tab will create
    an empty 'Promo Tracker' tab and lose all rows."""
    from app.tracker import TRACKER_TAB
    assert TRACKER_TAB == "Marketing Tracker"


def test_reconciler_skips_pos_display():
    """Per 2026-05-15: POS / Display investments don't go in tracker.
    Everything else does (Training, SPA, SBA, Influencer, etc.)."""
    from app.reconcile import _should_add_to_tracker
    # Skip
    assert _should_add_to_tracker("POS Display") is False
    assert _should_add_to_tracker("Display Investment") is False
    assert _should_add_to_tracker("Displays") is False
    # Track everything else
    for mt in ["Discount", "Training", "SPA+SBA Campaign", "SOA",
               "Sales Contest", "Influencer Code", "Instagram KOL",
               "Promoter Trial", "Premium Retail Display",
               "Product Launch", "Event Coupon"]:
        assert _should_add_to_tracker(mt) is True, f"{mt!r} should track"
    # Empty / unknown → track (data quality signal)
    assert _should_add_to_tracker("") is True
    assert _should_add_to_tracker("Unknown") is True


def test_live_flow_skips_pos_display():
    """The live event flow (log_pending_promo) shares the same filter so
    both write paths agree on what reaches the Marketing Tracker."""
    from app.tracker import _should_track_marketing_type
    assert _should_track_marketing_type("POS") is False
    assert _should_track_marketing_type("Pos") is False
    assert _should_track_marketing_type("Display") is False
    assert _should_track_marketing_type("Discount") is True
    assert _should_track_marketing_type("Training") is True
    assert _should_track_marketing_type("") is True
