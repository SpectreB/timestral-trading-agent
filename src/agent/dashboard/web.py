from __future__ import annotations

import argparse
import json
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent.checks.risk import Z80
from agent.config import AgentConfig, AssetType, evolve, tool_agent_config
from agent.dashboard.records import (
    _MEASURE_ROWS,
    _TAGLINE,
    _TOOL_LABEL,
    AUDIT_BRANCH,
    DEFAULT_REPO,
    PIPELINE_SUMMARY,
    SIM_BODY,
    SIM_CHOICES,
    SIM_TITLE,
    SLOT_HOURS,
    TOOLS_FOLDER,
    TOOLS_SUMMARY,
    AgentView,
    _alerts,
    _analysis,
    _corrections,
    _cumulative,
    _load_evals,
    _load_json,
    _measures,
    _next_slot,
    _points,
    _scenario_counts,
    _tool_calls,
    _view,
    chronological,
)
from agent.evaluation.metrics import MIN_SESSIONS_FOR_CI, SERIES
from agent.evaluation.scoring import calibration
from agent.execution.audit import FileAuditStore
from agent.jobs.simulate import DEFAULT_OPTIONS, OPTIONS

WEB_DIR = Path(__file__).resolve().parents[3] / "web"

_LEVELS = ("alert", "no new risk", "60% invested at most", "half the baseline, LLM paused")


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="seconds")


def _forecast(run: dict[str, Any]) -> dict[str, Any]:
    p = run["portfolios"]
    names = {a["symbol"]: a for a in run["config"]["universe"]}
    assets = []
    for symbol, by_h in run["forecasts"].items():
        horizons = sorted(by_h, key=int)
        assets.append(
            {
                "symbol": symbol,
                "name": names[symbol]["name"],
                "type": names[symbol]["type"],
                "rule_weight": p["baseline"][symbol],
                "final_weight": p["final"][symbol],
                "horizons": [
                    {
                        "h": int(h),
                        "q10": by_h[h]["q10"],
                        "median": by_h[h]["median"],
                        "q90": by_h[h]["q90"],
                        "spread": by_h[h]["width_80"],
                    }
                    for h in horizons
                ],
            }
        )
    assets.sort(key=lambda a: -a["horizons"][-1]["spread"])
    return {
        "model": run["config"]["forecast"]["model_id"],
        "last_close": run["data"]["last_session"],
        "context_days": run["config"]["forecast"]["context_length"],
        "assets": assets,
        "raw": run["forecasts"],
        "tool_quantiles": _tool_quantiles(run),
    }


def _tool_quantiles(run: dict[str, Any]) -> dict[str, Any] | None:
    answers = [
        c["result"]
        for c in _tool_calls(run)
        if c["name"] == "get_forecasts" and "error" not in c["result"]
    ]
    if not answers:
        return None
    best = max(answers, key=lambda r: (r["horizon"], len(r["assets"])))
    return {"horizon": best["horizon"], "assets": best["assets"]}


def _holdings(run: dict[str, Any]) -> list[dict[str, Any]]:
    p = run["portfolios"]
    proposed = p["proposed"] or {}
    shares = (run.get("risk") or {}).get("risk_contributions_final", {})
    equity = run["account"]["equity"]
    names = {a["symbol"]: a for a in run["config"]["universe"]}
    out = []
    for symbol in sorted(p["final"], key=lambda s: -p["final"][s]):
        view = proposed.get(symbol) or {}
        out.append(
            {
                "symbol": symbol,
                "name": names[symbol]["name"],
                "type": names[symbol]["type"],
                "current": p["current"][symbol],
                "rule": p["baseline"][symbol],
                "proposed": view.get("weight"),
                "final": p["final"][symbol],
                "value": p["final"][symbol] * equity,
                "confidence": view.get("confidence"),
                "rationale": view.get("rationale", ""),
                "risk_share": shares.get(symbol),
            }
        )
    return out


