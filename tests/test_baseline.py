"""Baseline rule: inverse forecast width, always within the risk invariants (D-004)."""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from agent.checks.safety import final_check
from agent.decision.baseline import baseline_weights
from agent.inputs.forecast import forecast_assets
from tests.helpers import CFG, FakeTimestral, make_closes


def baseline_for(spreads: list[float]) -> dict[str, float]:
    fcs = forecast_assets(make_closes(64), FakeTimestral(spreads), CFG)  # type: ignore[arg-type]
    return baseline_weights(fcs, CFG)


def test_equal_uncertainty_gives_equal_weights() -> None:
    w = baseline_for([0.02] * 15)
    assert all(v == pytest.approx(0.06) for v in w.values())
    final_check(w, 1 - sum(w.values()), CFG)


def test_more_uncertain_assets_get_less_weight() -> None:
    spreads = [0.02] * 15
    spreads[CFG.symbols.index("NVDA")] = 0.04  # twice as uncertain
    w = baseline_for(spreads)
    assert w["NVDA"] == pytest.approx(w["SPY"] / 2)


def test_caps_bind_and_the_excess_goes_to_cash() -> None:
    spreads = [0.05] * 15
    spreads[CFG.symbols.index("TLT")] = 0.002  # extremely certain: would take most weight
    w = baseline_for(spreads)
    assert w["TLT"] == pytest.approx(CFG.risk.max_weight_etf)
    assert sum(w.values()) < CFG.risk.max_invested
    final_check(w, 1 - sum(w.values()), CFG)


def test_group_cap_binds() -> None:
    spreads = [0.05] * 15
    for s in ("XLE", "XOM"):
        spreads[CFG.symbols.index(s)] = 0.004
    w = baseline_for(spreads)
    assert w["XLE"] + w["XOM"] == pytest.approx(CFG.risk.max_weight_group)


def test_incomplete_forecasts_are_a_bug() -> None:
    fcs = forecast_assets(make_closes(64), FakeTimestral([0.02] * 15), CFG)  # type: ignore[arg-type]
    del fcs["SPY"]
    with pytest.raises(ValueError, match="every asset"):
        baseline_weights(fcs, CFG)


@settings(max_examples=500, deadline=None)
@given(st.lists(st.floats(0.001, 0.08), min_size=15, max_size=15))
def test_baseline_always_passes_the_final_check(spreads: list[float]) -> None:
    w = baseline_for(spreads)
    final_check(w, 1 - sum(w.values()), CFG)
