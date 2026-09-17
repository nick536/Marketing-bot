from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# Market economics (one per market, read from Market Economics tab)
# ---------------------------------------------------------------------------

@dataclass
class MarketEconomics:
    """Pricing and cost structure for a market."""
    market: str = ""                        # e.g. "CA", "Poland", "GCC"
    currency: str = "USD"
    fx_rate_to_usd: float = 1.0
    buy_price_local: float = 0.0            # brand revenue per unit (excl channel margin)  # REDACTED
    buy_price_usd: float = 0.0              # buy_price_local × fx_rate  # REDACTED
    retail_price_usd: float = 1.0           # consumer-facing price  # REDACTED
    channel_margin_usd: float = 0.0         # retail - buy price  # REDACTED
    trade_discount_brand_pct: float = 0.0      # the brand's share of the discount cost  # REDACTED
    return_rate: float = 0.0                # % of activations, from returns sheet  # REDACTED
    distributor: str = ""
    # Provenance — set by sheets.load_market_economics so reviewers can tell
    # whether the bot is using real sheet data or hardcoded fallback values.
    # "sheet" = read from Bot Input Variables tab.
    # "default" = MARKET_DEFAULTS hardcoded placeholder (may be stale).
    # "missing" = no value found anywhere.
    buy_price_source: str = "missing"
    td_share_source: str = "missing"
    return_rate_source: str = "missing"


# ---------------------------------------------------------------------------
# Historical promo records
# ---------------------------------------------------------------------------

@dataclass
class PromoRecord:
    """Single historical promo entry."""
    market: str = ""
    retailer: str = ""
    promo_name: str = ""
    promo_type: str = ""  # promo, spiv, newsletter, emailer, ecom_campaign, etc.
    promo_start: str = ""
    promo_end: str = ""
    duration_days: int = 0
    discount_pct: Optional[float] = None
    mdf_cost_usd: float = 0.0              # marketing spend by the brand
    trade_discount_total_usd: float = 0.0   # the brand's share of discount on units sold
    total_investment_usd: float = 0.0       # MDF + trade discount
    units_sold: int = 0                     # dupro ringmo
    baseline_units_weekly: Optional[float] = None  # pre-promo avg
    incremental_units: int = 0
    revenue_usd: float = 0.0
    roas: Optional[float] = None
    season_event: str = ""                  # Spring, BFCM, Mother's Day, etc.
    channel: str = ""                       # Email, homepage, Criteo, in-store
    month: int = 0                          # 1-12, for seasonality matching
    notes: str = ""


@dataclass
class PromoHistory:
    """All historical promo data for a retailer/region."""
    records: list[PromoRecord] = field(default_factory=list)
    baseline_units_weekly: Optional[float] = None
    avg_roas: Optional[float] = None
    best_roas: Optional[float] = None
    worst_roas: Optional[float] = None
    sample_count: int = 0
    retailer_matched: Optional[str] = None
    comparable_promo: Optional[PromoRecord] = None
    # Seasonal baseline: monthly index (1.0 = average month)
    seasonal_index: dict = field(default_factory=dict)  # {month_num: index}
    fallback_source: str = ""  # "" = direct match, else "cross-region: Poland→GCC"
    retailer_breakdown: dict = field(default_factory=dict)  # {retailer_name: weekly_units} for distributor-level


# ---------------------------------------------------------------------------
# P&L impact model
# ---------------------------------------------------------------------------

@dataclass
class PLImpact:
    """P&L waterfall for incremental promo volume."""
    incremental_units: int = 0
    # Revenue
    gross_revenue_incl_channel: float = 0.0   # retail price × units
    gross_revenue_excl_channel: float = 0.0   # buy price × units
    channel_margin: float = 0.0
    trade_discount_total: float = 0.0         # the brand's share
    returns_cost: float = 0.0
    return_rate_used: float = 0.0
    net_revenue: float = 0.0
    # Costs
    cogs_total: float = 0.0                   # [REDACTED] × units
    gross_margin: float = 0.0
    warranty_cost: float = 0.0               # 15% of net revenue
    marketing_spend: float = 0.0              # from the request
    # Brheinshopm line
    cm3_cash: float = 0.0
    cm3_cash_pct: Optional[float] = None      # CM3-Cash / Net Revenue
    grade: str = ""                           # A / B / C / REJECT
    # Investment metrics
    total_promo_investment: float = 0.0       # MDF + trade discount
    roas: Optional[float] = None              # gross rev incl channel / total investment
    # Per-unit
    trade_discount_per_unit: float = 0.0
    brand_bears_per_unit: float = 0.0         # flat $ the brand support per unit (replaces TD formula)
    returns_per_unit: float = 0.0
    net_revenue_per_unit: float = 0.0
    margin_per_unit: float = 0.0           # net rev - COGS per unit
    breakeven_units: Optional[int] = None  # units needed to cover marketing spend
    # SOA (Sell-Out Activation) per-unit cost
    soa_per_unit: float = 0.0
    soa_total: float = 0.0
    # Rebate (% of buy price, separate from SOA)
    rebate_per_unit: float = 0.0
    rebate_total: float = 0.0
    # Addon COGS (e.g. free product giveaway)
    addon_cogs_per_unit: float = 0.0
    addon_cogs_total: float = 0.0
    addon_cogs_label: str = ""
    # Volume confidence and scenario analysis
    volume_confidence: str = "high"  # high | low
    scenarios: list = field(default_factory=list)  # list of ScenarioRow
    # Sensitivity
    sensitivity: list = field(default_factory=list)  # list of SensitivityRow


