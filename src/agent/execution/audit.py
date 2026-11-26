"""Audit log (D-011): one immutable JSON record per run, plus derived flat tables. Files are never
overwritten, and the JSON is written last: its existence means the run is complete."""

from __future__ import annotations

import csv
import json
import os
import tempfile
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from agent.checks.risk import risk_snapshot
from agent.config import AgentConfig
from agent.decision.decide import Decision, DecisionInput
from agent.decision.schemas import Proposal
from agent.evaluation.metrics import SERIES, shadow_returns
from agent.evaluation.scoring import score_forecasts

SCHEMA_VERSION = 1


class AuditError(RuntimeError): ...


class OrderRecord(BaseModel):
    symbol: str
    side: str
    quantity: float
    limit_price: float | None
    client_order_id: str
    status: str
    reference_price: float | None = None


class RunRecord(BaseModel):
    schema_version: int = SCHEMA_VERSION
    run_id: str
    mode: str
    started_at: str
    git_sha: str
    config: dict[str, Any]
    data: dict[str, Any]
    account: dict[str, Any]
    forecasts: dict[str, Any]
    portfolios: dict[str, Any]
    decision: dict[str, Any]
    messages: list[dict[str, Any]]
    orders: list[OrderRecord]
    execution_status: str
    reasoning_file: str
    risk: dict[str, Any] | None = None
    requested_by: str | None = None
    news: dict[str, Any] | None = None


def run_id(now: datetime, mode: str) -> str:
    return f"{now.date().isoformat()}_{mode}"


def code_version() -> str:
    return os.environ.get("GITHUB_SHA", "local")


def _proposal_view(text: str) -> tuple[dict[str, Any] | None, float | None]:
    try:
        proposal = Proposal.model_validate_json(text)
    except ValidationError:
        return None, None
    positions = {
        p.symbol: {"weight": p.target_weight, "confidence": p.confidence, "rationale": p.rationale}
        for p in proposal.positions
    }
    return positions, proposal.cash_weight


def build_record(
    *,
    mode: str,
    now: datetime,
    cfg: AgentConfig,
    inp: DecisionInput,
    decision: Decision,
    orders: Sequence[OrderRecord],
    execution_status: str,
    label: str | None = None,
    requested_by: str | None = None,
) -> tuple[RunRecord, str]:
    rid = run_id(now, label or mode)
    horizons = tuple(range(1, cfg.forecast.horizon + 1))
    last = decision.attempts[-1]
    proposed, proposed_cash = _proposal_view(last.response.text)

    attempts = [
        {
            "model": a.response.model,
            "prompt_tokens": a.response.prompt_tokens,
            "completion_tokens": a.response.completion_tokens,
            "answer": a.response.text,
            "guardrails": a.guardrails.model_dump(),
        }
        for a in decision.attempts
    ]
    reasoning = "\n\n".join(
        [
            *(
                f"===== exploration turn {i + 1} =====\n{s.reasoning}"
                for i, s in enumerate(decision.steps)
            ),
            *(
                f"===== attempt {i + 1} =====\n{a.response.reasoning}"
                for i, a in enumerate(decision.attempts)
            ),
        ]
    )
    record = RunRecord(
        run_id=rid,
        mode=mode,
        started_at=now.isoformat(timespec="seconds"),
        git_sha=code_version(),
        config=cfg.model_dump(mode="json"),
        data={
            "last_session": inp.closes.index[-1].date().isoformat(),
            "n_sessions": len(inp.closes),
            "symbols": list(inp.closes.columns),
        },
        account={
            "equity": inp.equity,
            "drawdown_from_peak": inp.drawdown,
        },
        forecasts={s: f.summary(horizons)["horizons"] for s, f in inp.forecasts.items()},
        portfolios={
            "current": dict(inp.current),
            "baseline": dict(inp.baseline),
            "proposed": proposed,
            "proposed_cash": proposed_cash,
            "final": decision.target_weights,
            "final_cash": decision.cash,
        },
        decision={
            "agent": cfg.agent,
            "prompt_version": decision.prompt_version,
            "outcome": decision.outcome,
            "attempts": attempts,
            "steps": [
                {
                    "model": s.model,
                    "prompt_tokens": s.prompt_tokens,
                    "completion_tokens": s.completion_tokens,
                    "text": s.text,
                    "tool_calls": [c.model_dump() for c in s.tool_calls],
                }
                for s in decision.steps
            ],
            "tool_calls": decision.tool_calls,
        },
        messages=decision.messages,
        orders=list(orders),
        execution_status=execution_status,
        reasoning_file=f"{rid}_reasoning.txt",
        requested_by=requested_by,
        risk=risk_snapshot(
            inp.forecasts,
            inp.closes,
            (1, cfg.forecast.horizon),
            {
                "current": dict(inp.current),
                "baseline": dict(inp.baseline),
                "proposed": (
                    {s: v["weight"] for s, v in proposed.items()} if proposed is not None else None
                ),
                "final": decision.target_weights,
            },
        ),
    )
    return record, reasoning


