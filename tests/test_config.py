"""Consistency checks on the configuration: a bad parameter should fail CI, not a live run."""

from agent.config import AgentConfig, AssetType

CFG = AgentConfig()


def test_universe_has_15_unique_symbols() -> None:
    assert len(CFG.symbols) == 15
    assert len(set(CFG.symbols)) == len(CFG.symbols)


def test_universe_composition_matches_d005() -> None:
    etfs = [a for a in CFG.universe if a.type is AssetType.ETF]
    stocks = [a for a in CFG.universe if a.type is AssetType.STOCK]
    assert len(etfs) == 10
    assert len(stocks) == 5


def test_stock_cap_is_stricter_than_etf_cap() -> None:
    assert CFG.risk.max_weight_stock < CFG.risk.max_weight_etf


def test_cash_and_leverage_limits_are_consistent() -> None:
    # Investing at most 90% must leave exactly the 10% cash floor: no leverage possible.
    assert abs(CFG.risk.max_invested + CFG.risk.min_cash - 1.0) < 1e-12


def test_caps_allow_full_investment() -> None:
    # The per-asset and per-group caps must not make the 90% investment target unreachable.
    by_group: dict[str, float] = {}
    for a in CFG.universe:
        by_group[a.group] = by_group.get(a.group, 0.0) + CFG.max_weight(a.symbol)
    reachable = sum(min(cap, CFG.risk.max_weight_group) for cap in by_group.values())
    assert reachable >= CFG.risk.max_invested


def test_drawdown_levels_are_increasingly_severe() -> None:
    thresholds = [lvl.threshold for lvl in CFG.drawdown_levels]
    assert all(t < 0 for t in thresholds)
    assert thresholds == sorted(thresholds, reverse=True)


def test_llm_model_is_pinned() -> None:
    assert "latest" not in CFG.llm.model


def test_dry_run_is_the_default() -> None:
    assert CFG.execution.dry_run is True
