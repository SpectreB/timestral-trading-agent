"""Decision of the tool agent, offline (D-018): exploration, budget, guardrails, failures."""

from __future__ import annotations

import json
from typing import Any

import pytest

from agent.config import AgentConfig, ToolConfig, tool_agent_config
from agent.decision.decide import LLMResponse, LLMStep, ToolCall, load_system_prompt
from agent.decision.decide_tools import FINAL_INSTRUCTION, decide_agent, decide_with_tools
from tests.helpers import BASELINE, FakeTimestral, proposal
from tests.test_decide import GOOD, FakeLLM, make_input

CFG = tool_agent_config()
EVALUATE = json.dumps({"weights": [{"symbol": "TLT", "weight": 0.2}], "horizon": 5})


def step(*calls: tuple[str, str], text: str = "") -> LLMStep:
    return LLMStep(
        text=text,
        reasoning="exploring...",
        tool_calls=[ToolCall(id=f"c{i}", name=n, arguments=a) for i, (n, a) in enumerate(calls)],
        model="fake",
        prompt_tokens=10,
        completion_tokens=5,
    )


class FakeToolLLM:
    """Scripted exploration turns, then scripted final answers; records what it was sent."""

    def __init__(self, steps: list[LLMStep | Exception], answers: list[str | Exception]) -> None:
        self.steps = steps
        self.answers = answers
        self.step_calls: list[list[dict[str, Any]]] = []
        self.finish_calls: list[list[dict[str, Any]]] = []
        self.tools: list[dict[str, Any]] = []

    def step(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> LLMStep:
        self.step_calls.append(list(messages))
        self.tools = tools
        nxt = (
            self.steps[len(self.step_calls) - 1]
            if len(self.step_calls) <= len(self.steps)
            else step()
        )
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    def finish(
        self, messages: list[dict[str, Any]], schema: dict[str, Any], tools: list[dict[str, Any]]
    ) -> LLMResponse:
        self.finish_calls.append(list(messages))
        answer = self.answers[len(self.finish_calls) - 1]
        if isinstance(answer, Exception):
            raise answer
        return LLMResponse(
            text=answer, reasoning="deciding...", model="fake", prompt_tokens=100,
            completion_tokens=50,
        )  # fmt: skip

    def complete(self, messages: list[dict[str, Any]], schema: dict[str, Any]) -> LLMResponse:
        raise AssertionError("the tool agent answers through finish()")


def run(llm: FakeToolLLM, cfg: AgentConfig = CFG, **inp: Any) -> Any:
    return decide_with_tools(make_input(**inp), llm, FakeTimestral([0.02]), cfg)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- configuration


def test_the_tool_agent_differs_only_where_decided() -> None:
    base = AgentConfig()
    assert CFG.agent == "tools" and base.agent == "pipeline"
    assert CFG.forecast.model_id == "mistralai/Timestral-209M"
    assert CFG.llm.prompt_version == "decide_tools_v2"
    assert CFG.risk.max_increase_vs_baseline == 1.0  # option B: no anchor on the baseline
    # Every absolute limit is the pipeline agent's.
    for field in ("min_cash", "max_invested", "max_weight_etf", "max_weight_stock",
                  "max_weight_group", "max_corrected_assets", "max_total_correction"):  # fmt: skip
        assert getattr(CFG.risk, field) == getattr(base.risk, field)
    assert CFG.drawdown_levels == base.drawdown_levels
    assert CFG.execution == base.execution
    assert CFG.llm.model == base.llm.model


def test_the_tools_prompt_states_the_limits_and_the_tools() -> None:
    prompt = load_system_prompt(CFG)
    assert "{" not in prompt.replace("{symbol, weight}", "")
    for fragment in ("Cash >= 10%", "each single stock <= 10%", "get_forecasts",
                     "evaluate_portfolio", "get_baseline", "at most 12 tool calls over 6 turns",
                     "At most 30% of the portfolio", "no reliable directional skill"):  # fmt: skip
        assert fragment in prompt
    assert "15 percentage points" not in prompt


# --------------------------------------------------------------------------- exploration


def test_tool_calls_are_answered_then_the_decision_goes_through_the_guardrails() -> None:
    llm = FakeToolLLM(
        [step(("get_forecasts", '{"symbols": [], "horizon": 5}'), ("get_baseline", "{}")),
         step(("evaluate_portfolio", EVALUATE)),
         step(text="I have what I need.")],
        [GOOD],
    )  # fmt: skip
    decision = run(llm)
    assert decision.outcome == "accepted"
    assert [c["name"] for c in decision.tool_calls] == [
        "get_forecasts",
        "get_baseline",
        "evaluate_portfolio",
    ]
    assert len(decision.steps) == 3
    # The first message carries no forecast and no baseline: the LLM must ask for them.
    payload = json.loads(llm.step_calls[0][1]["content"])
    assert "forecast" not in payload["assets"][0]
    assert "baseline_weight" not in payload["assets"][0]
    assert payload["forecast_horizons_trading_days"] == [1, 2, 3, 4, 5]
    # The second turn sees the assistant's calls and one tool message per call.
    second = llm.step_calls[1]
    assert second[2]["role"] == "assistant" and len(second[2]["tool_calls"]) == 2
    assert [m["role"] for m in second[3:]] == ["tool", "tool"]
    assert second[3]["tool_call_id"] == "c0" and "assets" in json.loads(second[3]["content"])
    # The final request keeps the closing text and asks for the decision.
    final = llm.finish_calls[0]
    assert final[-2] == {"role": "assistant", "content": "I have what I need."}
    assert final[-1] == {"role": "user", "content": FINAL_INSTRUCTION}
    assert [t["function"]["name"] for t in llm.tools] == [
        "get_forecasts",
        "evaluate_portfolio",
        "get_baseline",
    ]


def test_no_baseline_bound_but_every_absolute_cap_applies() -> None:
    # +19 pts on TLT above its baseline: allowed for the tool agent, within the 25% ETF cap.
    tilted = proposal(TLT=0.25, SPY=0.0, QQQ=0.0, IWM=0.0, EFA=0.01)
    decision = run(FakeToolLLM([step()], [json.dumps(tilted)]))
    assert decision.outcome == "accepted"
    assert decision.target_weights["TLT"] == pytest.approx(0.25)
    # An absolute cap still clips: a single stock above 10%.
    capped = run(FakeToolLLM([step()], [json.dumps(proposal(NVDA=0.14, SPY=0.0, QQQ=0.0))]))
    assert capped.outcome == "corrected"
    assert capped.target_weights["NVDA"] == pytest.approx(0.10)


def test_exploration_stops_after_max_rounds() -> None:
    cfg = tool_agent_config()
    looping = [step(("get_baseline", "{}")) for _ in range(10)]
    llm = FakeToolLLM(looping, [GOOD])  # type: ignore[arg-type]
    decision = run(llm, cfg)
    assert len(llm.step_calls) == cfg.tools.max_rounds
    assert len(decision.tool_calls) == cfg.tools.max_rounds
    assert decision.outcome == "accepted"


def test_exploration_stops_when_the_call_budget_is_spent() -> None:
    from agent.config import evolve

    cfg = evolve(CFG, tools=ToolConfig(max_rounds=6, max_calls=3))
    burst = step(*[("get_baseline", "{}")] * 4)
    llm = FakeToolLLM([burst, burst], [GOOD])
    decision = run(llm, cfg)
    assert len(llm.step_calls) == 1  # budget spent within the first turn
    assert "budget exhausted" in decision.tool_calls[-1]["result"]["error"]


def test_a_rejected_answer_is_retried_then_held() -> None:
    llm = FakeToolLLM([step()], ['{"broken', '{"broken'])
    decision = run(llm, current=dict(BASELINE))
    assert decision.outcome == "fallback_hold"
    assert decision.target_weights == BASELINE
    assert len(llm.finish_calls) == 2
    assert "rejected by the guardrails" in llm.finish_calls[1][-1]["content"]


# --------------------------------------------------------------------------- failures


def test_an_llm_failure_while_exploring_goes_to_the_final_answer() -> None:
    llm = FakeToolLLM([TimeoutError("read timed out")], [GOOD])
    decision = run(llm)
    assert decision.outcome == "accepted"
    assert decision.steps[0].reasoning.startswith("LLM call failed: TimeoutError")


def test_an_llm_outage_ends_in_a_recorded_hold() -> None:
    llm = FakeToolLLM([ConnectionError("down")], [ConnectionError("down")] * 2)
    decision = run(llm, current=dict(BASELINE))
    assert decision.outcome == "fallback_hold"
    assert [a.guardrails.rejection_reason for a in decision.attempts] == ["llm_call_failed"] * 2


def test_the_replay_raises_on_an_outage_instead_of_holding() -> None:
    llm = FakeToolLLM([ConnectionError("down")], [GOOD])
    with pytest.raises(ConnectionError):
        decide_with_tools(make_input(), llm, FakeTimestral([0.02]), CFG, on_llm_failure="raise")  # type: ignore[arg-type]


# --------------------------------------------------------------------------- dispatch


def test_decide_agent_dispatches_on_the_agent_kind() -> None:
    pipeline = decide_agent(make_input(), FakeLLM([GOOD]), None, AgentConfig())
    assert pipeline.outcome == "accepted" and pipeline.tool_calls == []
    tools = decide_agent(make_input(), FakeToolLLM([step()], [GOOD]), FakeTimestral([0.02]), CFG)  # type: ignore[arg-type]
    assert tools.outcome == "accepted"


def test_decide_agent_refuses_an_impossible_setup() -> None:
    from agent.config import evolve

    with pytest.raises(TypeError, match="tool calls"):
        decide_agent(make_input(), FakeLLM([GOOD]), FakeTimestral([0.02]), CFG)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="Timestral model"):
        decide_agent(make_input(), FakeToolLLM([], [GOOD]), None, CFG)
    with pytest.raises(ValueError, match="'pipeline' or 'tools'"):
        evolve(CFG, agent="oracle")
