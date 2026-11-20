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
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from importlib import resources
from typing import Any, Literal, Protocol

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
    def complete(self, messages: list[dict[str, str]], schema: dict[str, Any]) -> LLMResponse: ...


class MistralLLM:  # pragma: no cover - network adapter, exercised by the smoke test
    def __init__(self, api_key: str, cfg: AgentConfig) -> None:
        from mistralai.client import Mistral

        self._client = Mistral(api_key=api_key)
        self._cfg = cfg.llm

    def complete(self, messages: list[dict[str, str]], schema: dict[str, Any]) -> LLMResponse:
        from mistralai.client.models import JSONSchema, ResponseFormat

        response = self._client.chat.complete(
            model=self._cfg.model,
            messages=messages,  # type: ignore[arg-type]
            temperature=self._cfg.temperature,
            reasoning_effort=self._cfg.reasoning_effort,  # type: ignore[arg-type]
            response_format=ResponseFormat(
                type="json_schema",
                json_schema=JSONSchema(
                    name="portfolio_decision", schema_definition=schema, strict=True
                ),
            ),
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
        usage = response.usage
        return LLMResponse(
            text=text,
            reasoning=reasoning,
            model=response.model or self._cfg.model,
            prompt_tokens=usage.prompt_tokens or 0,
            completion_tokens=usage.completion_tokens or 0,
        )


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


class Attempt(BaseModel):
    response: LLMResponse
    guardrails: GuardrailResult


class Decision(BaseModel):
    prompt_version: str
    messages: list[dict[str, str]]
    attempts: list[Attempt]
    outcome: Literal["accepted", "corrected", "overridden", "fallback_hold"]
    target_weights: dict[str, float]
    cash: float


# --------------------------------------------------------------------------- prompt


def load_system_prompt(cfg: AgentConfig) -> str:
    template = resources.files("agent.prompts").joinpath(f"{cfg.llm.prompt_version}.md").read_text()
    risk = cfg.risk
    return template.format(
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


def build_payload(inp: DecisionInput, cfg: AgentConfig) -> dict[str, Any]:
    h = cfg.forecast.horizon
    assets = []
    for a in cfg.universe:
        fc = inp.forecasts[a.symbol].summary((1, h))
        last_close = float(inp.closes[a.symbol].iloc[-1])
        latest = inp.latest_prices.get(a.symbol)
        assets.append(
            {
                "symbol": a.symbol,
                "name": a.name,
                "type": a.type.value,
                "group": a.group,
                "current_weight": round(inp.current[a.symbol], 4),
                "baseline_weight": round(inp.baseline[a.symbol], 4),
                "max_weight": cfg.max_weight(a.symbol),
                "forecast": fc["horizons"],
                "recent": _recent_stats(inp.closes[a.symbol]),
                "latest_price": None if latest is None else round(latest, 4),
                "move_since_last_close": (
                    None if latest is None else round(latest / last_close - 1, 5)
                ),
            }
        )
    return {
        "as_of": inp.now.isoformat(timespec="minutes"),
        "run": inp.mode,
        "last_completed_session": inp.closes.index[-1].date().isoformat(),
        "forecast_horizons_trading_days": [1, h],
        "portfolio": {
            "equity_usd": round(inp.equity, 2),
            "current_cash": round(1 - sum(inp.current.values()), 4),
            "baseline_cash": round(1 - sum(inp.baseline.values()), 4),
            "drawdown_from_peak": round(inp.drawdown, 4),
            "drawdown_actions": drawdown_actions(inp.drawdown, cfg),
        },
        "assets": assets,
        "news": "No news feed is connected yet.",
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


def decide(inp: DecisionInput, llm: LLMClient, cfg: AgentConfig) -> Decision:
    messages = [
        {"role": "system", "content": load_system_prompt(cfg)},
        {"role": "user", "content": json.dumps(build_payload(inp, cfg), indent=1)},
    ]
    schema = response_schema(cfg)
    attempts: list[Attempt] = []

    for _ in range(1 + cfg.llm.max_retries):
        response = llm.complete(messages, schema)
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
    )
