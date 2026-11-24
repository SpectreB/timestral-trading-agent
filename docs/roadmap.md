# Roadmap: decisions and remaining work

The single place to see where the project stands. Details of each decision are in [decisions.md](decisions.md); this file only tracks status. Updated with every change of status.

**Key dates:** live trading from **Mon 2026-11-23** (first run 10:00 ET = 16:00 Paris) · submission **Wed 2026-11-25** (no development after it) · review on **3 or 4 December** (both agents keep trading until then).

## Decisions

| ID | Decision | Status |
|---|---|---|
| D-001 | Python 3.11 | Accepted |
| D-002 | GitHub Actions cron for scheduling | Provisional, validated in practice (runs on GitHub since 2026-11-20); to accept formally |
| D-003 | pip + venv | Accepted |
| D-004 | Timestral = risk engine for a baseline rule; the LLM adjusts it (asymmetric bounds) | Accepted |
| D-005 | Universe: 10 ETFs + 5 stocks | Accepted |
| D-006 | Timestral prices, 512-day context; the four sizes are indistinguishable (backtest completed 2026-11-22): 28M for the pipeline agent, 209M for the tool agent | Accepted |
| D-007 | Two runs a day (10:00 and 15:00 ET), 5-day horizon | Accepted |
| D-008 | Mistral Large 4 (pinned), strict JSON, analysis first | Accepted |
| D-009 | Development guardrails (tests, coverage gate, secret scan, CI, branch protection) | Accepted |
| D-010 | Runtime guardrails; turnover cap waived once for the initial deployment (2026-11-22) | Accepted |
| D-011 | Immutable audit log on the `audit-log` branch | Accepted |
| D-012 | Dashboard: static page on GitHub Pages | Accepted, confirmed by the owner 2026-11-22 |
| D-013 | Forecast scoring from recorded closes | Accepted, confirmed by the owner 2026-11-22 (a hybrid was discussed, kept optional) |
| D-014 | Performance metrics: shadow portfolios, forecast risk (Timestral volatilities x historical correlation) | Accepted 2026-11-22 |
| D-015 | Historical replay (test case 1): ~63 recent sessions (Aug to Nov 2026), daily, same agent as live, anonymised, point-in-time, simulated fills | Accepted 2026-11-22 |
| D-016 | LLM soundness checks: seven behaviour scenarios on the proposal, plus stability over repeated calls | Accepted; first results 5 of 7 (fails: calm market, tempting median); prompt v1 kept for the test window, decided with the owner 2026-11-22 |
| D-017 | Simulations requested by the evaluators through a GitHub issue (option B; full app, option D, kept in mind) | Accepted 2026-11-22 |
| D-018 | A second agent that uses Timestral as a tool ("bounded freedom", 3 tools), on its own paper account; the final agent, the pipeline agent kept as reference | Accepted 2026-11-22 |
| D-019 | News analysed into Timestral covariates: Mistral analyst, `news` (stocks) and `events` (earnings, company events, FRED macro dates), ETFs as control group, one `covariates` setting on the tools | Accepted for integration 2026-11-23; adoption after measurement |

## Waiting for the owner

| ID | Question | Recommendation |
|---|---|---|
| P-1 | ~~Replay design~~ | Resolved 2026-11-22: see D-015 |
| P-2 | ~~Switch the kill switch on (`TRADING_ENABLED`)~~ | Done 2026-11-22 15:27 UTC by the owner's decision: orders are sent from the first run on Mon 2026-11-23 (16:00 Paris) |
| P-3 | ~~Confirm or revise D-012 (dashboard technology) and D-013 (forecast scoring)~~ | Resolved 2026-11-22: both confirmed as they are |
| P-4 | Accept D-002 formally | Accept |

## Tasks

