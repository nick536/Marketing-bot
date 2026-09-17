"""Newsletter sponsorship evaluator — third-party email-list placements.

Distinct from `newsletter` / `emailer` (which is the brand or retailer sending to their
OWN list — uses retail BAU + lift). A newsletter SPONSORSHIP is a flat-fee
placement in a third-party list (cold audience). The buyer flow is D2C.

Mirrors SKILL.md Step 4d. Uses D2C economics ([REDACTED] sell, [REDACTED] brand-funded TD,
[REDACTED] return rate). Pro Ring COGS = [REDACTED] when detected; otherwise [REDACTED].

Reach funnel (industry-standard email benchmarks if user doesn't supply):
  opens     = list_size × open_rate          (default [REDACTED])
  clicks    = opens × CTR                    (default [REDACTED])
  purchases = clicks × CVR                   (default [REDACTED])
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from app.config import (
    COGS_PER_RING_USD,
    GRADE_A_FLOOR,
    GRADE_B_FLOOR,
    cogs_for_request,
)
from app.parser import PromoRequest

logger = logging.getLogger(__name__)


# D2C economics — same as evaluator_d2c.py
D2C_SELL_PRICE_USD = 0.0  # REDACTED
D2C_RETURN_RATE = 0.0  # REDACTED
WARRANTY_PCT_OF_NET_REV = 0.0  # REDACTED

# Industry-standard email reach funnel defaults (cold audience, paid placement)
DEFAULT_OPEN_RATE = 0.0  # REDACTED
DEFAULT_CTR = 0.0  # REDACTED
DEFAULT_CVR = 0.0  # REDACTED


@dataclass
class NewsletterSponsorshipResult:
    flat_fee_usd: float
    list_size: int
    open_rate: float
    ctr: float
    cvr: float
    discount_pct: float
    cogs_per_unit: float
    margin_per_unit: float
    funnel_purchases: int
    breakeven_rings: int
    funnel_cm3: float
    funnel_cm3_pct: Optional[float]
    grade: str
    verdict: str
    notes: list[str] = field(default_factory=list)


def _grade(cm3_cash: float, cm3_cash_pct: Optional[float]) -> str:
    if cm3_cash < 0:
        return "REJECT"
    if cm3_cash_pct is None:
        return "C"
    if cm3_cash_pct >= GRADE_A_FLOOR:
        return "A"
    if cm3_cash_pct >= GRADE_B_FLOOR:
        return "B"
    return "C"


def evaluate_newsletter_sponsorship(
    request: PromoRequest,
    raw_text: str = "",
    list_size: int = 0,
    open_rate: float = DEFAULT_OPEN_RATE,
    ctr: float = DEFAULT_CTR,
    cvr: float = DEFAULT_CVR,
) -> NewsletterSponsorshipResult:
    """Evaluate a newsletter sponsorship placement.

    Inputs:
      flat_fee   : `request.marketing_spend` — the placement cost.
      discount   : `request.discount_pct` — consumer discount applied at checkout (often 0 or 10%).
      list_size  : passed in explicitly (parsed by caller from message text).
      open_rate / ctr / cvr : either user-provided or industry defaults above.

    Output: per-unit D2C margin, breakeven rings, expected funnel purchases,
    CM3 at expected funnel volume, grade.
    """
    flat_fee = float(request.discount_amount or 0.0)
    discount = float(request.discount_pct or 0.0)
    cogs = cogs_for_request(raw_text)

    # Per-unit D2C economics
    consumer_price = D2C_SELL_PRICE_USD * (1 - discount / 100.0)
    returns_per_unit = D2C_RETURN_RATE * consumer_price
    net_rev_per_unit = consumer_price - returns_per_unit
    warranty_per_unit = WARRANTY_PCT_OF_NET_REV * net_rev_per_unit
    margin_per_unit = net_rev_per_unit - cogs - warranty_per_unit

    notes: list[str] = []
    if cogs == 1:  # REDACTED (was a real Pro Ring COGS value)
        notes.append("Pro Ring detected — COGS $[REDACTED].")
    else:
        notes.append("Regular Base Ring COGS $[REDACTED] (no Pro Ring signal in message).")

    if margin_per_unit <= 0:
        return NewsletterSponsorshipResult(
            flat_fee_usd=flat_fee,
            list_size=list_size,
            open_rate=open_rate,
            ctr=ctr,
            cvr=cvr,
            discount_pct=discount,
            cogs_per_unit=cogs,
            margin_per_unit=margin_per_unit,
            funnel_purchases=0,
            breakeven_rings=0,
            funnel_cm3=-flat_fee,
            funnel_cm3_pct=None,
            grade="REJECT",
            verdict=f"Margin/unit ≤ 0 at {discount:.0f}% discount with ${cogs} COGS. Cannot recover any fee.",
            notes=notes,
        )

    breakeven_rings = int(round(flat_fee / margin_per_unit)) if flat_fee > 0 else 0

    # Reach funnel
    if list_size > 0:
        opens = list_size * open_rate
        clicks = opens * ctr
        purchases = int(round(clicks * cvr))
    else:
        purchases = 0
        notes.append("List size not provided — funnel volume cannot be projected; only breakeven shown.")

    funnel_net_rev = purchases * net_rev_per_unit
    funnel_cm3 = purchases * margin_per_unit - flat_fee
    funnel_cm3_pct = (funnel_cm3 / funnel_net_rev * 100) if funnel_net_rev > 0 else None

    grade = _grade(funnel_cm3, funnel_cm3_pct)

    if purchases == 0:
        verdict = (
            f"Need {breakeven_rings} rings via tracked link/code to break even on ${flat_fee:,.0f}. "
            f"No list size given — supply it to project funnel volume."
        )
    elif funnel_cm3 >= 0:
        verdict = (
            f"Funnel projects ~{purchases} purchases ({list_size:,} list × "
            f"{open_rate*100:.0f}% open × {ctr*100:.1f}% CTR × {cvr*100:.1f}% CVR). "
            f"CM3 ${funnel_cm3:,.0f} ({funnel_cm3_pct:.1f}%). Breakeven {breakeven_rings} rings."
        )
    else:
        verdict = (
            f"Funnel projects only ~{purchases} purchases vs {breakeven_rings} needed to break even. "
            f"CM3 ${funnel_cm3:,.0f} ({funnel_cm3_pct:.1f}% of net rev). Renegotiate fee or list quality."
        )

    return NewsletterSponsorshipResult(
        flat_fee_usd=flat_fee,
        list_size=list_size,
        open_rate=open_rate,
        ctr=ctr,
        cvr=cvr,
        discount_pct=discount,
        cogs_per_unit=cogs,
        margin_per_unit=margin_per_unit,
        funnel_purchases=purchases,
        breakeven_rings=breakeven_rings,
        funnel_cm3=funnel_cm3,
        funnel_cm3_pct=funnel_cm3_pct,
        grade=grade,
        verdict=verdict,
        notes=notes,
    )