@dataclass
class SensitivityRow:
    """One row in the sensitivity analysis."""
    lever: str = ""           # what changed
    value_from: str = ""      # original
    value_to: str = ""        # changed to
    cm3_cash: float = 0.0
    cm3_cash_pct: float = 0.0
    grade: str = ""
    delta: float = 0.0       # change in CM3-Cash


@dataclass
class ScenarioRow:
    """One row in the volume scenario table."""
    label: str = ""           # Bear / Base / Bull / Strong Bull
    units: int = 0
    cm3_cash: float = 0.0
    cm3_cash_pct: Optional[float] = None
    grade: str = ""
    uplift_pct: Optional[float] = None  # % weekly lift over BAU this scenario requires


# ---------------------------------------------------------------------------
# Velocity
# ---------------------------------------------------------------------------

@dataclass
class VelocityCheck:
    """Current sell-through velocity from Metabase."""
    weekly_run_rate: int = 0
    monthly_units: int = 0
    prior_monthly_units: int = 0
    pct_change: Optional[float] = None
    direction: str = "unknown"  # up | flat | down | unknown


# ---------------------------------------------------------------------------
# Risk flags and evaluation
# ---------------------------------------------------------------------------

@dataclass
class RiskFlag:
    flag: str
    detail: str


@dataclass
class PromoEvaluation:
    """Master evaluation result."""
    grade: str = "C"                  # A / B / C / REJECT
    grade_emoji: str = ""
    promo_history: PromoHistory = field(default_factory=PromoHistory)
    pl_impact: PLImpact = field(default_factory=PLImpact)
    velocity: VelocityCheck = field(default_factory=VelocityCheck)
    market_economics: Optional[MarketEconomics] = None
    risk_flags: list[RiskFlag] = field(default_factory=list)
    conditions: list[str] = field(default_factory=list)
    recommendations: list[str] = field(default_factory=list)
    verdict: str = ""  # one-line verdict with rationale
    seasonality_note: str = ""
    data_quality: str = ""  # "direct match" / "cross-region fallback" / "insufficient data"
    ad_metrics: Optional["AdMetrics"] = None
    ad_campaign: Optional["AdCampaignData"] = None  # Historical SPA/SBA tab data
    monthly_ad: Optional["MonthlyAdPerformance"] = None  # Monthly ROAS breakdown
    seasonal_lift: Optional["PromoLiftData"] = None  # Seasonal lift curves
    amazon_benchmark_lift: Optional[float] = None   # Amazon lift at this discount level
    amazon_benchmark_source: str = ""               # "direct" or "similar: US"
    commentary: str = ""                            # LLM-generated qualitative commentary
    llm_tokens_in: int = 0                         # input tokens used by generate_commentary
    llm_tokens_out: int = 0                        # output tokens used by generate_commentary
    influencer_pl: Optional["InfluencerPL"] = None   # set only for influencer channel evals


# ---------------------------------------------------------------------------
# Amazon ad performance (from the internal data API)
# ---------------------------------------------------------------------------

@dataclass
class AdMetrics:
    """Amazon advertising performance for a marketplace, last 30 days."""
    marketplace_key: str = ""
    period_days: int = 30
    total_spend: float = 0.0
    total_sales: float = 0.0
    total_orders: int = 0
    total_impressions: int = 0
    total_clicks: int = 0
    roas: Optional[float] = None
    ctr: Optional[float] = None
    roas_by_type: dict = field(default_factory=dict)    # {"SP": 5.2, "SB": 3.1, "SD": 0.8}
    spend_by_type: dict = field(default_factory=dict)
    roas_prior_30d: Optional[float] = None
    roas_trend: str = "unknown"        # "up" / "flat" / "down"
    is_heavy_ad_period: bool = False
    promo_lift: Optional["PromoLiftData"] = None
    available: bool = False
    error: Optional[str] = None


