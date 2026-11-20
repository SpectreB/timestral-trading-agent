"""Full cycle, offline: dry run, live, kill switch, idempotency, drawdown, fallback."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agent.audit import FileAuditStore
from agent.execute import ExecutionError, Position
from agent.run import CycleResult, run_cycle
from tests.helpers import (
    BASELINE,
    CFG,
    FakeBroker,
    FakeMarketData,
    FakeTimestral,
    as_json,
    make_closes,
    positions_for,
    proposal,
)
from tests.test_decide import BROKEN, NOW, FakeLLM

CLOSES = make_closes(520)
LIVE_ENV = {"TRADING_ENABLED": "true"}


def cycle(
    tmp_path: Path,
    *,
    mode: str = "morning",
    live: bool = False,
    env: dict[str, str] | None = None,
    broker: FakeBroker | None = None,
    llm: FakeLLM | None = None,
    market_open: bool = True,
) -> tuple[CycleResult, FakeBroker, FakeLLM, FileAuditStore]:
    broker = broker or FakeBroker()
    llm = llm or FakeLLM([as_json(proposal())] * 4)
    store = FileAuditStore(tmp_path)
    result = run_cycle(
        mode=mode,  # type: ignore[arg-type]
        now=NOW,
        cfg=CFG,
        market=FakeMarketData(CLOSES, market_open=market_open),
        broker=broker,
        model=FakeTimestral([0.02] * 15),
        llm=llm,
        store=store,
        live=live,
        env=env or {},
    )
    return result, broker, llm, store


def test_dry_run_records_everything_and_sends_nothing(tmp_path: Path) -> None:
    result, broker, _, store = cycle(tmp_path)
    assert result == CycleResult("2026-11-23_morning", "done", "accepted", "dry_run", 15)
    assert broker.submitted == []
    record = store.read_runs()[0]
    assert record["execution_status"] == "dry_run"
    assert {o["status"] for o in record["orders"]} == {"dry_run"}
    assert record["portfolios"]["baseline"]["SPY"] == pytest.approx(0.06)
    assert record["data"]["last_session"] == "2026-11-20"
    assert not (tmp_path / "reconciliation").exists()


def test_the_decision_payload_includes_live_prices(tmp_path: Path) -> None:
    _, _, llm, _ = cycle(tmp_path)
    payload: dict[str, Any] = json.loads(llm.calls[0][1]["content"])
    spy = payload["assets"][0]
    assert spy["latest_price"] == 100.0
    assert spy["move_since_last_close"] == pytest.approx(100.0 / float(CLOSES["SPY"].iloc[-1]) - 1,
                                                          abs=1e-4)  # fmt: skip


def test_live_run_writes_ahead_then_submits(tmp_path: Path) -> None:
    result, broker, _, store = cycle(tmp_path, live=True, env=LIVE_ENV)
    assert result.execution_status == "live" and result.n_orders == 15
    assert len(broker.submitted) == 15 and all(o["side"] == "buy" for o in broker.submitted)
    record = store.read_runs()[0]
    assert {o["status"] for o in record["orders"]} == {"planned"}  # recorded before sending
    submissions = json.loads((tmp_path / "reconciliation" / "2026-11-23_morning.json").read_text())
    assert {o["status"] for o in submissions["submissions"]} == {"accepted"}


def test_without_the_kill_switch_a_live_request_stays_a_dry_run(tmp_path: Path) -> None:
    for env in ({}, {"TRADING_ENABLED": "false"}):
        sub = tmp_path / str(len(env))
        result, broker, _, _ = cycle(sub, live=True, env=env)
        assert result.execution_status == "dry_run_kill_switch"
        assert broker.submitted == []


def test_live_run_does_nothing_when_the_market_is_closed(tmp_path: Path) -> None:
    result, broker, llm, store = cycle(tmp_path, live=True, env=LIVE_ENV,
                                       broker=FakeBroker(market_open=False))  # fmt: skip
    assert result.status == "market_closed"
    assert llm.calls == [] and broker.submitted == [] and not store.has_run(result.run_id)


def test_a_dry_run_works_with_the_market_closed(tmp_path: Path) -> None:
    result, _, _, _ = cycle(tmp_path, broker=FakeBroker(market_open=False), market_open=False)
    assert result.status == "done"


def test_a_retried_run_is_a_noop(tmp_path: Path) -> None:
    cycle(tmp_path, live=True, env=LIVE_ENV)
    result, broker, llm, _ = cycle(tmp_path, live=True, env=LIVE_ENV)
    assert result.status == "already_done"
    assert llm.calls == [] and broker.submitted == []


def test_morning_and_afternoon_are_separate_runs(tmp_path: Path) -> None:
    cycle(tmp_path, mode="morning")
    result, _, _, store = cycle(tmp_path, mode="afternoon")
    assert result.status == "done"
    assert [r["run_id"] for r in store.read_runs()] == [
        "2026-11-23_afternoon",
        "2026-11-23_morning",
    ]


def test_unparseable_answers_fall_back_to_holding(tmp_path: Path) -> None:
    held = positions_for({"SPY": 0.05})
    broker = FakeBroker(positions=held)
    result, _, llm, store = cycle(tmp_path, live=True, env=LIVE_ENV, broker=broker,
                                  llm=FakeLLM([BROKEN, BROKEN]))  # fmt: skip
    assert result.outcome == "fallback_hold" and result.execution_status == "hold"
    assert result.n_orders == 0 and broker.submitted == [] and len(llm.calls) == 2
    assert store.read_runs()[0]["portfolios"]["final"]["SPY"] == pytest.approx(0.05)


def test_drawdown_comes_from_the_peak_recorded_in_the_audit_log(tmp_path: Path) -> None:
    cycle(tmp_path, mode="morning")  # records equity 100,000
    drifted = FakeBroker(equity=94_000.0, positions=positions_for(BASELINE, 94_000.0))
    result, _, llm, store = cycle(tmp_path, mode="afternoon", broker=drifted)
    assert result.status == "done"
    record = next(r for r in store.read_runs() if r["mode"] == "afternoon")
    assert record["account"]["drawdown_from_peak"] == pytest.approx(-0.06)
    payload = json.loads(llm.calls[0][1]["content"])
    assert payload["portfolio"]["drawdown_actions"] == ["alert", "reduce_only"]


def test_positions_outside_the_universe_stop_the_run(tmp_path: Path) -> None:
    broker = FakeBroker(positions={"TSLA": Position(10.0, 2500.0)})
    with pytest.raises(ExecutionError, match="TSLA"):
        cycle(tmp_path, broker=broker)
    assert not (tmp_path / "runs").exists()


def test_a_non_paper_account_stops_the_run_before_any_llm_call(tmp_path: Path) -> None:
    llm = FakeLLM([])
    with pytest.raises(ExecutionError, match="paper"):
        cycle(tmp_path, broker=FakeBroker(is_paper=False), llm=llm)
    assert llm.calls == []
