"""Performance metrics (D-014): shadow portfolios, comparisons, risk and confidence calibration.

A two-asset world with hand-computable returns:
    SPY 100 -> 102 -> 101    (Fri 11-20, Mon 11-23, Tue 11-24)
    TLT 100 ->  99 -> 100
"""

from __future__ import annotations

from typing import Any

import pytest

from agent.metrics import (
    block_bootstrap_ci,
    comparisons,
    confidence_calibration,
    performance_report,
    risk_calibration,
    session_returns,
    shadow_returns,
    summarise,
    summarise_confidence,
    summarise_risk,
)

OBS = {
    "as_of": "2026-11-24",
    "closes": {
        "2026-11-20": {"SPY": 100.0, "TLT": 100.0},
        "2026-11-23": {"SPY": 102.0, "TLT": 99.0},
        "2026-11-24": {"SPY": 101.0, "TLT": 100.0},
    },
}


def run(
    day: str,
    mode: str = "morning",
    final: dict[str, float] | None = None,
    proposed: dict[str, Any] | None = None,
    last_session: str = "2026-11-20",
    equity: float = 100_000.0,
    risk: dict[str, Any] | None = None,
) -> dict[str, Any]:
    hour = "10" if mode == "morning" else "15"
    return {
        "run_id": f"{day}_{mode}",
        "mode": mode,
        "started_at": f"{day}T{hour}:00:00-05:00",
        "data": {"last_session": last_session},
        "config": {"risk": {"max_invested": 0.9}},
        "account": {"equity": equity},
        "portfolios": {
            "baseline": {"SPY": 0.3, "TLT": 0.6},
            "proposed": proposed,
            "final": final or {"SPY": 0.5, "TLT": 0.4},
        },
        "risk": risk,
    }


PROPOSED = {
    "SPY": {"weight": 0.6, "confidence": 0.7, "rationale": "x"},
    "TLT": {"weight": 0.3, "confidence": 0.6, "rationale": "y"},
}
MORNING = run("2026-11-23", proposed=PROPOSED)
AFTERNOON = run("2026-11-23", "afternoon", final={"SPY": 0.0, "TLT": 0.9})


def test_session_returns_from_the_observations() -> None:
    r = session_returns([OBS])
    assert r["2026-11-23"]["SPY"] == pytest.approx(0.02)
    assert r["2026-11-24"]["TLT"] == pytest.approx(100 / 99 - 1)
    assert "2026-11-20" not in r  # no previous close


def test_sessions_with_no_common_observation_or_missing_prices_are_skipped() -> None:
    a = {"as_of": "2026-11-20", "closes": {"2026-11-19": {"SPY": 1.0}, "2026-11-20": {"SPY": 2.0}}}
    b = {
        "as_of": "2026-11-24",
        "closes": {"2026-11-23": {"SPY": 1.0, "TLT": None}, "2026-11-24": {"SPY": 1.1, "TLT": 5.0}},
    }
    r = session_returns([b, a])
    assert set(r) == {"2026-11-20", "2026-11-24"}  # 11-20 -> 11-23 is in no single observation
    assert r["2026-11-24"] == {"SPY": pytest.approx(0.1)}


def test_the_morning_run_earns_its_own_session_and_the_afternoon_run_the_next() -> None:
    rows = shadow_returns([AFTERNOON, MORNING], [OBS])
    assert [(r["session"], r["run_id"]) for r in rows] == [
        ("2026-11-23", "2026-11-23_morning"),
        ("2026-11-24", "2026-11-23_afternoon"),
    ]
    mon, tue = rows
    assert mon["agent"] == pytest.approx(0.5 * 0.02 + 0.4 * -0.01)
    assert mon["baseline"] == pytest.approx(0.3 * 0.02 + 0.6 * -0.01)
    assert mon["llm_proposal"] == pytest.approx(0.6 * 0.02 + 0.3 * -0.01)
    assert mon["equal_weight"] == pytest.approx(0.45 * 0.02 + 0.45 * -0.01)
    assert mon["spy"] == pytest.approx(0.02)
    assert tue["agent"] == pytest.approx(0.9 * (100 / 99 - 1))
    assert tue["llm_proposal"] == tue["agent"]  # no proposal recorded: falls back to the final


def test_sessions_before_the_first_run_are_not_counted() -> None:
    late = run("2026-11-24")
    assert [r["session"] for r in shadow_returns([late], [OBS])] == ["2026-11-24"]


