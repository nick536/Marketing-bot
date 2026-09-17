"""Slack modal form for structured promo submissions — no regex parsing needed.

The form only asks for what the bot can't look up itself.
brand funding %, return rate, buy price → all from Bot Input Variables tab.
"""

import logging
from app.config import _FALLBACK_RETAILER_REGION_MAP

logger = logging.getLogger(__name__)


def _build_retailer_options() -> tuple[dict, list]:
    """Build retailer options from live sheet + fallback config.

    Called at form-build time so new retailers added to the sheet
    appear in the dropdown without a redeploy.
    """
    try:
        from app.lookups import get_retailer_region_map
        live_map = get_retailer_region_map()
    except Exception:
        live_map = {}

    # Merge: live sheet takes precedence, fallback fills gaps
    merged = {**_FALLBACK_RETAILER_REGION_MAP, **live_map}

    retailer_region: dict[str, str] = {}
    for name, region in merged.items():
        title = name.title()
        retailer_region[title] = region

    return retailer_region, sorted(retailer_region.keys())

_REGION_OPTIONS = [
    "US", "CA", "UK", "Europe", "GCC", "India", "Poland",
    "AU", "NZ", "JP", "SG", "MY", "TH", "PH", "Mexico", "Israel", "ZA",
]

_DISTRIBUTOR_OPTIONS = [
    "Mapleline", "Gulfshore", "Vistula", "Albion UK", "Lowlands NL", "Nordica AB",
    "Alderon", "Novarep", "Ozlink", "Kiwilink", "Sakura", "Straitline", "Isleco",
    "Harbourline Ventures", "APCOM", "Pedalworks", "Veldcorp", "Alpencomp",
    "Iberica (Galerie ES)", "Galerie FR", "Nordlink (GGR)", "Continental Distribution",
    "Carmel", "Voltmart", "Shopnet", "Gadgetree",
]

_MARKETING_TYPE_OPTIONS = [
    ("discount_promo", "Discount Promo"),
    ("spiv", "SPIV (Sales Incentive)"),
    ("ecom_campaign", "Ecom / Marketing Campaign"),
    ("emailer", "Emailer / Newsletter"),
    ("sales_contest", "Sales Contest"),
    ("product_launch", "Product Launch"),
    ("pos", "POS Investment"),
]

_CURRENCY_OPTIONS = ["USD", "EUR", "GBP", "AED", "CAD", "INR", "AUD", "NZD", "JPY"]


