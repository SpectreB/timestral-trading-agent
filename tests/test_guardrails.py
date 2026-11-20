"""Guardrail unit tests and adversarial LLM outputs (D-009, D-010).

Each adversarial case is a response a misbehaving LLM could plausibly return.
"""

from __future__ import annotations

import json
import math

import pytest

from agent.guardrails import apply_guardrails, drawdown_actions
from agent.safety import final_check
from tests.helpers import ALL_CASH, BASELINE, CFG, as_json, proposal


def run(raw: object, current: dict[str, float] | None = None, drawdown: float = 0.0):  # type: ignore[no-untyped-def]
    result = apply_guardrails(raw, BASELINE, current or ALL_CASH, drawdown, CFG)  # type: ignore[arg-type]
    if result.status != "rejected":
        assert result.weights is not None and result.cash is not None
        final_check(result.weights, result.cash, CFG)  # every non-rejected output must be safe
    return result


def rules(result) -> list[str]:  # type: ignore[no-untyped-def]
    return [c.rule for c in result.corrections]


# --------------------------------------------------------------------------- happy path


def test_baseline_proposal_is_accepted_unchanged() -> None:
    r = run(as_json(proposal()))
    assert r.status == "accepted"
    assert r.corrections == []
    assert r.weights == pytest.approx(BASELINE)
    assert r.cash == pytest.approx(0.10)


def test_decoded_dict_is_accepted() -> None:
    assert run(proposal()).status == "accepted"


def test_integer_weights_are_accepted() -> None:
    p = proposal(SPY=0)
    p["positions"][0]["target_weight"] = 0  # JSON integer, as LLMs often write it
    assert run(as_json(p)).status == "accepted"


def test_reducing_risk_is_free() -> None:
    # Moving everything to cash is a large deviation, but decreases are never bounded.
    r = run(proposal(weights=dict(ALL_CASH), cash=1.0))
    assert r.status == "accepted"
    assert r.cash == pytest.approx(1.0)


# --------------------------------------------------------------------------- layer 1: severe


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        ("this is not json", "invalid_json"),
        ('{"analysis": "truncated...', "invalid_json"),
        (json.dumps({"positions": []}), "schema_violation"),  # missing fields
        (as_json({**proposal(), "leverage": 2}), "schema_violation"),  # unexpected field
    ],
)
def test_unusable_output_is_rejected(raw: str, reason: str) -> None:
    r = run(raw)
    assert r.status == "rejected"
    assert r.rejection_reason == reason
    assert r.weights is None


@pytest.mark.parametrize("bad_value", ["0.1", True, None, "NaN"])
def test_non_numeric_weight_is_rejected(bad_value: object) -> None:
    p = proposal()
    p["positions"][0]["target_weight"] = bad_value
    assert run(p).rejection_reason == "schema_violation"


@pytest.mark.parametrize("bad_value", [math.nan, math.inf, -math.inf])
def test_non_finite_weight_is_rejected(bad_value: float) -> None:
    p = proposal()
    p["positions"][3]["target_weight"] = bad_value
    assert run(as_json(p)).rejection_reason == "schema_violation"


def test_duplicate_asset_is_rejected() -> None:
    p = proposal()
    p["positions"].append(dict(p["positions"][0]))
    assert run(p).rejection_reason == "duplicate_asset"


def test_invented_asset_is_rejected() -> None:
    p = proposal()
    p["positions"][0]["symbol"] = "TSLA"
    assert run(p).rejection_reason == "unknown_asset"


def test_missing_asset_is_rejected() -> None:
    p = proposal()
    p["positions"].pop()
    assert run(p).rejection_reason == "missing_asset"


def test_unjustified_deviation_is_rejected() -> None:
    p = proposal(SPY=0.10, rationale="   ")
    assert run(p).rejection_reason == "unjustified_deviation"


def test_empty_rationale_is_fine_without_deviation() -> None:
    assert run(proposal(rationale="")).status == "accepted"


@pytest.mark.parametrize("cash", [0.5, -0.2, 0.0])
def test_weights_far_from_100_percent_are_rejected(cash: float) -> None:
    assert run(proposal(cash=cash)).rejection_reason == "weights_do_not_sum_to_one"


# --------------------------------------------------------------------------- layer 2: corrections


def test_small_sum_error_is_renormalised_without_penalty() -> None:
    r = run(proposal(cash=0.13))  # 103% in total: rounding noise
    assert r.status == "accepted"
    assert rules(r) == ["renormalised"]
    assert sum(r.weights.values()) + r.cash == pytest.approx(1.0)  # type: ignore[union-attr]


def test_short_attempt_is_set_to_zero_and_flagged() -> None:
    r = run(proposal(SPY=-0.05))
    assert r.status == "corrected"
    assert "negative_weight" in rules(r)
    assert r.weights["SPY"] == 0.0  # type: ignore[index]