def _tool_steps(run: dict[str, Any]) -> list[dict[str, Any]]:
    steps = []
    for c in _tool_calls(run):
        result = c["result"]
        try:
            args = json.loads(c["arguments"]) if isinstance(c["arguments"], str) else c["arguments"]
        except ValueError:
            args = {}
        if "error" in result:
            seen = f"refused: {result['error']}"
        elif c["name"] == "evaluate_portfolio":
            pf = result["timestral_portfolio_forecast"]
            seen = (
                f"invested {result['invested']:.0%}, probability of a loss "
                f"{pf['probability_of_loss']:.0%}, bad case (5%) {pf['q05']:+.1%}, guardrails: "
                f"{result['guardrails']['would_be']}"
            )
        elif c["name"] == "get_forecasts":
            seen = f"{len(result['assets'])} asset(s) at {result['horizon']} day(s)"
        else:
            seen = "the rule-based weights"
        steps.append(
            {
                "tool": c["name"],
                "label": _TOOL_LABEL.get(c["name"], c["name"]),
                "horizon": (args or {}).get("horizon"),
                "result": seen,
            }
        )
    return steps


def _decision(run: dict[str, Any], repo: str, prefix: str) -> dict[str, Any]:
    attempt = run["decision"]["attempts"][-1]
    return {
        "run_id": run["run_id"],
        "at": run["started_at"],
        "mode": run["mode"],
        "outcome": run["decision"]["outcome"],
        "execution": run["execution_status"],
        "model": attempt["model"],
        "prompt": run["decision"]["prompt_version"],
        "code": run["git_sha"][:7],
        "analysis": _analysis(run),
        "corrections": [
            {"rule": c["rule"], "symbol": c["symbol"], "before": c["before"], "after": c["after"]}
            for c in _corrections(run)
        ],
        "tool_steps": _tool_steps(run),
        "cash": run["portfolios"]["final_cash"],
        "drawdown": run["account"]["drawdown_from_peak"],
        "portfolio_vol": _portfolio_vol(run),
        "record": f"{repo}/blob/{AUDIT_BRANCH}/{prefix}runs/{run['run_id']}.json",
    }


def _portfolio_vol(run: dict[str, Any]) -> dict[str, Any] | None:
    vols = (run.get("risk") or {}).get("portfolio_vol") or {}
    if not vols:
        return None
    h = max(vols, key=int)
    v = vols[h]
    return {"horizon": int(h), "final": v.get("final"), "rule": v.get("baseline")}


def _orders(a: AgentView) -> list[dict[str, Any]]:
    out = []
    reconciled = {f["run_id"] for f in a.fills}
    for f in reversed(a.fills):
        for o in f["orders"]:
            filled = o["status"] == "filled" and o["avg_fill_price"] is not None
            out.append(
                {
                    "run_id": f["run_id"],
                    "at": o.get("filled_at") or o.get("submitted_at"),
                    "symbol": o["symbol"],
                    "side": o["side"],
                    "quantity": o["filled_qty"] or o["quantity"],
                    "status": o["status"],
                    "price": o["avg_fill_price"] if filled else None,
                    "value": o["filled_qty"] * o["avg_fill_price"] if filled else None,
                    "slippage_bps": o.get("slippage_bps"),
                    "limit_price": o.get("limit_price"),
                    "reference_price": o.get("reference_price"),
                    "submitted_at": o.get("submitted_at"),
                }
            )
    for run in reversed(a.runs):
        if run["run_id"] in reconciled or run["execution_status"] != "live":
            continue
        out += [
            {
                "run_id": run["run_id"],
                "at": run["started_at"],
                "symbol": o["symbol"],
                "side": o["side"],
                "quantity": o["quantity"],
                "status": "sent",
                "price": None,
                "value": None,
                "slippage_bps": None,
                "limit_price": o.get("limit_price"),
                "reference_price": o.get("reference_price"),
                "submitted_at": run["started_at"],
            }
            for o in run["orders"]
        ]
    return out


def _replay(report: dict[str, Any] | None, repo: str, folder: str) -> dict[str, Any] | None:
    if not report or report.get("sessions", 0) < 1:
        return None
    daily = report["daily"]
    return {
        "sessions": report["sessions"],
        "first": report["first_session"],
        "last": report["last_session"],
        "account_return": report["account_return"],
        "sessions_list": [d["session"] for d in daily],
        "curves": {s: _cumulative(daily, s) for s in SERIES},
        "stats": {
            s: {
                "return": v["cumulative_return"],
                "volatility": v["annualised_volatility"],
                "max_drawdown": v["max_drawdown"],
            }
            for s, v in report["series"].items()
        },
        "comparisons": [
            {
                "label": c["label"],
                "difference": c["cumulative_difference"],
                "sessions": c["sessions"],
                "conclusive": c["conclusive"],
                "measured": c["ci95_mean_daily"] is not None,
            }
            for c in report["comparisons"]
        ],
        "risk_calibration": report.get("risk_calibration", []),
        "readme": f"{repo}/blob/main/{folder}/README.md",
    }


