"""Reconciliation: what the broker did with the orders. Idempotent, immutable, append-only."""

from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from agent.decision.decide import decide
from agent.execution.audit import FileAuditStore, OrderRecord, build_record
from agent.execution.reconcile import OrderState, reconcile_runs, slippage_bps
from tests.helpers import CFG, FakeBroker, as_json, proposal
from tests.test_decide import FakeLLM, make_input
from tests.test_run import LIVE_ENV, cycle

LATER = datetime(2026, 11, 23, 17, 0, tzinfo=ZoneInfo("America/New_York"))


class Lookup:
    """Dict-backed OrderLookup that can also fail."""

    def __init__(self, states: dict[str, OrderState | None], boom: set[str] | None = None) -> None:
        self.states, self.boom = states, boom or set()

    def order_by_client_id(self, client_order_id: str) -> OrderState | None:
        if client_order_id in self.boom:
            raise ConnectionError("broker unreachable")
        return self.states.get(client_order_id)


def filled(qty: float, price: float, status: str = "filled") -> OrderState:
    return OrderState(
        status=status,
        filled_qty=qty,
        filled_avg_price=price,
        submitted_at="2026-11-23T15:00:01+00:00",
        filled_at="2026-11-23T15:00:02+00:00",
    )


def live_run(tmp_path: Path, **kwargs: Any) -> tuple[FileAuditStore, FakeBroker]:
    _, broker, _, store = cycle(tmp_path, live=True, env=LIVE_ENV, **kwargs)
    return store, broker


def states_for(broker: FakeBroker, factor: float = 1.001) -> dict[str, OrderState | None]:
    """Every submitted order filled completely at `factor` times its reference price (100)."""
    return {o["id"]: filled(o["quantity"], 100.0 * factor) for o in broker.submitted}


def fills(store: FileAuditStore) -> dict[str, Any]:
    (record,) = store.read_fills()
    return record


def test_slippage_is_signed_so_that_positive_is_always_worse() -> None:
    assert slippage_bps("buy", 100.0, 100.10) == pytest.approx(10.0)
    assert slippage_bps("sell", 100.0, 99.90) == pytest.approx(10.0)
    assert slippage_bps("buy", 100.0, 99.90) == pytest.approx(-10.0)
    assert slippage_bps("sell", 100.0, 100.10) == pytest.approx(-10.0)


def test_orders_carry_their_reference_price(tmp_path: Path) -> None:
    store, _ = live_run(tmp_path)
    assert {o["reference_price"] for o in store.read_runs()[0]["orders"]} == {100.0}


def test_fully_filled_orders_are_reconciled_with_their_slippage(tmp_path: Path) -> None:
    store, broker = live_run(tmp_path)
    result = reconcile_runs(store, Lookup(states_for(broker)), LATER)
    assert result.written == ["2026-11-23_morning"] and not result.pending and not result.errors
    record = fills(store)
    assert record["run_id"] == "2026-11-23_morning"
    assert record["reconciled_at"] == "2026-11-23T17:00:00-05:00"
    assert {o["status"] for o in record["orders"]} == {"filled"}
    assert all(o["slippage_bps"] == pytest.approx(10.0) for o in record["orders"])
    s = record["summary"]
    assert (s["n_orders"], s["n_filled"], s["n_partial"], s["n_unfilled"]) == (15, 15, 0, 0)
    assert s["slippage_bps_weighted"] == pytest.approx(10.0)
    assert s["filled_notional"] == pytest.approx(
        sum(o["quantity"] * 100.1 for o in broker.submitted)
    )


def test_partial_unfilled_and_rejected_orders_are_told_apart(tmp_path: Path) -> None:
    store, broker = live_run(tmp_path)
    states = states_for(broker)
    ids = [o["id"] for o in broker.submitted]
    states[ids[0]] = filled(broker.submitted[0]["quantity"] / 2, 100.0, status="canceled")
    states[ids[1]] = OrderState(
        status="expired", filled_qty=0.0, submitted_at="2026-11-23T15:00:01+00:00"
    )
    states[ids[2]] = OrderState(status="rejected", filled_qty=0.0)
    reconcile_runs(store, Lookup(states), LATER)
    s = fills(store)["summary"]
    assert (s["n_filled"], s["n_partial"], s["n_unfilled"]) == (12, 1, 2)
    unfilled = next(o for o in fills(store)["orders"] if o["status"] == "expired")
    assert unfilled["slippage_bps"] is None and unfilled["filled_qty"] == 0.0


