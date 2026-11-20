"""The independent final check must catch every invariant violation on its own (D-009)."""

from __future__ import annotations

import math

import pytest

from agent.safety import UnsafePortfolioError, final_check
from tests.helpers import BASELINE, CFG


def test_valid_portfolio_passes() -> None:
    final_check(BASELINE, 0.10, CFG)


def test_all_cash_passes() -> None:
    final_check({}, 1.0, CFG)


@pytest.mark.parametrize(
    ("changes", "cash", "fragment"),
    [
        ({"TSLA": 0.01}, 0.09, "not in the universe"),
        ({"SPY": math.nan}, 0.10, "not a finite number"),
        ({"SPY": -0.02}, 0.18, "short position"),
        ({"SPY": 0.26, "IWM": 0.0, "EFA": 0.0, "EEM": 0.0, "XLE": 0.0}, 0.04, "above its cap"),
        ({"NVDA": 0.11, "SPY": 0.01}, 0.14, "above its cap"),
        (
            {"XLE": 0.25, "XOM": 0.10, "SPY": 0.0, "IWM": 0.0, "EFA": 0.0, "EEM": 0.0},
            0.12,
            "group energy",
        ),
        ({"SPY": 0.12}, 0.04, "cash floor breached"),
        ({}, 0.05, "below the 10% floor"),
        ({}, math.nan, "below the 10% floor"),
        ({}, 0.20, "not 100%"),
    ],
)
def test_each_violation_is_caught(changes: dict[str, float], cash: float, fragment: str) -> None:
    weights = {**BASELINE, **changes}
    with pytest.raises(UnsafePortfolioError) as exc:
        final_check(weights, cash, CFG)
    assert any(fragment in v for v in exc.value.violations), exc.value.violations
