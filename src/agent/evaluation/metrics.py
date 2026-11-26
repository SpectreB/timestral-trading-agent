from __future__ import annotations

import math
from collections.abc import Sequence
from itertools import pairwise
from typing import Any

import numpy as np

from agent.evaluation.scoring import realised_return

Z80 = 1.2815515655446004
SERIES = ("agent", "llm_proposal", "baseline", "equal_weight", "spy")
COMPARISONS = (
    ("Timestral rule vs equal weight", "baseline", "equal_weight"),
    ("LLM vs Timestral rule", "llm_proposal", "baseline"),
    ("Guardrails (final vs LLM proposal)", "agent", "llm_proposal"),
    ("Agent vs market (SPY)", "agent", "spy"),
)
MIN_SESSIONS_FOR_CI = 10
BLOCK = 5
DEVIATION_THRESHOLD = 0.005


def _date(run: dict[str, Any]) -> str:
    return str(run["started_at"])[:10]


def _chronological(runs: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(runs, key=lambda r: (r["started_at"], r["mode"] != "morning"))


def session_returns(observations: Sequence[dict[str, Any]]) -> dict[str, dict[str, float]]:
    ordered = sorted(observations, key=lambda o: o["as_of"])
    dates = sorted({d for o in ordered for d in o["closes"]})
    out: dict[str, dict[str, float]] = {}
    for prev, cur in pairwise(dates):
        obs = next((o for o in ordered if prev in o["closes"] and cur in o["closes"]), None)
        if obs is None:
            continue
        a, b = obs["closes"][prev], obs["closes"][cur]
        out[cur] = {s: b[s] / a[s] - 1 for s in a if a[s] is not None and b.get(s) is not None}
    return out


def _weights(run: dict[str, Any], series: str) -> dict[str, float]:
    p = run["portfolios"]
    if series == "agent":
        return dict(p["final"])
    if series == "llm_proposal":
        proposed = p["proposed"]
        return {s: v["weight"] for s, v in proposed.items()} if proposed else dict(p["final"])
    if series == "baseline":
        return dict(p["baseline"])
    if series == "equal_weight":
        symbols = list(p["final"])
        invested = run["config"]["risk"]["max_invested"]
        return dict.fromkeys(symbols, invested / len(symbols))
    return {run["config"].get("benchmark", "SPY"): 1.0}


def shadow_returns(
    runs: Sequence[dict[str, Any]], observations: Sequence[dict[str, Any]]
) -> list[dict[str, Any]]:
    ordered = _chronological(runs)
    rows = []
    for session, returns in sorted(session_returns(observations).items()):
        active = [
            r
            for r in ordered
            if _date(r) < session or (_date(r) == session and r["mode"] == "morning")
        ]
        if not active:
            continue
        run = active[-1]
        row: dict[str, Any] = {"session": session, "run_id": run["run_id"]}
        for series in SERIES:
            row[series] = sum(w * returns.get(s, 0.0) for s, w in _weights(run, series).items())
        rows.append(row)
    return rows


def summarise(daily: Sequence[float]) -> dict[str, Any]:
    n = len(daily)
    wealth = np.cumprod([1.0, *(1 + r for r in daily)])
    drawdown = float(np.min(wealth / np.maximum.accumulate(wealth) - 1))
    std = float(np.std(daily, ddof=1)) if n >= 2 else 0.0
    return {
        "sessions": n,
        "cumulative_return": float(wealth[-1] - 1),
        "annualised_volatility": std * math.sqrt(252) if n >= 2 else None,
        "sharpe": float(np.mean(daily)) / std * math.sqrt(252) if n >= 2 and std > 0 else None,
        "max_drawdown": drawdown,
    }


def block_bootstrap_ci(
    values: Sequence[float], block: int = BLOCK, reps: int = 2000, seed: int = 0
) -> tuple[float, float] | None:
    n = len(values)
    if n < MIN_SESSIONS_FOR_CI:
        return None
    x = np.asarray(values, dtype=float)
    rng = np.random.default_rng(seed)
    n_blocks = math.ceil(n / block)
    starts = rng.integers(0, n - block + 1, size=(reps, n_blocks))
    idx = (starts[:, :, None] + np.arange(block)[None, None, :]).reshape(reps, -1)[:, :n]
    means = x[idx].mean(axis=1)
    return float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))


