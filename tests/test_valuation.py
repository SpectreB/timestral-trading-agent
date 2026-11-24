from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from agent.audit import FileAuditStore
from agent.execute import AccountSnapshot, Position
from agent.valuation import record, sample

NOW = datetime(2026, 11, 24, 16, 5, 30, tzinfo=UTC)


class Reading:
    def account(self) -> AccountSnapshot:
        return AccountSnapshot(
            equity=100_250.5, is_paper=True, status="ACTIVE", trading_blocked=False
        )

    def positions(self) -> dict[str, Position]:
        return {"SPY": Position(10, 6_000.25), "AAPL": Position(5, 1_000.0)}


def test_a_sample_gives_equity_invested_cash_and_each_position() -> None:
    s = sample(Reading(), NOW)
    assert s["at"] == "2026-11-24T16:05:30+00:00"
    assert s["equity"] == 100_250.5
    assert s["invested"] == 7_000.25 and s["cash"] == 93_250.25
    assert s["positions"] == {"AAPL": 1_000.0, "SPY": 6_000.25}


def test_samples_are_stored_once_per_minute_and_read_back_in_order(tmp_path: Path) -> None:
    store = FileAuditStore(tmp_path)
    assert store.read_valuations() == []
    first = record(store, Reading(), NOW)
    assert first is not None and first.name == "20261124T1605Z.json"
    assert record(store, Reading(), NOW.replace(second=59)) is None  # same minute: nothing
    later = record(store, Reading(), NOW.replace(hour=17))
    assert later is not None
    stored = store.read_valuations()
    assert [v["at"][11:16] for v in stored] == ["16:05", "17:05"]
    assert stored[0]["schema_version"] and stored[0]["equity"] == 100_250.5