def build_promo_modal() -> dict:
    """Build the Slack modal view JSON for /promo command.

    Only asks for what the bot can't look up:
    - Who: retailer + region
    - What: marketing type + discount/spend details
    - When: dates
    - Why: context
    """
    _retailer_region, _retailer_options = _build_retailer_options()
    return {
        "type": "modal",
        "callback_id": "promo_form_submit",
        "title": {"type": "plain_text", "text": "Promo Evaluation"},
        "submit": {"type": "plain_text", "text": "Evaluate"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            # --- INSTRUCTIONS ---
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        "*How this works*\n"
                        "Fill in the promo details below. The bot auto-pulls buy price, "
                        "return rate, brand funding %, and BAU from the Promo Sheet — you don't need to enter those.\n\n"
                        "It will post a full P&L evaluation (scenarios, grade, recommendation) "
                        "to #claude-marketing-approvals for Approver's review."
                    ),
                },
            },
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": (
                            ":clipboard: *Before submitting, make sure:*\n"
                            "1. Past promos for this retailer are logged in the Promo Sheet (region tab) — the bot uses them for comparables and BAU\n"
                            "2. Bot Input Variables tab has a row for this distributor/region (buy price, return rate, brand TD%)\n"
                            "3. Sales tab has recent monthly sell-out data for BAU calculation\n"
                            "4. If this is a new retailer/region, add it to the sheet first — otherwise the bot uses defaults ([REDACTED] buy, [REDACTED] returns)"
                        ),
                    },
                ],
            },
            {"type": "divider"},
            # --- WHO ---
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "*Who is this for?*"},
            },
            # Promo Scope: retailer vs distributor
            {
                "type": "input",
                "block_id": "scope_block",
                "label": {"type": "plain_text", "text": "Promo Scope"},
                "element": {
                    "type": "static_select",
                    "action_id": "scope_select",
                    "options": [
                        {"text": {"type": "plain_text", "text": "Retailer — specific store or chain"}, "value": "retailer"},
                        {"text": {"type": "plain_text", "text": "Distributor — all accounts (e.g. all Gulfshore)"}, "value": "distributor"},
                    ],
                    "initial_option": {
                        "text": {"type": "plain_text", "text": "Retailer — specific store or chain"},
                        "value": "retailer",
                    },
                },
                "hint": {"type": "plain_text", "text": "Distributor = promo applies to all retailers under that distributor. BAU and history are aggregated across all accounts."},
            },
            # Distributor — prominent for distributor-scope promos
            {
                "type": "input",
                "block_id": "distributor_block",
                "optional": True,
                "label": {"type": "plain_text", "text": "Distributor (required for distributor-scope; optional for retailer-scope)"},
                "element": {
                    "type": "static_select",
                    "action_id": "distributor_select",
                    "options": [
                        {"text": {"type": "plain_text", "text": d}, "value": d}
                        for d in _DISTRIBUTOR_OPTIONS
                    ],
                    "placeholder": {"type": "plain_text", "text": "Select distributor"},
                },
                "hint": {"type": "plain_text", "text": "For distributor-scope promos: select the distributor (e.g. Gulfshore). Leave blank for retailer-scope — it will be auto-derived from region."},
            },
            # Retailer (optional for distributor-scope, required for retailer-scope)
            {
                "type": "input",
                "block_id": "retailer_block",
                "optional": True,
                "label": {"type": "plain_text", "text": "Retailer (required for retailer-scope; leave blank for distributor-scope)"},
                "element": {
                    "type": "static_select",
                    "action_id": "retailer_select",
                    "options": [
                        {"text": {"type": "plain_text", "text": r}, "value": r}
                        for r in _retailer_options
                    ],
                    "placeholder": {"type": "plain_text", "text": "Search retailer..."},
                },
                "hint": {"type": "plain_text", "text": "For distributor-scope, leave blank to cover all retailers. For retailer-scope, select the specific store or chain."},
            },
            # Region (auto-derived but overridable)
            {
                "type": "input",
                "block_id": "region_block",
                "optional": True,
                "label": {"type": "plain_text", "text": "Region (auto-detected from retailer or distributor, override if needed)"},
                "element": {
                    "type": "static_select",
                    "action_id": "region_select",
                    "options": [
                        {"text": {"type": "plain_text", "text": r}, "value": r}
                        for r in _REGION_OPTIONS
                    ],
                    "placeholder": {"type": "plain_text", "text": "Override region"},
                },
            },
            {"type": "divider"},
            # --- WHAT ---
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "*What's the promo?*"},
            },
            # Marketing Type
            {
                "type": "input",
                "block_id": "type_block",
                "label": {"type": "plain_text", "text": "Marketing Type"},
                "element": {
                    "type": "static_select",
                    "action_id": "type_select",
                    "options": [
                        {"text": {"type": "plain_text", "text": label}, "value": value}
                        for value, label in _MARKETING_TYPE_OPTIONS
                    ],
                    "placeholder": {"type": "plain_text", "text": "Select type"},
                },
                "hint": {"type": "plain_text", "text": "Discount Promo = % off retail. SPIV = per-ring sales incentive. Ecom = marketing spend (ads, placement)."},
            },
            # Discount % (only for discount promos)
            {
                "type": "input",
                "block_id": "discount_block",
                "optional": True,
                "label": {"type": "plain_text", "text": "Discount % (consumer-facing discount off retail)"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "discount_input",
                    "placeholder": {"type": "plain_text", "text": "e.g. 15"},
                },
                "hint": {"type": "plain_text", "text": "Leave blank for non-discount promos. the brand's share of the discount is auto-pulled from the sheet."},
            },
            # Marketing Spend — currency + amount side by side
            {
                "type": "input",
                "block_id": "spend_currency_block",
                "optional": True,
                "label": {"type": "plain_text", "text": "Spend Currency"},
                "element": {
                    "type": "static_select",
                    "action_id": "spend_currency_select",
                    "options": [
                        {"text": {"type": "plain_text", "text": c}, "value": c}
                        for c in _CURRENCY_OPTIONS
                    ],
                    "initial_option": {"text": {"type": "plain_text", "text": "USD"}, "value": "USD"},
                },
            },
            {
                "type": "input",
                "block_id": "spend_amount_block",
                "optional": True,
                "label": {"type": "plain_text", "text": "Spend Amount (total marketing investment)"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "spend_amount_input",
                    "placeholder": {"type": "plain_text", "text": "e.g. 5000"},
                },
                "hint": {"type": "plain_text", "text": "Fixed spend: ad placement, MDF, SBA, training cost, display unit cost, etc. This is the amount the brand needs to recover."},
            },
            # SPIV per unit (only for SPIV type)
            {
                "type": "input",
                "block_id": "spiv_block",
                "optional": True,
                "label": {"type": "plain_text", "text": "SPIV $/ring (sales incentive per ring sold)"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "spiv_input",
                    "placeholder": {"type": "plain_text", "text": "e.g. 17.50"},
                },
                "hint": {"type": "plain_text", "text": "Paid to retail staff per ring sold. Applies to ALL units (baseline + incremental), not just incremental."},
            },
            # SOA per unit
            {
                "type": "input",
                "block_id": "soa_block",
                "optional": True,
                "label": {"type": "plain_text", "text": "SOA $/ring (sell-out accelerator per ring)"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "soa_input",
                    "placeholder": {"type": "plain_text", "text": "e.g. 2.50"},
                },
                "hint": {"type": "plain_text", "text": "Activation cost per ring — e.g. in-store demo, display placement fee."},
            },
            # the brand bears per unit (overrides TD% calculation)
            {
                "type": "input",
                "block_id": "brand_bears_block",
                "optional": True,
                "label": {"type": "plain_text", "text": "the brand bears $/ring (if explicitly agreed)"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "brand_bears_input",
                    "placeholder": {"type": "plain_text", "text": "e.g. 34.50"},
                },
                "hint": {"type": "plain_text", "text": "Only fill if the brand's per-unit cost is explicitly agreed (overrides the auto-calculated TD from discount %)."},
            },
            # Rebate %
            {
                "type": "input",
                "block_id": "rebate_block",
                "optional": True,
                "label": {"type": "plain_text", "text": "Rebate % (of buy price)"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "rebate_input",
                    "placeholder": {"type": "plain_text", "text": "e.g. 5"},
                },
                "hint": {"type": "plain_text", "text": "Rebate paid back to distributor as % of buy price. Leave blank if none."},
            },
            {"type": "divider"},
            # --- WHEN ---
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "*When?*"},
            },
            {
                "type": "input",
                "block_id": "start_date_block",
                "optional": True,
                "label": {"type": "plain_text", "text": "Start Date"},
                "element": {
                    "type": "datepicker",
                    "action_id": "start_date_pick",
                },
            },
            {
                "type": "input",
                "block_id": "end_date_block",
                "optional": True,
                "label": {"type": "plain_text", "text": "End Date"},
                "element": {
                    "type": "datepicker",
                    "action_id": "end_date_pick",
                },
                "hint": {"type": "plain_text", "text": "Duration is calculated from these dates. If no dates, bot defaults to 14 days."},
            },
            {"type": "divider"},
            # --- WHY / CONTEXT ---
            {
                "type": "input",
                "block_id": "context_block",
                "optional": True,
                "label": {"type": "plain_text", "text": "Additional Context"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "context_input",
                    "multiline": True,
                    "placeholder": {"type": "plain_text", "text": "e.g. Follow-up to May promo that did 120 units. Competitor running 20% off. Retailer anniversary event."},
                },
                "hint": {"type": "plain_text", "text": "The more context you give, the better the AI commentary. Include: why now, what happened last time, any retailer pressure."},
            },
        ],
    }


