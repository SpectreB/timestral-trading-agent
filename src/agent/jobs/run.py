"""One full cycle: data -> forecast -> baseline -> decide -> guardrails -> execute -> audit. Dry run
unless --live, TRADING_ENABLED and an open market; the record is written before any order is
sent."""

from __future__ import annotations

import argparse
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import pandas as pd

from agent.checks.safety import final_check
from agent.config import AgentConfig, news_agent_config, tool_agent_config
from agent.decision.baseline import baseline_weights
from agent.decision.decide import DecisionInput, LLMClient, Mode
from agent.decision.decide_tools import decide_agent
from agent.execution.audit import FileAuditStore, build_record, run_id
from agent.execution.execute import (
    Broker,
    ExecutionError,
    Plan,
    order_records,
    plan_orders,
    preflight,
    submit_orders,
    trading_enabled,
)
from agent.inputs.data import NEW_YORK, MarketData, load_context
from agent.inputs.forecast import QuantileModel, forecast_assets
from agent.inputs.news import NewsCovariates, news_record

Status = Literal["done", "already_done", "market_closed"]

# D-018: one Alpaca paper account per agent; "news" (D-019) would take over the tool agent's.
AGENTS = ("pipeline", "tools", "news")
ALPACA_KEY_NAMES = {
    "pipeline": ("ALPACA_API_KEY", "ALPACA_SECRET_KEY"),
    "tools": ("ALPACA_TOOLS_API_KEY", "ALPACA_TOOLS_SECRET_KEY"),
    "news": ("ALPACA_TOOLS_API_KEY", "ALPACA_TOOLS_SECRET_KEY"),
}


def config_for(agent: str) -> AgentConfig:
    if agent not in AGENTS:
        raise ValueError(f"unknown agent {agent!r}")
    if agent == "news":
        return news_agent_config()
    return tool_agent_config() if agent == "tools" else AgentConfig()


def news_provider(
    cfg: AgentConfig, audit_dir: Path, env: Mapping[str, str]
) -> Callable[[pd.DataFrame], NewsCovariates] | None:  # pragma: no cover - network wiring
    """The production news pipeline of D-019: Alpaca News, the Mistral analyst, the score cache kept
    with the audit log, and the FRED calendar for the extraction check."""
    if not cfg.news.enabled:
        return None
    from dataclasses import replace

    from agent.decision.decide import MistralLLM
    from agent.inputs.news import AlpacaNews, ScoreCache, fred_release_dates, news_covariates

    key, secret = alpaca_keys(cfg.agent if cfg.agent in ALPACA_KEY_NAMES else "news", env)
    analyst_cfg = replace(
        cfg,
        llm=replace(cfg.llm, model=cfg.news.analyst_model, temperature=0.0, reasoning_effort=""),
    )
    analyst = MistralLLM(env["MISTRAL_API_KEY"], analyst_cfg)
    fred = env.get("FRED_API_KEY")

    def provide(closes: pd.DataFrame) -> NewsCovariates:
        return news_covariates(
            closes=closes,
            cfg=cfg,
            source=AlpacaNews(key, secret),
            analyst=analyst,
            cache=ScoreCache(audit_dir),
            fred_macro=fred_release_dates(fred, cfg.news.fred_release_ids) if fred else None,
        )

    return provide


def alpaca_keys(agent: str, env: Mapping[str, str]) -> tuple[str, str]:
    """The keys of the agent's own paper account: the two agents never share an account."""
    key_name, secret_name = ALPACA_KEY_NAMES[agent]
    key, secret = env.get(key_name, ""), env.get(secret_name, "")
    if not key or not secret:
        raise KeyError(f"{key_name} and {secret_name} must be set for the {agent} agent")
    return key, secret


@dataclass(frozen=True)
class CycleResult:
    run_id: str
    status: Status
    outcome: str = ""
    execution_status: str = ""
    n_orders: int = 0


