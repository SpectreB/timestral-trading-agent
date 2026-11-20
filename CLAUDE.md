# CLAUDE.md — Project context and handoff

Read this first. It holds the full context needed to continue the work on any machine.

## The task

Mistral take-home assignment (brief: [mistral_take_home_assignment.md](mistral_take_home_assignment.md), logistics: [mistral_take_home_next_steps.md](mistral_take_home_next_steps.md)).

Build an **autonomous paper-trading agent**:
- **Timestral** (Mistral's time-series foundation model, run locally on CPU) produces probabilistic forecasts.
- A **Mistral LLM** (via API, $20 of credits granted) uses those forecasts to decide positions.
- Orders execute on an **Alpaca paper account** during the assignment week.
- Optional bonus: news analysis (e.g. GDELT) used to build covariates for Timestral.

**What is evaluated:** not PnL. Evaluators look at how Timestral is integrated with the LLM, the design and deployment of an autonomous agent, and **how we know its decisions are sound**. It should be treated as a solution shipped to a client: clean repo, runnable instructions, and a read-only view of trading activity (export or dashboard).

## Timeline

- Email received **Wednesday 2026-11-18** (afternoon), so the submission deadline is **Wednesday 2026-11-25**.
- US market closed Thursday 2026-11-26 (Thanksgiving); half day Friday 2026-11-27.
- The 20-minute review (presentation + Q&A) must be booked with **at least 8 days' notice**. The agent can keep trading after submission, up to the review.
- **Priority: get a minimal pipeline trading live ASAP** (by Friday 2026-11-20 if possible), then iterate.

## Status

| Item | Status |
|---|---|
| Mistral org ID sent for credits | Done |
| Git repo | Done: https://github.com/SpectreB/timestral-trading-agent |
| Timestral installed and explored | Done, see [docs/timestral_notes.md](docs/timestral_notes.md) |
| Alpaca paper account and API keys | Done (paper account verified 2026-11-20: $100k, keys in local .env) |
| Mistral API key | Done (verified 2026-11-20, keys in local .env) |
| Review booked | Unknown, ask the user |
| Live pipeline | Not started |

## How the user wants to work

- **Every stack or design decision goes into [docs/decisions.md](docs/decisions.md)**, using the template there: context, options compared (pros and cons), what was chosen, why, and the impact on further development. Decisions are made **step by step with the user**. Propose options with a recommendation; don't silently pick.
- Explain findings and their implications, not just code.
- Commit at meaningful checkpoints.

## Decisions so far (details in docs/decisions.md)

- **D-001 Python 3.11:** accepted.
- **D-002 GitHub Actions cron for scheduling and deployment:** provisional. Consequences: the pipeline must be stateless and idempotent, the audit log is committed back to the repo, and model weights are cached with `actions/cache`. CPU speed has since been validated locally (≈12 ms per series).

## Planned architecture (agreed direction, not yet built)

```
Data -> Forecast (Timestral) -> Decide (Mistral LLM, structured JSON) -> Guardrails (deterministic) -> Execute (Alpaca) -> Audit log -> Dashboard
```

- **Data:** Alpaca daily bars for a small universe (8–15 liquid ETFs and large caps), FRED macro data, and optionally GDELT news.
- **Forecast:** Timestral quantile forecasts per ticker. News sentiment, scored by the LLM, is a candidate **past covariate** (bonus).
- **Decide:** the LLM receives forecast quantiles, the portfolio, risk stats and a news summary. It returns JSON target weights with a confidence and rationale per position.
- **Guardrails:** code that clips or validates the LLM output (max position size, gross exposure, cash buffer, turnover, market hours). It falls back to "hold" on malformed output.
- **Execute:** rebalance to target weights through Alpaca, with idempotent client order IDs and a dry-run mode.
- **Audit log:** every run stores its inputs, forecasts, the LLM prompt and response, guardrail adjustments, orders and fills.
- **Dashboard:** static page (GitHub Pages) or Streamlit built from the audit log. This doubles as the read-only view for reviewers.

## Evaluation plan ("decisions are sound")

1. Forecast quality: rolling backtest against naive baselines, measuring quantile coverage, pinball loss/CRPS and directional hit rate (started in `scripts/explore_timestral.py`).
2. Ablations on a historical replay: full agent vs forecast-only rule vs LLM-only vs equal-weight buy-and-hold.
3. Caveat: **LLM lookahead bias**. The LLM may already know how historical prices moved, so the live period is the clean test.
4. Live monitoring: does each decision agree with its forecast, how often do guardrails fire, how often does LLM output fail to parse, and how do forecasts compare with realized prices.

## Key finding so far

On SPY daily data, Timestral is **well calibrated** (80% interval coverage ≈ 80%) but has **little directional edge** (hit rate within noise of 50%). It only slightly beats empirical-quantile baselines. Working hypothesis: use Timestral mainly as a **risk/uncertainty engine** (sizing positions by forecast spread) and let the LLM bring the directional view from context and news. Covariates are where Timestral might add signal. Not yet a recorded decision; validate on more tickers first.

## Immediate next steps

1. Run the multi-ticker exploration (candidate universe × 2–3 model sizes). Then record decisions for the universe, model size, and whether to feed prices or returns.
2. Build the minimal live pipeline (Alpaca data, Timestral, LLM, guardrails, Alpaca orders, JSONL audit log) and deploy it on GitHub Actions.
3. Then: evaluation suite, news covariates, dashboard, README.

Open decisions are listed at the bottom of docs/decisions.md.

## Environment setup

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate   |   macOS/Linux: source .venv/bin/activate
pip install -r requirements-dev.txt   # runtime + dev tooling (requirements.txt is runtime only)
pre-commit install                    # gitleaks secret scan before every commit
python scripts/explore_timestral.py --ticker SPY --model mistralai/Timestral-28M
```

Checks (identical to CI, D-009), must pass before every commit:

```bash
ruff check . && ruff format --check . && mypy && pytest
```

Secrets go in `.env` (git-ignored): `MISTRAL_API_KEY`, `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`. All parameters live in `src/agent/config.py`; each traces back to a decision in docs/decisions.md.

## Gotchas

- yfinance returns **today's incomplete bar** during market hours. The live pipeline must use only completed bars.
- yfinance is for exploration only. Production data comes from Alpaca.
- Alpaca also returns **today's incomplete daily bar** while the market is open. Drop it, or check the clock, before forecasting.
- The Alpaca paper account has **margin (2x buying power) and shorting enabled** by default. Guardrails must enforce long-only and no leverage in code.
- `mistralai` 3.x moved the client: use `from mistralai.client import Mistral` (the old `from mistralai import Mistral` fails).
- macOS python.org Python lacks system CA certificates: if a download fails with `CERTIFICATE_VERIFY_FAILED`, run with `SSL_CERT_FILE=$(python -m certifi)`.
- On Linux (CI), install PyTorch from the CPU index (`--extra-index-url https://download.pytorch.org/whl/cpu`), otherwise pip pulls multi-GB CUDA wheels.
