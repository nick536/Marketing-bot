from typing import Optional

from app.parser import PromoRequest
from app.models import PromoEvaluation, SensitivityRow, ScenarioRow, InfluencerPL
from app.config import APPROVER_USER_ID, RETAIL_PRICE_USD, COGS_PER_RING_USD


def _fmt(n: float) -> str:
    if n < 0:
        return f"-${abs(n):,.0f}"
    return f"${n:,.0f}"


def build_influencer_pl_block(pl: Optional[InfluencerPL]) -> str:
    """Render the per-unit influencer CM3 waterfall (reference-sheet layout).
    Zero-valued line items are omitted. '' when pl is None.

    Per-unit lines use :.2f (cents matter for one ring); cm3_cash uses :,.0f
    as it is an aggregate.
    """
    if pl is None:
        return ""
    lines = ["*Influencer CM3 (per unit)*",
             f"  Gross ${pl.gross:.2f}"]
    lines.append(f"  − Trade Discount ${pl.trade_discount:.2f}")
    if pl.channel_margin:
        lines.append(f"  − Channel Margin ${pl.channel_margin:.2f}")
    if pl.marketplace_commission:
        lines.append(f"  − Marketplace Commission ${pl.marketplace_commission:.2f}")
    lines.append(f"  − Returns ${pl.returns:.2f}")
    if pl.tax:
        lines.append(f"  − Tax ${pl.tax:.2f}")
    lines.append(f"  *= Net Revenue ${pl.net_revenue:.2f}*")
    lines.append(f"  − COGS ${pl.cogs:.2f}  − Commission ${pl.commission:.2f}"
                 f"  − Marketing ${pl.marketing:.2f}  − Warranty ${pl.warranty:.2f}")
    if pl.payment_gateway:
        lines.append(f"  − Payment Gateway ${pl.payment_gateway:.2f}")
    lines.append(f"  *CM3 ${pl.cm3_per_unit:.2f}/unit  ({pl.cm3_pct:.1f}% of net)*"
                 f"  —  grade {pl.grade}")
    lines.append(f"  ≈ ${pl.cm3_cash:,.0f} CM3-cash at BAU run-rate "
                 f"({pl.estimated_units:.0f} units, excl. promo uplift)")
    return "\n".join(lines)


