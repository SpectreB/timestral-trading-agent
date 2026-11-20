# Audit log

This branch holds the **read-only audit trail** of the paper-trading agent. It is written automatically by the agent workflows; the code lives on `main`. See `docs/decisions.md` (D-011) on `main` for the design.

```
runs/YYYY-MM-DD_<mode>.json            one immutable record per run: inputs, Timestral forecasts,
                                       baseline / LLM / executed portfolios, prompt, answer,
                                       guardrail corrections, orders
runs/YYYY-MM-DD_<mode>_reasoning.txt   the LLM reasoning trace of that run
reconciliation/YYYY-MM-DD.json         fills and realised outcomes, appended by later runs
tables/*.csv                           flat tables derived from the records (decisions, weights,
                                       forecasts, orders), regenerable at any time
```

Records are never modified after they are written.

