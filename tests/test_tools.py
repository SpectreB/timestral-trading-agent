"""Timestral tools of the tool agent (D-018): results, validation, budget, record."""

from __future__ import annotations

import json
from typing import Any

import numpy as np
import pytest

from agent.config import ToolConfig, evolve, tool_agent_config
from agent.decision.tools import TimestralTools, _probability_below_zero
from agent.inputs.forecast import forecast_assets
from tests.helpers import ALL_CASH, BASELINE, LEVELS, FakeTimestral, make_closes

CFG = tool_agent_config()
CLOSES = make_closes(300)
SPREADS = [0.01 * (1 + i % 3) for i in range(15)]  # three levels of uncertainty


def toolbox(drawdown: float = 0.0, current: dict[str, float] | None = None, **cfg: Any) -> Any:
    config = evolve(CFG, **cfg) if cfg else CFG
    model = FakeTimestral(SPREADS)
    forecasts = forecast_assets(CLOSES, model, config)  # type: ignore[arg-type]
    return TimestralTools(
        forecasts=forecasts,
        closes=CLOSES,
        model=model,  # type: ignore[arg-type]
        baseline=BASELINE,
        current=current or ALL_CASH,
        drawdown=drawdown,
        cfg=config,
    )


def call(tools: TimestralTools, name: str, **args: Any) -> dict[str, Any]:
    return json.loads(tools.call(name, json.dumps(args)))  # type: ignore[no-any-return]


def weights(**w: float) -> list[dict[str, Any]]:
    return [{"symbol": s, "weight": v} for s, v in w.items()]


# --------------------------------------------------------------------------- specs


def test_specs_declare_the_three_tools_with_strict_parameters() -> None:
    specs = toolbox().specs()
    assert [s["function"]["name"] for s in specs] == [
        "get_forecasts",
        "evaluate_portfolio",
        "get_baseline",
    ]
    for s in specs:
        assert s["function"]["parameters"]["additionalProperties"] is False
    symbols = specs[0]["function"]["parameters"]["properties"]["symbols"]["items"]["enum"]
    assert symbols == list(CFG.symbols)


# --------------------------------------------------------------------------- get_forecasts


def test_get_forecasts_returns_quantiles_width_and_volatility() -> None:
    out = call(toolbox(), "get_forecasts", symbols=["SPY", "TLT"], horizon=5)
    assert out["horizon"] == 5 and out["last_close_date"] == "2026-11-20"
    assert list(out["assets"]) == ["SPY", "TLT"]
    spy = out["assets"]["SPY"]
    assert spy["q05"] < spy["q10"] < spy["median"] < spy["q90"] < spy["q95"]
    # The fake gives width_80 = 0.8 * spread * sqrt(h); SPY has spread 0.01.
    assert spy["width_80"] == pytest.approx(0.8 * 0.01 * np.sqrt(5), abs=1e-5)
    assert spy["volatility"] == pytest.approx(spy["width_80"] / 2.563, rel=1e-3)


def test_an_empty_symbol_list_means_every_asset() -> None:
    out = call(toolbox(), "get_forecasts", symbols=[], horizon=1)
    assert list(out["assets"]) == list(CFG.symbols)


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ({"symbols": ["SPY"], "horizon": 0}, "between 1 and 5"),
        ({"symbols": ["SPY"], "horizon": 6}, "between 1 and 5"),
        ({"symbols": ["SPY"], "horizon": 2.5}, "integer"),
        ({"symbols": ["SPY"], "horizon": True}, "integer"),
        ({"symbols": ["SPY"]}, "integer"),
        ({"symbols": "SPY", "horizon": 5}, "list of strings"),
        ({"symbols": ["SPY", "BTC"], "horizon": 5}, "not in the universe: BTC"),
    ],
)
def test_bad_forecast_arguments_return_an_error(args: dict[str, Any], message: str) -> None:
    out = call(toolbox(), "get_forecasts", **args)
    assert message in out["error"]


