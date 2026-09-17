import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from fastapi import FastAPI, Request
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from slack_bolt import App
from slack_bolt.adapter.fastapi import SlackRequestHandler

from app.config import (
    SLACK_BOT_TOKEN, SLACK_SIGNING_SECRET, PROMO_CHANNEL_ID,
    DISTRIBUTOR_LEVEL_REGIONS, APPROVER_USER_IDS, DEBUG_TOKEN,
)
from app.lookups import get_region_distributor_map, get_region_keywords, get_debug_info
from app.regions import normalize_region, register_aliases
from app.parser import parse_promo_message, parse_workflow_message, is_d2c_request
from app.sheets import get_promo_history_with_fallback, get_market_economics, get_sales_velocity, get_ad_campaign_data, get_distributor_bau_breakdown
# Metabase disabled — velocity comes from sales tabs / promo records
# from app.metabase import get_velocity
from app.ad_metrics import get_ad_metrics, get_monthly_ad_roas, get_seasonal_lift
from app.evaluator import evaluate_promo
from app.responder import build_evaluation_blocks, build_error_reply
from app.tracker import (
    log_pending_promo, mark_approved, mark_rejected, mark_no_response,
    log_actuals, get_all_promos, start_reminder_scheduler,
    _build_actuals_confirmation,
    parse_actuals_from_reply,
)
from app.form import build_promo_modal, parse_form_submission
from app.llm import generate_commentary

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Fold sheet-supplied region spellings into the canonical resolver once at import.
try:
    register_aliases(get_region_keywords())
except Exception:  # pragma: no cover - never block startup on alias merge
    logger.warning("register_aliases(get_region_keywords()) failed at startup", exc_info=True)

# ignoring_self_events_enabled=False: Workflow Builder posts via this app's bot_id,
# so Bolt's default self-event filter silently drops workflow form messages.
# Safe because handle_message only calls _analyze_and_reply when parse_workflow_message
# succeeds — our own evaluation replies never parse as workflow forms.
bolt_app = App(token=SLACK_BOT_TOKEN, signing_secret=SLACK_SIGNING_SECRET, ignoring_self_events_enabled=False)
handler = SlackRequestHandler(bolt_app)

limiter = Limiter(key_func=get_remote_address)
app = FastAPI(title="Promo Approval Bot")
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


from fastapi import Depends, HTTPException
from fastapi.security import HTTPBearer

_bearer = HTTPBearer(auto_error=False)


def _debug_auth(credentials=Depends(_bearer)):
    """Dependency: require a valid DEBUG_TOKEN to access internal endpoints.

    Accepts token via Authorization: Bearer <token> header ONLY.
    Query param (?token=) is intentionally not supported — it would appear in
    Nginx access logs, Render log drains, browser history, and proxy logs.

    If DEBUG_TOKEN is not configured, all debug endpoints are blocked.
    """
    if not DEBUG_TOKEN:
        raise HTTPException(status_code=403, detail="Debug endpoints are disabled (DEBUG_TOKEN not configured)")
    provided = credentials.credentials if credentials else ""
    if not provided or provided != DEBUG_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid or missing debug token")


def _attach_commentary(promo, evaluation) -> None:
    """Call LLM commentary generation and attach result to evaluation.commentary.
    Fails silently — never blocks the main evaluation flow."""
    # Influencer evals carry a deterministic commentary set in evaluate_promo
    # and have no retail P&L for the LLM to summarize — skip the LLM call.
    if getattr(evaluation, "influencer_pl", None) is not None:
        return
    try:
        from app.config import ANTHROPIC_API_KEY
        if not ANTHROPIC_API_KEY:
            return

        pl = evaluation.pl_impact
        hist = evaluation.promo_history
        econ = evaluation.market_economics

        # Scenario rows
        scenarios = [
            {"label": s.label, "units": s.units, "cm3_cash": s.cm3_cash,
             "cm3_pct": s.cm3_cash_pct, "grade": s.grade}
            for s in (pl.scenarios or [])
        ]

        # Historical comparable
        comp = None
        if hist and hist.comparable_promo:
            c = hist.comparable_promo
            comp = {
                "name": c.promo_name or c.season_event,
                "discount_pct": c.discount_pct,
                "units_sold": c.units_sold,
                "incremental_units": c.incremental_units,
                "duration_days": c.duration_days,
                "roas": c.roas,
                "notes": c.notes,
            }

        promo_summary = {
            "retailer": promo.retailer,
            "region": promo.region,
            "distributor": getattr(promo, "distributor", None),
            "promo_type": promo.promo_type,
            "discount_pct": promo.discount_pct,
            "discount_amount": promo.discount_amount,
            "discount_currency": getattr(promo, "discount_currency", "USD"),
            "spiv_per_unit": promo.spiv_per_unit,
            "soa_per_unit": promo.soa_per_unit,
            "rebate_pct": promo.rebate_pct,
            "duration": promo.duration,
            "context": promo.raw_text if promo.raw_text else "",
        }
        eval_summary = {
            "grade": evaluation.grade,
            "buy_price": econ.buy_price_usd if econ else None,
            "retail_price": econ.retail_price_usd if econ else None,
            "return_rate_pct": round((econ.return_rate or 0) * 100, 1) if econ else None,
            "cogs_per_unit": 0,  # REDACTED
            "margin_per_unit": pl.margin_per_unit,
            "td_per_unit": pl.trade_discount_per_unit,
            "returns_per_unit": pl.returns_per_unit,
            "incremental_units": pl.incremental_units,
            "breakeven_units": pl.breakeven_units,
            "cm3_cash": pl.cm3_cash,
            "cm3_pct": pl.cm3_cash_pct,
            "roas": pl.roas,
            "total_investment": pl.total_promo_investment,
            "bau_weekly": hist.baseline_units_weekly if hist else None,
            "scenarios": scenarios,
            "historical_comparable": comp,
            "risk_flags": [{"flag": f.flag, "detail": f.detail} for f in evaluation.risk_flags],
            "conditions": evaluation.conditions,
            "data_quality": evaluation.data_quality,
        }
        result = generate_commentary(promo_summary, eval_summary)
        if result:
            evaluation.commentary, evaluation.llm_tokens_in, evaluation.llm_tokens_out = result
    except Exception as e:
        logger.warning(f"Commentary generation failed (non-blocking): {e}")


@app.on_event("startup")
def on_startup():
    """Start background scheduler and enforce required security configuration."""
    # Hard stop: APPROVER_USER_IDS must be set before the app is allowed to run.
    # This prevents the bot from operating with no authorization gate — e.g.
    # after a token rotation where the env var was not re-populated.
    if not APPROVER_USER_IDS:
        raise RuntimeError(
            "APPROVER_USER_IDS env var is not set or empty. "
            "Set it to a comma-separated list of authorized Slack user IDs "
            "(e.g. 'U0REDACT010') before starting the app."
        )
    logger.info(f"Startup: {len(APPROVER_USER_IDS)} authorized approver(s) configured.")

    try:
        start_reminder_scheduler(bolt_app.client)
    except Exception as e:
        logger.error(f"Reminder scheduler failed to start (non-blocking): {e}")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/webhook/promo")
