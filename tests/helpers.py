"""Shared builders for guardrail tests."""

from __future__ import annotations

import json
from typing import Any

from agent.config import AgentConfig

CFG = AgentConfig()

# A valid baseline: 6% per asset = 90% invested, every cap respected.
BASELINE: dict[str, float] = {s: 0.06 for s in CFG.symbols}
ALL_CASH: dict[str, float] = {s: 0.0 for s in CFG.symbols}


def proposal(
    weights: dict[str, float] | None = None,
    cash: float | None = None,
    rationale: str = "Justified by the forecast interval.",
    confidence: float = 0.6,
    **overrides: float,
) -> dict[str, Any]:
    """Build an LLM proposal. Starts from the baseline; `overrides` change single assets."""
    w = dict(weights if weights is not None else BASELINE)
    w.update(overrides)
    if cash is None:
        cash = 1.0 - sum(w.values())
    return {
        "analysis": "Test analysis.",
        "positions": [
            {"symbol": s, "target_weight": v, "rationale": rationale, "confidence": confidence}
            for s, v in w.items()
        ],
        "cash_weight": cash,
    }


def as_json(p: dict[str, Any]) -> str:
    return json.dumps(p)


# --------------------------------------------------------------------------- offline fakes

LEVELS = (0.01, 0.05, 0.1, 0.15, 0.2, 0.25, 0.3, 0.35, 0.4, 0.45, 0.5,
          0.55, 0.6, 0.65, 0.7, 0.75, 0.8, 0.85, 0.9, 0.95, 0.99)  # fmt: skip


class FakeOutput:
    def __init__(self, quantiles: Any, levels: tuple[float, ...]) -> None:
        self.quantiles = quantiles
        self.quantile_levels = levels


class FakeTimestral:
    """Deterministic stand-in for Timestral: price quantiles widen with sqrt(h).

    `daily_spread[i]` sets the uncertainty of series i, so tests know the expected
    interval width exactly: width_80(h) = 0.8 * daily_spread * sqrt(h).
    """

    def __init__(self, daily_spread: list[float], drift: float = 0.0) -> None:
        self.daily_spread = daily_spread
        self.drift = drift
        self.calls: list[tuple[Any, int]] = []

    def forecast(self, context: Any, prediction_length: int) -> FakeOutput:
        import numpy as np

        self.calls.append((context, prediction_length))
        last = context[:, -1]
        h = np.arange(1, prediction_length + 1)
        z = np.asarray(LEVELS) - 0.5  # linear in the level: q90 - q10 = 0.8 * spread
        # A batch of another size (e.g. one portfolio series, D-018) reuses the spreads in turn.
        spreads = np.resize(np.asarray(self.daily_spread, dtype=float), len(last))
        spread = spreads[:, None, None] * np.sqrt(h)[None, :, None]
        rel = self.drift * h[None, :, None] + 2 * spread * z[None, None, :] / 2
        q = last[:, None, None] * (1 + rel)
        return FakeOutput(q[:, None, :, :], LEVELS)  # (B, V=1, H, L) like Timestral


class FakeMarketData:
    def __init__(self, closes: Any, market_open: bool = True) -> None:
        self.closes = closes
        self.market_open = market_open

    def daily_closes(self, symbols: Any, start: Any, end: Any) -> Any:
        return self.closes

    def is_market_open(self) -> bool:
        return self.market_open


def make_closes(
    n_days: int, end: str = "2026-11-20", symbols: tuple[str, ...] = CFG.symbols
) -> Any:
    """Business-day closes ending on `end` (included), gently trending per symbol."""
    import numpy as np
    import pandas as pd

    idx = pd.bdate_range(end=end, periods=n_days)
    data = {s: 100.0 * (1 + 0.0005 * i) ** np.arange(n_days) for i, s in enumerate(symbols)}
    return pd.DataFrame(data, index=idx)


class FakeBroker:
    """In-memory broker: records submitted orders, can be told to fail on given symbols."""

    def __init__(
        self,
        equity: float = 100_000.0,
        positions: dict[str, Any] | None = None,
        prices: dict[str, float] | None = None,
        market_open: bool = True,
        is_paper: bool = True,
        status: str = "ACTIVE",
        blocked: bool = False,
        fail_on: frozenset[str] = frozenset(),
    ) -> None:
        self.equity = equity
        self._positions = positions or {}
        self._prices = prices or dict.fromkeys(CFG.symbols, 100.0)
        self.market_open = market_open
        self.is_paper = is_paper
        self.status = status
        self.blocked = blocked
        self.fail_on = fail_on
        self.submitted: list[dict[str, Any]] = []
        self.states: dict[str, Any] = {}  # client_order_id -> OrderState, set by the tests

    def account(self) -> Any:
        from agent.execute import AccountSnapshot

        return AccountSnapshot(self.equity, self.is_paper, self.status, self.blocked)

    def positions(self) -> dict[str, Any]:
        return dict(self._positions)

    def latest_prices(self, symbols: Any) -> dict[str, float]:
        return {s: self._prices[s] for s in symbols}

    def is_market_open(self) -> bool:
        return self.market_open

    def order_by_client_id(self, client_order_id: str) -> Any:
        return self.states.get(client_order_id)

    def submit_limit_order(
        self, symbol: str, side: str, quantity: float, limit_price: float, client_order_id: str
    ) -> str:
        if symbol in self.fail_on:
            raise RuntimeError("insufficient buying power")
        self.submitted.append(
            {"symbol": symbol, "side": side, "quantity": quantity, "limit": limit_price,
             "id": client_order_id}
        )  # fmt: skip
        return "accepted"


def positions_for(
    weights: dict[str, float], equity: float = 100_000.0, price: float = 100.0
) -> Any:
    """Positions worth `weight * equity` each, at a constant price."""
    from agent.execute import Position

    return {
        s: Position(quantity=w * equity / price, market_value=w * equity)
        for s, w in weights.items()
        if w > 0
    }
