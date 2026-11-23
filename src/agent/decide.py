"""Decision stage: the LLM adjusts the baseline portfolio, the guardrails bound it (D-004, D-008).

Flow:
1. Build the prompt: versioned system prompt + a JSON payload (forecasts, baseline,
   current portfolio, recent market data, drawdown state).
2. Call the LLM with a strict JSON schema (`analysis` first). Its reasoning trace is kept.
3. Apply the guardrails. If the proposal is rejected, retry once, telling the LLM why.
4. If it is still rejected, fall back to holding the current positions (D-010).

Every attempt (prompt, raw answer, reasoning, guardrail result) is returned for the audit log.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from importlib import resources
from typing import Any, Literal, Protocol, runtime_checkable

import numpy as np
import pandas as pd
from pydantic import BaseModel

from agent.config import AgentConfig
from agent.forecast import AssetForecast
from agent.guardrails import GuardrailResult, apply_guardrails, drawdown_actions

Mode = Literal["morning", "afternoon"]


# --------------------------------------------------------------------------- LLM client


class LLMResponse(BaseModel):
    text: str  # the JSON answer
    reasoning: str  # native reasoning trace, empty if none
    model: str
    prompt_tokens: int
    completion_tokens: int


class LLMClient(Protocol):
    def complete(self, messages: list[dict[str, Any]], schema: dict[str, Any]) -> LLMResponse: ...


class ToolCall(BaseModel):
    id: str
    name: str
    arguments: str  # JSON text, untrusted


class LLMStep(BaseModel):
    """One turn of the tool agent (D-018): either tool calls, or a plain text answer."""

    text: str
    reasoning: str
    tool_calls: list[ToolCall]
    model: str
    prompt_tokens: int
    completion_tokens: int


@runtime_checkable
class ToolLLMClient(LLMClient, Protocol):
    def step(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> LLMStep: ...

    def finish(
        self,
        messages: list[dict[str, Any]],
        schema: dict[str, Any],
        tools: list[dict[str, Any]],
    ) -> LLMResponse: ...


class MistralLLM:  # pragma: no cover - network adapter, exercised by the smoke test
    def __init__(self, api_key: str, cfg: AgentConfig) -> None:
        from mistralai.client import Mistral
        from mistralai.client.utils import BackoffStrategy, RetryConfig

        # Transient failures (connection, 429, 5xx) are retried by the SDK with a backoff;
        # anything that still fails is handled by decide() (an unavailable LLM never crashes a run).
        self._client = Mistral(
            api_key=api_key,
            timeout_ms=cfg.llm.timeout_s * 1000,
            retry_config=RetryConfig(
                "backoff",
                BackoffStrategy(2_000, 60_000, 2.0, 300_000),
                retry_connection_errors=True,
                status_codes_override=["429", "500", "502", "503", "504"],
            ),
        )
        self._cfg = cfg.llm

    def _call(self, messages: list[dict[str, Any]], **extra: Any) -> LLMStep:
        # A model without native reasoning (the news analyst, D-019) gets no such parameter.
        if self._cfg.reasoning_effort:
            extra["reasoning_effort"] = self._cfg.reasoning_effort
        response = self._client.chat.complete(
            model=self._cfg.model,
            messages=messages,  # type: ignore[arg-type]
            temperature=self._cfg.temperature,
            **extra,
        )
        message = response.choices[0].message
        content = message.content if message is not None else None
        text, reasoning = "", ""
        if isinstance(content, str):
            text = content
        else:
            for chunk in content or []:
                kind = type(chunk).__name__
                if kind == "ThinkChunk":
                    reasoning += "".join(getattr(t, "text", "") for t in chunk.thinking)  # type: ignore[union-attr]
                elif kind == "TextChunk":
                    text += chunk.text  # type: ignore[union-attr]
        calls = [
            ToolCall(
                id=c.id or f"call_{i}",
                name=c.function.name,
                arguments=(
                    c.function.arguments
                    if isinstance(c.function.arguments, str)
                    else json.dumps(c.function.arguments)
                ),
            )
            for i, c in enumerate((message.tool_calls if message is not None else None) or [])
        ]
        usage = response.usage
        return LLMStep(
            text=text,
            reasoning=reasoning,
            tool_calls=calls,
            model=response.model or self._cfg.model,
            prompt_tokens=usage.prompt_tokens or 0,
            completion_tokens=usage.completion_tokens or 0,
        )

    @staticmethod
    def _json_format(schema: dict[str, Any]) -> Any:
        from mistralai.client.models import JSONSchema, ResponseFormat

        return ResponseFormat(
            type="json_schema",
            json_schema=JSONSchema(
                name="portfolio_decision", schema_definition=schema, strict=True
            ),
        )

    @staticmethod
    def _response(step: LLMStep) -> LLMResponse:
        return LLMResponse(**step.model_dump(exclude={"tool_calls"}))

    def complete(self, messages: list[dict[str, Any]], schema: dict[str, Any]) -> LLMResponse:
        return self._response(self._call(messages, response_format=self._json_format(schema)))

    def step(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> LLMStep:
        return self._call(messages, tools=tools, tool_choice="auto", parallel_tool_calls=True)

    def finish(
        self,
        messages: list[dict[str, Any]],
        schema: dict[str, Any],
        tools: list[dict[str, Any]],
    ) -> LLMResponse:
        # The tools stay declared (the history contains tool calls) but can no longer be called.
        step = self._call(
            messages, tools=tools, tool_choice="none", response_format=self._json_format(schema)
        )
        return self._response(step)


# --------------------------------------------------------------------------- inputs / outputs


@dataclass(frozen=True)
class DecisionInput:
    now: datetime
    mode: Mode
    forecasts: Mapping[str, AssetForecast]
    baseline: Mapping[str, float]
    current: Mapping[str, float]
    closes: pd.DataFrame  # completed daily closes, used for recent returns and volatility
    drawdown: float  # from peak equity, <= 0
    equity: float
    latest_prices: Mapping[str, float] = field(default_factory=dict)  # live reference prices
    news: str = "No news feed is connected yet."  # untrusted text: a prompt-injection surface
    # D-019: news analysed into Timestral covariates (agent.news.NewsCovariates), tool agent only.
    news_covariates: Any = None


class Attempt(BaseModel):
    response: LLMResponse
    guardrails: GuardrailResult


class Decision(BaseModel):
    prompt_version: str
    messages: list[dict[str, Any]]
    attempts: list[Attempt]
    outcome: Literal["accepted", "corrected", "overridden", "fallback_hold"]
    target_weights: dict[str, float]
    cash: float
    # Tool agent only (D-018): every tool call with its result, and every exploration turn.
    tool_calls: list[dict[str, Any]] = []
    steps: list[LLMStep] = []


# --------------------------------------------------------------------------- prompt


def load_system_prompt(cfg: AgentConfig) -> str:
    """The versioned system prompt, filled with the limits of `cfg` (unused keys are ignored,
    so one function serves both agents' prompts)."""
    template = resources.files("agent.prompts").joinpath(f"{cfg.llm.prompt_version}.md").read_text()
    risk = cfg.risk
    return template.format(
        max_turnover_pct=round(100 * cfg.execution.max_turnover_per_run),
        max_calls=cfg.tools.max_calls,
        max_rounds=cfg.tools.max_rounds,
        n_assets=len(cfg.universe),
        horizon=cfg.forecast.horizon,
        min_cash_pct=round(100 * risk.min_cash),
        max_etf_pct=round(100 * risk.max_weight_etf),
        max_stock_pct=round(100 * risk.max_weight_stock),
        max_group_pct=round(100 * risk.max_weight_group),
        max_increase_pts=round(100 * risk.max_increase_vs_baseline),
        max_corrected_assets=risk.max_corrected_assets,
        max_total_correction_pts=round(100 * risk.max_total_correction),
        no_trade_band_pts=round(100 * cfg.execution.no_trade_band),
    )


def response_schema(cfg: AgentConfig) -> dict[str, Any]:
    """Strict JSON schema sent to the API. Property order puts `analysis` first."""
    position = {
        "type": "object",
        "properties": {
            "symbol": {"type": "string", "enum": list(cfg.symbols)},
            "target_weight": {"type": "number"},
            "rationale": {"type": "string"},
            "confidence": {"type": "number"},
        },
        "required": ["symbol", "target_weight", "rationale", "confidence"],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {
            "analysis": {"type": "string"},
            "positions": {"type": "array", "items": position},
            "cash_weight": {"type": "number"},
        },
        "required": ["analysis", "positions", "cash_weight"],
        "additionalProperties": False,
    }


def _recent_stats(closes: pd.Series) -> dict[str, float]:
    c = closes.dropna()
    daily = np.log(c).diff().dropna()
    return {
        "return_1d": round(float(c.iloc[-1] / c.iloc[-2] - 1), 5),
        "return_5d": round(float(c.iloc[-1] / c.iloc[-6] - 1), 5),
        "return_20d": round(float(c.iloc[-1] / c.iloc[-21] - 1), 5),
        "volatility_20d_annualised": round(float(daily.iloc[-20:].std() * np.sqrt(252)), 4),
    }


def build_payload(
    inp: DecisionInput, cfg: AgentConfig, with_forecasts: bool = True
) -> dict[str, Any]:
    """The decision input sent to the LLM. Without forecasts (the tool agent, D-018), Timestral's
    forecasts and the baseline are left out: the LLM asks for them through its tools."""
    h = cfg.forecast.horizon
    assets = []
    for a in cfg.universe:
        last_close = float(inp.closes[a.symbol].iloc[-1])
        latest = inp.latest_prices.get(a.symbol)
        asset: dict[str, Any] = {
            "symbol": a.symbol,
            "name": a.name,
            "type": a.type.value,
            "group": a.group,
            "current_weight": round(inp.current[a.symbol], 4),
        }
        if with_forecasts:
            asset["baseline_weight"] = round(inp.baseline[a.symbol], 4)
        asset["max_weight"] = cfg.max_weight(a.symbol)
        if with_forecasts:
            asset["forecast"] = inp.forecasts[a.symbol].summary((1, h))["horizons"]
        asset |= {
            "recent": _recent_stats(inp.closes[a.symbol]),
            "latest_price": None if latest is None else round(latest, 4),
            "move_since_last_close": None if latest is None else round(latest / last_close - 1, 5),
        }
        assets.append(asset)
    portfolio: dict[str, Any] = {
        "equity_usd": round(inp.equity, 2),
        "current_cash": round(1 - sum(inp.current.values()), 4),
    }
    if with_forecasts:
        portfolio["baseline_cash"] = round(1 - sum(inp.baseline.values()), 4)
    portfolio |= {
        "drawdown_from_peak": round(inp.drawdown, 4),
        "drawdown_actions": drawdown_actions(inp.drawdown, cfg),
    }
    return {
        "as_of": inp.now.isoformat(timespec="minutes"),
        "run": inp.mode,
        "last_completed_session": inp.closes.index[-1].date().isoformat(),
        "forecast_horizons_trading_days": [1, h] if with_forecasts else list(range(1, h + 1)),
        "portfolio": portfolio,
        "assets": assets,
        "news": inp.news,
    }


def _retry_message(result: GuardrailResult) -> str:
    lines = [f"Your previous answer was rejected by the guardrails: {result.rejection_reason}."]
    if result.corrections:
        lines.append("Corrections that would have been needed:")
        lines += [
            f"- {c.rule} {c.symbol or ''}: {c.before:.4f} -> {c.after:.4f}"
            for c in result.corrections
            if c.counted
        ]
    lines.append("Answer again with all assets, respecting every rule yourself.")
    return "\n".join(lines)


# --------------------------------------------------------------------------- decision


def failure_text(error: Exception) -> str:
    return f"LLM call failed: {type(error).__name__}: {str(error)[:300]}"


def decide(
    inp: DecisionInput,
    llm: LLMClient,
    cfg: AgentConfig,
    on_llm_failure: Literal["hold", "raise"] = "hold",
) -> Decision:
    """`on_llm_failure`: live, a call that fails (timeout, outage) counts as a rejected attempt,
    so the agent ends up holding its positions and the run is still recorded; the replay
    raises instead, because an outage must not be recorded as a decision to hold."""
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": load_system_prompt(cfg)},
        {"role": "user", "content": json.dumps(build_payload(inp, cfg), indent=1)},
    ]
    return final_decision(messages, llm.complete, inp, cfg, on_llm_failure)


