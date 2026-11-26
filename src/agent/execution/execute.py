"""Execution stage: target weights -> marketable limit orders (D-010). Safeguards: paper-account
check, idempotent client order ids, TRADING_ENABLED kill switch."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol, cast

from pydantic import Field, ValidationError

from agent.checks.safety import UnsafePortfolioError, final_check
from agent.config import AgentConfig, Frozen
from agent.execution.audit import OrderRecord
from agent.execution.reconcile import OrderState

MIN_NOTIONAL = 1.0  # Alpaca minimum for fractional orders, in USD
QTY_DECIMALS = 4


class ExecutionError(RuntimeError): ...


class AccountSnapshot(Frozen):
    equity: float
    is_paper: bool
    status: str
    trading_blocked: bool


class Position(Frozen):
    quantity: float
    market_value: float


class Broker(Protocol):
    def account(self) -> AccountSnapshot: ...
    def positions(self) -> dict[str, Position]: ...
    def latest_prices(self, symbols: Sequence[str]) -> dict[str, float]: ...
    def is_market_open(self) -> bool: ...
    def submit_limit_order(
        self,
        symbol: str,
        side: Literal["buy", "sell"],
        quantity: float,
        limit_price: float,
        client_order_id: str,
    ) -> str: ...


def trading_enabled(env: Mapping[str, str]) -> bool:
    return env.get("TRADING_ENABLED", "").strip().lower() == "true"


def preflight(broker: Broker) -> AccountSnapshot:
    try:
        account = broker.account()
    except ValidationError as error:
        raise ExecutionError(f"invalid account data from the broker: {error}") from error
    problems = []
    if not account.is_paper:
        problems.append("not a paper account")
    if account.status != "ACTIVE":
        problems.append(f"account status is {account.status}")
    if account.trading_blocked:
        problems.append("trading is blocked on the account")
    if not math.isfinite(account.equity) or account.equity <= 0:
        problems.append(f"invalid equity {account.equity}")
    if problems:
        raise ExecutionError("; ".join(problems))
    return account


class PlannedOrder(Frozen):
    symbol: str = Field(min_length=1)
    side: Literal["buy", "sell"]
    quantity: float = Field(gt=0)
    limit_price: float = Field(gt=0)
    client_order_id: str = Field(min_length=1, max_length=128)
    reference_price: float = Field(gt=0)


@dataclass(frozen=True)
class Plan:
    orders: list[PlannedOrder]
    post_weights: dict[str, float]
    post_cash: float
    turnover: float
    scale: float


def plan_orders(
    target: Mapping[str, float],
    positions: Mapping[str, Position],
    equity: float,
    prices: Mapping[str, float],
    cfg: AgentConfig,
    rid: str,
) -> Plan:
    ex = cfg.execution
    symbols = cfg.symbols
    current_value = {s: positions[s].market_value if s in positions else 0.0 for s in symbols}
    deltas = {s: target[s] * equity - current_value[s] for s in symbols}
    initial_deployment = sum(current_value.values()) / equity <= ex.initial_deployment_max_invested

    last_error = ""
    # If the plan within the band is unsafe (a buy kept, the sells that fund it skipped), the
    # band is relaxed step by step rather than dropped at once, so the fallback does not turn
    # every few-dollar difference into an order.
    b = ex.no_trade_band
    for band in dict.fromkeys((b, b / 2, b / 4, 0.0)):
        threshold = max(band - ex.band_tolerance, 0.0) * equity
        trades = {
            s: d
            for s, d in deltas.items()
            if abs(d) >= MIN_NOTIONAL and (abs(d) >= threshold or target[s] == 0)
        }
        turnover = sum(abs(d) for d in trades.values()) / equity
        cap = 1.0 if initial_deployment else ex.max_turnover_per_run
        scale = 1.0 if turnover <= cap else cap / turnover
        post_value = {s: current_value[s] + scale * trades.get(s, 0.0) for s in symbols}
        post_weights = {s: v / equity for s, v in post_value.items()}
        post_cash = 1.0 - sum(post_weights.values())
        try:
            final_check(post_weights, post_cash, cfg)
        except UnsafePortfolioError as error:
            last_error = str(error)
            continue
        orders = _build_orders(trades, scale, target, positions, prices, cfg, rid)
        return Plan(orders, post_weights, post_cash, turnover, scale)
    raise ExecutionError(f"no safe plan: {last_error}")


def _build_orders(
    trades: Mapping[str, float],
    scale: float,
    target: Mapping[str, float],
    positions: Mapping[str, Position],
    prices: Mapping[str, float],
    cfg: AgentConfig,
    rid: str,
) -> list[PlannedOrder]:
    tolerance = cfg.execution.limit_price_tolerance
    orders: list[PlannedOrder] = []
    for symbol, delta in trades.items():
        ref = prices.get(symbol, math.nan)
        if not math.isfinite(ref) or ref <= 0:
            raise ExecutionError(f"{symbol}: no valid reference price ({ref})")
        side: Literal["buy", "sell"] = "buy" if delta > 0 else "sell"
        if side == "sell" and scale == 1.0 and target[symbol] == 0:
            quantity = positions[symbol].quantity  # exit fully, leave no dust
        else:
            quantity = round(abs(delta) * scale / ref, QTY_DECIMALS)
            if side == "sell":
                quantity = min(quantity, positions[symbol].quantity)
        if quantity <= 0 or quantity * ref < MIN_NOTIONAL:
            continue
        limit = ref * (1 + tolerance) if side == "buy" else ref * (1 - tolerance)
        orders.append(
            PlannedOrder(
                symbol=symbol,
                side=side,
                quantity=quantity,
                limit_price=round(limit, 2),
                client_order_id=f"{rid}-{symbol}-{side}",
                reference_price=ref,
            )
        )
    return sorted(orders, key=lambda o: (o.side == "buy", o.symbol))


def order_records(plan: Plan, status: str) -> list[OrderRecord]:
    return [
        OrderRecord(
            symbol=o.symbol,
            side=o.side,
            quantity=o.quantity,
            limit_price=o.limit_price,
            client_order_id=o.client_order_id,
            status=status,
            reference_price=o.reference_price,
        )
        for o in plan.orders
    ]


def submit_orders(plan: Plan, broker: Broker) -> list[OrderRecord]:
    records: list[OrderRecord] = []
    sell_failed = False
    for o in plan.orders:
        if o.side == "buy" and sell_failed:
            status = "skipped_after_sell_failure"
        else:
            try:
                status = broker.submit_limit_order(
                    o.symbol, o.side, o.quantity, o.limit_price, o.client_order_id
                )
            except Exception as error:  # broker errors are data, not crashes
                status = f"error: {type(error).__name__}: {str(error)[:150]}"
                sell_failed = sell_failed or o.side == "sell"
        records.append(
            OrderRecord(
                symbol=o.symbol,
                side=o.side,
                quantity=o.quantity,
                limit_price=o.limit_price,
                client_order_id=o.client_order_id,
                status=status,
                reference_price=o.reference_price,
            )
        )
    return records


class AlpacaBroker:  # pragma: no cover - network adapter, exercised by the smoke test
    def __init__(self, api_key: str, secret_key: str) -> None:
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.trading.client import TradingClient

        self._trading = TradingClient(api_key, secret_key, paper=True)
        self._data = StockHistoricalDataClient(api_key, secret_key)

    def account(self) -> AccountSnapshot:
        from alpaca.trading.models import TradeAccount

        a = cast(TradeAccount, self._trading.get_account())
        return AccountSnapshot(
            equity=float(a.equity or 0),
            is_paper=str(a.account_number).startswith("PA"),
            status=str(getattr(a.status, "value", a.status)),
            trading_blocked=bool(a.trading_blocked),
        )

    def positions(self) -> dict[str, Position]:
        from alpaca.trading.models import Position as AlpacaPosition

        raw = cast(list[AlpacaPosition], self._trading.get_all_positions())
        return {
            p.symbol: Position(quantity=float(p.qty), market_value=float(p.market_value or 0))
            for p in raw
        }

    def latest_prices(self, symbols: Sequence[str]) -> dict[str, float]:
        from alpaca.data.enums import DataFeed
        from alpaca.data.models import Trade
        from alpaca.data.requests import StockLatestTradeRequest

        request = StockLatestTradeRequest(symbol_or_symbols=list(symbols), feed=DataFeed.IEX)
        trades = cast(dict[str, Trade], self._data.get_stock_latest_trade(request))
        return {s: float(t.price) for s, t in trades.items()}

    def order_by_client_id(self, client_order_id: str) -> OrderState | None:
        from alpaca.common.exceptions import APIError
        from alpaca.trading.models import Order

        try:
            order = cast(Order, self._trading.get_order_by_client_id(client_order_id))
        except APIError as error:
            if error.status_code == 404:
                return None
            raise
        return OrderState(
            status=str(getattr(order.status, "value", order.status)),
            filled_qty=float(order.filled_qty or 0),
            filled_avg_price=float(order.filled_avg_price) if order.filled_avg_price else None,
            submitted_at=order.submitted_at.isoformat() if order.submitted_at else None,
            filled_at=order.filled_at.isoformat() if order.filled_at else None,
        )

    def is_market_open(self) -> bool:
        from alpaca.trading.models import Clock

        return bool(cast(Clock, self._trading.get_clock()).is_open)

    def submit_limit_order(
        self,
        symbol: str,
        side: Literal["buy", "sell"],
        quantity: float,
        limit_price: float,
        client_order_id: str,
    ) -> str:
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.models import Order
        from alpaca.trading.requests import LimitOrderRequest

        request = LimitOrderRequest(
            symbol=symbol,
            qty=quantity,
            side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
            time_in_force=TimeInForce.DAY,  # fractional orders must be day orders
            limit_price=limit_price,
            client_order_id=client_order_id,
        )
        order = cast(Order, self._trading.submit_order(request))
        return str(getattr(order.status, "value", order.status))
