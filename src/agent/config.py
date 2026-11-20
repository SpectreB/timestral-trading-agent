"""Central configuration: every tunable parameter of the agent lives here.

Each value traces back to a decision in docs/decisions.md. Values marked "provisional"
are parameters we expect to revisit with evidence (see the parameters-vs-architecture
distinction in the decision log).
"""

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
    # Risk group used by the sector cap (D-010). Assets in the same group are
    # aggregated, e.g. XLE + XOM both count towards "energy".
    group: str


# D-005: 10 diversified ETFs + 5 single stocks, one per sector.
# Group assignment is provisional: QQQ, NVDA and AMZN share "tech_growth" because
# they are dominated by the same large-cap growth names.
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
    """D-006 (provisional values, to be confirmed by the multi-ticker backtest)."""

    model_id: str = "mistralai/Timestral-28M"  # switch to Timestral-123M after the backtest
    input_type: str = "price"  # "price" or "log_return"
    context_length: int = 512  # trading days (~2 years)
    horizon: int = 5  # D-007: decisions use the 5-day forecast; days 1-5 are all logged


@dataclass(frozen=True)
class LLMConfig:
    """D-008."""

    model: str = "mistral-large-4-0"  # pinned version, never a "-latest" alias
    temperature: float = 0.1
    max_retries: int = 1  # one retry on invalid output, then fallback (D-010)


@dataclass(frozen=True)
class RiskLimits:
    """D-004 and D-010. Weights are fractions of portfolio equity."""

    min_cash: float = 0.10
    max_invested: float = 0.90  # no leverage: Alpaca would allow 2x buying power
    max_weight_etf: float = 0.25
    max_weight_stock: float = 0.10
    max_weight_group: float = 0.30
    max_increase_vs_baseline: float = 0.15  # asymmetric: decreases are unbounded
    # Severity thresholds: beyond these, the whole proposal is rejected.
    weight_sum_tolerance: float = 0.05  # weights + cash must be within 95-105%
    max_corrected_assets: int = 3
    max_total_correction: float = 0.20


@dataclass(frozen=True)
class DrawdownLevel:
    threshold: float  # drawdown from peak equity, as a negative fraction
    action: str


# D-010: graduated circuit breaker, measured from the portfolio's peak equity.
DRAWDOWN_LEVELS: tuple[DrawdownLevel, ...] = (
    DrawdownLevel(-0.03, "alert"),
    DrawdownLevel(-0.05, "reduce_only"),
    DrawdownLevel(-0.08, "cap_exposure_60"),
    DrawdownLevel(-0.12, "baseline_half_exposure_and_pause"),
)


@dataclass(frozen=True)
class ExecutionConfig:
    """D-007 and D-010."""

    no_trade_band: float = 0.02  # skip orders that move a weight by less than 2 pts
    max_turnover_per_run: float = 0.30
    limit_price_tolerance: float = 0.003  # marketable limit: +/-0.3% beyond current price
    dry_run: bool = True  # safe default: real orders require an explicit opt-in


@dataclass(frozen=True)
class AgentConfig:
    universe: tuple[Asset, ...] = UNIVERSE
    forecast: ForecastConfig = field(default_factory=ForecastConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    risk: RiskLimits = field(default_factory=RiskLimits)
    drawdown_levels: tuple[DrawdownLevel, ...] = DRAWDOWN_LEVELS
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)

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
