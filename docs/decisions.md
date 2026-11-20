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
- The audit log must be committed back to the repo (or a dedicated branch) so the dashboard can be built from it.
- Hugging Face weights should be cached with `actions/cache`.
- Revisit if runtime or reliability on the runner proves insufficient.

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
| Both (one as target, the other as covariate) | — | Returns are a deterministic transform of prices: no new information, redundant input |

**Context length (4b)**
| Option | Pros | Cons |
|---|---|---|
| **512 days (~2 years)** | Tested; reflects the recent regime | Misses older crises |
| 1024–2048 days (4–8 years) | More regimes seen | May dilute recent behaviour |
| Full history (~10 years) | Maximum information | Same dilution risk, stronger |

### Decision
- Build the first pipeline with **28M** to get familiar with the system. The model name is a config parameter; the 123M weights are already downloaded so we can switch. Move to 123M if the backtest shows a gain, and consider 209M if 123M is conclusive.
- **Prices** as input (provisional), **512-day** context (provisional).

### Rationale
Start with the tested, lightweight configuration to de-risk the pipeline, and let a backtest on the full 15-asset universe decide the final values rather than intuition.

### Impact on further development
- Model name, input type and context length are config parameters.
- Backtest to run: 28M vs 123M (vs 209M), prices vs returns, 512 vs 1024 vs 2048 days, on the 15 assets.
- Covariate slots are reserved for genuinely new information (VIX, volume, news sentiment, earnings dates), not transforms of the price.

---

## Open decisions

- Trading frequency and forecast horizon
- Asset universe
- Timestral model size (5M / 28M / 123M / 209M)
- LLM model and decision output schema
- Risk guardrails
- Audit log storage format
- Dashboard technology
- News source and covariate construction (bonus)
