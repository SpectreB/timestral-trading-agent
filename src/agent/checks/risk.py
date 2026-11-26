"""Forecast portfolio risk (D-014): Timestral volatilities combined with historical correlations.

Timestral forecasts each asset separately: its quantiles describe a *marginal* distribution,
not a joint one, so it cannot give a covariance matrix on its own. The standard way to combine
the two pieces of information:

    covariance = D * R * D

- D (diagonal): forward-looking volatility of each asset from Timestral, at a given horizon.
  The 80% interval of a normal distribution spans 2 x 1.2816 standard deviations, so
  sigma ≈ (q90 - q10) / 2.563. This is the part Timestral does well (see the D-006 backtest).
- R: correlation of daily log returns over the last year, shrunk towards the identity to be
  stable with 15 assets and ~250 observations.

From it: the forecast volatility of any portfolio (cash has no risk) and each asset's share of
that risk. These numbers are logged with every run so the forecast risk can later be compared
with what happened. In a later version the same functions become a tool the LLM can call
(D-004, option D).
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

import numpy as np
import pandas as pd

from agent.inputs.forecast import AssetForecast

Z80 = 1.2815515655446004  # standard normal quantile at 0.90
CORRELATION_WINDOW = 252  # sessions, about one year
SHRINKAGE = 0.2  # weight of the identity in the shrunk correlation


def forecast_vols(forecasts: Mapping[str, AssetForecast], horizon: int) -> dict[str, float]:
    """Standard deviation of each asset's cumulative return at `horizon`, from its 80% interval."""
    return {s: f.width(horizon) / (2 * Z80) for s, f in forecasts.items()}


def correlation(
    closes: pd.DataFrame, window: int = CORRELATION_WINDOW, shrinkage: float = SHRINKAGE
) -> pd.DataFrame:
    """Shrunk correlation of daily log returns over the last `window` sessions."""
    log_prices = pd.DataFrame(np.log(closes.to_numpy(dtype=float)), columns=closes.columns)
    returns = log_prices.diff().iloc[-window:]
    raw = returns.corr().fillna(0.0).to_numpy(copy=True)  # pandas 3 views are read-only
    np.fill_diagonal(raw, 1.0)  # a constant series has no defined correlation, not zero variance
    shrunk = (1 - shrinkage) * raw + shrinkage * np.eye(len(raw))
    return pd.DataFrame(shrunk, index=closes.columns, columns=closes.columns)


def covariance(vols: Mapping[str, float], corr: pd.DataFrame) -> pd.DataFrame:
    sigma = np.array([vols[s] for s in corr.columns])
    return pd.DataFrame(
        np.outer(sigma, sigma) * corr.to_numpy(), index=corr.index, columns=corr.columns
    )


def _vector(weights: Mapping[str, float], cov: pd.DataFrame) -> np.ndarray:
    return np.array([weights.get(s, 0.0) for s in cov.columns])


def portfolio_vol(weights: Mapping[str, float], cov: pd.DataFrame) -> float:
    w = _vector(weights, cov)
    return math.sqrt(max(float(w @ cov.to_numpy() @ w), 0.0))


def risk_contributions(weights: Mapping[str, float], cov: pd.DataFrame) -> dict[str, float]:
    """Each asset's share of the portfolio's forecast variance (shares sum to 1)."""
    w = _vector(weights, cov)
    variance = float(w @ cov.to_numpy() @ w)
    if variance <= 0:
        return {s: 0.0 for s in cov.columns}
    marginal = cov.to_numpy() @ w
    return {s: float(w[i] * marginal[i] / variance) for i, s in enumerate(cov.columns)}


def risk_snapshot(
    forecasts: Mapping[str, AssetForecast],
    closes: pd.DataFrame,
    horizons: tuple[int, ...],
    portfolios: Mapping[str, Mapping[str, float] | None],
) -> dict[str, Any]:
    """Everything the audit record keeps about forecast risk for one run."""
    corr = correlation(closes)
    vols: dict[str, dict[str, float]] = {}
    pvols: dict[str, dict[str, float]] = {}
    contributions: dict[str, float] = {}
    for h in horizons:
        v = forecast_vols(forecasts, h)
        cov = covariance(v, corr)
        vols[str(h)] = {s: round(x, 6) for s, x in v.items()}
        pvols[str(h)] = {
            name: round(portfolio_vol(w, cov), 6) for name, w in portfolios.items() if w is not None
        }
        final = portfolios.get("final")
        if h == max(horizons) and final is not None:
            contributions = {s: round(x, 6) for s, x in risk_contributions(final, cov).items()}
    return {
        "method": "timestral_vol_x_shrunk_historical_correlation",
        "correlation_window": min(CORRELATION_WINDOW, len(closes) - 1),
        "shrinkage": SHRINKAGE,
        "vols": vols,
        "portfolio_vol": pvols,
        "risk_contributions_final": contributions,
        "correlation": {
            "symbols": list(corr.columns),
            "matrix": [[round(float(x), 4) for x in row] for row in corr.to_numpy()],
        },
    }