@limiter.limit("10/minute")
async def webhook_promo(request: Request):
    """Receive structured promo data from Slack Workflow Builder form.

    Expected JSON keys (from Workflow Builder webhook step):
    - retailer, region, marketing_type, discount_pct,
      marketing_investment (e.g. "USD 20000" — currency + amount combined),
      spiv_per_unit, soa_per_unit, start_date, end_date, context
    """
    from fastapi.responses import JSONResponse
    from app.parser import PromoRequest

    from slack_sdk.signature import SignatureVerifier

    raw_body = await request.body()
    timestamp = request.headers.get("X-Slack-Request-Timestamp", "")
    signature = request.headers.get("X-Slack-Signature", "")

    # Verify Slack HMAC signature when headers are present.
    # Only reject if headers ARE present but invalid — allows future
    # direct API calls without breaking anything.
    if timestamp and signature:
        verifier = SignatureVerifier(SLACK_SIGNING_SECRET)
        if not verifier.is_valid(body=raw_body, timestamp=timestamp, signature=signature):
            logger.warning("webhook_promo: invalid Slack signature — request rejected")
            return JSONResponse(status_code=403, content={"error": "Invalid Slack signature"})

    import json as _json
    body = _json.loads(raw_body)
    logger.info(f"Webhook promo received: {body}")

    retailer = body.get("retailer", "")
    region = body.get("region", "")
    marketing_type = body.get("marketing_type", "promo")
    discount_str = str(body.get("discount_pct", "")).strip()

    # Marketing investment: "USD 20000" or just "20000"
    investment_raw = str(body.get("marketing_investment", "")).strip()
    spend_currency = "USD"
    spend_str = ""
    if investment_raw:
        parts = investment_raw.split()
        if len(parts) >= 2 and parts[0].isalpha():
            spend_currency = parts[0].upper()
            spend_str = " ".join(parts[1:])
        else:
            spend_str = investment_raw
    # Also accept separate fields as fallback
    if not spend_str:
        spend_str = str(body.get("spend_amount", "")).strip()
        spend_currency = body.get("spend_currency", "USD") or spend_currency

    spiv_str = str(body.get("spiv_per_unit", "")).strip()
    soa_str = str(body.get("soa_per_unit", "")).strip()

    # Duration: "10th Apr - 20th Apr" or separate start/end dates
    duration_raw = str(body.get("duration", "")).strip()
    start_date = body.get("start_date", "")
    end_date = body.get("end_date", "")
    if duration_raw and not start_date:
        import dateparser
        # Split on " - " or " to "
        for sep in [" - ", " – ", " to "]:
            if sep in duration_raw:
                left, right = duration_raw.split(sep, 1)
                d1 = dateparser.parse(left.strip(), settings={"PREFER_DATES_FROM": "future"})
                d2 = dateparser.parse(right.strip(), settings={"PREFER_DATES_FROM": "future"})
                if d1:
                    start_date = d1.strftime("%Y-%m-%d")
                if d2:
                    end_date = d2.strftime("%Y-%m-%d")
                break

    _MAX_FIELD_BYTES = 10_240  # 10 KB per field
    def _cap(s: str) -> str:
        b = s.encode("utf-8")
        return b[:_MAX_FIELD_BYTES].decode("utf-8", errors="ignore") if len(b) > _MAX_FIELD_BYTES else s

    context = _cap(str(body.get("context", "")))
    distributor = str(body.get("distributor", "")).strip()
    submitter = body.get("submitter", "")
    store_name = str(body.get("store_name", "")).strip() or None
    promo_scope = str(body.get("promo_scope", "")).strip().lower()

    # Distributor is now mandatory — derive region from it when region is missing
    _DIST_REGION_MAP = {
        "Gulfshore": "GCC", "Vistula": "Poland", "Mapleline": "CA",
        "Ozlink": "AU", "Kiwilink": "NZ", "Sakura": "JP", "Straitline": "SG",
        "Isleco": "PH", "Harbourline Ventures": "PH", "APCOM": "APCOM",
        "Alderon": "Europe", "Alpencomp": "Europe",
        "Albion UK": "UK", "Lowlands NL": "Europe", "Nordica AB": "Nordics",
        "Carmel": "Israel", "Novarep": "Mexico",
        "Siamline": "TH", "Meridian": "MY", "Veldcorp": "ZA",
    }
    if not region and distributor:
        region = _DIST_REGION_MAP.get(distributor, "")
        if region:
            logger.info(f"Region derived from distributor '{distributor}': {region}")
    # Normalise: accept "distributor", "distributor - all accounts", "all", etc.
    if promo_scope.startswith("distributor") or promo_scope == "all":
        promo_scope = "distributor"
    else:
        promo_scope = "retailer"

    def _safe_float(s):
        try:
            return float(s.replace(",", "")) if s else None
        except (ValueError, TypeError):
            return None

    # For POS type: if Marketing Investment is blank, try to parse cost from context
    # e.g. "The display costs 3000 AED" or "AED 3000" or "3000 AED"
    if marketing_type.lower().startswith("pos") and not spend_str and context:
        import re as _re
        # Match "3000 AED", "AED 3000", "3,000 AED", currency amounts
        _cost_m = _re.search(
            r'(?:(AED|USD|EUR|GBP|INR)\s*([\d,]+(?:\.\d{1,2})?)|([\d,]+(?:\.\d{1,2})?)\s*(AED|USD|EUR|GBP|INR))',
            context, _re.IGNORECASE,
        )
        if _cost_m:
            _curr = (_cost_m.group(1) or _cost_m.group(4) or "USD").upper()
            _amt_raw = _cost_m.group(2) or _cost_m.group(3) or ""
            _amt = _safe_float(_amt_raw)
            if _amt:
                spend_str = str(_amt)
                spend_currency = _curr
                logger.info(f"POS cost parsed from context: {_curr} {_amt}")

    dates = [d for d in [start_date, end_date] if d]
    duration = None
    if start_date and end_date:
        from datetime import datetime as dt
        try:
            days = (dt.strptime(end_date, "%Y-%m-%d") - dt.strptime(start_date, "%Y-%m-%d")).days
            if days > 0:
                duration = f"{days} days"
        except ValueError:
            pass

    scope_label = f"All {distributor or region}" if promo_scope == "distributor" else (retailer or region)
    raw_parts = [f"{scope_label} ({region}) — {marketing_type}"]
    if discount_str:
        raw_parts.append(f"{discount_str}% discount")
    if spend_str:
        raw_parts.append(f"{spend_currency} {spend_str} spend")
    if spiv_str:
        raw_parts.append(f"SPIV ${spiv_str}/unit")
    if soa_str:
        raw_parts.append(f"SOA ${soa_str}/unit")
    if dates:
        raw_parts.append(f"Dates: {' — '.join(dates)}")
    if context:
        raw_parts.append(f"Context: {context}")

    promo = PromoRequest(
        retailer=retailer or None,
        region=region or None,
        promo_type=marketing_type,
        promo_scope=promo_scope,
        discount_pct=_safe_float(discount_str),
        discount_amount=_safe_float(spend_str),
        discount_currency=spend_currency,
        spiv_per_unit=_safe_float(spiv_str),
        soa_per_unit=_safe_float(soa_str),
        distributor=distributor or None,
        store_name=store_name,
        dates=dates,
        duration=duration,
        raw_text=" | ".join(raw_parts),
        parse_confidence="high",
    )

    channel = PROMO_CHANNEL_ID
    client = bolt_app.client

    def _run():
        try:
            submitter_tag = f" (from <@{submitter}>)" if submitter else ""
            summary = f"*Promo request{submitter_tag}* (via form):\n{promo.raw_text}"
            post = client.chat_postMessage(channel=channel, text=summary)
            thread_ts = post["ts"]

            target_month = _guess_target_month(promo.raw_text)
            promo_weeks = _guess_promo_weeks(promo.raw_text, promo.duration, promo.dates)
            data = _fetch_all_data(promo, target_month=target_month)

            from app.models import PromoHistory, VelocityCheck, MarketEconomics, AdMetrics, AdCampaignData, MonthlyAdPerformance

            history = data.get("history") or PromoHistory()
            econ = data.get("econ") or MarketEconomics()
            velocity = data.get("velocity") or VelocityCheck()
            ad_metrics = data.get("ad_metrics") or AdMetrics()
            ad_campaign = data.get("ad_campaign") or AdCampaignData()
            monthly_ad = data.get("monthly_ad") or MonthlyAdPerformance()
            seasonal_lift = data.get("seasonal_lift")
            if data.get("bau_breakdown"):
                history.retailer_breakdown = data["bau_breakdown"]

            if velocity.direction == "unknown" and promo.region:
                sales_vel = get_sales_velocity(promo.region)
                if sales_vel:
                    velocity = VelocityCheck(
                        weekly_run_rate=sales_vel.get("weekly_run_rate", 0),
                        direction=sales_vel.get("direction", "unknown"),
                        pct_change=sales_vel.get("pct_change", 0),
                    )
            if velocity.direction == "unknown" and history.records:
                velocity = _velocity_from_promo_records(history.records)

            evaluation = evaluate_promo(
                request=promo, history=history, econ=econ, velocity=velocity,
                promo_weeks=promo_weeks, target_month=target_month, ad_metrics=ad_metrics,
                ad_campaign=ad_campaign, monthly_ad=monthly_ad,
                seasonal_lift=seasonal_lift,
            )
            _attach_commentary(promo, evaluation)

            blocks = build_evaluation_blocks(promo, evaluation, history, econ, velocity)
            client.chat_postMessage(
                channel=channel, thread_ts=thread_ts,
                blocks=blocks, text=f"Promo evaluation: {evaluation.grade}",
            )
            try:
                log_pending_promo(thread_ts, channel, promo, evaluation, submitter=submitter)
            except Exception as track_err:
                logger.error(f"Tracker logging failed: {track_err}")

        except Exception as e:
            logger.exception(f"Webhook promo evaluation failed: {e}")

    threading.Thread(target=_run, daemon=True).start()
    return {"status": "ok", "message": "Evaluation started"}


