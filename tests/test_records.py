"""Audit records read into per-agent views (src/agent/dashboard/records.py)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agent.dashboard.records import AgentView, _alerts, _analysis, _measures, chronological
from agent.execution.audit import FileAuditStore
from tests.helpers import BASELINE, FakeBroker, positions_for, proposal
from tests.site_fixtures import _tool_cycle
from tests.test_decide import BROKEN, FakeLLM
from tests.test_run import cycle


def _view(key: str, runs: list[Any], fills: list[Any] | None = None, **kw: Any) -> AgentView:
    return AgentView.model_validate(
        {"key": key, "title": key, "summary": "", "runs": runs, "fills": fills or [],
         "scores": [], "report": None, **kw}
    )  # fmt: skip


def _last(store: FileAuditStore) -> dict[str, object]:
    return chronological(store.read_runs())[-1]


def _held(equity: float) -> dict[str, object]:
    return positions_for(BASELINE, equity)  # type: ignore[no-any-return]


def _afternoon_at(tmp_path: Path, equity: float) -> list[tuple[str, str]]:
    cycle(tmp_path, mode="morning")
    broker = FakeBroker(equity=equity, positions=_held(equity))
    _, _, _, store = cycle(tmp_path, mode="afternoon", broker=broker)
    return _alerts(_last(store), [])  # type: ignore[arg-type]


def test_a_rejected_run_raises_an_alert_and_has_no_analysis(tmp_path: Path) -> None:
    _, _, _, store = cycle(tmp_path, llm=FakeLLM([BROKEN, BROKEN]))
    run = _last(store)
    assert ("crit", "LLM proposal rejected (invalid_json): positions held") in _alerts(run, [])  # type: ignore[arg-type]
    assert _analysis(run) == ""  # type: ignore[arg-type]


def test_an_analysis_that_is_not_text_is_ignored(tmp_path: Path) -> None:
    _, _, _, store = cycle(tmp_path, llm=FakeLLM(['{"analysis": 5}', '{"analysis": 5}']))
    assert _analysis(_last(store)) == ""  # type: ignore[arg-type]


def test_alerts_surface_corrections_and_circuit_breaker_levels(tmp_path: Path) -> None:
    llm = FakeLLM([json.dumps(proposal(NVDA=0.15, SPY=0.01))])
    _, _, _, store = cycle(tmp_path / "a", llm=llm)
    assert ("warn", "1 guardrail correction(s) at the last run") in _alerts(_last(store), [])  # type: ignore[arg-type]
    assert ("crit", "Circuit breaker active: reduce-only mode") in _afternoon_at(
        tmp_path / "b", 94_000.0
    )
    assert ("warn", "Drawdown alert level reached") in _afternoon_at(tmp_path / "c", 96_500.0)
    assert ("crit", "Circuit breaker override: the LLM was bypassed") in _afternoon_at(
        tmp_path / "d", 80_000.0
    )


def test_unfilled_orders_of_the_last_run_raise_an_alert(tmp_path: Path) -> None:
    _, _, _, store = cycle(tmp_path)
    run = _last(store)
    fills = [
        {"run_id": run["run_id"], "summary": {"n_not_sent": 0, "n_not_found": 0, "n_unfilled": 1}}
    ]
    assert ("warn", "1 unfilled, 0 unsent order(s)") in _alerts(run, fills)  # type: ignore[arg-type]


def test_measures_that_cannot_be_computed_are_left_out_not_invented(tmp_path: Path) -> None:
    record = chronological(_tool_cycle(tmp_path).read_runs())[0]
    record["risk"] = None  # a run recorded without its risk figures
    fills = [
        {"run_id": "r", "summary": {"n_orders": 2, "n_filled": 1, "slippage_bps_weighted": None}}
    ]
    view = _view("pipeline", [record], fills, replay={"sessions": 0})
    out = _measures(view, {"tools": None, "pipeline": None})
    assert out["risk"] == "n/a" and out["filled"] == "1/2" and "slippage" not in out
    assert "past_return" not in out and "tests" not in out and out["accepted"] == "1/1"
    assert _measures(_view("tools", []), None) == {}


def test_the_next_slot_skips_the_weekend() -> None:
    from datetime import UTC, datetime

    from agent.dashboard.records import _next_slot

    friday_evening = datetime(2026, 11, 20, 23, 0, tzinfo=UTC)
    assert _next_slot(friday_evening).strftime("%a %H:%M") == "Mon 10:00"


def test_partial_risk_figures_and_replays_are_skipped(tmp_path: Path) -> None:
    record = chronological(_tool_cycle(tmp_path).read_runs())[0]
    record["risk"] = {"portfolio_vol": {"5": {"final": None, "baseline": 0.01}}}
    replay = {
        "sessions": 3,
        "series": {"agent": {"cumulative_return": 0.01, "annualised_volatility": 0.1,
                             "max_drawdown": 0.02}},
        "risk_calibration": [{"horizon": 1, "inside_80": 0.8}],
    }  # fmt: skip
    out = _measures(_view("tools", [record], replay=replay), None)
    assert out["risk"] == "n/a" and out["past_return"] == "+1.0%" and "past_cov" not in out
    replay["risk_calibration"] = [{"horizon": 5, "inside_80": 0.79}]
    assert _measures(_view("tools", [record], replay=replay), None)["past_cov"] == "79%"
