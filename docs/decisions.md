# Decision Log

Each entry records a technical choice: the context, the options considered, what we chose and why, and how it shapes later work.

**Status values:** `Accepted` (settled), `Provisional` (chosen but to be validated), `Superseded by D-XXX`.

## Template

```markdown
## D-XXX — Title

- **Date:** YYYY-MM-DD
- **Status:** Accepted | Provisional | Superseded by D-YYY

### Context
What problem are we solving, and what constraints apply?

### Options
| Option | Pros | Cons |
|---|---|---|
| A | ... | ... |
| B | ... | ... |

### Decision
What we chose.

### Rationale
Why this option wins under our constraints.

### Impact on further development
What this enables, what it rules out, and what we must revisit later.
```

---

## D-001 — Implementation language: Python

- **Date:** 2026-11-19
- **Status:** Accepted

### Context
The agent must run Timestral locally, call the Mistral API, call the Alpaca API, and do data and evaluation work.

### Options
| Option | Pros | Cons |
|---|---|---|
| Python | Timestral reference code is Python/PyTorch; official `mistralai` and `alpaca-py` SDKs; pandas/numpy for evaluation | Slower runtime (irrelevant at daily frequency) |
| TypeScript / other | Good for web dashboards | No Timestral inference path; the ML ecosystem is weaker |

### Decision
Python 3.11.

### Rationale
Every critical dependency (Timestral, Mistral SDK, Alpaca SDK, data libraries) is Python-first. Performance is not a constraint at our trading frequency.

### Impact on further development
One language from data to evaluation. The dashboard is also likely to be Python-generated (static HTML or Streamlit) unless a later decision says otherwise.

---

## D-002 — Scheduling and deployment: GitHub Actions cron

- **Date:** 2026-11-19
- **Status:** Provisional (validate Timestral CPU runtime and weight caching on a GitHub-hosted runner)

### Context
The agent must run unattended during the assignment week, at no cost, in a way reviewers can inspect.

### Options
| Option | Pros | Cons |
|---|---|---|
| GitHub Actions cron | Free; every run is logged and visible to reviewers; no server to maintain; secrets management built in | Stateless runners (state must be persisted to the repo or artifacts); cron may start minutes late; weights must be cached or downloaded each run |
| Small VM (e.g. a cloud free tier) | Persistent state; precise timing | Server upkeep; harder for reviewers to audit; free tiers can be unreliable |
| Local machine + task scheduler | Simplest to build | Not autonomous (depends on the laptop being on); not shippable |

### Decision
GitHub Actions scheduled workflows.

### Rationale
At daily frequency, a few minutes of cron jitter is harmless. Auditability (a public run history) supports the "how do you know decisions are sound" requirement. Timestral is small enough for CPU inference (the blog reports 17–113 ms per forecast on 8 threads), so a standard runner should be enough.

### Impact on further development
- The pipeline must be **stateless and idempotent**: each run reconstructs state from Alpaca and the persisted audit log.
- The audit log must be committed back to the repo (or a dedicated branch) so the dashboard can be built from it. **Refined by D-011:** a dedicated `audit-log` branch, because `main` is protected (D-009) and cannot receive automated commits.
- Hugging Face weights should be cached with `actions/cache`.
- Revisit if runtime or reliability on the runner proves insufficient.

### Implementation notes (workflows, 2026-11-20)
- **`trade.yml`:** two cron schedules (15:00 and 20:00 UTC, i.e. 10:00 and 15:00 ET while the US is on standard time; shift by one hour when daylight saving time starts in March) plus a manual dispatch. A `gate` job requires a successful CI run on the exact commit being deployed ("trading gated on CI", D-009). Runs are serialised (`concurrency`, never cancelled midway). On a holiday or half-day afternoon the market-open check ends the run before any LLM call.
- **Kill switch:** the repository variable `TRADING_ENABLED` must be `true` for orders to be sent. Scheduled runs always request live orders; with the variable unset or `false` they are recorded as `dry_run_kill_switch`, which lets us watch the first runs before enabling trading.
- **Audit log:** the `audit-log` branch is checked out into `audit/`, the run writes there, and the workflow pushes it afterwards, **also after a failure** (the record is written before any order, D-011). Derived CSV tables are rebuilt on every publish.
- **`smoke.yml`:** a daily smoke test at 13:30 UTC (08:30 ET) on the real services, no order and no audit record: paper account, data validation, Timestral forecast, live prices, and a strict-schema call to the **pinned** Mistral model, whose answer must report the pinned model id (a silent alias change would otherwise go unnoticed). It is an alert, not a gate: a failure is visible and the kill switch is one variable away.
- **Caching:** the Hugging Face weights and pip downloads are cached; CPU-only PyTorch is installed from the PyTorch CPU index.

---

## D-003 — Package and environment management: pip + venv

- **Date:** 2026-11-20
- **Status:** Accepted

### Context
We need a reproducible environment that reviewers can install, and that GitHub Actions can install on every run. The deadline is tight, so tooling overhead matters.

### Options
| Option | Pros | Cons |
|---|---|---|
| pip + venv + pinned `requirements.txt` | Ships with Python, nothing extra to install; universally understood by reviewers; trivial in GitHub Actions | No lockfile for transitive dependencies; slower installs |
| uv | Very fast installs; proper lockfile | One more tool for reviewers and CI to install |
| poetry | Lockfile; project metadata | Heavier; slower; more configuration |

### Decision
pip + venv, with direct dependencies pinned to exact versions (and Timestral pinned to a git commit) in `requirements.txt`.

### Rationale
Zero extra tooling, and every reviewer can run it. Pinning direct dependencies, including the Timestral commit, gives enough reproducibility for a one-week project.

### Impact on further development
- Setup instructions are `python -m venv .venv && pip install -r requirements.txt`.
- The CI workflow uses `actions/setup-python` with pip caching.
- Transitive dependencies are not locked. Revisit (uv) if a dependency drift breaks a run.

---

## D-004 — Division of labour between Timestral and the LLM

- **Date:** 2026-11-20
- **Status:** Accepted (options C and D deferred, D is the candidate for a v2)

### Context
This is the core of the assignment: how Timestral's forecasts and the LLM combine into positions. We are judged on process, soundness and guardrails, not PnL. Our exploration showed Timestral is well calibrated (reliable uncertainty) but has no significant directional edge on daily data.

### Options
| Option | Principle | Pros | Cons |
|---|---|---|---|
| A. LLM decides everything | Forecasts go into the prompt; the LLM outputs weights | Simplest | Black box: cannot tell whether forecasts were used; hard to evaluate |
| B. Rule proposes, LLM adjusts | A deterministic rule turns forecasts into a baseline portfolio; the LLM adjusts within bounds and justifies each deviation | Measurable (rule vs rule + LLM); each component does what it is good at; deviations are traced | More code; the LLM's freedom must be tuned |
| C. Strict split | LLM picks direction (buy/avoid), Timestral sizes | Very readable roles | Rigid; under-uses the LLM (a binary switch) |
| D. LLM orchestrates Timestral as a tool | LLM calls forecasting/news tools via function calling | Most "agentic" | Non-deterministic behaviour; hard to reproduce, bound and evaluate in one week |

**Sub-question 1a — how much freedom for the LLM**
| Option | Pros | Cons |
|---|---|---|
| ±5 pts around baseline | Very safe | LLM contribution too small to measure |
| ±15 pts, symmetric | Real conviction possible | Can block a necessary large de-risking move |
| ±30 pts or more | Near-free | Collapses towards option A |
| Variable with LLM confidence | Elegant | LLM self-reported confidence is often miscalibrated and gameable |
| **Asymmetric: reducing risk is free, adding is capped at +15 pts; confidence logged and measured before being used** | Can react fully to a crisis (sell, go to cash); risk-taking stays bounded; evidence-based path to variable freedom | Needs enough decisions to evaluate calibration |