@app.get("/expenditure/summary")
def expenditure_summary(_: None = Depends(_debug_auth)):
    """Return aggregated marketing spend summary from Promo Tracker."""
    from app.expenditure import aggregate_tracker_data
    rows = get_all_promos()
    return aggregate_tracker_data(rows)


@app.get("/reminders/trigger")
def trigger_reminders(_: None = Depends(_debug_auth)):
    """Manually trigger reminder check (for testing)."""
    from app.tracker import check_and_send_reminders
    sent = check_and_send_reminders(bolt_app.client)
    return {"reminders_sent": sent}


@app.get("/reconcile/trigger")
def trigger_reconcile(days: int = 14, _: None = Depends(_debug_auth)):
    """Manually run the reconciliation pass.

    Walks recent threads in the marketing-approval channels, hands each to
    Sonnet for classification + extraction, and appends Approved rows for
    promos the live tracker write missed. Idempotent.

    Use `?days=180` for a one-shot historical backfill instead of the
    default 14-day daily window. Server-side capped at 365 days to keep
    Slack API call counts bounded.
    """
    from app.reconcile import reconcile_missed_approvals

    if days < 1:
        days = 1
    if days > 365:
        days = 365
    summary = reconcile_missed_approvals(bolt_app.client, days_back=days)
    return {"days_back": days, **summary}


@app.get("/debug/evaluate")
def debug_evaluate(text: str = "", _: None = Depends(_debug_auth)):
    """Run the full analysis pipeline on arbitrary text and return results or errors."""
    _MAX_FIELD_BYTES = 10_240
    b = text.encode("utf-8")
    if len(b) > _MAX_FIELD_BYTES:
        text = b[:_MAX_FIELD_BYTES].decode("utf-8", errors="ignore")
    try:
        promo = parse_promo_message(text)
        parsed = {
            "promo_type": promo.promo_type,
            "retailer": promo.retailer,
            "region": promo.region,
            "discount_pct": promo.discount_pct,
            "discount_amount": promo.discount_amount,
            "spiv_per_unit": promo.spiv_per_unit,
            "soa_per_unit": promo.soa_per_unit,
            "dates": promo.dates,
            "duration": promo.duration,
        }
        target_month = _guess_target_month(text)
        promo_weeks = _guess_promo_weeks(text, promo.duration, promo.dates)

        data = _fetch_all_data(promo, target_month=target_month)
        from app.models import PromoHistory, VelocityCheck, MarketEconomics, AdMetrics, AdCampaignData, MonthlyAdPerformance
        history = data.get("history") or PromoHistory()
        econ = data.get("econ") or MarketEconomics()
        velocity = data.get("velocity") or VelocityCheck()
        ad_metrics = data.get("ad_metrics") or AdMetrics()
        ad_campaign = data.get("ad_campaign") or AdCampaignData()
        monthly_ad = data.get("monthly_ad") or MonthlyAdPerformance()
        seasonal_lift = data.get("seasonal_lift")

        evaluation = evaluate_promo(
            request=promo, history=history, econ=econ,
            velocity=velocity, promo_weeks=promo_weeks, target_month=target_month,
            ad_metrics=ad_metrics, ad_campaign=ad_campaign,
            monthly_ad=monthly_ad, seasonal_lift=seasonal_lift,
        )
        return {
            "parsed": parsed,
            "target_month": target_month,
            "promo_weeks": promo_weeks,
            "grade": evaluation.grade,
            "incremental_units": evaluation.pl_impact.incremental_units,
            "cm3_cash": evaluation.pl_impact.cm3_cash,
            "spiv_total": evaluation.pl_impact.soa_total,
            "breakeven": evaluation.pl_impact.breakeven_units,
            "comparable": evaluation.promo_history.comparable_promo.promo_name if evaluation.promo_history.comparable_promo else None,
            "data_quality": evaluation.data_quality,
        }
    except Exception as e:
        logger.exception("debug_evaluate failed")
        return {"error": "Internal server error"}