def build_evaluation_blocks(
    promo: PromoRequest,
    ev: PromoEvaluation,
) -> list[dict]:
    blocks = []
    pl = ev.pl_impact
    hist = ev.promo_history
    vel = ev.velocity
    econ = ev.market_economics

    grade_label = {"A": "APPROVE", "B": "CONDITIONAL", "C": "REVIEW NEEDED", "REJECT": "NOT RECOMMENDED"}
    grade_emoji = {"A": ":large_green_circle:", "B": ":large_yellow_circle:", "C": ":red_circle:", "REJECT": ":no_entry:"}
    promo_scope = getattr(promo, "promo_scope", "retailer")
    if promo_scope == "distributor":
        distributor_name = promo.distributor or (ev.market_economics.distributor if ev.market_economics else "") or promo.region or "Distributor"
        retailer_label = f"All {distributor_name}"
    else:
        retailer_label = promo.retailer or promo.region or "Unknown"
    disc_label = f"{promo.discount_pct:.0f}%" if promo.discount_pct else "N/A"

    # ── Section 1: Grade Header ──
    # Influencer evals grade on per-unit CM3% where B = APPROVE; use the verdict
    # so the header never contradicts the verdict line shown below.
    header_label = (ev.verdict if ev.influencer_pl is not None
                    else grade_label.get(ev.grade, ev.grade))
    blocks.append({
        "type": "header",
        "text": {"type": "plain_text",
                 "text": f"Grade {ev.grade}: {header_label}"},
    })
    blocks.append({
        "type": "section",
        "text": {"type": "mrkdwn",
                 "text": f"{grade_emoji.get(ev.grade, ':white_circle:')} *{retailer_label}* ({promo.region or '?'}) | {disc_label} discount"},
    })
    blocks.append({"type": "divider"})

    # ── Section 2: Request ──
    lines = [":clipboard: *Request*"]
    if promo_scope == "distributor":
        lines.append(f"Scope: *Distributor-level* — {retailer_label} ({promo.region or '?'})")
    elif promo.retailer:
        lines.append(f"Retailer: {promo.retailer} | Region: {promo.region or '?'}")
    if promo.promo_type and promo.promo_type not in ("promo", ""):
        lines.append(f"Type: {promo.promo_type}")
    if promo.discount_pct:
        disc_line = f"Discount: {disc_label} (~${RETAIL_PRICE_USD * promo.discount_pct / 100:.0f} off)"
        if promo.brand_funding_pct:
            disc_line += f" | the brand funds {promo.brand_funding_pct}%, partner funds {promo.discount_pct - promo.brand_funding_pct:.1f}%"
        lines.append(disc_line)
    if promo.brand_bears_per_unit:
        lines.append(f"the brand Support: ${promo.brand_bears_per_unit:.2f}/unit")
    if promo.discount_amount:
        lines.append(f"Marketing spend: {_fmt(promo.discount_amount)} {promo.discount_currency}")
    if promo.promo_type == "spiv" and promo.spiv_per_unit:
        lines.append(f"SPIV: ${promo.spiv_per_unit:.2f}/unit (on all units sold)")
    elif promo.soa_per_unit:
        lines.append(f"SOA: ${promo.soa_per_unit:.2f}/unit")
    if promo.rebate_pct:
        lines.append(f"Rebate: {promo.rebate_pct:.1f}% of buy price per unit")
    if promo.addon_cogs_per_unit:
        lines.append(f"{promo.addon_cogs_label}: ${promo.addon_cogs_per_unit:.0f}/unit")
    if promo.dates:
        lines.append(f"Dates: {', '.join(promo.dates)}")
    if promo.duration:
        lines.append(f"Duration: {promo.duration}")
    if promo.parse_confidence == "low":
        lines.append(":warning: _Low parse confidence — verify details above_")
    blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}})
    blocks.append({"type": "divider"})

    # ── Influencer short-circuit: render influencer CM3 block, skip retail sections ──
    if ev.influencer_pl is not None:
        ipl_text = build_influencer_pl_block(ev.influencer_pl)
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": ipl_text}})
        blocks.append({"type": "divider"})
        if ev.risk_flags:
            warning_lines = "\n".join(f":warning: {rf.detail}" for rf in ev.risk_flags)
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": warning_lines}})
            blocks.append({"type": "divider"})
        if ev.commentary:
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": f":brain: *Analysis*\n{ev.commentary}"},
            })
            blocks.append({"type": "divider"})
        if ev.verdict:
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": f":mega: *Verdict: {ev.verdict}*"},
            })
        blocks.append({"type": "divider"})
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"<@{APPROVER_USER_ID}> — please review."},
        })
        return blocks

    # ── Section 3: Economics ──
    if econ:
        e_lines = [":bank: *Economics* (Bot Input Variables)"]
        if promo.brand_bears_per_unit:
            # When the brand bears is specified, show the flat $ support instead of TD%
            e_lines.append(
                f"Buy: ${econ.buy_price_usd:.0f} | Retail: ${econ.retail_price_usd:.0f} | "
                f"the brand Support: ${promo.brand_bears_per_unit:.2f}/unit | Returns: {econ.return_rate*100:.0f}% | "
                f"COGS: ${COGS_PER_RING_USD}"
            )
        else:
            e_lines.append(
                f"Buy: ${econ.buy_price_usd:.0f} | Retail: ${econ.retail_price_usd:.0f} | "
                f"TD the brand: {econ.trade_discount_brand_pct*100:.0f}% | Returns: {econ.return_rate*100:.0f}% | "
                f"COGS: ${COGS_PER_RING_USD}"
            )
        margin = econ.buy_price_usd - (econ.return_rate * econ.buy_price_usd) - COGS_PER_RING_USD
        if promo.brand_bears_per_unit:
            margin -= promo.brand_bears_per_unit
        elif promo.discount_pct:
            td_per_unit = econ.retail_price_usd * (promo.discount_pct / 100) * econ.trade_discount_brand_pct
            margin -= td_per_unit
        if promo.addon_cogs_per_unit:
            margin -= promo.addon_cogs_per_unit
            e_lines.append(f"{promo.addon_cogs_label}: ${promo.addon_cogs_per_unit:.0f}/unit")
        if promo.rebate_pct:
            rebate_pu = econ.buy_price_usd * (promo.rebate_pct / 100)
            margin -= rebate_pu
            e_lines.append(f"Rebate: {promo.rebate_pct:.1f}% of buy price = ${rebate_pu:.2f}/unit")
        e_lines.append(f"Margin/unit: ${margin:.0f}")
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(e_lines)}})
        blocks.append({"type": "divider"})

    # ── Section 4: Historical Reference ──
    direct_count = sum(1 for r in hist.records if "(cross-region" not in (r.notes or ""))
    cross_count = sum(1 for r in hist.records if "(cross-region" in (r.notes or ""))
    record_label = f"{hist.sample_count} promos on record"
    if cross_count > 0:
        record_label = f"{direct_count} direct + {cross_count} cross-region promos"

    h_lines = [f":bar_chart: *Historical* ({hist.retailer_matched or retailer_label} — {record_label})"]
    if hist.comparable_promo:
        c = hist.comparable_promo
        comp_weekly = c.incremental_units / max(c.duration_days / 7, 1) if c.incremental_units > 0 and c.duration_days > 0 else None
        comp_source_parts = []
        if c.market:
            comp_source_parts.append(c.market)
        if c.retailer:
            comp_source_parts.append(c.retailer)
        comp_source_label = " · ".join(comp_source_parts) if comp_source_parts else ""
        is_cross_region = "(cross-region" in (c.notes or "")

        comp_desc = f"Comp: *{c.promo_name or c.season_event}*"
        if comp_source_label:
            comp_desc += f" ({comp_source_label})"
        if c.discount_pct is not None:
            comp_desc += f" — {c.discount_pct:.0f}% off, {c.units_sold} units in {c.duration_days}d"
        else:
            comp_desc += f" — {c.units_sold} units in {c.duration_days}d"
        if comp_weekly:
            comp_desc += f", {comp_weekly:.0f} incr/wk"
        if c.roas:
            comp_desc += f", ROAS {c.roas}x"
        if is_cross_region:
            comp_desc += " :globe_with_meridians:"
        h_lines.append(comp_desc)
    if hist.baseline_units_weekly:
        bau_label = "Baseline (all accounts)" if promo_scope == "distributor" else "Baseline"
        h_lines.append(f"{bau_label}: ~{hist.baseline_units_weekly:.0f} units/wk (non-promo)")
        if promo_scope == "distributor" and hist.retailer_breakdown:
            breakdown_parts = [f"{r}: {v:.0f}" for r, v in sorted(hist.retailer_breakdown.items(), key=lambda x: -x[1])]
            h_lines.append("  ↳ " + " · ".join(breakdown_parts))
    if hist.avg_roas is not None:
        h_lines.append(f"Avg ROAS: {hist.avg_roas}x | Best: {hist.best_roas}x | Worst: {hist.worst_roas}x")
    if hist.fallback_source:
        h_lines.append(f":warning: _Fallback: {hist.fallback_source}_")
    if ev.seasonality_note:
        h_lines.append(f":calendar: _{ev.seasonality_note}_")
    blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(h_lines)}})
    blocks.append({"type": "divider"})

    # ── Section 4b: Market Context (Amazon as demand signal) ──
    MONTH_NAMES = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
                   "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    ad = ev.ad_metrics
    if ad and ad.available:
        ad_lines = [":globe_with_meridians: *Market Context* (Amazon ads, last 30d)"]

        trend_arrow = {"up": ":arrow_upper_right:", "flat": ":left_right_arrow:", "down": ":arrow_lower_right:"}
        roas_str = f"{ad.roas}x ROAS" if ad.roas else "N/A"
        prior_str = f" (was {ad.roas_prior_30d}x)" if ad.roas_prior_30d else ""
        ad_lines.append(f"Amazon {ad.marketplace_key}: *{roas_str}*{prior_str} {trend_arrow.get(ad.roas_trend, '')}")

        spend_fmt = f"{ad.total_spend:,.0f}"
        ad_lines.append(f"Spend: {spend_fmt} | Orders: {ad.total_orders:,} | CTR: {ad.ctr}%")

        if ad.roas_by_type and len(ad.roas_by_type) > 1:
            sorted_types = sorted(ad.roas_by_type.items(), key=lambda x: x[1], reverse=True)
            best_type, best_roas = sorted_types[0]
            worst_type, worst_roas = sorted_types[-1]
            ad_lines.append(f"Best: {best_type} ({best_roas}x) | Worst: {worst_type} ({worst_roas}x)")

        # Monthly ROAS context: show target month vs annual average
        mad = ev.monthly_ad
        if mad and mad.available and mad.target_month:
            m_name = MONTH_NAMES[mad.target_month] if mad.target_month <= 12 else "?"
            if mad.target_month_roas and mad.annual_avg_roas:
                delta = "higher" if mad.target_month_roas > mad.annual_avg_roas else "lower"
                ad_lines.append(
                    f"*{m_name} ROAS: {mad.target_month_roas}x* (annual avg: {mad.annual_avg_roas}x — {delta})"
                )
                if mad.target_month_roas_by_type:
                    parts = [f"{t}: {r}x" for t, r in sorted(mad.target_month_roas_by_type.items())]
                    ad_lines.append(f"  {m_name} by type: {' | '.join(parts)}")
            elif mad.annual_avg_roas:
                ad_lines.append(f"No {m_name} data — annual avg: {mad.annual_avg_roas}x")

        # Demand signal interpretation
        if ad.roas_trend == "down":
            ad_lines.append("_Demand signal: declining — acquisition getting harder across channels_")
        elif ad.roas_trend == "up":
            ad_lines.append("_Demand signal: improving — market receptivity increasing_")

        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(ad_lines)}})
        blocks.append({"type": "divider"})

    # Show monthly context even when trailing 30d has no data
    elif not (ad and ad.available):
        mad = ev.monthly_ad
        if mad and mad.available and mad.target_month:
            m_name = MONTH_NAMES[mad.target_month] if mad.target_month <= 12 else "?"
            ad_lines = [":globe_with_meridians: *Market Context* (Amazon monthly data)"]
            if mad.target_month_roas:
                ad_lines.append(f"*{m_name} ROAS: {mad.target_month_roas}x* (annual avg: {mad.annual_avg_roas}x)")
                if mad.target_month_roas_by_type:
                    parts = [f"{t}: {r}x" for t, r in sorted(mad.target_month_roas_by_type.items())]
                    ad_lines.append(f"  {m_name} by type: {' | '.join(parts)}")
            elif mad.annual_avg_roas:
                ad_lines.append(f"No {m_name} data — annual avg: {mad.annual_avg_roas}x")
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(ad_lines)}})
            blocks.append({"type": "divider"})

    # ── Section 4c: Lift Curves (Amazon promo + historical) ──
    _append_lift_curves(blocks, promo, ev)

    # ── Section 4d: Ad Campaign History (SPA/SBA tab data) ──
    ac = ev.ad_campaign
    if ac and ac.available:
        ac_lines = [f":bar_chart: *Ad Campaign History* ({ac.tab_name})"]
        ac_lines.append(
            f"Spend: {_fmt(ac.total_spend)} | Sales: {_fmt(ac.total_sales)} | "
            f"ROAS: *{ac.roas:.1f}x*"
        )
        ac_lines.append(
            f"Orders: {ac.total_orders:,} | Units: {ac.total_units:,} | "
            f"CTR: {ac.ctr}% | CVR: {ac.cvr}%"
        )
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(ac_lines)}})
        blocks.append({"type": "divider"})

    # ── Section 5: P&L Waterfall ──
    p_lines = [":moneybag: *P&L Waterfall*"]
    if pl.incremental_units > 0:
        # Show volume source for ROAS-based estimates
        vol_source = ""
        if ev.data_quality and ev.data_quality.startswith("ROAS-based:"):
            vol_source = f" _(via {ev.data_quality})_"
        p_lines.append(f"Incremental units: *{pl.incremental_units}*{vol_source}")
        p_lines.append("")
        u = pl.incremental_units
        buy = econ.buy_price_usd
        retail = econ.retail_price_usd
        disc = promo.discount_pct or 0
        td_pct = econ.trade_discount_brand_pct * 100 if econ else 0  # REDACTED
        td_per_unit = round(pl.trade_discount_total / u) if u else 0
        ret_per_unit = round(pl.returns_cost / u) if u else 0
        p_lines.append(f"`{'Gross Revenue':<22}{_fmt(pl.gross_revenue_excl_channel):>10}  (${buy:.0f}/u × {u}u)`")
        # Show "the brand Support" when flat $ bears is used, "Trade Disc" otherwise
        if pl.brand_bears_per_unit > 0:
            p_lines.append(f"`{'the brand Support':<22}-{_fmt(pl.trade_discount_total):>10}  (${pl.brand_bears_per_unit:.0f}/u × {u}u)`")
        else:
            p_lines.append(f"`{'Trade Disc (the brand '+f'{td_pct:.0f}%)':<22}-{_fmt(pl.trade_discount_total):>10}  (${td_per_unit}/u × {u}u)`")
        p_lines.append(f"`{'Returns ('+f'{pl.return_rate_used*100:.0f}%)':<22}-{_fmt(pl.returns_cost):>10}  (${ret_per_unit}/u × {u}u)`")
        p_lines.append(f"`{'Net Revenue':<22}{_fmt(pl.net_revenue):>10}`")
        p_lines.append(f"`{'COGS ($'+f'{COGS_PER_RING_USD}/ring)':<22}-{_fmt(pl.cogs_total):>10}  ([REDACTED] × {u}u)`")
        if pl.addon_cogs_per_unit > 0:
            label = pl.addon_cogs_label or "Addon COGS"
            p_lines.append(f"`{label:<22}-{_fmt(pl.addon_cogs_total):>10}`")
        p_lines.append(f"`{'Gross Margin':<22}{_fmt(pl.gross_margin):>10}`")
        p_lines.append(f"`{'Marketing':<22}-{_fmt(pl.marketing_spend):>10}`")
        if pl.soa_per_unit > 0:
            cost_label = "SPIV" if promo.promo_type == "spiv" else "SOA"
            p_lines.append(f"`{f'{cost_label} (${pl.soa_per_unit:.0f}/u)':<22}-{_fmt(pl.soa_total):>10}  (${pl.soa_per_unit:.0f}/u × {u}u)`")
        if pl.rebate_per_unit > 0:
            p_lines.append(f"`{f'Rebate (${pl.rebate_per_unit:.0f}/u)':<22}-{_fmt(pl.rebate_total):>10}`")
        p_lines.append(f"`{'Warranty ([REDACTED] NR)':<22}-{_fmt(pl.warranty_cost):>10}  ([REDACTED] × {_fmt(pl.net_revenue)})`")
        p_lines.append(f"`{'─' * 34}`")
        p_lines.append(f"`{'*CM3-Cash (Excl Tax)':<22}{_fmt(pl.cm3_cash):>10}*`")
        p_lines.append("")
        p_lines.append(f"*CM3% (Excl Tax): {pl.cm3_cash_pct}% of Net Rev | ROAS: {pl.roas}x*")
    else:
        p_lines.append("_Could not estimate volume — fill in promo history or Bot Input Variables tab._")
        if pl.margin_per_unit > 0:
            p_lines.append(f"Margin/unit: {_fmt(pl.margin_per_unit)}")

    if pl.breakeven_units:
        p_lines.append(f"Breakeven: *{pl.breakeven_units} units* to cover {_fmt(pl.marketing_spend)} spend")
    blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(p_lines)}})
    blocks.append({"type": "divider"})

    # ── Section 6: Scenario Analysis (Bear/Base/Bull/StrongBull) ──
    if pl.scenarios:
        has_uplift = any(s.uplift_pct is not None for s in pl.scenarios)

        # For pure discount promos with no fixed spend, CM3% is mathematically constant
        # across all scenarios (it's per-unit margin / per-unit net rev, volume-independent).
        # Showing the same % four times is noise — collapse to a single header note instead.
        pct_values = {s.cm3_cash_pct for s in pl.scenarios if s.cm3_cash_pct is not None}
        grades = {s.grade for s in pl.scenarios}
        constant_pct = len(pct_values) == 1 and pl.marketing_spend == 0

        if constant_pct:
            fixed_pct = next(iter(pct_values))
            fixed_grade = next(iter(grades))
            sc_lines = [
                f":chart_with_upwards_trend: *Scenario Analysis* | "
                f"CM3% fixed at {fixed_pct:.1f}% → Grade {fixed_grade} regardless of volume"
            ]
            sc_lines.append("_Volume affects CM3-Cash dollars only. Grade improves via lower discount or better TD terms._")
            if has_uplift:
                sc_lines.append("`Scenario      Units  CM3-Cash   BAU Uplift`")
                for s in pl.scenarios:
                    uplift_str = f"+{int(s.uplift_pct)}%" if s.uplift_pct is not None else ""
                    sc_lines.append(f"`{s.label:<13} {s.units:>5}  {_fmt(s.cm3_cash):>9}  {uplift_str}`")
            else:
                sc_lines.append("`Scenario      Units  CM3-Cash`")
                for s in pl.scenarios:
                    sc_lines.append(f"`{s.label:<13} {s.units:>5}  {_fmt(s.cm3_cash):>9}`")
        elif has_uplift:
            sc_lines = [":chart_with_upwards_trend: *Scenario Analysis*"]
            sc_lines.append("`Scenario      Units  CM3(ExTax)   CM3%  Grade  BAU Uplift`")
            for s in pl.scenarios:
                pct_str = f"{s.cm3_cash_pct:.1f}%" if s.cm3_cash_pct is not None else "N/A"
                uplift_str = f"+{int(s.uplift_pct)}%" if s.uplift_pct is not None else ""
                sc_lines.append(f"`{s.label:<13} {s.units:>5}  {_fmt(s.cm3_cash):>9}  {pct_str:>5}  {s.grade:<6} {uplift_str}`")
        else:
            sc_lines = [":chart_with_upwards_trend: *Scenario Analysis*"]
            sc_lines.append("`Scenario      Units  CM3(ExTax)  CM3%  Grade`")
            for s in pl.scenarios:
                pct_str = f"{s.cm3_cash_pct:.1f}%" if s.cm3_cash_pct is not None else "N/A"
                sc_lines.append(f"`{s.label:<13} {s.units:>5}  {_fmt(s.cm3_cash):>9}  {pct_str:>5}  {s.grade}`")
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(sc_lines)}})
        blocks.append({"type": "divider"})

    # ── Section 7: Recommendations ──
    if ev.recommendations:
        r_lines = [":bulb: *Recommendations*"]
        for i, rec in enumerate(ev.recommendations, 1):
            r_lines.append(f"{i}. {rec}")
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(r_lines)}})
        blocks.append({"type": "divider"})

    # ── Section 7.5: AI Commentary ──
    if getattr(ev, "commentary", ""):
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f":brain: *Analysis*\n{ev.commentary}"},
        })
        blocks.append({"type": "divider"})

    # ── Section 8: Verdict ──
    if ev.verdict:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn",
                     "text": f":mega: *Verdict: {ev.verdict}*"},
        })
    else:
        # Fallback verdict from grade
        verdict_map = {
            "A": "APPROVE",
            "B": "CONDITIONAL — review conditions above",
            "C": "REVIEW NEEDED — insufficient margin or data",
            "REJECT": "DO NOT APPROVE — negative CM3",
        }
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn",
                     "text": f":mega: *Verdict: {verdict_map.get(ev.grade, ev.grade)}*"},
        })

    blocks.append({"type": "divider"})

    # ── Data quality + Velocity (compact footer) ──
    footer_parts = []
    if vel and vel.direction != "unknown":
        arrow = {"up": ":arrow_upper_right:", "flat": ":left_right_arrow:", "down": ":arrow_lower_right:"}
        footer_parts.append(
            f"Velocity: ~{vel.weekly_run_rate}/wk {arrow.get(vel.direction, '')} ({vel.pct_change:+.1f}%)"
        )
    if ev.data_quality and ev.data_quality != "direct match":
        footer_parts.append(f"Data: {ev.data_quality}")
    tokens_in = getattr(ev, "llm_tokens_in", 0)
    tokens_out = getattr(ev, "llm_tokens_out", 0)
    if tokens_in or tokens_out:
        footer_parts.append(f"LLM: {tokens_in:,}↑ {tokens_out:,}↓ tokens")
    if footer_parts:
        blocks.append({
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": " | ".join(footer_parts)}],
        })


    return blocks