def run_cycle(
    *,
    mode: Mode,
    now: datetime,
    cfg: AgentConfig,
    market: MarketData,
    broker: Broker,
    model: QuantileModel,
    llm: LLMClient,
    store: FileAuditStore,
    live: bool,
    env: Mapping[str, str],
    label: str | None = None,
    requested_by: str | None = None,
    history: FileAuditStore | None = None,
    news: Callable[[pd.DataFrame], NewsCovariates] | None = None,
) -> CycleResult:
    """One cycle."""
    now_ny = now.astimezone(NEW_YORK)
    rid = run_id(now_ny, label or mode)
    if store.has_run(rid):  # idempotent: a retried job must not trade twice
        return CycleResult(rid, "already_done")
    if live and not broker.is_market_open():
        return CycleResult(rid, "market_closed")

    account = preflight(broker)
    equity = account.equity

    closes = load_context(market, cfg, now)
    forecasts = forecast_assets(closes, model, cfg)
    baseline = baseline_weights(forecasts, cfg)

    positions = broker.positions()
    unexpected = sorted(set(positions) - set(cfg.symbols))
    if unexpected:
        raise ExecutionError(f"positions outside the universe: {unexpected}")
    current = {
        s: (positions[s].market_value / equity if s in positions else 0.0) for s in cfg.symbols
    }
    prices = broker.latest_prices(cfg.symbols)

    # Drawdown from the peak equity ever recorded (stateless: the audit log is the memory).
    peak = max([equity, *(r["account"]["equity"] for r in (history or store).read_runs())])
    drawdown = min(0.0, equity / peak - 1.0)

    covariates, news_error = None, None
    if cfg.news.enabled and news is not None:
        try:  # news is a bonus: its failure never stops a run (the tools say it is unavailable)
            covariates = news(closes)
        except Exception as error:
            news_error = f"{type(error).__name__}: {str(error)[:300]}"

    inp = DecisionInput(
        now=now_ny,
        mode=mode,
        forecasts=forecasts,
        baseline=baseline,
        current=current,
        closes=closes,
        drawdown=drawdown,
        equity=equity,
        latest_prices=prices,
        news_covariates=covariates,
    )
    decision = decide_agent(inp, llm, model, cfg)

    plan: Plan | None = None
    if decision.outcome != "fallback_hold":
        final_check(decision.target_weights, decision.cash, cfg)  # independent last line
        plan = plan_orders(decision.target_weights, positions, equity, prices, cfg, rid)

    live_orders = live and trading_enabled(env)
    if plan is None:
        execution_status = "hold"
    elif not live:
        execution_status = "dry_run"
    elif not live_orders:
        execution_status = "dry_run_kill_switch"
    else:
        execution_status = "live"

    planned = (
        order_records(plan, "planned" if live_orders else "dry_run") if plan is not None else []
    )
    record, reasoning = build_record(
        label=label,
        requested_by=requested_by,
        mode=mode,
        now=now_ny,
        cfg=cfg,
        inp=inp,
        decision=decision,
        orders=planned,
        execution_status=execution_status,
    )
    if cfg.news.enabled:
        record = record.model_copy(update={"news": news_record(covariates, news_error)})
    store.write_run(record, reasoning)  # write-ahead: recorded before anything is sent

    if live_orders and plan is not None and plan.orders:
        results = submit_orders(plan, broker)
        store.write_reconciliation(rid, {"submissions": [o.model_dump() for o in results]})
    return CycleResult(rid, "done", decision.outcome, execution_status, len(planned))


def main() -> None:  # pragma: no cover - CLI wiring, exercised by the smoke test
    from dotenv import load_dotenv

    from agent.decision.decide import MistralLLM
    from agent.execution.execute import AlpacaBroker
    from agent.inputs.data import AlpacaMarketData
    from agent.inputs.forecast import load_model

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["morning", "afternoon"], default="morning")
    parser.add_argument("--live", action="store_true", help="send real (paper) orders")
    parser.add_argument("--audit-dir", default="audit", type=Path)
    parser.add_argument("--agent", choices=AGENTS, default="pipeline")
    args = parser.parse_args()

    load_dotenv()
    cfg = config_for(args.agent)
    key, secret = alpaca_keys(args.agent, os.environ)
    news = news_provider(cfg, args.audit_dir, os.environ)
    result = run_cycle(
        mode=args.mode,
        now=datetime.now(UTC),
        cfg=cfg,
        market=AlpacaMarketData(key, secret),
        broker=AlpacaBroker(key, secret),
        model=load_model(cfg.forecast.model_id),
        llm=MistralLLM(os.environ["MISTRAL_API_KEY"], cfg),
        store=FileAuditStore(args.audit_dir),
        live=args.live,
        env=os.environ,
        news=news,
    )
    print(result)


if __name__ == "__main__":  # pragma: no cover
    main()
