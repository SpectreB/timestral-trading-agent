"""Simulations requested by the evaluators (D-017). Never sends an order. The issue text is reduced
to closed-list options and never reaches a shell."""

from __future__ import annotations

import argparse
import html
import json
import math
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from agent.config import AgentConfig, Asset, AssetType, evolve
from agent.decision.decide import LLMClient
from agent.evaluation.covariate_eval import PANEL_STOCKS
from agent.execution.audit import FileAuditStore
from agent.execution.execute import Broker, Position
from agent.inputs.data import NEW_YORK, MarketData
from agent.inputs.forecast import QuantileModel
from agent.inputs.news import NewsCovariates
from agent.jobs.run import CycleResult, run_cycle

TITLE = "Simulation request"
COOLDOWN = timedelta(minutes=1)
DAILY_CAP = 20
ANALYSIS_LIMIT = 1200
REASON_LIMIT = 140
DASHBOARD = "https://spectreb.github.io/timestral-trading-agent/"
REPO = "https://github.com/SpectreB/timestral-trading-agent"
SIMULATIONS = "simulations"
AUDIT_BRANCH = "audit-log"


# What an evaluator may choose. Closed lists: anything else is ignored.
OPTIONS: dict[str, tuple[str, ...]] = {
    "agent": ("pipeline", "tools", "news"),
    "stance": ("balanced", "cautious", "bold"),
    "start": ("current", "cash", "equal"),
    "universe": ("standard", "wide"),
}
DEFAULT_OPTIONS = {key: values[0] for key, values in OPTIONS.items()}
EQUAL_INVESTED = 0.90
_LINE = re.compile(r"^\s*(agent|stance|start|universe)\s*[:=]\s*([a-z]+)\s*$", re.IGNORECASE)

_GROUP = {
    "tech": "tech_growth", "comm": "tech_growth", "fin": "financials", "energy": "energy",
    "health": "health", "consumer": "consumer", "staples": "staples", "industrial": "industrials",
}  # fmt: skip
MAX_TEXT = 4000


def parse_options(text: str) -> tuple[dict[str, str], list[str]]:
    chosen, refused, seen = dict(DEFAULT_OPTIONS), [], set()
    for line in text[:MAX_TEXT].splitlines():
        found = _LINE.match(line)
        if not found:
            continue
        key, value = found.group(1).lower(), found.group(2).lower()
        if key in seen:
            continue
        seen.add(key)
        if value in OPTIONS[key]:
            chosen[key] = value
        else:
            refused.append(key)
    return chosen, refused


def apply_universe(cfg: AgentConfig, universe: str) -> AgentConfig:
    if universe != "wide":
        return cfg
    held = set(cfg.symbols)
    extra = tuple(
        Asset(symbol=symbol, name=name, type=AssetType.STOCK, group=_GROUP[group])
        for symbol, name, group in PANEL_STOCKS
        if symbol not in held
    )
    return evolve(cfg, universe=(*cfg.universe, *extra))


def simulation_config(base: AgentConfig, options: Mapping[str, str]) -> AgentConfig:
    chosen = {**DEFAULT_OPTIONS, **options}
    return apply_stance(apply_universe(base, chosen["universe"]), chosen["stance"])


def apply_stance(cfg: AgentConfig, stance: str) -> AgentConfig:
    r = cfg.risk
    if stance == "cautious":
        risk = evolve(
            r,
            min_cash=0.30,
            max_invested=0.70,
            max_weight_etf=0.15,
            max_weight_stock=0.05,
            max_weight_group=0.20,
            max_increase_vs_baseline=min(r.max_increase_vs_baseline, 0.05),
        )
    elif stance == "bold":
        risk = evolve(
            r,
            max_weight_etf=0.30,
            max_weight_stock=0.15,
            max_weight_group=0.40,
            max_increase_vs_baseline=max(r.max_increase_vs_baseline, 0.30),
        )
    else:
        return cfg
    return evolve(cfg, risk=risk)


