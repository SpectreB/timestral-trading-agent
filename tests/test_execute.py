"""Execution: preflight, order planning, safe submission (D-010, P0 safeguards)."""

from __future__ import annotations

import pytest
from hypothesis import given, settings

from agent.execute import (
    ExecutionError,
    Position,
    order_records,
    plan_orders,
    preflight,
    submit_orders,
    trading_enabled,
)
from agent.safety import final_check
from tests.helpers import BASELINE, CFG, FakeBroker, positions_for
from tests.test_guardrails_properties import valid_portfolio

EQUITY = 100_000.0
PRICES = dict.fromkeys(CFG.symbols, 100.0)
RID = "2026-11-23_morning"


def plan(target, current=None, prices=None, equity=EQUITY):  # type: ignore[no-untyped-def]
    positions = positions_for(current or {}, equity)
    return plan_orders(target, positions, equity, prices or PRICES, CFG, RID)


# --------------------------------------------------------------------------- safeguards


@pytest.mark.parametrize(
    ("broker", "fragment"),
    [
        (FakeBroker(is_paper=False), "not a paper account"),
        (FakeBroker(status="ACCOUNT_UPDATED"), "account status"),
        (FakeBroker(blocked=True), "blocked"),
        (FakeBroker(equity=0.0), "invalid equity"),
        (FakeBroker(equity=float("nan")), "invalid equity"),
    ],
)
def test_preflight_refuses_unsafe_accounts(broker: FakeBroker, fragment: str) -> None:
    with pytest.raises(ExecutionError, match=fragment):
        preflight(broker)


def test_preflight_accepts_an_active_paper_account() -> None:
    assert preflight(FakeBroker()).equity == EQUITY


@pytest.mark.parametrize(
    ("env", "enabled"),
    [({}, False), ({"TRADING_ENABLED": ""}, False), ({"TRADING_ENABLED": "no"}, False),
     ({"TRADING_ENABLED": "true"}, True), ({"TRADING_ENABLED": " TRUE "}, True)],
)  # fmt: skip
def test_kill_switch(env: dict[str, str], enabled: bool) -> None:
    assert trading_enabled(env) is enabled


# --------------------------------------------------------------------------- planning


def test_the_first_deployment_from_cash_is_not_throttled() -> None:
    p = plan(BASELINE)  # from all cash to 90% invested in one run
    assert p.turnover == pytest.approx(0.90)
    assert p.scale == 1.0
    assert len(p.orders) == 15
    assert all(o.side == "buy" for o in p.orders)
    assert all(o.quantity == pytest.approx(60.0) for o in p.orders)  # 6% of 100k at $100
    assert sum(p.post_weights.values()) == pytest.approx(0.90)
    final_check(p.post_weights, p.post_cash, CFG)


def test_later_large_changes_are_throttled_by_the_turnover_cap() -> None:
    # 10% invested (above the 5% "from cash" threshold): rotating into the baseline is capped.
    current = {"TLT": 0.10}
    p = plan(BASELINE, current=current)
    assert p.turnover == pytest.approx(0.04 + 0.84)
    assert p.scale == pytest.approx(0.30 / 0.88)
    final_check(p.post_weights, p.post_cash, CFG)


def test_a_small_residual_position_still_counts_as_starting_from_cash() -> None:
    p = plan(BASELINE, current={"TLT": 0.04})  # 4% invested: still the initial deployment
    assert p.scale == 1.0


def test_orders_are_marketable_limits_at_the_configured_tolerance() -> None:
    p = plan({**dict.fromkeys(CFG.symbols, 0.0), "SPY": 0.06}, current={"TLT": 0.06})
    sell = next(o for o in p.orders if o.side == "sell")
    buy = next(o for o in p.orders if o.side == "buy")
    assert sell.limit_price == pytest.approx(99.70)  # 0.3% below the reference price
    assert buy.limit_price == pytest.approx(100.30)  # 0.3% above


def test_sells_come_before_buys() -> None:
    p = plan({**dict.fromkeys(CFG.symbols, 0.0), "SPY": 0.06}, current={"TLT": 0.06, "GLD": 0.03})
    sides = [o.side for o in p.orders]
    assert sides == sorted(sides, key=lambda s: s == "buy")
    assert sides[0] == "sell" and sides[-1] == "buy"


def test_client_order_ids_are_deterministic() -> None:
    first = plan(BASELINE)
    again = plan(BASELINE)
    assert [o.client_order_id for o in first.orders] == [o.client_order_id for o in again.orders]
    assert first.orders[0].client_order_id == f"{RID}-{first.orders[0].symbol}-buy"
    assert len({o.client_order_id for o in first.orders}) == 15


def test_small_moves_inside_the_band_are_not_traded() -> None:
    current = {**BASELINE, "SPY": 0.07, "TLT": 0.05}  # 1 pt away from the target, band is 2 pts
    p = plan(BASELINE, current=current)
    assert p.orders == []
    assert p.scale == 1.0


def test_band_is_relaxed_when_it_would_leave_an_invariant_broken() -> None:
    # NVDA drifted above its 10% cap; the 1-pt correction is inside the band but must happen.
    current = {**BASELINE, "NVDA": 0.11, "SPY": 0.01}
    target = {**BASELINE, "NVDA": 0.10, "SPY": 0.02}
    p = plan(target, current=current)
    assert {o.symbol for o in p.orders} == {"NVDA", "SPY"}
    assert p.post_weights["NVDA"] == pytest.approx(0.10)


