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
| D-015 | Historical replay (test case 1) | Agreed in principle 2026-11-22 (recent test period, anonymised assets); details open (P-1) |

## Waiting for the owner

| ID | Question | Recommendation |
|---|---|---|
| P-1 | Replay design: test period, decision frequency, how the earlier period informs the agent | Most recent months as test period; daily decisions (cost is not a constraint, see below); earlier period used to tune parameters and write a "lessons" note for the prompt |
| P-2 | When to switch `TRADING_ENABLED` to `true` | Before Mon 16:00 Paris (T-01 has passed) |
| P-3 | Confirm or revise D-012 and D-013 | |
| P-4 | Accept D-002 formally | Accept |

## Tasks

| ID | Task | Owner | Status |
|---|---|---|---|
| T-01 | Unpublished test of the trade workflow on GitHub (new reconcile and observation steps) | Claude | Done 2026-11-22: every step passed (decision accepted, 14 orders planned in dry run, reconciliation and observation OK), nothing published |
| T-02 | Switch the kill switch on | Owner | Before Mon 16:00 Paris |
| T-03 | Metrics: forecast risk module, shadow portfolios (agent, LLM proposal, baseline, equal weight, SPY), comparisons with confidence intervals, risk and LLM-confidence calibration; record D-014 | Claude | In progress: code written on `feat/metrics`, tests to write |
| T-04 | Dashboard redesign, user-first: one page answering four questions (is it running, how is it doing, what does it hold and why, can we trust it), evidence collapsed, metrics from T-03 | Claude | To do (after T-03) |
| T-05 | Replay harness (test case 1): point-in-time data enforced in code, anonymisation, leakage probe, simulated fills, separate store, same guardrails, variants compared; record D-015 | Claude | To do (after P-1) |
| T-06 | LLM soundness: stability across repeated calls, a few behaviour scenarios (simulated crash, very wide intervals) | Claude | To do (partly covered by T-03 and T-05) |
| T-07 | Final pass before submission: README, CLAUDE.md, decision statuses, release `v1.0` | Claude | Wed 2026-11-25 morning |

Done (most recent first): turnover cap waiver and no-publish test option (#12) · forecast scoring and backtest (#11) · dashboard on Pages (#10) · README (#9) · reconciliation (#8) · workflows (#7) · execution and full cycle (#6) · audit log (#5) · decision stage (#4) · forecast stage (#3) · guardrails (#2) · tooling and docs (#1).

## Not before submission (documented as future work)

Timestral as a tool the LLM calls (portfolio risk, scenarios; D-004 option D, prepared by T-03) · news covariates (bonus) · more asset types. See *Future work* in [decisions.md](decisions.md).

## Mistral API budget (checked 2026-11-22)

Not a constraint. The organisation is on a **Team plan sponsored by Mistral until 2027-02-20**, and API calls use the free tier included in it (pay-as-you-go is **not** activated and should stay off). Rate limits returned by the API: 30 M tokens and 4,000 requests per minute. Measured cost so far: 8 requests, about $0.09 estimated, i.e. about one cent per decision; a daily replay over six months (~125 decisions) would be about $1.5 at that rate.
