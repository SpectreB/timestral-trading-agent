"""Forecast risk (D-014): Timestral volatilities x shrunk historical correlation."""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from agent.checks.risk import (
    SHRINKAGE,
    Z80,
    correlation,
    covariance,
    forecast_vols,
    portfolio_vol,
    risk_contributions,
    risk_snapshot,
)
from agent.inputs.forecast import forecast_assets
from tests.helpers import BASELINE, CFG, FakeTimestral, make_closes
from tests.test_run import cycle

SPREADS = [0.01 + 0.002 * i for i in range(15)]


def test_volatility_comes_from_the_width_of_the_80_percent_interval() -> None:
    fcs = forecast_assets(make_closes(64), FakeTimestral(SPREADS), CFG)  # type: ignore[arg-type]
    vols = forecast_vols(fcs, 5)
    expected = 0.8 * SPREADS[0] * math.sqrt(5) / (2 * Z80)  # width / 2.563
    assert vols["SPY"] == pytest.approx(expected)


def test_correlation_is_shrunk_towards_the_identity() -> None:
    rng = np.random.default_rng(0)
    common = rng.normal(0, 0.01, 300)
    prices = pd.DataFrame(
        {
            "A": 100 * np.exp(np.cumsum(common)),
            "B": 100 * np.exp(np.cumsum(common)),  # identical moves: raw correlation 1
            "C": 100 * np.exp(np.cumsum(rng.normal(0, 0.01, 300))),  # independent
        }
    )
    corr = correlation(prices)
    assert corr.loc["A", "B"] == pytest.approx(1 - SHRINKAGE)
    assert abs(corr.loc["A", "C"]) < 0.2
    assert np.allclose(np.diag(corr.to_numpy()), 1.0)


def test_constant_series_get_zero_correlation_not_nan() -> None:
    prices = pd.DataFrame({"A": [100.0] * 30, "B": np.linspace(100, 110, 30)})
    corr = correlation(prices)
    assert not corr.isna().to_numpy().any()
    assert corr.loc["A", "B"] == 0.0 and corr.loc["A", "A"] == 1.0


def test_portfolio_volatility_combines_vols_and_correlation() -> None:
    corr = pd.DataFrame([[1.0, 0.5], [0.5, 1.0]], index=["A", "B"], columns=["A", "B"])
    cov = covariance({"A": 0.1, "B": 0.1}, corr)
    # 0.5^2 * 0.01 * 2 + 2 * 0.5 * 0.5 * 0.5 * 0.01 = 0.0075
    assert portfolio_vol({"A": 0.5, "B": 0.5}, cov) == pytest.approx(math.sqrt(0.0075))
    assert portfolio_vol({}, cov) == 0.0  # all cash carries no risk


def test_risk_contributions_sum_to_one_and_cash_is_zero() -> None:
    corr = pd.DataFrame([[1.0, 0.3], [0.3, 1.0]], index=["A", "B"], columns=["A", "B"])
    cov = covariance({"A": 0.2, "B": 0.05}, corr)
    shares = risk_contributions({"A": 0.3, "B": 0.6}, cov)
    assert sum(shares.values()) == pytest.approx(1.0)
    assert shares["A"] > shares["B"]  # the volatile asset dominates despite a smaller weight
    assert risk_contributions({}, cov) == {"A": 0.0, "B": 0.0}


def test_snapshot_covers_every_portfolio_and_horizon() -> None:
    closes = make_closes(300)
    fcs = forecast_assets(closes, FakeTimestral(SPREADS), CFG)  # type: ignore[arg-type]
    snap = risk_snapshot(
        fcs,
        closes,
        (1, 5),
        {"current": {}, "baseline": BASELINE, "proposed": None, "final": BASELINE},
    )
    assert set(snap["portfolio_vol"]) == {"1", "5"}
    assert set(snap["portfolio_vol"]["5"]) == {"current", "baseline", "final"}  # None skipped
    assert snap["portfolio_vol"]["5"]["current"] == 0.0
    assert snap["portfolio_vol"]["5"]["baseline"] > snap["portfolio_vol"]["1"]["baseline"]
    assert sum(snap["risk_contributions_final"].values()) == pytest.approx(1.0, abs=1e-4)
    assert snap["correlation_window"] == 252
    assert len(snap["correlation"]["matrix"]) == 15


def test_every_run_record_carries_its_forecast_risk(tmp_path: Path) -> None:
    _, _, _, store = cycle(tmp_path)
    risk = store.read_runs()[0]["risk"]
    vols = risk["portfolio_vol"]["5"]
    assert vols["current"] == 0.0 and vols["baseline"] > 0 and vols["final"] > 0
    assert "proposed" in vols