def _tokens(run: dict[str, Any]) -> tuple[int, int]:
    calls = [*(run["decision"].get("steps") or []), *run["decision"]["attempts"]]
    return (
        sum(int(c.get("prompt_tokens") or 0) for c in calls),
        sum(int(c.get("completion_tokens") or 0) for c in calls),
    )


# Mistral's list price for Mistral Large 4, USD per million tokens, read on 2026-11-24 at
# https://docs.mistral.ai/inference/pricing (the page mistral.ai/pricing/api redirects to).
PRICE_IN_PER_M, PRICE_OUT_PER_M = 0.70, 2.10
SESSIONS_PER_MONTH = 21


def _cost(runs: list[dict[str, Any]]) -> dict[str, str]:
    if not runs:
        return {}
    used = [_tokens(r) for r in runs]
    tin = sum(u[0] for u in used) / len(used)
    tout = sum(u[1] for u in used) / len(used)
    usd = (tin * PRICE_IN_PER_M + tout * PRICE_OUT_PER_M) / 1_000_000
    return {
        "tokens": f"{(tin + tout) / 1000:.0f}k ({tin / 1000:.0f}k in, {tout / 1000:.0f}k out)",
        "usd": f"${usd:.3f}",
        "usd_month": f"${usd * len(SLOT_HOURS) * SESSIONS_PER_MONTH:.2f}",
    }


def _traits(key: str) -> list[list[str]]:
    if key == "tools":
        cfg = tool_agent_config()
        return [
            ["Who decides", "the LLM"],
            ["Timestral", f"on request, up to {cfg.tools.max_calls} tool calls"],
            ["LLM freedom", "any weights within the limits"],
            ["Model", cfg.forecast.model_id.split("/")[-1]],
        ]
    if key == "pipeline":
        cfg = AgentConfig()
        return [
            ["Who decides", "the Timestral rule, the LLM adjusts"],
            ["Timestral", f"always, once, all {len(cfg.universe)} assets"],
            [
                "LLM freedom",
                f"at most +{100 * cfg.risk.max_increase_vs_baseline:.0f} pts over the rule",
            ],
            ["Model", cfg.forecast.model_id.split("/")[-1]],
        ]
    return []


def agent_data(
    a: AgentView, evals: dict[str, Any] | None, now: datetime, repo: str
) -> dict[str, Any]:
    base = {
        "key": a.key,
        "title": a.title,
        "tagline": _TAGLINE.get(a.key, a.summary),
        "summary": a.summary,
        "traits": _traits(a.key),
        "records": f"{repo}/tree/{AUDIT_BRANCH}/{a.prefix}",
        "started": bool(a.runs),
        "measures": {**_measures(a, evals), **_cost(a.runs)},
        "replay": _replay(a.replay, repo, a.replay_folder),
    }
    if not a.runs:
        return base
    first, last = a.runs[0], a.runs[-1]
    series = _points(a.runs, a.valuations)
    report = a.report or {}
    return {
        **base,
        "start": {"equity": first["account"]["equity"], "at": first["started_at"]},
        "now": {
            "equity": series[-1]["equity"],
            "at": _iso(series[-1]["at"]),
            "source": "run" if series[-1]["label"] != "account sample" else "sample",
        },
        "range80": _range80(series[-1]["equity"], _portfolio_vol(last)),
        "n_runs": len(a.runs),
        "next_run": _iso(_next_slot(now)),
        "alerts": [{"level": k, "text": t} for k, t in _alerts(last, a.fills)],
        "series": [
            {"at": _iso(p["at"]), "equity": p["equity"], "label": p["label"]} for p in series
        ],
        "decision": _decision(last, repo, a.prefix),
        "holdings": _holdings(last),
        "forecast": _forecast(last),
        "orders": _orders(a),
        "runs": [
            {
                "run_id": r["run_id"],
                "at": r["started_at"],
                "outcome": r["decision"]["outcome"],
                "execution": r["execution_status"],
                "equity": r["account"]["equity"],
                "orders": len(r["orders"]),
                "corrections": len(_corrections(r)),
                "record": f"{repo}/blob/{AUDIT_BRANCH}/{a.prefix}runs/{r['run_id']}.json",
            }
            for r in reversed(a.runs)
        ],
        "calibration": calibration(a.scores),
        "risk_calibration": report.get("risk_calibration", []),
    }


