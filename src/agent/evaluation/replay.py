"""Historical replay of the agent (D-015, test case 1). Point-in-time data and anonymised assets,
against the LLM's lookahead bias."""

from __future__ import annotations

import argparse
import json
import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from agent.checks.safety import final_check
from agent.config import AgentConfig, Asset
from agent.decision.baseline import baseline_weights
from agent.decision.decide import DecisionInput, LLMClient, build_payload
from agent.decision.decide_tools import decide_agent
from agent.evaluation.metrics import performance_report
from agent.execution.audit import FileAuditStore, build_record
from agent.execution.execute import ExecutionError, Position, order_records, plan_orders
from agent.inputs.data import NEW_YORK
from agent.inputs.forecast import QuantileModel, forecast_assets

START_EQUITY = 100_000.0
COST_BPS = 5.0  # assumed cost per side on top of the open; the live slippage will be measured
SYNTHETIC_START = "2100-01-04"
OBSERVATION_WINDOW = 12


class ReplayError(RuntimeError):
    """The replay cannot continue without breaking its own rules."""


def anonymise(cfg: AgentConfig, seed: int = 0) -> tuple[AgentConfig, dict[str, str]]:
    """The configuration seen by the LLM: aliases in a shuffled order, generic names."""
    rng = random.Random(seed)
    aliases = [f"A{i + 1:02d}" for i in range(len(cfg.universe))]
    rng.shuffle(aliases)
    mapping = {a.symbol: alias for a, alias in zip(cfg.universe, aliases, strict=True)}
    universe = tuple(
        Asset(mapping[a.symbol], f"Asset {mapping[a.symbol]}", a.type, a.group)
        for a in cfg.universe
    )
    return replace(cfg, universe=universe, benchmark=mapping[cfg.benchmark]), mapping


def point_in_time(history: pd.DataFrame, session: pd.Timestamp, n: int) -> pd.DataFrame:
    """The last `n` closes strictly before `session`: nothing from the future gets through."""
    window = history.loc[history.index < session].iloc[-n:]
    if len(window) < n:
        raise ReplayError(f"only {len(window)} sessions before {session.date()}, {n} needed")
    if not (window.index < session).all():  # pragma: no cover - guarded by the filter above
        raise ReplayError("look-ahead: the window contains the decision session")
    return window


def _view(
    closes_a: pd.DataFrame, synthetic: pd.DatetimeIndex, session: pd.Timestamp, cfg: AgentConfig
) -> tuple[pd.DataFrame, pd.DataFrame, datetime]:
    """What the decision at `session` may see: the real history before it (for the simulation), the
    same history on a synthetic calendar (for the LLM), and a synthetic 10:00 timestamp."""
    history = point_in_time(closes_a, session, cfg.forecast.context_length)
    shown = history.copy()
    shown.index = synthetic[closes_a.index.get_indexer(history.index)]
    day = pd.Timestamp(synthetic[int(closes_a.index.get_indexer(pd.Index([session]))[0])]).date()
    return (
        history,
        shown,
        datetime.combine(day, datetime.min.time()).replace(hour=10, tzinfo=NEW_YORK),
    )


@dataclass
class Portfolio:
    cash: float = START_EQUITY
    shares: dict[str, float] = field(default_factory=dict)

    def value(self, prices: Mapping[str, float]) -> float:
        return self.cash + sum(q * prices[s] for s, q in self.shares.items())

    def weights(self, symbols: Sequence[str], prices: Mapping[str, float]) -> dict[str, float]:
        equity = self.value(prices)
        return {s: self.shares.get(s, 0.0) * prices[s] / equity for s in symbols}


def execute_at_open(
    pf: Portfolio,
    target: Mapping[str, float],
    opens: Mapping[str, float],
    cfg: AgentConfig,
    rid: str,
    cost_bps: float = COST_BPS,
) -> list[Any]:
    """Plan with the production planner and fill every order at the open plus a cost."""
    positions = {s: Position(q, q * opens[s]) for s, q in pf.shares.items() if q > 0}
    plan = plan_orders(target, positions, pf.value(opens), opens, cfg, rid)
    cost = cost_bps / 1e4
    for order in plan.orders:
        if order.side == "buy":
            fill = opens[order.symbol] * (1 + cost)
            pf.cash -= order.quantity * fill
            pf.shares[order.symbol] = pf.shares.get(order.symbol, 0.0) + order.quantity
        else:
            fill = opens[order.symbol] * (1 - cost)
            pf.cash += order.quantity * fill
            pf.shares[order.symbol] = pf.shares.get(order.symbol, 0.0) - order.quantity
    return order_records(plan, "replay_filled")


