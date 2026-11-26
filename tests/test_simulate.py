"""Simulations requested by the evaluators (D-017): never trade, throttled, stored apart."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from agent.checks.safety import final_check
from agent.config import tool_agent_config
from agent.execution.audit import FileAuditStore
from agent.execution.execute import Position
from agent.jobs.simulate import (
    COOLDOWN,
    DAILY_CAP,
    DEFAULT_OPTIONS,
    OPTIONS,
    TITLE,
    StartingPoint,
    apply_stance,
    comment,
    label_for,
    neutralise,
    parse_options,
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
    assert "issue.title }}" not in text
    # The issue body is received as an environment variable by one step and never appears in
    # any shell command: only `parse_options` reads it, and it keeps three closed-list words.
    for step in job["steps"]:
        assert "issue.body" not in step.get("run", "")
        if "issue.body" in str(step.get("env", "")):
            assert step["name"].startswith("Run the simulation")
    run_step = next(s for s in job["steps"] if s.get("name", "").startswith("Run the simulation"))
    assert run_step["env"]["OPTIONS"] == "${{ github.event.issue.body }}"
    assert run_step["env"]["TRADING_ENABLED"] == "false" and "--live" not in run_step["run"]
    assert workflow["concurrency"] == {"group": "simulation", "cancel-in-progress": False}


def test_only_words_from_closed_lists_are_taken_from_an_issue() -> None:
    chosen, refused = parse_options("agent: tools\nstance: bold\nstart: cash\nfree text")
    assert chosen == {"agent": "tools", "stance": "bold", "start": "cash", "universe": "standard"}
    assert refused == []
    assert parse_options("") == (DEFAULT_OPTIONS, [])
    # Case and spacing are tolerated; the first line of a key wins; unknown values are refused.
    chosen, refused = parse_options(
        "  Agent = TOOLS \nagent: pipeline\nstance: reckless\nstart:cash"
    )
    assert chosen == {
        "agent": "tools",
        "stance": "balanced",
        "start": "cash",
        "universe": "standard",
    }
    assert refused == ["stance"]
    # Injection attempts change nothing, and are not echoed back (only our own key names are).
    evil = "agent: tools; rm -rf /\nstance: `id`\nstart: $(curl x)\n@everyone <script>"
    chosen, refused = parse_options(evil)
    assert chosen == DEFAULT_OPTIONS and refused == []
    chosen, refused = parse_options("stance: " + "a" * 100_000)
    assert chosen == DEFAULT_OPTIONS and refused == ["stance"]  # a long word is just refused
    chosen, _ = parse_options("x" * 5000 + "\nagent: tools")  # past the length cap: ignored
    assert chosen == DEFAULT_OPTIONS


def test_a_stance_moves_the_limits_but_never_allows_leverage() -> None:
    base = tool_agent_config()
    cautious, bold = apply_stance(base, "cautious"), apply_stance(base, "bold")
    assert apply_stance(base, "balanced") is base
    assert cautious.risk.max_invested == 0.70 and cautious.risk.min_cash == 0.30
    assert cautious.risk.max_weight_stock < base.risk.max_weight_stock
    assert cautious.risk.max_increase_vs_baseline == 0.05  # anchored to the rule
    assert bold.risk.max_weight_etf > base.risk.max_weight_etf
    for cfg in (cautious, bold):  # whatever the stance: long only, no leverage, cash kept
        assert cfg.risk.max_invested <= 0.90 and cfg.risk.min_cash >= 0.10
    final_check({s: 0.04 for s in CFG.symbols}, 1 - 0.04 * 15, cautious)
    with pytest.raises(Exception, match="above its cap"):
        final_check({**dict.fromkeys(CFG.symbols, 0.0), "NVDA": 0.08}, 0.92, cautious)
    # The pipeline agent's bound relative to the rule widens for bold, stays for the tool agent.
    assert apply_stance(CFG, "bold").risk.max_increase_vs_baseline == 0.30
    assert bold.risk.max_increase_vs_baseline == base.risk.max_increase_vs_baseline


def test_the_starting_point_changes_the_holdings_and_never_trades() -> None:
    real = FakeBroker(positions={"SPY": Position(quantity=10, market_value=1000.0)})
    assert StartingPoint(real, "current", CFG.symbols).positions() == real.positions()
    assert StartingPoint(real, "cash", CFG.symbols).positions() == {}
    equal = StartingPoint(real, "equal", CFG.symbols).positions()
    assert set(equal) == set(CFG.symbols)
    assert all(p.market_value == pytest.approx(6000.0) for p in equal.values())  # 6% of 100,000
    assert all(p.quantity == pytest.approx(60.0) for p in equal.values())  # at 100 a share
    point = StartingPoint(real, "cash", CFG.symbols)
    assert point.account().equity == 100_000.0 and point.is_market_open()
    assert point.latest_prices(["SPY"]) == {"SPY": 100.0}
    with pytest.raises(RuntimeError, match="never sends an order"):
        point.submit_limit_order("SPY", "buy", 1.0, 100.0, "x")


def test_a_simulation_follows_the_chosen_options_and_still_never_trades(tmp_path: Path) -> None:
    broker = FakeBroker(positions={})
    options = {"agent": "pipeline", "stance": "cautious", "start": "equal"}
    reply, result = simulate(
        now=NOW, cfg=CFG, market=FakeMarketData(CLOSES), broker=broker,
        model=FakeTimestral([0.02] * 15), llm=FakeLLM([json.dumps(proposal(TLT=0.05))] * 4),
        root=tmp_path, requester="reviewer-1", env=LIVE_ENV, options=options, refused=["agent"],
    )  # fmt: skip
    assert broker.submitted == [] and result.execution_status in ("dry_run", "hold")
    assert "stance **cautious**" in reply and "start from **equal**" in reply
    assert "an equal-weight portfolio" in reply and "Unknown value for agent" in reply
    record = FileAuditStore(tmp_path / "simulations").read_run(result.run_id)
    assert record["run_id"].endswith("sim-100000-cautious-equal")  # the options are in the id
    assert record["config"]["risk"]["max_invested"] == 0.70  # the stance is in the record
    assert record["portfolios"]["current"]["SPY"] == pytest.approx(0.06)  # the chosen start


def test_the_default_run_id_and_reply_are_unchanged() -> None:
    assert label_for(NOW, DEFAULT_OPTIONS) == "sim-100000" == label_for(NOW)
    assert label_for(NOW, {"agent": "tools"}) == "sim-100000-tools"
    assert set(OPTIONS) == {"agent", "stance", "start", "universe"} and all(
        OPTIONS[k] for k in OPTIONS
    )


def test_the_dashboard_form_offers_exactly_the_options_the_parser_accepts() -> None:
    from agent.dashboard.records import SIM_CHOICES

    assert set(SIM_CHOICES) == set(OPTIONS)
    for key, (_, choices) in SIM_CHOICES.items():
        values = [value for value, _ in choices]
        assert set(values) == set(OPTIONS[key]) and len(values) == len(set(values))
        assert parse_options(f"{key}: {values[0]}")[0][key] == values[0]  # each one is accepted


def test_the_dashboard_button_opens_the_request_the_workflow_expects() -> None:
    from agent.dashboard.records import SIM_TITLE

    assert SIM_TITLE == TITLE
    assert BASELINE  # shared fixtures stay in use


def test_the_wide_universe_adds_the_panel_stocks_in_the_existing_sector_groups() -> None:
    from agent.config import AgentConfig
    from agent.jobs.simulate import apply_universe

    base = AgentConfig()
    assert apply_universe(base, "standard") is base
    wide = apply_universe(base, "wide")
    assert len(wide.universe) == 40 and len(set(wide.symbols)) == 40
    assert wide.symbols[:15] == base.symbols  # the live assets first, unchanged
    groups = {a.group for a in base.universe}
    assert wide.asset("GOOGL").group == "tech_growth" and wide.asset("BAC").group in groups
    assert parse_options("universe: wide")[0]["universe"] == "wide"


def test_the_equal_weight_start_invests_90_percent_whatever_the_universe() -> None:
    from agent.config import AgentConfig
    from agent.jobs.simulate import StartingPoint, apply_universe

    for cfg in (AgentConfig(), apply_universe(AgentConfig(), "wide")):
        prices = dict.fromkeys(cfg.symbols, 100.0)
        held = StartingPoint(FakeBroker(prices=prices), "equal", cfg.symbols).positions()
        assert sum(p.market_value for p in held.values()) == pytest.approx(0.9 * 100_000)


def test_the_whole_run_sees_one_universe_news_included() -> None:
    """2026-11-25: news + wide universe failed: the news covariates were built for the 15 live
    assets while the forecasts covered 40. One configuration now serves both."""
    from agent.config import news_agent_config
    from agent.jobs.simulate import simulation_config

    options = {"agent": "news", "stance": "cautious", "start": "equal", "universe": "wide"}
    cfg = simulation_config(news_agent_config(), options)
    assert len(cfg.symbols) == 40 and cfg.risk.min_cash == 0.30
    assert (
        simulation_config(cfg, options).symbols == cfg.symbols
    )  # applying it again changes nothing