def test_increase_above_baseline_bound_is_clipped() -> None:
    r = run(proposal(SPY=0.24, XLF=0.0, TLT=0.0, GLD=0.0))
    assert "above_baseline_bound" in rules(r)
    assert r.weights["SPY"] == pytest.approx(0.06 + 0.15)  # type: ignore[index]


def test_single_stock_cap_is_enforced() -> None:
    r = run(proposal(NVDA=0.15, SPY=0.01))
    assert "above_asset_cap" in rules(r)
    assert r.weights["NVDA"] == pytest.approx(0.10)  # type: ignore[index]


def test_sector_cap_aggregates_etf_and_stock() -> None:
    # Energy: XLE 0.21 + XOM 0.10 = 0.31 > 0.30, each individually within its cap.
    r = run(proposal(XLE=0.21, XOM=0.10, SPY=0.0, IWM=0.0, EFA=0.0, cash=0.10 - 0.01))
    assert "above_group_cap" in rules(r)
    assert r.weights["XLE"] + r.weights["XOM"] == pytest.approx(0.30)  # type: ignore[index]


def test_cash_floor_scales_positions_down() -> None:
    r = run(proposal(SPY=0.10, cash=0.06))  # 94% invested
    assert rules(r) == ["exposure_cap"]
    assert r.cash == pytest.approx(0.10)


def test_freed_weight_goes_to_cash_never_to_other_assets() -> None:
    r = run(proposal(NVDA=0.15, SPY=0.01))
    assert r.weights is not None
    for s in CFG.symbols:
        if s != "NVDA":
            assert r.weights[s] <= BASELINE[s] + 1e-12


def test_confidence_out_of_range_is_clipped_without_penalty() -> None:
    r = run(proposal(confidence=1.7))
    assert r.status == "accepted"
    assert set(r.confidences.values()) == {1.0}
    assert "confidence_out_of_range" in rules(r)


# --------------------------------------------------------------------------- severity


def test_too_many_violations_reject_the_whole_proposal() -> None:
    p = proposal(SPY=-0.01, IWM=-0.01, EFA=-0.01, EEM=-0.01, cash=0.10 + 0.28)
    r = run(p)
    assert r.rejection_reason == "too_many_corrected_assets"
    assert len(r.corrections) >= 4  # corrections are still reported for the audit log


def test_one_huge_violation_rejects_the_whole_proposal() -> None:
    p = proposal(XLE=0.50, SPY=0.0, IWM=0.0, EFA=0.0, EEM=0.0, XLV=0.0, XLF=0.0, TLT=0.0)
    r = run(p)
    assert r.rejection_reason == "corrections_too_large"


# --------------------------------------------------------------------------- circuit breaker


def test_drawdown_levels_are_cumulative() -> None:
    assert drawdown_actions(0.0, CFG) == []
    assert drawdown_actions(-0.03, CFG) == ["alert"]
    assert drawdown_actions(-0.09, CFG) == ["alert", "reduce_only", "cap_exposure_60"]


def test_alert_level_changes_nothing() -> None:
    r = run(proposal(), current=BASELINE, drawdown=-0.035)
    assert r.status == "accepted"
    assert r.drawdown_actions == ["alert"]


def test_reduce_only_blocks_increases() -> None:
    r = run(proposal(SPY=0.09, cash=0.07), current=BASELINE, drawdown=-0.06)
    assert "drawdown_reduce_only" in rules(r)
    assert r.weights["SPY"] == pytest.approx(0.06)  # type: ignore[index]


def test_exposure_capped_at_60_percent() -> None:
    r = run(proposal(weights={s: 0.04 for s in CFG.symbols}), current=BASELINE, drawdown=-0.09)
    assert r.weights is not None
    assert sum(r.weights.values()) == pytest.approx(0.60)


def test_last_level_overrides_the_llm_with_half_baseline() -> None:
    r = run(proposal(SPY=0.20, cash=0.0, TLT=0.0), current=BASELINE, drawdown=-0.15)
    assert r.status == "overridden"
    assert r.weights == pytest.approx({s: 0.03 for s in CFG.symbols})
    assert r.cash == pytest.approx(0.55)


# --------------------------------------------------------------------------- programming errors


def test_invalid_baseline_is_a_bug_not_an_llm_error() -> None:
    with pytest.raises(ValueError, match="baseline"):
        apply_guardrails(proposal(), {"SPY": 0.5}, ALL_CASH, 0.0, CFG)
    with pytest.raises(ValueError, match="current"):
        apply_guardrails(proposal(), BASELINE, {s: -0.1 for s in CFG.symbols}, 0.0, CFG)