@app.get("/debug/raw/{region}")
def debug_raw(region: str, _: None = Depends(_debug_auth)):
    """Show raw sheet data for a region to debug parsing."""
    from app.config import PROMO_SHEET_ID, REGION_TAB_KEYWORDS, REGION_TAB_MAP_FALLBACK
    from app.sheets import _get_client, _discover_tab, _find_header_row
    import gspread
    try:
        client = _get_client()
        sheet = client.open_by_key(PROMO_SHEET_ID)
        tab_name = _discover_tab(sheet, region, REGION_TAB_KEYWORDS, REGION_TAB_MAP_FALLBACK)
        if not tab_name:
            return {"error": f"No tab for {region}"}
        ws = sheet.worksheet(tab_name)
        all_values = ws.get_all_values()
        header_idx = _find_header_row(all_values)
        headers = [str(h).strip() for h in all_values[header_idx]]
        data_rows = []
        for row in all_values[header_idx + 1: header_idx + 6]:
            row_dict = {headers[i]: str(row[i]).strip() for i in range(min(len(headers), len(row)))}
            data_rows.append(row_dict)
        # Also run actual parser to compare
        from app.sheets import _parse_promo_records
        all_raw = []
        for row in all_values[header_idx + 1:]:
            row_dict = {headers[i]: str(row[i]).strip() for i in range(min(len(headers), len(row)))}
            all_raw.append(row_dict)
        history = _parse_promo_records(all_raw, "", region)
        return {
            "tab": tab_name,
            "total_rows": len(all_values),
            "header_idx": header_idx,
            "headers": headers,
            "first_5_data_rows": data_rows,
            "parser_result": {
                "records": len(history.records),
                "sample_count": history.sample_count,
                "avg_roas": history.avg_roas,
                "baseline": history.baseline_units_weekly,
                "matched": history.retailer_matched,
            },
            "raw_rows_passed": len(all_raw),
        }
    except Exception as e:
        logger.exception("debug_raw failed for region=%s", region)
        return {"error": "Internal server error"}


@app.get("/debug/sheets")
def debug_sheets(_: None = Depends(_debug_auth)):
    """Test every region's data pipeline — history, economics, velocity."""
    from app.config import PROMO_SHEET_ID, REGION_TAB_KEYWORDS
    from app.sheets import (
        _get_client, _discover_tab, get_promo_history_with_fallback,
        get_market_economics, get_sales_velocity, _read_input_variables,
    )
    result = {"regions": {}, "input_variables": {}}

    # Show all Bot Input Variables rows
    try:
        market_rows, partner_rows = _read_input_variables()
        result["input_variables"] = {
            "market_rows": market_rows,
            "partner_rows": partner_rows,
        }
    except Exception as e:
        result["input_variables"] = {"error": str(e)}

    # Discover tabs dynamically
    try:
        client = _get_client()
        sheet = client.open_by_key(PROMO_SHEET_ID)
        all_tabs = [ws.title for ws in sheet.worksheets()]
        result["available_tabs"] = all_tabs
    except Exception as e:
        result["tab_error"] = str(e)
        return result

    # Test each region that has keyword patterns
    for region in REGION_TAB_KEYWORDS:
        tab_name = _discover_tab(sheet, region, REGION_TAB_KEYWORDS, {})
        r = {"tab": tab_name or "NOT FOUND"}
        try:
            econ = get_market_economics(region, "")
            r["buy_price"] = econ.buy_price_usd
            r["td_pct"] = econ.trade_discount_brand_pct
            r["return_rate"] = econ.return_rate
            r["econ_ok"] = econ.buy_price_usd > 0
        except Exception as e:
            r["econ_error"] = str(e)

        try:
            history = get_promo_history_with_fallback("", region)
            r["promo_count"] = history.sample_count
            r["baseline"] = history.baseline_units_weekly
            r["matched"] = history.retailer_matched
            r["fallback"] = history.fallback_source
            r["history_ok"] = history.sample_count > 0
        except Exception as e:
            r["history_error"] = str(e)

        try:
            vel = get_sales_velocity(region)
            r["velocity"] = vel if vel else "no sales tab"
        except Exception as e:
            r["velocity_error"] = str(e)

        result["regions"][region] = r

    return result


@app.get("/debug/lookups")
def debug_lookups(_: None = Depends(_debug_auth)):
    """Show sheet-derived lookup maps (retailer→region, keywords, distributors)."""
    return get_debug_info()


@app.post("/slack/events")
async def slack_events(req: Request):
    return await handler.handle(req)


def _velocity_from_promo_records(records) -> "VelocityCheck":
    """Derive rough velocity from promo records when no sales tab data exists.

    Groups records by month, compares latest two months with data.
    """
    from app.models import VelocityCheck
    monthly = {}
    for rec in records:
        if rec.month and rec.units_sold > 0:
            monthly.setdefault(rec.month, []).append(rec.units_sold)

    if len(monthly) < 2:
        return VelocityCheck()

    # Sort by month, take last 2
    sorted_months = sorted(monthly.keys())
    latest_m = sorted_months[-1]
    prev_m = sorted_months[-2]
    latest_units = sum(monthly[latest_m])
    prev_units = sum(monthly[prev_m])

    weekly_rate = latest_units / 4.33
    pct_change = ((latest_units - prev_units) / prev_units * 100) if prev_units > 0 else 0
    if pct_change > 10:
        direction = "up"
    elif pct_change < -10:
        direction = "down"
    else:
        direction = "flat"

    return VelocityCheck(
        weekly_run_rate=int(round(weekly_rate)),
        direction=direction,
        pct_change=round(pct_change, 1),
    )


def _guess_target_month(text: str) -> int:
    """Extract target month from promo request text.

    Handles:
    - ISO date strings: "2026-05-15" → May (5)  ← workflow form submissions
    - Slash dates: "15/05/2026" or "05/15/2026"
    - Text months: "May", "June", etc.
    - Event keywords: "Black Friday" → 11, etc.
    """
    import re as _re

    # ISO date: YYYY-MM-DD — check first (workflow form always uses this format)
    iso_match = _re.search(r'\b(\d{4})-(\d{2})-\d{2}\b', text)
    if iso_match:
        m = int(iso_match.group(2))
        if 1 <= m <= 12:
            return m

    month_keywords = {
        "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
        "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6,
        "july": 7, "jul": 7, "august": 8, "aug": 8, "september": 9, "sep": 9,
        "october": 10, "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
        "mother's day": 5, "mothers day": 5, "father's day": 6, "fathers day": 6,
        "valentine": 2, "easter": 4, "spring": 4,
        "black friday": 11, "bfcm": 11, "cyber monday": 11,
        "christmas": 12, "new year": 1, "prime day": 10,
    }
    text_lower = text.lower()
    for keyword, month in sorted(month_keywords.items(), key=lambda x: -len(x[0])):
        if keyword in text_lower:
            return month
    return datetime.utcnow().month


