"""Exploration script: run Timestral on daily stock data and sanity-check its forecasts.

Questions it answers:
  1. What does the API return (shapes, quantile levels, types)?
  2. How fast is CPU inference for a realistic batch?
  3. Should we feed prices or log returns?
  4. Do the forecasts beat naive baselines out of sample (rolling backtest)?

Data comes from yfinance (no API key) for exploration only; production will use Alpaca.

Usage:
    python scripts/explore_timestral.py --ticker SPY --model mistralai/Timestral-28M
"""

from __future__ import annotations

import argparse
import time

import numpy as np
import pandas as pd
import yfinance as yf
from timestral import TimestralForecaster


def to_np(x) -> np.ndarray:
    """Convert a torch tensor or array-like to a NumPy array."""
    if hasattr(x, "detach"):
        x = x.detach().cpu().numpy()
    return np.asarray(x)


def load_closes(ticker: str, years: int) -> pd.Series:
    df = yf.download(ticker, period=f"{years}y", interval="1d", auto_adjust=True, progress=False)
    close = df["Close"]
    if isinstance(close, pd.DataFrame):  # yfinance may return a MultiIndex column
        close = close.iloc[:, 0]
    return close.dropna()


def pinball(y: np.ndarray, q: np.ndarray, levels: np.ndarray) -> float:
    """Mean pinball loss. y: (N,), q: (N, L), levels: (L,)."""
    diff = y[:, None] - q
    return float(np.mean(np.maximum(levels * diff, (levels - 1) * diff)))


