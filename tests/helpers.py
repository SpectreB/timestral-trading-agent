"""Shared builders for guardrail tests."""

from __future__ import annotations

import json
from typing import Any

from agent.config import AgentConfig

CFG = AgentConfig()

# A valid baseline: 6% per asset = 90% invested, every cap respected.
BASELINE: dict[str, float] = {s: 0.06 for s in CFG.symbols}
ALL_CASH: dict[str, float] = {s: 0.0 for s in CFG.symbols}


def proposal(
    weights: dict[str, float] | None = None,
    cash: float | None = None,
    rationale: str = "Justified by the forecast interval.",
    confidence: float = 0.6,
    **overrides: float,
) -> dict[str, Any]:
    """Build an LLM proposal. Starts from the baseline; `overrides` change single assets."""
    w = dict(weights if weights is not None else BASELINE)
    w.update(overrides)
    if cash is None:
        cash = 1.0 - sum(w.values())
    return {
        "analysis": "Test analysis.",
        "positions": [
            {"symbol": s, "target_weight": v, "rationale": rationale, "confidence": confidence}
            for s, v in w.items()
        ],
        "cash_weight": cash,
    }


def as_json(p: dict[str, Any]) -> str:
    return json.dumps(p)
