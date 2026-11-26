"""Response models of the dashboard API (D-022): the contract between the back end and the page."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


class Out(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Links(Out):
    audit_log: str
    decisions: str
    readme: str
    tables: list[str]


class Rule(Out):
    title: str
    text: str


class Level(Out):
    threshold: float
    label: str


class MeasureRow(Out):
    group: str
    key: str
    label: str
    help: str


class InputItem(Out):
    text: str
    help: str


class InputRow(Out):
    label: str
    items: list[InputItem]


class NextItem(Out):
    kind: str
    id: str
    title: str
    text: str


class Overview(Out):
    generated_at: str
    repo: str
    links: Links
    measure_rows: list[MeasureRow]
    min_sessions_for_ci: int
    rules: list[Rule]
    drawdown_levels: list[Level]
    inputs: list[InputRow]
    next: list[NextItem]


class Value(Out):
    equity: float
    at: str
    source: Literal["run", "sample"] | None = None


class Range80(Out):
    horizon: int
    low: float
    high: float


class Alert(Out):
    level: Literal["crit", "warn"]
    text: str


class Point(Out):
    at: str
    equity: float
    label: str


class Holding(Out):
    symbol: str
    name: str
    type: Literal["etf", "stock"]
    current: float
    rule: float | None
    proposed: float | None
    final: float
    value: float
    confidence: float | None
    rationale: str
    risk_share: float | None = None


class Order(Out):
    run_id: str
    at: str
    symbol: str
    side: Literal["buy", "sell"]
    quantity: float
    status: str
    price: float | None = None
    value: float | None = None
    slippage_bps: float | None = None
    limit_price: float | None
    reference_price: float | None
    submitted_at: str | None = None


class RunRow(Out):
    run_id: str
    at: str
    outcome: str
    execution: str
    equity: float
    orders: int
    corrections: int
    record: str


class AgentSummary(Out):
    key: Literal["pipeline", "tools"]
    title: str
    tagline: str
    summary: str
    traits: list[tuple[str, str]]
    records: str
    started: bool
    measures: dict[str, str]
    start: Value | None = None
    now: Value | None = None
    range80: Range80 | None = None
    n_runs: int = 0
    next_run: str | None = None
    alerts: list[Alert] = []


class Agent(AgentSummary):
    replay: dict[str, Any] | None
    series: list[Point] = []
    decision: dict[str, Any] | None = None
    holdings: list[Holding] = []
    forecast: dict[str, Any] | None = None
    orders: list[Order] = []
    runs: list[RunRow] = []
    calibration: list[dict[str, Any]] = []
    risk_calibration: list[dict[str, Any]] = []


class Evidence(Out):
    behaviour: dict[str, Any] | None
    covariates: dict[str, Any] | None


class Choice(Out):
    key: str
    label: str
    options: list[dict[str, str]]


class Simulations(Out):
    issue_url: str
    title: str
    body: str
    choices: list[Choice]
    recent: list[dict[str, Any]]


class Health(Out):
    status: Literal["ok"]
    generated_at: str
    source: str
