"""One full cycle: data -> forecast -> baseline -> decide -> guardrails -> execute -> audit.

Safe by default: without `--live` nothing is ever sent to the broker (dry run). Live orders
need `--live`, the TRADING_ENABLED kill switch set to "true", and an open market.

Order of operations is write-ahead: the run record (including the planned orders) is written
to the audit log *before* any order is sent, so the agent can never act without a record.
Submission results then go to `reconciliation/<run_id>.json`.

Usage:
    python -m agent.run --mode morning [--live] [--audit-dir audit]
"""

from __future__ import annotations

import argparse
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from agent.audit import FileAuditStore, build_record, run_id
from agent.baseline import baseline_weights
from agent.config import AgentConfig
from agent.data import NEW_YORK, MarketData, load_context
from agent.decide import DecisionInput, LLMClient, Mode, decide
from agent.execute import (
    Broker,
    ExecutionError,
    Plan,
    order_records,
    plan_orders,
    preflight,
    submit_orders,
    trading_enabled,
)
from agent.forecast import QuantileModel, forecast_assets
from agent.safety import final_check

Status = Literal["done", "already_done", "market_closed"]


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
) -> CycleResult:
    now_ny = now.astimezone(NEW_YORK)
    rid = run_id(now_ny, mode)
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
    peak = max([equity, *(r["account"]["equity"] for r in store.read_runs())])
    drawdown = min(0.0, equity / peak - 1.0)

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
    )
    decision = decide(inp, llm, cfg)

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
        mode=mode,
        now=now_ny,
        cfg=cfg,
        inp=inp,
        decision=decision,
        orders=planned,
        execution_status=execution_status,
    )
    store.write_run(record, reasoning)  # write-ahead: recorded before anything is sent

    if live_orders and plan is not None and plan.orders:
        results = submit_orders(plan, broker)
        store.write_reconciliation(rid, {"submissions": [o.model_dump() for o in results]})
    return CycleResult(rid, "done", decision.outcome, execution_status, len(planned))


def main() -> None:  # pragma: no cover - CLI wiring, exercised by the smoke test
    from dotenv import load_dotenv

    from agent.data import AlpacaMarketData
    from agent.decide import MistralLLM
    from agent.execute import AlpacaBroker
    from agent.forecast import load_model

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["morning", "afternoon"], default="morning")
    parser.add_argument("--live", action="store_true", help="send real (paper) orders")
    parser.add_argument("--audit-dir", default="audit", type=Path)
    args = parser.parse_args()

    load_dotenv()
    cfg = AgentConfig()
    key, secret = os.environ["ALPACA_API_KEY"], os.environ["ALPACA_SECRET_KEY"]
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
    )
    print(result)


if __name__ == "__main__":  # pragma: no cover
    main()
