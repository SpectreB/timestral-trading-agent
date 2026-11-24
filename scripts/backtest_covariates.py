"""What do the news covariates add to Timestral? (D-019, measurement plan)

Timestral-209M, the model of the tool agent, forecasts a panel of 30 stocks and the 10 ETFs of the
trading universe at every past origin of the news coverage, with prices alone and with each family
of covariates (`news`, `events`, both). Each 5-day forecast is scored against what happened. The
ETFs are the control group: they receive no company news.

The adoption rule is in `agent.covariate_eval`, fixed before the results were known.

Network use, in this order, each cached so that a rerun is cheap:
  1. Alpaca: adjusted daily closes of the 40 assets, and the Benzinga news of the 30 stocks;
  2. the Mistral analyst (`mistral-small-2603`): each (article, stock) scored once, cached in
     `--cache` (the score cache of the live agents is never touched);
  3. FRED: the official dates of the CPI and employment releases.

Usage:
    python scripts/backtest_covariates.py --check            # what would run, no network
    python scripts/backtest_covariates.py [--step 1] [--out docs/covariates]

Caveat, stated in the results: on history the analyst may know what followed an article, so the
sentiment can leak the future. Article volume and announced dates are not exposed to that bias.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
from dotenv import load_dotenv

from agent.config import AssetType
from agent.covariate_eval import BASE, VARIANTS, paired, panel_config, score, summarise, verdict
from agent.data import AlpacaMarketData
from agent.forecast import _to_numpy, load_model
from agent.news import (
    AlpacaNews,
    ScoreCache,
    build_covariates,
    fred_release_dates,
    news_covariates,
)

HORIZON = 5
MODEL = "mistralai/Timestral-209M"
FAMILIES = {BASE: (), "news": ("news",), "events": ("events",), "news+events": ("news", "events")}


def load_closes(symbols: list[str], context: int) -> pd.DataFrame:
    source = AlpacaMarketData(os.environ["ALPACA_API_KEY"], os.environ["ALPACA_SECRET_KEY"])
    end = datetime.now(UTC) - timedelta(minutes=16)
    closes = source.daily_closes(symbols, end - timedelta(days=int(context * 1.5) + 400), end)
    if source.is_market_open():  # today's bar is not a close yet
        closes = closes[closes.index < pd.Timestamp(datetime.now(UTC).date())]
    return closes[symbols].dropna()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=Path("docs/covariates"), type=Path)
    parser.add_argument("--cache", default=Path("replay_out/covariates"), type=Path)
    parser.add_argument("--step", default=1, type=int, help="one origin every N sessions")
    parser.add_argument("--check", action="store_true", help="print the plan, use no network")
    args = parser.parse_args()
    load_dotenv()

    cfg = panel_config()
    symbols = list(cfg.symbols)
    stocks = [a.symbol for a in cfg.universe if a.type is AssetType.STOCK]
    coverage_start = date.fromisoformat(cfg.news.coverage_start)
    context = cfg.forecast.context_length
    if args.check:
        print(f"{len(symbols)} assets: {len(stocks)} stocks, {len(symbols) - len(stocks)} ETFs")
        print(f"model {MODEL}, horizon {HORIZON}, context {context}, variants {list(VARIANTS)}")
        print(f"origins: every {args.step} session(s) from {coverage_start} to 5 sessions ago")
        print(f"news and scores cached in {args.cache}; results in {args.out}")
        sys.exit(0)

    from agent.decide import MistralLLM

    analyst_cfg = replace(
        cfg,
        llm=replace(cfg.llm, model=cfg.news.analyst_model, temperature=0.0, reasoning_effort=""),
    )
    closes = load_closes(symbols, context)
    print(f"closes {closes.index[0].date()} to {closes.index[-1].date()}, {len(closes)} sessions")

    # 1-2. Score every article of the coverage once (cached), through the same code as production.
    args.cache.mkdir(parents=True, exist_ok=True)
    cache = ScoreCache(args.cache)
    fred_key = os.environ.get("FRED_API_KEY")
    fred_dates = fred_release_dates(fred_key, cfg.news.fred_release_ids) if fred_key else None
    t0 = time.time()
    full = news_covariates(
        closes=closes.iloc[-context:],
        cfg=cfg,
        source=AlpacaNews(os.environ["ALPACA_API_KEY"], os.environ["ALPACA_SECRET_KEY"]),
        analyst=MistralLLM(os.environ["MISTRAL_API_KEY"], analyst_cfg),
        cache=cache,
        fred_macro=fred_dates,
    )
    scores = list(full.scores)
    print(f"{len(scores)} scores in {time.time() - t0:.0f}s, FRED dates {len(full.fred_macro)}")

    # 3. Forecast at every origin, with and without covariates, and score against the outcome.
    model = load_model(MODEL)
    dates = [d.date() for d in closes.index]
    first = next(i for i, d in enumerate(dates) if d >= coverage_start)
    origins = list(range(max(first, context - 1), len(dates) - HORIZON, args.step))
    values = closes.to_numpy(dtype=np.float64)
    kinds = ["stock" if s in stocks else "etf" for s in symbols]
    rows = []
    for n, o in enumerate(origins):
        sessions = dates[o - context + 1 : o + 1]
        known_by_then = [s for s in scores if s.session <= sessions[-1].isoformat()]
        cov = build_covariates(
            sessions=sessions,
            horizon=HORIZON,
            cfg=cfg,
            scores=known_by_then,
            coverage_start=coverage_start,
            fred_macro=full.fred_macro,
        )
        window = values[o - context + 1 : o + 1].T  # (B, T)
        outcome = values[o + HORIZON] / values[o] - 1
        for variant, families in FAMILIES.items():
            kwargs = {k: v for k, v in cov.arrays(families).items() if v is not None}
            out = model.forecast(window, prediction_length=HORIZON, **kwargs)
            levels = tuple(float(v) for v in _to_numpy(out.quantile_levels))
            q = _to_numpy(out.quantiles).reshape(len(symbols), HORIZON, len(levels))
            cum = np.maximum.accumulate(q[:, HORIZON - 1, :] / values[o][:, None] - 1.0, axis=1)
            for i, symbol in enumerate(symbols):
                rows.append(
                    {"origin": dates[o].isoformat(), "symbol": symbol, "kind": kinds[i],
                     "variant": variant, **score(levels, cum[i], float(outcome[i]))}
                )  # fmt: skip
        if n % 10 == 0:
            print(f"origin {n + 1}/{len(origins)} ({dates[o]})", flush=True)

    frame = pd.DataFrame(rows)
    summary = summarise(frame)
    pairs = [paired(frame, v, k) for v in VARIANTS if v != BASE for k in ("stock", "etf")]
    verdicts = [verdict(summary, pairs, v) for v in VARIANTS if v != BASE]
    args.out.mkdir(parents=True, exist_ok=True)
    result = {
        "model": MODEL,
        "horizon": HORIZON,
        "origins": len(origins),
        "first_origin": dates[origins[0]].isoformat(),
        "last_origin": dates[origins[-1]].isoformat(),
        "stocks": len(stocks),
        "etfs": len(symbols) - len(stocks),
        "analyst": cfg.news.analyst_model,
        "summary": summary,
        "paired": pairs,
        "verdicts": verdicts,
    }
    (args.out / "results.json").write_text(json.dumps(result, indent=1), encoding="utf-8")
    frame.to_csv(args.cache / "forecast_scores.csv", index=False)
    for v in verdicts:
        print(v["variant"], "->", v["decision"], v)


if __name__ == "__main__":
    main()