def _append_lift_curves(
    blocks: list[dict],
    promo: "PromoRequest",
    ev: "PromoEvaluation",
) -> None:
    """Add lift curve benchmark section.

    Shows retailer historical lift vs Amazon lift side-by-side so Approver can
    judge whether the estimate is in the right ballpark. Priority chain:
    Retailer history >> Amazon (direct geo) >> Amazon (similar geo)
    """
    ad = ev.ad_metrics
    hist = ev.promo_history
    has_amazon = (ad and ad.available and ad.promo_lift and ad.promo_lift.available)
    has_historical = bool(hist.records)

    if not has_amazon and not has_historical:
        return
    # Skip if historical records exist but no baseline to compute lift (all rows would show "Default")
    hist_baseline = hist.baseline_units_weekly or 0
    if not has_amazon and hist_baseline <= 0:
        return

    # Amazon source label
    amz_source = ""
    if has_amazon:
        pl = ad.promo_lift
        if pl.fallback_source:
            amz_source = f" :globe_with_meridians: _{pl.fallback_source}_"
        else:
            amz_source = f" ({ad.marketplace_key})"

    lines = [f":chart_with_upwards_trend: *Lift Benchmark*{amz_source}"]
    lines.append("`Discount     Retailer    Amazon   Source`")

    # Build historical lift by bracket from promo records
    hist_lift = {}
    hist_count = {}
    baseline = hist.baseline_units_weekly or 0
    if baseline > 0:
        bracket_data = {}
        for r in hist.records:
            if r.discount_pct is not None and r.units_sold > 0 and r.duration_days > 0:
                weekly = r.units_sold / max(r.duration_days / 7, 1)
                rec_base = r.baseline_units_weekly or baseline
                if rec_base > 0:
                    lift = weekly / rec_base
                    if r.discount_pct <= 15:
                        bracket_data.setdefault("5-15", []).append(lift)
                    elif r.discount_pct <= 25:
                        bracket_data.setdefault("15-25", []).append(lift)
                    else:
                        bracket_data.setdefault("25+", []).append(lift)
        for bracket, lifts in bracket_data.items():
            hist_lift[bracket] = round(sum(lifts) / len(lifts), 1)
            hist_count[bracket] = len(lifts)

    amz_lift = ad.promo_lift.lift_by_bracket if has_amazon else {}

    # Seasonal lift (target quarter) if available
    sl = ev.seasonal_lift
    has_seasonal = sl and sl.available and sl.seasonal_lift_by_bracket
    if has_seasonal:
        lines[0] += f" | Seasonal: {sl.seasonal_source}"
        lines[1] = "`Discount     Retailer    Amazon  Seasonal  Source`"

    for bracket in ["5-15", "15-25", "25+"]:
        label = f"{bracket}%"
        h_val = f"{hist_lift[bracket]:.1f}x" if bracket in hist_lift else "—"
        a_val = f"{amz_lift[bracket]:.1f}x" if bracket in amz_lift else "—"
        s_val = f"{sl.seasonal_lift_by_bracket[bracket]:.1f}x" if has_seasonal and bracket in sl.seasonal_lift_by_bracket else "—"
        # Show which source the bot uses for this bracket
        if bracket in hist_lift:
            src = f"Retailer ({hist_count[bracket]})"
        elif has_seasonal and bracket in sl.seasonal_lift_by_bracket:
            src = "Seasonal"
        elif bracket in amz_lift:
            src = "Amazon (annual)"
        else:
            src = "Default"
        if has_seasonal:
            lines.append(f"`{label:<13}{h_val:>8}    {a_val:>8}  {s_val:>8}  {src}`")
        else:
            lines.append(f"`{label:<13}{h_val:>8}    {a_val:>8}   {src}`")

    # Benchmark annotation for this specific promo
    if promo.discount_pct and ev.amazon_benchmark_lift:
        disc = promo.discount_pct
        amz_b = ev.amazon_benchmark_lift
        # Compute what lift the bot actually used
        if ev.pl_impact.incremental_units > 0 and baseline > 0:
            # Approximate weeks from duration string or default to 1
            est_weeks = 1
            if promo.duration:
                import re
                wk_match = re.search(r'(\d+)\s*w', promo.duration.lower())
                if wk_match:
                    est_weeks = max(1, int(wk_match.group(1)))
            used_lift = (ev.pl_impact.incremental_units / est_weeks + baseline) / baseline
            comparison = ""
            if used_lift > amz_b * 1.3:
                comparison = " :warning: estimate above Amazon benchmark"
            elif used_lift < amz_b * 0.5:
                comparison = " :small_blue_diamond: conservative vs Amazon"
            lines.append(f"_This promo ({disc:.0f}%): est {used_lift:.1f}x vs Amazon {amz_b:.1f}x{comparison}_")
        else:
            lines.append(f"_This promo ({disc:.0f}%) → Amazon benchmark: {amz_b:.1f}x lift ({ev.amazon_benchmark_source})_")

    blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}})
    blocks.append({"type": "divider"})


def build_error_reply() -> list[dict]:
    return [{
        "type": "section",
        "text": {"type": "mrkdwn",
                 "text": f":warning: Analysis failed. <@{APPROVER_USER_ID}> please review manually."},
    }]
