"""Forecast wrapper: shapes, price-to-return conversion and sanity checks (offline)."""

from __future__ import annotations

from dataclasses import replace

import numpy as np
import pytest

from agent.config import AgentConfig, ForecastConfig
from agent.forecast import ForecastError, forecast_assets
from tests.helpers import CFG, FakeTimestral, make_closes

H = CFG.forecast.horizon
SPREADS = [0.01 + 0.002 * i for i in range(15)]


def run(model: object, closes=None):  # type: ignore[no-untyped-def]
    closes = make_closes(64) if closes is None else closes
    return forecast_assets(closes, model, CFG)  # type: ignore[arg-type]


def test_one_batched_call_for_the_whole_universe() -> None:
    model = FakeTimestral(SPREADS)
    fcs = run(model)
    assert len(model.calls) == 1
    context, horizon = model.calls[0]
    assert context.shape == (15, 64)
    assert horizon == H
    assert list(fcs) == list(CFG.symbols)


def test_price_quantiles_become_cumulative_returns() -> None:
    fcs = run(FakeTimestral(SPREADS, drift=0.001))
    spy = fcs["SPY"]
    assert spy.median(1) == pytest.approx(0.001)
    assert spy.median(H) == pytest.approx(0.001 * H)
    assert spy.width(1) == pytest.approx(0.8 * SPREADS[0])
    assert spy.width(H) == pytest.approx(0.8 * SPREADS[0] * np.sqrt(H))
    assert spy.quantile(1, 0.1) < spy.median(1) < spy.quantile(1, 0.9)


def test_torch_tensors_are_accepted() -> None:
    import torch

    class TorchModel(FakeTimestral):
        def forecast(self, context, prediction_length):  # type: ignore[no-untyped-def]
            out = super().forecast(context, prediction_length)
            out.quantiles = torch.from_numpy(out.quantiles)  # what the real Timestral returns
            return out

    assert run(TorchModel(SPREADS))["SPY"].width(1) == pytest.approx(0.8 * SPREADS[0])


def test_summary_is_json_friendly() -> None:
    s = run(FakeTimestral(SPREADS))["TLT"].summary((1, H))
    assert s["symbol"] == "TLT"
    assert set(s["horizons"]) == {"1", str(H)}
    assert set(s["horizons"]["1"]) == {"q10", "median", "q90", "width_80"}


def test_non_finite_forecast_is_rejected() -> None:
    class NaNModel(FakeTimestral):
        def forecast(self, context, prediction_length):  # type: ignore[no-untyped-def]
            out = super().forecast(context, prediction_length)
            out.quantiles[2, 0, 0, 5] = np.nan
            return out

    with pytest.raises(ForecastError, match="non-finite"):
        run(NaNModel(SPREADS))


def test_crossing_quantiles_are_rejected() -> None:
    class CrossingModel(FakeTimestral):
        def forecast(self, context, prediction_length):  # type: ignore[no-untyped-def]
            out = super().forecast(context, prediction_length)
            out.quantiles[0, 0, 0, :] = out.quantiles[0, 0, 0, ::-1].copy()
            return out

    with pytest.raises(ForecastError, match="not increasing"):
        run(CrossingModel(SPREADS))


@pytest.mark.parametrize("spread", [0.0, 0.5])
def test_implausible_width_is_rejected(spread: float) -> None:
    with pytest.raises(ForecastError, match="implausible"):
        run(FakeTimestral([spread] * 15))


def test_returns_input_is_not_supported_in_production() -> None:
    cfg = replace(CFG, forecast=ForecastConfig(input_type="log_return"))
    with pytest.raises(NotImplementedError):
        forecast_assets(make_closes(64), FakeTimestral(SPREADS), cfg)


def test_config_is_unchanged() -> None:
    assert AgentConfig().forecast.input_type == "price"