def final_decision(
    messages: list[dict[str, Any]],
    complete: Callable[[list[dict[str, Any]], dict[str, Any]], LLMResponse],
    inp: DecisionInput,
    cfg: AgentConfig,
    on_llm_failure: Literal["hold", "raise"],
    **extra: Any,
) -> Decision:
    """Ask for the JSON decision, apply the guardrails, retry once if rejected, else hold.

    Shared by both agents; `extra` fills the tool agent's fields of the Decision."""
    schema = response_schema(cfg)
    attempts: list[Attempt] = []

    for _ in range(1 + cfg.llm.max_retries):
        try:
            response = complete(messages, schema)
        except Exception as error:  # an unavailable LLM must not crash a run (D-010)
            if on_llm_failure == "raise":
                raise
            failed = LLMResponse(
                text="",
                reasoning=failure_text(error),
                model=cfg.llm.model,
                prompt_tokens=0,
                completion_tokens=0,
            )
            rejected = GuardrailResult(
                status="rejected",
                weights=None,
                cash=None,
                corrections=[],
                rejection_reason="llm_call_failed",
            )
            attempts.append(Attempt(response=failed, guardrails=rejected))
            continue  # ask again, with the same messages
        result = apply_guardrails(response.text, inp.baseline, inp.current, inp.drawdown, cfg)
        attempts.append(Attempt(response=response, guardrails=result))
        if result.status != "rejected":
            assert result.weights is not None and result.cash is not None
            return Decision(
                prompt_version=cfg.llm.prompt_version,
                messages=messages,
                attempts=attempts,
                outcome=result.status,
                target_weights=result.weights,
                cash=result.cash,
                **extra,
            )
        messages = [
            *messages,
            {"role": "assistant", "content": response.text},
            {"role": "user", "content": _retry_message(result)},
        ]

    current = dict(inp.current)
    return Decision(
        prompt_version=cfg.llm.prompt_version,
        messages=messages,
        attempts=attempts,
        outcome="fallback_hold",
        target_weights=current,
        cash=1 - sum(current.values()),
        **extra,
    )
