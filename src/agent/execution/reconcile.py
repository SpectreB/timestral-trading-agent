from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from pydantic import Field

from agent.config import Frozen
from agent.execution.audit import FileAuditStore

TERMINAL = frozenset({"filled", "canceled", "expired", "rejected", "done_for_day"})


class OrderState(Frozen):
    status: str
    filled_qty: float = Field(ge=0)
    filled_avg_price: float | None = Field(default=None, gt=0)
    submitted_at: str | None = None
    filled_at: str | None = None


class OrderLookup(Protocol):
    def order_by_client_id(self, client_order_id: str) -> OrderState | None: ...


@dataclass
class ReconcileResult:
    written: list[str] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)


def slippage_bps(side: str, reference: float, fill: float) -> float:
    sign = 1.0 if side == "buy" else -1.0
    return sign * (fill - reference) / reference * 1e4


def _order_row(sub: dict[str, Any], ref: float | None, state: OrderState | None) -> dict[str, Any]:
    row: dict[str, Any] = {
        "symbol": sub["symbol"],
        "side": sub["side"],
        "quantity": sub["quantity"],
        "limit_price": sub["limit_price"],
        "client_order_id": sub["client_order_id"],
        "reference_price": ref,
        "submission_status": sub["status"],
        "status": "not_sent",
        "filled_qty": 0.0,
        "avg_fill_price": None,
        "slippage_bps": None,
        "submitted_at": None,
        "filled_at": None,
    }
    if state is not None:
        row.update(
            status=state.status,
            filled_qty=state.filled_qty,
            avg_fill_price=state.filled_avg_price,
            submitted_at=state.submitted_at,
            filled_at=state.filled_at,
        )
        if state.filled_qty > 0 and state.filled_avg_price and ref:
            row["slippage_bps"] = slippage_bps(sub["side"], ref, state.filled_avg_price)
    return row


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    sent = [r for r in rows if r["status"] != "not_sent"]
    notional = {r["client_order_id"]: r["filled_qty"] * (r["avg_fill_price"] or 0) for r in sent}
    total = sum(notional.values())
    weighted = [
        r["slippage_bps"] * notional[r["client_order_id"]]
        for r in sent
        if r["slippage_bps"] is not None
    ]
    return {
        "n_orders": len(rows),
        "n_not_sent": len(rows) - len(sent),
        "n_filled": sum(r["status"] == "filled" for r in sent),
        "n_partial": sum(r["filled_qty"] > 0 and r["status"] != "filled" for r in sent),
        "n_unfilled": sum(r["filled_qty"] == 0 for r in sent),
        "n_not_found": sum(r["status"] == "not_found" for r in sent),
        "filled_notional": total,
        "slippage_bps_weighted": sum(weighted) / total if total > 0 and weighted else None,
    }


def _reconcile_one(
    run: dict[str, Any], submissions: dict[str, Any], lookup: OrderLookup, now: datetime
) -> dict[str, Any] | None:
    reference = {o["client_order_id"]: o.get("reference_price") for o in run["orders"]}
    rows: list[dict[str, Any]] = []
    for sub in submissions["submissions"]:
        coid = sub["client_order_id"]
        ref = reference.get(coid)
        if sub["status"].startswith(("error", "skipped")):
            rows.append(_order_row(sub, ref, None))  # never reached the broker
            continue
        state = lookup.order_by_client_id(coid)
        if state is None:
            rows.append(_order_row(sub, ref, OrderState(status="not_found", filled_qty=0.0)))
        elif state.status not in TERMINAL:
            return None
        else:
            rows.append(_order_row(sub, ref, state))
    return {
        "run_id": run["run_id"],
        "reconciled_at": now.isoformat(timespec="seconds"),
        "orders": rows,
        "summary": _summary(rows),
    }


def reconcile_runs(store: FileAuditStore, lookup: OrderLookup, now: datetime) -> ReconcileResult:
    result = ReconcileResult()
    for run in store.read_runs():
        rid = run["run_id"]
        submissions = store.read_reconciliation(rid)
        if submissions is None or store.has_reconciliation(f"{rid}_fills"):
            continue
        try:
            payload = _reconcile_one(run, submissions, lookup, now)
        except Exception as error:  # one bad lookup must not block the other runs
            result.errors[rid] = f"{type(error).__name__}: {str(error)[:150]}"
            continue
        if payload is None:
            result.pending.append(rid)
            continue
        store.write_reconciliation(f"{rid}_fills", payload)
        result.written.append(rid)
    return result


def main() -> None:  # pragma: no cover - CLI wiring, runs in the trade workflow
    import os
    import sys
    from datetime import UTC

    from dotenv import load_dotenv

    from agent.execution.execute import AlpacaBroker
    from agent.jobs.run import AGENTS, alpaca_keys

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit-dir", default="audit", type=Path)
    parser.add_argument("--agent", choices=AGENTS, default="pipeline")
    args = parser.parse_args()

    load_dotenv()
    broker = AlpacaBroker(*alpaca_keys(args.agent, os.environ))
    store = FileAuditStore(args.audit_dir)
    result = reconcile_runs(store, broker, datetime.now(UTC))
    print(f"reconciled: {result.written} | pending: {result.pending} | errors: {result.errors}")
    sys.exit(1 if result.errors else 0)


if __name__ == "__main__":  # pragma: no cover
    main()