**Sub-question 1b — what the baseline rule uses**
| Option | Pros | Cons |
|---|---|---|
| **Forecast interval width only (inverse uncertainty)** | Uses what Timestral is good at; robust | Ignores forecast direction |
| Width + median tilt | Uses the whole forecast | Direction measured as no better than chance: adds noise |
| Median / width ratio | Familiar risk-adjusted return | Same dependence on unreliable direction |
| Equal weight (no Timestral) | Trivial | Timestral's role becomes invisible |

### Decision
Option B. The baseline weights are inversely proportional to Timestral's forecast interval width. The LLM may freely reduce any position or raise cash, may increase a position by at most +15 percentage points above baseline, and must justify every deviation. It also reports a confidence per position, which is logged but not used for sizing yet. Cash is an explicit choice for the LLM (minimum 10%, enforced by guardrails).

### Rationale
B is the only option that lets us answer "are the decisions sound?" with numbers: the rule, the LLM proposal and the executed portfolio are all logged, so the LLM's contribution and Timestral's contribution can each be measured. Asymmetric bounds address the main weakness of a fixed band (being stuck during a crisis): an over-cautious agent costs a little return, an over-aggressive one can cost a lot.

### Impact on further development
- The audit log stores three portfolios per run: baseline, LLM proposal, executed.
- The LLM output schema includes per-position target weight, rationale and confidence.
- Evaluation: rule vs rule + LLM; value added by LLM deviations; calibration of LLM confidence. If confidence proves calibrated, revisit 1a (variable freedom).
- Revisit 1b (width + median) once news covariates are in, if they improve directional skill.
- Forecasting, news and portfolio access are written as separate functions so they can become LLM tools in a v2 (option D), still behind the same guardrails.

---

## D-005 — Asset universe: diversified ETFs + major single stocks (15 assets)

- **Date:** 2026-11-20
- **Status:** Accepted (the universe is a configuration parameter, see below)

### Context
The universe determines what kind of decisions the LLM can make, which risks the guardrails must handle, and what news is relevant. Alpaca paper trading supports US stocks and ETFs (fractional shares), options and crypto.

### Options
| Option | Example | Pros | Cons |
|---|---|---|---|
| Diversified ETFs (~10) | SPY, QQQ, IWM, EFA, EEM, XLE, XLV, XLF, TLT, GLD | Real cross-asset trade-offs; no earnings shocks; long history | Few asset-specific news items for the LLM |
| Small universe (5–6 ETFs) | SPY, QQQ, TLT, GLD, EFA | Very readable | Few interesting decisions |
| Single large caps (~10) | AAPL, NVDA, MSFT, JPM… | Rich company news | Earnings shocks; highly correlated (tech); noisy |
| ETFs + stocks, no overlap (11) | 6 macro ETFs + 5 stocks from distinct sectors | Clean separation of macro and micro | Fewer sector instruments |
| **ETFs + stocks, full (15)** | 10 ETFs + 5 stocks from distinct sectors | Broad sector coverage; both macro and company-level news; ETF/stock pairs offer partial diversification and extra information | Some overlap (e.g. XLE/XOM): concentration risk must be handled by guardrails; longer prompt |
| Sector ETFs only | XLK, XLF, XLE, XLV, XLU… | Classic sector rotation | No bonds or gold, so no refuge in drawdowns |
| Add crypto / more asset types | BTC, ETH (Alpaca crypto) | More diversification, 24/7 news flow | Different trading calendar (24/7), extreme volatility, specific guardrails needed |

### Decision
15 assets:
- **ETFs (10):** SPY (S&P 500), QQQ (Nasdaq 100), IWM (US small caps), EFA (developed ex-US), EEM (emerging markets), XLE (energy), XLV (health care), XLF (financials), TLT (long-term US Treasuries), GLD (gold).
- **Stocks (5), one per sector:** NVDA (tech), JPM (banking), XOM (energy), LLY (health care), AMZN (consumer / e-commerce).
- **Cash** is an explicit allocation choice (see D-004).

### Rationale
- Covers many sectors and both macro-level (ETFs) and company-level (stocks) decisions, which gives the LLM real news to interpret.
- Overlapping pairs (XLE/XOM, XLV/LLY, XLF/JPM, QQQ/NVDA/AMZN) are highly but not perfectly correlated, so they still diversify somewhat, and the stock adds company-specific information. The concentration risk is handled by guardrails (stricter cap per single stock, sector-level exposure cap).
- **Diminishing returns of diversification:** portfolio variance splits into specific (idiosyncratic) risk, which shrinks roughly as 1/N, and systematic (market) risk, which does not diversify away (measured by beta in CAPM terms). Beyond about 15 to 20 distinct assets, adding more barely reduces risk, while it lengthens the prompt and dilutes the LLM's reasoning per asset.
- Other asset types are already reachable through ETFs (TLT for bonds, GLD for gold; USO/DBC/UUP exist for oil, commodities and the dollar).

### Impact on further development
- **The universe is a configuration parameter, not an architecture choice**: a list in the config. Adding an asset means adding a line.
- Guardrails must include a per-stock cap (stricter than per-ETF) and a sector exposure cap that aggregates ETF + stock in the same sector.
- Earnings dates of the 5 stocks are known in advance: candidate **known-future covariate** for Timestral (bonus).
- **Future extensions (only if time allows):** more bond exposure (e.g. IEF, LQD, HYG) and crypto. Conditions: the guardrails these assets require are in place and validated, and we have evaluated how the LLM behaves with them (e.g. its tendency to chase volatile assets).

---

## D-006 — Timestral configuration (model size, input, context)

- **Date:** 2026-11-20
- **Status:** Accepted on 2026-11-21 after the multi-asset backtest (28M, prices, 512 days). Parameters: revisit if live evidence disagrees.

### Context
Timestral comes in four sizes and accepts several input representations. Our only test so far is SPY daily data with the 28M model, 512-day context.

### Options
**Model size**
| Model | Size | Pros | Cons |
|---|---|---|---|
| 5M | 21 MB | Fastest | Least accurate; speed is not a constraint for us |
| **28M** | 111 MB | Tested: well calibrated on SPY; ~12 ms per series on CPU | Slightly less accurate than larger models |
| 123M | 490 MB | Within 0.2% of 209M per Mistral's benchmarks | Untested on our data; heavier CI cache |
| 209M | 837 MB | Most accurate | Marginal gain over 123M; heaviest |

**Input representation (4a)**
| Option | Pros | Cons |
|---|---|---|
| **Prices** | Slightly better on SPY (pinball loss, hit rate) | Tested on one asset only |
| Log returns | Scale-free, statistically cleaner | Slightly worse on SPY |
| Both (prices as target, returns as past covariate) | Gives the model "two views" of the same series | Returns are a deterministic transform of prices (r_t = p_t / p_{t-1} − 1): no new information, redundant input that may slightly disturb the model |

**Context length (4b)**
| Option | Pros | Cons |
|---|---|---|
| **512 days (~2 years)** | Tested; reflects the recent regime | Misses older crises |
| 1024–2048 days (4–8 years) | More regimes seen | May dilute recent behaviour |
| Full history (~10 years) | Maximum information | Same dilution risk, stronger |

