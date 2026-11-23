"""Simulations requested by the evaluators (D-017): never trade, throttled, stored apart."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from agent.audit import FileAuditStore
from agent.simulate import (
    COOLDOWN,
    DAILY_CAP,
    TITLE,
    comment,
    label_for,
    neutralise,
    refusal,
    simulate,
    throttle,
)
from tests.helpers import (
    BASELINE,
    CFG,
    FakeBroker,
    FakeMarketData,
    FakeTimestral,
    make_closes,
    proposal,
)
from tests.test_decide import BROKEN, NOW, FakeLLM
from tests.test_run import cycle

CLOSES = make_closes(520)
LIVE_ENV = {"TRADING_ENABLED": "true"}


def request(
    root: Path, now: Any = NOW, llm: FakeLLM | None = None, broker: FakeBroker | None = None,
    requester: str = "reviewer-1",
) -> tuple[str, Any, FakeBroker, FakeLLM]:  # fmt: skip
    broker = broker or FakeBroker()
    llm = llm or FakeLLM([json.dumps(proposal(TLT=0.10, NVDA=0.02))] * 4)
    reply, result = simulate(
        now=now, cfg=CFG, market=FakeMarketData(CLOSES), broker=broker,
        model=FakeTimestral([0.02] * 15), llm=llm, root=root, requester=requester, env=LIVE_ENV,
    )  # fmt: skip
    return reply, result, broker, llm


def test_a_simulation_never_trades_even_with_the_kill_switch_on(tmp_path: Path) -> None:
    reply, result, broker, _ = request(tmp_path)
    assert broker.submitted == []  # TRADING_ENABLED=true in the environment, market open
    assert result.execution_status == "dry_run" and result.n_orders > 0  # planned, not sent
    assert "No order was sent" in reply


def test_it_is_stored_apart_with_its_own_id_and_requester(tmp_path: Path) -> None:
    _, result, _, _ = request(tmp_path)
    assert result.run_id == "2026-11-23_sim-100000"
    sims = FileAuditStore(tmp_path / "simulations")
    (record,) = sims.read_runs()
    assert record["requested_by"] == "reviewer-1" and record["run_id"] == result.run_id
    assert FileAuditStore(tmp_path).read_runs() == []  # the real agent's log is untouched


def test_the_drawdown_comes_from_the_real_agents_history(tmp_path: Path) -> None:
    cycle(tmp_path, mode="morning")  # the real agent recorded 100,000
    _, _, _, llm = request(tmp_path, broker=FakeBroker(equity=94_000.0))
    payload = json.loads(llm.calls[0][1]["content"])
    assert payload["portfolio"]["drawdown_from_peak"] == pytest.approx(-0.06)


def test_requests_are_throttled(tmp_path: Path) -> None:
    request(tmp_path)
    reply, result, _, llm = request(tmp_path, now=NOW + timedelta(seconds=20))
    assert result is None and llm.calls == []
    assert "Simulation not started" in reply and "less than a minute ago" in reply
    assert "about 40 second(s)" in reply
    later, result, _, _ = request(tmp_path, now=NOW + COOLDOWN + timedelta(seconds=1))
    assert result is not None and "Simulation finished" in later


def test_the_daily_cap_applies(tmp_path: Path) -> None:
    store = FileAuditStore(tmp_path / "simulations")
    for i in range(DAILY_CAP):
        request(tmp_path, now=NOW + timedelta(minutes=2 * i))
    assert len(store.read_runs()) == DAILY_CAP
    reason = throttle(store, NOW + timedelta(minutes=2 * DAILY_CAP))
    assert reason is not None and "20 simulations a day" in reason
    assert throttle(store, NOW + timedelta(days=1, minutes=2 * DAILY_CAP)) is None  # a new day


def test_the_reply_reports_the_decision_moves_corrections_and_reading(tmp_path: Path) -> None:
    llm = FakeLLM([json.dumps(proposal(NVDA=0.15, SPY=0.01))] * 2)
    reply, _, _, _ = request(tmp_path, llm=llm)
    assert reply.startswith("### Simulation finished: corrected")
    assert "Requested by @reviewer-1" in reply
    assert "| NVDA |" in reply and "above\\_asset\\_cap" not in reply  # rule names are code
    assert "`above_asset_cap` NVDA" in reply
    assert "Test analysis." in reply and "simulations/runs/2026-11-23_sim-100000.json" in reply


def test_a_rejected_simulation_still_gets_an_answer(tmp_path: Path) -> None:
    reply, result, _, _ = request(tmp_path, llm=FakeLLM([BROKEN, BROKEN]))
    assert result.outcome == "fallback_hold"
    assert "fallback hold" in reply and "No change" in reply and "No readable analysis" in reply


def test_llm_text_is_neutralised_before_being_posted() -> None:
    evil = "<script>x</script> @everyone ping `code` **bold** [link](http://x) | table"
    clean = neutralise(evil, 500)
    assert "<script>" not in clean and "&lt;script&gt;" in clean
    assert "@everyone" not in clean and "@​everyone" in clean
    assert "\\`code\\`" in clean and "\\*\\*bold\\*\\*" in clean and "\\[link\\]" in clean
    assert "\\|" in clean
    assert neutralise("word " * 100, 20).endswith("…") and len(neutralise("word " * 100, 20)) == 20


def test_an_analysis_that_is_not_text_is_ignored(tmp_path: Path) -> None:
    _, result, _, _ = request(tmp_path)
    record = FileAuditStore(tmp_path / "simulations").read_run(result.run_id)
    record["decision"]["attempts"][-1]["answer"] = json.dumps({"analysis": 3})
    assert "No readable analysis" in comment(record, "x")


def test_labels_and_refusals() -> None:
    assert label_for(NOW) == "sim-100000"
    assert refusal("wait").startswith("### Simulation not started")


def test_the_workflow_only_reacts_to_simulation_requests_and_never_reads_the_issue_text() -> None:
    text = Path(".github/workflows/simulate.yml").read_text()
    workflow = yaml.safe_load(text)
    job = workflow["jobs"]["simulate"]
    assert job["if"] == f"startsWith(github.event.issue.title, '{TITLE}')"
    assert "issue.body" not in text and "issue.title }}" not in text  # no untrusted interpolation
    run_step = next(s for s in job["steps"] if s.get("name", "").startswith("Run the simulation"))
    assert run_step["env"]["TRADING_ENABLED"] == "false" and "--live" not in run_step["run"]
    assert workflow["concurrency"] == {"group": "simulation", "cancel-in-progress": False}


def test_the_dashboard_button_opens_the_request_the_workflow_expects() -> None:
    from agent.dashboard import SIM_TITLE

    assert SIM_TITLE == TITLE
    assert BASELINE  # shared fixtures stay in use