@dataclass
class AdCampaignData:
    """Historical ad campaign performance from sheet tabs (e.g. Alderon_Rheinshop_SPA)."""
    tab_name: str = ""
    total_spend: float = 0.0
    total_sales: float = 0.0
    total_orders: int = 0
    total_units: int = 0
    total_clicks: int = 0
    total_impressions: int = 0
    roas: Optional[float] = None
    ctr: Optional[float] = None
    cvr: Optional[float] = None
    available: bool = False
    error: Optional[str] = None


@dataclass
class MonthlyAdPerformance:
    """Monthly ROAS breakdown from Amazon ad data — enables month×type granularity."""
    # {month_num: {roas: 2.1, spend: 5000, sales: 10500, by_type: {SP: 2.3, SB: 1.8}}}
    monthly_roas: dict = field(default_factory=dict)
    target_month: int = 0                         # the month we're evaluating for
    target_month_roas: Optional[float] = None     # ROAS for that specific month
    target_month_roas_by_type: dict = field(default_factory=dict)  # {SP: 2.3, SB: 1.8}
    annual_avg_roas: Optional[float] = None
    available: bool = False
    error: Optional[str] = None


@dataclass
class PromoLiftData:
    """Amazon promo lift curves by discount bracket for a region.

    Compares total weekly units during heavy-promo weeks (>5% promo share)
    to baseline weeks (<5% promo share) to measure market-level price elasticity.
    Used as a reference for all retailers in the same geo.
    """
    baseline_weekly: float = 0.0          # avg weekly units with no/minimal promo
    lift_by_bracket: dict = field(default_factory=dict)  # {"5-15": 1.9, "15-25": 2.5, "25+": 3.7}
    weeks_by_bracket: dict = field(default_factory=dict) # weeks of data per bracket
    seasonal_lift_by_bracket: dict = field(default_factory=dict)  # same but for target quarter
    seasonal_baseline_weekly: float = 0.0  # baseline for target quarter
    seasonal_source: str = ""              # e.g. "Q2 (Apr-Jun)"
    fallback_source: str = ""  # "" = direct region, else "similar: US→CA"
    available: bool = False


# ---------------------------------------------------------------------------
# Influencer channel evaluation (2026-05-21)
# ---------------------------------------------------------------------------

@dataclass
class InfluencerCommercials:
    """Channel-level economics for influencer promos, read from the
    'Influencer commercials' Promo Sheet tab. Percentage fields use the
    0-100 convention (matches the sheet cells)."""
    retail_price_usd: float = 1.0              # REDACTED
    cogs_usd: float = 0.0                       # REDACTED
    return_rate_pct: float = 0.0               # 0-100  # REDACTED
    commission_pct: float = 0.0                # 0-100; default if form omits it  # REDACTED
    warranty_pct: float = 0.0                  # 0-100  # REDACTED
    marketing_pct: float = 0.0                 # 0-100  # REDACTED
    channel_margin_pct: float = 0.0            # 0-100; 0 for influencer D2C
    marketplace_commission_pct: float = 0.0    # 0-100; 0 for influencer D2C
    tax_pct: float = 0.0                       # 0-100; 0 for influencer D2C
    payment_gateway_pct: float = 0.0           # 0-100; 0 for influencer D2C
    source: str = "default"                    # "sheet" when read from the tab


@dataclass
class InfluencerPL:
    """Per-unit influencer CM3 waterfall, matching the reference P&L sheet.
    cm3_pct is a % of net revenue, volume-invariant, and drives the grade;
    cm3_cash scales with estimated_units (display only)."""
    gross: float = 0.0
    trade_discount: float = 0.0
    channel_margin: float = 0.0
    marketplace_commission: float = 0.0
    returns: float = 0.0
    tax: float = 0.0
    net_revenue: float = 0.0
    cogs: float = 0.0
    commission: float = 0.0
    marketing: float = 0.0
    warranty: float = 0.0
    payment_gateway: float = 0.0
    total_sm: float = 0.0
    total_warranty: float = 0.0
    cm3_per_unit: float = 0.0
    cm3_pct: float = 0.0                       # % of net revenue
    estimated_units: float = 0.0
    cm3_cash: float = 0.0
    grade: str = ""


# ---------------------------------------------------------------------------
# Post-promo tracking
# ---------------------------------------------------------------------------

@dataclass
class TrackedPromo:
    """A promo logged for post-evaluation tracking."""
    thread_ts: str = ""
    channel: str = ""
    retailer: str = ""
    region: str = ""
    discount_pct: float = 0.0
    start_date: str = ""
    end_date: str = ""
    predicted_units: int = 0
    predicted_cm3: float = 0.0
    grade: str = ""
    status: str = "Pending"  # Pending / Approved / Actuals Received
    actual_units: Optional[int] = None
    actual_cm3: Optional[float] = None
    accuracy_pct: Optional[float] = None
    approved_at: str = ""
    posted_at: str = ""