### Decision
- Build the first pipeline with **28M** to get familiar with the system. The model name is a config parameter; the 123M weights are already downloaded so we can switch. Move to 123M if the backtest shows a gain, and consider 209M if 123M is conclusive.
- **Prices** as input (provisional), **512-day** context (provisional).
- A single representation of the price is fed as the target. "Prices + returns as covariate" is still included in the backtest, so the redundancy argument is checked with evidence rather than assumed.

### Rationale
Start with the tested, lightweight configuration to de-risk the pipeline, and let a backtest on the full 15-asset universe decide the final values rather than intuition.

On 4a, we discussed giving Timestral both prices and returns ("two pieces of information rather than one"). Returns carry exactly the same information as prices (like a distance in km and in miles), and Timestral already sees the changes when it reads prices. Covariates are valuable when they bring **information the price does not contain**: volume, VIX (market fear), news sentiment, earnings dates. So the covariate slots are kept for those. On 4b, 512 days is kept provisionally; 4 to 8 years will be tested to check whether recent market behaviour gets diluted.

### Backtest result (2026-11-21)
Script: [scripts/backtest_timestral.py](../scripts/backtest_timestral.py); raw numbers: [docs/timestral_backtest.csv](timestral_backtest.csv). 15 assets, 125 rolling origins each (2025-11-18 to 2026-11-13, every second session), so 1,875 forecasts per configuration, scored at 1 and 5 days. Baseline: empirical quantiles of the asset's own past returns over the same window, with no model.

5-day cumulative return (the horizon the baseline rule uses), price input:

| Configuration | Pinball loss | Inside the 80% interval (target 0.80) | Width-to-risk rank correlation* |
|---|---|---|---|
| Empirical, 512 days (no model) | 0.0085 | 0.788 | 0.329 |
| Empirical, 1024 / 2048 days | 0.0086 | 0.803 / 0.808 | 0.311 / 0.297 |
| **Timestral 28M, 512 days** | 0.0085 | **0.784** | 0.383 |
| Timestral 28M, 1024 / 2048 days | 0.0085 | 0.764 / 0.749 | 0.385 / 0.387 |
| Timestral 123M, 512 days | 0.0085 | 0.782 | 0.384 |
| Timestral 123M, 1024 days | 0.0085 | 0.757 | 0.389 |

\* Spearman correlation between the forecast interval width and the realised absolute return, pooled over assets and origins. This is the property the baseline rule relies on.

Findings:
- **Model size does not matter here.** 28M and 123M are indistinguishable on every metric, and the 123M is about 3 to 4 times slower. No reason to upgrade; 209M was not downloaded for the same reason (and for disk space).
- **A longer context hurts calibration.** At 1024 and 2048 days the intervals are too narrow (coverage 0.75 to 0.76 against a target of 0.80). 512 days is the best calibrated configuration, confirming the provisional value.
- **Prices or log returns: no difference** (1-day coverage 0.808 versus 0.802, same pinball loss). The 5-day return input could not be compared fairly because quantiles of a sum are not the sum of quantiles, so prices stay.
- **Where Timestral earns its place is risk ranking, not accuracy.** Pinball loss is identical to four decimals for every configuration including the no-model baseline. But its interval width ranks the realised risk better than the empirical baseline does (0.38 versus 0.33 at 5 days, 0.33 versus 0.31 at 1 day), which is what the baseline rule uses. This supports D-004.
- **No directional skill**, again: hit rates between 0.50 and 0.55.

Caveats, stated plainly: one year of origins in a single market regime; origins two sessions apart have overlapping 5-day windows, so the effective sample is much smaller than 1,875 (roughly 400 independent 5-day periods per asset-pooled estimate, i.e. a standard error of about 0.05 on a correlation). The ranking advantage is therefore **consistent in sign across every configuration but not conclusive**. The live scoring of D-013 is the cleaner, out-of-sample test.

### Impact on further development
- Model name, input type and context length are config parameters.
- Backtest to run: 28M vs 123M (vs 209M), prices vs returns, 512 vs 1024 vs 2048 days, on the 15 assets.
- Covariate slots are reserved for genuinely new information (VIX, volume, news sentiment, earnings dates), not transforms of the price.

---

## D-007 — Run frequency and timing: two differentiated runs per day; 5-day horizon

- **Date:** 2026-11-20
- **Status:** Accepted (times, horizon and no-trade band are parameters)

### Context
How often the agent wakes up determines whether it can react to news, how many decisions we can evaluate before the review, and whether Timestral receives clean data. Timestral works on daily bars; a daily bar is incomplete until the 16:00 ET close. GitHub Actions cron can start 15–30 minutes late.

The initial recommendation was one run per day. The objection raised: with a single run, the agent is blind during the whole trading session, which under-uses one of its strengths, reasoning at given moments about what just happened, not only forecasting. Overnight (close to open) and intraday (open to close) price behaviour also differ, and two runs let us observe both. Counterpoint: even one run per day reasons about the news since the previous day, so it is not "only prediction". But re-running an identical full cycle in the afternoon would feed Timestral an incomplete daily bar, and add nothing new.

### Options
| Option | Principle | Pros | Cons |
|---|---|---|---|
| 1 run per day (10:00 ET) | Forecast + decision + execution | Simple; clean data | Blind to everything happening during the session |
| 2 identical runs (10:00 and 15:30 ET) | Same full cycle twice | Twice the decisions | Afternoon forecast uses an incomplete bar or repeats the morning's; churn |
| **2 differentiated runs** | **10:00 ET:** full cycle (new Timestral forecast, decision, rebalance). **~15:00 ET:** reassessment: the LLM gets the day's news, the intraday move and the morning forecast, and decides whether to adjust | Uses the agent's reasoning on fresh events; Timestral stays on clean data; morning vs afternoon decisions can be compared | Slightly more code (a run mode); must avoid needless churn |
| Event-driven | Run when an important news item arrives | Most reactive | Needs continuous news monitoring; hard to bound and test in one week |
| 1 run per week | Rebalance weekly | Very stable | 1–2 decisions before the deadline: cannot be evaluated |

