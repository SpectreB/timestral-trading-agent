"""The data behind the dashboard (D-012): one JSON file read by the React page in `web/`.

Python computes, the page only displays. Every figure comes from the audit records, the replay
reports, the behaviour tests and the covariate trial, through the same functions as before
(`agent.dashboard`), so the two views cannot disagree. Values stay raw (fractions, dollars,
ISO times): formatting belongs to the page.

The site has three parts, in the order a newcomer needs them:
- **Live**: what each agent holds, what it decided at its last run and why, Timestral's raw
  forecast behind it, the orders, the account value over time;
- **How it works**: the design, the guardrails, and the evidence that the decisions are sound;
- **Try it**: ask an agent to decide now on live data, without sending any order.

The previous server-rendered page is still written next to it (`classic.html`) as a fallback.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent.audit import FileAuditStore
from agent.config import AgentConfig, AssetType, tool_agent_config
from agent.dashboard import (
    _MEASURE_ROWS,
    _TAGLINE,
    _TOOL_LABEL,
    AUDIT_BRANCH,
    DEFAULT_REPO,
    PIPELINE_SUMMARY,
    SIM_BODY,
    SIM_CHOICES,
    SIM_TITLE,
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
from agent.dashboard import build as build_classic
from agent.metrics import MIN_SESSIONS_FOR_CI, SERIES
from agent.scoring import calibration

WEB_DIR = Path(__file__).resolve().parents[2] / "web"  # index.html, app.js, style.css

_LEVELS = ("alert", "no new risk", "60% invested at most", "half the baseline, LLM paused")


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- one agent


def _forecast(run: dict[str, Any]) -> dict[str, Any]:
    """Timestral's output at the run, as recorded: for each asset and each horizon the 10%
    and 90% quantiles, the median and the spread (width of the 80% interval, q90 - q10).
    With the weight the rule derives from it (inversely proportional to the spread) and the
    final weight, so the page can show the path from the raw forecast to the portfolio."""
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
    assets.sort(key=lambda a: -a["horizons"][-1]["spread"])  # riskiest first
    return {
        "model": run["config"]["forecast"]["model_id"],
        "last_close": run["data"]["last_session"],
        "context_days": run["config"]["forecast"]["context_length"],
        "assets": assets,
        "raw": run["forecasts"],  # exactly as stored in the audit record
        "tool_quantiles": _tool_quantiles(run),
    }


def _tool_quantiles(run: dict[str, Any]) -> dict[str, Any] | None:
    """The tool agent sees more of Timestral's distribution (7 quantiles) when it asks for
    forecasts; the last such answer, at the longest horizon it asked for."""
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
    """What the tool agent asked Timestral before deciding, in order (D-018)."""
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
    """Every order, most recent first: reconciled ones with what the broker did, sent ones that
    are not reconciled yet shown as such (never hidden)."""
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


def _traits(key: str) -> list[list[str]]:
    """What sets the agent apart, in keywords, read from its configuration (D-018)."""
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
    """Everything the page shows about one agent."""
    base = {
        "key": a.key,
        "title": a.title,
        "tagline": _TAGLINE.get(a.key, a.summary),
        "summary": a.summary,
        "traits": _traits(a.key),
        "records": f"{repo}/tree/{AUDIT_BRANCH}/{a.prefix}",
        "started": bool(a.runs),
        "measures": _measures(a, evals),
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


# --------------------------------------------------------------------------- the design


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
            "text": "Invalid JSON, an unknown or missing asset, weights that do not add up: the "
            "agent keeps its positions.",
        },
        {
            "title": "Violations are corrected and logged",
            "text": f"Long only, no leverage, cash at least {r.min_cash:.0%}, at most "
            f"{r.max_weight_etf:.0%} per ETF, {r.max_weight_stock:.0%} per stock, "
            f"{r.max_weight_group:.0%} per sector. Too many corrections and the answer is "
            "rejected.",
        },
        {"title": "Losses trigger a circuit breaker", "text": f"Drawdown from peak: {levels}."},
        {
            "title": "Orders are bounded",
            "text": f"No trade under {cfg.execution.no_trade_band:.0%} of the portfolio, turnover "
            f"capped at {cfg.execution.max_turnover_per_run:.0%} per run, limit orders within "
            f"{cfg.execution.limit_price_tolerance:.1%} of the last price, one order id per "
            "decision, paper accounts only, a kill switch per agent.",
        },
        {
            "title": "An independent final check",
            "text": "A deliberately simple second implementation re-verifies the core limits "
            "before any order leaves.",
        },
    ]


def _inputs(cov: dict[str, Any] | None) -> list[dict[str, Any]]:
    pipe, tool = AgentConfig(), tool_agent_config()
    etfs = sum(a.type is AssetType.ETF for a in pipe.universe)
    return [
        {
            "label": "Data",
            "items": [
                "Alpaca daily bars, completed sessions only",
                f"{len(pipe.universe)} assets: {etfs} ETFs, {len(pipe.universe) - etfs} stocks",
                f"{pipe.forecast.context_length}-day context",
            ],
        },
        {
            "label": "Forecast",
            "items": [
                f"{tool.forecast.model_id.split('/')[-1]} (tool agent)",
                f"{pipe.forecast.model_id.split('/')[-1]} (pipeline agent)",
                f"quantiles, 1 to {pipe.forecast.horizon} days ahead",
                "run locally on CPU",
            ],
        },
        {"label": "Decision", "items": [pipe.llm.model, "native reasoning", "strict JSON schema"]},
        {
            "label": "Covariates",
            "items": [
                "news sentiment (Alpaca News)",
                "company events and macro dates (FRED)",
                f"analyst {pipe.news.analyst_model}",
                "measured, not adopted" if cov else "built, not measured",
            ],
        },
        {
            "label": "Orders",
            "items": ["Alpaca paper accounts", "marketable limit orders", "idempotent order ids"],
        },
    ]


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
                "record": f"{repo}/blob/{AUDIT_BRANCH}/simulations/runs/{r['run_id']}.json",
            }
            for r in list(reversed(sims))[:10]
        ],
    }


# What is running, what comes next and what was deliberately left for later. Written by hand:
# keep it in step with docs/roadmap.md, every item with its identifier and title.
WHATS_NEXT: tuple[dict[str, str], ...] = (
    {
        "status": "running",
        "id": "D-018",
        "title": "Two agents on their own paper accounts",
        "text": "Both trade twice a day; the account value is sampled every hour of the US session "
        "since 2026-11-24. The live period is the clean test: the LLM cannot know what follows.",
    },
    {
        "status": "running",
        "id": "D-002",
        "title": "GitHub Actions cron for scheduling",
        "text": "Scheduled runs started late on 2026-11-24. Start times are watched; an external "
        "trigger is added if the delays persist.",
    },
    {
        "status": "next",
        "id": "D-019",
        "title": "News analysed into covariates for Timestral",
        "text": "News sentiment brought no gain. Next: fix the news matching for GOOGL, then "
        "measure "
        "what bears on risk: known-in-advance calendars (earnings, central banks) and market "
        "variables such as volatility, with the adoption rule written before the results.",
    },
    {
        "status": "next",
        "id": "D-016",
        "title": "How to check that the LLM behaves soundly",
        "text": "Repeat the seven behaviour scenarios as often for both agents, so their pass "
        "rates "
        "rest on the same number of runs.",
    },
    {
        "status": "later",
        "id": "D-020",
        "title": "A risk budget on the forecast portfolio volatility (draft)",
        "text": "Cap the portfolio risk Timestral forecasts, on top of the per-asset limits. Set "
        "aside until the live record shows whether it is needed.",
    },
    {
        "status": "later",
        "id": "D-012",
        "title": "Dashboard: React with a build step",
        "text": "Move the page to React with Vite and TypeScript; the data file stays the same.",
    },
    {
        "status": "later",
        "id": "D-021",
        "title": "Agent orchestration: our own loop",
        "text": "Consider LangGraph if the agents grow into several cooperating ones, for example "
        "the news analyst and the decision-maker exchanging messages.",
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
            "classic": "classic.html",
        },
        "agents": [agent_data(a, evals, now, repo) for a in agents],
        "measure_rows": [{"group": g, "key": k, "label": lbl} for g, k, lbl in _MEASURE_ROWS],
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


# --------------------------------------------------------------------------- build


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
    """Writes the site: the React page and its data, and the classic page as a fallback."""
    out_dir.mkdir(parents=True, exist_ok=True)
    classic = build_classic(
        audit_dir, out_dir, now, repo, replay_dir, tool_replay_dir, evals_dir, covariates_dir
    )
    classic.replace(out_dir / "classic.html")
    for name in ("index.html", "app.js", "style.css"):
        shutil.copy(web_dir / name, out_dir / name)
    pipeline = _view(FileAuditStore(audit_dir), "pipeline", "Pipeline agent", PIPELINE_SUMMARY, "")
    pipeline = replace(
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
        tool = replace(
            tool,
            replay=_load_json(tool_replay_dir / "report.json") if tool_replay_dir else None,
            replay_folder="docs/replay_tools",
        )
        agents.insert(0, tool)  # the final agent comes first
    tables = sorted(p.name for p in (out_dir / "data").glob("*.csv"))
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