def comparisons(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for label, a, b in COMPARISONS:
        diff = [r[a] - r[b] for r in rows]
        ci = block_bootstrap_ci(diff)
        out.append(
            {
                "label": label,
                "series": a,
                "reference": b,
                "sessions": len(diff),
                "cumulative_difference": (
                    summarise([r[a] for r in rows])["cumulative_return"]
                    - summarise([r[b] for r in rows])["cumulative_return"]
                ),
                "mean_daily_difference": float(np.mean(diff)) if diff else None,
                "ci95_mean_daily": ci,
                "conclusive": ci is not None and (ci[0] > 0 or ci[1] < 0),
            }
        )
    return out


def risk_calibration(
    runs: Sequence[dict[str, Any]], observations: Sequence[dict[str, Any]]
) -> list[dict[str, Any]]:
    ordered = sorted(observations, key=lambda o: o["as_of"])
    rows = []
    for run in runs:
        risk = run.get("risk")
        if not risk:
            continue
        d0 = run["data"]["last_session"]
        for h_key, vols in risk["portfolio_vol"].items():
            sigma = vols.get("final", 0.0)
            weights = run["portfolios"]["final"]
            outcomes = [realised_return(ordered, d0, int(h_key), s) for s in weights]
            if sigma <= 0 or any(o is None for o in outcomes):
                continue
            realised = sum(w * o[1] for w, o in zip(weights.values(), outcomes, strict=True) if o)
            z = realised / sigma
            rows.append(
                {
                    "run_id": run["run_id"],
                    "horizon": int(h_key),
                    "forecast_vol": sigma,
                    "realised": realised,
                    "z": z,
                    "inside_80": abs(z) <= Z80,
                }
            )
    return rows


def summarise_risk(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for h in sorted({r["horizon"] for r in rows}):
        sub = [r for r in rows if r["horizon"] == h]
        out.append(
            {
                "horizon": h,
                "n": len(sub),
                "inside_80": sum(r["inside_80"] for r in sub) / len(sub),
                "rms_z": math.sqrt(sum(r["z"] ** 2 for r in sub) / len(sub)),
            }
        )
    return out


def confidence_calibration(
    runs: Sequence[dict[str, Any]], observations: Sequence[dict[str, Any]], horizon: int = 5
) -> list[dict[str, Any]]:
    ordered = sorted(observations, key=lambda o: o["as_of"])
    rows = []
    for run in runs:
        proposed = run["portfolios"]["proposed"]
        if not proposed:
            continue
        d0 = run["data"]["last_session"]
        for s, view in proposed.items():
            tilt = view["weight"] - run["portfolios"]["baseline"][s]
            found = realised_return(ordered, d0, horizon, s)
            if abs(tilt) <= DEVIATION_THRESHOLD or found is None or found[1] == 0:
                continue
            rows.append(
                {
                    "run_id": run["run_id"],
                    "symbol": s,
                    "tilt": tilt,
                    "confidence": view["confidence"],
                    "realised": found[1],
                    "paid_off": tilt * found[1] > 0,
                }
            )
    return rows


def summarise_confidence(rows: Sequence[dict[str, Any]]) -> dict[str, Any] | None:
    if not rows:
        return None
    outcomes = [float(r["paid_off"]) for r in rows]
    hit = sum(outcomes) / len(outcomes)
    bins = []
    for lo, hi in ((0.0, 0.55), (0.55, 0.65), (0.65, 0.75), (0.75, 1.01)):
        sub = [r for r in rows if lo <= r["confidence"] < hi]
        if sub:
            bins.append(
                {
                    "range": f"{lo:.2f}-{min(hi, 1.0):.2f}",
                    "n": len(sub),
                    "mean_confidence": sum(r["confidence"] for r in sub) / len(sub),
                    "hit_rate": sum(r["paid_off"] for r in sub) / len(sub),
                }
            )
    return {
        "n": len(rows),
        "mean_confidence": sum(r["confidence"] for r in rows) / len(rows),
        "hit_rate": hit,
        "brier": sum((r["confidence"] - o) ** 2 for r, o in zip(rows, outcomes, strict=True))
        / len(rows),
        "bins": bins,
    }


def performance_report(
    runs: Sequence[dict[str, Any]], observations: Sequence[dict[str, Any]]
) -> dict[str, Any]:
    rows = shadow_returns(runs, observations)
    ordered = _chronological(runs)
    equity = [r["account"]["equity"] for r in ordered]
    risk_rows = risk_calibration(ordered, observations)
    confidence_rows = confidence_calibration(ordered, observations)
    return {
        "sessions": len(rows),
        "first_session": rows[0]["session"] if rows else None,
        "last_session": rows[-1]["session"] if rows else None,
        "series": {s: summarise([r[s] for r in rows]) for s in SERIES},
        "comparisons": comparisons(rows),
        "daily": rows,
        "account_return": equity[-1] / equity[0] - 1 if equity else None,
        "risk_calibration": summarise_risk(risk_rows),
        "confidence": summarise_confidence(confidence_rows),
    }