def _dump(payload: Any) -> str:
    return json.dumps(payload, indent=1, sort_keys=False, allow_nan=False) + "\n"


def _write_once(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        try:
            os.link(tmp, path)  # fails if `path` exists: no silent overwrite
        except FileExistsError:
            raise AuditError(f"{path.name} already exists: audit records are immutable") from None
    finally:
        tmp.unlink(missing_ok=True)


class FileAuditStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def _run_path(self, rid: str) -> Path:
        return self.root / "runs" / f"{rid}.json"

    def has_run(self, rid: str) -> bool:
        return self._run_path(rid).exists()

    def write_run(self, record: RunRecord, reasoning: str) -> Path:
        if self.has_run(record.run_id):
            raise AuditError(f"{record.run_id} already exists: audit records are immutable")
        # Python-mode dump, then a strict JSON encoder: pydantic's own JSON mode would turn
        # NaN into null and silently hide a data problem. This fails before any file is written.
        content = _dump(record.model_dump())
        reasoning_path = self.root / "runs" / record.reasoning_file
        _write_once(reasoning_path, reasoning)
        _write_once(self._run_path(record.run_id), content)  # last: marks the run complete
        return self._run_path(record.run_id)

    def write_reconciliation(self, name: str, payload: dict[str, Any]) -> Path:
        path = self.root / "reconciliation" / f"{name}.json"
        _write_once(path, _dump({"schema_version": SCHEMA_VERSION, "name": name, **payload}))
        return path

    def read_run(self, rid: str) -> dict[str, Any]:
        record: dict[str, Any] = json.loads(self._run_path(rid).read_text(encoding="utf-8"))
        return record

    def has_observation(self, name: str) -> bool:
        return (self.root / "observations" / f"{name}.json").exists()

    def write_observation(self, name: str, payload: dict[str, Any]) -> Path:
        path = self.root / "observations" / f"{name}.json"
        _write_once(path, _dump({"schema_version": SCHEMA_VERSION, **payload}))
        return path

    def read_observations(self) -> list[dict[str, Any]]:
        files = sorted((self.root / "observations").glob("*.json"))
        return [json.loads(f.read_text(encoding="utf-8")) for f in files]

    def has_valuation(self, name: str) -> bool:
        return (self.root / "valuations" / f"{name}.json").exists()

    def write_valuation(self, name: str, payload: dict[str, Any]) -> Path:
        path = self.root / "valuations" / f"{name}.json"
        _write_once(path, _dump({"schema_version": SCHEMA_VERSION, **payload}))
        return path

    def read_valuations(self) -> list[dict[str, Any]]:
        files = sorted((self.root / "valuations").glob("*.json"))
        return [json.loads(f.read_text(encoding="utf-8")) for f in files]

    def has_reconciliation(self, name: str) -> bool:
        return (self.root / "reconciliation" / f"{name}.json").exists()

    def read_reconciliation(self, name: str) -> dict[str, Any] | None:
        path = self.root / "reconciliation" / f"{name}.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    def read_fills(self) -> list[dict[str, Any]]:
        files = sorted((self.root / "reconciliation").glob("*_fills.json"))
        return [json.loads(f.read_text(encoding="utf-8")) for f in files]

    def read_runs(self) -> list[dict[str, Any]]:
        files = sorted((self.root / "runs").glob("*.json"))
        return [json.loads(f.read_text(encoding="utf-8")) for f in files]

    def build_tables(self) -> list[Path]:
        runs = self.read_runs()
        out = self.root / "tables"
        out.mkdir(parents=True, exist_ok=True)
        tables = {
            "decisions.csv": _decisions_rows(runs),
            "weights.csv": _weights_rows(runs),
            "forecasts.csv": _forecast_rows(runs),
            "orders.csv": _order_rows(runs),
            "fills.csv": _fill_rows(self.read_fills()),
            "forecast_scores.csv": _score_rows(score_forecasts(runs, self.read_observations())),
            "shadow_portfolios.csv": _shadow_rows(shadow_returns(runs, self.read_observations())),
        }
        paths = []
        for name, (header, rows) in tables.items():
            path = out / name
            with path.open("w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(header)
                writer.writerows(rows)
            paths.append(path)
        return paths


Rows = tuple[list[str], list[list[Any]]]


def _decisions_rows(runs: list[dict[str, Any]]) -> Rows:
    header = [
        "run_id", "mode", "started_at", "git_sha", "prompt_version", "model", "outcome",
        "attempts", "corrections", "rejection_reason", "equity", "drawdown_from_peak",
        "final_cash", "prompt_tokens", "completion_tokens", "execution_status", "n_orders",
    ]  # fmt: skip
    rows = []
    for r in runs:
        attempts = r["decision"]["attempts"]
        last = attempts[-1]
        rows.append(
            [
                r["run_id"], r["mode"], r["started_at"], r["git_sha"],
                r["decision"]["prompt_version"], last["model"], r["decision"]["outcome"],
                len(attempts), len(last["guardrails"]["corrections"]),
                last["guardrails"]["rejection_reason"] or "",
                r["account"]["equity"], r["account"]["drawdown_from_peak"],
                r["portfolios"]["final_cash"],
                sum(a["prompt_tokens"] for a in attempts),
                sum(a["completion_tokens"] for a in attempts),
                r["execution_status"], len(r["orders"]),
            ]
        )  # fmt: skip
    return header, rows


def _weights_rows(runs: list[dict[str, Any]]) -> Rows:
    header = ["run_id", "symbol", "current", "baseline", "proposed", "final", "confidence",
              "rationale"]  # fmt: skip
    rows = []
    for r in runs:
        p = r["portfolios"]
        for symbol, final in p["final"].items():
            view = (p["proposed"] or {}).get(symbol)
            rows.append(
                [
                    r["run_id"], symbol, p["current"][symbol], p["baseline"][symbol],
                    view["weight"] if view else "", final,
                    view["confidence"] if view else "", view["rationale"] if view else "",
                ]
            )  # fmt: skip
    return header, rows


def _forecast_rows(runs: list[dict[str, Any]]) -> Rows:
    header = ["run_id", "last_session", "symbol", "horizon", "q10", "median", "q90", "width_80"]
    rows = []
    for r in runs:
        for symbol, horizons in r["forecasts"].items():
            for h, q in horizons.items():
                rows.append(
                    [r["run_id"], r["data"]["last_session"], symbol, int(h),
                     q["q10"], q["median"], q["q90"], q["width_80"]]
                )  # fmt: skip
    return header, rows


def _order_rows(runs: list[dict[str, Any]]) -> Rows:
    header = ["run_id", "symbol", "side", "quantity", "limit_price", "reference_price",
              "client_order_id", "status"]  # fmt: skip
    rows = []
    for r in runs:
        for o in r["orders"]:
            rows.append(
                [r["run_id"], o["symbol"], o["side"], o["quantity"],
                 _blank(o["limit_price"]), _blank(o.get("reference_price")),
                 o["client_order_id"], o["status"]]
            )  # fmt: skip
    return header, rows


def _blank(value: Any) -> Any:
    return "" if value is None else value


def _fill_rows(fills: list[dict[str, Any]]) -> Rows:
    header = ["run_id", "symbol", "side", "quantity", "status", "filled_qty", "avg_fill_price",
              "reference_price", "slippage_bps", "submitted_at", "filled_at"]  # fmt: skip
    rows = []
    for f in fills:
        for o in f["orders"]:
            rows.append(
                [f["run_id"], o["symbol"], o["side"], o["quantity"], o["status"],
                 o["filled_qty"], _blank(o["avg_fill_price"]), _blank(o["reference_price"]),
                 _blank(o["slippage_bps"]), _blank(o["submitted_at"]), _blank(o["filled_at"])]
            )  # fmt: skip
    return header, rows


def _score_rows(scores: list[dict[str, Any]]) -> Rows:
    header = ["run_id", "last_session", "symbol", "horizon", "realised_session", "q10", "median",
              "q90", "width_80", "realised", "inside_80", "direction_hit",
              "abs_error_median"]  # fmt: skip
    rows = [[_blank(row[column]) for column in header] for row in scores]
    return header, rows


def _shadow_rows(rows: list[dict[str, Any]]) -> Rows:
    header = ["session", "run_id", *SERIES]
    return header, [[row[column] for column in header] for row in rows]
