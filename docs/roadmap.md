# Roadmap: decisions and remaining work

The single place to see where the project stands. Details of each decision are in [decisions.md](decisions.md); this file only tracks status. Updated with every change of status.

**Key dates:** live trading from **Mon 2026-11-23** (first run 10:00 ET = 16:00 Paris) · submission **Wed 2026-11-25** (no development after it) · review booked by the owner (the agent keeps trading until then).

## Decisions

| ID | Decision | Status |
|---|---|---|
| D-001 | Python 3.11 | Accepted |
| D-002 | GitHub Actions cron for scheduling | Provisional, validated in practice (runs on GitHub since 2026-11-20); to accept formally |
| D-003 | pip + venv | Accepted |
| D-004 | Timestral = risk engine for a baseline rule; the LLM adjusts it (asymmetric bounds) | Accepted |
| D-005 | Universe: 10 ETFs + 5 stocks | Accepted |
| D-006 | Timestral 28M, prices, 512-day context | Accepted after the backtest (2026-11-21) |
| D-007 | Two runs a day (10:00 and 15:00 ET), 5-day horizon | Accepted |
| D-008 | Mistral Large 4 (pinned), strict JSON, analysis first | Accepted |
| D-009 | Development guardrails (tests, coverage gate, secret scan, CI, branch protection) | Accepted |
| D-010 | Runtime guardrails; turnover cap waived once for the initial deployment (2026-11-22) | Accepted |
| D-011 | Immutable audit log on the `audit-log` branch | Accepted |
| D-012 | Dashboard: static page on GitHub Pages | Taken autonomously; **to confirm by the owner** |
| D-013 | Forecast scoring from recorded closes | Taken autonomously; **to confirm by the owner** |
| D-014 | Performance metrics: shadow portfolios, forecast risk (Timestral volatilities x historical correlation) | Agreed in principle 2026-11-22; to record with T-03 |
| D-015 | Historical replay (test case 1): ~63 recent sessions (Aug to Nov 2026), daily, same agent as live, anonymised, point-in-time, simulated fills | Accepted 2026-11-22 |

## Waiting for the owner

| ID | Question | Recommendation |
|---|---|---|
| P-1 | ~~Replay design~~ | Resolved 2026-11-22: see D-015 |
| P-2 | ~~Switch the kill switch on (`TRADING_ENABLED`)~~ | Done 2026-11-22 15:27 UTC by the owner's decision: orders are sent from the first run on Mon 2026-11-23 (16:00 Paris) |
| P-3 | Confirm or revise D-012 and D-013 | |
| P-4 | Accept D-002 formally | Accept |

## Tasks

| ID | Task | Owner | Status |
|---|---|---|---|
| T-01 | Unpublished test of the trade workflow on GitHub (new reconcile and observation steps) | Claude | Done 2026-11-22: every step passed (decision accepted, 14 orders planned in dry run, reconciliation and observation OK), nothing published |
| T-02 | Switch the kill switch on | Claude, on the owner's instruction | Done 2026-11-22 |
| T-03 | Metrics: forecast risk module, shadow portfolios (agent, LLM proposal, baseline, equal weight, SPY), comparisons with confidence intervals, risk and LLM-confidence calibration; record D-014 | Claude | In progress: code written on `feat/metrics`, tests to write |
| T-04 | Dashboard redesign, user-first. **Structure validated by the owner 2026-11-22 (reversible):** one page, grouped by the reader's question: status banner (live or simulation, last and next run, alerts) · today (what changed, why, how the orders were filled) · performance (one chart with the benchmarks, comparisons with their confidence interval and sample size, exposure, distance to the circuit-breaker levels) · portfolio (allocation against the baseline, risk concentration) · evidence, collapsed (forecast calibration, forecast vs realised risk, LLM confidence, guardrail activity, executions, run history) · 3-month replay once it exists. Light interactivity only: tooltips, collapsible sections, click a run or a point to open its detail on the same page, click a legend entry to hide a curve; no filters, tabs or date ranges. Also shows: version that decided (model, prompt, code), real account against the theoretical portfolio, definitions of the benchmarks | Claude | To do (after T-03; waits for the owner's confirmation that the static page, D-012, is enough) |
| T-05 | Replay harness (test case 1) as specified in D-015: point-in-time data enforced in code, anonymisation, leakage probe, simulated fills, separate output, shadow portfolios | Claude | To do (after T-03) |
| T-06 | LLM soundness: stability across repeated calls, a few behaviour scenarios (simulated crash, very wide intervals) | Claude | To do (partly covered by T-03 and T-05) |
| T-07 | Final pass before submission: README, CLAUDE.md, decision statuses, release `v1.0` | Claude | Wed 2026-11-25 morning |

Done (most recent first): turnover cap waiver and no-publish test option (#12) · forecast scoring and backtest (#11) · dashboard on Pages (#10) · README (#9) · reconciliation (#8) · workflows (#7) · execution and full cycle (#6) · audit log (#5) · decision stage (#4) · forecast stage (#3) · guardrails (#2) · tooling and docs (#1).

## Not before submission (documented as future work)

Timestral as a tool the LLM calls (portfolio risk, scenarios; D-004 option D, prepared by T-03) · news covariates (bonus) · more asset types. See *Future work* in [decisions.md](decisions.md).

## Mistral API budget (checked 2026-11-22)

Not a constraint. The organisation is on a **Team plan sponsored by Mistral until 2027-02-20**, and API calls use the free tier included in it (pay-as-you-go is **not** activated and should stay off). Rate limits returned by the API: 30 M tokens and 4,000 requests per minute. Measured cost so far: 8 requests, about $0.09 estimated, i.e. about one cent per decision; a daily replay over six months (~125 decisions) would be about $1.5 at that rate.
