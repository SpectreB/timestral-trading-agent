"""Forecast scoring (D-013): observations are immutable facts, scores are derived."""

from __future__ import annotations

import csv
import math
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from agent.evaluation.scoring import WINDOW, calibration, record_observation, score_forecasts
from agent.execution.audit import AuditError, FileAuditStore
from tests.helpers import CFG, FakeMarketData, make_closes
from tests.test_decide import NOW
from tests.test_run import cycle

SYMS = list(CFG.symbols)


def run_record(d0: str = "2026-11-20", median: float = 0.01) -> dict[str, Any]:
    q = {"q10": -0.02, "median": median, "q90": 0.03, "width_80": 0.05}
    return {
        "run_id": f"{d0}_morning",
        "data": {"last_session": d0},
        "forecasts": {s: {"1": dict(q), "2": dict(q), "5": dict(q)} for s in SYMS},
    }


def observation(as_of: str, prices: dict[str, float], **overrides: float | None) -> dict[str, Any]:
    """Closes by session date: `prices` maps a date to the same close for every asset."""
    closes = {d: dict.fromkeys(SYMS, p) for d, p in prices.items()}
    for key, value in overrides.items():  # e.g. SPY_2026-11-23 is awkward: use closes below
        raise AssertionError(key, value)
    return {"as_of": as_of, "closes": closes}


OBS = observation(
    "2026-11-24",
    {"2026-11-20": 100.0, "2026-11-23": 101.0, "2026-11-24": 100.5},
)


def test_a_forecast_is_scored_against_the_closes_that_followed() -> None:
    rows = score_forecasts([run_record()], [OBS])
    by = {(r["symbol"], r["horizon"]): r for r in rows}
    h1, h2 = by[("SPY", 1)], by[("SPY", 2)]
    assert h1["realised_session"] == "2026-11-23" and h1["realised"] == pytest.approx(0.01)
    assert h2["realised_session"] == "2026-11-24" and h2["realised"] == pytest.approx(0.005)
    assert h1["inside_80"] is True and h1["direction_hit"] is True
    assert h1["abs_error_median"] == pytest.approx(0.0)
    assert h2["abs_error_median"] == pytest.approx(0.005)
    assert len(rows) == 15 * 2  # horizon 5 is not known yet


def test_outside_the_interval_and_wrong_direction() -> None:
    obs = observation("2026-11-24", {"2026-11-20": 100.0, "2026-11-23": 90.0, "2026-11-24": 90.0})
    row = next(r for r in score_forecasts([run_record()], [obs]) if r["horizon"] == 1)
    assert row["inside_80"] is False and row["direction_hit"] is False
    assert row["realised"] == pytest.approx(-0.10)


def test_a_zero_median_or_zero_move_has_no_direction() -> None:
    flat = observation(
        "2026-11-24", {"2026-11-20": 100.0, "2026-11-23": 100.0, "2026-11-24": 100.0}
    )
    assert {r["direction_hit"] for r in score_forecasts([run_record()], [flat])} == {None}
    assert {r["direction_hit"] for r in score_forecasts([run_record(median=0.0)], [OBS])} == {None}


def test_horizons_not_yet_realised_are_not_scored() -> None:
    early = observation("2026-11-23", {"2026-11-20": 100.0, "2026-11-23": 101.0})
    rows = score_forecasts([run_record()], [early])
    assert {r["horizon"] for r in rows} == {1}
    assert score_forecasts([run_record("2026-11-23")], [early]) == []  # no later session at all


def test_the_earliest_observation_containing_both_dates_is_used() -> None:
    first = observation("2026-11-23", {"2026-11-20": 100.0, "2026-11-23": 102.0})
    later = observation("2026-11-24", {"2026-11-20": 50.0, "2026-11-23": 51.0, "2026-11-24": 52.0})
    h1 = next(r for r in score_forecasts([run_record()], [later, first]) if r["horizon"] == 1)
    assert h1["realised"] == pytest.approx(0.02)  # not the later observation's basis