def test_summary_statistics() -> None:
    s = summarise([0.10, -0.10])
    assert s["sessions"] == 2
    assert s["cumulative_return"] == pytest.approx(1.1 * 0.9 - 1)
    assert s["max_drawdown"] == pytest.approx(-0.10)
    assert s["annualised_volatility"] > 0 and s["sharpe"] is not None
    one = summarise([0.01])
    assert one["annualised_volatility"] is None and one["sharpe"] is None
    flat = summarise([0.0, 0.0])
    assert flat["sharpe"] is None and flat["max_drawdown"] == 0.0
    empty = summarise([])
    assert empty["sessions"] == 0 and empty["cumulative_return"] == 0.0


def test_no_confidence_interval_below_ten_sessions() -> None:
    assert block_bootstrap_ci([0.01] * 9) is None


def test_bootstrap_interval_brackets_the_mean_and_is_reproducible() -> None:
    values = [0.01 * ((i * 7) % 5 - 2) for i in range(40)]
    ci = block_bootstrap_ci(values)
    assert ci is not None and ci[0] <= sum(values) / len(values) <= ci[1]
    assert block_bootstrap_ci(values) == ci  # fixed seed
    assert block_bootstrap_ci([0.002] * 12) == pytest.approx((0.002, 0.002))


def test_a_comparison_is_conclusive_only_when_its_interval_excludes_zero() -> None:
    steady = [{"agent": 0.003, "llm_proposal": 0.001, "baseline": 0.001, "equal_weight": 0.001,
               "spy": 0.001} for _ in range(12)]  # fmt: skip
    out = {c["label"]: c for c in comparisons(steady)}
    guard = out["Guardrails (final vs LLM proposal)"]
    assert guard["conclusive"] is True and guard["mean_daily_difference"] == pytest.approx(0.002)
    assert out["LLM vs Timestral rule"]["conclusive"] is False  # identical series
    short = {c["label"]: c for c in comparisons(steady[:3])}
    assert short["Agent vs market (SPY)"]["ci95_mean_daily"] is None
    assert short["Agent vs market (SPY)"]["conclusive"] is False
    assert comparisons([])[0]["mean_daily_difference"] is None


def test_forecast_risk_is_compared_with_the_realised_move() -> None:
    risk = {"portfolio_vol": {"1": {"final": 0.01}, "5": {"final": 0.02}, "2": {"final": 0.0}}}
    rows = risk_calibration([run("2026-11-23", risk=risk), run("2026-11-23", "afternoon")], [OBS])
    assert len(rows) == 1  # horizon 5 unknown, zero vol skipped, run without risk skipped
    (row,) = rows
    assert row["realised"] == pytest.approx(0.006) and row["z"] == pytest.approx(0.6)
    assert row["inside_80"] is True
    (summary,) = summarise_risk(rows)
    assert summary == {"horizon": 1, "n": 1, "inside_80": 1.0, "rms_z": pytest.approx(0.6)}


def test_the_llm_confidence_is_scored_against_what_its_tilts_earned() -> None:
    proposed = {**PROPOSED, "TLT": {"weight": 0.602, "confidence": 0.9, "rationale": "tiny"}}
    rows = confidence_calibration(
        [run("2026-11-23", proposed=proposed), AFTERNOON], [OBS], horizon=1
    )
    assert [r["symbol"] for r in rows] == ["SPY"]  # TLT tilt below 0.5 pt, afternoon has none
    assert rows[0]["paid_off"] is True and rows[0]["tilt"] == pytest.approx(0.3)
    both = confidence_calibration([MORNING], [OBS], horizon=1)
    summary = summarise_confidence(both)
    assert summary is not None
    assert summary["n"] == 2 and summary["hit_rate"] == 1.0
    assert summary["brier"] == pytest.approx(((0.7 - 1) ** 2 + (0.6 - 1) ** 2) / 2)
    assert {b["range"] for b in summary["bins"]} == {"0.65-0.75", "0.55-0.65"}
    assert summarise_confidence([]) is None


def test_unknown_or_zero_outcomes_are_not_scored() -> None:
    flat = {**OBS, "closes": {**OBS["closes"], "2026-11-23": {"SPY": 100.0, "TLT": 100.0}}}
    assert confidence_calibration([MORNING], [flat], horizon=1) == []
    assert confidence_calibration([MORNING], [OBS], horizon=5) == []


def test_the_full_report() -> None:
    report = performance_report([MORNING, run("2026-11-23", "afternoon", equity=101_000.0)], [OBS])
    assert report["sessions"] == 2
    assert (report["first_session"], report["last_session"]) == ("2026-11-23", "2026-11-24")
    assert set(report["series"]) == {"agent", "llm_proposal", "baseline", "equal_weight", "spy"}
    assert report["account_return"] == pytest.approx(0.01)
    assert len(report["comparisons"]) == 4
    assert report["confidence"] is None  # 5-day outcomes not known yet
    empty = performance_report([], [])
    assert empty["sessions"] == 0 and empty["account_return"] is None
    assert empty["first_session"] is None