def _rules() -> list[dict[str, str]]:
    cfg = AgentConfig()
    r = cfg.risk
    levels = ", ".join(
        f"{abs(lv.threshold):.0%} {label}"
        for lv, label in zip(cfg.drawdown_levels, _LEVELS, strict=True)
    )
    return [
        {
            "title": "Unusable answers are rejected",
            "text": "Invalid JSON or weights: the positions are kept.",
        },
        {
            "title": "Violations are corrected",
            "text": f"Long only, no leverage, cash {r.min_cash:.0%} or more, "
            f"{r.max_weight_etf:.0%} per ETF, {r.max_weight_stock:.0%} per stock, "
            f"{r.max_weight_group:.0%} per sector.",
        },
        {"title": "Losses trigger a circuit breaker", "text": f"From the peak: {levels}."},
        {
            "title": "Orders are bounded",
            "text": f"{cfg.execution.no_trade_band:.0%} no-trade band, "
            f"{cfg.execution.max_turnover_per_run:.0%} turnover cap, limit orders, kill switch.",
        },
        {
            "title": "An independent final check",
            "text": "A second, deliberately simple check runs before any order.",
        },
    ]


def _inputs(cov: dict[str, Any] | None) -> list[dict[str, Any]]:
    pipe, tool = AgentConfig(), tool_agent_config()
    etfs = sum(a.type is AssetType.ETF for a in pipe.universe)
    ex = pipe.execution

    def items(*pairs: tuple[str, str]) -> list[dict[str, str]]:
        return [{"text": t, "help": h} for t, h in pairs]

    return [
        {
            "label": "Data",
            "items": items(
                ("Alpaca daily bars, completed sessions only",
                 "Daily prices from Alpaca; today's unfinished bar is never used."),
                (f"{len(pipe.universe)} assets: {etfs} ETFs, {len(pipe.universe) - etfs} stocks",
                 "ETFs across regions, sectors, bonds and gold, plus five large stocks."),
                (f"{pipe.forecast.context_length}-day context",
                 f"Timestral sees the last {pipe.forecast.context_length} closes of each asset."),
            ),
        },
        {
            "label": "Forecast",
            "items": items(
                (f"{tool.forecast.model_id.split('/')[-1]} (tool agent)",
                 "The largest Timestral, used by the final agent."),
                (f"{pipe.forecast.model_id.split('/')[-1]} (pipeline agent)",
                 "A smaller Timestral; the backtest found the sizes indistinguishable."),
                (f"quantiles, 1 to {pipe.forecast.horizon} days ahead",
                 "A distribution of the cumulative return for each horizon, not one number."),
                ("run locally on CPU", "No GPU, no API: the model runs inside each workflow run."),
            ),
        },
        {
            "label": "Decision",
            "items": items(
                (pipe.llm.model, "Mistral Large 4, pinned to an exact version."),
                ("native reasoning",
                 "The model reasons before answering; the trace is kept in the audit log."),
                ("strict JSON schema", "The answer must match a schema, or it is rejected."),
            ),
        },
        {
            "label": "Covariates",
            "items": items(
                ("news sentiment (Alpaca News)",
                 "Each article scored for sentiment and relevance, per stock."),
                ("company events and macro dates (FRED)",
                 "Earnings and company events from the news, macro release dates from FRED."),
                (f"analyst {pipe.news.analyst_model}",
                 "The smaller Mistral model that scores the articles."),
                ("measured, not adopted", "No gain over prices alone: the live agents use prices.")
                if cov
                else ("built, not measured", "Integrated, not yet measured against prices alone."),
            ),
        },
        {
            "label": "Orders",
            "items": items(
                ("Alpaca paper accounts", "One per agent, $100,000 each, fictitious money."),
                ("marketable limit orders",
                 f"Limit {ex.limit_price_tolerance:.1%} beyond the last price: fills like a "
                 "market order, never far from it."),
                ("idempotent order ids",
                 "One id per decision and asset: a repeated run cannot send an order twice."),
            ),
        },
    ]  # fmt: skip


