"""The account's value, sampled between runs.

A run records the account once, when it starts. Between two runs the positions move with the
market and the page would still show the value of the last run. This module takes one sample of
the real account (equity and the market value of each position) and writes it to the audit log
(`valuations/`). It only reads from the broker: it can never send an order.

Samples are facts, one file per minute and per agent; a repeated call within the same minute
writes nothing.
"""

from __future__ import annotations

import argparse
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from agent.execution.audit import FileAuditStore
from agent.execution.execute import AccountSnapshot, Position


class Reader(Protocol):
    def account(self) -> AccountSnapshot: ...
    def positions(self) -> dict[str, Position]: ...


def sample(broker: Reader, now: datetime) -> dict[str, Any]:
    """The account at `now`: equity, invested value, cash and the value of each position."""
    account = broker.account()
    held = {s: round(p.market_value, 2) for s, p in sorted(broker.positions().items())}
    invested = round(sum(held.values()), 2)
    return {
        "at": now.astimezone(UTC).isoformat(timespec="seconds"),
        "equity": round(account.equity, 2),
        "invested": invested,
        "cash": round(account.equity - invested, 2),
        "positions": held,
    }


def record(store: FileAuditStore, broker: Reader, now: datetime) -> Path | None:
    """Take and store one sample; None when this minute already has one."""
    name = now.astimezone(UTC).strftime("%Y%m%dT%H%MZ")
    if store.has_valuation(name):
        return None
    return store.write_valuation(name, sample(broker, now))


def main() -> None:  # pragma: no cover - CLI wiring, exercised by the workflow
    from dotenv import load_dotenv

    from agent.execution.execute import AlpacaBroker
    from agent.jobs.run import AGENTS, alpaca_keys

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent", choices=AGENTS, default="pipeline")
    parser.add_argument("--audit-dir", default="audit", type=Path)
    args = parser.parse_args()
    load_dotenv()
    key, secret = alpaca_keys(args.agent, os.environ)
    path = record(FileAuditStore(args.audit_dir), AlpacaBroker(key, secret), datetime.now(UTC))
    print(path or "already sampled this minute")


if __name__ == "__main__":  # pragma: no cover
    main()
