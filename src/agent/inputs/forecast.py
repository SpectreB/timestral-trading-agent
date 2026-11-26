from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any, Protocol

import numpy as np
import numpy.typing as npt
import pandas as pd

from agent.config import AgentConfig

MAX_INTERVAL_WIDTH = 0.5
MONOTONE_TOLERANCE = 1e-9


class ForecastError(RuntimeError): ...


class QuantileModel(Protocol):
    def forecast(self, context: Any, prediction_length: int, **covariates: Any) -> Any: ...


@dataclass(frozen=True)
class AssetForecast:
    symbol: str
    last_date: date
    last_price: float
    levels: tuple[float, ...]
    cum_returns: npt.NDArray[np.float64]  # shape (horizon, n_levels): return from last close

    def quantile(self, h: int, level: float) -> float:
        i = int(np.argmin(np.abs(np.asarray(self.levels) - level)))
        return float(self.cum_returns[h - 1, i])

    def median(self, h: int) -> float:
        return self.quantile(h, 0.5)

    def width(self, h: int) -> float:
        return self.quantile(h, 0.9) - self.quantile(h, 0.1)

    def summary(self, horizons: tuple[int, ...]) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "last_date": self.last_date.isoformat(),
            "last_price": round(self.last_price, 4),
            "horizons": {
                str(h): {
                    "q10": round(self.quantile(h, 0.1), 5),
                    "median": round(self.median(h), 5),
                    "q90": round(self.quantile(h, 0.9), 5),
                    "width_80": round(self.width(h), 5),
                }
                for h in horizons
            },
        }


def _to_numpy(x: Any) -> npt.NDArray[np.float64]:
    if hasattr(x, "detach"):
        x = x.detach().cpu().numpy()
    return np.asarray(x, dtype=np.float64)


def load_model(model_id: str) -> QuantileModel:  # pragma: no cover - loads real weights
    from timestral import TimestralForecaster

    model: QuantileModel = TimestralForecaster.from_pretrained(model_id)
    return model


def forecast_assets(
    closes: pd.DataFrame,
    model: QuantileModel,
    cfg: AgentConfig,
    covariates: Mapping[str, Any] | None = None,
) -> dict[str, AssetForecast]:
    if cfg.forecast.input_type != "price":
        raise NotImplementedError("only price input is supported in production (D-006)")
    horizon = cfg.forecast.horizon
    context = closes.to_numpy(dtype=np.float64).T
    out = model.forecast(context, prediction_length=horizon, **(covariates or {}))

    levels = tuple(float(v) for v in _to_numpy(out.quantile_levels))
    q = _to_numpy(out.quantiles).reshape(len(closes.columns), horizon, len(levels))
    last_prices = context[:, -1]
    cum = q / last_prices[:, None, None] - 1.0

    result: dict[str, AssetForecast] = {}
    last_date = closes.index[-1].date()
    for i, symbol in enumerate(closes.columns):
        fc = AssetForecast(
            symbol=str(symbol),
            last_date=last_date,
            last_price=float(last_prices[i]),
            levels=levels,
            cum_returns=cum[i],
        )
        _sanity_check(fc, horizon)
        result[fc.symbol] = fc
    return result


def _sanity_check(fc: AssetForecast, horizon: int) -> None:
    if not np.isfinite(fc.cum_returns).all():
        raise ForecastError(f"{fc.symbol}: non-finite forecast")
    if (np.diff(fc.cum_returns, axis=1) < -MONOTONE_TOLERANCE).any():
        raise ForecastError(f"{fc.symbol}: quantiles are not increasing")
    width = fc.width(horizon)
    if not 0.0 < width < MAX_INTERVAL_WIDTH:  # also false for NaN
        raise ForecastError(f"{fc.symbol}: implausible interval width {width:.4f}")
