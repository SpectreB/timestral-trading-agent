# Timestral Paper-Trading Agent

Two autonomous agents trade a small portfolio of US ETFs and stocks, each on its own [Alpaca](https://alpaca.markets) **paper account** (real prices, fictitious money). [Timestral](https://mistral.ai/news/timestral/), Mistral's time-series foundation model, estimates the risk. A Mistral LLM chooses the portfolio. Deterministic code, not the LLM, decides what may reach the broker.

**Dashboard:** https://spectreb.github.io/timestral-trading-agent/ (live activity, how it works, the evidence, and a simulation anyone can request).

> Paper trading only, for demonstration. Nothing here is financial advice.

The project is judged on **how the decisions are made and how we know they are sound**, not on profit. Three things carry the design: a measurable role for each model, guardrails the LLM cannot bypass, and an audit trail that cannot be edited.

## How it works

The two agents share the data, the 15 assets, the guardrails and the order code. They differ in one thing: how the LLM and Timestral are combined.

```
Pipeline agent   Alpaca bars ─► Timestral ─► rule: weight ∝ 1/spread ─► LLM adjusts ─► guardrails ─► final check ─► orders
                                (all assets, once)                     (+15 pts max)

Tool agent       Alpaca bars ─► LLM ⇄ Timestral tools ─► LLM decides ─► guardrails ─► final check ─► orders
                               (calls again or stops,
                                6 rounds, 12 calls max)

Both             a rejected answer gets one retry with the reason, then the positions are kept
                 every input, answer and order ─► immutable audit log ─► dashboard
```

| Stage | What it does | Code |
|---|---|---|
| **Data** | Adjusted daily closes for 15 assets (10 ETFs, 5 large stocks). Today's unfinished bar is dropped; any data problem stops the run. | [data.py](src/agent/inputs/data.py) |
| **Forecast** | Timestral on CPU: quantiles of the cumulative return 1 to 5 trading days ahead, for all assets in one batch. | [forecast.py](src/agent/inputs/forecast.py) |
| **Rule** | Each weight inversely proportional to the width of Timestral's 80% interval (the *spread*): the more uncertain, the smaller the position. | [baseline.py](src/agent/decision/baseline.py) |
| **Decide, pipeline agent** | Mistral Large 4 (pinned, native reasoning) gets the forecasts, the rule's portfolio and the market context, and returns strict JSON: its analysis, then a weight, a reason and a confidence per asset. It may cut risk freely but add at most 15 points to an asset. | [decide.py](src/agent/decision/decide.py), [prompt](src/agent/decision/prompts/decide_v1.md) |
| **Decide, tool agent** | The same model starts without the forecasts and asks for them through three tools: `get_forecasts`, `evaluate_portfolio` (Timestral's forecast of a candidate portfolio, and whether the guardrails would accept it) and `get_baseline`. Our own loop, no agent framework ([D-021](docs/decisions.md)). | [decide_tools.py](src/agent/decision/decide_tools.py), [tools.py](src/agent/decision/tools.py), [prompt](src/agent/decision/prompts/decide_tools_v2.md) |
| **Guardrails** | Reject unusable answers, correct and log rule breaches, apply a graduated drawdown circuit breaker. | [guardrails.py](src/agent/checks/guardrails.py) |
| **Final check** | A second, deliberately simple implementation re-checks the core limits before any order. | [safety.py](src/agent/checks/safety.py) |
| **Execute** | Marketable limit orders, sells before buys, one order id per decision and asset, paper-account check, kill switch. | [execute.py](src/agent/execution/execute.py) |
| **Record** | One immutable record per run, then fills and slippage; the account value sampled every hour of the US session. | [audit.py](src/agent/execution/audit.py), [reconcile.py](src/agent/execution/reconcile.py), [valuation.py](src/agent/jobs/valuation.py) |

### Why Timestral is a risk engine

On daily data Timestral's intervals are **well calibrated** (about 80% of outcomes fall inside its 80% interval) but its **direction has no measurable edge** (hit rate within noise of 50%, see [docs/timestral_notes.md](docs/timestral_notes.md)). So the agents use what it does reliably, the width of its intervals, and not its median. The LLM brings what a time-series model cannot: reasoning across assets, about the market regime and about events. Because the rule's portfolio, the LLM's proposal and the executed portfolio are all recorded, what each layer adds can be measured separately.

## How we know the decisions are sound

- **Situations built on purpose.** Seven market situations where the sound decision is known in advance (a crash, a very uncertain asset, drawdown limits, a calm market, a tempting median, an instruction hidden in the news), each repeated, plus the stability of repeated answers ([D-016](docs/decisions.md), [docs/evals](docs/evals)).
- **Replay on 63 past sessions**, one decision a day with only the data of that day, assets renamed and dates shifted so the LLM cannot recognise the period ([D-015](docs/decisions.md), [docs/replay](docs/replay), [docs/replay_tools](docs/replay_tools)).
- **Live scoring of Timestral's forecasts** against the prices that followed ([D-013](docs/decisions.md)).
- **News covariates measured before adoption**: a rule written before the results, 118 days, 29 stocks and 10 ETFs as control. No gain, so not adopted ([D-019](docs/decisions.md), [docs/covariates](docs/covariates)).
- **Guardrails tested adversarially**: invented tickers, text instead of numbers, `NaN`, short positions, concentration, truncated JSON, and thousands of random proposals per test run (property-based tests). Every one is rejected or passes the independent final check.
- **100% line and branch coverage** enforced in CI on the 24 modules of the decision, execution and reporting path. Tests run offline: fake brokers, data, models and LLMs, never the network, never an order.
- **A write-ahead audit log**: what the agent saw and decided is written before any order is sent, and never modified. Re-running a run id fails.
- **Every design choice is recorded** with the options considered in [docs/decisions.md](docs/decisions.md), including what was set aside.

## Safety model

| Layer | Protection |
|---|---|
| Output validity | Strict JSON schema, assets limited to the universe, finite numbers only, weights summing to 100% |
| Portfolio rules | Long only, no leverage (90% invested at most), cash 10% or more, at most 25% per ETF, 10% per stock, 30% per sector group; pipeline agent: at most +15 points over the rule |
| Circuit breaker | Drawdown from peak: −3% alert, −5% no new risk, −8% exposure capped at 60%, −12% rule at half exposure and LLM paused |
| Orders | 2-point no-trade band, 30% turnover cap per run, sells before buys, a failed sell cancels the buys |
| System | Paper-account check, a kill switch per agent, run-level idempotency, green CI required on the deployed commit |

A rejected answer gets one retry with the reason. If it is rejected again, the agent keeps its positions: it never sells because of a formatting error.

## Quick start

Requires Python 3.11.

```bash
python -m venv .venv
source .venv/bin/activate              # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt    # on Linux, add --extra-index-url https://download.pytorch.org/whl/cpu
pip install --no-deps -e .
cp .env.example .env                   # then fill in your Mistral and Alpaca PAPER keys
```

Run one cycle. **This is a dry run: nothing is sent to the broker.**

```bash
python -m agent.jobs.run --mode morning --audit-dir audit                                    # pipeline agent
python -m agent.jobs.run --agent tools --mode morning --audit-dir audit/agents/tools         # tool agent (its own keys)
```

It loads the data, forecasts, calls the LLM, applies the guardrails, plans the orders and writes the record under `audit/`. The first call downloads the Timestral weights from Hugging Face.

Other entry points:

```bash
python -m agent.jobs.smoke                  # check every external service, no order
python -m agent.dashboard.web --audit-dir audit --out site   # build the dashboard locally (site/index.html)
python scripts/run_forecast.py         # forecasts and the rule's portfolio only
ruff check . && ruff format --check . && mypy && pytest --cov=agent   # the CI checks
```

## Operation

GitHub Actions runs everything. An external cron service (cron-job.org) starts the scheduled workflows on time through GitHub's API, because GitHub's own scheduler barely fired for this repository ([D-002](docs/decisions.md), [D-007](docs/decisions.md)):

| Workflow | When (US Eastern time) | What |
|---|---|---|
| **Trade** | 10:00 (full cycle) and 15:00 (reassessment), trading days | One cycle per agent, reconciliation, the day's closes, news scoring, then the records are pushed to the [`audit-log`](../../tree/audit-log) branch |
| **Valuation** | every hour of the session, and after the close | Reads both accounts (no order) |
| **Smoke test** | 08:30 | Checks Alpaca, Timestral and the pinned Mistral model, without trading |
| **Simulate** | when a "Simulation request" issue is opened | One cycle with the requested options, never an order, the answer posted in the issue |
| **Dashboard** | after every Trade, Valuation and Simulate run, and on changes to the page | Rebuilds the page and deploys it to GitHub Pages |
| **CI** | every push and pull request | Lint, types, tests, coverage gate, secret scan |

GitHub's own crons are kept as a fallback. A run checks that the market is open, and a repeated run of the same slot does nothing.

Setup: the secrets `MISTRAL_API_KEY`, `ALPACA_API_KEY` and `ALPACA_SECRET_KEY` (and `ALPACA_TOOLS_API_KEY`, `ALPACA_TOOLS_SECRET_KEY`, `FRED_API_KEY` for the tool agent and the news scoring), and the repository variables `TRADING_ENABLED` and `TRADING_ENABLED_TOOLS`. **These variables are the kill switches**: an agent sends orders only when its variable is `true`; otherwise its runs are recorded as dry runs. `main` is protected: changes go through pull requests with green CI.

## Stack

| Layer | Choice | Why | Decision |
|---|---|---|---|
| Language | Python 3.11, pip and venv | One runtime for data, model and agent | [D-001, D-003](docs/decisions.md) |
| Market data and broker | Alpaca: daily bars (completed sessions only), two paper accounts | Free paper trading with real prices, one API for data and orders | [D-002](docs/decisions.md) |
| Forecast | Timestral 28M (pipeline agent) and 209M (tool agent), on CPU | Calibrated risk; no measurable direction, so used for risk | [D-004, D-006](docs/decisions.md) |
| Decision | Mistral Large 4 (`mistral-large-4-0`, pinned), native reasoning, strict JSON | A reasoning trace and a schema-valid answer in one call | [D-008](docs/decisions.md) |
| Orchestration | A simple harness of our own on the Mistral SDK: no LangChain, no LangGraph | One bounded decision with three tools: every step plain, tested and recorded | [D-021](docs/decisions.md) |
| Safety | Deterministic guardrails and an independent final check, in Python | The LLM proposes; code decides what reaches the broker | [D-009, D-010](docs/decisions.md) |
| Scheduling | GitHub Actions: trading twice a day, the accounts read hourly | No server to run; every run leaves a log | [D-002, D-007](docs/decisions.md) |
| Records | Immutable JSON on the `audit-log` branch, CSV tables derived from it | Append-only, readable, public | [D-011](docs/decisions.md) |
| Dashboard | React 18 front end (htm, no build step) on GitHub Pages, reading a typed FastAPI API (live server, with a static export as fallback) | A declared, tested contract; the page works even when the server sleeps | [D-012, D-022](docs/decisions.md) |
| Validation | Pydantic at the boundaries: LLM output, configuration, broker data, orders, news scores, API responses | Bad data fails where it enters, not deep in a run | [D-008, D-022](docs/decisions.md) |
| Quality | ruff, mypy, pytest with hypothesis, 100% coverage on the core, gitleaks, protected `main` | Changes reach trading only through green CI | [D-009](docs/decisions.md) |

## Dashboard

https://spectreb.github.io/timestral-trading-agent/, rebuilt after every run and every hourly account reading. Four views: **Live** (both accounts, the last decision and its reasoning, Timestral's forecast, the portfolio, the orders), **How it works** (the design, the inputs, the guardrails), **Evidence** (the checks above, side by side for the two agents) and **Try it** (ask an agent to decide now; no order is sent). Python computes the data from the audit log ([web.py](src/agent/dashboard/web.py)) and serves it through a read-only FastAPI API ([api.py](src/agent/dashboard/api.py), [models](src/agent/dashboard/models.py), documented at `/api/docs`); a React page in [web/](web) lays it out ([D-012, D-022](docs/decisions.md)). The page shows the API's snapshot published with the site at once, then the live server's answer when it is awake. Locally: `python -m agent.dashboard.api --audit-dir audit` serves the page and the API on http://localhost:8000.

## Reading the audit log

The [`audit-log`](../../tree/audit-log) branch is the record of the agents' activity. The pipeline agent's records are at the root, the tool agent's under `agents/tools/`, with the same layout:

```
runs/YYYY-MM-DD_<mode>.json            one record per run: inputs, forecasts, the four portfolios
                                       (current, rule, LLM proposal, final), prompts, answers,
                                       tool calls, guardrail results, planned orders
runs/YYYY-MM-DD_<mode>_reasoning.txt   the LLM's reasoning trace
reconciliation/*.json                  order submissions, then fills and slippage
valuations/*.json                      the account value, read every hour of the session
observations/*.json                    the closes used to score the forecasts
tables/*.csv                           flat tables derived from the records
simulations/                           the runs requested from the dashboard (never traded)
news/                                  the scored news articles (D-019)
```

## Repository layout

```
src/agent/
  config.py           every parameter, each traced to its decision
  inputs/             market data, Timestral forecasts, news covariates
  decision/           the rule baseline, both LLM agents, their tools and versioned prompts
  checks/             guardrails, the independent final check, risk figures
  execution/          orders, reconciliation with the broker, the audit log
  evaluation/         forecast scoring, metrics, replays, behaviour tests
  jobs/               the entry points the workflows run (trade, smoke, valuation, simulate)
  dashboard/          the dashboard's data and its API (FastAPI)
web/                  the dashboard page (React, no build step)
tests/                offline unit, adversarial and property-based tests
scripts/              exploration, backtests and read-only live runs
docs/decisions.md     every design decision: options, choice, rationale, impact, future work
docs/roadmap.md       the status of each decision and task
docs/timestral_notes.md   what we learned about Timestral, with measurements
docs/evals/           the situations built on purpose, with their results
docs/replay/, docs/replay_tools/   the 63-session replays of the two agents
docs/covariates/      the news covariate measurement
.github/workflows/    CI, trading, valuation, smoke test, simulations, dashboard
CLAUDE.md             working context for the AI coding agent used on this project
```

## Limits

- The live period is a few trading days: too short to compare the agents' results, which is why the evidence relies on built situations and a replay as well.
- On a historical replay an LLM may remember what happened next. Assets are renamed and dates shifted to prevent it, but the live period remains the clean test.
- Timestral's daily direction has no measurable skill here; the design does not rely on it.
- The schedule depends on an external cron service; a missed slot can be run by hand from the Actions tab.
- Reference prices come from Alpaca's free IEX feed.