@dataclass(frozen=True)
class ReplayResult:
    sessions: int
    resumed: int
    execution_errors: int
    equity: list[float]


def record_observations(
    store: FileAuditStore, closes: pd.DataFrame, first: pd.Timestamp, last: pd.Timestamp
) -> None:
    """The observed closes (alias symbols, real dates) that the metrics of D-014 need."""
    for pos, ts in enumerate(closes.index):
        if not first <= ts <= last:
            continue
        name = ts.date().isoformat()
        if store.has_observation(name):
            continue
        window = closes.iloc[max(0, pos - OBSERVATION_WINDOW + 1) : pos + 1]
        store.write_observation(
            name,
            {
                "as_of": name,
                "closes": {
                    pd.Timestamp(d).date().isoformat(): {str(s): float(v) for s, v in row.items()}
                    for d, (_, row) in zip(window.index, window.iterrows(), strict=True)
                },
            },
        )


def run_replay(
    *,
    closes: pd.DataFrame,
    opens: pd.DataFrame,
    sessions: Sequence[pd.Timestamp],
    cfg: AgentConfig,
    model: QuantileModel,
    llm: LLMClient,
    store: FileAuditStore,
    seed: int = 0,
    cost_bps: float = COST_BPS,
    log: Any = None,
) -> ReplayResult:
    alias_cfg, mapping = anonymise(cfg, seed)
    closes_a = closes[list(cfg.symbols)].rename(columns=mapping)
    opens_a = opens[list(cfg.symbols)].rename(columns=mapping)
    synthetic = pd.bdate_range(SYNTHETIC_START, periods=len(closes_a))
    symbols = list(alias_cfg.symbols)
    record_observations(store, closes_a, sessions[0] - pd.Timedelta(days=5), closes_a.index[-1])

    pf, resumed, errors = Portfolio(), 0, 0
    equity: list[float] = []
    for session in sessions:
        rid = f"{session.date().isoformat()}_morning"
        history, history_syn, now = _view(closes_a, synthetic, session, alias_cfg)
        prev_close = {str(s): float(v) for s, v in history.iloc[-1].items()}
        session_open = {str(s): float(v) for s, v in opens_a.loc[[session]].iloc[0].items()}
        equity_prev = pf.value(prev_close)
        peak = max([START_EQUITY, equity_prev, *equity])
        done = store.has_run(rid)

        if done:
            record = store.read_run(rid)
            outcome, target = record["decision"]["outcome"], record["portfolios"]["final"]
            resumed += 1
        else:
            forecasts = forecast_assets(history_syn, model, alias_cfg)
            baseline = baseline_weights(forecasts, alias_cfg)
            inp = DecisionInput(
                now=now,
                mode="morning",
                forecasts=forecasts,
                baseline=baseline,
                current=pf.weights(symbols, prev_close),
                closes=history_syn,
                drawdown=min(0.0, equity_prev / peak - 1.0),
                equity=equity_prev,
            )
            decision = decide_agent(inp, llm, model, alias_cfg, on_llm_failure="raise")
            outcome, target = decision.outcome, decision.target_weights
            if outcome != "fallback_hold":
                final_check(decision.target_weights, decision.cash, alias_cfg)

        orders: list[Any] = []
        if outcome != "fallback_hold":
            try:
                orders = execute_at_open(pf, target, session_open, alias_cfg, rid, cost_bps)
            except ExecutionError as error:
                errors += 1
                if log:
                    log(f"{rid}: no safe plan, no trade ({error})")

        if not done:
            record_obj, reasoning = build_record(
                mode="morning",
                now=now,
                cfg=alias_cfg,
                inp=inp,
                decision=decision,
                orders=orders,
                execution_status="dry_run",
            )
            record_obj.run_id = rid
            record_obj.started_at = f"{session.date().isoformat()}T10:00:00-05:00"
            record_obj.data["last_session"] = history.index[-1].date().isoformat()
            record_obj.reasoning_file = f"{rid}_reasoning.txt"
            store.write_run(record_obj, reasoning)
        equity.append(equity_prev)
        if log:
            log(f"{rid}: {outcome}, equity at the previous close {equity_prev:,.0f}")

    store.build_tables()
    return ReplayResult(len(sessions), resumed, errors, equity)


