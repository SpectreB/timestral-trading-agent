"""Run the forecast stage on live data and print the forecasts and the baseline portfolio.

Read-only: fetches Alpaca market data and runs Timestral locally. Places no orders.

Usage:
    python scripts/run_forecast.py [--model mistralai/Timestral-123M]
"""

from __future__ import annotations

import argparse
import os
import time
from dataclasses import replace
from datetime import UTC, datetime

from dotenv import load_dotenv

from agent.baseline import baseline_weights
from agent.config import AgentConfig, ForecastConfig
from agent.data import AlpacaMarketData, load_context
from agent.forecast import forecast_assets, load_model


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default=None, help="override the Timestral model id")
    args = parser.parse_args()

    cfg = AgentConfig()
    if args.model:
        cfg = replace(cfg, forecast=replace(cfg.forecast, model_id=args.model))
    assert isinstance(cfg.forecast, ForecastConfig)

    load_dotenv()
    source = AlpacaMarketData(os.environ["ALPACA_API_KEY"], os.environ["ALPACA_SECRET_KEY"])
    closes = load_context(source, cfg, datetime.now(UTC))
    print(
        f"Context: {len(closes)} completed sessions, "
        f"{closes.index[0].date()} -> {closes.index[-1].date()}"
    )

    t0 = time.perf_counter()
    model = load_model(cfg.forecast.model_id)
    t1 = time.perf_counter()
    forecasts = forecast_assets(closes, model, cfg)
    t2 = time.perf_counter()
    print(f"Model {cfg.forecast.model_id}: load {t1 - t0:.1f}s, forecast {t2 - t1:.2f}s\n")

    h = cfg.forecast.horizon
    weights = baseline_weights(forecasts, cfg)
    print(f"{'asset':6} {'last':>9} | {f'{h}-day return (%)':^26} | {'width':>6} | {'baseline':>8}")
    print(f"{'':6} {'close':>9} | {'q10':>8} {'median':>8} {'q90':>8} | {'80%':>6} | {'weight':>8}")
    print("-" * 66)
    for s in sorted(cfg.symbols, key=lambda x: -weights[x]):
        f = forecasts[s]
        print(
            f"{s:6} {f.last_price:9.2f} | {100 * f.quantile(h, 0.1):8.2f} "
            f"{100 * f.median(h):8.2f} {100 * f.quantile(h, 0.9):8.2f} | "
            f"{100 * f.width(h):6.2f} | {100 * weights[s]:7.2f}%"
        )
    invested = sum(weights.values())
    print("-" * 66)
    print(f"{'invested':>51} {100 * invested:7.2f}%")
    print(f"{'cash':>51} {100 * (1 - invested):7.2f}%")


if __name__ == "__main__":
    main()