def test_a_whole_number_horizon_given_as_a_float_is_accepted() -> None:
    assert call(toolbox(), "get_forecasts", symbols=["SPY"], horizon=5.0)["horizon"] == 5


# --------------------------------------------------------------------------- evaluate_portfolio


def test_evaluate_portfolio_forecasts_the_portfolio_and_checks_the_guardrails() -> None:
    tools = toolbox()
    out = call(tools, "evaluate_portfolio", weights=weights(SPY=0.2, TLT=0.2, GLD=0.1), horizon=5)
    assert out["invested"] == pytest.approx(0.5) and out["cash"] == pytest.approx(0.5)
    pf = out["timestral_portfolio_forecast"]
    assert pf["q05"] <= pf["q10"] <= pf["median"] <= pf["q90"] <= pf["q95"]
    assert 0 < pf["probability_of_loss"] < 1
    assert out["forecast_volatility"] > 0
    assert set(out["risk_share_top5"]) == {"SPY", "TLT", "GLD"}
    assert out["guardrails"] == {"would_be": "accepted", "rejection_reason": None, "breaches": []}
    # Against the portfolio held now (all cash): everything is traded, all risk is added.
    vs = out["versus_current_portfolio"]
    assert vs["points_traded"] == pytest.approx(50.0)
    assert vs["forecast_volatility_change"] == pytest.approx(out["forecast_volatility"])
    assert vs["bad_case_q05_change"] == pytest.approx(pf["q05"])
    # Timestral was called on one series: the portfolio's value history.
    context, horizon = tools.model.calls[-1]
    assert context.shape == (1, len(CLOSES)) and horizon == 5
    assert context[0, -1] == pytest.approx(1.0)


def test_the_portfolio_series_is_the_value_of_todays_holdings() -> None:
    tools = toolbox()
    w = dict.fromkeys(CFG.symbols, 0.0) | {"SPY": 0.5}
    series = tools.portfolio_series(w)
    spy = CLOSES["SPY"].to_numpy()
    assert series == pytest.approx(0.5 + 0.5 * spy / spy[-1])


def test_evaluate_portfolio_reports_breaches_before_the_llm_commits() -> None:
    out = call(toolbox(), "evaluate_portfolio", weights=weights(NVDA=0.15, SPY=0.3), horizon=5)
    assert out["guardrails"]["would_be"] == "corrected"
    assert any(b.startswith("above_asset_cap NVDA") for b in out["guardrails"]["breaches"])


def test_evaluate_portfolio_applies_the_drawdown_rules() -> None:
    out = call(toolbox(drawdown=-0.06), "evaluate_portfolio", weights=weights(SPY=0.1), horizon=1)
    assert any("drawdown_reduce_only" in b for b in out["guardrails"]["breaches"])


def test_an_all_cash_portfolio_has_no_risk_and_does_not_call_timestral() -> None:
    tools = toolbox()
    n = len(tools.model.calls)
    out = call(tools, "evaluate_portfolio", weights=[], horizon=5)
    assert out["invested"] == 0 and out["forecast_volatility"] == 0
    assert out["timestral_portfolio_forecast"]["median"] == 0
    assert out["timestral_portfolio_forecast"]["probability_of_loss"] == 0
    assert out["risk_share_top5"] == {}
    assert len(tools.model.calls) == n


@pytest.mark.parametrize(
    ("w", "message"),
    [
        ("SPY", "list of {symbol, weight}"),
        (["SPY"], "must be an object"),
        ([{"symbol": "BTC", "weight": 0.1}], "not in the universe: BTC"),
        ([{"symbol": "SPY", "weight": 0.1}] * 2, "SPY appears twice"),
        ([{"symbol": "SPY", "weight": "a lot"}], "finite number"),
        ([{"symbol": "SPY", "weight": True}], "finite number"),
        ([{"symbol": "SPY", "weight": -0.1}], "long-only"),
        (weights(SPY=0.6, QQQ=0.6), "no leverage"),
    ],
)
def test_bad_portfolios_return_an_error(w: Any, message: str) -> None:
    out = call(toolbox(), "evaluate_portfolio", weights=w, horizon=5)
    assert message in out["error"]


