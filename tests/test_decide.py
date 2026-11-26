"""Decision stage, offline: prompt construction, schema, retry and fallback (D-008, D-010)."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from agent.decision.decide import (
    DecisionInput,
    LLMResponse,
    build_payload,
    decide,
    load_system_prompt,
    response_schema,
)
from agent.decision.schemas import Proposal
from agent.inputs.forecast import forecast_assets
from tests.helpers import ALL_CASH, BASELINE, CFG, FakeTimestral, make_closes, proposal

NOW = datetime(2026, 11, 23, 10, 0, tzinfo=ZoneInfo("America/New_York"))


class FakeLLM:
    """Returns scripted answers in order and records what it was sent."""

    def __init__(self, answers: list[str]) -> None:
        self.answers = answers
        self.calls: list[list[dict[str, str]]] = []

    def complete(self, messages: list[dict[str, str]], schema: dict[str, Any]) -> LLMResponse:
        self.calls.append(messages)
        return LLMResponse(
            text=self.answers[len(self.calls) - 1],
            reasoning="thinking...",
            model="fake",
            prompt_tokens=100,
            completion_tokens=50,
        )


def make_input(current: dict[str, float] | None = None, drawdown: float = 0.0) -> DecisionInput:
    closes = make_closes(64)
    fcs = forecast_assets(closes, FakeTimestral([0.02] * 15), CFG)  # type: ignore[arg-type]
    return DecisionInput(
        now=NOW,
        mode="morning",
        forecasts=fcs,
        baseline=BASELINE,
        current=current or ALL_CASH,
        closes=closes,
        drawdown=drawdown,
        equity=100_000.0,
    )


GOOD = json.dumps(proposal())
TILTED = json.dumps(proposal(TLT=0.10, NVDA=0.02))
BROKEN = '{"analysis": "cut off'


def test_system_prompt_states_the_actual_limits() -> None:
    prompt = load_system_prompt(CFG)
    assert "{" not in prompt  # every placeholder was filled
    for fragment in ("Cash >= 10%", "each single stock <= 10%", "at most 15 percentage points"):
        assert fragment in prompt
    assert "no reliable directional skill" in prompt


def test_schema_puts_analysis_first_and_lists_the_universe() -> None:
    schema = response_schema(CFG)
    assert next(iter(schema["properties"])) == "analysis"
    symbols = schema["properties"]["positions"]["items"]["properties"]["symbol"]["enum"]
    assert symbols == list(CFG.symbols)


def test_schema_and_guardrail_contract_agree() -> None:
    # A proposal shaped like the API schema must parse with the guardrails' contract.
    Proposal.model_validate(proposal())
    assert set(response_schema(CFG)["required"]) == set(Proposal.model_fields)


def test_payload_contains_what_the_llm_needs() -> None:
    payload = build_payload(make_input(), CFG)
    assert payload["last_completed_session"] == "2026-11-20"
    assert len(payload["assets"]) == 15
    spy = payload["assets"][0]
    assert spy["baseline_weight"] == 0.06
    assert set(spy["forecast"]) == {"1", "5"}
    assert set(spy["recent"]) == {
        "return_1d",
        "return_5d",
        "return_20d",
        "volatility_20d_annualised",
    }
    assert payload["portfolio"]["current_cash"] == 1.0
    json.dumps(payload)  # must be serialisable as is


def test_valid_answer_is_used_directly() -> None:
    llm = FakeLLM([TILTED])
    d = decide(make_input(), llm, CFG)
    assert d.outcome == "accepted"
    assert d.target_weights["TLT"] == pytest.approx(0.10)
    assert len(d.attempts) == 1
    assert d.attempts[0].response.reasoning == "thinking..."
    assert d.prompt_version == "decide_v1"


def test_rejected_answer_is_retried_with_the_reason() -> None:
    llm = FakeLLM([BROKEN, GOOD])
    d = decide(make_input(), llm, CFG)
    assert d.outcome == "accepted"
    assert len(d.attempts) == 2
    retry = llm.calls[1]
    assert retry[-2] == {"role": "assistant", "content": BROKEN}
    assert "invalid_json" in retry[-1]["content"]


def test_retry_message_lists_the_needed_corrections() -> None:
    too_much = json.dumps(proposal(SPY=-0.01, IWM=-0.01, EFA=-0.01, EEM=-0.01, cash=0.38))
    llm = FakeLLM([too_much, GOOD])
    decide(make_input(), llm, CFG)
    feedback = llm.calls[1][-1]["content"]
    assert "too_many_corrected_assets" in feedback
    assert "negative_weight SPY" in feedback


def test_two_rejections_fall_back_to_current_positions() -> None:
    llm = FakeLLM([BROKEN, BROKEN])
    current = dict(BASELINE)
    d = decide(make_input(current=current), llm, CFG)
    assert d.outcome == "fallback_hold"
    assert d.target_weights == current
    assert d.cash == pytest.approx(0.10)
    assert len(d.attempts) == 2


def test_corrections_are_reported() -> None:
    d = decide(make_input(), FakeLLM([json.dumps(proposal(NVDA=0.15, SPY=0.01))]), CFG)
    assert d.outcome == "corrected"
    assert d.target_weights["NVDA"] == pytest.approx(0.10)


class FlakyLLM(FakeLLM):
    """Raises on the calls listed in `fail_on` (1-based), answers normally otherwise."""

    def __init__(self, answers: list[str], fail_on: set[int]) -> None:
        super().__init__(answers)
        self.fail_on = fail_on
        self.seen = 0

    def complete(self, messages: list[dict[str, str]], schema: dict[str, Any]) -> LLMResponse:
        self.seen += 1
        if self.seen in self.fail_on:
            self.calls.append(messages)
            raise TimeoutError("The read operation timed out")
        self.answers.insert(len(self.calls), self.answers[0])
        return super().complete(messages, schema)


def test_a_failed_llm_call_is_retried_with_the_same_question() -> None:
    llm = FlakyLLM([GOOD], fail_on={1})
    d = decide(make_input(), llm, CFG)
    assert d.outcome == "accepted" and len(d.attempts) == 2
    first = d.attempts[0]
    assert first.guardrails.rejection_reason == "llm_call_failed"
    assert "TimeoutError" in first.response.reasoning and first.response.text == ""
    assert llm.calls[1] == llm.calls[0]  # no "you were rejected" feedback after an outage


def test_an_unavailable_llm_makes_the_agent_hold_instead_of_crashing() -> None:
    current = dict(BASELINE)
    d = decide(make_input(current=current), FlakyLLM([GOOD], fail_on={1, 2}), CFG)
    assert d.outcome == "fallback_hold" and d.target_weights == current
    assert [a.guardrails.rejection_reason for a in d.attempts] == ["llm_call_failed"] * 2


def test_the_replay_raises_on_an_outage_instead_of_recording_a_hold() -> None:
    with pytest.raises(TimeoutError):
        decide(make_input(), FlakyLLM([GOOD], fail_on={1}), CFG, on_llm_failure="raise")