PROBE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "assets": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "alias": {"type": "string"},
                    "ticker": {"type": ["string", "null"]},
                },
                "required": ["alias", "ticker"],
                "additionalProperties": False,
            },
        },
        "period": {"type": ["string", "null"]},
    },
    "required": ["assets", "period"],
    "additionalProperties": False,
}

PROBE_QUESTION = (
    "This is anonymised market data. Try to identify each asset (a US ticker) and the calendar "
    "period (year and month, format YYYY-MM) it comes from. If you cannot tell, answer null. "
    "Do not guess to please: a null answer is better than a wrong one."
)


def score_probe(answer: str, mapping: Mapping[str, str], true_period: str) -> dict[str, Any]:
    """How much of the anonymised data the LLM managed to identify."""
    real = {alias: symbol for symbol, alias in mapping.items()}
    try:
        data = json.loads(answer)
        guesses = {a["alias"]: a["ticker"] for a in data["assets"]}
        period = data["period"]
    except (ValueError, KeyError, TypeError):
        return {"parsed": False, "correct_assets": 0, "n_assets": len(real), "period_ok": False}
    correct = sum(
        1
        for alias, symbol in real.items()
        if isinstance(guesses.get(alias), str) and guesses[alias].upper() == symbol
    )
    return {
        "parsed": True,
        "correct_assets": correct,
        "n_assets": len(real),
        "guessed_assets": sum(1 for g in guesses.values() if g),
        "period_guess": period,
        "period_ok": isinstance(period, str) and period[:4] == true_period[:4],
        "true_period": true_period,
    }


def run_probe(
    *,
    closes: pd.DataFrame,
    sessions: Sequence[pd.Timestamp],
    cfg: AgentConfig,
    model: QuantileModel,
    llm: LLMClient,
    seed: int = 0,
) -> list[dict[str, Any]]:
    """Ask the LLM to identify the anonymised payload of each given session."""
    alias_cfg, mapping = anonymise(cfg, seed)
    closes_a = closes[list(cfg.symbols)].rename(columns=mapping)
    synthetic = pd.bdate_range(SYNTHETIC_START, periods=len(closes_a))
    out = []
    for session in sessions:
        _, history_syn, now = _view(closes_a, synthetic, session, alias_cfg)
        forecasts = forecast_assets(history_syn, model, alias_cfg)
        cash = dict.fromkeys(alias_cfg.symbols, 0.0)
        inp = DecisionInput(
            now=now, mode="morning", forecasts=forecasts,
            baseline=baseline_weights(forecasts, alias_cfg), current=cash,
            closes=history_syn, drawdown=0.0, equity=START_EQUITY,
        )  # fmt: skip
        payload = json.dumps(build_payload(inp, alias_cfg), indent=1)
        response = llm.complete(
            [
                {"role": "system", "content": PROBE_QUESTION},
                {"role": "user", "content": payload},
            ],
            PROBE_SCHEMA,
        )
        result = score_probe(response.text, mapping, session.date().isoformat()[:7])
        out.append({"session": session.date().isoformat(), **result, "answer": response.text})
    return out


def write_manifest(
    out: Path, mapping: Mapping[str, str], cfg: AgentConfig, seed: int, cost_bps: float,
    sessions: Sequence[pd.Timestamp],
) -> Path:  # fmt: skip
    manifest = {
        "decision": "D-015",
        "first_session": sessions[0].date().isoformat(),
        "last_session": sessions[-1].date().isoformat(),
        "sessions": len(sessions),
        "alias_seed": seed,
        "alias_to_real": {alias: symbol for symbol, alias in mapping.items()},
        "cost_bps_per_side": cost_bps,
        "fill_price": "the open of the decision session, plus the cost",
        "llm": {"model": cfg.llm.model, "prompt": cfg.llm.prompt_version,
                "reasoning_effort": cfg.llm.reasoning_effort},
        "timestral": cfg.forecast.model_id,
        "note": "Replay records use real dates and alias symbols; the LLM only saw aliases, a "
        "synthetic calendar and no price level.",
    }  # fmt: skip
    out.mkdir(parents=True, exist_ok=True)
    path = out / "manifest.json"
    path.write_text(json.dumps(manifest, indent=1) + "\n", encoding="utf-8")
    return path


def replay_report(store: FileAuditStore) -> dict[str, Any]:
    """The D-014 report over the replay records (the benchmark alias comes from each record)."""
    runs = sorted(store.read_runs(), key=lambda r: r["started_at"])
    return performance_report(runs, store.read_observations())