def test_non_finite_weights_are_refused() -> None:
    tools = toolbox()
    out = json.loads(tools.call("evaluate_portfolio", '{"weights": [{"symbol": "SPY", '
                                '"weight": NaN}], "horizon": 5}'))  # fmt: skip
    assert "finite number" in out["error"]


# --------------------------------------------------------------------------- baseline


def test_get_baseline_returns_the_reference_portfolio() -> None:
    out = call(toolbox(), "get_baseline")
    assert out["weights"]["SPY"] == pytest.approx(0.06)
    assert out["cash"] == pytest.approx(0.1)


# --------------------------------------------------------------------------- dispatch


def test_every_call_is_recorded_with_its_result() -> None:
    tools = toolbox()
    call(tools, "get_baseline")
    tools.call("get_forecasts", "not json")
    tools.call("drop_table", "{}")
    tools.call("get_forecasts", "[1, 2]")
    tools.call("get_baseline", "")
    assert [c["name"] for c in tools.calls] == [
        "get_baseline",
        "get_forecasts",
        "drop_table",
        "get_forecasts",
        "get_baseline",
    ]
    assert tools.calls[1]["result"] == {"error": "arguments are not valid JSON"}
    assert tools.calls[2]["result"] == {"error": "unknown tool 'drop_table'"}
    assert tools.calls[3]["result"] == {"error": "arguments must be a JSON object"}
    assert "weights" in tools.calls[4]["result"]


def test_the_call_budget_is_enforced() -> None:
    tools = toolbox(tools=ToolConfig(max_rounds=2, max_calls=2))
    call(tools, "get_baseline")
    call(tools, "get_baseline")
    assert tools.budget_left == 0
    assert "budget exhausted" in call(tools, "get_baseline")["error"]


# --------------------------------------------------------------------------- helpers


def test_probability_of_loss_is_read_from_the_quantile_function() -> None:
    levels = (0.1, 0.5, 0.9)
    assert _probability_below_zero(levels, np.array([-0.02, 0.0, 0.02])) == pytest.approx(0.5)
    assert _probability_below_zero(levels, np.array([0.01, 0.02, 0.03])) == pytest.approx(0.1)
    assert _probability_below_zero(levels, np.array([-0.03, -0.02, -0.01])) == pytest.approx(0.9)
    assert len(LEVELS) == 21


def test_keeping_the_current_portfolio_trades_nothing_and_changes_no_risk() -> None:
    held = dict.fromkeys(CFG.symbols, 0.0) | {"SPY": 0.3, "TLT": 0.3}
    tools = toolbox(current=held)
    out = call(tools, "evaluate_portfolio", weights=weights(SPY=0.3, TLT=0.3), horizon=5)
    assert out["versus_current_portfolio"] == {
        "points_traded": 0.0,
        "forecast_volatility_change": 0.0,
        "bad_case_q05_change": 0.0,
    }
    # A small reshuffle: points traded are visible next to the (small) change in risk.
    moved = call(tools, "evaluate_portfolio", weights=weights(SPY=0.25, TLT=0.35), horizon=5)
    assert moved["versus_current_portfolio"]["points_traded"] == pytest.approx(10.0)


# --------------------------------------------------------------------------- news (D-019)


def news_toolbox(with_news: bool = True) -> TimestralTools:
    from agent.config import news_agent_config
    from tests.test_news import build

    config = news_agent_config()
    model = FakeTimestral(SPREADS)
    closes = make_closes(30, end="2026-11-20")
    forecasts = forecast_assets(closes, model, config)  # type: ignore[arg-type]
    return TimestralTools(
        forecasts=forecasts,
        closes=closes,
        model=model,  # type: ignore[arg-type]
        baseline=BASELINE,
        current=ALL_CASH,
        drawdown=0.0,
        cfg=config,
        news=build(fred=("2026-11-25",)) if with_news else None,
    )


