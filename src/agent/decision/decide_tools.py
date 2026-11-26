"""Decision stage of the agent that uses Timestral as a tool (D-018).

Flow:
1. The LLM receives its own system prompt and the market context **without** Timestral's
   forecasts or the baseline: it obtains them by calling its tools (`agent.decision.tools`).
2. Exploration: up to `max_rounds` turns in which it may call tools (`max_calls` calls in total).
   Each call is validated, answered and recorded.
3. Final answer: the same strict JSON schema as the pipeline agent, then **the same guardrails**,
   the same retry and the same fallback (hold the current positions), via `final_decision`.

The only guardrail that differs is the one tied to the baseline: with "bounded freedom" (option
B) the baseline is a reference, not an anchor, so the +15 point bound is off in this agent's
config (`tool_agent_config`). Every absolute limit applies unchanged.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from agent.config import AgentConfig
from agent.decision.decide import (
    Decision,
    DecisionInput,
    LLMClient,
    LLMStep,
    ToolLLMClient,
    build_payload,
    decide,
    failure_text,
    final_decision,
    load_system_prompt,
)
from agent.decision.tools import TimestralTools
from agent.inputs.forecast import QuantileModel

FINAL_INSTRUCTION = (
    "Exploration is over. Give your final decision now, as JSON matching the schema, with all "
    "assets."
)


def decide_agent(
    inp: DecisionInput,
    llm: LLMClient,
    model: QuantileModel | None,
    cfg: AgentConfig,
    on_llm_failure: Literal["hold", "raise"] = "hold",
) -> Decision:
    """The decision of whichever agent `cfg` describes: the run, the replay and the evaluations
    all go through here, so both agents are run by exactly the same code around the decision."""
    if cfg.agent == "tools":
        if not isinstance(llm, ToolLLMClient):
            raise TypeError("the tool agent needs an LLM client that supports tool calls")
        if model is None:
            raise ValueError("the tool agent needs the Timestral model (its portfolio tool)")
        return decide_with_tools(inp, llm, model, cfg, on_llm_failure)
    if cfg.agent != "pipeline":
        raise ValueError(f"unknown agent {cfg.agent!r}")
    return decide(inp, llm, cfg, on_llm_failure)


def _assistant_message(step: LLMStep) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": step.text,
        "tool_calls": [
            {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": c.arguments}}
            for c in step.tool_calls
        ],
    }


def decide_with_tools(
    inp: DecisionInput,
    llm: ToolLLMClient,
    model: QuantileModel,
    cfg: AgentConfig,
    on_llm_failure: Literal["hold", "raise"] = "hold",
) -> Decision:
    tools = TimestralTools(
        forecasts=inp.forecasts,
        closes=inp.closes,
        model=model,
        baseline=inp.baseline,
        current=inp.current,
        drawdown=inp.drawdown,
        cfg=cfg,
        news=inp.news_covariates,
    )
    specs = tools.specs()
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": load_system_prompt(cfg)},
        {
            "role": "user",
            "content": json.dumps(build_payload(inp, cfg, with_forecasts=False), indent=1),
        },
    ]
    steps: list[LLMStep] = []

    for _ in range(cfg.tools.max_rounds):
        try:
            step = llm.step(messages, specs)
        except Exception as error:  # same policy as the final answer (D-010)
            if on_llm_failure == "raise":
                raise
            steps.append(
                LLMStep(
                    text="",
                    reasoning=failure_text(error),
                    tool_calls=[],
                    model=cfg.llm.model,
                    prompt_tokens=0,
                    completion_tokens=0,
                )
            )
            break  # go straight to the final answer, which has its own retry and fallback
        steps.append(step)
        if not step.tool_calls:  # done exploring; keep what it said for the final answer
            if step.text.strip():
                messages.append({"role": "assistant", "content": step.text})
            break
        messages.append(_assistant_message(step))
        for call in step.tool_calls:
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "name": call.name,
                    "content": tools.call(call.name, call.arguments),
                }
            )
        if tools.budget_left <= 0:
            break

    messages.append({"role": "user", "content": FINAL_INSTRUCTION})
    return final_decision(
        messages,
        lambda m, s: llm.finish(m, s, specs),
        inp,
        cfg,
        on_llm_failure,
        tool_calls=tools.calls,
        steps=steps,
    )