| ID | Task | Owner | Status |
|---|---|---|---|
| T-01 | Unpublished test of the trade workflow on GitHub (new reconcile and observation steps) | Claude | Done 2026-11-22: every step passed (decision accepted, 14 orders planned in dry run, reconciliation and observation OK), nothing published |
| T-02 | Switch the kill switch on | Claude, on the owner's instruction | Done 2026-11-22 |
| T-03 | Performance metrics: forecast risk module, shadow portfolios (agent, LLM proposal, Timestral rule, equal weight, SPY), comparisons with confidence intervals, risk and LLM-confidence calibration (D-014) | Claude | Done 2026-11-22 (merged) |
| T-04 | Dashboard redesign, user-first (structure validated by the owner, reversible; see the redesign note in D-012) | Claude | Done 2026-11-22 (merged). Replay results (T-05) will take their place under Performance |
| T-05 | Replay harness (test case 1) as specified in D-015 | Claude | Done 2026-11-23: 63 sessions replayed (pipeline agent), report and records in `docs/replay`; to show on the dashboard (T-11) |
| T-06 | LLM soundness checks (D-016): behaviour scenarios and stability | Claude | Done 2026-11-22: 5 of 7 scenarios pass, stability measured; report in `docs/evals` |
| T-08 | Simulations requested by the evaluators (D-017): simulation run type, issue-triggered workflow with throttling, reply in the issue, dashboard button and list | Claude | Done 2026-11-22: merged (#18), tested end to end with issue #20 (reply in 3 min 19, issue closed, simulation listed on the dashboard) |
| T-09 | Tool agent (D-018): the three Timestral tools, the decision loop, its prompt, both agents in the run, replay, evaluations, reconciliation and Trade workflow | Claude | Done 2026-11-22 (#21), first real run accepted |
| T-10 | Second Alpaca paper account for the tool agent: create it at 100,000 USD, add the secrets `ALPACA_TOOLS_API_KEY` and `ALPACA_TOOLS_SECRET_KEY`, then the variable `TRADING_ENABLED_TOOLS` | Owner | To do |
| T-11 | Dashboard with both agents, the tool agent first, and the comparison of the two approaches | Claude | Done 2026-11-23 (#23): comparison card, one tab per agent, tool use shown, replay card |
| T-12 | Soundness checks (D-016) and replay (D-015) on the tool agent | Claude | Checks done: v1 6 of 7; prompt decide_tools_v2 adopted on 10-run evidence (calm market 7/10 against 4/10, churn halved, crash unchanged). Replay with v1 running, replay with v2 next |
| T-13 | News as covariates for Timestral (D-019, the brief's bonus) | Claude | Integration done (analyst, covariates, tools, prompt v3, daily scoring in the Trade workflow). **Priority since 2026-11-23:** measurement done 2026-11-24: **no gain, not adopted**, live tool agent unchanged (result on the dashboard and in D-019). Follow-up: fix GOOGL matching, then measure calendars and market variables (rule declared beforehand) |
| T-07 | Final pass before submission: README, CLAUDE.md, decision statuses, release `v1.0` | Claude | Wed 2026-11-25 morning |

Done (most recent first): evaluator simulations (#18) · LLM outage handling, no crash on a timeout (#19) · turnover cap waiver and no-publish test option (#12) · forecast scoring and backtest (#11) · dashboard on Pages (#10) · README (#9) · reconciliation (#8) · workflows (#7) · execution and full cycle (#6) · audit log (#5) · decision stage (#4) · forecast stage (#3) · guardrails (#2) · tooling and docs (#1).

## Not before submission (documented as future work)

More asset types · volatility targeting · re-basing detection (D-013 hybrid, dropped: dividends move prices by less than 1%, splits are rare) · `pip-audit` (last, if time allows). See *Future work* in [decisions.md](decisions.md).

## Mistral API budget (checked 2026-11-22)

Not a constraint. The organisation is on a **Team plan sponsored by Mistral until 2027-02-20**, and API calls use the free tier included in it (pay-as-you-go is **not** activated and should stay off). Rate limits returned by the API: 30 M tokens and 4,000 requests per minute. Measured cost so far: 8 requests, about $0.09 estimated, i.e. about one cent per decision; a daily replay over six months (~125 decisions) would be about $1.5 at that rate.