def test_the_agent_without_news_keeps_exactly_its_tools() -> None:
    specs = toolbox().specs()
    assert [s["function"]["name"] for s in specs] == [
        "get_forecasts", "evaluate_portfolio", "get_baseline"
    ]  # fmt: skip
    assert "covariates" not in specs[0]["function"]["parameters"]["properties"]
    out = call(toolbox(), "get_forecasts", symbols=["SPY"], horizon=5)
    assert "covariates" not in out and "vs_prices_only" not in out["assets"]["SPY"]
    assert "unknown tool" in call(toolbox(), "get_news_signals", symbols=[])["error"]
    refused = call(toolbox(), "get_forecasts", symbols=["SPY"], horizon=5, covariates=["news"])
    assert "not available" in refused["error"]


def test_the_news_agent_gets_covariates_and_a_news_tool() -> None:
    specs = news_toolbox().specs()
    assert [s["function"]["name"] for s in specs] == [
        "get_forecasts", "evaluate_portfolio", "get_baseline", "get_news_signals"
    ]  # fmt: skip
    cov = specs[0]["function"]["parameters"]["properties"]["covariates"]
    assert cov["items"]["enum"] == ["news", "events"]
    assert "covariates" in specs[1]["function"]["parameters"]["required"]


def test_forecasts_with_covariates_show_what_the_news_changed() -> None:
    tools = news_toolbox()
    plain = call(tools, "get_forecasts", symbols=["NVDA", "SPY"], horizon=5, covariates=[])
    assert plain["covariates"] == [] and "vs_prices_only" not in plain["assets"]["NVDA"]
    out = call(tools, "get_forecasts", symbols=["NVDA", "SPY"], horizon=5,
               covariates=["events", "news"])  # fmt: skip
    assert out["covariates"] == ["news", "events"]  # canonical order
    # The fake widens the interval on announced event days: NVDA earnings and the macro date.
    assert out["assets"]["NVDA"]["vs_prices_only"]["width_80_change"] > 0
    assert out["assets"]["SPY"]["vs_prices_only"]["width_80_change"] > 0
    assert tools.model.covariates[-1].keys() == {"past_covariates", "known_covariates"}
    n = len(tools.model.calls)
    call(tools, "get_forecasts", symbols=["LLY"], horizon=1, covariates=["news", "events"])
    assert len(tools.model.calls) == n  # each combination is computed once


@pytest.mark.parametrize(
    ("cov", "message"),
    [("news", "list of strings"), (["vix"], "unknown covariates: vix"), ([1], "list of strings")],
)
def test_bad_covariates_are_refused(cov: Any, message: str) -> None:
    out = call(news_toolbox(), "get_forecasts", symbols=["SPY"], horizon=5, covariates=cov)
    assert message in out["error"]


def test_covariates_are_refused_when_the_news_failed_in_this_run() -> None:
    tools = news_toolbox(with_news=False)
    out = call(tools, "get_forecasts", symbols=["SPY"], horizon=5, covariates=["news"])
    assert "not available" in out["error"]
    assert "not available" in call(tools, "get_news_signals", symbols=["NVDA"])["error"]


def test_evaluate_portfolio_applies_covariates_to_the_decomposition() -> None:
    tools = news_toolbox()
    w = weights(NVDA=0.1, SPY=0.2)
    plain = call(tools, "evaluate_portfolio", weights=w, horizon=5, covariates=[])
    news = call(tools, "evaluate_portfolio", weights=w, horizon=5, covariates=["events"])
    assert news["covariates"] == ["events"]
    assert news["forecast_volatility"] > plain["forecast_volatility"]
    assert news["timestral_portfolio_forecast"] == plain["timestral_portfolio_forecast"]


def test_news_signals_return_numbers_and_dates() -> None:
    out = call(news_toolbox(), "get_news_signals", symbols=["NVDA", "SPY"])
    assert out["forecast_days"][0] == "2026-11-23"
    assert out["assets"]["NVDA"]["upcoming_events"][0] == {"type": "earnings", "date": "2026-11-24"}
    assert "control group" in out["assets"]["SPY"]["company_news"]
