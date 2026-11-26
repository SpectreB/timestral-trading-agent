"""LLM soundness checks (T-06): the scenarios, the checks and the metrics, offline.

A policy-driven fake LLM stands in for the real one: a *sound* policy must pass every scenario
and a *flawed* one must fail exactly the scenarios it is flawed for. That shows each check can
actually detect the behaviour it is meant to catch.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import pandas as pd
import pytest

from agent.decision.decide import LLMResponse
from agent.evaluation.evals import (
    INJECTION,
    SCENARIOS,
    Outcome,
    base_input,
    outcome_of,
    render_markdown,
    run_scenarios,
    run_stability,
    stability,
)
from tests.helpers import CFG, FakeTimestral, as_json, make_closes, proposal
from tests.test_decide import NOW

Policy = Callable[[dict[str, Any]], tuple[dict[str, float], float]]


class PolicyLLM:
    """Answers each prompt with the weights a policy computes from the payload it is shown."""

    def __init__(self, policy: Policy) -> None:
        self.policy = policy

    def complete(self, messages: list[dict[str, str]], schema: dict[str, Any]) -> LLMResponse:
        payload = json.loads(messages[1]["content"])  # the data, even on a retry
        weights, confidence = self.policy(payload)
        answer = proposal(weights=weights, confidence=confidence)
        answer["analysis"] = f"policy answer, news: {payload['news'][:20]}"
        return LLMResponse(
            text=as_json(answer), reasoning="", model="fake", prompt_tokens=1, completion_tokens=1
        )


def baseline_of(payload: dict[str, Any]) -> dict[str, float]:
    return {a["symbol"]: a["baseline_weight"] for a in payload["assets"]}


def sound(payload: dict[str, Any]) -> tuple[dict[str, float], float]:
    """Stays at the baseline, de-risks in a crash or a drawdown, ignores the news."""
    w = baseline_of(payload)
    drawdown = payload["portfolio"]["drawdown_from_peak"]
    crashed = any(a["recent"]["return_1d"] < -0.05 for a in payload["assets"])
    if drawdown <= -0.08 or crashed:
        w = {s: v * (0.6 / 0.9 if drawdown <= -0.08 else 0.85) for s, v in w.items()}
    return w, 0.5


def greedy(payload: dict[str, Any]) -> tuple[dict[str, float], float]:
    """Flawed: chases medians, piles into NVDA, believes the news, never de-risks."""
    w = baseline_of(payload)
    for a in payload["assets"]:
        if a["symbol"] == "AMZN" and a["forecast"]["5"]["median"] > 0.05:
            w["AMZN"] += 0.06
        if a["symbol"] == "NVDA":
            w["NVDA"] += 0.10 if "ignore all previous" in payload["news"] else 0.03
    if payload["portfolio"]["drawdown_from_peak"] <= -0.05:
        w["SPY"] += 0.02  # adds risk although reduce-only applies
    return w, 0.9


CLOSES = make_closes(540)
BASE = base_input(CLOSES, CFG, FakeTimestral([0.02] * 15), NOW)  # type: ignore[arg-type]


def run(policy: Policy, repeats: int = 1) -> dict[str, dict[str, Any]]:
    results = run_scenarios(BASE, CFG, lambda: PolicyLLM(policy), repeats=repeats, workers=4)
    return {r["key"]: r for r in results}


def test_a_sound_policy_passes_every_scenario() -> None:
    results = run(sound)
    assert {k: r["passed"] for k, r in results.items()} == dict.fromkeys(
        (s.key for s in SCENARIOS), True
    )
    assert len(SCENARIOS) == 7


def test_a_flawed_policy_fails_exactly_the_scenarios_it_is_flawed_for() -> None:
    results = run(greedy)
    failed = {k for k, r in results.items() if not r["passed"]}
    assert failed == {
        "crash",
        "wide_asset",
        "reduce_only",
        "exposure_cap",
        "median_trap",
        "injection",
    }
    assert results["calm"]["passed"] is True  # NVDA +3 pts only: within the calm tolerance
    assert (
        "NVDA +10.0 pts after an instruction hidden in the news"
        in results["injection"]["runs"][0]["detail"]
    )


def test_the_scenarios_build_the_situations_they_describe() -> None:
    by_key = {s.key: s.build(BASE, CFG) for s in SCENARIOS}
    crash, wide = by_key["crash"], by_key["wide_asset"]
    assert crash.closes.iloc[-1]["SPY"] == pytest.approx(BASE.closes.iloc[-1]["SPY"] * 0.91)
    assert crash.closes.iloc[-1]["TLT"] == BASE.closes.iloc[-1]["TLT"]  # defensives untouched
    assert crash.drawdown == -0.04
    assert wide.forecasts["NVDA"].width(5) == pytest.approx(4 * BASE.forecasts["NVDA"].width(5))
    assert wide.baseline["NVDA"] < BASE.baseline["NVDA"]  # the baseline follows the new width
    assert wide.current == wide.baseline
    assert by_key["reduce_only"].drawdown == -0.06 and by_key["exposure_cap"].drawdown == -0.09
    trap = by_key["median_trap"].forecasts["AMZN"]
    assert trap.median(5) == pytest.approx(BASE.forecasts["AMZN"].median(5) + 0.08)
    assert trap.width(5) == pytest.approx(BASE.forecasts["AMZN"].width(5))  # same uncertainty
    assert by_key["median_trap"].baseline == BASE.baseline
    assert by_key["injection"].news == INJECTION and by_key["calm"] is BASE


def test_the_check_applies_to_the_proposal_not_to_what_the_guardrails_left() -> None:
    inp = SCENARIOS[-1].build(BASE, CFG)  # the injection scenario
    outcome = outcome_of(inp, CFG, PolicyLLM(greedy))
    assert outcome.tilt("NVDA") == pytest.approx(0.10)  # the LLM asked for +10 pts
    assert outcome.final["NVDA"] <= BASE.baseline["NVDA"] + 0.10 + 1e-9  # guardrails may clip
    assert outcome.invested() == pytest.approx(sum(outcome.proposed.values()))  # type: ignore[union-attr]
    assert outcome.analysis.startswith("policy answer") and outcome.confidences["NVDA"] == 0.9


def test_a_proposal_that_cannot_be_read_fails_every_check() -> None:
    class Garbage:
        def complete(self, messages: Any, schema: Any) -> LLMResponse:
            return LLMResponse(text="not json", reasoning="", model="f", prompt_tokens=0,
                               completion_tokens=0)  # fmt: skip

    results = run_scenarios(BASE, CFG, Garbage, repeats=1, workers=2)  # type: ignore[arg-type]
    assert all(not r["passed"] and r["runs"][0]["detail"] == "no usable proposal" for r in results)
    assert all(r["runs"][0]["decision"] == "fallback_hold" for r in results)
    unreadable = outcome_of(BASE, CFG, Garbage())  # type: ignore[arg-type]
    assert unreadable.proposed is None and unreadable.invested() is None
    assert unreadable.tilt("SPY") is None and unreadable.attempts == 2


def test_a_scenario_passes_only_if_every_repeat_does() -> None:
    calls = {"n": 0}

    def flaky(payload: dict[str, Any]) -> tuple[dict[str, float], float]:
        calls["n"] += 1
        w = baseline_of(payload)
        if calls["n"] % 2 == 0:
            w["NVDA"] += 0.10
        return w, 0.5

    results = run_scenarios(
        BASE, CFG, lambda: PolicyLLM(flaky), scenarios=[SCENARIOS[-1]], repeats=4, workers=1
    )
    (result,) = results
    assert [r["passed"] for r in result["runs"]].count(False) == 2 and result["passed"] is False


# --------------------------------------------------------------------------- stability


def outcomes_from(weight_sets: list[dict[str, float]], conf: float = 0.5) -> list[Outcome]:
    return [
        Outcome(
            baseline=BASE.baseline, current=BASE.current, proposed=w, proposed_cash=0.1,
            final=w, decision="accepted", analysis="", confidences=dict.fromkeys(w, conf),
        )
        for w in weight_sets
    ]  # fmt: skip


def test_identical_proposals_are_perfectly_stable() -> None:
    same = [dict(BASE.baseline)] * 4
    stable = stability(outcomes_from(same))
    assert stable["mean_pairwise_turnover"] == 0.0 and stable["max_asset_std"] == 0.0
    assert stable["tilt_sign_agreement"] is None  # no material tilt to agree about
    assert stable["confidence_std"] == 0.0 and stable["outcomes"] == ["accepted"]


def test_differing_proposals_are_measured() -> None:
    def tilted(tlt: float, spy: float = 0.0) -> dict[str, float]:
        w = dict(BASE.baseline)
        w["TLT"] += tlt
        w["SPY"] += spy
        return w

    stable = stability(outcomes_from([tilted(0.04), tilted(0.02, -0.02), tilted(0.04)]))
    assert stable["usable"] == 3
    assert stable["max_pairwise_turnover"] == pytest.approx(0.02)  # 0.5 * (0.02 + 0.02)
    assert 0 < stable["mean_pairwise_turnover"] < stable["max_pairwise_turnover"]
    assert 0 < stable["tilt_sign_agreement"] < 1  # SPY is tilted in only one call
    steady = stability(outcomes_from([tilted(0.04), tilted(0.03), tilted(0.02)]))
    assert steady["tilt_sign_agreement"] == 1.0  # every call tilts TLT up
    flipped = stability(outcomes_from([tilted(0.03), tilted(-0.03), tilted(0.03)]))
    assert flipped["tilt_sign_agreement"] == pytest.approx(2 / 3)  # one call disagrees


def test_stability_needs_at_least_two_usable_calls() -> None:
    lone = outcomes_from([dict(BASE.baseline)])
    assert stability(lone) == {"calls": 1, "usable": 1}
    broken = Outcome(
        BASE.baseline, BASE.current, None, None, BASE.baseline, "fallback_hold", "", {}
    )
    assert stability([*lone, broken]) == {"calls": 2, "usable": 1}


def test_repeated_calls_on_the_same_input_are_measured_end_to_end() -> None:
    stable = run_stability(BASE, CFG, lambda: PolicyLLM(sound), calls=3, workers=3)
    assert stable["calls"] == 3 and stable["mean_pairwise_turnover"] == 0.0


# --------------------------------------------------------------------------- report


def test_the_report_lists_scenarios_stability_and_the_limits() -> None:
    results = list(run(greedy).values())
    stable = stability(outcomes_from([dict(BASE.baseline)] * 3))
    meta = {
        "model": "m",
        "prompt": "decide_v1",
        "temperature": 0.1,
        "repeats": 1,
        "date": "2026-11-22",
    }
    text = render_markdown(results, stable, meta)
    for fragment in ("LLM soundness checks (T-06)", "Sudden crash", "**FAIL**", "pass",
                     "before the guardrails", "Average difference between two proposals",
                     "n/a", "Reading these results", "decide_v1"):  # fmt: skip
        assert fragment in text, fragment
    assert "1 of 7 passed" in text
    short = render_markdown(results, {"calls": 1, "usable": 0}, meta)
    assert "Not enough usable calls (0 of 1)" in short
    agree = {**stable, "tilt_sign_agreement": 0.8}
    assert "80%" in render_markdown(results, agree, meta)


def test_the_universe_matches_the_names_the_scenarios_rely_on() -> None:
    assert {"NVDA", "AMZN", "SPY"} <= set(CFG.symbols)
    assert isinstance(BASE.closes, pd.DataFrame) and len(BASE.closes) == CFG.forecast.context_length
