"""Historical replay (D-015): point-in-time data, anonymity, simulated fills, resumption."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from agent.config import AgentConfig
from agent.evaluation.replay import (
    COST_BPS,
    Portfolio,
    ReplayError,
    anonymise,
    execute_at_open,
    point_in_time,
    record_observations,
    render_markdown,
    replay_report,
    run_probe,
    run_replay,
    score_probe,
    write_manifest,
)
from agent.execution.audit import FileAuditStore
from tests.helpers import BASELINE, CFG, FakeTimestral, as_json, proposal
from tests.test_decide import BROKEN, FakeLLM

N_CONTEXT = CFG.forecast.context_length
DAYS = N_CONTEXT + 12
INDEX = pd.bdate_range(end="2026-11-20", periods=DAYS)
SESSIONS = list(INDEX[-4:])


def history(seed: int = 3) -> tuple[pd.DataFrame, pd.DataFrame]:
    rng = np.random.default_rng(seed)
    walk = 100 * np.exp(np.cumsum(rng.normal(0.0003, 0.01, (DAYS, 15)), axis=0))
    closes = pd.DataFrame(walk, index=INDEX, columns=list(CFG.symbols))
    opens = closes * 1.001  # opens sit a little above the previous-day-like close
    return closes, opens


class ScriptedLLM(FakeLLM):
    """Always proposes the baseline of the (anonymised) universe it is shown."""

    def __init__(self, answers: list[str] | None = None) -> None:
        super().__init__(answers or [])
        self.sent: list[str] = []

    def complete(self, messages: list[dict[str, str]], schema: dict[str, Any]) -> Any:
        self.sent.append(messages[-1]["content"])
        payload = json.loads(messages[-1]["content"])
        weights = {a["symbol"]: a["baseline_weight"] for a in payload["assets"]}
        self.answers = [as_json(proposal(weights=weights))]
        self.calls = []
        return super().complete(messages, schema)


def replay(tmp_path: Path, llm: ScriptedLLM | None = None, sessions: Any = None) -> Any:
    closes, opens = history()
    store = FileAuditStore(tmp_path)
    llm = llm or ScriptedLLM()
    result = run_replay(
        closes=closes, opens=opens, sessions=sessions or SESSIONS, cfg=CFG,
        model=FakeTimestral([0.02] * 15), llm=llm, store=store,
    )  # fmt: skip
    return result, store, llm


# --------------------------------------------------------------------------- anonymisation


def test_aliases_are_a_shuffled_bijection_that_keeps_type_and_group() -> None:
    alias_cfg, mapping = anonymise(CFG, seed=0)
    assert sorted(mapping.values()) == [f"A{i:02d}" for i in range(1, 16)]
    assert list(mapping.values()) != sorted(mapping.values())  # the order does not leak
    for real, alias in mapping.items():
        original, renamed = CFG.asset(real), alias_cfg.asset(alias)
        assert (renamed.type, renamed.group) == (original.type, original.group)
        assert renamed.name == f"Asset {alias}"
    assert alias_cfg.benchmark == mapping["SPY"]
    assert anonymise(CFG, seed=0)[1] == mapping and anonymise(CFG, seed=1)[1] != mapping


def test_the_llm_sees_no_real_ticker_name_date_or_price_level(tmp_path: Path) -> None:
    _, _, llm = replay(tmp_path)
    prompt_payload = llm.sent[0]
    for asset in CFG.universe:
        assert not re.search(rf"\b{asset.symbol}\b", prompt_payload), asset.symbol
        assert asset.name not in prompt_payload
    assert "2026" not in prompt_payload and "2025" not in prompt_payload
    assert '"latest_price": null' in prompt_payload  # no live price in a replay
    assert re.search(r'"as_of": "21\d\d-', prompt_payload)  # the synthetic calendar


# --------------------------------------------------------------------------- point in time


def test_a_decision_only_receives_closes_before_its_session() -> None:
    closes, _ = history()
    session = INDEX[-3]
    window = point_in_time(closes, session, N_CONTEXT)
    assert len(window) == N_CONTEXT and window.index[-1] < session
    assert window.index[-1] == INDEX[-4]


def test_not_enough_history_is_an_error() -> None:
    closes, _ = history()
    with pytest.raises(ReplayError, match="sessions before"):
        point_in_time(closes, INDEX[5], N_CONTEXT)


def test_changing_the_future_does_not_change_a_decision(tmp_path: Path) -> None:
    closes, opens = history()
    altered = closes.copy()
    altered.loc[altered.index >= SESSIONS[-1]] *= 3.0  # the last session and after: rewritten
    first = FileAuditStore(tmp_path / "a")
    second = FileAuditStore(tmp_path / "b")
    for store, c in ((first, closes), (second, altered)):
        run_replay(
            closes=c, opens=opens, sessions=SESSIONS[:-1], cfg=CFG,
            model=FakeTimestral([0.02] * 15), llm=ScriptedLLM(), store=store,
        )  # fmt: skip
    for a, b in zip(first.read_runs(), second.read_runs(), strict=True):
        assert a["portfolios"] == b["portfolios"] and a["messages"] == b["messages"]


# --------------------------------------------------------------------------- simulated fills


def test_fills_happen_at_the_open_plus_a_cost() -> None:
    pf = Portfolio()
    opens = dict.fromkeys(CFG.symbols, 100.0)
    orders = execute_at_open(pf, BASELINE, opens, CFG, "r")
    assert len(orders) == 15 and {o.status for o in orders} == {"replay_filled"}
    bought = sum(pf.shares.values()) * 100.0
    assert bought == pytest.approx(0.90 * 100_000.0)  # first deployment, not throttled
    assert pf.cash == pytest.approx(100_000.0 - bought * (1 + COST_BPS / 1e4))
    assert pf.value(opens) == pytest.approx(100_000.0 - bought * COST_BPS / 1e4)  # only the cost


def test_sells_free_cash_and_pay_the_cost_too() -> None:
    pf = Portfolio()
    opens = dict.fromkeys(CFG.symbols, 100.0)
    execute_at_open(pf, BASELINE, opens, CFG, "r1")
    before = pf.value(opens)
    execute_at_open(pf, {**BASELINE, "SPY": 0.0, "TLT": 0.12}, opens, CFG, "r2")
    assert pf.shares["SPY"] == pytest.approx(0.0, abs=1e-6) and pf.cash > 0
    assert pf.value(opens) < before  # costs only


def test_portfolio_weights_and_value() -> None:
    pf = Portfolio(cash=500.0, shares={"A": 2.0})
    assert pf.value({"A": 100.0}) == 700.0
    assert pf.weights(["A", "B"], {"A": 100.0, "B": 5.0}) == {
        "A": pytest.approx(200 / 700),
        "B": 0.0,
    }


# --------------------------------------------------------------------------- the replay itself


def test_one_record_per_session_with_real_dates_and_alias_symbols(tmp_path: Path) -> None:
    result, store, _ = replay(tmp_path)
    assert (result.sessions, result.resumed, result.execution_errors) == (4, 0, 0)
    runs = sorted(store.read_runs(), key=lambda r: r["run_id"])
    assert [r["run_id"] for r in runs] == [f"{s.date()}_morning" for s in SESSIONS]
    first = runs[0]
    assert first["started_at"].startswith(str(SESSIONS[0].date()))
    assert first["data"]["last_session"] == str(INDEX[-5].date())
    assert all(re.fullmatch(r"A\d\d", s) for s in first["portfolios"]["final"])
    assert first["execution_status"] == "dry_run" and first["config"]["benchmark"].startswith("A")
    assert {o["status"] for o in first["orders"]} == {"replay_filled"}
    assert first["reasoning_file"] == f"{SESSIONS[0].date()}_morning_reasoning.txt"
    assert (tmp_path / "tables" / "decisions.csv").exists()


def test_the_portfolio_is_marked_at_the_previous_close_and_grows_from_cash(tmp_path: Path) -> None:
    result, store, _ = replay(tmp_path)
    assert result.equity[0] == 100_000.0  # nothing held before the first decision
    runs = sorted(store.read_runs(), key=lambda r: r["run_id"])
    assert sum(runs[0]["portfolios"]["current"].values()) == 0.0
    assert sum(runs[1]["portfolios"]["current"].values()) == pytest.approx(0.87, abs=0.05)


def test_observations_cover_the_decision_period_with_alias_symbols(tmp_path: Path) -> None:
    _, store, _ = replay(tmp_path)
    obs = store.read_observations()
    assert obs and all(re.fullmatch(r"A\d\d", s) for s in next(iter(obs[-1]["closes"].values())))
    assert obs[-1]["as_of"] == str(INDEX[-1].date())


def test_a_rerun_resumes_without_calling_the_llm_and_keeps_the_same_state(tmp_path: Path) -> None:
    first, _store, _ = replay(tmp_path)
    silent = ScriptedLLM()
    again, _, llm = replay(tmp_path, llm=silent)
    assert again.resumed == 4 and llm.sent == []  # nothing new was asked
    assert again.equity == pytest.approx(first.equity)


def test_a_partial_replay_continues_where_it_stopped(tmp_path: Path) -> None:
    _, store, _ = replay(tmp_path, sessions=SESSIONS[:2])
    result, _, llm = replay(tmp_path)
    assert result.resumed == 2 and len(llm.sent) == 2
    assert len(store.read_runs()) == 4


def test_a_rejected_proposal_holds_the_positions_in_the_replay(tmp_path: Path) -> None:
    llm = ScriptedLLM()
    llm.complete = lambda messages, schema: FakeLLM([BROKEN] * 2).complete(messages, schema)  # type: ignore[method-assign]
    result, store, _ = replay(tmp_path, llm=llm, sessions=SESSIONS[:2])
    runs = store.read_runs()
    assert {r["decision"]["outcome"] for r in runs} == {"fallback_hold"}
    assert all(r["orders"] == [] for r in runs) and result.equity == [100_000.0, 100_000.0]


def test_an_unsafe_plan_is_logged_and_nothing_is_traded(tmp_path: Path, monkeypatch: Any) -> None:
    from agent.evaluation import replay as module
    from agent.execution.execute import ExecutionError

    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise ExecutionError("no safe plan: test")

    monkeypatch.setattr(module, "execute_at_open", refuse)
    logs: list[str] = []
    closes, opens = history()
    result = run_replay(
        closes=closes, opens=opens, sessions=SESSIONS[:2], cfg=CFG,
        model=FakeTimestral([0.02] * 15), llm=ScriptedLLM(), store=FileAuditStore(tmp_path),
        log=logs.append,
    )  # fmt: skip
    assert result.execution_errors == 2 and any("no safe plan" in m for m in logs)


# --------------------------------------------------------------------------- report and probe


def test_the_report_reuses_the_live_metrics_with_the_alias_benchmark(tmp_path: Path) -> None:
    _, store, _ = replay(tmp_path)
    report = replay_report(store)
    assert report["sessions"] == 4
    spy = report["series"]["spy"]
    assert spy["sessions"] == 4 and spy["cumulative_return"] != 0.0  # the benchmark is found
    assert len(report["comparisons"]) == 4


def test_the_markdown_report_states_the_rules_and_the_evidence(tmp_path: Path) -> None:
    _, store, _ = replay(tmp_path)
    manifest = json.loads(write_manifest(
        tmp_path, anonymise(CFG)[1], CFG, 0, COST_BPS, SESSIONS).read_text())  # fmt: skip
    probes = [{"session": "2026-10-01", "correct_assets": 3, "n_assets": 15,
               "period_guess": "2026-10", "period_ok": True}]  # fmt: skip
    text = render_markdown(replay_report(store), manifest, probes)
    for fragment in ("Historical replay (D-015)", "point-in-time data enforced in code",
                     "5.0 bps per side", "Timestral rule", "3/15", "Leakage probe",
                     "fewer than 10 sessions", "Caveats"):  # fmt: skip
        assert fragment in text, fragment
    assert manifest["alias_to_real"] and manifest["sessions"] == 4
    assert "not run" in render_markdown(replay_report(store), manifest, [])


def test_the_report_renders_with_intervals_and_confidence_when_there_is_enough_data() -> None:
    manifest = {"first_session": "a", "last_session": "b", "cost_bps_per_side": 5.0,
                "llm": {"model": "m", "prompt": "p", "reasoning_effort": "high"},
                "timestral": "t"}  # fmt: skip
    stats = {"cumulative_return": 0.02, "annualised_volatility": 0.1, "sharpe": 1.2,
             "max_drawdown": -0.03}  # fmt: skip
    series = {k: dict(stats) for k in ("agent", "llm_proposal", "baseline", "equal_weight", "spy")}
    report = {
        "sessions": 40, "account_return": 0.01, "series": series,
        "comparisons": [{"label": "x", "cumulative_difference": 0.01, "sessions": 40,
                         "ci95_mean_daily": (0.0001, 0.0009), "conclusive": True}],
        "risk_calibration": [{"horizon": 1, "n": 30, "inside_80": 0.8, "rms_z": 1.0}],
        "confidence": {"n": 50, "mean_confidence": 0.6, "hit_rate": 0.5, "brier": 0.26},
    }  # fmt: skip
    text = render_markdown(report, manifest, [])
    assert "[+0.010, +0.090] pts/day" in text and "| yes |" in text
    assert "50 tilts resolved" in text and "+1.20%" not in text and "1.20" in text


def test_the_probe_scores_what_the_llm_could_identify() -> None:
    mapping = {"SPY": "A01", "TLT": "A02", "NVDA": "A03"}
    answers = [{"alias": "A01", "ticker": "spy"}, {"alias": "A02", "ticker": "IEF"},
               {"alias": "A03", "ticker": None}]  # fmt: skip
    good = json.dumps({"assets": answers, "period": "2026-09"})
    scored = score_probe(good, mapping, "2026-10")
    assert scored["correct_assets"] == 1 and scored["guessed_assets"] == 2
    assert scored["period_ok"] is True  # same year
    bad = score_probe("not json", mapping, "2026-10")
    assert bad == {"parsed": False, "correct_assets": 0, "n_assets": 3, "period_ok": False}
    assert (
        score_probe(json.dumps({"assets": [], "period": None}), mapping, "2026-10")["period_ok"]
        is False
    )


def test_the_probe_sends_the_same_anonymised_payload_and_records_the_answers() -> None:
    closes, _ = history()

    class Prober(FakeLLM):
        def complete(self, messages: list[dict[str, str]], schema: dict[str, Any]) -> Any:
            self.calls.append(messages)
            _, mapping = anonymise(CFG)
            answer = json.dumps({"assets": [{"alias": a, "ticker": s} for s, a in mapping.items()],
                                 "period": "2026-11"})  # fmt: skip
            return (
                super().complete(messages, schema) if False else type("R", (), {"text": answer})()
            )

    llm = Prober([])
    out = run_probe(closes=closes, sessions=SESSIONS[:2], cfg=CFG,
                    model=FakeTimestral([0.02] * 15), llm=llm)  # type: ignore[arg-type]  # fmt: skip
    assert [p["session"] for p in out] == [str(s.date()) for s in SESSIONS[:2]]
    assert all(p["correct_assets"] == 15 and p["period_ok"] for p in out)  # a leaky LLM
    sent = llm.calls[0][-1]["content"]
    assert "SPY" not in sent and re.search(r'"as_of": "21\d\d-', sent)
    assert "identify each asset" in llm.calls[0][0]["content"]


def test_record_observations_is_idempotent(tmp_path: Path) -> None:
    closes, _ = history()
    store = FileAuditStore(tmp_path)
    args = (store, closes.rename(columns=anonymise(CFG)[1]), INDEX[-3], INDEX[-1])
    record_observations(*args)
    record_observations(*args)
    assert [o["as_of"] for o in store.read_observations()] == [str(d.date()) for d in INDEX[-3:]]


def test_defaults_match_the_decision() -> None:
    assert AgentConfig().benchmark == "SPY" and COST_BPS == 5.0


def test_an_unsafe_plan_without_a_logger_is_simply_skipped(
    tmp_path: Path, monkeypatch: Any
) -> None:
    from agent.evaluation import replay as module
    from agent.execution.execute import ExecutionError

    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise ExecutionError("no safe plan: test")

    monkeypatch.setattr(module, "execute_at_open", refuse)
    closes, opens = history()
    result = run_replay(
        closes=closes, opens=opens, sessions=SESSIONS[:1], cfg=CFG,
        model=FakeTimestral([0.02] * 15), llm=ScriptedLLM(), store=FileAuditStore(tmp_path),
    )  # fmt: skip
    assert result.execution_errors == 1


def test_an_llm_outage_stops_the_replay_without_recording_a_decision(tmp_path: Path) -> None:
    class Down(ScriptedLLM):
        def complete(self, messages: list[dict[str, str]], schema: dict[str, Any]) -> Any:
            raise ConnectionError("Mistral API unreachable")

    closes, opens = history()
    store = FileAuditStore(tmp_path)
    with pytest.raises(ConnectionError):
        run_replay(
            closes=closes, opens=opens, sessions=SESSIONS, cfg=CFG,
            model=FakeTimestral([0.02] * 15), llm=Down(), store=store,
        )  # fmt: skip
    assert store.read_runs() == []  # resumable: nothing half-done was recorded
