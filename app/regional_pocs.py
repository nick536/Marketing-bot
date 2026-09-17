"""Regional retail POC mapping.

Single source of truth for "given a `Region` from Promo Tracker, who is
the retail POC for that geo?" Used by all three reminder types to
fan-out tags by geography rather than by submitter.

The actual region definitions (POCs + calendar columns + aliases) now
live in `app.regions` so calendar verifier and POC tagging stay in sync.
This module is a thin backward-compat shim.
"""
from __future__ import annotations

from app.regions import REGIONS, poc_ids_for as _poc_ids_for

# Deprecated: use app.regions.poc_ids_for(). Kept as a thin backward-compat
# view for tests/callers that import this dict directly.
REGIONAL_POC_MAP: dict[str, list[str]] = {
    canonical: info["poc_ids"]
    for canonical, info in REGIONS.items()
}


def get_regional_poc_ids(region: str | None) -> list[str]:
    """Return the list of Slack user IDs for the regional retail POC(s).
    Empty list if region is empty/None/unknown — caller should treat as
    "no regional fan-out" and tag only the default keepers.

    Delegates to app.regions.poc_ids_for() which handles alias normalization
    (UAE/GCC, DE/Germany, AT/Austria, etc. — see app/regions.py).
    """
    return _poc_ids_for(region)
