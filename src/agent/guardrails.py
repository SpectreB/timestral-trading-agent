"""Deterministic guardrails between the LLM proposal and the broker (D-010, layers 1 and 2).

Pipeline of checks, in order:

Layer 1: is the LLM output usable? (severe -> reject)
    invalid JSON, schema violation, non-finite value, duplicate / unknown / missing asset,
    deviation from baseline without a rationale, weights + cash outside 95-105%.

Layer 2: does the portfolio respect the risk rules? (correct and log)
    renormalise, negative weight -> 0, drawdown circuit breaker, asymmetric bound vs
    baseline, per-asset cap, per-group cap, exposure cap (cash floor).

Severity: if the LLM needed too many corrections (too many assets, or too many points in
total), it did not understand the rules and the whole proposal is rejected.

Weight freed by a correction always goes to cash, never to other assets: a correction can
only reduce risk.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ValidationError

from agent.config import AgentConfig
from agent.schemas import Proposal

# A deviation from baseline smaller than this does not need a rationale (rounding noise).
DEVIATION_NEEDING_RATIONALE = 0.005
_EPS = 1e-9


class Correction(BaseModel):
    rule: str
    symbol: str | None
    before: float
    after: float
    # Counted corrections are LLM rule violations and feed the severity check.
    # Uncounted ones are routine adjustments (renormalisation, confidence clipping).
    counted: bool


class GuardrailResult(BaseModel):
    status: Literal["accepted", "corrected", "rejected", "overridden"]
    weights: dict[str, float] | None  # None when rejected: the caller applies the fallback
    cash: float | None
    corrections: list[Correction]
    rejection_reason: str | None = None
    drawdown_actions: list[str] = []
    confidences: dict[str, float] = {}


def _reject(reason: str, corrections: list[Correction] | None = None) -> GuardrailResult:
    return GuardrailResult(
        status="rejected",
        weights=None,
        cash=None,
        corrections=corrections or [],
        rejection_reason=reason,
    )


def drawdown_actions(drawdown: float, cfg: AgentConfig) -> list[str]:
    """Circuit-breaker levels reached. Levels are cumulative: -8% also implies reduce-only."""
    return [lvl.action for lvl in cfg.drawdown_levels if drawdown <= lvl.threshold + _EPS]


def _check_reference(name: str, weights: Mapping[str, float], cfg: AgentConfig) -> None:
    """Baseline and current portfolios come from our own code: a bad one is a bug, not an
    LLM error, so it raises instead of rejecting."""
    if set(weights) != set(cfg.symbols):
        raise ValueError(f"{name} portfolio must cover exactly the universe")
    if any(w < 0 for w in weights.values()) or sum(weights.values()) > 1 + _EPS:
        raise ValueError(f"{name} portfolio must be long-only and fully funded")


def apply_guardrails(
    raw: str | Mapping[str, Any],
    baseline: Mapping[str, float],
    current: Mapping[str, float],
    drawdown: float,
    cfg: AgentConfig,
) -> GuardrailResult:
    """Validate and bound an LLM proposal.

    raw: the LLM output (JSON text or already-decoded dict).
    baseline: the rule-based portfolio (D-004), weights per symbol.
    current: the portfolio currently held, weights per symbol.
    drawdown: current drawdown from peak equity, as a fraction <= 0.
    """
    _check_reference("baseline", baseline, cfg)
    _check_reference("current", current, cfg)
    risk = cfg.risk

    # ---------------------------------------------------------------- Layer 1
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return _reject("invalid_json")
    try:
        proposal = Proposal.model_validate(raw)
    except ValidationError:
        return _reject("schema_violation")

    symbols = [p.symbol for p in proposal.positions]
    if len(symbols) != len(set(symbols)):
        return _reject("duplicate_asset")
    if set(symbols) - set(cfg.symbols):
        return _reject("unknown_asset")
    if set(cfg.symbols) - set(symbols):
        return _reject("missing_asset")

    for p in proposal.positions:
        deviates = abs(p.target_weight - baseline[p.symbol]) > DEVIATION_NEEDING_RATIONALE
        if deviates and not p.rationale.strip():
            return _reject("unjustified_deviation")

    total = sum(p.target_weight for p in proposal.positions) + proposal.cash_weight
    if abs(total - 1.0) > risk.weight_sum_tolerance:
        return _reject("weights_do_not_sum_to_one")

    # ---------------------------------------------------------------- Layer 2
    corrections: list[Correction] = []
    weights = {p.symbol: p.target_weight for p in proposal.positions}

    def clip(rule: str, symbol: str, new: float, counted: bool = True) -> None:
        if weights[symbol] - new > _EPS:
            corrections.append(
                Correction(
                    rule=rule, symbol=symbol, before=weights[symbol], after=new, counted=counted
                )
            )
            weights[symbol] = new

    confidences: dict[str, float] = {}
    for p in proposal.positions:
        clipped = min(max(p.confidence, 0.0), 1.0)
        if clipped != p.confidence:
            corrections.append(
                Correction(
                    rule="confidence_out_of_range",
                    symbol=p.symbol,
                    before=p.confidence,
                    after=clipped,
                    counted=False,
                )
            )
        confidences[p.symbol] = clipped

    if abs(total - 1.0) > _EPS:
        corrections.append(
            Correction(rule="renormalised", symbol=None, before=total, after=1.0, counted=False)
        )
        weights = {s: w / total for s, w in weights.items()}

    for s in cfg.symbols:
        if weights[s] < 0:
            corrections.append(
                Correction(
                    rule="negative_weight", symbol=s, before=weights[s], after=0.0, counted=True
                )
            )
            weights[s] = 0.0

    actions = drawdown_actions(drawdown, cfg)
    if "baseline_half_exposure_and_pause" in actions:
        # Last circuit-breaker level: the LLM is overridden entirely.
        half = {s: baseline[s] * 0.5 for s in cfg.symbols}
        return GuardrailResult(
            status="overridden",
            weights=half,
            cash=1.0 - sum(half.values()),
            corrections=corrections,
            drawdown_actions=actions,
            confidences=confidences,
        )
    if "reduce_only" in actions:
        for s in cfg.symbols:
            clip("drawdown_reduce_only", s, min(weights[s], current[s]))

    for s in cfg.symbols:
        clip(
            "above_baseline_bound", s, min(weights[s], baseline[s] + risk.max_increase_vs_baseline)
        )
        clip("above_asset_cap", s, min(weights[s], cfg.max_weight(s)))

    groups: dict[str, list[str]] = {}
    for a in cfg.universe:
        groups.setdefault(a.group, []).append(a.symbol)
    for members in groups.values():
        group_total = sum(weights[s] for s in members)
        if group_total > risk.max_weight_group + _EPS:
            factor = risk.max_weight_group / group_total
            for s in members:
                clip("above_group_cap", s, weights[s] * factor)

    max_invested = risk.max_invested
    if "cap_exposure_60" in actions:
        max_invested = min(max_invested, 0.60)
    invested = sum(weights.values())
    if invested > max_invested + _EPS:
        corrections.append(
            Correction(
                rule="exposure_cap", symbol=None, before=invested, after=max_invested, counted=True
            )
        )
        factor = max_invested / invested
        weights = {s: w * factor for s, w in weights.items()}

    # ---------------------------------------------------------------- Severity
    counted = [c for c in corrections if c.counted]
    corrected_assets = {c.symbol for c in counted if c.symbol is not None}
    total_correction = sum(abs(c.before - c.after) for c in counted)
    if len(corrected_assets) > risk.max_corrected_assets:
        return _reject("too_many_corrected_assets", corrections)
    if total_correction > risk.max_total_correction + _EPS:
        return _reject("corrections_too_large", corrections)

    return GuardrailResult(
        status="corrected" if counted else "accepted",
        weights=weights,
        cash=1.0 - sum(weights.values()),
        corrections=corrections,
        drawdown_actions=actions,
        confidences=confidences,
    )
