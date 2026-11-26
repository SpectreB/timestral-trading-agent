"""Audit records read into per-agent views for the site (D-012)."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from agent.evaluation.metrics import performance_report
from agent.evaluation.scoring import score_forecasts
from agent.execution.audit import FileAuditStore

DEFAULT_REPO = "https://github.com/SpectreB/timestral-trading-agent"

AUDIT_BRANCH = "audit-log"

TOOLS_FOLDER = "agents/tools"  # the tool agent's records on the audit-log branch (D-018)

SIM_TITLE = "Simulation request"  # the Simulate workflow reacts to issues titled so (D-017)

SIM_BODY = (
    "Submitting this issue asks the agent to run one simulation now: live market data, "
    "Timestral forecasts, the LLM and the guardrails, on its current portfolio. No order is sent. "
    "The answer is posted here in about 6 minutes. At most one simulation a minute and "
    "20 a day."
)


def _pct(x: float, digits: int = 1) -> str:
    return f"{100 * x:.{digits}f}%"


def _spct(x: float, digits: int = 1) -> str:
    return f"{100 * x:+.{digits}f}%"


_OUTCOME_BADGE = {
    "accepted": ("accepted", "good", "✓"),
    "corrected": ("accepted with corrections", "warn", "!"),
    "overridden": ("circuit breaker override", "crit", "✕"),
    "fallback_hold": ("rejected, positions held", "crit", "✕"),
}


def _last_attempt(run: dict[str, Any]) -> dict[str, Any]:
    attempt: dict[str, Any] = run["decision"]["attempts"][-1]
    return attempt


def _analysis(run: dict[str, Any]) -> str:
    try:
        text = json.loads(_last_attempt(run)["answer"])["analysis"]
    except (ValueError, KeyError, TypeError):
        return ""
    return text if isinstance(text, str) else ""


def _corrections(run: dict[str, Any]) -> list[dict[str, Any]]:
    corrections: list[dict[str, Any]] = _last_attempt(run)["guardrails"]["corrections"]
    return [c for c in corrections if c["counted"]]


ET = ZoneInfo("America/New_York")


def _et(iso: str) -> datetime:
    return datetime.fromisoformat(iso).astimezone(ET)


def _points(runs: list[dict[str, Any]], valuations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The account's value over time: the start of every run (with its detail) and the samples
    taken between runs, oldest first. A sample taken before the first run is left out."""
    first = _et(runs[0]["started_at"])
    out = [
        {
            "at": _et(r["started_at"]),
            "equity": r["account"]["equity"],
            "label": r["run_id"],
            "extra": [
                f"drawdown from peak {_pct(r['account']['drawdown_from_peak'])}",
                _OUTCOME_BADGE[r["decision"]["outcome"]][0],
            ],
        }
        for r in runs
    ]
    out += [
        {"at": _et(v["at"]), "equity": v["equity"], "label": "account sample", "extra": []}
        for v in valuations
        if _et(v["at"]) > first
    ]
    return sorted(out, key=lambda p: p["at"])


_LEVEL_LABEL = {
    "alert": "alert",
    "reduce_only": "reduce-only mode",
    "cap_exposure_60": "exposure capped at 60%",
    "baseline_half_exposure_and_pause": "baseline at half exposure, LLM paused",
}

SLOT_HOURS = (10, 15)  # the two scheduled runs of the day, US Eastern time


def _next_slot(now: datetime) -> datetime:
    """The next scheduled slot after `now` (weekdays only; holidays are not modelled). Counted
    from the present, not from the last run: a run that did not happen must not make the
    page announce a slot that is already in the past."""
    et = now.astimezone(ZoneInfo("America/New_York"))
    day = et.replace(hour=0, minute=0, second=0, microsecond=0)
    while True:
        if day.weekday() < 5:
            for hour in SLOT_HOURS:
                if (slot := day.replace(hour=hour)) > et:
                    return slot
        day += timedelta(days=1)


