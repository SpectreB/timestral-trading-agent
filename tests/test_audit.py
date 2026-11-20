"""Audit log (D-011): complete records, immutability, atomic writes, derived tables."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

import pytest

from agent.audit import (
    AuditError,
    FileAuditStore,
    OrderRecord,
    RunRecord,
    build_record,
    code_version,
    run_id,
)
from agent.decide import decide
from tests.helpers import CFG, as_json, proposal
from tests.test_decide import BROKEN, NOW, FakeLLM, make_input

ORDER = OrderRecord(
    symbol="TLT", side="buy", quantity=10.5, limit_price=80.6, client_order_id="x", status="dry_run"
)


def make_record(
    answers: list[str] | None = None, mode: str = "morning", orders: list[OrderRecord] | None = None
) -> tuple[RunRecord, str]:
    inp = make_input()
    decision = decide(inp, FakeLLM(answers or [as_json(proposal(TLT=0.10, NVDA=0.02))]), CFG)
    return build_record(
        mode=mode,
        now=NOW,
        cfg=CFG,
        inp=inp,
        decision=decision,
        orders=[ORDER] if orders is None else orders,
        execution_status="dry_run",
    )


def test_run_id_uses_the_new_york_date_and_mode() -> None:
    assert run_id(NOW, "morning") == "2026-11-23_morning"


def test_code_version_reads_the_ci_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_SHA", raising=False)
    assert code_version() == "local"
    monkeypatch.setenv("GITHUB_SHA", "abc123")
    assert code_version() == "abc123"


def test_record_captures_the_whole_decision() -> None:
    record, reasoning = make_record()
    assert record.run_id == "2026-11-23_morning"
    assert record.schema_version == 1
    assert record.data["last_session"] == "2026-11-20"
    assert set(record.forecasts) == set(CFG.symbols)
    assert set(record.forecasts["SPY"]) == {"1", "2", "3", "4", "5"}
    p = record.portfolios
    assert set(p["current"]) == set(p["baseline"]) == set(p["final"]) == set(CFG.symbols)
    assert p["proposed"]["TLT"]["weight"] == pytest.approx(0.10)
    assert record.decision["prompt_version"] == "decide_v1"
    assert record.decision["outcome"] == "accepted"
    assert record.messages[0]["role"] == "system"
    assert record.config["llm"]["model"] == "mistral-large-4-0"
    assert record.reasoning_file == "2026-11-23_morning_reasoning.txt"
    assert "attempt 1" in reasoning and "thinking..." in reasoning
    json.dumps(record.model_dump(mode="json"), allow_nan=False)


def test_all_attempts_are_recorded_even_when_the_run_falls_back() -> None:
    record, reasoning = make_record(answers=[BROKEN, BROKEN])
    assert record.decision["outcome"] == "fallback_hold"
    assert len(record.decision["attempts"]) == 2
    assert record.portfolios["proposed"] is None  # the last answer could not be parsed
    assert record.decision["attempts"][-1]["guardrails"]["rejection_reason"] == "invalid_json"
    assert "attempt 2" in reasoning


def test_write_creates_json_and_reasoning(tmp_path: Path) -> None:
    store = FileAuditStore(tmp_path)
    record, reasoning = make_record()
    assert not store.has_run(record.run_id)
    path = store.write_run(record, reasoning)
    assert path == tmp_path / "runs" / "2026-11-23_morning.json"
    assert store.has_run(record.run_id)
    assert json.loads(path.read_text())["run_id"] == record.run_id
    assert (tmp_path / "runs" / record.reasoning_file).read_text() == reasoning
    assert not list((tmp_path / "runs").glob(".tmp-*"))  # no temp file left behind


def test_run_records_are_immutable(tmp_path: Path) -> None:
    store = FileAuditStore(tmp_path)
    record, reasoning = make_record()
    path = store.write_run(record, reasoning)
    original = path.read_text()
    other, other_reasoning = make_record(answers=[as_json(proposal())])
    with pytest.raises(AuditError, match="immutable"):
        store.write_run(other, other_reasoning)
    assert path.read_text() == original  # unchanged
    assert not list((tmp_path / "runs").glob(".tmp-*"))


def test_existing_files_are_never_overwritten_even_without_a_record(tmp_path: Path) -> None:
    # A leftover reasoning file (e.g. from a crash before the JSON) must not be overwritten.
    store = FileAuditStore(tmp_path)
    record, reasoning = make_record()
    leftover = tmp_path / "runs" / record.reasoning_file
    leftover.parent.mkdir(parents=True)
    leftover.write_text("earlier content")
    with pytest.raises(AuditError, match="immutable"):
        store.write_run(record, reasoning)
    assert leftover.read_text() == "earlier content"
    assert not store.has_run(record.run_id)  # no JSON: the run is not marked complete


def test_non_finite_numbers_are_refused_before_anything_is_written(tmp_path: Path) -> None:
    store = FileAuditStore(tmp_path)
    record, reasoning = make_record()
    record.account["equity"] = float("nan")
    with pytest.raises(ValueError, match="Out of range"):
        store.write_run(record, reasoning)
    assert not (tmp_path / "runs").exists()


def test_reconciliation_files_are_immutable_too(tmp_path: Path) -> None:
    store = FileAuditStore(tmp_path)
    path = store.write_reconciliation("2026-11-23", {"fills": [{"symbol": "TLT", "price": 80.7}]})
    content = json.loads(path.read_text())
    assert content["name"] == "2026-11-23"
    assert content["fills"][0]["price"] == 80.7
    with pytest.raises(AuditError, match="immutable"):
        store.write_reconciliation("2026-11-23", {"fills": []})


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def test_tables_are_derived_from_the_run_records(tmp_path: Path) -> None:
    store = FileAuditStore(tmp_path)
    store.write_run(*make_record())
    store.write_run(*make_record(mode="afternoon", answers=[BROKEN, BROKEN], orders=[]))
    paths = store.build_tables()
    assert {p.name for p in paths} == {
        "decisions.csv",
        "weights.csv",
        "forecasts.csv",
        "orders.csv",
        "fills.csv",
    }

    decisions = {r["run_id"]: r for r in read_csv(tmp_path / "tables" / "decisions.csv")}
    assert set(decisions) == {"2026-11-23_morning", "2026-11-23_afternoon"}
    morning, afternoon = decisions["2026-11-23_morning"], decisions["2026-11-23_afternoon"]
    assert morning["outcome"] == "accepted" and morning["attempts"] == "1"
    assert morning["prompt_tokens"] == "100" and morning["n_orders"] == "1"
    assert afternoon["outcome"] == "fallback_hold" and afternoon["attempts"] == "2"
    assert afternoon["rejection_reason"] == "invalid_json"
    assert afternoon["prompt_tokens"] == "200"  # both attempts counted

    weights = read_csv(tmp_path / "tables" / "weights.csv")
    assert len(weights) == 2 * 15
    tlt = next(w for w in weights if w["run_id"] == "2026-11-23_morning" and w["symbol"] == "TLT")
    assert float(tlt["baseline"]) == pytest.approx(0.06)
    assert float(tlt["proposed"]) == pytest.approx(0.10)
    assert float(tlt["final"]) == pytest.approx(0.10)
    unparsed = next(w for w in weights if w["run_id"] == "2026-11-23_afternoon")
    assert unparsed["proposed"] == "" and unparsed["confidence"] == ""

    forecasts = read_csv(tmp_path / "tables" / "forecasts.csv")
    assert len(forecasts) == 2 * 15 * 5
    assert {f["horizon"] for f in forecasts} == {"1", "2", "3", "4", "5"}

    orders = read_csv(tmp_path / "tables" / "orders.csv")
    assert len(orders) == 1 and orders[0]["symbol"] == "TLT"
    assert orders[0]["limit_price"] == "80.6"


def test_tables_are_deterministic_and_regenerable(tmp_path: Path) -> None:
    store = FileAuditStore(tmp_path)
    store.write_run(*make_record())
    store.build_tables()
    first = {p.name: p.read_bytes() for p in (tmp_path / "tables").iterdir()}
    store.build_tables()
    assert {p.name: p.read_bytes() for p in (tmp_path / "tables").iterdir()} == first


def test_order_without_limit_price_is_exported_as_empty(tmp_path: Path) -> None:
    store = FileAuditStore(tmp_path)
    market = ORDER.model_copy(update={"limit_price": None})
    store.write_run(*make_record(orders=[market]))
    store.build_tables()
    assert read_csv(tmp_path / "tables" / "orders.csv")[0]["limit_price"] == ""


def test_no_runs_gives_empty_tables(tmp_path: Path) -> None:
    store = FileAuditStore(tmp_path)
    (tmp_path / "runs").mkdir()
    store.build_tables()
    rows: list[Any] = read_csv(tmp_path / "tables" / "decisions.csv")
    assert rows == []