def parse_form_submission(view: dict) -> "PromoRequest":
    """Convert Slack modal submission values → PromoRequest (no regex needed).

    brand funding %, return rate, buy price are NOT in the form —
    the evaluator pulls these from the Bot Input Variables tab automatically.
    """
    from app.parser import PromoRequest, validate_promo_request

    values = view["state"]["values"]

    def _get_text(block_id: str, action_id: str) -> str:
        block = values.get(block_id, {})
        action = block.get(action_id, {})
        return (action.get("value") or "").strip()

    def _get_select(block_id: str, action_id: str) -> str:
        block = values.get(block_id, {})
        action = block.get(action_id, {})
        selected = action.get("selected_option")
        return selected["value"] if selected else ""

    def _get_date(block_id: str, action_id: str) -> str:
        block = values.get(block_id, {})
        action = block.get(action_id, {})
        return action.get("selected_date") or ""

    def _parse_float(s: str) -> float:
        s = s.replace(",", "").replace("$", "").replace("€", "").replace("£", "").strip()
        try:
            return float(s)
        except (ValueError, TypeError):
            return 0.0

    # Extract fields
    promo_scope = _get_select("scope_block", "scope_select") or "retailer"
    retailer = _get_select("retailer_block", "retailer_select")
    region_override = _get_select("region_block", "region_select")
    distributor = _get_select("distributor_block", "distributor_select")
    promo_type = _get_select("type_block", "type_select")
    discount_str = _get_text("discount_block", "discount_input")
    spend_currency = _get_select("spend_currency_block", "spend_currency_select") or "USD"
    spend_str = _get_text("spend_amount_block", "spend_amount_input")
    spiv_str = _get_text("spiv_block", "spiv_input")
    soa_str = _get_text("soa_block", "soa_input")
    brand_bears_str = _get_text("brand_bears_block", "brand_bears_input")
    rebate_str = _get_text("rebate_block", "rebate_input")
    start_date = _get_date("start_date_block", "start_date_pick")
    end_date = _get_date("end_date_block", "end_date_pick")
    context = _get_text("context_block", "context_input")

    # Auto-derive region: retailer → region, or distributor → region
    _retailer_region, _ = _build_retailer_options()
    _DISTRIBUTOR_REGION_MAP = {
        "Gulfshore": "GCC", "Vistula": "Poland", "Mapleline": "CA",
        "Ozlink": "AU", "Kiwilink": "NZ", "Sakura": "JP", "Straitline": "SG",
        "Isleco": "PH", "Harbourline Ventures": "PH", "APCOM": "APCOM",
        "Pedalworks": "TH", "Meridian": "MY", "Veldcorp": "ZA", "Alpencomp": "Europe",
        "Alderon": "Europe", "Albion UK": "UK", "Lowlands NL": "Europe", "Nordica AB": "Nordics",
        "Iberica (Galerie ES)": "Europe", "Galerie FR": "Europe",
        "Nordlink (GGR)": "Europe", "Continental Distribution": "Europe",
        "Carmel": "Israel", "Novarep": "Mexico",
        "Voltmart": "India", "Shopnet": "India", "Gadgetree": "India",
    }
    if region_override:
        region = region_override
    elif retailer:
        region = _retailer_region.get(retailer, "")
    elif distributor:
        region = _DISTRIBUTOR_REGION_MAP.get(distributor, "")
    else:
        region = ""

    # For distributor scope with no retailer, clear retailer so BAU aggregates correctly
    if promo_scope == "distributor" and not retailer:
        retailer = ""

    # Build dates list
    dates = []
    if start_date:
        dates.append(start_date)
    if end_date:
        dates.append(end_date)

    # Duration from dates
    duration = None
    if start_date and end_date:
        from datetime import datetime
        try:
            d1 = datetime.strptime(start_date, "%Y-%m-%d")
            d2 = datetime.strptime(end_date, "%Y-%m-%d")
            days = (d2 - d1).days
            if days > 0:
                duration = f"{days} days"
        except ValueError:
            pass

    # Build raw_text for display/logging
    type_label = dict(_MARKETING_TYPE_OPTIONS).get(promo_type, promo_type)
    scope_label = "All " + (distributor or region) if promo_scope == "distributor" else (retailer or region)
    raw_parts = [f"{scope_label} ({region}) — {type_label}"]
    if discount_str:
        raw_parts.append(f"{discount_str}% discount")
    if spend_str:
        raw_parts.append(f"{spend_currency} {spend_str} marketing spend")
    if spiv_str:
        raw_parts.append(f"SPIV ${spiv_str}/unit")
    if soa_str:
        raw_parts.append(f"SOA ${soa_str}/unit")
    if brand_bears_str:
        raw_parts.append(f"the brand bears ${brand_bears_str}/unit")
    if rebate_str:
        raw_parts.append(f"Rebate {rebate_str}%")
    if dates:
        raw_parts.append(f"Dates: {' to '.join(dates)}")
    if context:
        raw_parts.append(f"Context: {context}")

    req = PromoRequest(
        retailer=retailer or None,
        region=region or None,
        promo_scope=promo_scope,
        distributor=distributor or None,
        promo_type=promo_type or "promo",
        discount_pct=_parse_float(discount_str) if discount_str else None,
        discount_amount=_parse_float(spend_str) if spend_str else None,
        discount_currency=spend_currency,
        spiv_per_unit=_parse_float(spiv_str) if spiv_str else None,
        soa_per_unit=_parse_float(soa_str) if soa_str else None,
        brand_bears_per_unit=_parse_float(brand_bears_str) if brand_bears_str else None,
        rebate_pct=_parse_float(rebate_str) if rebate_str else None,
        # Return rate, buy price → pulled from Bot Input Variables tab by evaluator
        dates=dates,
        duration=duration,
        raw_text=" | ".join(raw_parts),
        parse_confidence="high",
    )
    return validate_promo_request(req)