def _behaviour(evals: dict[str, Any] | None) -> dict[str, Any] | None:
    if not evals:
        return None
    pipeline, tools = evals.get("pipeline"), evals.get("tools")
    base = pipeline or tools
    if not base:
        return None
    p = _scenario_counts(pipeline) if pipeline else {}
    t = _scenario_counts(tools, evals.get("tools_focus")) if tools else {}
    return {
        "scenarios": [
            {
                "title": s["title"],
                "expectation": s["expectation"],
                "tools": list(t[s["key"]]) if s["key"] in t else None,
                "pipeline": list(p[s["key"]]) if s["key"] in p else None,
            }
            for s in base["scenarios"]
        ],
        "stability": {
            key: {
                "calls": res["stability"]["calls"],
                "max_difference": res["stability"]["max_pairwise_turnover"],
            }
            for key, res in (("pipeline", pipeline), ("tools", tools))
            if res
        },
    }


def _covariates(cov: dict[str, Any] | None) -> dict[str, Any] | None:
    if not cov:
        return None
    return {
        "model": cov.get("model", "Timestral"),
        "horizon": cov["horizon"],
        "origins": cov["origins"],
        "first": cov["first_origin"],
        "last": cov["last_origin"],
        "stocks": cov.get("stocks_with_news", cov["stocks"]),
        "etfs": cov["etfs"],
        "summary": cov["summary"],
        "paired": cov["paired"],
        "verdicts": {v["variant"]: v["decision"] for v in cov["verdicts"]},
    }


def _sim_options(run_id: str) -> dict[str, str]:
    chosen = dict(DEFAULT_OPTIONS)
    by_value = {v: k for k, values in OPTIONS.items() for v in values}
    for token in run_id.split("-")[1:]:
        if token in by_value:
            chosen[by_value[token]] = token
    return chosen


def _sim_portfolio(run: dict[str, Any]) -> list[dict[str, Any]]:
    current, final = run["portfolios"]["current"], run["portfolios"]["final"]
    kinds = {a["symbol"]: a["type"] for a in run["config"]["universe"]}
    held = sorted((s for s in final if final[s] >= 0.0005), key=lambda s: -final[s])
    exited = sorted(s for s in final if final[s] < 0.0005 <= current[s])
    return [
        {"symbol": s, "type": kinds[s], "weight": final[s], "before": current[s]}
        for s in (*held, *exited)
    ]


def _range80(equity: float, vol: dict[str, Any] | None) -> dict[str, Any] | None:
    if not vol or not vol.get("final"):
        return None
    sigma = vol["final"]
    return {
        "horizon": vol["horizon"],
        "low": equity * (1 - Z80 * sigma),
        "high": equity * (1 + Z80 * sigma),
    }


def _sim_outlook(run: dict[str, Any]) -> dict[str, Any]:
    p, equity = run["portfolios"], run["account"]["equity"]
    cur, fin = p["current"], p["final"]
    vols = (run.get("risk") or {}).get("portfolio_vol") or {}
    horizon = max(vols, key=int) if vols else None
    sigma = vols[horizon].get("final") if horizon else None
    return {
        "equity": equity,
        "moved": sum(abs(fin[s] - cur[s]) for s in fin),
        "added": sorted(s for s in fin if fin[s] >= 0.0005 and cur[s] < 0.0005),
        "removed": sorted(s for s in fin if cur[s] >= 0.0005 and fin[s] < 0.0005),
        "horizon": int(horizon) if horizon else None,
        "range80": [equity * (1 - Z80 * sigma), equity * (1 + Z80 * sigma)] if sigma else None,
    }


