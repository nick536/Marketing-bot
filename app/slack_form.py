"""Parse structured Slack form fields from approval messages.

Promo submitters use a Slack workflow that posts messages with bold
field labels: *Region*\\n<value>. We parse these directly instead of
relying on LLM classification — eliminates a class of bugs (mis-classified
Marketing Type, missed dates, region typos) since the form is the
authoritative source.
"""
from __future__ import annotations

import re

# Pattern: bold field label on one line, value on the next.
# *Region*
# Europe
# Tolerates trailing whitespace. Stops at the next *Field* header.
_FIELD_RE = re.compile(
    r"\*([^*\n]+?)\*\s*\n([^\n*]*(?:\n(?!\*)[^\n*]*)*)",
    re.MULTILINE,
)

# Canonical → list of label spellings the form may use (case-insensitive).
# Submitters / form versions sometimes vary capitalization.
_FIELD_ALIASES: dict[str, list[str]] = {
    "region":            ["region", "region/country", "geography"],
    "channel":           ["channel"],
    "retailer":          ["retailer", "retail partner"],
    "marketing_type":    ["marketing type", "type", "promo type", "campaign type"],
    "discount_pct":      ["discount %", "discount", "discount percent"],
    "commission_pct":    ["commission %", "commission percent"],
    "marketing_spend":   ["marketing investment", "marketing spend", "spend", "budget"],
    "start_date":        ["start date", "start"],
    "end_date":          ["end date", "end"],
    "spiv":              ["spiv per unit", "spiv"],
    "additional_context":["additional context", "context", "notes"],
}


def parse_slack_form(text: str | None) -> dict[str, str]:
    """Extract structured fields from a Slack form parent message.

    Returns a dict keyed by canonical field name — see ``_FIELD_ALIASES`` for
    the full set (includes channel, commission_pct, and others). Missing fields
    are absent from the result; NA / blank / dash values are returned as empty
    strings.

    Caller can fall back to LLM extraction for fields not present in
    the form output.
    """
    if not text:
        return {}
    out: dict[str, str] = {}
    for match in _FIELD_RE.finditer(text):
        raw_label = match.group(1).strip().lower()
        raw_value = match.group(2).strip()
        # Normalize NA / dash to empty
        if raw_value.upper() in ("NA", "N/A", "-", "--"):
            raw_value = ""
        # Map label to canonical
        for canonical, aliases in _FIELD_ALIASES.items():
            if raw_label in aliases:
                # First match wins (form fields are unique)
                if canonical not in out:
                    out[canonical] = raw_value
                break
    return out


def has_form_fields(text: str | None) -> bool:
    """Return True if the message looks like a Slack form output (has at
    least 3 *Bold* labels). Caller can use this to decide whether to
    parse the form vs fall back to LLM extraction."""
    if not text:
        return False
    return len(_FIELD_RE.findall(text)) >= 3