def _pct(x: float | None, digits: int = 2) -> str:
    return "n/a" if x is None else f"{100 * x:+.{digits}f}%"


def _sharpe(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.2f}"


def render_markdown(
    report: dict[str, Any], manifest: Mapping[str, Any], probes: Sequence[Mapping[str, Any]]
) -> str:
    series = report["series"]
    rows = "\n".join(
        f"| {name} | {_pct(s['cumulative_return'])} | "
        f"{_pct(s['annualised_volatility'], 1).lstrip('+')} | "
        f"{_sharpe(s['sharpe'])} | {_pct(s['max_drawdown'])} |"
        for name, s in series.items()
    )
    comps = "\n".join(
        f"| {c['label']} | {100 * c['cumulative_difference']:+.2f} pts | {c['sessions']} | "
        + (
            f"[{100 * c['ci95_mean_daily'][0]:+.3f}, {100 * c['ci95_mean_daily'][1]:+.3f}] pts/day"
            if c["ci95_mean_daily"]
            else "n/a (fewer than 10 sessions)"
        )
        + f" | {'yes' if c['conclusive'] else 'no'} |"
        for c in report["comparisons"]
    )
    risk = (
        "\n".join(
            f"| {r['horizon']} | {r['n']} | {100 * r['inside_80']:.0f}% | {r['rms_z']:.2f} |"
            for r in report["risk_calibration"]
        )
        or "| no resolved forecast | | | |"
    )
    c = report["confidence"]
    confidence = (
        f"{c['n']} tilts resolved, average stated confidence {c['mean_confidence']:.2f}, paid off "
        f"{100 * c['hit_rate']:.0f}%, Brier score {c['brier']:.3f} (0.25 = always answering 0.5)."
        if c
        else "No tilt resolved."
    )
    probe_rows = (
        "\n".join(
            f"| {p['session']} | {p.get('correct_assets', 0)}/{p.get('n_assets', 0)} | "
            f"{p.get('period_guess')} | {'yes' if p.get('period_ok') else 'no'} |"
            for p in probes
        )
        or "| not run | | | |"
    )
    return f"""# Historical replay (D-015)

Test case 1 of the evaluation: the production agent run over {report["sessions"]} past sessions
({manifest["first_session"]} to {manifest["last_session"]}), one decision per session.
Generated by `python -m agent.evaluation.replay report`; raw records, tables and prices
are in this folder.

**How it differs from live** (see D-015): point-in-time data enforced in code; assets renamed,
synthetic calendar, no price level or news shown to the LLM; simulated fills at the open plus
{manifest["cost_bps_per_side"]} bps per side; no broker access.
Agent: {manifest["llm"]["model"]}, prompt {manifest["llm"]["prompt"]}, reasoning
{manifest["llm"]["reasoning_effort"]}, Timestral {manifest["timestral"]}. Parameters frozen.

## Performance of each layer (valued as if executed at the close, no costs)

| Portfolio | Cumulative return | Annualised volatility | Sharpe | Maximum drawdown |
|---|---|---|---|---|
{rows}

Simulated account (fills at the open plus costs): {_pct(report["account_return"])} between the
first and the last decision.

## What does each layer add?

| Question | Difference | Sessions | 95% interval of the mean daily difference | Conclusive |
|---|---|---|---|---|
{comps}

A difference is conclusive only when its 95% moving-block-bootstrap interval excludes zero.

## Forecast portfolio risk against what happened

| Horizon (days) | Resolved forecasts | Inside the 80% band (target 80%) | RMS ratio (1 is exact) |
|---|---|---|---|
{risk}

## The LLM's stated confidence

{confidence}

## Leakage probe

The LLM was shown anonymised payloads and asked to identify the assets and the period.
Assets are identified when the ticker matches exactly. Sector groups are kept in the payload
(the guardrails need them), so bonds and gold are easier to name than the others.

| Session | Assets identified | Period guess | Right year |
|---|---|---|---|
{probe_rows}

## Caveats

- One market regime and {report["sessions"]} sessions: read the intervals, not the point
  estimates.
- The Timestral configuration of D-006 was chosen on a backtest whose origins overlap this period,
  without any change of the value fixed beforehand.
- Anonymisation reduces, and does not remove, the chance that the LLM recognises a market episode.
"""


