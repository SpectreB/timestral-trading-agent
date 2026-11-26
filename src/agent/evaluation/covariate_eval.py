"""Measuring what the news covariates add to Timestral (D-019, measurement plan).

The same Timestral model forecasts a panel of stocks and the ETFs of the universe at many past
origins, with and without each family of covariates, and every forecast is scored against what
happened. The ETFs are the control group: they receive no company news, so a covariate that
"improves" them is improving something other than the news.

This module holds the parts that can be tested without a network or a model: the panel, the
scoring of one forecast, the summary, the paired comparison with a block bootstrap, and the
adoption rule, written **before** the results were known (D-019).
"""

from __future__ import annotations

import math
from dataclasses import replace
from typing import Any

import numpy as np
import pandas as pd

from agent.checks.risk import Z80
from agent.config import AgentConfig, Asset, AssetType, news_agent_config

BASE = "prices"
VARIANTS = (BASE, "news", "events", "news+events")

# Large US stocks across sectors, plus the five stocks the agents trade. The groups are only
# labels here: no guardrail is applied to a measurement.
PANEL_STOCKS: tuple[tuple[str, str, str], ...] = (
    ("NVDA", "NVIDIA", "tech"), ("AMZN", "Amazon", "consumer"), ("JPM", "JPMorgan Chase", "fin"),
    ("XOM", "Exxon Mobil", "energy"), ("LLY", "Eli Lilly", "health"),
    ("AAPL", "Apple", "tech"), ("MSFT", "Microsoft", "tech"), ("GOOGL", "Alphabet", "comm"),
    ("META", "Meta", "comm"), ("TSLA", "Tesla", "consumer"), ("NFLX", "Netflix", "comm"),
    ("DIS", "Disney", "comm"), ("WMT", "Walmart", "staples"), ("COST", "Costco", "staples"),
    ("PG", "Procter & Gamble", "staples"), ("HD", "Home Depot", "consumer"),
    ("MCD", "McDonald's", "consumer"), ("NKE", "Nike", "consumer"),
    ("UNH", "UnitedHealth", "health"), ("JNJ", "Johnson & Johnson", "health"),
    ("PFE", "Pfizer", "health"), ("ABBV", "AbbVie", "health"),
    ("BAC", "Bank of America", "fin"), ("GS", "Goldman Sachs", "fin"), ("V", "Visa", "fin"),
    ("CVX", "Chevron", "energy"), ("COP", "ConocoPhillips", "energy"),
    ("CAT", "Caterpillar", "industrial"), ("BA", "Boeing", "industrial"),
    ("GE", "GE Aerospace", "industrial"),
)  # fmt: skip

# The adoption rule (D-019), fixed before the measurement.
MIN_IMPROVEMENT = 0.0  # stocks: the pinball loss must be lower than with prices alone
MAX_COVERAGE_LOSS = 0.03  # stocks: the 80% interval may not drift 3 points further from 80%
MAX_ETF_DEGRADATION = 0.01  # ETF control: at most 1% more pinball loss


def panel_config(base: AgentConfig | None = None) -> AgentConfig:
    """The measurement universe: the panel stocks and the ETFs of the trading universe."""
    base = base or news_agent_config()
    etfs = tuple(a for a in base.universe if a.type is AssetType.ETF)
    stocks = tuple(Asset(s, name, AssetType.STOCK, group) for s, name, group in PANEL_STOCKS)
    return replace(base, universe=(*etfs, *stocks))


def score(
    levels: tuple[float, ...], quantiles: np.ndarray, realised: float
) -> dict[str, float | bool]:
    """One forecast against its outcome: pinball loss averaged over the quantile levels,
    whether the realised return fell inside the 80% interval, the interval width, the sign."""
    lv = np.asarray(levels, dtype=float)
    q = np.asarray(quantiles, dtype=float)
    err = realised - q
    pinball = float(np.mean(np.maximum(lv * err, (lv - 1.0) * err)))
    lo, hi = q[int(np.argmin(np.abs(lv - 0.1)))], q[int(np.argmin(np.abs(lv - 0.9)))]
    median = float(q[int(np.argmin(np.abs(lv - 0.5)))])
    return {
        "pinball": pinball,
        "inside_80": bool(lo <= realised <= hi),
        "width": float(hi - lo),
        "hit": bool(median * realised > 0),
        "realised": float(realised),
    }


