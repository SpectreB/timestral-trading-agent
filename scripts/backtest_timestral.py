"""Multi-asset backtest of the Timestral configuration (D-006).

Question: which model size, input type (prices or log returns) and context length should the
live agent use? Each configuration forecasts the 15 assets of the universe from a rolling set
of past origins (Alpaca adjusted daily closes), and is scored against what actually happened:

- calibration: share of outcomes inside the 80% interval (target 0.80);
- pinball loss averaged over the 21 quantile levels (lower is better);
- risk ranking: Spearman correlation between the forecast interval width and the realised
  absolute return. This is what the baseline rule uses, so it is the metric that matters most;
- directional hit rate (the agent does not rely on it; reported for completeness).

Baseline for comparison: empirical quantiles of the asset's own past returns over the same
context window (no model at all). A Timestral configuration only earns its place if it beats it.

Usage:
    python scripts/backtest_timestral.py [--origins 125] [--step 2] [--out docs/timestral_backtest]
"""

from __future__ import annotations

import argparse
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
from dotenv import load_dotenv

from agent.config import AgentConfig
from agent.inputs.data import AlpacaMarketData

HORIZON = 5
CHUNK = 125


@dataclass(frozen=True)
class Config:
    model: str
    kind: str  # "price" or "log_return"
    context: int

    @property
    def name(self) -> str:
        return f"{self.model.split('-')[-1]} {self.kind} ctx{self.context}"


CONFIGS = [
    Config("mistralai/Timestral-28M", "price", 512),
    Config("mistralai/Timestral-28M", "price", 1024),
    Config("mistralai/Timestral-28M", "price", 2048),
    Config("mistralai/Timestral-28M", "log_return", 512),
    Config("mistralai/Timestral-28M", "log_return", 1024),
    Config("mistralai/Timestral-28M", "log_return", 2048),
    Config("mistralai/Timestral-123M", "price", 512),
    Config("mistralai/Timestral-123M", "price", 1024),
    Config("mistralai/Timestral-123M", "log_return", 512),
    Config("mistralai/Timestral-5M", "price", 512),
    Config("mistralai/Timestral-209M", "price", 512),
    Config("mistralai/Timestral-209M", "price", 1024),
]


def pinball(q: np.ndarray, y: np.ndarray, levels: np.ndarray) -> float:
    """Mean pinball loss. q: (N, L) quantiles, y: (N,) outcomes."""
    diff = y[:, None] - q
    return float(np.mean(np.maximum(levels * diff, (levels - 1) * diff)))


def score(q: np.ndarray, y: np.ndarray, levels: np.ndarray) -> dict[str, float]:
    i10, i50, i90 = (int(np.argmin(np.abs(levels - v))) for v in (0.1, 0.5, 0.9))
    width = q[:, i90] - q[:, i10]
    ranks = pd.DataFrame({"w": width, "a": np.abs(y)}).rank()
    return {
        "pinball": pinball(q, y, levels),
        "coverage80": float(np.mean((y >= q[:, i10]) & (y <= q[:, i90]))),
        "width80": float(width.mean()),
        "spearman_width_vs_abs": float(ranks["w"].corr(ranks["a"])),
        "hit_rate": float(np.mean(np.sign(q[:, i50]) == np.sign(y))),
    }


def empirical_quantiles(
    closes: np.ndarray, o: int, context: int, h: int, levels: np.ndarray
) -> np.ndarray:
    """Quantiles of the h-day returns inside the context window ending at origin o."""
    window = closes[o - context + 1 : o + 1]
    returns = window[h:] / window[:-h] - 1
    return np.quantile(returns, levels)


def load_closes() -> pd.DataFrame:
    load_dotenv()
    cfg = AgentConfig()
    source = AlpacaMarketData(os.environ["ALPACA_API_KEY"], os.environ["ALPACA_SECRET_KEY"])
    end = datetime.now(UTC) - timedelta(minutes=16)
    closes = source.daily_closes(list(cfg.symbols), end - timedelta(days=int(10.6 * 365)), end)
    if source.is_market_open():  # drop today's incomplete bar
        closes = closes[closes.index < pd.Timestamp(datetime.now(UTC).date())]
    return closes[list(cfg.symbols)].dropna()