def test_exit_sells_the_whole_position_without_dust() -> None:
    positions = positions_for(BASELINE)
    positions["NVDA"] = Position(quantity=59.9999, market_value=6000.0)
    p = plan_orders({**BASELINE, "NVDA": 0.0}, positions, EQUITY, PRICES, CFG, RID)
    (order,) = p.orders
    assert (order.symbol, order.side, order.quantity) == ("NVDA", "sell", 59.9999)


def test_partial_sell_never_exceeds_the_position() -> None:
    positions = positions_for(BASELINE)
    positions["SPY"] = Position(quantity=10.0, market_value=6000.0)  # stale quantity
    p = plan_orders({**BASELINE, "SPY": 0.0, "QQQ": 0.06}, positions, EQUITY, PRICES, CFG, RID)
    sell = next(o for o in p.orders if o.symbol == "SPY")
    assert sell.quantity <= 10.0


def test_dust_below_the_minimum_notional_is_ignored() -> None:
    positions = positions_for(BASELINE)
    positions["NVDA"] = Position(quantity=0.005, market_value=0.5)
    p = plan_orders({**BASELINE, "NVDA": 0.0}, positions, EQUITY, PRICES, CFG, RID)
    assert all(o.symbol != "NVDA" for o in p.orders)


def test_a_sell_without_any_quantity_is_dropped() -> None:
    positions = positions_for(BASELINE)
    positions["SPY"] = Position(quantity=0.0, market_value=6000.0)  # stale: nothing to sell
    p = plan_orders({**BASELINE, "SPY": 0.0}, positions, EQUITY, PRICES, CFG, RID)
    assert all(o.symbol != "SPY" for o in p.orders)


@pytest.mark.parametrize("bad", [0.0, -5.0, float("nan")])
def test_missing_or_invalid_price_stops_the_run(bad: float) -> None:
    with pytest.raises(ExecutionError, match="reference price"):
        plan(BASELINE, prices={**PRICES, "SPY": bad})


def test_missing_price_entry_stops_the_run() -> None:
    prices = {s: p for s, p in PRICES.items() if s != "SPY"}
    with pytest.raises(ExecutionError, match="SPY"):
        plan(BASELINE, prices=prices)


def test_a_plan_that_leaves_the_portfolio_unsafe_is_refused() -> None:
    # Held and targeted: NVDA at 20%, twice its cap. No trade is planned, so nothing fixes it.
    unsafe = {**BASELINE, "NVDA": 0.20, "SPY": 0.0, "IWM": 0.0}
    with pytest.raises(ExecutionError, match="no safe plan"):
        plan(unsafe, current=unsafe)


def test_a_throttled_plan_stays_safe_even_for_an_unsafe_target() -> None:
    # Already invested: the turnover cap only moves 30% of equity towards the target.
    p = plan({**BASELINE, "SPY": 0.20}, current={"TLT": 0.10})
    final_check(p.post_weights, p.post_cash, CFG)


def test_an_unsafe_target_from_cash_is_refused_outright() -> None:
    # No cap on the first deployment, so nothing dilutes an unsafe target: the plan is refused.
    with pytest.raises(ExecutionError, match="no safe plan"):
        plan({**BASELINE, "SPY": 0.20})


@settings(max_examples=400, deadline=None)
@given(valid_portfolio(), valid_portfolio())
def test_any_plan_is_safe_and_well_formed(target: dict[str, float], current: dict[str, float]):  # type: ignore[no-untyped-def]
    try:
        p = plan(target, current=current)
    except ExecutionError:
        return  # refusing is always acceptable; trading unsafely never is
    final_check(p.post_weights, p.post_cash, CFG)
    sides = [o.side for o in p.orders]
    assert sides == sorted(sides, key=lambda s: s == "buy")
    for o in p.orders:
        assert o.quantity > 0 and o.quantity * 100.0 >= 1.0
        assert (o.limit_price > 100.0) == (o.side == "buy")
    assert 0 < p.scale <= 1.0


# --------------------------------------------------------------------------- submission


def test_records_carry_the_status_given() -> None:
    p = plan(BASELINE)
    records = order_records(p, "dry_run")
    assert len(records) == 15 and {r.status for r in records} == {"dry_run"}
    assert records[0].client_order_id == p.orders[0].client_order_id


def test_submission_sends_sells_before_buys() -> None:
    broker = FakeBroker()
    p = plan({**dict.fromkeys(CFG.symbols, 0.0), "SPY": 0.06}, current={"TLT": 0.06})
    records = submit_orders(p, broker)
    assert [o["side"] for o in broker.submitted] == ["sell", "buy"]
    assert {r.status for r in records} == {"accepted"}


def test_a_failed_sell_cancels_the_buys() -> None:
    broker = FakeBroker(fail_on=frozenset({"TLT"}))
    p = plan({**dict.fromkeys(CFG.symbols, 0.0), "SPY": 0.06}, current={"TLT": 0.06})
    records = {r.symbol: r.status for r in submit_orders(p, broker)}
    assert records["TLT"].startswith("error: RuntimeError")
    assert records["SPY"] == "skipped_after_sell_failure"
    assert broker.submitted == []  # nothing reached the broker


def test_a_failed_buy_does_not_stop_the_others() -> None:
    broker = FakeBroker(fail_on=frozenset({"SPY"}))
    records = {r.symbol: r.status for r in submit_orders(plan(BASELINE), broker)}
    assert records["SPY"].startswith("error")
    assert records["TLT"] == "accepted"
    assert len(broker.submitted) == 14
