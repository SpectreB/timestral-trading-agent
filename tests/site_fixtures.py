"""Audit records and result files shared by the site tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent.dashboard.records import chronological
from agent.execution.audit import FileAuditStore
from tests.helpers import FakeBroker, FakeMarketData, FakeTimestral, as_json, make_closes, proposal
from tests.test_decide import BROKEN, NOW
from tests.test_run import cycle

STAMP = datetime(2026, 11, 23, 22, 0, tzinfo=UTC)


def _report(tmp_path: Path, closes_after: tuple[float, ...] = (101.0, 102.0, 101.5)) -> Any:
    """A real run plus observed closes: the performance report as the page gets it."""
    from agent.evaluation.metrics import performance_report
    from tests.test_scoring import SYMS

    _, _, _, store = cycle(tmp_path)
    dates = ["2026-11-20", "2026-11-23", "2026-11-24", "2026-11-25"]
    prices = (100.0, *closes_after)
    closes = {d: dict.fromkeys(SYMS, p) for d, p in zip(dates, prices, strict=True)}
    closes["2026-11-24"]["SPY"] = 99.0  # the market falls on day two: references differ
    store.write_observation("2026-11-25", {"as_of": "2026-11-25", "closes": closes})
    runs = chronological(store.read_runs())
    return store, runs, performance_report(runs, store.read_observations())


def _tool_cycle(audit: Path, broken: bool = False) -> FileAuditStore:
    """A tool agent run, recorded where the Trade workflow puts it."""
    from agent.config import tool_agent_config
    from agent.jobs.run import run_cycle
    from tests.test_decide_tools import EVALUATE, FakeToolLLM, step

    calls = [
        ("get_forecasts", '{"symbols": ["SPY"], "horizon": 5}'),
        ("evaluate_portfolio", EVALUATE),
        ("evaluate_portfolio", EVALUATE),
        ("get_baseline", "{}"),
        ("get_forecasts", '{"symbols": ["BTC"], "horizon": 5}'),
    ]
    store = FileAuditStore(audit / "agents" / "tools")
    run_cycle(
        mode="morning",
        now=NOW,
        cfg=tool_agent_config(),
        market=FakeMarketData(make_closes(520)),
        broker=FakeBroker(),
        model=FakeTimestral([0.02] * 15),
        llm=FakeToolLLM([step(*calls), step()], [BROKEN if broken else as_json(proposal())] * 2),  # type: ignore[arg-type]
        store=store,
        live=False,
        env={},
    )
    return store


def _replay_report(tmp_path: Path) -> dict[str, Any]:
    _, _, report = _report(tmp_path / "replay")
    return {**report, "account_return": -0.0123}


def _evals_dir(tmp_path: Path) -> Path:
    folder = tmp_path / "evals"
    (folder / "tools_v2").mkdir(parents=True)

    def results(passes: list[bool], key: str = "calm") -> dict[str, Any]:
        scenario = {"key": key, "title": "Nothing happens", "expectation": "Stays close.",
                    "passed": all(passes), "runs": [{"repeat": i, "passed": x}
                                                    for i, x in enumerate(passes)]}  # fmt: skip
        return {"scenarios": [scenario], "stability": {"calls": 5, "max_pairwise_turnover": 0.06}}

    (folder / "results.json").write_text(json.dumps(results([False, False])))
    (folder / "tools_v2" / "results.json").write_text(json.dumps(results([True, False, True])))
    (folder / "tools_v2" / "focus_10_runs_v2.json").write_text(
        json.dumps({"calm": [True] * 7 + [False] * 3, "details": {}})
    )
    return folder


def _covariate_results() -> dict[str, Any]:
    def summary(variant: str, cov: float) -> list[dict[str, Any]]:
        return [
            {"variant": variant, "kind": k, "n": 10, "coverage_80": cov, "pinball": 0.01,
             "rms_ratio": 1.0, "hit_rate": 0.5}
            for k in ("stock", "etf")
        ]  # fmt: skip

    variants = ("news", "events", "news+events")
    return {
        "horizon": 5, "origins": 118, "first_origin": "2026-06-01", "last_origin": "2026-11-16",
        "stocks": 30, "stocks_with_news": 29, "etfs": 10,
        "summary": summary("prices", 0.78) + [r for v in variants for r in summary(v, 0.79)],
        "paired": [
            {"variant": v, "kind": k, "origins": 118, "mean_diff": -1e-5, "relative": -0.001,
             "ci95": [-2e-5, 1e-6]}
            for v in variants for k in ("stock", "etf")
        ],
        "verdicts": [{"variant": v, "decision": "no"} for v in variants],
    }  # fmt: skip