def run(
    config: Config, closes: pd.DataFrame, origins: np.ndarray, model: object
) -> dict[str, float]:
    values = closes.to_numpy(dtype=np.float64)  # (days, assets)
    n_assets = values.shape[1]
    contexts, targets = [], {1: [], HORIZON: []}
    keys = [(a, int(o)) for a in range(n_assets) for o in origins]
    for a, o in keys:
        series = values[:, a]
        if config.kind == "price":
            contexts.append(series[o - config.context + 1 : o + 1])
        else:
            contexts.append(np.diff(np.log(series[o - config.context : o + 1])))
        for h in targets:
            targets[h].append(series[o + h] / series[o] - 1)
    x = np.stack(contexts)
    last = np.array([values[o, a] for a, o in keys])

    parts = []
    for start in range(0, len(x), CHUNK):
        parts.append(model.forecast(x[start : start + CHUNK], prediction_length=HORIZON))  # type: ignore[attr-defined]
    levels = np.asarray(_np(parts[0].quantile_levels), dtype=np.float64)
    q = np.concatenate(
        [_np(p.quantiles).reshape(len(_np(p.quantiles)), HORIZON, -1) for p in parts]
    )

    result: dict[str, float] = {}
    for h in (1, HORIZON):
        y = np.asarray(targets[h])
        if config.kind == "price":
            qh = q[:, h - 1, :] / last[:, None] - 1
        elif h == 1:
            qh = np.exp(q[:, 0, :]) - 1
        else:
            continue  # quantiles of a sum are not the sum of quantiles: not comparable
        for name, value in score(qh, y, levels).items():
            result[f"{name}_h{h}"] = value
    return result


def _np(x: object) -> np.ndarray:
    return x.detach().cpu().numpy() if hasattr(x, "detach") else np.asarray(x)  # type: ignore[attr-defined, no-any-return]


def baseline_row(closes: pd.DataFrame, origins: np.ndarray, context: int) -> dict[str, float]:
    values = closes.to_numpy(dtype=np.float64)
    levels = np.linspace(0.05, 0.95, 19)
    levels = np.concatenate([[0.01], levels, [0.99]])
    result: dict[str, float] = {}
    for h in (1, HORIZON):
        qs, ys = [], []
        for a in range(values.shape[1]):
            for o in origins:
                qs.append(empirical_quantiles(values[:, a], int(o), context, h, levels))
                ys.append(values[int(o) + h, a] / values[int(o), a] - 1)
        for name, value in score(np.stack(qs), np.asarray(ys), levels).items():
            result[f"{name}_h{h}"] = value
    return result


def main() -> None:
    from timestral import TimestralForecaster

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origins", type=int, default=125)
    parser.add_argument("--step", type=int, default=2)
    parser.add_argument("--out", type=Path, default=Path("docs/timestral_backtest"))
    parser.add_argument(
        "--models", nargs="*", help="only these sizes, e.g. 5M 209M (default: every config)"
    )
    args = parser.parse_args()
    configs = [c for c in CONFIGS if not args.models or c.model.split("-")[-1] in args.models]

    closes = load_closes()
    n = len(closes)
    last_origin = n - 1 - HORIZON
    origins = np.arange(last_origin - (args.origins - 1) * args.step, last_origin + 1, args.step)
    print(
        f"{n} sessions {closes.index[0].date()} -> {closes.index[-1].date()}, "
        f"{len(origins)} origins per asset ({closes.index[origins[0]].date()} -> "
        f"{closes.index[origins[-1]].date()}), {closes.shape[1]} assets",
        flush=True,
    )

    rows = []
    for context in (512, 1024, 2048):
        row = {
            "config": f"empirical ctx{context} (no model)",
            **baseline_row(closes, origins, context),
        }
        rows.append(row)
    models: dict[str, object] = {}
    for config in configs:
        if config.model not in models:
            models[config.model] = TimestralForecaster.from_pretrained(config.model)
        t0 = time.perf_counter()
        row = {"config": config.name, **run(config, closes, origins, models[config.model])}
        row["seconds"] = time.perf_counter() - t0
        rows.append(row)
        print(f"{config.name:28} done in {row['seconds']:.0f}s", flush=True)

    table = pd.DataFrame(rows).set_index("config")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    table.to_csv(args.out.with_suffix(".csv"))
    print(table.round(4).to_string())


if __name__ == "__main__":
    main()