def test_missing_prices_are_skipped_not_guessed() -> None:
    obs = observation("2026-11-24", {"2026-11-20": 100.0, "2026-11-23": 101.0, "2026-11-24": 100.5})
    obs["closes"]["2026-11-23"]["NVDA"] = None
    rows = score_forecasts([run_record()], [obs])
    assert not [r for r in rows if r["symbol"] == "NVDA" and r["horizon"] == 1]
    assert [r for r in rows if r["symbol"] == "NVDA" and r["horizon"] == 2]


def test_calibration_summarises_by_horizon() -> None:
    wide = observation("2026-11-24", {"2026-11-20": 100.0, "2026-11-23": 101.0, "2026-11-24": 95.0})
    summary = {c["horizon"]: c for c in calibration(score_forecasts([run_record()], [wide]))}
    assert summary[1]["n"] == 15 and summary[1]["inside_80"] == 1.0
    assert summary[2]["inside_80"] == 0.0 and summary[2]["direction_hit"] == 0.0
    assert summary[2]["abs_error_median"] == pytest.approx(0.06)
    assert calibration([]) == []


def test_calibration_without_any_direction_has_no_hit_rate() -> None:
    flat = observation(
        "2026-11-24", {"2026-11-20": 100.0, "2026-11-23": 100.0, "2026-11-24": 100.0}
    )
    (first,) = calibration(score_forecasts([run_record()], [flat]))[:1]
    assert first["direction_hit"] is None


# --------------------------------------------------------------------------- observations


def market_with(rows: int = 30, **kwargs: Any) -> FakeMarketData:
    return FakeMarketData(make_closes(rows), **kwargs)


def test_the_latest_completed_window_is_recorded(tmp_path: Path) -> None:
    store = FileAuditStore(tmp_path)
    name = record_observation(store, market_with(market_open=False), CFG, NOW)
    assert name == "2026-11-20"
    (obs,) = store.read_observations()
    assert obs["as_of"] == "2026-11-20" and len(obs["closes"]) == WINDOW
    assert set(obs["closes"]["2026-11-20"]) == set(SYMS)
    assert obs["schema_version"] == 1


def test_todays_unfinished_bar_is_not_recorded(tmp_path: Path) -> None:
    store = FileAuditStore(tmp_path)
    now = NOW.replace(day=20)  # a Friday session still open at 10:00 ET
    name = record_observation(store, market_with(market_open=True), CFG, now)
    assert name == "2026-11-19"


def test_an_observation_is_recorded_once(tmp_path: Path) -> None:
    store = FileAuditStore(tmp_path)
    record_observation(store, market_with(market_open=False), CFG, NOW)
    assert record_observation(store, market_with(market_open=False), CFG, NOW) is None
    assert len(store.read_observations()) == 1


def test_missing_values_become_null_not_nan(tmp_path: Path) -> None:
    closes = make_closes(30)
    closes.iloc[-3, 0] = np.nan
    store = FileAuditStore(tmp_path)
    record_observation(store, FakeMarketData(closes, market_open=False), CFG, NOW)
    (obs,) = store.read_observations()
    assert obs["closes"][closes.index[-3].date().isoformat()][SYMS[0]] is None
    assert not math.isnan(obs["closes"]["2026-11-20"][SYMS[0]])


def test_observation_files_are_immutable(tmp_path: Path) -> None:
    store = FileAuditStore(tmp_path)
    store.write_observation("2026-11-20", {"as_of": "2026-11-20", "closes": {}})
    with pytest.raises(AuditError, match="immutable"):
        store.write_observation("2026-11-20", {"as_of": "2026-11-20", "closes": {}})


def test_scores_are_derived_into_a_table(tmp_path: Path) -> None:
    _, _, _, store = cycle(tmp_path)
    dates = ["2026-11-20", "2026-11-23", "2026-11-24"]
    store.write_observation(
        "2026-11-24",
        {"as_of": "2026-11-24", "closes": {d: dict.fromkeys(SYMS, 100.0) for d in dates}},
    )
    store.build_tables()
    with (tmp_path / "tables" / "forecast_scores.csv").open(newline="") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 15 * 2
    assert {r["horizon"] for r in rows} == {"1", "2"}
    assert rows[0]["direction_hit"] == ""  # a flat market has no direction
