# Timestral Paper-Trading Agent

An autonomous agent that trades a small portfolio on an [Alpaca](https://alpaca.markets) **paper account** (fictitious money, real prices). [Timestral](https://mistral.ai/news/timestral/), Mistral's time-series foundation model, forecasts risk. A Mistral LLM turns those forecasts and market context into positions. Deterministic code, not the LLM, decides what is allowed to reach the broker.

> Paper trading only, for demonstration. Nothing here is financial advice.

The project is evaluated on **how the decisions are made and how we know they are sound**, not on profit. The design therefore revolves around three things: a measurable role for each model, guardrails that cannot be bypassed, and an audit trail that cannot be edited.

## How it works

```
Alpaca bars ─► Timestral ─► Baseline rule ─► Mistral LLM ─► Guardrails ─► Final check ─► Alpaca orders
 (completed     quantile     weights ∝        adjusts the     validate,     independent     marketable
  days only)    forecasts    1 / interval     baseline,       correct or    re-check of     limit orders
                1-5 days     width            explains        reject        core invariants
                                                  │
                         every step, input and output ─► immutable audit log ─► dashboard
```

| Stage | What it does | Code |
|---|---|---|
| **Data** | Adjusted daily closes for 15 assets (10 diversified ETFs, 5 large stocks). Today's incomplete bar is dropped, and any data problem stops the run. | [data.py](src/agent/data.py) |
| **Forecast** | One batched Timestral call (CPU, about 0.15 s for the 15 assets) gives quantiles of the cumulative return for the next 1 to 5 trading days. | [forecast.py](src/agent/forecast.py) |
| **Baseline** | Each weight is inversely proportional to the width of Timestral's 80% interval: the more uncertain, the smaller the position. | [baseline.py](src/agent/baseline.py) |
| **Decide** | Mistral Large 4 (pinned version, native reasoning) receives forecasts, baseline, portfolio and recent market data, and returns a strict JSON: its analysis first, then a weight, rationale and confidence per asset. | [decide.py](src/agent/decide.py), [prompt](src/agent/prompts/decide_v1.md) |
| **Guardrails** | Reject unusable answers, correct and log rule violations, apply a graduated drawdown circuit breaker. | [guardrails.py](src/agent/guardrails.py) |
| **Final check** | A separate, deliberately simple function re-checks the core invariants before any order. | [safety.py](src/agent/safety.py) |
| **Execute** | Marketable limit orders, sells before buys, idempotent client order ids, paper-account check, kill switch. | [execute.py](src/agent/execute.py) |
| **Audit** | One immutable record per run, then fills and slippage appended afterwards. | [audit.py](src/agent/audit.py), [reconcile.py](src/agent/reconcile.py) |

### Why Timestral is a risk engine

On daily data, Timestral's intervals are **well calibrated** (80% intervals contain about 80% of outcomes) but its **direction has no measurable edge** (hit rate within noise of 50%, see [docs/timestral_notes.md](docs/timestral_notes.md)). So the baseline uses what Timestral does reliably, the width of its intervals, and ignores its median. The LLM brings what a time-series model cannot: cross-asset reasoning, regime and event awareness. It is free to **reduce risk** without limit (down to cash) but may **add** at most 15 points above the baseline, and must justify every deviation. Keeping the baseline is a legitimate decision. Because the baseline, the LLM proposal and the executed portfolio are all logged, the value added by each component can be measured separately.

## How we know the decisions are sound

- **Guardrails are tested adversarially.** Invented tickers, text instead of numbers, `NaN`, short attempts, concentration, truncated JSON, and about 7,000 random proposals per run (property-based tests): every output must be rejected or pass the independent final check.
- **100% line and branch coverage** is enforced in CI on the 12 modules that sit on the decision and execution path.
- **Offline tests only.** Fake brokers, data sources, models and LLMs mean tests never touch the network or place an order.
- **Immutable, write-ahead audit log.** The record of what the agent saw and decided is written before any order is sent, and never modified afterwards. Re-running the same run id fails loudly.
- **Daily smoke test** on the real services, including a check that the pinned LLM version is the one answering.
- **Every design choice is recorded** with the options considered in [docs/decisions.md](docs/decisions.md), including what was deliberately deferred.

## Safety model

| Layer | Protection |
|---|---|
| Output validity | Strict JSON schema, assets limited to the universe, finite numbers only, weights must sum to 100% |
| Portfolio rules | Long-only, no leverage (max 90% invested), cash at least 10%, at most 25% per ETF, 10% per stock, 30% per sector group, at most +15 points above the baseline |
| Circuit breaker | Drawdown from peak: −3% alert, −5% reduce-only, −8% exposure capped at 60%, −12% baseline at half exposure and LLM paused |
| Orders | No-trade band, turnover cap per run, sells before buys, a failed sell cancels the buys |
| System | Paper-account check, kill switch, run-level idempotency, CI must be green on the deployed commit |

On rejection the agent keeps its current positions. It never liquidates because of a formatting error.

## Quick start

Requires Python 3.11.

```bash
python -m venv .venv
source .venv/bin/activate              # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
pip install --no-deps -e .
cp .env.example .env                   # then fill in your Mistral and Alpaca PAPER keys
```

Run one full cycle. **This is a dry run: nothing is sent to the broker.**

```bash
python -m agent.run --mode morning --audit-dir audit
```

It loads data, forecasts, calls the LLM, applies the guardrails, plans the orders and writes the audit record under `audit/`. The first call downloads the Timestral weights (about 110 MB) from Hugging Face.

Other entry points:

```bash
python -m agent.smoke                  # check every external service, no order
python -m agent.reconcile              # append fills and slippage for sent orders
python scripts/run_forecast.py         # forecasts and baseline only
ruff check . && ruff format --check . && mypy && pytest --cov=agent   # the CI checks
```

## Operation

Two GitHub Actions workflows run the agent on a schedule (see [D-002](docs/decisions.md) and [D-007](docs/decisions.md)):

| Workflow | When (ET) | What |
|---|---|---|
| **Trade** | 10:00 (full cycle) and 15:00 (reassessment), trading days | One cycle, then reconciliation, then the audit log is pushed to the [`audit-log`](../../tree/audit-log) branch |
| **Smoke test** | 08:30 | Checks Alpaca, Timestral and the pinned Mistral model without trading |
| **Dashboard** | after every Trade run | Rebuilds the page and deploys it to GitHub Pages |
| **CI** | every push and pull request | Lint, types, tests, 100% coverage gate, secret scan |

Setup: add the secrets `ALPACA_API_KEY`, `ALPACA_SECRET_KEY` and `MISTRAL_API_KEY`, and the repository variable `TRADING_ENABLED`. **`TRADING_ENABLED` is the kill switch**: orders are sent only when it is `true`; otherwise runs are recorded as dry runs. `main` is protected: changes go through pull requests with green CI, and the trading workflow only runs on a commit whose CI passed.

## Stack

| Layer | Choice | Why | Decision |
|---|---|---|---|
| Language | Python 3.11, pip and venv | One runtime for data, model and agent | [D-001, D-003](docs/decisions.md) |
| Market data and broker | Alpaca: daily bars (completed sessions only), two paper accounts | Free paper trading with real prices, one API for data and orders | [D-002](docs/decisions.md) |
| Forecast | Timestral 28M and 209M, run locally on CPU | Calibrated risk; no measurable direction, so used as a risk engine | [D-004, D-006](docs/decisions.md) |
| Decision | Mistral Large 4 (`mistral-large-4-0`, pinned), native reasoning, strict JSON | Reasoning trace and a schema-valid answer in one call | [D-008](docs/decisions.md) |
| Orchestration | Our own tool loop on the Mistral SDK, no agent framework | One bounded decision with three tools: every step plain, tested and logged | [D-021](docs/decisions.md) |
| Safety | Deterministic guardrails and an independent final check, in Python | The LLM proposes; code decides what reaches the broker | [D-009, D-010](docs/decisions.md) |
| Scheduling | GitHub Actions: trade twice a day, account sampled hourly | No server to run; every run leaves a log | [D-002, D-007](docs/decisions.md) |
| Records | Immutable JSON on the `audit-log` branch, CSV tables derived | Append-only, diffable, public | [D-011](docs/decisions.md) |
| Dashboard | React page (no build step) on GitHub Pages, data written by Python | Readable without installing anything; always agrees with the records | [D-012](docs/decisions.md) |
| Quality | ruff, mypy, pytest with hypothesis, 100% coverage on the core, gitleaks, protected `main` | Changes reach trading only through green CI | [D-009](docs/decisions.md) |

## Dashboard

A read-only dashboard is published at **https://spectreb.github.io/timestral-trading-agent/**, rebuilt from the audit log after every trade run and every hourly account sample. Three entry points: **Live** (account value, the last decision with the LLM's reasoning and its calls to Timestral, Timestral's raw forecast with its spread, the target portfolio, orders, every run), **How it works** (design, guardrails, the two agents side by side, behaviour tests, replay, risk calibration, the news covariate trial) and **Try it** (a simulation that never sends orders). Python writes the data (`python -m agent.web --audit-dir audit --out site`), a React page in [web/](web) lays it out; a plain page without JavaScript is published next to it ([D-012](docs/decisions.md)).

## Reading the audit log

The [`audit-log`](../../tree/audit-log) branch is the read-only record of the agent's activity:

```
runs/YYYY-MM-DD_<mode>.json            one record per run: inputs, forecasts, the four portfolios
                                       (current, baseline, LLM proposal, final), prompt, answers,
                                       guardrail results, planned orders
runs/YYYY-MM-DD_<mode>_reasoning.txt   the LLM's reasoning trace
reconciliation/*.json                  order submissions, then fills and slippage
tables/*.csv                           flat tables derived from the records:
                                       decisions, weights, forecasts, orders, fills
```

## Repository layout

```
src/agent/          the pipeline (one module per stage) and the versioned prompt
tests/              offline unit, adversarial and property-based tests
scripts/            exploration and read-only live runs
docs/decisions.md   every design decision: options, choice, rationale, impact, future work
docs/timestral_notes.md   what we learned about Timestral, with measurements
.github/workflows/  CI, trading and smoke test
CLAUDE.md           development context for the AI coding agent used on this project
```

## Status and limits

Working: the full cycle, guardrails, audit, reconciliation, scheduled operation, and the read-only [dashboard](https://spectreb.github.io/timestral-trading-agent/). Also done: the multi-asset backtest that settled the Timestral configuration ([D-006](docs/decisions.md)) and live scoring of the forecasts against realised prices ([D-013](docs/decisions.md)). Still to come: LLM behaviour scenarios, calibration of the LLM's stated confidence, and the optional news covariates. See the open items and the **Future work** section in [docs/decisions.md](docs/decisions.md).

Known limits worth knowing:
- On historical replays an LLM may already know what happened (look-ahead bias), so the live period is the clean test.
- Timestral's directional skill on daily data is not significant; the design does not rely on it.
- Reference prices come from Alpaca's free IEX feed.