**Sub-question 3a — time of day:** opening (09:30, most volatile minutes), **~30 min after open (10:00, liquid and calm, forecasts on yesterday's complete bars)**, just before close (incomplete bar, risk of missing the close if cron is late), after close for next open (overnight gap risk, fills not visible in the same run).

**Sub-question 3b — forecast horizon:** 1 day (noisiest), **5 days (one trading week, less noise, matches the LLM's "this week" reasoning)**, 20 days (nothing verifiable before the review).

### Decision
- Two runs per trading day: **morning full cycle at ~10:00 ET** and **afternoon reassessment at ~15:00–15:15 ET** (scheduled early because of cron delays; the code checks the market is still open before acting).
- **No-trade band:** an order is only placed if the target weight differs from the current one by more than ~2 points; plus a turnover cap per run.
- **Horizon: 5 trading days** for decisions. Timestral forecasts 1 to 5 days in the same call, so the **1-day forecast is logged too**, for next-day evaluation.

### Rationale
The differentiated design keeps what each component is good at: Timestral on clean daily data, the LLM reacting to what happened during the day. It doubles the number of logged decisions for evaluation, and the no-trade band and turnover cap prevent the extra run from producing noise trading.

### Impact on further development
- `run.py` takes a mode (`morning` / `afternoon`); the afternoon run reuses the morning's forecast from the audit log.
- Two cron schedules in GitHub Actions, in UTC (currently 15:00 and 20:00 UTC; shift by one hour when US daylight saving time starts in March).
- **The horizon is a parameter with ripple effects** (baseline rule, evaluation windows, prompt). Fixed at 5 provisionally; a shorter horizon remains a candidate, especially with two runs per day.
- Evaluation can compare morning vs afternoon decisions, and overnight vs intraday returns.

---

## D-008 — Decision LLM and output contract

- **Date:** 2026-11-20
- **Status:** Accepted (model is a parameter)

### Context
The LLM interprets forecasts, portfolio and news, and adjusts the baseline portfolio (D-004). About 2 calls per trading day (D-007), so cost is not a constraint: what matters is reasoning quality and reliable structured output. Models available on our key (checked 2026-11-20): `mistral-large-4-0` (alias `mistral-large-latest`, reasoning capability, 1M context), `mistral-large-2512` (previous, no reasoning), `mistral-medium-2604` / Medium 3.5 (reasoning; `magistral-medium-latest` is now an alias of it), `mistral-small-2603`.

### Options
**Model**
| Option | Pros | Cons |
|---|---|---|
| Mistral Small | Fast, cheap | Weaker reasoning across 15 assets + news |
| Mistral Medium 3.5 | Good balance, reasoning capable | Below Large on complex trade-offs |
| **Mistral Large 4** | Strongest model, native reasoning, strict JSON schema works together with reasoning (tested) | Slightly slower (irrelevant at 2 calls per day) |
| Dedicated reasoning model (Magistral) | Explicit step-by-step reasoning | Now merged into Medium 3.5; no advantage over Large 4 |

**Output format (5a)**
| Option | Pros | Cons |
|---|---|---|
| Free text then parsing | Natural | Very fragile |
| JSON mode | Simple | Structure not guaranteed |
| **Strict JSON schema + pydantic validation** | Structure enforced by the API, re-validated in our code | Schema to maintain |
| Function calling | Same reliability; basis for option D (v2) | Heavier for a single call |

**Reasoning before deciding (5b)**
| Option | Pros | Cons |
|---|---|---|
| Direct answer | Short | Less considered, nothing to audit |
| **`analysis` as the first JSON field** (+ the model's native reasoning, logged) | Weights are generated after the analysis; full audit trail | Longer answers |

**Randomness (5c)**
| Option | Pros | Cons |
|---|---|---|
| **Low temperature (0–0.2)** | Stable, reproducible | A single "opinion" |
| Normal temperature | Variety | Unstable decisions |
| Several samples + median (self-consistency) | Robust; measures stability | 3–5× calls; more complex |

### Decision
- **Mistral Large 4, pinned as `mistral-large-4-0`** (not the `-latest` alias, which can change silently). The model ID is logged on every run.
- Native reasoning enabled; the reasoning trace (`ThinkChunk`) is stored in the audit log alongside the answer.
- Strict JSON schema enforced through `response_format`, re-validated with pydantic. Fields: `analysis` first (market reading, risks), then per asset: target weight, deviation from baseline, rationale, confidence (0–1), forecast elements used; cash weight; afternoon run: hold / adjust with reason.
- Low temperature (≈0.1) in production.

### Rationale
The agent's value is in its reasoning (interpreting news, cross-asset trade-offs), and cost is negligible at our call volume, so the strongest reasoning model is the right default. A test on 2026-11-20 confirmed Large 4 returns a reasoning trace and a schema-valid JSON in the same call (~6 s).

### Impact on further development
- Evaluation: replay the same inputs with Medium 3.5 (and different reasoning efforts) to measure whether the larger model changes decisions; run the same prompt ~5 times to measure decision stability (self-consistency as a test, not in production).
- `mistralai` 3.x: `from mistralai.client import Mistral`; reasoning comes back as `ThinkChunk` + `TextChunk` content parts.

---

## D-009 — Development guardrails

- **Date:** 2026-11-20 (extended the same day with a second round of safeguards; branch protection adopted once the repository became public)
- **Status:** Accepted

### Context
We are judged on how the agent was built, not only on how it runs. Its safety rests on the guardrail code, so we need evidence that it is correct. The agent also handles API keys and places orders, which adds two specific risks: leaking a secret, and deploying broken code that then trades.

### Options
**Round 1: code quality and tests**
| Option | What it brings |
|---|---|
| Unit tests (pytest) | Check each rule individually |
| Adversarial LLM outputs | Deliberately bad responses (200% NVDA, negative weight, invented ticker, broken JSON) must never pass |
| Property-based tests (hypothesis) | Thousands of random proposals: the final portfolio must always satisfy every constraint |
| Dry-run mode | Full cycle without sending orders |
| CI checks | Lint, types and tests on every push |
| Ruff (linter + formatter) | Catches likely bugs and style issues |
| Mypy (strict static typing) | Catches type errors before runtime; pairs well with pydantic |

**Round 2: safety-critical safeguards**
| Option | What it brings | Cost | Chosen |
|---|---|---|---|
| Secret detection (GitHub push protection + gitleaks in CI and pre-commit) | Blocks any commit/push containing an API key | Low | Yes |
| Independent final check before sending orders | A separate, deliberately simple function re-checks the core invariants (long-only, no leverage, cash floor, universe); any violation aborts the run. Defence in depth against a bug in `guardrails.py` | Low | Yes |
| 100% test coverage on `guardrails.py` (pytest-cov) | No line of the critical module escapes the tests | Low | Yes |
| Offline tests (fake Alpaca and Mistral clients, recorded responses) | Fast, reproducible, free; tests can never place an order; CI needs no secrets | Medium | Yes (see rationale) |
| Daily real smoke test (full cycle in dry-run on the real APIs before the morning run) | Detects external breakage (e.g. the `mistralai` 3.x import change) before trading, not during | Low | Yes (see rationale) |
| Trading gated on CI | The trading workflow only runs if the latest CI on `main` is green | Low | Yes |
| LLM behaviour scenarios (5–10 fixed situations with an expected behaviour: simulated crash → de-risk; huge intervals → lower confidence; contradictory news → flagged) | Unit tests for the LLM's judgement; direct evidence that decisions are sound. Run manually and on prompt changes, not on every commit (API cost, non-determinism) | Medium | Yes |
| Versioned prompt (prompt file in the repo, version logged on every run) | Behaviour changes can be attributed to prompt, model or data | Low | Yes |
| Branch protection + pull requests on `main` (CI must pass before merge) | Team-standard workflow; nothing reaches `main` untested | Friction for a solo developer; on a free GitHub plan, requires a public repository | Yes (first deferred, adopted once the repository became public) |
| `pip-audit` (known vulnerabilities in dependencies) | Good "client" practice | Low risk for a one-week, non-exposed project | No (deferred) |
| Pre-commit hooks for ruff/mypy | Errors caught before CI | Friction; CI already does it | Only gitleaks in pre-commit |

### Decision
Everything in round 1, and in round 2 everything except `pip-audit`. Pre-commit is used only for secret detection.

### Rationale
- Adversarial and property-based tests, 100% coverage on the guardrails and an independent final check together give strong evidence that no LLM output can produce a forbidden portfolio.
- Secret leakage is the most likely serious incident on this kind of project, and the cheapest to prevent.
- **Offline tests** were judged essential: without them, tests would call the real APIs (cost, flakiness, risk of a real order from a test) and CI would need the secrets.
- **The daily smoke test** was judged essential: external APIs have already changed under us once (the `mistralai` 3.x client import), and the agent runs unattended; a break must be detected before the 10:00 ET run.
- LLM behaviour scenarios are the most direct answer to "how do you know its decisions are sound?".
- Branch protection was first deferred (solo developer, private repository, which on a free plan cannot be protected). Once the repository was made public for reviewers, it became free to enable, and it is the practice a client would expect: nothing reaches `main`, and therefore nothing trades, without green CI. Rule set on `main`: pull request required (0 approvals, since a solo author cannot approve their own PR), status checks `checks` and `secrets` required, force pushes and deletion blocked.

### Impact on further development
- Dev dependencies: `ruff`, `mypy`, `hypothesis`, `pytest-cov`, `pre-commit`.
- Work happens on branches and reaches `main` through pull requests with green CI.
- External clients (Alpaca, Mistral) are injected behind small interfaces so tests can swap in fakes.
- Workflows: `ci.yml` (lint, types, tests, coverage, gitleaks), `smoke.yml` (daily dry-run on real APIs), `trade.yml` (gated on CI).
- The prompt lives in a versioned file; scenario evals live in `evals/`.

---

## D-010 — Runtime guardrails

- **Date:** 2026-11-20
- **Status:** Accepted (thresholds are parameters; fallback on rejection is provisional)

### Context
The LLM proposes a portfolio (D-004, D-008); deterministic code decides what actually reaches the broker. The Alpaca paper account allows margin (2× buying power) and shorting by default, so every restriction must be enforced in our code.

### Architecture: four layers
1. **LLM output validity:** schema, assets within the universe, numeric weights, weights + cash sum to 100%.
2. **Portfolio risk rules:** long-only; no leverage (invested ≤ 90%); cash ≥ 10%; ≤ 25% per ETF; ≤ 10% per single stock; ≤ 30% per sector (ETF + stock aggregated); asymmetric bound vs baseline (free decrease, increase ≤ +15 pts).
3. **Order sanity:** no-trade band (2 pts), turnover cap per run (≈30%), market open and not in the first minutes, idempotent client order IDs.
4. **System safety:** paper-account check, kill switch, fresh data check, forecast sanity (no NaN, no absurd interval).

### Implementation priority
Layers 1 and 2 first. From layers 3 and 4, four items are required **before the first real order**, because each costs a few lines and its absence can cause real damage:

| Priority | Item | Failure it prevents |
|---|---|---|
| P0 | Paper-account check | A configuration mistake trading real money |
| P0 | Market-open check | Orders queued while closed and filled later at an unexpected price |
| P0 | Idempotent client order IDs | A re-run (e.g. GitHub retry) buying twice |
| P0 | Kill switch (repository variable) | Having to edit code in an emergency to stop the agent |
| P1 | No-trade band, turnover cap, fresh-data and forecast sanity checks | Churn, trading on stale or broken inputs |

### Handling violations
**Options**
| Option | Pros | Cons |
|---|---|---|
| Reject everything, do nothing | Very cautious | A trivial excess (26% vs 25%) blocks the whole decision |
| Correct silently | Agent keeps going | Hides problems: unacceptable for audit |
| **Correct and log each correction; reject when severe** | Proportionate; every correction is traceable | Must define "severe" |

**Severe → reject the whole proposal**
| Case | Why |
|---|---|
| Invalid JSON, even after one retry | Nothing usable |
| Asset outside the universe | Hallucination: the rest of the answer cannot be trusted |
| Missing or non-numeric value | Same |
| Weights + cash outside 95–105% | Calculation error or misunderstood instructions |
| Too many corrections in one answer (> 3 assets or > 20 pts corrected in total) | The rules were not understood |
| Deviation from baseline without justification | Justification is mandatory (D-004) |

**Correctable → fix and log**
| Case | Correction |
|---|---|
| Weights + cash within 95–105% | Renormalise |
| Above per-asset or per-sector cap | Clip to the cap |
| Increase above baseline + 15 pts | Clip to +15 pts |
| Cash below 10% | Scale positions down proportionally |
| Negative weight (attempted short) | Set to 0 and flag |

The number of corrections per run is a monitored metric.

**Fallback when rejected**
| Option | Pros | Cons |
|---|---|---|
| **Keep current positions** (after one LLM retry) | "First, do no harm"; simplest | Day one, the portfolio stays 100% cash |
| Apply the baseline portfolio | Always a reasonable decision | Trades without LLM validation, silently changing the agent's nature |
| Liquidate to cash | Zero risk | Costly round trips caused by a formatting error. Ruled out. |

Chosen provisionally: keep current positions. "Apply baseline" and "reject and do nothing" remain under consideration.

### Order type
| Option | Pros | Cons |
|---|---|---|
| Market order | Always filled | Price not guaranteed |
| Limit order at current price | Price controlled | May never fill |
| **Marketable limit** (limit slightly beyond the current price, e.g. ±0.3%) | Fills immediately like a market order, but protected if the price jumps; a single order | Unfilled if the price moves more than the tolerance (cancelled and logged; the next run rebalances) |
| Escalation: limit, then market after N minutes | Best price | Run must wait and poll; more code and test cases |

Chosen: marketable limit. To verify at implementation: Alpaca's support for limit orders on fractional quantities (otherwise round to whole shares).

### Drawdown circuit breaker
| Option | Pros | Cons |
|---|---|---|
| Single "reduce-only" mode | Simple | Only one level of response |
| **Graduated levels** | Proportionate; each level testable | Thresholds to choose |
| Full stop (all cash) | Maximum protection | Sells at the worst moment |
| Volatility targeting with Timestral (preventive) | Acts before losses; uses Timestral for what it does well | Not a circuit breaker strictly; more code |

Chosen: graduated levels, measured from the portfolio's peak equity:
- **−3%:** alert; the LLM is told the portfolio is in drawdown.
- **−5%:** reduce-only (the LLM can no longer increase positions).
- **−8%:** gross exposure capped at 60%.
- **−12%:** revert to the baseline portfolio at half exposure; LLM paused until a human re-enables it.

Volatility targeting with Timestral is kept as a strong candidate: it would strengthen Timestral's role (anticipating losses rather than reacting).

### Impact on further development
- `guardrails.py` returns the corrected portfolio **and** a structured list of corrections or the rejection reason; both go into the audit log.
- Thresholds live in the config.
- A diversified 15-asset portfolio rarely loses 5% in one or two weeks, so the circuit breaker is validated with simulated drawdowns in tests rather than waiting for a live trigger.

### Implementation notes (execution, 2026-11-20)
- **Fractional limit orders are accepted** by Alpaca paper (verified: a 0.5-share limit order was accepted, then cancelled with no fill). They must be day orders; limit prices are rounded to the cent. The reference price comes from the IEX feed (the free plan refuses recent SIP data).
- **Write-ahead audit:** the run record, including the planned orders, is written before any order is sent; submission results go to `reconciliation/<run_id>.json`. The agent can never act without a record, and a crash in between is detectable (a record with no submissions).
- **Kill switch:** live orders need the `--live` flag **and** `TRADING_ENABLED=true`; otherwise the run is recorded as `dry_run_kill_switch`. Idempotency: a run id already in the audit log is skipped, and client order ids are `<run_id>-<symbol>-<side>`.
- **Sells before buys, and a failed sell cancels the buys:** without the proceeds of the sell, the buys could only be funded with margin, i.e. leverage.
- **No-trade band relaxed when needed:** if skipping small moves would leave an invariant broken (for example a position drifting above its cap), the band is dropped for that run; if no plan is safe, the run aborts.
- **Turnover cap and the initial deployment (revised 2026-11-22 with the project owner):** the 30% cap would have throttled the first deployment from 100% cash to three runs, i.e. the portfolio fully invested only on the second trading day out of three before submission. The cap exists to brake sudden *changes* of an invested portfolio, not the entry. It is therefore **waived once, when the portfolio is at most 5% invested** (`initial_deployment_max_invested`); every later run is capped at 30%. Options considered: keep 30% everywhere (one day of data lost), 50% everywhere (a looser brake for the whole test), the one-off waiver (chosen). The guardrails and the final check still apply in full to the target and to the planned portfolio.
- **Test runs without publication:** a manual dispatch of the trade workflow can untick `publish`, so a test leaves no record in the public audit log, which must reflect only the agent's real activity.
- Positions outside the universe, an unexpected short or leverage in the account stop the run.

---

## D-011 — Audit log: format, storage and immutability

- **Date:** 2026-11-20
- **Status:** Accepted (file names and the split between files are parameters)

### Context
"How do we know the decisions are sound?" requires a record of everything the agent saw and did: inputs, Timestral forecasts, the three portfolios (baseline, LLM proposal, executed), the full prompt and answer, the reasoning trace, guardrail corrections, orders and fills. A live record is about 60 KB (prompt 20 KB, reasoning 24 KB, answer 6 KB); at two runs a day for about 20 trading days that is about 2.5 MB, which Git handles easily. The log also feeds the dashboard and the read-only export for reviewers.

### Options
**Storage format**
| Option | Pros | Cons |
|---|---|---|
| A. One JSON file per run | Readable directly on GitHub; no write conflicts between runs; file name doubles as an idempotency lock; readable diffs | Cross-run statistics require loading every file |
| B. A single JSONL file | Simple to stream | One 60 KB line per run is unreadable; one growing file; conflicts if runs overlap |
| C. SQLite database in the repository | Easy to query | Binary: invisible on GitHub, no readable diffs, unmergeable; reviewers must download it |
| D. CSV or Parquet only | Ideal for analysis and charts | Nested data (prompt, reasoning, attempts) fits badly |
| **E. Hybrid: A as source of truth + derived flat tables** | Complete and readable (JSON), easy to analyse (CSV); the CSVs are directly the "export" the assignment asks for; tables can always be regenerated | One more small script to maintain and test |

**Where to store it**
| Option | Pros | Cons |
|---|---|---|
| Commit to `main` | Code and log together | Blocked by the branch protection of D-009; bypassing it would weaken "nothing trades without green CI" |
| **Dedicated `audit-log` branch** | No bypass: the code stays protected while runs commit the log automatically; visible, immutable history; readable on GitHub | One more branch to explain; the log is not on the default branch |
| GitHub Actions artifacts | No repository writes | Deleted after 90 days; not browsable without signing in: useless as a read-only view for reviewers |
| External service (hosted database, object storage) | Robust in theory | An extra account, keys and service; nothing left to read in the repository |

**Fills (order results arrive after the decision)**
| Option | Pros | Cons |
|---|---|---|
| Rewrite the run file once results are known | One complete file | The log is no longer immutable: the proof of what was decided at time T is lost |
| **Immutable run records + separate reconciliation files** | The decision record is never modified; later runs write fills, price slippage and forecast-vs-realised comparisons to `reconciliation/` | Analysis must join the two kinds of files (the derived tables do it) |

### Decision
- **Format E.** One JSON file per run (`runs/YYYY-MM-DD_morning.json`, `..._afternoon.json`) is the source of truth. Flat CSV tables are derived from it by a script: `decisions.csv` (one row per run), `weights.csv` (one row per run and asset: current, baseline, LLM proposal, final, confidence, rationale), `forecasts.csv` (quantiles per run, asset and horizon) and `orders.csv`.
- **Storage:** a dedicated `audit-log` branch.
- **Immutability:** a run record is written once and never modified. Writing an existing run is an error, which also makes a re-run (for example a GitHub retry) detectable. Fills and realised outcomes go to separate `reconciliation/YYYY-MM-DD.json` files written by later runs.
- **Reasoning trace** in a sibling file (`..._reasoning.txt`, referenced by the run record) so the JSON that tools read stays light. The reasoning file is written first and the JSON last, so the JSON's existence means the run is complete.
- Each record carries a `schema_version`, the code version (git commit), a snapshot of the configuration and the prompt version.

### Rationale
Immutability is the strongest argument for the "are decisions sound" question: the record of what the agent saw and decided cannot be edited afterwards, and its outcome is appended later. The hybrid format serves both audiences, a complete audit trail (JSON) and quick reading and export (CSV). The dedicated branch is the consequence of adopting branch protection: the code path stays protected, the data path is automated.

### Impact on further development
- `audit.py` builds run records from the pipeline objects, writes them atomically without overwriting, and derives the tables. Tests use a temporary directory, no network.
- Workflows check out the `audit-log` branch into a folder, run the agent against it and push the new files. The dashboard reads the derived tables from that branch.
- No secret may ever enter a record (gitleaks also scans the branch history).

### Implementation notes (reconciliation, 2026-11-20)
- Order records carry the **reference price** the limit was derived from. `reconcile.py` looks every sent order up by its client order id and, once **all** orders of a run have reached a final state (day orders end at the close), writes `reconciliation/<run_id>_fills.json` once: final status, filled quantity, average fill price, **slippage in basis points** against the reference price (positive always means worse for us) and a summary (filled, partial, unfilled, not sent, not found, notional-weighted slippage). Orders that never reached the broker (a failed sell cancels the buys) are marked `not_sent`.
- It is **idempotent and append-only**: runs already reconciled are skipped, runs with open orders wait for a later pass, and a failing lookup is reported without blocking the other runs. The trade workflow runs it after each cycle (also after a failed one), so the morning orders are reconciled by the afternoon run and the afternoon orders by the next morning's.
- A `fills.csv` table is derived from these files for the dashboard and the export.
- The record and the reconciliation files are public, like the repository: the prompt and reasoning are exactly what reviewers want to examine.

---

## D-012 — Dashboard: a static page generated from the audit log, published on GitHub Pages

- **Date:** 2026-11-21
- **Status:** Accepted. Taken autonomously on 2026-11-21 while the project owner was away, then **confirmed by the owner on 2026-11-22** after reviewing the pros and cons, who judged the static page sufficient as it is. The dashboard is a replaceable layer: all its data comes from the derived CSV tables, so a different front end could be built on them without touching the agent.

### Context
The assignment asks for a read-only view of the trading activity (export or dashboard). It must be viewable by reviewers without installing anything, must always agree with the audit log, and must not become a second system to operate.

### Options
| Option | Pros | Cons |
|---|---|---|
| **A. Static HTML page generated from the audit log, deployed to GitHub Pages** | No server and nothing to install; the page is a pure function of the immutable records, so it cannot drift from them; free; one public URL; testable like any other code; CSV export beside it | Updated after each trade run, not live; a single generated page rather than an app with filters |
| B. Streamlit app | Rich interactivity, quick to write | Needs a running server (Streamlit Community Cloud or similar): an extra service, account and failure mode; sleeps when idle; not readable without it |
| C. Notebook or Quarto report | Good for analysis and narrative | Needs execution to refresh; the evaluation write-up, not a monitoring view |
| D. Markdown report committed to the `audit-log` branch | Zero infrastructure, readable on GitHub | No charts of any quality; no hover, no responsive layout |
| E. Hosted BI tool (Grafana, Metabase, Evidence) | Powerful | An external service and credentials for a one-week project; data would have to be pushed out of the repository |
| F. Interactive single-page app in JavaScript with the data embedded | Filters, client-side charts | A front-end build and dependencies to test; charts with no JavaScript fallback; heavier than the content needs |

**Charts: server-rendered SVG with a ~20-line tooltip script** (chosen) versus a charting library from a CDN (an external dependency that can fail or change, and breaks the "self-contained" property) versus Canvas (not accessible, not testable as text).

### Decision
Option A. `agent.dashboard` builds one self-contained `index.html` (inline CSS, server-rendered SVG, a short script for tooltips; no external resource at all) plus the derived CSV tables for download. The `Dashboard` workflow rebuilds and deploys it to GitHub Pages whenever the `Trade` workflow completes (a push made with the workflow token does not trigger other workflows, so it listens to the end of `Trade`, which also covers reconciliation) and when the dashboard code changes.

Content: portfolio value (hero figure and a line chart with its table view), headline counters (runs, guardrail corrections, rejected proposals, average slippage), the target allocation against the Timestral baseline, the Timestral 5-day forecast intervals sorted by uncertainty, the latest decision with the LLM's analysis and any guardrail corrections, all runs linked to their immutable records, executions with fills and slippage, and the CSV export.

### Rationale
The page is derived data: the audit log stays the single source of truth, and the dashboard can be regenerated or thrown away. Pages gives reviewers a URL and nothing to run. Server-rendered SVG keeps the charts testable (the tests assert on the output) and readable with scripts disabled. Hover details are a progressive enhancement and every value is also reachable in a table view. All text that comes from the data, notably the LLM's analysis and rationales, is HTML-escaped because LLM output is untrusted; a test injects markup and checks it is displayed, never executed.

Design checks done on the rendered page (headless Chrome, light, dark and a 390 px viewport): this caught a real bug (an interval wider than the axis overflowed its card), fixed and covered by a test. On phones the charts keep a minimum width and scroll instead of shrinking their text.

### Impact on further development
- The dashboard reads only what the audit module writes: any new section needs its data in the records first.
- Two sections are intentionally absent until their data exists: forecast calibration against realised prices, and the evaluation of the LLM's stated confidence (see Open decisions).
- Enabling GitHub Pages is a repository setting: the site is public, like the repository and the audit log.
- Revisit if reviewers need live updates or filtering (options B or F).

---

## D-013 — Forecast scoring: record observed closes as facts, derive the scores

- **Date:** 2026-11-21
- **Status:** Accepted. Taken autonomously on 2026-11-21, then **confirmed by the owner on 2026-11-22**: record the observed closes, do not query the broker on demand. A hybrid was discussed (see the last row of the options table) and kept as an optional improvement.

### Context
To know whether Timestral is useful live, each forecast must be compared with the prices that followed: how often the outcome falls inside the 80% interval, and how the median did. Forecasts are made at 10:00 ET from completed sessions, and a horizon of up to 5 sessions means the outcome is only known days later. The audit log is immutable (D-011), so the scores need a home that respects that.

### Options
| Option | Pros | Cons |
|---|---|---|
| Fetch prices from Alpaca when scoring (dashboard build, reconcile job) | No new files | The dashboard and the export would depend on the network and the API keys; prices can be re-adjusted later, so the same score could change |
| Write a score file per run once all 5 horizons are known | Simple, immutable | Nothing visible for 5 sessions, past the submission deadline; or up to 10 small files a day if written per horizon |
| Store scores as events in the log | Immutable | Scores are derived data: storing them freezes any bug in the scoring code |
| **Record observed closes as immutable facts, derive the scores** | Facts are recorded once and never change; scores are a pure function of run records plus observations, regenerated at any time, testable offline; the 1-day outcomes show up the next day | One more file type (`observations/`) |
| Hybrid: record, plus an automatic check that compares each new window with the previous one to flag a dividend or split re-basing (no extra API call) | Makes the re-basing risk visible instead of hidden, at no extra cost in calls | About 20 lines and their tests; does not change the scores (they already use one basis per observation). **Optional, not scheduled** |

**Price basis.** Alpaca's adjusted closes are re-based when a dividend or split occurs, so closes recorded on different days can sit on different bases. Each observation therefore holds a **window of the last 12 sessions taken at one time**; a return is only computed between two dates of the same file. The earliest observation containing both dates is used, which keeps the choice deterministic.

### Decision
- `observations/YYYY-MM-DD.json`: written once per completed session by the trade workflow (a no-op if the day is already recorded). The unfinished bar of the current session is never recorded.
- `score_forecasts` joins run forecasts (q10, median, q90 at horizons 1 to 5) with the observations and yields one row per (run, asset, horizon) whose outcome is known: realised return, inside the 80% interval, direction hit (undefined if the median or the move is exactly zero), absolute error of the median.
- Derived into `tables/forecast_scores.csv`, and into a **live calibration** section on the dashboard: per horizon, the share of outcomes inside the 80% interval against the 80% target, with the number of scored forecasts shown first.

### Rationale
The log stays append-only and the scores can never disagree with the facts they come from. The dashboard needs no keys or network. The page states that early figures are noisy and that one day's 15 forecasts are strongly correlated (assets move together), so the effective sample is much smaller than the count.

### Impact on further development
- Scores appear from the second trading day (1-day horizon) and complete a run after 5 sessions.
- The same join is the basis for the evaluation of the LLM's contribution: the baseline, the LLM proposal and the final portfolio can be valued against these observed closes.
- A stored pinball loss would need the full 21 quantiles in the run record (today only q10, median and q90 are kept); revisit if the evaluation needs it.

---

## D-014 — Performance metrics: shadow portfolios and forecast portfolio risk

- **Date:** 2026-11-22
- **Status:** Accepted with the project owner (principle agreed 2026-11-22); implemented in `metrics.py` and `risk.py`

### Context
"How do we know the decisions are sound" needs robust measures, and the owner asked for them explicitly. Two questions matter beyond raw performance: **what does each layer add** (Timestral, the LLM, the guardrails), and **does Timestral's risk forecast hold** at the portfolio level. Timestral forecasts each asset separately: its quantiles are marginal distributions, so it cannot give a covariance matrix on its own. The live window is short, so every figure must say how much evidence stands behind it.

### Options
**Measuring the contribution of each layer**
| Option | Pros | Cons |
|---|---|---|
| Account performance only | Real (paper) money | One number mixing everything: cannot tell what the LLM or Timestral added |
| Separate backtests per variant | Clean comparison | Needs a replay engine for each variant; not live |
| **Shadow portfolios from the audit log** | Every record already holds baseline, LLM proposal and final; valued with observed closes they isolate each layer at no extra cost, live and in the replay | Counterfactual valuation ignores costs and slippage (reported separately from the account) |

**Portfolio-level forecast risk**
| Option | Pros | Cons |
|---|---|---|
| Historical covariance only | Simple | Ignores Timestral entirely |
| Multivariate Timestral run | One model | Timestral outputs marginal quantiles, not a joint distribution: no covariance to read |
| **Timestral volatilities x shrunk historical correlation** | Standard construction; Timestral supplies what it does well (forward-looking uncertainty per asset), history supplies the dependence | Normal approximation to turn an 80% interval into a standard deviation; correlation assumed stable over the horizon |

**Robustness**
| Option | Pros | Cons |
|---|---|---|
| Point estimates only | Simple | Over-claims on a few days of data |
| i.i.d. bootstrap | Standard | Ignores the dependence between consecutive days |
| **Moving-block bootstrap (blocks of 5 sessions), from 10 sessions on; nothing claimed below** | Respects serial dependence; honest about small samples | Wide intervals early on, which is the truth |

### Decision
- **Shadow portfolios** valued daily from the observed closes of D-013: `agent` (final target), `llm_proposal` (before guardrails; the final when the proposal was rejected), `baseline` (Timestral rule), `equal_weight` (6% per asset, same invested share), `spy`. Comparisons: Timestral rule vs equal weight, LLM vs rule, guardrails (final vs proposal), agent vs SPY; each with its number of sessions, cumulative and mean daily difference, and a 95% block-bootstrap interval, flagged *conclusive* only when the interval excludes zero. The real account return is reported separately.
- **Forecast risk** (`risk.py`): sigma = (q90 - q10) / 2.563 from Timestral, correlation of daily log returns over 252 sessions shrunk 20% towards the identity; portfolio volatility at 1 and 5 days for every portfolio and each asset's share of the risk, **logged in every run record**. Risk calibration: realised portfolio move over the horizon divided by the forecast volatility; about 80% of outcomes should fall within +/-1.28, and the root mean square of these ratios should be close to 1.
- **LLM confidence calibration:** each deviation above 0.5 pt with its stated confidence; it "paid off" when the tilt times the asset's realised 5-day return is positive. Brier score (0 is perfect, always answering 0.5 scores 0.25) and reliability by confidence band.
- Timing approximation, stated in the code: a session's return is earned by the latest run before it, or by that day's morning run.

### Rationale
Shadow portfolios answer the question the evaluation actually asks (is each component useful?) with data already recorded, and serve the live run and the replay (D-015) identically. The risk construction is the honest way to get a covariance from a model that forecasts marginals, and it prepares Timestral as a tool the LLM can call later (D-004 option D). Intervals and minimum sample sizes stop a few lucky days from being read as skill.

### Impact on further development
- `tables/shadow_portfolios.csv` is derived with the other tables; the dashboard (T-04, dashboard redesign) shows these metrics first.
- The replay (T-05) reuses the same functions.
- In a later version `portfolio_vol` and `risk_contributions` become tools the LLM can call before answering.

---

## D-015 — Historical replay of the agent (test case 1)

- **Date:** 2026-11-22
- **Status:** Accepted with the project owner

### Context
The live window before submission is about three trading days: far too few decisions to judge the agent statistically. A replay over past data gives a larger sample, but carries one specific risk: **look-ahead bias**. Mistral Large 4 was trained on data that very likely includes 2024-2025 markets; shown real names and dates, it may "remember" what happened next, and the replay would flatter it without anyone noticing. Mistral API cost is not a constraint (roadmap budget check: about one cent per decision on the sponsored plan's free tier); time is, because decisions are sequential (each depends on the portfolio left by the previous one) and a call takes 2 to 5 minutes.

### Options
**a) Test period**
| Option | Pros | Cons |
|---|---|---|
| **Most recent months (2026)** | Least likely to be in the LLM's training data | A single market regime |
| Part of 2025 (initial idea) | More history before it | The LLM very likely saw 2025 |
| Both, compared | The gap between them measures the leakage itself | Twice as long |

**b) Decision frequency**
| Option | Decisions | Duration |
|---|---|---|
| **Daily over 3 months** | ~63 | ~2.5 h |
| Daily over 6 months | ~125 | ~5 h |
| Weekly over 6 months | ~26 | ~1 h, too few decisions to judge the LLM |

**c) Role of the earlier period** (the LLM is not retrained)
| Option | Pros | Cons |
|---|---|---|
| **Parameters fixed beforehand, same prompt as live** | The replay tests exactly the agent that trades live | The LLM "learns" nothing from the period |
| A "lessons" note from 2024-2025 added to the prompt | The LLM benefits from the past | Another prompt, hence another agent than the live one |
| Past decisions with their outcomes as examples | Concrete | Very long prompt; risk of overfitting |

### Decision
- **Test period:** the ~63 most recent sessions whose outcomes are fully known (about mid-August to mid-November 2026), one decision per session.
- **Same agent as live:** prompt `decide_v1`, Mistral Large 4 pinned, guardrails and parameters as in D-004 to D-010, frozen. The earlier period (2024-2026 before each decision date) feeds Timestral's 512-session context and the correlation window, exactly as live.
- The "lessons note" variant is optional, only if time allows, and would be reported as a different agent.

### Rules of the replay (rights and actions differ from live)
| | Replay (test case 1) | Live (test case 2) |
|---|---|---|
| Data | **Point-in-time, enforced in code**: a decision at session t only receives data up to t-1 | Alpaca, completed sessions |
| What the LLM sees | **Anonymised**: assets renamed (type and sector group kept), prices rebased, no dates, no news | Real names and dates |
| Leakage probe | The LLM is asked to identify assets and period from a payload; answers recorded | Not applicable |
| Execution | **Simulated**, at the next open plus a stated cost assumption; **no broker access at all** | Alpaca paper, marketable limit orders |
| Runs per day | One (the morning decision) | Two (morning and afternoon) |
| Comparison | Shadow portfolios of D-014: baseline alone, LLM proposal, final, equal weight, SPY | Same, live |
| Log | Separate experiment output in the repository (`docs/replay/`), never the `audit-log` branch | Immutable audit log |
| Kill switch | Not applicable | Yes |

### Rationale
Recent data minimises look-ahead bias, and anonymisation plus a leakage probe make the remaining risk visible rather than assumed away. Keeping the agent identical to the live one means the replay and the live run measure the same thing, so their results can be read together. Three months of daily decisions balances sample size (63 decisions, 63 sessions of returns, enough for the bootstrap intervals of D-014) against a duration that fits before submission.

### Impact on further development
- Caveat to state in the results: the Timestral configuration (D-006) was evaluated on origins that overlap the test period. Its choice did not change from the value fixed before the backtest, so selection bias is minimal, but it is not zero.
- Reuses the metrics of D-014 (T-03), so T-03 comes first.
- The afternoon reassessment run is not replayed (it would need intraday data).

---

## Open decisions

- Fallback when the LLM proposal is rejected: keep positions (provisional) vs apply baseline vs do nothing (D-010)
- News source and covariate construction (bonus)

---

## Future work: with more time or in a longer project

Options deliberately set aside for this one-week assignment. Each would make sense in a longer-lived project, a team setting or a production deployment.

| Item | From | Why deferred | When it would make sense |
|---|---|---|---|
| LLM orchestrating Timestral and news as tools (function calling) | D-004 option D | Hard to bound, reproduce and evaluate in one week | Once the v1 pipeline and its evaluation are stable; the modules are already written as separate functions to allow it |
| Variable LLM freedom based on its confidence | D-004 (1a) | LLM self-reported confidence is unproven | If the logged confidence proves calibrated against outcomes |
| Baseline rule using forecast direction (width + median) | D-004 (1b) | No measured directional skill | If news covariates improve Timestral's directional accuracy |
| Volatility targeting with Timestral forecasts | D-010 | More code; the graduated circuit breaker covers the urgent need | Natural next step: makes Timestral act before losses rather than after |
| More bond exposure (IEF, LQD, HYG) and crypto | D-005 | Each needs specific guardrails, and crypto a 24/7 calendar | Once the guardrails are validated and the LLM's behaviour with volatile assets is evaluated |
| Larger Timestral models (123M, 209M) | D-006 | Start with the tested 28M | If the multi-ticker backtest shows a gain |
| Event-driven runs (triggered by news) | D-007 | Requires continuous news monitoring | With a streaming news source and a longer evaluation period |
| `pip-audit` (dependency vulnerability scanning) | D-009 | Low exposure for a short, non-public project | Any production or client deployment |
| Pre-commit hooks for ruff and mypy | D-009 | CI already runs them | Larger team, to catch issues before CI |
