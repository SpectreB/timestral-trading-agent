"""Central configuration: every tunable parameter of the agent lives here."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


M = TypeVar("M", bound=BaseModel)


def evolve(obj: M, /, **changes: Any) -> M:
    """A validated copy: `model_copy(update=...)` would skip validation."""
    return type(obj).model_validate({**dict(obj), **changes})


class AssetType(StrEnum):
    ETF = "etf"
    STOCK = "stock"


class Asset(Frozen):
    symbol: str
    name: str
    type: AssetType
    group: str


UNIVERSE: tuple[Asset, ...] = (
    Asset(symbol="SPY", name="S&P 500", type=AssetType.ETF, group="us_broad"),
    Asset(symbol="QQQ", name="Nasdaq 100", type=AssetType.ETF, group="tech_growth"),
    Asset(symbol="IWM", name="US small caps", type=AssetType.ETF, group="us_broad"),
    Asset(symbol="EFA", name="Developed markets ex-US", type=AssetType.ETF, group="intl_developed"),
    Asset(symbol="EEM", name="Emerging markets", type=AssetType.ETF, group="emerging"),
    Asset(symbol="XLE", name="Energy sector", type=AssetType.ETF, group="energy"),
    Asset(symbol="XLV", name="Health care sector", type=AssetType.ETF, group="health"),
    Asset(symbol="XLF", name="Financials sector", type=AssetType.ETF, group="financials"),
    Asset(symbol="TLT", name="Long-term US Treasuries", type=AssetType.ETF, group="bonds"),
    Asset(symbol="GLD", name="Gold", type=AssetType.ETF, group="gold"),
    Asset(symbol="NVDA", name="NVIDIA", type=AssetType.STOCK, group="tech_growth"),
    Asset(symbol="JPM", name="JPMorgan Chase", type=AssetType.STOCK, group="financials"),
    Asset(symbol="XOM", name="Exxon Mobil", type=AssetType.STOCK, group="energy"),
    Asset(symbol="LLY", name="Eli Lilly", type=AssetType.STOCK, group="health"),
    Asset(symbol="AMZN", name="Amazon", type=AssetType.STOCK, group="tech_growth"),
)


class ForecastConfig(Frozen):
    model_id: str = "mistralai/Timestral-28M"  # switch to Timestral-123M after the backtest
    input_type: str = "price"
    context_length: int = Field(default=512, gt=0)
    horizon: int = Field(
        default=5, ge=1
    )  # D-007: decisions use the 5-day forecast; days 1-5 are all logged


class LLMConfig(Frozen):
    model: str = "mistral-large-4-0"  # pinned version, never a "-latest" alias
    temperature: float = 0.1
    reasoning_effort: str = "high"
    prompt_version: str = "decide_v1"
    max_retries: int = Field(default=1, ge=0)
    timeout_s: int = Field(default=600, gt=0)

    @model_validator(mode="after")
    def _pinned(self) -> LLMConfig:
        if self.model.endswith("latest"):
            raise ValueError(f"{self.model}: pin a model version, never a -latest alias")
        return self


class RiskLimits(Frozen):
    min_cash: float = Field(default=0.10, ge=0.0, le=1.0)
    max_invested: float = Field(
        default=0.90, ge=0.0, le=1.0
    )  # no leverage: Alpaca would allow 2x buying power
    max_weight_etf: float = Field(default=0.25, ge=0.0, le=1.0)
    max_weight_stock: float = Field(default=0.10, ge=0.0, le=1.0)
    max_weight_group: float = Field(default=0.30, ge=0.0, le=1.0)
    max_increase_vs_baseline: float = Field(
        default=0.15, ge=0.0, le=1.0
    )  # asymmetric: decreases are unbounded
    weight_sum_tolerance: float = Field(default=0.05, ge=0.0, le=1.0)
    max_corrected_assets: int = Field(default=3, ge=0)
    max_total_correction: float = Field(default=0.20, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _no_leverage(self) -> RiskLimits:
        if self.max_invested > 1.0 - self.min_cash + 1e-9:
            raise ValueError("max_invested + min_cash above 100%: that would be leverage")
        return self


class DrawdownLevel(Frozen):
    threshold: float = Field(lt=0.0)  # drawdown from peak equity, as a negative fraction
    action: str


DRAWDOWN_LEVELS: tuple[DrawdownLevel, ...] = (
    DrawdownLevel(threshold=-0.03, action="alert"),
    DrawdownLevel(threshold=-0.05, action="reduce_only"),
    DrawdownLevel(threshold=-0.08, action="cap_exposure_60"),
    DrawdownLevel(threshold=-0.12, action="baseline_half_exposure_and_pause"),
)


class ExecutionConfig(Frozen):
    no_trade_band: float = Field(
        default=0.02, ge=0.0, le=1.0
    )  # skip orders that move a weight by less than 2 pts
    # A move meant as 2 points that drifted by a few dollars since the decision still counts.
    # Found on 2026-11-24: two 2-point sells fell $3 short of the band and were skipped.
    band_tolerance: float = Field(default=0.001, ge=0.0, le=1.0)
    max_turnover_per_run: float = Field(default=0.30, ge=0.0, le=1.0)
    initial_deployment_max_invested: float = Field(default=0.05, ge=0.0, le=1.0)
    limit_price_tolerance: float = Field(
        default=0.003, ge=0.0, le=1.0
    )  # marketable limit: +/-0.3% beyond current price


class ToolConfig(Frozen):
    max_rounds: int = Field(default=6, ge=1)
    max_calls: int = Field(default=12, ge=0)


class NewsConfig(Frozen):
    enabled: bool = False
    analyst_model: str = "mistral-small-2603"
    batch_size: int = Field(default=20, ge=1)
    # First day of the free Alpaca news history for our stocks (measured on 2026-11-23):
    # before it, covariates are missing (NaN), not zero.
    coverage_start: str = "2026-06-01"  # ISO date (kept a string: the config is logged as JSON)
    fred_release_ids: tuple[int, ...] = (10, 50)


class AgentConfig(Frozen):
    universe: tuple[Asset, ...] = UNIVERSE
    forecast: ForecastConfig = ForecastConfig()
    llm: LLMConfig = LLMConfig()
    risk: RiskLimits = RiskLimits()
    drawdown_levels: tuple[DrawdownLevel, ...] = DRAWDOWN_LEVELS
    execution: ExecutionConfig = ExecutionConfig()
    benchmark: str = "SPY"
    agent: Literal["pipeline", "tools"] = "pipeline"
    tools: ToolConfig = ToolConfig()
    news: NewsConfig = NewsConfig()

    @model_validator(mode="after")
    def _consistent(self) -> AgentConfig:
        symbols = [a.symbol for a in self.universe]
        if len(set(symbols)) != len(symbols):
            raise ValueError("a symbol appears twice in the universe")
        levels = [lv.threshold for lv in self.drawdown_levels]
        if levels != sorted(levels, reverse=True):
            raise ValueError("drawdown levels must go from the mildest to the deepest")
        return self

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
    base = base or AgentConfig()
    return evolve(
        base,
        agent="tools",
        forecast=evolve(base.forecast, model_id="mistralai/Timestral-209M"),
        llm=evolve(base.llm, prompt_version="decide_tools_v2"),  # v1 failed "calm market"
        risk=evolve(base.risk, max_increase_vs_baseline=1.0),  # 1.0 = no baseline bound
    )


def news_agent_config(base: AgentConfig | None = None) -> AgentConfig:
    cfg = tool_agent_config(base)
    return evolve(
        cfg,
        llm=evolve(cfg.llm, prompt_version="decide_tools_v3"),
        news=evolve(cfg.news, enabled=True),
    )