def test_runs_with_open_orders_wait_and_are_reconciled_later(tmp_path: Path) -> None:
    store, broker = live_run(tmp_path)
    states = states_for(broker)
    states[broker.submitted[0]["id"]] = OrderState(status="new", filled_qty=0.0)
    first = reconcile_runs(store, Lookup(states), LATER)
    assert first.pending == ["2026-11-23_morning"] and first.written == []
    assert store.read_fills() == []  # nothing is written until every order is final
    second = reconcile_runs(store, Lookup(states_for(broker)), LATER)
    assert second.written == ["2026-11-23_morning"]


def test_reconciliation_is_idempotent_and_never_rewrites(tmp_path: Path) -> None:
    store, broker = live_run(tmp_path)
    reconcile_runs(store, Lookup(states_for(broker)), LATER)
    path = tmp_path / "reconciliation" / "2026-11-23_morning_fills.json"
    before = path.read_text()
    again = reconcile_runs(store, Lookup(states_for(broker, factor=1.5)), LATER)
    assert again.written == [] and again.pending == [] and again.errors == {}
    assert path.read_text() == before


def test_dry_runs_have_nothing_to_reconcile(tmp_path: Path) -> None:
    _, _, _, store = cycle(tmp_path)  # dry run: no submissions file
    result = reconcile_runs(store, Lookup({}), LATER)
    assert result.written == result.pending == [] and result.errors == {}


def test_orders_that_never_reached_the_broker_are_marked_not_sent(tmp_path: Path) -> None:
    store, broker = live_run(tmp_path, broker=FakeBroker(fail_on=frozenset({"SPY"})))
    reconcile_runs(store, Lookup(states_for(broker)), LATER)
    record = fills(store)
    spy = next(o for o in record["orders"] if o["symbol"] == "SPY")
    assert spy["status"] == "not_sent" and spy["submission_status"].startswith("error")
    assert record["summary"]["n_not_sent"] == 1 and record["summary"]["n_filled"] == 14


def test_an_order_unknown_to_the_broker_is_flagged(tmp_path: Path) -> None:
    store, broker = live_run(tmp_path)
    states = states_for(broker)
    states[broker.submitted[0]["id"]] = None
    reconcile_runs(store, Lookup(states), LATER)
    record = fills(store)
    assert record["summary"]["n_not_found"] == 1
    assert sum(o["status"] == "not_found" for o in record["orders"]) == 1


def test_a_failing_lookup_is_reported_and_does_not_block_other_runs(tmp_path: Path) -> None:
    store, morning = live_run(tmp_path)
    _, afternoon, _, _ = cycle(tmp_path, mode="afternoon", live=True, env=LIVE_ENV)
    states = {**states_for(morning), **states_for(afternoon)}
    boom = {afternoon.submitted[0]["id"]}
    result = reconcile_runs(store, Lookup(states, boom), LATER)
    assert result.written == ["2026-11-23_morning"]
    assert list(result.errors) == ["2026-11-23_afternoon"]
    assert "ConnectionError" in result.errors["2026-11-23_afternoon"]
    assert not store.has_reconciliation("2026-11-23_afternoon_fills")


def test_records_without_a_reference_price_are_reconciled_without_slippage(tmp_path: Path) -> None:
    store = FileAuditStore(tmp_path)
    inp = make_input()
    decision = decide(inp, FakeLLM([as_json(proposal())]), CFG)
    order = OrderRecord(
        symbol="SPY",
        side="buy",
        quantity=1.0,
        limit_price=100.3,
        client_order_id="old",
        status="planned",
    )
    record, reasoning = build_record(
        mode="morning", now=LATER, cfg=CFG, inp=inp, decision=decision, orders=[order],
        execution_status="live",
    )  # fmt: skip
    store.write_run(record, reasoning)
    store.write_reconciliation(
        record.run_id, {"submissions": [{**order.model_dump(), "status": "accepted"}]}
    )
    reconcile_runs(store, Lookup({"old": filled(1.0, 100.1)}), LATER)
    (row,) = fills(store)["orders"]
    assert row["reference_price"] is None and row["slippage_bps"] is None
    assert fills(store)["summary"]["slippage_bps_weighted"] is None


def test_the_fills_table_is_derived_from_the_reconciliation_files(tmp_path: Path) -> None:
    store, broker = live_run(tmp_path)
    reconcile_runs(store, Lookup(states_for(broker)), LATER)
    store.build_tables()
    with (tmp_path / "tables" / "fills.csv").open(newline="") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 15
    assert rows[0]["status"] == "filled" and float(rows[0]["slippage_bps"]) == pytest.approx(10.0)
    json.dumps(rows)
    with (tmp_path / "tables" / "orders.csv").open(newline="") as f:
        assert float(next(csv.DictReader(f))["reference_price"]) == 100.0
