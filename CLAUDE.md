# CLAUDE.md: development context

This file gives a coding agent, or a new contributor, the context needed to work on this repository. The project is developed with an AI coding agent, and this file is the agent's working memory. User-facing documentation is in [README.md](README.md). Design decisions and their rationale are in [docs/decisions.md](docs/decisions.md).

## Project

An **autonomous paper-trading agent** built for a Mistral AI take-home assignment:

- **Timestral** (Mistral's time-series foundation model, run locally on CPU) produces probabilistic forecasts.
- A **Mistral LLM** uses those forecasts, the portfolio and market context to decide positions.
- **Deterministic guardrails** validate and bound every LLM decision.
- Orders execute on an **Alpaca paper account**. Every run is logged and published as a read-only view.

**Success criteria:** not PnL. What matters is how Timestral is integrated with the LLM, how the autonomous agent is designed and deployed, and **how we know its decisions are sound**. The repository is treated as a solution shipped to a client.

The assignment brief is kept locally (`mistral_take_home_*.md`, git-ignored). Never commit it or copy contact details from it into tracked files.

## Timeline

- Submission: **Wednesday 2026-11-25**. The agent can keep trading after submission, up to the review.
- US market closed Thursday 2026-11-26 (Thanksgiving); half day Friday 2026-11-27.
- Goal: trading live from **Monday 2026-11-23**.

## Architecture

```
Data (Alpaca) -> Forecast (Timestral) -> Baseline rule -> Decide (Mistral LLM) -> Guardrails -> Final check -> Execute (Alpaca) -> Audit log -> Dashboard
```

| Stage | Role | Decision |
|---|---|---|
| Data | Daily bars from Alpaca, completed bars only | D-002 |
| Forecast | Timestral quantile forecasts, horizon 1 to 5 days, 15 assets in one batch | D-006 |
| Baseline rule | Weights inversely proportional to the forecast interval width (Timestral as risk engine) | D-004 |
| Decide | Mistral Large 4 adjusts the baseline: free to reduce risk, at most +15 pts to add; strict JSON schema, `analysis` first | D-004, D-008 |
| Guardrails | 4 layers: output validity, portfolio risk rules, order sanity, system safety; correct-and-log or reject | D-010 |
| Final check | Independent, deliberately simple re-check of core invariants before any order | D-009 |
| Execute | Marketable limit orders, idempotent client order IDs, dry-run by default | D-010 |
| Schedule | GitHub Actions: 10:00 ET full cycle, ~15:00 ET reassessment | D-002, D-007 |

All parameters live in [src/agent/config.py](src/agent/config.py), each traced to its decision.

## Roadmap

**Start here:** [docs/roadmap.md](docs/roadmap.md) lists every decision with its status, the questions waiting for the owner and the remaining tasks. Keep it up to date with every change of status. Work point by point with the owner.

## Decisions

Recorded in [docs/decisions.md](docs/decisions.md): D-001 to D-022. The file ends with the open decisions and a **Future work** section (options deliberately deferred).

## How we work

- **Every design decision is recorded** in docs/decisions.md: context, options compared with pros and cons, choice, rationale, impact. Decisions are made step by step with the project owner: propose options with a recommendation, never pick silently.
- Distinguish **parameters** (provisional value in config, revisited with evidence) from **architecture** choices (deliberated).
- Explain findings and their implications, not just code.
- Work on branches; changes reach `main` through pull requests with green CI.

## Key findings

- On SPY daily data, Timestral is **well calibrated** (80% interval coverage ≈ 80%) but has **no significant directional edge** (54.8% hit rate over 250 days, ≈1.5 standard errors from 50%). Hence its role as a risk engine (D-004). Details: [docs/timestral_notes.md](docs/timestral_notes.md).
- Mistral Large 4 returns a native reasoning trace and a schema-valid JSON in the same call (~6 s).

## Status

| Item | Status |
|---|---|
| Timestral exploration (SPY) | Done |
| Alpaca paper account, Mistral key, GitHub secrets and variable | Done (kill switch `TRADING_ENABLED` is `false` until the owner enables it) |
| Design decisions D-001 to D-013 | Done (see docs/decisions.md) |
| Dev tooling: ruff, mypy, pytest, hypothesis, coverage gate, gitleaks, CI, branch protection | Done |
| Guardrails + independent final check | Done, 100% coverage |
| Data, forecast, baseline, decide, audit, execute, reconcile, run, smoke | Done, 215 offline tests |
| Workflows: CI, trade (10:00 and 15:00 ET), smoke test (08:30 ET) | Done, dry run verified on GitHub 2026-11-20 |
| README | Done |
| Dashboard (React page on GitHub Pages, typed FastAPI API with a static fallback, D-012, D-022) | Done; hosting the live API: waiting for the owner |
| Multi-asset backtest (D-006: 28M, prices, 512 days confirmed), live forecast scoring (D-013) | Done |
| LLM behaviour scenarios, confidence calibration | To do |
| News covariates (bonus) | Later |

## Evaluation plan

1. **Forecasts:** rolling backtest against naive baselines on the 15 assets (coverage, pinball loss, hit rate).
2. **Ablations:** baseline rule alone vs rule + LLM vs equal weight; value added by LLM deviations.
3. **LLM soundness:** behaviour scenarios, stability across repeated calls, calibration of its stated confidence.
4. **Live monitoring:** guardrail corrections and rejections per run, forecast vs realised prices.
5. Caveat: **LLM lookahead bias** on historical replays (the model may know what happened), so the live period is the clean test.

## Environment setup

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate   |   macOS/Linux: source .venv/bin/activate
pip install -r requirements-dev.txt   # runtime + dev tooling (requirements.txt is runtime only)
pre-commit install                    # gitleaks secret scan before every commit
cp .env.example .env                  # then fill in the keys
```

Checks (identical to CI), must pass before every commit:

```bash
ruff check . && ruff format --check . && mypy && pytest --cov=agent
```

## Gotchas

- Alpaca (and yfinance) return **today's incomplete daily bar** while the market is open. Use completed bars only.
- The Alpaca paper account has **margin (2× buying power) and shorting enabled** by default. Long-only and no leverage are enforced in our code.
- `mistralai` 3.x moved the client: `from mistralai.client import Mistral`. Reasoning comes back as `ThinkChunk` + `TextChunk` content parts.
- Pin LLM versions (`mistral-large-4-0`), never `-latest` aliases.
- macOS python.org Python lacks system CA certificates: on `CERTIFICATE_VERIFY_FAILED`, run with `SSL_CERT_FILE=$(python -m certifi)`.
- On Linux (CI), install PyTorch from the CPU index (`--extra-index-url https://download.pytorch.org/whl/cpu`), otherwise pip pulls multi-GB CUDA wheels.
- yfinance is for exploration only. Production data comes from Alpaca.
