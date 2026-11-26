"""Market data: daily closes for the universe, completed bars only, validated.

The `MarketData` protocol keeps the pipeline independent of Alpaca: tests use a fake
source (no network, D-009), production uses `AlpacaMarketData`.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

import pandas as pd

from agent.config import AgentConfig

NEW_YORK = ZoneInfo("America/New_York")
# Trading days per calendar day is ~252/365; ask for comfortably more history than needed.
CALENDAR_DAYS_PER_TRADING_DAY = 1.6
MAX_MISSING_SHARE = 0.05  # per symbol, within the context window
MAX_STALENESS_DAYS = 5  # last completed bar older than this (calendar days) means a data problem


class DataError(RuntimeError):
    """The market data is not fit for forecasting: the run must not trade."""


class MarketData(Protocol):
    def daily_closes(self, symbols: Sequence[str], start: datetime, end: datetime) -> pd.DataFrame:
        """Adjusted daily closes, one column per symbol, indexed by session date."""
        ...

    def is_market_open(self) -> bool: ...


def completed_closes(closes: pd.DataFrame, today: date, market_open: bool) -> pd.DataFrame:
    """Drop today's bar while the session is still running: it is not a close yet."""
    if market_open:
        return closes[closes.index < pd.Timestamp(today)]
    return closes


def load_context(source: MarketData, cfg: AgentConfig, now: datetime) -> pd.DataFrame:
    """Return the last `context_length` completed closes for the universe, validated.

    Raises DataError if a symbol is missing, history is too short, the latest bar is
    incomplete for any symbol, too many values are missing, or the data is stale.
    """
    symbols = list(cfg.symbols)
    n = cfg.forecast.context_length
    end = now - timedelta(minutes=16)  # free data plan: consolidated feed delayed by 15 min
    start = end - timedelta(days=math.ceil(n * CALENDAR_DAYS_PER_TRADING_DAY) + 30)

    raw = source.daily_closes(symbols, start, end)
    today = now.astimezone(NEW_YORK).date()
    closes = completed_closes(raw, today, source.is_market_open())

    missing = [s for s in symbols if s not in closes.columns]
    if missing:
        raise DataError(f"no data for {missing}")
    closes = closes[symbols].sort_index()
    if len(closes) < n:
        raise DataError(f"only {len(closes)} completed bars, {n} needed")
    closes = closes.iloc[-n:]

    last = closes.iloc[-1]
    if last.isna().any():
        raise DataError(f"latest bar missing for {list(last[last.isna()].index)}")
    share = closes.isna().mean()
    if (share > MAX_MISSING_SHARE).any():
        raise DataError(
            f"too many missing closes for {list(share[share > MAX_MISSING_SHARE].index)}"
        )
    if (closes <= 0).any().any():
        raise DataError("non-positive prices")

    last_date = closes.index[-1].date()
    if (today - last_date).days > MAX_STALENESS_DAYS:
        raise DataError(f"stale data: latest completed bar is {last_date}")
    return closes


class AlpacaMarketData:  # pragma: no cover - network adapter, exercised by the smoke test
    """Alpaca implementation: consolidated (SIP) feed, prices adjusted for splits and
    dividends (without adjustment a 10:1 split looks like a -90% crash)."""

    def __init__(self, api_key: str, secret_key: str) -> None:
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.trading.client import TradingClient

        self._data = StockHistoricalDataClient(api_key, secret_key)
        self._trading = TradingClient(api_key, secret_key, paper=True)

    def daily_closes(self, symbols: Sequence[str], start: datetime, end: datetime) -> pd.DataFrame:
        from alpaca.data.enums import Adjustment, DataFeed
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame

        request = StockBarsRequest(
            symbol_or_symbols=list(symbols),
            timeframe=TimeFrame.Day,
            start=start.astimezone(UTC),
            end=end.astimezone(UTC),
            adjustment=Adjustment.ALL,
            feed=DataFeed.SIP,
        )
        from alpaca.data.models import BarSet

        barset = self._data.get_stock_bars(request)
        if not isinstance(barset, BarSet):  # raw_data mode returns a plain dict
            raise DataError("unexpected raw response from Alpaca market data")
        closes = barset.df["close"].unstack(level="symbol")
        # Bars are stamped at midnight New York time: keep the session date only.
        closes.index = pd.DatetimeIndex([ts.tz_convert(NEW_YORK).date() for ts in closes.index])
        return closes

    def is_market_open(self) -> bool:
        from alpaca.trading.models import Clock

        clock = self._trading.get_clock()
        if not isinstance(clock, Clock):  # raw_data mode returns a plain dict
            raise DataError("unexpected raw response from the Alpaca clock")
        return bool(clock.is_open)