def _simulations(sims: list[dict[str, Any]], repo: str) -> dict[str, Any]:
    return {
        "issue_url": f"{repo}/issues/new",
        "title": SIM_TITLE,
        "body": SIM_BODY,
        "choices": [
            {"key": key, "label": label, "options": [{"value": v, "label": t} for v, t in opts]}
            for key, (label, opts) in SIM_CHOICES.items()
        ],
        "recent": [
            {
                "run_id": r["run_id"],
                "at": r["started_at"],
                "requested_by": r.get("requested_by") or "unknown",
                "outcome": r["decision"]["outcome"],
                "orders": len(r["orders"]),
                "analysis": _analysis(r),
                "options": _sim_options(r["run_id"]),
                "assets": len(r["config"]["universe"]),
                "cash": r["portfolios"]["final_cash"],
                "portfolio": _sim_portfolio(r),
                "outlook": _sim_outlook(r),
                "record": f"{repo}/blob/{AUDIT_BRANCH}/simulations/runs/{r['run_id']}.json",
            }
            for r in list(reversed(sims))[:10]
        ],
    }


_SHOWN_ELSEWHERE = {"tests", "past_return", "past_vol", "past_dd", "past_cov"}

MEASURE_HELP: dict[str, str] = {
    "calls": "How often the LLM queried Timestral before deciding. The pipeline agent runs "
    "Timestral once, always.",
    "accepted": "Runs whose decision the guardrails accepted without any correction.",
    "corrections": "Guardrail corrections per run, on average. 0: the LLM stayed within every "
    "limit.",
    "traded": "Points of the portfolio changed per run (sum of the weight changes), median. "
    "Lower is calmer.",
    "away": "Distance from the final portfolio to the Timestral rule, in points (sum of the "
    "differences), median. 0 is the rule itself.",
    "risk": "Forecast volatility of the final portfolio divided by the rule's. 1.00x: as much risk "
    "as the rule; below: less.",
    "invested": "Share of the account in assets rather than cash, on average.",
    "filled": "Orders the broker executed, out of the orders sent.",
    "slippage": "Fill price against the reference price, in basis points. Negative is better.",
    "tests": "Behaviour scenarios passed, out of their repeats (see Behaviour tests).",
    "past_return": "Return of the simulated account over the replayed sessions, costs included.",
    "past_vol": "Volatility of the daily returns in the replay, annualised.",
    "past_dd": "Largest fall from a peak in the replay.",
    "tokens": "Tokens sent to and received from the Mistral LLM for one decision, tool rounds "
    "included, averaged over the live runs. Timestral itself runs on CPU in the workflow: no "
    "API cost.",
    "usd": "Those tokens at Mistral's list price for Mistral Large 4: $0.70 per million in, $2.10 "
    "per million out (docs.mistral.ai/inference/pricing, read 2026-11-24). Here the calls run on "
    "the sponsored plan's free tier: nothing is billed.",
    "usd_month": "The price per decision times 2 runs a day and 21 trading sessions a month. "
    "Decisions only: the daily news scoring by the smaller analyst model is not counted.",
    "past_cov": "Share of 5-day portfolio outcomes inside Timestral's 80% risk interval. "
    "Target: 80%.",
}

WHATS_NEXT: tuple[dict[str, str], ...] = (
    {
        "kind": "New covariates",
        "id": "D-019",
        "title": "News analysed into covariates for Timestral",
        "text": "Known-in-advance calendars (earnings, central banks) and market volatility, with "
        "the adoption rule written before the measurement.",
    },
    {
        "kind": "A wider scope",
        "id": "D-005",
        "title": "Asset universe: diversified ETFs + major single stocks (15 assets)",
        "text": "More single stocks, where company news should matter most.",
    },
    {
        "kind": "Conversation",
        "id": "D-018",
        "title": "A second agent that uses Timestral as a tool, on its own paper account",
        "text": "Talk with Mistral Large 4 about its choices, and leave a view it weighs at its "
        "next decision: same guardrails, on the record.",
    },
)