@dataclass
class StartingPoint:
    inner: Broker
    start: str
    symbols: Sequence[str]

    def account(self) -> Any:
        return self.inner.account()

    def latest_prices(self, symbols: Sequence[str]) -> dict[str, float]:
        return self.inner.latest_prices(symbols)

    def is_market_open(self) -> bool:
        return self.inner.is_market_open()

    def positions(self) -> dict[str, Position]:
        if self.start == "current":
            return self.inner.positions()
        if self.start == "cash":
            return {}
        equity = self.inner.account().equity
        prices = self.inner.latest_prices(self.symbols)
        value = equity * EQUAL_INVESTED / len(self.symbols)
        return {s: Position(quantity=value / prices[s], market_value=value) for s in self.symbols}

    def submit_limit_order(
        self, symbol: str, side: Any, quantity: float, limit_price: float, client_order_id: str
    ) -> str:
        raise RuntimeError("a simulation never sends an order")


_START_TEXT = {
    "current": "the agent's current portfolio",
    "cash": "an all-cash portfolio",
    "equal": "an equal-weight portfolio (90% invested, spread evenly)",
}


def label_for(now: datetime, options: Mapping[str, str] | None = None) -> str:
    chosen = {**DEFAULT_OPTIONS, **(options or {})}
    extra = "".join(f"-{chosen[k]}" for k in OPTIONS if chosen[k] != DEFAULT_OPTIONS[k])
    return f"sim-{now.astimezone(NEW_YORK):%H%M%S}{extra}"


def throttle(store: FileAuditStore, now: datetime) -> str | None:
    times = [datetime.fromisoformat(r["started_at"]) for r in store.read_runs()]
    today = now.astimezone(NEW_YORK).date()
    if sum(t.astimezone(NEW_YORK).date() == today for t in times) >= DAILY_CAP:
        return f"The limit of {DAILY_CAP} simulations a day is reached. Please try again tomorrow."
    if times and now - max(times) < COOLDOWN:
        seconds = max(1, math.ceil((COOLDOWN - (now - max(times))).total_seconds()))
        return (
            "Another simulation was started less than a minute ago. Please try again in about "
            f"{seconds} second(s)."
        )
    return None


def neutralise(text: str, limit: int) -> str:
    clean = html.escape(" ".join(text.split()), quote=False).replace("@", "@​")
    for char in "`*_#|[]":
        clean = clean.replace(char, "\\" + char)
    return clean if len(clean) <= limit else clean[: limit - 1].rstrip() + "…"


def _analysis(record: Mapping[str, Any]) -> str:
    try:
        text = json.loads(record["decision"]["attempts"][-1]["answer"])["analysis"]
    except (ValueError, KeyError, TypeError):
        return ""
    return text if isinstance(text, str) else ""


def comment(
    record: Mapping[str, Any],
    requester: str,
    options: Mapping[str, str] | None = None,
    refused: Sequence[str] = (),
) -> str:
    chosen = {**DEFAULT_OPTIONS, **(options or {})}
    chosen_line = (
        f"Options: agent **{chosen['agent']}**, stance **{chosen['stance']}**, start from "
        f"**{chosen['start']}**, universe **{chosen['universe']}**."
    )
    if refused:
        chosen_line += f" Unknown value for {', '.join(refused)}: the default was used."
    p = record["portfolios"]
    cur, fin = p["current"], p["final"]
    proposed = p["proposed"] or {}
    moves = sorted(
        ((s, fin[s] - cur[s]) for s in fin if abs(fin[s] - cur[s]) >= 0.005),
        key=lambda m: -abs(m[1]),
    )[:6]
    rows = "\n".join(
        f"| {s} | {100 * cur[s]:.1f}% → {100 * fin[s]:.1f}% | {100 * d:+.1f} pts | "
        f"{neutralise(proposed[s]['rationale'], REASON_LIMIT) if s in proposed else ''} |"
        for s, d in moves
    )
    table = (
        f"| Asset | Weight | Change | Reason given by the LLM |\n|---|---|---|---|\n{rows}"
        if moves
        else "No change: the agent would keep its positions."
    )
    corrections = [
        c for c in record["decision"]["attempts"][-1]["guardrails"]["corrections"] if c["counted"]
    ]
    fixes = (
        "\n".join(
            f"- `{c['rule']}` {c['symbol'] or ''}: {c['before']:.3f} → {c['after']:.3f}"
            for c in corrections
        )
        if corrections
        else "None: the proposal respected every rule."
    )
    analysis = _analysis(record)
    reading = f"> {neutralise(analysis, ANALYSIS_LIMIT)}" if analysis else "_No readable analysis._"
    rid = record["run_id"]
    model = record["decision"]["attempts"][-1]["model"]
    prompt = record["decision"]["prompt_version"]
    link = f"{REPO}/blob/{AUDIT_BRANCH}/{SIMULATIONS}/runs/{rid}.json"
    return f"""### Simulation finished: {record["decision"]["outcome"].replace("_", " ")}

Requested by @{requester}. {chosen_line}
A full cycle ran on {_START_TEXT[chosen["start"]]}: live market data, Timestral forecasts, the
LLM's decision and the guardrails. **No order was sent**: simulations never trade.

**What the agent would do**

{table}

**Guardrail corrections**

{fixes}

**The LLM's reading**

{reading}

Model {model}, prompt {prompt}, code `{record["git_sha"][:7]}`.
[Full record]({link}) · [Dashboard]({DASHBOARD}) (updated in a minute or two)
"""