def load_history(  # pragma: no cover - network and cache
    cache: Path, cfg: AgentConfig, end: pd.Timestamp, n_sessions: int
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Adjusted closes and opens, from the snapshot if present, else from Alpaca (then saved)."""
    closes_file, opens_file = cache / "closes.csv", cache / "opens.csv"
    if closes_file.exists() and opens_file.exists():
        return (
            pd.read_csv(closes_file, index_col=0, parse_dates=True),
            pd.read_csv(opens_file, index_col=0, parse_dates=True),
        )
    import os

    from alpaca.data.enums import Adjustment, DataFeed
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.models import BarSet
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    from dotenv import load_dotenv

    load_dotenv()
    client = StockHistoricalDataClient(
        os.environ["ALPACA_API_KEY"], os.environ["ALPACA_SECRET_KEY"]
    )
    start = end - pd.Timedelta(days=int((cfg.forecast.context_length + n_sessions + 25) * 1.5))
    request = StockBarsRequest(
        symbol_or_symbols=list(cfg.symbols),
        timeframe=TimeFrame.Day,
        start=start.to_pydatetime(),
        end=(end + pd.Timedelta(days=2)).to_pydatetime(),
        adjustment=Adjustment.ALL,
        feed=DataFeed.SIP,
    )
    barset = client.get_stock_bars(request)
    if not isinstance(barset, BarSet):
        raise ReplayError("unexpected raw response from Alpaca market data")
    frame = barset.df
    out = []
    for column in ("close", "open"):
        wide = frame[column].unstack(level="symbol")
        wide.index = pd.DatetimeIndex(
            [pd.Timestamp(ts).tz_convert(NEW_YORK).date() for ts in wide.index]
        )
        out.append(wide[list(cfg.symbols)])
    cache.mkdir(parents=True, exist_ok=True)
    out[0].to_csv(closes_file)
    out[1].to_csv(opens_file)
    return out[0], out[1]


def main() -> None:  # pragma: no cover - CLI wiring, long-running
    import os
    import sys

    from dotenv import load_dotenv

    from agent.decision.decide import MistralLLM
    from agent.inputs.forecast import load_model
    from agent.jobs.run import AGENTS, config_for

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["run", "probe", "report"])
    parser.add_argument("--out", default="docs/replay", type=Path)
    parser.add_argument("--sessions", default=63, type=int)
    parser.add_argument("--end", default="2026-11-20")
    parser.add_argument("--seed", default=0, type=int)
    parser.add_argument("--n", default=5, type=int, help="probe: number of sessions to probe")
    parser.add_argument("--agent", choices=AGENTS, default="pipeline")
    args = parser.parse_args()

    load_dotenv()
    cfg = config_for(args.agent)
    store = FileAuditStore(args.out)
    if args.command == "report":
        manifest = json.loads((args.out / "manifest.json").read_text(encoding="utf-8"))
        probes_file = args.out / "probes.json"
        probes = json.loads(probes_file.read_text(encoding="utf-8")) if probes_file.exists() else []
        report = replay_report(store)
        (args.out / "report.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
        (args.out / "README.md").write_text(
            render_markdown(report, manifest, probes), encoding="utf-8"
        )
        print(f"report written for {report['sessions']} sessions")
        sys.exit(0)

    end = pd.Timestamp(args.end)
    closes, opens = load_history(args.out / "data", cfg, end, args.sessions)
    sessions = list(closes.index[closes.index <= end][-args.sessions :])
    model = load_model(cfg.forecast.model_id)
    llm = MistralLLM(os.environ["MISTRAL_API_KEY"], cfg)
    if args.command == "run":
        _, mapping = anonymise(cfg, args.seed)
        write_manifest(args.out, mapping, cfg, args.seed, COST_BPS, sessions)
        result = run_replay(
            closes=closes, opens=opens, sessions=sessions, cfg=cfg, model=model, llm=llm,
            store=store, seed=args.seed, log=lambda m: print(m, flush=True),
        )  # fmt: skip
        print(
            f"done: {result.sessions} sessions, {result.resumed} resumed, "
            f"{result.execution_errors} execution error(s)"
        )
    else:
        picks = [
            sessions[round(i * (len(sessions) - 1) / max(args.n - 1, 1))] for i in range(args.n)
        ]
        probes = run_probe(
            closes=closes, sessions=picks, cfg=cfg, model=model, llm=llm, seed=args.seed
        )
        (args.out / "probes.json").write_text(json.dumps(probes, indent=1), encoding="utf-8")
        print(f"probe done: {[(p['session'], p['correct_assets']) for p in probes]}")


if __name__ == "__main__":  # pragma: no cover
    main()
