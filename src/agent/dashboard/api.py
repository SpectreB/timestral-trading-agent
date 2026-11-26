"""The dashboard API (D-022): read-only, typed, the only way the page gets its data.

Served live by uvicorn, and exported route by route into the static site, so the page still
works when the server is asleep. Simulations are still requested through a GitHub issue (D-017):
the requester's GitHub login stays the identity, and the API never holds a key.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import tarfile
import tempfile
import threading
import time
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from agent.dashboard.models import (
    Agent,
    AgentSummary,
    Evidence,
    Health,
    Overview,
    Simulations,
)
from agent.dashboard.records import AUDIT_BRANCH, DEFAULT_REPO

REPO_ROOT = Path(__file__).resolve().parents[3]
ORIGINS = ("https://spectreb.github.io", "http://localhost:8765", "http://127.0.0.1:8765")
REFRESH_S = 300


def create_app(load: Callable[[], dict[str, Any]], source: str) -> FastAPI:
    app = FastAPI(title="Timestral trading agent", version="1.0", docs_url="/api/docs",
                  openapi_url="/api/openapi.json", redoc_url=None)  # fmt: skip
    origins = os.environ.get("ALLOWED_ORIGINS", ",".join(ORIGINS)).split(",")
    app.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["GET"])

    def route(path: str, model: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Each route also answers at `<path>.json`, the name of its file in the static site."""

        opts: dict[str, Any] = {"response_model": model, "response_model_exclude_unset": True}

        def register(handler: Callable[..., Any]) -> Callable[..., Any]:
            app.get(path + ".json", include_in_schema=False, **opts)(handler)
            app.get(path, **opts)(handler)
            return handler

        return register

    @route("/api/health", Health)
    def health() -> dict[str, Any]:
        return {"status": "ok", "generated_at": load()["generated_at"], "source": source}

    @route("/api/overview", Overview)
    def overview() -> dict[str, Any]:
        data = load()
        return {k: data[k] for k in Overview.model_fields}

    @route("/api/agents", list[AgentSummary])
    def agents() -> list[dict[str, Any]]:
        return [{k: a[k] for k in AgentSummary.model_fields if k in a} for a in load()["agents"]]

    @route("/api/agents/{key}", Agent)
    def agent(key: str) -> dict[str, Any]:
        for a in load()["agents"]:
            if a["key"] == key:
                return a  # type: ignore[no-any-return]
        raise HTTPException(404, f"no agent {key!r}")

    @route("/api/evidence", Evidence)
    def evidence() -> dict[str, Any]:
        data = load()
        return {"behaviour": data["behaviour"], "covariates": data["covariates"]}

    @route("/api/simulations", Simulations)
    def simulations() -> dict[str, Any]:
        return load()["simulations"]  # type: ignore[no-any-return]

    return app


def export(data: dict[str, Any], out_dir: Path) -> Path:
    """Writes every API response as a file of the static site, through the API itself."""
    from fastapi.testclient import TestClient

    client = TestClient(create_app(lambda: data, "snapshot"))
    paths = ["/api/health", "/api/overview", "/api/agents", "/api/evidence", "/api/simulations"]
    paths += [f"/api/agents/{a['key']}" for a in data["agents"]]
    for path in [*paths, "/api/openapi"]:
        response = client.get(path + ".json")
        response.raise_for_status()
        target = out_dir / f"{path.lstrip('/')}.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(response.json(), indent=1, allow_nan=False), encoding="utf-8")
    return out_dir / "api"


class Cached:
    """The site data, rebuilt at most every `ttl` seconds; the last good copy is kept on error."""

    def __init__(self, build: Callable[[], dict[str, Any]], ttl: float = REFRESH_S) -> None:
        self._build, self._ttl = build, ttl
        self._data: dict[str, Any] | None = None
        self._at = 0.0
        self._lock = threading.Lock()

    def __call__(self) -> dict[str, Any]:
        with self._lock:
            if self._data is None or time.monotonic() - self._at > self._ttl:
                try:
                    self._data = self._build()
                except Exception:
                    if self._data is None:
                        raise
                self._at = time.monotonic()
            return self._data


def folder_loader(audit_dir: Path, docs: Path = REPO_ROOT / "docs") -> Callable[[], dict[str, Any]]:
    from agent.dashboard.web import collect

    def load() -> dict[str, Any]:
        return collect(
            audit_dir, datetime.now(UTC), DEFAULT_REPO, docs / "replay",
            docs / "replay_tools", docs / "evals", docs / "covariates",
        )  # fmt: skip

    return load


def github_loader(repo: str = DEFAULT_REPO) -> Callable[[], dict[str, Any]]:  # pragma: no cover
    """The audit-log branch, downloaded as an archive: public, so no token on the server."""
    slug = repo.removeprefix("https://github.com/")
    url = f"https://codeload.github.com/{slug}/tar.gz/refs/heads/{AUDIT_BRANCH}"

    def load() -> dict[str, Any]:
        with urllib.request.urlopen(url, timeout=60) as response:
            archive = response.read()
        with tempfile.TemporaryDirectory() as tmp:
            with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
                tar.extractall(tmp, filter="data")
            (root,) = Path(tmp).iterdir()
            return folder_loader(root)()

    return load


def _app_from_env() -> FastAPI:  # pragma: no cover - configuration of the hosted server
    folder = os.environ.get("AUDIT_DIR")
    loader = folder_loader(Path(folder)) if folder else github_loader()
    return create_app(Cached(loader), "folder" if folder else "github")


def __getattr__(name: str) -> Any:  # pragma: no cover - `uvicorn agent.dashboard.api:app`
    if name == "app":
        return _app_from_env()
    raise AttributeError(name)


def main() -> None:  # pragma: no cover - local preview: the page and the API on one server
    import uvicorn
    from fastapi.staticfiles import StaticFiles

    parser = argparse.ArgumentParser(description="Serve the dashboard and its API locally.")
    parser.add_argument("--audit-dir", default="audit", type=Path)
    parser.add_argument("--port", default=8000, type=int)
    args = parser.parse_args()
    app = create_app(Cached(folder_loader(args.audit_dir), ttl=5), "folder")
    tables = Path(tempfile.mkdtemp()) / "data"
    shutil.copytree(args.audit_dir / "tables", tables, dirs_exist_ok=True)
    app.mount("/data", StaticFiles(directory=tables))
    app.mount("/", StaticFiles(directory=REPO_ROOT / "web", html=True))
    uvicorn.run(app, port=args.port)


if __name__ == "__main__":  # pragma: no cover
    main()