def _guess_promo_weeks(text: str, duration: str = None, dates: list = None) -> float:
    """Compute promo duration in weeks from text, duration string, or parsed dates.

    Uses dateparser for robust date parsing — handles any format:
    "23 Mar to 6 April", "Apr 30 - May 13", "30/04 - 13/05", etc.
    """
    if duration:
        m = re.search(r'(\d+)\s*week', duration, re.IGNORECASE)
        if m:
            return float(m.group(1))
        m = re.search(r'(\d+)\s*day', duration, re.IGNORECASE)
        if m:
            return float(m.group(1)) / 7.0

    import dateparser
    dp_settings = {
        'PREFER_DATES_FROM': 'future',
        'RELATIVE_BASE': datetime.utcnow(),
    }

    # Try to extract a date range from the raw text
    # Split on common range separators: -, –, to, through, until
    range_pattern = re.compile(
        r'(\d{1,2}\s*[/\-.]\s*\d{1,2}\s*[/\-.]\s*\d{2,4}'    # numeric: 23/03/2026
        r'|\d{1,2}\s*(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*(?:\s+\d{2,4})?'  # 23 Mar, 23Mar
        r'|(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*\s*\d{1,2}(?:\s*,?\s*\d{2,4})?)'  # Mar 23, Mar23
        r'\s*(?:[-–]|to|through|until)\s*'
        r'(\d{1,2}\s*[/\-.]\s*\d{1,2}\s*[/\-.]\s*\d{2,4}'
        r'|\d{1,2}\s*(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*(?:\s+\d{2,4})?'
        r'|(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*\s*\d{1,2}(?:\s*,?\s*\d{2,4})?)',
        re.IGNORECASE,
    )
    m = range_pattern.search(text)
    if m:
        d1 = dateparser.parse(m.group(1), settings=dp_settings)
        d2 = dateparser.parse(m.group(2), settings=dp_settings)
        if d1 and d2:
            days = abs((d2 - d1).days)
            if days > 0:
                logger.info(f"Date range from text: '{m.group(1)}' to '{m.group(2)}' = {days}d")
                return days / 7.0

    # Fallback: try same-month range like "Nov 10-20"
    m = re.search(
        r'(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*\s+(\d{1,2})\s*[-–]\s*(\d{1,2})',
        text, re.IGNORECASE,
    )
    if not m:
        m = re.search(
            r'(\d{1,2})\s*[-–]\s*(\d{1,2})\s*(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)',
            text, re.IGNORECASE,
        )
    if m:
        start, end = int(m.group(1)), int(m.group(2))
        if end > start:
            return (end - start) / 7.0

    # Fallback: parse individual dates from the dates list
    if dates and len(dates) >= 2:
        d1 = dateparser.parse(dates[0], settings=dp_settings)
        d2 = dateparser.parse(dates[1], settings=dp_settings)
        if d1 and d2:
            days = abs((d2 - d1).days)
            if days > 0:
                logger.info(f"Date range from parsed dates: '{dates[0]}' to '{dates[1]}' = {days}d")
                return days / 7.0

    return 1.0


def _fetch_all_data(promo, target_month: int = 0):
    results = {}
    retailer = promo.retailer or ""
    region = promo.region or ""
    promo_scope = getattr(promo, "promo_scope", "retailer")
    # Canonical region resolution (sheet aliases registered once at startup).
    _canon = normalize_region(region)
    region = _canon if _canon else region

    # Scope-aware history/BAU retailer selection:
    #   distributor scope → aggregate across all retailer rows
    #   retailer scope + specific retailer → retailer-specific lookup
    #   retailer scope + no retailer (distributor-level region) → fallback to distributor aggregate
    history_retailer = retailer
    is_distributor_scope = False
    if promo_scope == "distributor":
        # Prefer explicit distributor field (now mandatory) over map lookup
        distributor = promo.distributor or get_region_distributor_map().get(region, "")
        if distributor:
            history_retailer = distributor
            is_distributor_scope = True
            logger.info(f"Distributor scope: using {distributor} for history/BAU lookup (all retailers)")
    elif not retailer and region in DISTRIBUTOR_LEVEL_REGIONS:
        # No retailer specified in a distributor-level region — default to aggregate
        distributor = promo.distributor or get_region_distributor_map().get(region, "")
        if distributor:
            history_retailer = distributor
            is_distributor_scope = True
            logger.info(f"No retailer + distributor-level region {region}: defaulting to {distributor} aggregate")
    elif region in DISTRIBUTOR_LEVEL_REGIONS and retailer:
        # Retailer explicitly specified in a distributor-level region → retailer-specific
        logger.info(f"Retailer scope in distributor-level region {region}: using {retailer} for lookup")

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {}
        if region:
            futures[executor.submit(get_promo_history_with_fallback, history_retailer, region)] = "history"
            futures[executor.submit(get_market_economics, region, retailer, promo.distributor or "")] = "econ"
            futures[executor.submit(get_ad_metrics, region)] = "ad_metrics"
            # Monthly ROAS breakdown for contextual estimation
            futures[executor.submit(get_monthly_ad_roas, region, target_month)] = "monthly_ad"
            # Seasonal lift curves (target quarter ± 1 month)
            if target_month:
                futures[executor.submit(get_seasonal_lift, region, target_month)] = "seasonal_lift"
            # Per-retailer BAU breakdown for distributor-scope promos
            if is_distributor_scope:
                futures[executor.submit(get_distributor_bau_breakdown, region)] = "bau_breakdown"
        # Fetch ad campaign tab data (SPA/SBA) for ecom campaigns or when ad channels detected
        if (promo.ad_channels or promo.promo_type == "ecom_campaign") and region:
            futures[executor.submit(get_ad_campaign_data, retailer, region)] = "ad_campaign"

        for future in as_completed(futures):
            key = futures[future]
            try:
                results[key] = future.result()
            except Exception as e:
                logger.error(f"Failed to fetch {key}: {e}")
                results[key] = None

    return results


def is_authorized_approver(client, user_id: str) -> bool:
    """Verify a Slack user is an authorized approver.

    Two-layer check:
    1. user_id must be in the APPROVER_USER_IDS env-configured set
    2. Slack API confirms the user exists and is not deactivated in the workspace

    This prevents spoofed webhook payloads or compromised tokens from
    approving promos by impersonating a known user ID.
    """
    if not user_id or user_id not in APPROVER_USER_IDS:
        return False
    try:
        info = client.users_info(user=user_id)
        user = info.get("user", {})
        if user.get("deleted", True):
            logger.warning(f"Approver check: user {user_id} is deactivated")
            return False
        return True
    except Exception as e:
        logger.error(f"Approver verification failed for {user_id}: {e}")
        return False


def _notify_calendar_keepers(client, channel: str, thread_ts: str) -> None:
    """On approval: locate the tracker row and delegate to send_calendar_fyi,
    which handles retail-promo filter + dedup via the Calendar FYI Sent At
    column. Fire-and-forget — never blocks the approval message."""
    from app.tracker import (
        _get_client, _ensure_tracker_tab, PROMO_SHEET_ID,
        send_calendar_fyi,
    )
    try:
        gc = _get_client()
        ws = _ensure_tracker_tab(gc.open_by_key(PROMO_SHEET_ID))
        rows = ws.get_all_records()
        for i, row in enumerate(rows, start=2):
            if str(row.get("Thread TS", "")).strip() == thread_ts:
                send_calendar_fyi(client, row, i, ws=ws)
                return
        logger.warning(f"calendar FYI: no tracker row matched thread {thread_ts}")
    except Exception:
        logger.exception("Failed to send calendar-keepers FYI")


def _resolve_target_thread_ts(parent_text: str, local_thread_ts: str) -> str:
    """Return the tracker thread_ts. When the approval was posted in
    #approver-approvals-apr-2026 the parent message typically links back to
    the bot evaluation in #claude-marketing-approvals — extract that TS so
    mark_approved() finds the row the bot originally logged.
    Falls back to the local thread_ts if no permalink is found."""
    if not PROMO_CHANNEL_ID:
        return local_thread_ts
    # Slack permalinks look like .../archives/<CHANNEL_ID>/p<17-digit-ts>
    # where p<digits> = timestamp with the dot removed (first 10 digits . last 6-7)
    m = re.search(rf"archives/{re.escape(PROMO_CHANNEL_ID)}/p(\d{{10}})(\d+)", parent_text or "")
    if m:
        return f"{m.group(1)}.{m.group(2)}"
    return local_thread_ts


