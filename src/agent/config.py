"""Central configuration: every tunable parameter of the agent lives here."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class AssetType(StrEnum):
    ETF = "etf"
    STOCK = "stock"


@dataclass(frozen=True)
class Asset:
    symbol: str
    name: str
    type: AssetType
    group: str


UNIVERSE: tuple[Asset, ...] = (
    Asset("SPY", "S&P 500", AssetType.ETF, "us_broad"),
    Asset("QQQ", "Nasdaq 100", AssetType.ETF, "tech_growth"),
    Asset("IWM", "US small caps", AssetType.ETF, "us_broad"),
    Asset("EFA", "Developed markets ex-US", AssetType.ETF, "intl_developed"),
    Asset("EEM", "Emerging markets", AssetType.ETF, "emerging"),
    Asset("XLE", "Energy sector", AssetType.ETF, "energy"),
    Asset("XLV", "Health care sector", AssetType.ETF, "health"),
    Asset("XLF", "Financials sector", AssetType.ETF, "financials"),
    Asset("TLT", "Long-term US Treasuries", AssetType.ETF, "bonds"),
    Asset("GLD", "Gold", AssetType.ETF, "gold"),
    Asset("NVDA", "NVIDIA", AssetType.STOCK, "tech_growth"),
    Asset("JPM", "JPMorgan Chase", AssetType.STOCK, "financials"),
    Asset("XOM", "Exxon Mobil", AssetType.STOCK, "energy"),
    Asset("LLY", "Eli Lilly", AssetType.STOCK, "health"),
    Asset("AMZN", "Amazon", AssetType.STOCK, "tech_growth"),
)


@dataclass(frozen=True)
class ForecastConfig:
    model_id: str = "mistralai/Timestral-28M"  # switch to Timestral-123M after the backtest
    input_type: str = "price"
    context_length: int = 512
    horizon: int = 5  # D-007: decisions use the 5-day forecast; days 1-5 are all logged


@dataclass(frozen=True)
class LLMConfig:
    model: str = "mistral-large-4-0"  # pinned version, never a "-latest" alias
    temperature: float = 0.1
    reasoning_effort: str = "high"
    prompt_version: str = "decide_v1"
    max_retries: int = 1
    timeout_s: int = 600


@dataclass(frozen=True)
class RiskLimits:
    min_cash: float = 0.10
    max_invested: float = 0.90  # no leverage: Alpaca would allow 2x buying power
    max_weight_etf: float = 0.25
    max_weight_stock: float = 0.10
    max_weight_group: float = 0.30
    max_increase_vs_baseline: float = 0.15  # asymmetric: decreases are unbounded
    weight_sum_tolerance: float = 0.05
    max_corrected_assets: int = 3
    max_total_correction: float = 0.20


@dataclass(frozen=True)
class DrawdownLevel:
    threshold: float  # drawdown from peak equity, as a negative fraction
    action: str


DRAWDOWN_LEVELS: tuple[DrawdownLevel, ...] = (
    DrawdownLevel(-0.03, "alert"),
    DrawdownLevel(-0.05, "reduce_only"),
    DrawdownLevel(-0.08, "cap_exposure_60"),
    DrawdownLevel(-0.12, "baseline_half_exposure_and_pause"),
)


@dataclass(frozen=True)
class ExecutionConfig:
    no_trade_band: float = 0.02  # skip orders that move a weight by less than 2 pts
    # A move meant as 2 points that drifted by a few dollars since the decision still counts.
    # Found on 2026-11-24: two 2-point sells fell $3 short of the band and were skipped.
    band_tolerance: float = 0.001
    max_turnover_per_run: float = 0.30
    initial_deployment_max_invested: float = 0.05
    limit_price_tolerance: float = 0.003  # marketable limit: +/-0.3% beyond current price


@dataclass(frozen=True)
class ToolConfig:
    max_rounds: int = 6
    max_calls: int = 12


@dataclass(frozen=True)
class NewsConfig:
    enabled: bool = False
    analyst_model: str = "mistral-small-2603"
    batch_size: int = 20
    # First day of the free Alpaca news history for our stocks (measured on 2026-11-23):
    # before it, covariates are missing (NaN), not zero.
    coverage_start: str = "2026-06-01"  # ISO date (kept a string: the config is logged as JSON)
    fred_release_ids: tuple[int, ...] = (10, 50)


@dataclass(frozen=True)
class AgentConfig:
    universe: tuple[Asset, ...] = UNIVERSE
    forecast: ForecastConfig = field(default_factory=ForecastConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    risk: RiskLimits = field(default_factory=RiskLimits)
    drawdown_levels: tuple[DrawdownLevel, ...] = DRAWDOWN_LEVELS
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    benchmark: str = "SPY"
    agent: str = "pipeline"
    tools: ToolConfig = field(default_factory=ToolConfig)
    news: NewsConfig = field(default_factory=NewsConfig)

    @property
    def symbols(self) -> tuple[str, ...]:
        return tuple(a.symbol for a in self.universe)

    def asset(self, symbol: str) -> Asset:
        for a in self.universe:
            if a.symbol == symbol:
                return a
        raise KeyError(f"{symbol} is not in the universe")

    def max_weight(self, symbol: str) -> float:
        a = self.asset(symbol)
        return self.risk.max_weight_etf if a.type is AssetType.ETF else self.risk.max_weight_stock


def tool_agent_config(base: AgentConfig | None = None) -> AgentConfig:
    from dataclasses import replace

    base = base or AgentConfig()
    return replace(
        base,
        agent="tools",
        forecast=replace(base.forecast, model_id="mistralai/Timestral-209M"),
        llm=replace(base.llm, prompt_version="decide_tools_v2"),  # v1 failed "calm market"
        risk=replace(base.risk, max_increase_vs_baseline=1.0),  # 1.0 = no baseline bound
    )


def news_agent_config(base: AgentConfig | None = None) -> AgentConfig:
    from dataclasses import replace

    cfg = tool_agent_config(base)
    return replace(
        cfg,
        llm=replace(cfg.llm, prompt_version="decide_tools_v3"),
        news=replace(cfg.news, enabled=True),
    )