def site_data(
    agents: list[AgentView],
    sims: list[dict[str, Any]],
    now: datetime,
    repo: str = DEFAULT_REPO,
    evals: dict[str, Any] | None = None,
    covariates: dict[str, Any] | None = None,
    tables: list[str] | None = None,
) -> dict[str, Any]:
    cfg = AgentConfig()
    return {
        "generated_at": _iso(now),
        "repo": repo,
        "links": {
            "audit_log": f"{repo}/tree/{AUDIT_BRANCH}",
            "decisions": f"{repo}/blob/main/docs/decisions.md",
            "readme": f"{repo}#readme",
            "tables": [f"data/{t}" for t in tables or []],
        },
        "agents": [agent_data(a, evals, now, repo) for a in agents],
        "measure_rows": [
            {"group": g, "key": k, "label": lbl, "help": MEASURE_HELP.get(k, "")}
            for g, k, lbl in (
                *_MEASURE_ROWS,
                ("Cost", "tokens", "Mistral tokens per decision"),
                ("", "usd", "Price per decision, at list price"),
                ("", "usd_month", "Estimated per month"),
            )
            if k not in _SHOWN_ELSEWHERE
        ],
        "min_sessions_for_ci": MIN_SESSIONS_FOR_CI,
        "rules": _rules(),
        "drawdown_levels": [
            {"threshold": lv.threshold, "label": label}
            for lv, label in zip(cfg.drawdown_levels, _LEVELS, strict=True)
        ],
        "inputs": _inputs(covariates),
        "behaviour": _behaviour(evals),
        "covariates": _covariates(covariates),
        "simulations": _simulations(sims, repo),
        "next": list(WHATS_NEXT),
    }


def build(
    audit_dir: Path,
    out_dir: Path,
    now: datetime,
    repo: str = DEFAULT_REPO,
    replay_dir: Path | None = None,
    tool_replay_dir: Path | None = None,
    evals_dir: Path | None = None,
    covariates_dir: Path | None = None,
    web_dir: Path = WEB_DIR,
) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    for name in ("index.html", "app.js", "style.css"):
        shutil.copy(web_dir / name, out_dir / name)
    pipeline = _view(FileAuditStore(audit_dir), "pipeline", "Pipeline agent", PIPELINE_SUMMARY, "")
    pipeline = evolve(
        pipeline,
        replay=_load_json(replay_dir / "report.json") if replay_dir else None,
        replay_folder="docs/replay",
    )
    agents = [pipeline]
    tools_dir = audit_dir / TOOLS_FOLDER
    if (tools_dir / "runs").exists():
        tool = _view(
            FileAuditStore(tools_dir), "tools", "Tool agent", TOOLS_SUMMARY, f"{TOOLS_FOLDER}/"
        )
        tool = evolve(
            tool,
            replay=_load_json(tool_replay_dir / "report.json") if tool_replay_dir else None,
            replay_folder="docs/replay_tools",
        )
        agents.insert(0, tool)
    tables: list[str] = []
    for csv_file in sorted((audit_dir / "tables").glob("*.csv")):
        (out_dir / "data").mkdir(exist_ok=True)
        shutil.copy(csv_file, out_dir / "data" / csv_file.name)
        tables.append(csv_file.name)
    data = site_data(
        agents,
        chronological(FileAuditStore(audit_dir / "simulations").read_runs()),
        now,
        repo,
        _load_evals(evals_dir) if evals_dir else None,
        _load_json(covariates_dir / "results.json") if covariates_dir else None,
        tables,
    )
    path = out_dir / "data.json"
    path.write_text(json.dumps(data, indent=1, allow_nan=False), encoding="utf-8")
    return path


def main() -> None:  # pragma: no cover - CLI wiring, runs in the dashboard workflow
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit-dir", default="audit", type=Path)
    parser.add_argument("--out", default="site", type=Path)
    parser.add_argument("--replay-dir", default=Path("docs/replay"), type=Path)
    parser.add_argument("--tool-replay-dir", default=Path("docs/replay_tools"), type=Path)
    parser.add_argument("--evals-dir", default=Path("docs/evals"), type=Path)
    parser.add_argument("--covariates-dir", default=Path("docs/covariates"), type=Path)
    args = parser.parse_args()
    slug = os.environ.get("GITHUB_REPOSITORY")
    repo = f"https://github.com/{slug}" if slug else DEFAULT_REPO
    print(
        build(
            args.audit_dir,
            args.out,
            datetime.now(UTC),
            repo,
            args.replay_dir,
            args.tool_replay_dir,
            args.evals_dir,
            args.covariates_dir,
        )
    )


if __name__ == "__main__":  # pragma: no cover
    main()