def _classify_and_update(client, channel: str, thread_ts: str):
    """Read full thread, classify Approver's approval intent with Sonnet, update tracker."""
    from app.llm import classify_approval_intent
    from app.tracker import find_promo_by_thread

    try:
        # Read the full thread
        result = client.conversations_replies(channel=channel, ts=thread_ts, limit=50)
        messages = result.get("messages", [])
        if len(messages) < 2:
            return  # No replies yet

        # Cross-channel correlation: if approval was posted in the Approver approvals
        # channel, the row the bot logged lives under the claude-marketing-approvals
        # eval thread_ts — recover it from the permalink in the parent message.
        parent_text = messages[0].get("text", "") if messages else ""
        target_thread_ts = _resolve_target_thread_ts(parent_text, thread_ts)

        # Get user info for display names
        user_cache = {}
        thread_msgs = []
        approver_uid = ""
        for msg in messages:
            uid = msg.get("user", "")
            if uid not in user_cache:
                try:
                    info = client.users_info(user=uid)
                    user_cache[uid] = info["user"]["real_name"]
                except Exception:
                    user_cache[uid] = uid
            is_approver = is_authorized_approver(client, uid)
            if is_approver:
                approver_uid = uid
            thread_msgs.append({
                "user": user_cache[uid],
                "text": msg.get("text", ""),
                "is_approver": is_approver,
            })

        classification = classify_approval_intent(thread_msgs)
        if not classification:
            return

        decision = classification.get("decision", "pending")
        reason = classification.get("reason", "")
        logger.info(f"Approval classification for {thread_ts} (target={target_thread_ts}): {decision} — {reason}")

        if decision == "approved":
            success = mark_approved(
                target_thread_ts,
                actor_id=approver_uid,
                approval_channel_id=channel,
                approval_thread_ts=thread_ts,
            )
            if success:
                client.chat_postMessage(
                    channel=channel, thread_ts=thread_ts,
                    text=f":white_check_mark: Promo marked as *Approved*. _{reason}_\nWill follow up for actuals after it ends.",
                )
                # FYI the promo-calendar keepers on the original eval thread in
                # the promo channel. If approval happened in a different channel
                # (e.g. #approver-approvals-apr-2026) we still post the FYI to
                # #claude-marketing-approvals where submitters are watching.
                calendar_channel = PROMO_CHANNEL_ID or channel
                calendar_thread = target_thread_ts
                _notify_calendar_keepers(client, calendar_channel, calendar_thread)
        elif decision == "rejected":
            success = mark_rejected(target_thread_ts, actor_id=approver_uid)
            if success:
                client.chat_postMessage(
                    channel=channel, thread_ts=thread_ts,
                    text=f":x: Promo marked as *Rejected*. _{reason}_",
                )
        elif decision == "conditional":
            client.chat_postMessage(
                channel=channel, thread_ts=thread_ts,
                text=f":large_yellow_circle: *Conditional approval detected.* _{reason}_\nReply 'approved' once conditions are met to log to tracker.",
            )
        # "pending" → do nothing, wait for more context

    except Exception as e:
        logger.error(f"Approval classification failed: {e}")


_BOT_USER_ID_CACHE: "str | None" = None


def _get_bot_user_id(client) -> "str | None":
    """Resolve our own bot user_id once, cache forever. Used to filter
    out the bot's own thread replies (e.g. the chase ping example
    `actuals 379` would otherwise self-trigger logging)."""
    global _BOT_USER_ID_CACHE
    if _BOT_USER_ID_CACHE is not None:
        return _BOT_USER_ID_CACHE
    try:
        info = client.auth_test()
        _BOT_USER_ID_CACHE = info.get("user_id")
    except Exception as e:
        logger.warning(f"_get_bot_user_id: auth_test failed: {e}")
    return _BOT_USER_ID_CACHE


def _handle_thread_reply(
    client,
    channel: str,
    thread_ts: str,
    text: str,
    user: str,
    event: dict | None = None,
):
    """Handle replies in a promo evaluation thread (approvals, actuals).

    When an authorized approver posts in a thread, use Sonnet to classify
    approval intent from the full conversation context — not just keywords.
    """
    text_lower = text.lower().strip()

    # Actuals detection — `actuals N` inline OR a bare whole-message count
    # ("80", "80 units"). Both forms resolved by parse_actuals_from_reply,
    # shared with the backfill scanner in app.tracker.
    # Bug 2026-05-15: an earlier version ran 4 broad patterns (units/rings/pcs,
    # sold/did, around/~) that caught "236 units to breakeven", "we sold 30
    # last week", "around 50", "70 units at bulkclub" AND the bot's own chase
    # ping example "actuals 379". The bare form is ANCHORED (^...$) so it only
    # matches a message that is *nothing but* the count — prose numbers still
    # fall through. The 4 guards below (bot-self / row exists / Status==Approved
    # / idempotency) are the second line of defense.
    actual_units_parsed = parse_actuals_from_reply(text)

    if actual_units_parsed is not None:
        # Guard 1: bot-self filter. Don't log against our own chase ping
        # (which contains the example "actuals 379" in its body).
        # Bug 2026-05-15 dual-review: the prior `user == bot_uid` check
        # missed Slack bot messages that carry bot_id/subtype/app_id but
        # NO user field. Reuse the 5-layer _is_bot_reply helper from
        # app.tracker so all bot-post shapes are filtered consistently.
        from app.tracker import _is_bot_reply
        bot_uid = _get_bot_user_id(client)
        # Build the message dict _is_bot_reply expects. Prefer the full
        # event when the caller passed it (handle_message does so for
        # every thread reply); fall back to a synthesized minimal dict
        # for back-compat with any direct caller.
        msg_for_bot_check = event if event is not None else {
            "user": user,
            "text": text,
        }
        if _is_bot_reply(msg_for_bot_check, bot_uid):
            return

        # Guard 2: tracker row must exist for this thread.
        try:
            from app.tracker import get_promo_by_thread_ts
            promo_row = get_promo_by_thread_ts(thread_ts)
        except Exception as e:
            logger.warning(f"actuals inline: get_promo_by_thread_ts failed: {e}")
            return
        if not promo_row:
            return

        # Guard 3: only log actuals on Approved promos (not Pending /
        # Rejected / Initial).
        if promo_row.get("Status") != "Approved":
            return

        # Guard 4: idempotency — don't overwrite an already-filled
        # Actual Units cell. POC's novareply wins; corrections must
        # be applied manually to the sheet.
        existing = str(promo_row.get("Actual Units", "")).strip()
        if existing and existing != "-":
            return

        actual_units = actual_units_parsed
        # Optional CM3: "cm3 $1234" or "cm3 1234"
        cm3_match = re.search(r'cm3[:\s]*\$?([\d,.]+)', text_lower)
        actual_cm3 = float(cm3_match.group(1).replace(",", "")) if cm3_match else None

        # CM3 dropped from confirmation reply (2026-05-12): we only track units.
        # actual_cm3 is still passed through so back-compat with old reply syntax
        # (`actuals 379, cm3 -50`) doesn't break, but we don't surface it.
        result = log_actuals(thread_ts, actual_units, actual_cm3)
        if result:
            msg = _build_actuals_confirmation(
                units=actual_units,
                predicted=int(result.get("predicted_units") or 0),
            )
            client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text=msg,
            )
        return

    # Actuals reply: only relevant if the row has empty Actual Units AND past T+7.
    # The inline `actuals N` path above already handled exact unit logging.
    try:
        from app.tracker import (
            handle_actuals_reply, get_promo_by_thread_ts,
        )
        promo_row = get_promo_by_thread_ts(thread_ts)
        if promo_row and promo_row.get("Status") == "Approved":
            actual_filled = bool(str(promo_row.get("Actual Units", "")).strip())
            if not actual_filled:
                outcome = handle_actuals_reply(
                    client, thread_ts, channel, text, promo_row,
                )
                if outcome != "noop":
                    return
    except Exception as e:
        logger.warning(f"actuals reply dispatch failed: {e}")

    # Calendar reply: check if this thread is for an Approved promo whose
    # calendar status is still pending, and route to the bucket handler.
    try:
        from app.tracker import (
            handle_calendar_reply, get_promo_by_thread_ts,
            LOGGED_IN_CAL_COL,
        )
        promo_row = get_promo_by_thread_ts(thread_ts)
        if promo_row and promo_row.get("Status") == "Approved":
            already_logged = str(promo_row.get(LOGGED_IN_CAL_COL, "")).strip()
            if not already_logged:
                outcome = handle_calendar_reply(
                    client, thread_ts, channel, text, promo_row,
                )
                if outcome != "noop":
                    return
    except Exception as e:
        logger.warning(f"calendar reply dispatch failed: {e}")

    # Only classify approval when Approver posts (and no actuals were detected above)
    if is_authorized_approver(client, user):
        _classify_and_update(client, channel, thread_ts)
        return