def report(name: str, y: np.ndarray, q: np.ndarray, levels: np.ndarray) -> dict:
    """Metrics on 1-step-ahead log returns. y: (N,), q: (N, L)."""
    i10 = int(np.argmin(np.abs(levels - 0.10)))
    i50 = int(np.argmin(np.abs(levels - 0.50)))
    i90 = int(np.argmin(np.abs(levels - 0.90)))
    med = q[:, i50]
    nonzero = med != 0
    return {
        "model": name,
        "pinball(all q)": pinball(y, q, levels),
        "MAE(median)": float(np.mean(np.abs(y - med))),
        "80% PI coverage": float(np.mean((y >= q[:, i10]) & (y <= q[:, i90]))),
        "80% PI width": float(np.mean(q[:, i90] - q[:, i10])),
        "dir. hit rate": float(np.mean(np.sign(med[nonzero]) == np.sign(y[nonzero])))
        if nonzero.any()
        else np.nan,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticker", default="SPY")
    ap.add_argument("--model", default="mistralai/Timestral-28M")
    ap.add_argument("--years", type=int, default=10)
    ap.add_argument("--context", type=int, default=512, help="context length in trading days")
    ap.add_argument("--horizon", type=int, default=5, help="forecast horizon in trading days")
    ap.add_argument("--origins", type=int, default=250, help="number of rolling backtest origins")
    args = ap.parse_args()

    closes = load_closes(args.ticker, args.years)
    prices = closes.to_numpy(dtype=np.float64)
    logret = np.diff(np.log(prices))  # logret[t] = log(P[t+1] / P[t])
    print(
        f"{args.ticker}: {len(prices)} daily closes, "
        f"{closes.index[0].date()} -> {closes.index[-1].date()}"
    )

    # --- 1. Load the model and inspect the output -------------------------------------
    t0 = time.perf_counter()
    fc = TimestralForecaster.from_pretrained(args.model)
    print(f"Loaded {args.model} in {time.perf_counter() - t0:.1f}s")

    out = fc.forecast(logret[-args.context :], prediction_length=args.horizon)
    levels = to_np(out.quantile_levels).astype(np.float64)
    print("\n[API] output type:", type(out).__name__)
    print(
        "[API] quantiles shape:",
        to_np(out.quantiles).shape,
        "| median shape:",
        to_np(out.median).shape,
    )
    print("[API] quantile levels:", np.round(levels, 3).tolist())

    # --- 2. Rolling backtest: batch all origins into one call per mode ------------------
    # Origin o means "we know prices up to and including P[o]";
    # the target is the next-day return logret[o].
    n_ret = len(logret)
    last_origin = n_ret - args.horizon  # keep the full horizon observable for every origin
    origins = np.arange(last_origin - args.origins, last_origin)
    assert origins[0] >= args.context, (
        "Not enough history; increase --years or reduce --context/--origins"
    )

    ret_ctx = np.stack([logret[o - args.context : o] for o in origins])  # (B, T) returns up to P[o]
    px_ctx = np.stack(
        [prices[o + 1 - args.context : o + 1] for o in origins]
    )  # (B, T) prices up to P[o]
    y_1d = logret[origins]  # realized next-day return
    y_path = np.stack([logret[o : o + args.horizon] for o in origins])  # (B, H) realized returns

    t0 = time.perf_counter()
    out_ret = fc.forecast(ret_ctx, prediction_length=args.horizon)
    t_ret = time.perf_counter() - t0
    t0 = time.perf_counter()
    out_px = fc.forecast(px_ctx, prediction_length=args.horizon)
    t_px = time.perf_counter() - t0
    print(
        f"\n[speed] batch of {len(origins)} series x {args.context} steps, horizon {args.horizon}: "
        f"returns {t_ret:.2f}s, prices {t_px:.2f}s ({1000 * t_ret / len(origins):.1f} ms/series)"
    )

    q_ret = to_np(out_ret.quantiles).astype(np.float64)  # (B, 1, H, L) or (B, H, L)
    q_ret = q_ret.reshape(len(origins), args.horizon, -1)
    q_px = to_np(out_px.quantiles).astype(np.float64).reshape(len(origins), args.horizon, -1)

    # Price mode -> 1-day log-return quantiles (exact, since log(q/P0) is monotone in q).
    p0 = px_ctx[:, -1]
    q_px_as_ret = np.log(np.clip(q_px[:, 0, :], 1e-9, None) / p0[:, None])

    # Baselines
    q_zero = np.zeros((len(origins), len(levels)))  # random walk, point only
    q_emp = np.stack(
        [np.quantile(ret_ctx[i], levels) for i in range(len(origins))]
    )  # empirical quantiles of context
    q_emp_demeaned = q_emp - np.median(ret_ctx, axis=1, keepdims=True)  # same spread, zero median

    rows = [
        report("Timestral (returns input)", y_1d, q_ret[:, 0, :], levels),
        report("Timestral (prices input)", y_1d, q_px_as_ret, levels),
        report("Empirical quantiles", y_1d, q_emp, levels),
        report("Empirical, zero median", y_1d, q_emp_demeaned, levels),
        report("Random walk (point)", y_1d, q_zero, levels),
    ]
    print(
        f"\n[backtest] 1-day-ahead log returns, {len(origins)} origins "
        f"({closes.index[origins[0] + 1].date()} -> {closes.index[origins[-1] + 1].date()})"
    )
    print(f"  realized daily vol: {y_1d.std():.4f}, share of up days: {np.mean(y_1d > 0):.2f}")
    with pd.option_context("display.float_format", "{:.5f}".format, "display.width", 140):
        print(pd.DataFrame(rows).set_index("model").to_string())

    # --- 3. Multi-step: does calibration hold across the horizon? -----------------------
    print("\n[horizon] Timestral (returns input), per-step metrics")
    step_rows = []
    for h in range(args.horizon):
        r = report(f"t+{h + 1}", y_path[:, h], q_ret[:, h, :], levels)
        step_rows.append(r)
    with pd.option_context("display.float_format", "{:.5f}".format, "display.width", 140):
        print(pd.DataFrame(step_rows).set_index("model").to_string())

    # --- 4. Latest forecast, as the LLM would see it ------------------------------------
    i10, i50, i90 = (int(np.argmin(np.abs(levels - x))) for x in (0.10, 0.50, 0.90))
    q_live = to_np(out.quantiles).astype(np.float64).reshape(args.horizon, -1)
    print(
        f"\n[live] {args.ticker} next {args.horizon} trading days after {closes.index[-1].date()} "
        f"(daily log returns, %):"
    )
    for h in range(args.horizon):
        print(
            f"  t+{h + 1}: p10 {100 * q_live[h, i10]:+.2f}  p50 {100 * q_live[h, i50]:+.2f}  "
            f"p90 {100 * q_live[h, i90]:+.2f}"
        )


if __name__ == "__main__":
    main()
