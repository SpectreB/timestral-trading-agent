"""Data contracts shared across the pipeline.

`Proposal` is what the LLM must return (D-008). It is deliberately permissive on values
(negative weights, out-of-range confidence): the schema only guarantees *shape*, and the
guardrails decide what is correctable and what is severe (D-010). Non-finite numbers
(NaN, infinity) are rejected at parsing time.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class PositionDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, strict=True)

    symbol: str
    target_weight: float
    rationale: str
    confidence: float


class Proposal(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, strict=True)

    analysis: str
    positions: list[PositionDecision]
    cash_weight: float