def _run_d2c_eval_and_reply(client, channel: str, thread_ts: str, promo, submitter: str = ""):
    """Handle a D2C storewide promo evaluation end-to-end.

    Reads the request's region (=country), discount %, and dates; runs the
    D2C evaluator (live the data warehouse history + hardcoded D2C economics); posts
    the formatted result back as a thread reply.
    """
    from datetime import datetime
    from app.evaluator_d2c import evaluate_d2c_leg, format_d2c_eval_for_slack, D2CMultiLegResult

    # Extract dates and country.
    country = (promo.region or "").strip()
    discount_pct = promo.discount_pct or 0.0
    dates_label = ""

    start_dt = end_dt = None
    if promo.dates and len(promo.dates) >= 2:
        try:
            start_dt = datetime.strptime(promo.dates[0], "%Y-%m-%d").date()
            end_dt = datetime.strptime(promo.dates[1], "%Y-%m-%d").date()
            dates_label = f"{promo.dates[0]} to {promo.dates[1]}"
        except (ValueError, TypeError):
            pass

    # Validate inputs — bail early with a friendly message if anything's missing.
    missing = []
    if not country:
        missing.append("Region (country code, e.g. AU, IN)")
    if not discount_pct:
        missing.append("Discount %")
    if not start_dt or not end_dt:
        missing.append("Start Date / End Date (YYYY-MM-DD)")
    if missing:
        client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=(
                ":warning: D2C eval needs: " + ", ".join(missing) + ". "
                "Please re-submit with all fields populated."
            ),
        )
        return

    # Run the eval (single-leg for now; multi-region requests = separate
    # submissions for v1).
    leg = evaluate_d2c_leg(country, discount_pct, start_dt, end_dt, submitter=submitter)

    # Wrap as a multi-leg result with a single leg so we can reuse the
    # existing formatter.
    wrapper = D2CMultiLegResult(legs=[leg])
    wrapper.total_incremental_units = leg.incremental_units
    wrapper.total_incremental_cm3 = leg.incremental_cm3
    wrapper.total_incremental_net_rev = leg.incremental_net_rev
    from app.evaluator_d2c import D2C_SELL_PRICE_USD, D2C_TD_UH_PCT
    wrapper.total_discount_cost = leg.total_units * D2C_SELL_PRICE_USD * (leg.discount_pct / 100.0) * D2C_TD_UH_PCT
    if leg.incremental_net_rev > 0:
        wrapper.aggregate_incremental_cm3_pct = round(
            leg.incremental_cm3 / leg.incremental_net_rev * 100.0, 1
        )
    if wrapper.total_discount_cost > 0:
        wrapper.aggregate_incremental_roas = round(
            leg.incremental_cm3 / wrapper.total_discount_cost, 2
        )
    wrapper.aggregate_grade = leg.grade

    # Post the response.
    msg = format_d2c_eval_for_slack(wrapper, dates_label=dates_label)
    client.chat_postMessage(
        channel=channel,
        thread_ts=thread_ts,
        text=msg,
    )

    # Tracker logging — D2C rows go to the same Promo Tracker tab for now;
    # `log_pending_promo` accepts whatever shape we pass and stashes it in
    # memory. mark_approved later writes the row when Approver greenlights.
    try:
        log_pending_promo(thread_ts, channel, promo, _wrap_d2c_as_evaluation(leg), submitter=submitter)
    except Exception as track_err:
        logger.error(f"D2C tracker logging failed (non-blocking): {track_err}")


def _wrap_d2c_as_evaluation(leg):
    """Adapter: wrap a D2CEvalResult into the PromoEvaluation shape that
    `log_pending_promo` expects. Only the fields tracker.py reads need to
    be populated (grade, pl_impact with incremental_units + cm3_cash_pct).
    """
    from app.models import PromoEvaluation
    eval_obj = PromoEvaluation(grade=leg.grade)
    eval_obj.pl_impact = leg.pl_impact
    # Override the bot's CM3% with the Δ-CM3% the D2C eval grades on, so
    # downstream tracker / readers see the same number.
    if leg.incremental_cm3_pct is not None:
        eval_obj.pl_impact.cm3_cash_pct = leg.incremental_cm3_pct
    eval_obj.pl_impact.incremental_units = leg.incremental_units
    return eval_obj


def _analyze_and_reply(client, channel: str, thread_ts: str, text: str, submitter: str = ""):
    try:
        # Try structured workflow format first, fall back to regex parser
        promo = parse_workflow_message(text)
        if promo:
            logger.info(f"Parsed workflow message: retailer={promo.retailer}, region={promo.region}")
        else:
            promo = parse_promo_message(text)
        logger.info(f"Parsed: retailer={promo.retailer}, region={promo.region}, "
                     f"discount={promo.discount_pct}%, spend={promo.discount_amount}")

        # Route D2C storewide proposals to the D2C evaluator. Different math
        # (BAU × lift, [REDACTED] sell, [REDACTED] brand-funded, no channel margin), different
        # data path (live the data warehouse `purchase_history`), different output format.
        if is_d2c_request(promo):
            logger.info(f"Routing to D2C evaluator: region={promo.region}, discount={promo.discount_pct}")
            _run_d2c_eval_and_reply(
                client, channel, thread_ts, promo, submitter=submitter,
            )
            return

        target_month = _guess_target_month(text)
        promo_weeks = _guess_promo_weeks(text, promo.duration, promo.dates)

        data = _fetch_all_data(promo, target_month=target_month)

        from app.models import PromoHistory, VelocityCheck, MarketEconomics, AdMetrics, AdCampaignData, MonthlyAdPerformance

        history = data.get("history") or PromoHistory()
        econ = data.get("econ") or MarketEconomics()
        velocity = data.get("velocity") or VelocityCheck()
        ad_metrics = data.get("ad_metrics") or AdMetrics()
        ad_campaign = data.get("ad_campaign") or AdCampaignData()
        monthly_ad = data.get("monthly_ad") or MonthlyAdPerformance()
        seasonal_lift = data.get("seasonal_lift")

        # Velocity chain: sales tab → promo records
        if velocity.direction == "unknown" and promo.region:
            sales_vel = get_sales_velocity(promo.region)
            if sales_vel:
                velocity = VelocityCheck(
                    weekly_run_rate=sales_vel.get("weekly_run_rate", 0),
                    direction=sales_vel.get("direction", "unknown"),
                    pct_change=sales_vel.get("pct_change", 0),
                )

        # Last resort: derive velocity from promo records themselves
        if velocity.direction == "unknown" and history.records:
            velocity = _velocity_from_promo_records(history.records)

        evaluation = evaluate_promo(
            request=promo,
            history=history,
            econ=econ,
            velocity=velocity,
            promo_weeks=promo_weeks,
            target_month=target_month,
            ad_metrics=ad_metrics,
            ad_campaign=ad_campaign,
            monthly_ad=monthly_ad,
            seasonal_lift=seasonal_lift,
        )
        _attach_commentary(promo, evaluation)

        blocks = build_evaluation_blocks(promo, evaluation)
        client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            blocks=blocks,
            text=f"Promo Analysis — Grade {evaluation.grade}",
        )

        # Log to Promo Tracker (non-blocking)
        try:
            log_pending_promo(thread_ts, channel, promo, evaluation, submitter=submitter)
        except Exception as track_err:
            logger.error(f"Tracker logging failed (non-blocking): {track_err}")

    except Exception as e:
        logger.exception(f"Analysis failed: {e}")
        try:
            client.chat_postMessage(
                channel=channel, thread_ts=thread_ts,
                blocks=build_error_reply(),
                text="Promo analysis failed — please review manually.",
            )
        except Exception:
            logger.exception("Failed to send error reply")


