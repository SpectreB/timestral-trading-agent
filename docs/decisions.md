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

## Open decisions

- Package and environment management (pip+venv vs uv vs poetry)
- Trading frequency and forecast horizon
- Asset universe
- Timestral model size (5M / 28M / 123M / 209M)
- LLM model and decision output schema
- Risk guardrails
- Audit log storage format
- Dashboard technology
- News source and covariate construction (bonus)
