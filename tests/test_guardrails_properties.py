"""Property-based tests (D-009): whatever the LLM returns, nothing unsafe gets through.

Hypothesis generates thousands of random proposals (wrong sums, negative weights, huge
positions, invented assets...) and random market states. For every one of them, the
guardrails must either reject the proposal or output a portfolio that passes the
independent final check and respects the state-dependent rules.
"""

from __future__ import annotations

from typing import Any

from hypothesis import assume, given, settings
from hypothesis import strategies as st

from agent.guardrails import apply_guardrails, drawdown_actions
from agent.safety import final_check
from tests.helpers import CFG

SYMBOLS = list(CFG.symbols)
weight = st.floats(min_value=-0.5, max_value=1.5, allow_nan=False)


@st.composite
def valid_portfolio(draw: st.DrawFn) -> dict[str, float]:
    """A portfolio our own code could produce: within every cap."""
    w = {s: draw(st.floats(0.0, CFG.max_weight(s))) for s in SYMBOLS}
    groups: dict[str, list[str]] = {}
    for a in CFG.universe:
        groups.setdefault(a.group, []).append(a.symbol)
    for members in groups.values():
        total = sum(w[s] for s in members)
        if total > CFG.risk.max_weight_group:
            for s in members:
                w[s] *= CFG.risk.max_weight_group / total
    invested = sum(w.values())
    if invested > CFG.risk.max_invested:
        w = {s: v * CFG.risk.max_invested / invested for s, v in w.items()}
    return w


@st.composite
def llm_proposal(draw: st.DrawFn) -> dict[str, Any]:
    symbols = draw(
        st.one_of(
            st.just(SYMBOLS),  # well-formed most of the time
            st.lists(st.sampled_from([*SYMBOLS, "TSLA", "BTC"]), max_size=17),
        )
    )
    return {
        "analysis": "random",
        "positions": [
            {
                "symbol": s,
                "target_weight": draw(weight),
                "rationale": draw(st.sampled_from(["", "because"])),
                "confidence": draw(st.floats(-1, 2, allow_nan=False)),
            }
            for s in symbols
        ],
        "cash_weight": draw(weight),
    }


@st.composite
def near_valid_proposal(draw: st.DrawFn) -> dict[str, Any]:
    """Proposals that sum to ~100%, so they reach layer 2 and exercise the corrections."""
    raw = {s: draw(st.floats(-0.05, 0.40)) for s in SYMBOLS}
    cash = draw(st.floats(-0.05, 0.5))
    total = sum(raw.values()) + cash
    assume(total > 0.5)
    return {
        "analysis": "random",
        "positions": [
            {"symbol": s, "target_weight": v / total, "rationale": "because", "confidence": 0.5}
            for s, v in raw.items()
        ],
        "cash_weight": cash / total,
    }


def check_invariants(
    p: dict[str, Any], baseline: dict[str, float], current: dict[str, float], dd: float
) -> None:
    r = apply_guardrails(p, baseline, current, dd, CFG)
    if r.status == "rejected":
        assert r.weights is None
        return
    assert r.weights is not None and r.cash is not None
    final_check(r.weights, r.cash, CFG)  # core invariants, checked independently

    actions = drawdown_actions(dd, CFG)
    if r.status == "overridden":
        return
    for s in SYMBOLS:
        assert r.weights[s] <= baseline[s] + CFG.risk.max_increase_vs_baseline + 1e-9
        if "reduce_only" in actions:
            assert r.weights[s] <= current[s] + 1e-9
    if "cap_exposure_60" in actions:
        assert sum(r.weights.values()) <= 0.60 + 1e-9


@settings(max_examples=2000, deadline=None)
@given(llm_proposal(), valid_portfolio(), valid_portfolio(), st.floats(-0.2, 0.0))
def test_any_llm_output_is_rejected_or_safe(
    p: dict[str, Any], baseline: dict[str, float], current: dict[str, float], dd: float
) -> None:
    check_invariants(p, baseline, current, dd)


@settings(max_examples=2000, deadline=None)
@given(near_valid_proposal(), valid_portfolio(), valid_portfolio(), st.floats(-0.2, 0.0))
def test_corrected_outputs_are_always_safe(
    p: dict[str, Any], baseline: dict[str, float], current: dict[str, float], dd: float
) -> None:
    check_invariants(p, baseline, current, dd)


@st.composite
def plausible_case(
    draw: st.DrawFn,
) -> tuple[dict[str, Any], dict[str, float], dict[str, float], float]:
    """Realistic LLM behaviour: start from the baseline, tilt a few assets (sometimes past
    the limits), mostly outside drawdown. Exercises the correction paths of layer 2."""
    baseline = draw(valid_portfolio())
    current = draw(st.one_of(st.just(baseline), valid_portfolio()))
    dd = draw(st.one_of(st.just(0.0), st.sampled_from([-0.035, -0.06, -0.09]), st.floats(-0.2, 0)))
    weights = dict(baseline)
    for s in draw(st.lists(st.sampled_from(SYMBOLS), min_size=1, max_size=5, unique=True)):
        weights[s] = baseline[s] + draw(st.floats(-0.10, 0.25))
    cash = 1.0 - sum(weights.values()) + draw(st.floats(-0.03, 0.03))
    p = {
        "analysis": "plausible",
        "positions": [
            {"symbol": s, "target_weight": w, "rationale": "because", "confidence": 0.5}
            for s, w in weights.items()
        ],
        "cash_weight": cash,
    }
    return p, baseline, current, dd


@settings(max_examples=3000, deadline=None)
@given(plausible_case())
def test_plausible_llm_behaviour_is_always_safe(
    case: tuple[dict[str, Any], dict[str, float], dict[str, float], float],
) -> None:
    check_invariants(*case)
