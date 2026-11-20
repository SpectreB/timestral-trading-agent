"""Run data -> forecast -> baseline -> LLM decision -> guardrails on live data. No orders.

Prints the LLM's analysis, the baseline vs proposed vs final weights, the guardrail
corrections, and saves the full decision (prompt, answer, reasoning) as JSON.

Usage:
    python scripts/run_decision.py [--out decision.json]
"""

from __future__ import annotations

import argparse
import json
import os
import time
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from alpaca.trading.client import TradingClient
from dotenv import load_dotenv

from agent.baseline import baseline_weights
from agent.config import AgentConfig
from agent.data import AlpacaMarketData, load_context
from agent.decide import DecisionInput, MistralLLM, decide
from agent.forecast import forecast_assets, load_model
from agent.schemas import Proposal


def current_weights(
    client: TradingClient, symbols: tuple[str, ...]
) -> tuple[dict[str, float], float]:
    account = client.get_account()
    equity = float(account.equity)  # type: ignore[union-attr]
    weights = dict.fromkeys(symbols, 0.0)
    for p in client.get_all_positions():  # type: ignore[union-attr]
        if p.symbol in weights:  # type: ignore[union-attr]
            weights[p.symbol] = float(p.market_value) / equity  # type: ignore[union-attr]
    return weights, equity


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=None, help="save the full decision as JSON")
    args = parser.parse_args()

    load_dotenv()
    cfg = AgentConfig()
    key, secret = os.environ["ALPACA_API_KEY"], os.environ["ALPACA_SECRET_KEY"]
    now = datetime.now(UTC)

    closes = load_context(AlpacaMarketData(key, secret), cfg, now)
    forecasts = forecast_assets(closes, load_model(cfg.forecast.model_id), cfg)
    baseline = baseline_weights(forecasts, cfg)
    current, equity = current_weights(TradingClient(key, secret, paper=True), cfg.symbols)

    inp = DecisionInput(
        now=now.astimezone(ZoneInfo("America/New_York")),
        mode="morning",
        forecasts=forecasts,
        baseline=baseline,
        current=current,
        closes=closes,
        drawdown=0.0,
        equity=equity,
    )
    t0 = time.perf_counter()
    decision = decide(inp, MistralLLM(os.environ["MISTRAL_API_KEY"], cfg), cfg)
    elapsed = time.perf_counter() - t0

    last = decision.attempts[-1]
    print(f"Outcome: {decision.outcome} | attempts: {len(decision.attempts)} | {elapsed:.1f}s")
    print(
        f"Model: {last.response.model} | tokens: {last.response.prompt_tokens} in, "
        f"{last.response.completion_tokens} out | prompt {decision.prompt_version}\n"
    )
    proposal = None
    try:
        proposal = Proposal.model_validate_json(last.response.text)
        print("ANALYSIS\n" + proposal.analysis + "\n")
    except ValueError:
        print("(last answer could not be parsed)\n")

    proposed = {p.symbol: p for p in proposal.positions} if proposal else {}
    print(f"{'asset':6} {'baseline':>9} {'LLM':>8} {'final':>8} {'conf':>5}  rationale")
    for s in cfg.symbols:
        p = proposed.get(s)
        llm_w = f"{100 * p.target_weight:7.2f}%" if p else "      -"
        conf = f"{p.confidence:5.2f}" if p else "    -"
        rationale = (
            (p.rationale[:70] + "...")
            if p and len(p.rationale) > 70
            else (p.rationale if p else "")
        )
        print(
            f"{s:6} {100 * baseline[s]:8.2f}% {llm_w} {100 * decision.target_weights[s]:7.2f}% "
            f"{conf}  {rationale}"
        )
    print(
        f"{'cash':6} {100 * (1 - sum(baseline.values())):8.2f}% "
        f"{(100 * proposal.cash_weight if proposal else float('nan')):7.2f}% "
        f"{100 * decision.cash:7.2f}%"
    )

    corrections = last.guardrails.corrections
    print(f"\nGuardrail corrections: {len(corrections)}")
    for c in corrections:
        print(f"  {c.rule:24} {c.symbol or '':5} {c.before:.4f} -> {c.after:.4f}")
    print(f"Reasoning trace: {len(last.response.reasoning)} characters")

    if args.out:
        with open(args.out, "w") as f:
            json.dump(decision.model_dump(mode="json"), f, indent=1)
        print(f"Saved to {args.out}")


if __name__ == "__main__":
    main()
