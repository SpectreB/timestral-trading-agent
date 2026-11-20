"""Market data loading: completed bars only, and every data problem stops the run."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np
import pytest

from agent.data import DataError, completed_closes, load_context
from tests.helpers import CFG, FakeMarketData, make_closes

NY = ZoneInfo("America/New_York")
MORNING = datetime(2026, 11, 20, 10, 0, tzinfo=NY)  # Friday 10:00 ET, market open
N = CFG.forecast.context_length


def test_todays_bar_is_dropped_while_the_market_is_open() -> None:
    closes = make_closes(N + 10)
    ctx = load_context(FakeMarketData(closes, market_open=True), CFG, MORNING)
    assert ctx.index[-1].date().isoformat() == "2026-11-19"
    assert len(ctx) == N
    assert list(ctx.columns) == list(CFG.symbols)


def test_todays_bar_is_kept_after_the_close() -> None:
    closes = make_closes(N + 10)
    evening = datetime(2026, 11, 20, 17, 0, tzinfo=NY)
    ctx = load_context(FakeMarketData(closes, market_open=False), CFG, evening)
    assert ctx.index[-1].date().isoformat() == "2026-11-20"


def test_completed_closes_is_a_noop_without_todays_bar() -> None:
    closes = make_closes(5, end="2026-11-19")
    assert completed_closes(closes, MORNING.date(), market_open=True).equals(closes)


def test_missing_symbol_stops_the_run() -> None:
    closes = make_closes(N + 10).drop(columns=["NVDA"])
    with pytest.raises(DataError, match="NVDA"):
        load_context(FakeMarketData(closes), CFG, MORNING)


def test_short_history_stops_the_run() -> None:
    with pytest.raises(DataError, match="completed bars"):
        load_context(FakeMarketData(make_closes(N - 5)), CFG, MORNING)


def test_missing_latest_close_stops_the_run() -> None:
    closes = make_closes(N + 10)
    closes.loc[closes.index[-2], "TLT"] = np.nan  # yesterday's close, the last completed one
    with pytest.raises(DataError, match="latest bar missing"):
        load_context(FakeMarketData(closes), CFG, MORNING)


def test_sporadic_gaps_are_tolerated_but_not_many() -> None:
    closes = make_closes(N + 10)
    closes.iloc[100:105, 0] = np.nan  # 5 missing days: fine, Timestral treats NaN as missing
    load_context(FakeMarketData(closes), CFG, MORNING)
    closes.iloc[100:200, 0] = np.nan  # ~20% missing: not fine
    with pytest.raises(DataError, match="too many missing"):
        load_context(FakeMarketData(closes), CFG, MORNING)


def test_non_positive_price_stops_the_run() -> None:
    closes = make_closes(N + 10)
    closes.iloc[50, 3] = 0.0
    with pytest.raises(DataError, match="non-positive"):
        load_context(FakeMarketData(closes), CFG, MORNING)


def test_stale_data_stops_the_run() -> None:
    closes = make_closes(N + 10, end="2026-11-06")
    with pytest.raises(DataError, match="stale"):
        load_context(FakeMarketData(closes), CFG, MORNING)
