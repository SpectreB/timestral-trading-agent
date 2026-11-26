"""Data contracts shared across the pipeline. The schema checks shape only; the guardrails decide
what is correctable (D-010)."""

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