def _alerts(run: dict[str, Any], fills: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """What deserves attention at the last run: shown at the top, never folded away."""
    out: list[tuple[str, str]] = []
    attempt = _last_attempt(run)
    outcome = run["decision"]["outcome"]
    if outcome == "fallback_hold":
        reason = attempt["guardrails"]["rejection_reason"]
        out.append(("crit", f"LLM proposal rejected ({reason}): positions held"))
    if outcome == "overridden":
        out.append(("crit", "Circuit breaker override: the LLM was bypassed"))
    if n := len(_corrections(run)):
        out.append(("warn", f"{n} guardrail correction(s) at the last run"))
    actions = [a for a in attempt["guardrails"]["drawdown_actions"] if a != "alert"]
    if actions:
        out.append(
            ("crit", "Circuit breaker active: " + ", ".join(_LEVEL_LABEL[a] for a in actions))
        )
    elif attempt["guardrails"]["drawdown_actions"]:
        out.append(("warn", "Drawdown alert level reached"))
    for f in fills:
        s = f["summary"]
        if f["run_id"] == run["run_id"] and (
            s["n_not_sent"] or s["n_not_found"] or s["n_unfilled"]
        ):
            out.append(("warn", f"{s['n_unfilled']} unfilled, {s['n_not_sent']} unsent order(s)"))
    return out


def _cumulative(daily: list[dict[str, Any]], series: str) -> list[float]:
    out, wealth = [0.0], 1.0
    for row in daily:
        wealth *= 1 + row[series]
        out.append(wealth - 1)
    return out


SIM_CHOICES: dict[str, tuple[str, tuple[tuple[str, str], ...]]] = {
    "agent": (
        "Agent",
        (
            ("tools", "Tool agent (final)"),
            ("pipeline", "Pipeline agent (reference)"),
            ("news", "Tool agent + news covariates"),
        ),
    ),
    "stance": (
        "Risk stance",
        (
            ("balanced", "Balanced: the live limits"),
            ("cautious", "Cautious: 30% cash, small positions"),
            ("bold", "Bold: larger positions, no leverage"),
        ),
    ),
    "start": (
        "Start from",
        (
            ("current", "Its current portfolio"),
            ("cash", "All cash"),
            ("equal", "Equal weight, 90% invested"),
        ),
    ),
    "universe": (
        "Universe",
        (
            ("standard", "The 15 live assets"),
            ("wide", "Wide: 40 assets"),
        ),
    ),
}

_TOOL_LABEL = {
    "get_forecasts": "read Timestral's forecasts",
    "evaluate_portfolio": "had Timestral evaluate a candidate portfolio",
    "get_baseline": "looked at the Timestral rule",
}


def _tool_calls(run: dict[str, Any]) -> list[dict[str, Any]]:
    return list(run["decision"].get("tool_calls") or [])


@dataclass(frozen=True)
class AgentView:
    """One agent on the page (D-018): its records and where they live in the audit log."""

    key: str
    title: str
    summary: str
    runs: list[dict[str, Any]]
    fills: list[dict[str, Any]]
    scores: list[dict[str, Any]]
    report: dict[str, Any] | None
    prefix: str = ""  # folder of its records on the audit-log branch
    replay: dict[str, Any] | None = None  # its replay report (D-015), shown under Performance
    replay_folder: str = ""  # where the replay records live in the repository
    valuations: list[dict[str, Any]] = field(default_factory=list)  # hourly samples of the account


_TAGLINE = {
    "tools": "The final agent: the LLM questions Timestral through tools, then builds.",
    "pipeline": "The reference: Timestral and a rule set a baseline, the LLM adjusts it.",
}


def _pass_share(counts: dict[str, tuple[int, int]]) -> str:
    passed, total = sum(c[0] for c in counts.values()), sum(c[1] for c in counts.values())
    return f"{passed}/{total} ({100 * passed / total:.0f}%)" if total else "n/a"


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    mid = len(ordered) // 2
    return float(ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2)


def _measures(a: AgentView, evals: dict[str, Any] | None) -> dict[str, str]:
    """Concrete measures of one agent, as text: how it decides, what risk it takes, how it
    executes, how it behaved in tests, how it did on the past. 'n/a' when not measurable yet."""
    out: dict[str, str] = {}
    runs = a.runs
    if runs:
        n = len(runs)
        outcomes = Counter(r["decision"]["outcome"] for r in runs)
        port = [r["portfolios"] for r in runs]

        def moved(x: dict[str, Any], other: str) -> float:
            return float(100 * sum(abs(x["final"][s] - x[other][s]) for s in x["final"]))

        ratios = []
        for r in runs:
            vols = (r.get("risk") or {}).get("portfolio_vol") or {}
            if vols:
                v = vols[max(vols, key=int)]
                if v.get("final") and v.get("baseline"):
                    ratios.append(v["final"] / v["baseline"])
        traded, away = (
            _median([moved(x, "current") for x in port]),
            _median([moved(x, "baseline") for x in port]),
        )
        calls = [len(_tool_calls(r)) for r in runs]
        out["calls"] = f"{sum(calls) / n:.1f}" if any(calls) else "1 fixed run"
        out["accepted"] = f"{outcomes['accepted']}/{n}"
        out["corrections"] = f"{sum(len(_corrections(r)) for r in runs) / n:.1f}"
        out["traded"], out["away"] = f"{traded:.0f}", f"{away:.0f}"
        out["risk"] = f"{sum(ratios) / len(ratios):.2f}x" if ratios else "n/a"
        out["invested"] = _pct(sum(1 - x["final_cash"] for x in port) / n, 0)
        sums = [f["summary"] for f in a.fills]
        if sums:
            out["filled"] = f"{sum(x['n_filled'] for x in sums)}/{sum(x['n_orders'] for x in sums)}"
            slips = [
                x["slippage_bps_weighted"] for x in sums if x["slippage_bps_weighted"] is not None
            ]
            if slips:
                out["slippage"] = f"{sum(slips) / len(slips):+.1f} bps"
    rep = a.replay
    if rep and rep.get("sessions"):
        mine = rep["series"]["agent"]
        out["past_return"] = _spct(mine["cumulative_return"], 1)
        out["past_vol"] = _pct(mine["annualised_volatility"], 1)
        out["past_dd"] = _pct(mine["max_drawdown"], 1)
        five = next((r for r in rep["risk_calibration"] if r["horizon"] == 5), None)
        if five:
            out["past_cov"] = _pct(five["inside_80"], 0)
    if evals:
        res = evals.get("tools" if a.key == "tools" else "pipeline")
        if res:
            focus = evals.get("tools_focus") if a.key == "tools" else None
            out["tests"] = _pass_share(_scenario_counts(res, focus))
    return out


_MEASURE_ROWS: tuple[tuple[str, str, str], ...] = (
    ("Decisions", "calls", "Timestral calls per decision"),
    ("", "accepted", "Accepted as proposed"),
    ("", "corrections", "Guardrail corrections per run"),
    ("", "traded", "Points traded per run (median)"),
    ("", "away", "Points away from the Timestral rule (median)"),
    ("Risk", "risk", "Forecast risk against the rule"),
    ("", "invested", "Invested share"),
    ("Execution", "filled", "Orders filled"),
    ("", "slippage", "Average slippage"),
    ("Tests", "tests", "Behaviour tests passed (more repeats for the tool agent)"),
    ("The past", "past_return", "Return over 63 replayed sessions"),
    ("", "past_vol", "Volatility, annualised"),
    ("", "past_dd", "Largest fall from a peak"),
    ("", "past_cov", "Risk interval hit (5 days, target 80%)"),
)


def _scenario_counts(
    results: dict[str, Any], focus: dict[str, Any] | None = None
) -> dict[str, tuple[int, int]]:
    """Passes out of repeats per scenario; a dedicated longer series replaces the short one."""
    counts = {
        s["key"]: (sum(r["passed"] for r in s["runs"]), len(s["runs"]))
        for s in results["scenarios"]
    }
    for key, series in (focus or {}).items():
        if key in counts and isinstance(series, list) and series:
            counts[key] = (sum(bool(x) for x in series), len(series))
    return counts


PIPELINE_SUMMARY = (
    "Timestral always runs; a rule turns its forecast intervals into a baseline portfolio and "
    "the LLM adjusts it, at most +15 points per asset. The reference, unchanged since the start."
)

TOOLS_SUMMARY = (
    "The LLM queries Timestral through tools: forecasts at the horizon it chooses, a forecast of "
    "any candidate portfolio, a check against the guardrails, the rule as a reference. It builds "
    "the portfolio itself, within the same absolute limits. The final agent."
)


def _view(store: FileAuditStore, key: str, title: str, summary: str, prefix: str) -> AgentView:
    runs = chronological(store.read_runs())
    observations = store.read_observations()
    return AgentView(
        key,
        title,
        summary,
        runs,
        store.read_fills(),
        score_forecasts(runs, observations),
        performance_report(runs, observations),
        prefix,
        valuations=store.read_valuations(),
    )


def chronological(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Oldest first; for runs at the same instant the morning run comes before the afternoon."""
    return sorted(runs, key=lambda r: (r["started_at"], r["mode"] != "morning"))


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def _load_evals(folder: Path) -> dict[str, Any] | None:
    """The behaviour tests of D-016: the pipeline agent, and the tool agent's final prompt."""
    found = {
        "pipeline": _load_json(folder / "results.json"),
        "tools": _load_json(folder / "tools_v2" / "results.json"),
        "tools_focus": _load_json(folder / "tools_v2" / "focus_10_runs_v2.json"),
    }
    return found if found["pipeline"] or found["tools"] else None