def refusal(reason: str) -> str:
    return f"### Simulation not started\n\n{reason}\n\n[Dashboard]({DASHBOARD})\n"


def simulate(
    *,
    now: datetime,
    cfg: AgentConfig,
    market: MarketData,
    broker: Broker,
    model: QuantileModel,
    llm: LLMClient,
    root: Path,
    requester: str,
    env: Mapping[str, str],
    options: Mapping[str, str] | None = None,
    refused: Sequence[str] = (),
    news: Callable[[Any], NewsCovariates] | None = None,
    history_dir: Path | None = None,
) -> tuple[str, CycleResult | None]:
    chosen = {**DEFAULT_OPTIONS, **(options or {})}
    cfg = simulation_config(cfg, chosen)
    store = FileAuditStore(root / SIMULATIONS)
    reason = throttle(store, now)
    if reason:
        return refusal(reason), None
    result = run_cycle(
        mode="morning",
        now=now,
        cfg=cfg,
        market=market,
        broker=StartingPoint(broker, chosen["start"], cfg.symbols),
        model=model,
        llm=llm,
        store=store,
        live=False,
        env={**env, "TRADING_ENABLED": "false"},
        label=label_for(now, chosen),
        requested_by=requester,
        history=FileAuditStore(history_dir or root),
        news=news,
    )
    return comment(store.read_run(result.run_id), requester, chosen, refused), result


def main() -> None:  # pragma: no cover - CLI wiring, runs in the Simulate workflow
    import os
    from datetime import UTC

    from dotenv import load_dotenv

    from agent.decision.decide import MistralLLM
    from agent.execution.execute import AlpacaBroker
    from agent.inputs.data import AlpacaMarketData
    from agent.inputs.forecast import load_model

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit-dir", default="audit", type=Path)
    parser.add_argument("--requested-by", required=True)
    parser.add_argument("--comment-out", default="comment.md", type=Path)
    parser.add_argument("--options-env", default="OPTIONS", help="env variable with the issue text")
    args = parser.parse_args()

    load_dotenv()
    from agent.jobs.run import alpaca_keys, config_for, news_provider

    options, refused = parse_options(os.environ.get(args.options_env, ""))
    agent = options["agent"]
    cfg = simulation_config(config_for(agent), options)
    key, secret = alpaca_keys(agent, os.environ)
    reply, _ = simulate(
        now=datetime.now(UTC),
        cfg=cfg,
        market=AlpacaMarketData(key, secret),
        broker=AlpacaBroker(key, secret),
        model=load_model(cfg.forecast.model_id),
        llm=MistralLLM(os.environ["MISTRAL_API_KEY"], cfg),
        root=args.audit_dir,
        requester=args.requested_by,
        env=os.environ,
        options=options,
        refused=refused,
        news=news_provider(cfg, args.audit_dir, os.environ),
        history_dir=args.audit_dir if agent == "pipeline" else args.audit_dir / "agents" / "tools",
    )
    args.comment_out.write_text(reply, encoding="utf-8")
    print(reply)


if __name__ == "__main__":  # pragma: no cover
    main()