def summarise(frame: pd.DataFrame) -> list[dict[str, Any]]:
    """Per variant and kind: how many forecasts, calibration, pinball loss, RMS ratio, hit rate.
    `frame` has one row per (origin, symbol, variant) with the output of `score`."""
    out = []
    for (variant, kind), g in frame.groupby(["variant", "kind"], sort=False):
        sigma = g["width"] / (2 * Z80)
        out.append(
            {
                "variant": variant,
                "kind": kind,
                "n": len(g),
                "coverage_80": float(g["inside_80"].mean()),
                "pinball": float(g["pinball"].mean()),
                "rms_ratio": float(math.sqrt((g["realised"] ** 2).mean() / (sigma**2).mean())),
                "hit_rate": float(g["hit"].mean()),
            }
        )
    return out


def paired(
    frame: pd.DataFrame,
    variant: str,
    kind: str,
    *,
    base: str = BASE,
    block: int = 5,
    draws: int = 2000,
    seed: int = 0,
) -> dict[str, Any]:
    """Pinball loss of `base` minus that of `variant`, on the same forecasts (positive = the
    covariates help). Averaged per origin, then a block bootstrap over origins: the 5-day
    windows overlap, so origins are not independent and a plain interval would be too narrow."""
    sub = frame[frame["kind"] == kind]
    a = sub[sub["variant"] == base].set_index(["origin", "symbol"])["pinball"]
    b = sub[sub["variant"] == variant].set_index(["origin", "symbol"])["pinball"]
    diff = (a - b).dropna()
    per_origin = diff.groupby(level="origin").mean().sort_index().to_numpy()
    n = len(per_origin)
    if n == 0:
        return {"variant": variant, "kind": kind, "origins": 0, "mean_diff": None,
                "relative": None, "ci95": None}  # fmt: skip
    rng = np.random.default_rng(seed)
    size = min(block, n)
    starts_max = n - size + 1
    n_blocks = math.ceil(n / size)
    means = np.empty(draws)
    for i in range(draws):
        starts = rng.integers(0, starts_max, n_blocks)
        sample = np.concatenate([per_origin[s : s + size] for s in starts])[:n]
        means[i] = sample.mean()
    mean = float(per_origin.mean())
    return {
        "variant": variant,
        "kind": kind,
        "origins": n,
        "mean_diff": mean,
        "relative": mean / float(a.mean()),
        "ci95": [float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))],
    }


def verdict(
    summary: list[dict[str, Any]], pairs: list[dict[str, Any]], variant: str
) -> dict[str, Any]:
    """The adoption rule applied to one variant. 'clear' needs the 95% interval of the stocks'
    improvement to exclude zero; 'weak' is a positive mean whose interval includes zero (the
    owner decides); anything else is 'no'."""

    def row(kind: str) -> dict[str, Any]:
        return next(s for s in summary if s["variant"] == variant and s["kind"] == kind)

    def base(kind: str) -> dict[str, Any]:
        return next(s for s in summary if s["variant"] == BASE and s["kind"] == kind)

    stock_pair = next(p for p in pairs if p["variant"] == variant and p["kind"] == "stock")
    etf_pair = next(p for p in pairs if p["variant"] == variant and p["kind"] == "etf")
    gain = stock_pair["mean_diff"]
    ci = stock_pair["ci95"]
    improves = gain is not None and gain > MIN_IMPROVEMENT
    clear = improves and ci is not None and ci[0] > 0
    drift_with = abs(row("stock")["coverage_80"] - 0.8)
    drift_without = abs(base("stock")["coverage_80"] - 0.8)
    calibrated = drift_with <= drift_without + MAX_COVERAGE_LOSS
    etf_ok = etf_pair["relative"] is None or etf_pair["relative"] >= -MAX_ETF_DEGRADATION
    ok = improves and calibrated and etf_ok
    return {
        "variant": variant,
        "improves_stocks": bool(improves),
        "interval_excludes_zero": bool(clear),
        "calibration_kept": bool(calibrated),
        "etf_control_unharmed": bool(etf_ok),
        "decision": "clear" if ok and clear else "weak" if ok else "no",
    }