# ---------------------------------------------------------------------------
# /promo slash command — opens structured form (no regex parsing needed)
# ---------------------------------------------------------------------------

@bolt_app.command("/promo")
def handle_promo_command(ack, body, client):
    """Open the promo evaluation form modal."""
    ack()
    try:
        client.views_open(
            trigger_id=body["trigger_id"],
            view=build_promo_modal(),
        )
    except Exception as e:
        logger.exception(f"Failed to open promo modal: {e}")


@bolt_app.view("promo_form_submit")
def handle_promo_form_submission(ack, body, client, view):
    """Handle the promo form submission — evaluate and post to channel."""
    ack()
    user_id = body["user"]["id"]
    channel = PROMO_CHANNEL_ID or ""

    def _process_form():
        try:
            promo = parse_form_submission(view)
            logger.info(f"Form submission: retailer={promo.retailer}, region={promo.region}, "
                        f"type={promo.promo_type}, discount={promo.discount_pct}%")

            # Post the parsed request summary to the channel first
            summary = f"*Promo request from <@{user_id}>* (via /promo form):\n{promo.raw_text}"
            post = client.chat_postMessage(channel=channel, text=summary)
            thread_ts = post["ts"]

            target_month = _guess_target_month(promo.raw_text)
            promo_weeks = _guess_promo_weeks(promo.raw_text, promo.duration, promo.dates)

            data = _fetch_all_data(promo, target_month=target_month)

            from app.models import PromoHistory, VelocityCheck, MarketEconomics, AdMetrics, AdCampaignData, MonthlyAdPerformance

            history = data.get("history") or PromoHistory()
            econ = data.get("econ") or MarketEconomics()
            velocity = data.get("velocity") or VelocityCheck()
            ad_metrics = data.get("ad_metrics") or AdMetrics()
            ad_campaign = data.get("ad_campaign") or AdCampaignData()
            monthly_ad = data.get("monthly_ad") or MonthlyAdPerformance()
            seasonal_lift = data.get("seasonal_lift")
            if data.get("bau_breakdown"):
                history.retailer_breakdown = data["bau_breakdown"]

            # Velocity chain: sales tab → promo records
            if velocity.direction == "unknown" and promo.region:
                sales_vel = get_sales_velocity(promo.region)
                if sales_vel:
                    velocity = VelocityCheck(
                        weekly_run_rate=sales_vel.get("weekly_run_rate", 0),
                        direction=sales_vel.get("direction", "unknown"),
                        pct_change=sales_vel.get("pct_change", 0),
                    )

            if velocity.direction == "unknown" and history.records:
                velocity = _velocity_from_promo_records(history.records)

            evaluation = evaluate_promo(
                request=promo,
                history=history,
                econ=econ,
                velocity=velocity,
                promo_weeks=promo_weeks,
                target_month=target_month,
                ad_metrics=ad_metrics,
                ad_campaign=ad_campaign,
                monthly_ad=monthly_ad,
                seasonal_lift=seasonal_lift,
            )
            _attach_commentary(promo, evaluation)

            blocks = build_evaluation_blocks(promo, evaluation, history, econ, velocity)
            client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                blocks=blocks,
                text=f"Promo evaluation: {evaluation.grade}",
            )

            try:
                log_pending_promo(thread_ts, channel, promo, evaluation, submitter=user_id)
            except Exception as track_err:
                logger.error(f"Tracker logging failed (non-blocking): {track_err}")

        except Exception as e:
            logger.exception(f"Form evaluation failed: {e}")
            if channel:
                try:
                    client.chat_postMessage(
                        channel=channel,
                        blocks=build_error_reply(),
                        text="Promo analysis failed — please review manually.",
                    )
                except Exception:
                    logger.exception("Failed to send error reply")

    threading.Thread(target=_process_form, daemon=True).start()


@bolt_app.event("message")
def handle_message(event, client, ack):
    ack()
    channel = event.get("channel", "")
    text = event.get("text", "")
    ts = event.get("ts", "")
    thread_ts = event.get("thread_ts")  # Present only for thread replies
    subtype = event.get("subtype")
    user = event.get("user", "")

    # Allow bot_message (from Workflow Builder) but skip other subtypes
    if subtype and subtype != "bot_message":
        return

    from app.config import APPROVER_APPROVALS_CHANNEL_ID
    monitored_channels = {PROMO_CHANNEL_ID, APPROVER_APPROVALS_CHANNEL_ID} - {""}

    # Thread replies in monitored channels → approval/actuals handler
    if thread_ts and channel in monitored_channels:
        threading.Thread(
            target=_handle_thread_reply,
            args=(client, channel, thread_ts, text, user, event),
            daemon=True,
        ).start()
        return

    # Workflow Builder posts structured bot_messages to a monitored channel.
    # Parse and evaluate these — the workflow has no HTTP step to call the webhook directly.
    # Check both monitored channels: the workflow may post to either.
    if subtype == "bot_message" and channel in monitored_channels and not thread_ts:
        # Workflow Builder may put content in blocks instead of text — extract if needed
        effective_text = text
        if not text or not parse_workflow_message(text):
            blocks = event.get("blocks", [])
            parts = []
            for block in blocks:
                for el in (block.get("fields") or ([block.get("text")] if block.get("text") else [])):
                    if el:
                        parts.append((el.get("text") or "").strip("*_ \n"))
            if parts:
                effective_text = "\n".join(parts)

        logger.info(f"Workflow bot_message in {channel}: text_len={len(text)} effective_len={len(effective_text)}")
        promo = parse_workflow_message(effective_text)
        if promo:
            logger.info(f"Parsed: retailer={promo.retailer} region={promo.region}")
            threading.Thread(
                target=_analyze_and_reply,
                args=(client, channel, ts, effective_text),
                daemon=True,
            ).start()
        else:
            logger.warning(f"Did not parse as workflow message. effective_text={effective_text[:300]!r}")
        return

    # Free-text message parsing disabled — bot only responds to workflow bot_messages
