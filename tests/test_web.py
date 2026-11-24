"""The data file behind the React page (src/agent/web.py) and the page's own files (web/)."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import pytest

from agent.audit import FileAuditStore
from agent.reconcile import reconcile_runs
from agent.web import WEB_DIR, _tool_steps, build
from tests.test_dashboard import (
    STAMP,
    _covariate_results,
    _evals_dir,
    _replay_report,
    _tool_cycle,
)
from tests.test_reconcile import LATER, Lookup, live_run, states_for
from tests.test_run import cycle


def _site(tmp_path: Path, **kwargs: Any) -> dict[str, Any]:
    path = build(tmp_path / "audit", tmp_path / "site", STAMP, **kwargs)
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def _docs(tmp_path: Path) -> dict[str, Path]:
    replay, tool_replay, cov = tmp_path / "r", tmp_path / "rt", tmp_path / "cov"
    for folder in (replay, tool_replay, cov):
        folder.mkdir()
    report = _replay_report(tmp_path)
    (replay / "report.json").write_text(json.dumps(report), encoding="utf-8")
    (tool_replay / "report.json").write_text(json.dumps(report), encoding="utf-8")
    (cov / "results.json").write_text(json.dumps(_covariate_results()), encoding="utf-8")
    return {
        "replay_dir": replay,
        "tool_replay_dir": tool_replay,
        "evals_dir": _evals_dir(tmp_path),
        "covariates_dir": cov,
    }


def test_the_site_has_the_page_its_data_and_the_plain_fallback(tmp_path: Path) -> None:
    cycle(tmp_path / "audit")
    data = _site(tmp_path)
    site = tmp_path / "site"
    for name in ("index.html", "app.js", "style.css", "classic.html", "data.json"):
        assert (site / name).exists(), name
    assert (site / "index.html").read_text() == (WEB_DIR / "index.html").read_text()
    assert data["links"]["classic"] == "classic.html"
    assert [a["key"] for a in data["agents"]] == ["pipeline"]  # no tool agent records yet


def test_both_agents_with_their_live_state_the_tool_agent_first(tmp_path: Path) -> None:
    audit = tmp_path / "audit"
    cycle(audit)
    _tool_cycle(audit)
    store = FileAuditStore(audit)
    start = store.read_runs()[0]["account"]["equity"]
    day = store.read_runs()[0]["started_at"][:10]
    store.write_valuation("a", {"at": f"{day}T23:10:00+00:00", "equity": start + 500.0})
    data = _site(tmp_path, **_docs(tmp_path))
    tools, pipeline = data["agents"]
    assert (tools["key"], pipeline["key"]) == ("tools", "pipeline")
    assert pipeline["now"] == {
        "equity": start + 500.0,
        "at": f"{day}T23:10:00+00:00",
        "source": "sample",
    }
    assert tools["now"]["source"] == "run"
    assert pipeline["series"][-1]["label"] == "account sample"
    assert pipeline["next_run"] > data["generated_at"]
    # The tool agent's questions to Timestral, in order, and what came back.
    steps = tools["decision"]["tool_steps"]
    assert [s["tool"] for s in steps][:2] == ["get_forecasts", "evaluate_portfolio"]
    assert steps[0]["horizon"] == 5 and "asset(s) at 5 day(s)" in steps[0]["result"]
    assert "probability of a loss" in steps[1]["result"]
    assert steps[3]["result"] == "the rule-based weights"
    assert steps[4]["result"].startswith("refused:")
    # Measures, replay, tests and the covariate trial reach the page.
    assert tools["measures"]["calls"] and pipeline["replay"]["sessions"] > 0
    assert set(pipeline["replay"]["curves"]) >= {"agent", "baseline", "equal_weight", "spy"}
    assert data["behaviour"]["scenarios"] and data["covariates"]["verdicts"]["news"] == "no"
    assert data["covariates"]["stocks"] == 29
    assert data["inputs"][3]["items"][-1] == "measured, not adopted"
    json.dumps(data, allow_nan=False)  # strict JSON: no NaN reaches the page


def test_timestral_raw_output_and_its_spread_reach_the_page(tmp_path: Path) -> None:
    audit = tmp_path / "audit"
    cycle(audit)
    _tool_cycle(audit)
    tools, pipeline = _site(tmp_path)["agents"]
    f = pipeline["forecast"]
    run = FileAuditStore(audit).read_runs()[0]
    assert f["raw"] == run["forecasts"]  # exactly as recorded
    spreads = [a["horizons"][-1]["spread"] for a in f["assets"]]
    assert spreads == sorted(spreads, reverse=True)  # riskiest first
    first = f["assets"][0]
    last = first["horizons"][-1]
    assert last["spread"] == pytest.approx(last["q90"] - last["q10"], abs=1e-4)
    assert [h["h"] for h in first["horizons"]] == sorted(h["h"] for h in first["horizons"])
    assert first["rule_weight"] == run["portfolios"]["baseline"][first["symbol"]]
    assert f["tool_quantiles"] is None  # the pipeline agent asks nothing
    assert tools["forecast"]["tool_quantiles"]["horizon"] == 5


def test_orders_holdings_and_runs_of_a_live_run(tmp_path: Path) -> None:
    store, broker = live_run(tmp_path / "audit")
    sent = _site(tmp_path)["agents"][0]
    assert sent["orders"] and {o["status"] for o in sent["orders"]} == {"sent"}
    reconcile_runs(store, Lookup(states_for(broker)), LATER)
    a = _site(tmp_path)["agents"][0]
    assert {o["status"] for o in a["orders"]} == {"filled"}
    assert all(o["value"] == pytest.approx(o["quantity"] * o["price"]) for o in a["orders"])
    holdings = a["holdings"]
    assert [h["final"] for h in holdings] == sorted((h["final"] for h in holdings), reverse=True)
    assert holdings[0]["rationale"] and holdings[0]["value"] > 0
    assert a["runs"][0]["record"].endswith(f"runs/{a['runs'][0]['run_id']}.json")
    assert a["decision"]["portfolio_vol"]["horizon"] == 5


def test_an_agent_without_runs_and_a_page_without_evidence(tmp_path: Path) -> None:
    (tmp_path / "audit" / "agents" / "tools" / "runs").mkdir(parents=True)
    data = _site(tmp_path)
    tools, pipeline = data["agents"]
    assert tools["started"] is False and "decision" not in tools
    assert pipeline["started"] is False
    assert data["behaviour"] is None and data["covariates"] is None
    assert data["inputs"][3]["items"][-1] == "built, not measured"
    assert data["simulations"]["recent"] == []


def test_simulations_offer_the_closed_choices_and_list_past_runs(tmp_path: Path) -> None:
    cycle(tmp_path / "audit")
    cycle(tmp_path / "audit" / "simulations")
    sims = _site(tmp_path)["simulations"]
    assert [c["key"] for c in sims["choices"]] == ["agent", "stance", "start"]
    assert sims["recent"][0]["requested_by"] == "unknown"
    assert "/simulations/runs/" in sims["recent"][0]["record"]


def test_behaviour_without_the_pipeline_results(tmp_path: Path) -> None:
    from agent.web import _behaviour

    assert _behaviour({"pipeline": None, "tools": None}) is None
    evals = json.loads((_evals_dir(tmp_path) / "results.json").read_text())
    only = _behaviour({"tools": evals})
    assert only is not None and set(only["stability"]) == {"tools"}
    assert all(s["pipeline"] is None for s in only["scenarios"])


def test_tool_steps_tolerate_unreadable_arguments() -> None:
    run = {
        "decision": {
            "tool_calls": [{"name": "get_baseline", "arguments": "{not json", "result": {}}]
        }
    }
    (step,) = _tool_steps(run)
    assert step["horizon"] is None and step["result"] == "the rule-based weights"


def test_the_page_renders_text_only_and_loads_pinned_libraries() -> None:
    page = (WEB_DIR / "index.html").read_text()
    for lib in ("react@18.3.1", "react-dom@18.3.1", "htm@3.1.1"):
        assert lib in page
    app = (WEB_DIR / "app.js").read_text()
    assert "dangerouslySetInnerHTML" not in app and "innerHTML" not in app
    assert "data.json" in app
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
        f.write(app)
    assert subprocess.run([node, "--check", f.name], capture_output=True).returncode == 0


def test_a_run_without_a_risk_block_has_no_portfolio_volatility() -> None:
    from agent.web import _portfolio_vol

    assert _portfolio_vol({"risk": None}) is None
    assert _portfolio_vol(
        {"risk": {"portfolio_vol": {"1": {"final": 0.01, "baseline": 0.02}}}}
    ) == {
        "horizon": 1,
        "final": 0.01,
        "rule": 0.02,
    }
