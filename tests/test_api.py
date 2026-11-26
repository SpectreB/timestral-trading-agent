"""The dashboard API (src/agent/dashboard/api.py): routes, contract, snapshot, caching."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from agent.dashboard.api import Cached, create_app, export, folder_loader
from tests.site_fixtures import STAMP, _tool_cycle
from tests.test_run import cycle


@pytest.fixture
def data(tmp_path: Path) -> dict[str, Any]:
    from agent.dashboard.web import collect

    audit = tmp_path / "audit"
    cycle(audit)
    _tool_cycle(audit)
    return collect(audit, STAMP)


def test_every_route_answers_with_its_model_and_at_its_file_name(data: dict[str, Any]) -> None:
    client = TestClient(create_app(lambda: data, "test"))
    assert client.get("/api/health").json() == {
        "status": "ok", "generated_at": data["generated_at"], "source": "test"
    }  # fmt: skip
    summaries = client.get("/api/agents").json()
    assert [a["key"] for a in summaries] == ["tools", "pipeline"]
    assert "holdings" not in summaries[0]  # the list is light; the details are per agent
    for path in ("overview", "agents", "agents/tools", "evidence", "simulations"):
        assert client.get(f"/api/{path}").json() == client.get(f"/api/{path}.json").json()
    assert client.get("/api/agents/tools").json() == data["agents"][0]
    assert client.get("/api/agents/oracle").status_code == 404
    assert set(client.get("/api/openapi.json").json()["paths"]) >= {"/api/agents/{key}"}


def test_only_the_page_may_call_the_api_from_a_browser(data: dict[str, Any]) -> None:
    client = TestClient(create_app(lambda: data, "test"))
    ok = client.get("/api/overview", headers={"Origin": "https://spectreb.github.io"})
    assert ok.headers["access-control-allow-origin"] == "https://spectreb.github.io"
    other = client.get("/api/overview", headers={"Origin": "https://example.com"})
    assert "access-control-allow-origin" not in other.headers
    assert client.post("/api/overview").status_code == 405  # read-only


def test_the_snapshot_is_the_api_output_file_by_file(data: dict[str, Any], tmp_path: Path) -> None:
    api = export(data, tmp_path / "site")
    client = TestClient(create_app(lambda: data, "snapshot"))
    for path in ("health", "overview", "agents", "agents/pipeline", "evidence", "simulations"):
        assert json.loads((api / f"{path}.json").read_text()) == client.get(f"/api/{path}").json()


def test_the_cache_rebuilds_after_its_ttl_and_keeps_the_last_good_copy() -> None:
    calls: list[int] = []

    def build() -> dict[str, Any]:
        calls.append(1)
        if len(calls) == 3:
            raise OSError("GitHub is down")
        return {"n": len(calls)}

    cached = Cached(build, ttl=0.0)
    assert cached() == {"n": 1} and cached() == {"n": 2}
    assert cached() == {"n": 2}  # the failed rebuild serves the last good copy
    fresh = Cached(build, ttl=60.0)
    assert fresh() == {"n": 4} and fresh() == {"n": 4}  # within its ttl: no rebuild

    def broken() -> dict[str, Any]:
        raise OSError("no data yet")

    with pytest.raises(OSError):
        Cached(broken)()


def test_the_folder_loader_reads_the_audit_log_and_the_docs(tmp_path: Path) -> None:
    cycle(tmp_path / "audit")
    data = folder_loader(tmp_path / "audit", tmp_path / "no-docs")()
    assert [a["key"] for a in data["agents"]] == ["pipeline"]
