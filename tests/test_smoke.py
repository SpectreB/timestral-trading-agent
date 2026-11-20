"""Smoke test logic, offline: every way an external service can break must be reported."""

from __future__ import annotations

import json

import pytest

from agent.decide import LLMResponse
from agent.execute import ExecutionError
from agent.smoke import SmokeError, smoke_check
from tests.helpers import CFG, FakeBroker, FakeMarketData, FakeTimestral, make_closes
from tests.test_decide import NOW

CLOSES = make_closes(520)


class PingLLM:
    def __init__(self, text: str = '{"ok": true}', model: str = CFG.llm.model) -> None:
        self.text, self.model = text, model

    def complete(self, messages: list[dict[str, str]], schema: dict[str, object]) -> LLMResponse:
        return LLMResponse(
            text=self.text, reasoning="", model=self.model, prompt_tokens=10, completion_tokens=5
        )


def check(broker: FakeBroker | None = None, llm: PingLLM | None = None) -> dict[str, object]:
    return smoke_check(
        now=NOW,
        cfg=CFG,
        market=FakeMarketData(CLOSES),
        broker=broker or FakeBroker(),
        model=FakeTimestral([0.02] * 15),
        llm=llm or PingLLM(),
    )


def test_all_services_healthy() -> None:
    summary = check()
    assert summary["n_prices"] == 15
    assert summary["llm_model"] == "mistral-large-4-0"
    assert summary["last_session"] == "2026-11-20"
    json.dumps(summary)  # printable as one line


def test_a_non_paper_account_fails_the_smoke_test() -> None:
    with pytest.raises(ExecutionError, match="paper"):
        check(broker=FakeBroker(is_paper=False))


@pytest.mark.parametrize("bad", [float("nan"), 0.0, -1.0])
def test_invalid_live_prices_fail(bad: float) -> None:
    prices = dict.fromkeys(CFG.symbols, 100.0)
    prices["NVDA"] = bad
    with pytest.raises(SmokeError, match="NVDA"):
        check(broker=FakeBroker(prices=prices))


def test_a_silent_model_alias_change_is_detected() -> None:
    with pytest.raises(SmokeError, match="answered with mistral-large-5"):
        check(llm=PingLLM(model="mistral-large-5"))


@pytest.mark.parametrize("text", ["not json", "{}", "[1]", '{"ok": false}', '{"ok": "yes"}'])
def test_a_bad_llm_answer_fails(text: str) -> None:
    with pytest.raises(SmokeError):
        check(llm=PingLLM(text=text))
