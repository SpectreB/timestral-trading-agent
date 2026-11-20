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
- **Status:** Provisional (values are parameters, to be confirmed by a multi-ticker backtest)

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
- **Consequence of the turnover cap:** the first deployment from 100% cash is throttled to 30% of equity per run, so reaching the target takes about three runs. The cap is a parameter.
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

## Open decisions

- Fallback when the LLM proposal is rejected: keep positions (provisional) vs apply baseline vs do nothing (D-010)
- Dashboard technology
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
