# Timestral: technical notes

Sources: <https://mistral.ai/news/timestral/> and <https://github.com/mistralai/timestral> (README), read on 2026-11-19.

## Model

- Four sizes, all sharing the same architecture: `mistralai/Timestral-5M` (21 MB), `-28M` (111 MB), `-123M` (490 MB), `-209M` (837 MB), on the Hugging Face Hub.
- Licenses: code is Apache 2.0; weights are Modified MIT.
- **Architecture:** Decoupled Horizon Forecasting (learned forecast tokens read the full history) and covariate-aware attention (each series has a role, and information flows only from covariates to targets).
- **Training:** mostly synthetic data. Pre-trained at context 8192, then post-trained at 4x longer context.
- **Context:** up to 32,768 steps (the most recent steps are kept).
- **Blog CPU benchmark** (8 threads, one day ahead from three weeks of 15-minute data): 17 ms (5M) to 113 ms (209M). The 123M model is within 0.2% of 209M's accuracy.
- **Benchmarks:** the blog reports 209M as first among zero-shot pre-trained models on GIFT-Eval, TIME and fev-bench.

## API

```python
# pip install git+https://github.com/mistralai/timestral.git   (Python 3.10+, torch 2.4+)
from timestral import TimestralForecaster

fc = TimestralForecaster.from_pretrained("mistralai/Timestral-28M")  # device="cuda" optional
out = fc.forecast(context, prediction_length=H,
                  past_covariates=None,     # (B, C_past, T)       known over the context only
                  known_covariates=None,    # (B, C_known, T + H)  known over context + horizon
                  )
out.quantiles        # (B, V, H, 21)
out.quantile_levels  # [0.01, 0.05, 0.10, ..., 0.95, 0.99]
out.median           # (B, V, H)
out.quantile(level)
```

- `context` can have shape `(B, V, T)`, `(B, T)` or `(T,)`, as a NumPy array or torch tensor. NaN means missing; an optional `observation_mask` can mark more.
- **Horizon:** no hard maximum. Past one 1,024-step block, the model decodes block by block, feeding back the median.
- **Covariates:** these condition the forecast and are never forecast themselves. Known-future rows containing only 0s and 1s are treated as indicators. Categorical covariates go in `categorical_past_covariates` and `categorical_known_covariates` as integer codes; these are target-encoded for univariate targets.
- **Loading options:** `revision`, `cache_dir`, `token`, `local_files_only=True` / `HF_HUB_OFFLINE=1`, or a local directory containing `config.json` and `model.safetensors`.

## Implications for our design

- **News sentiment** is only known up to today, so it is a **past covariate**, not a known-future one.
- **Calendar events** (FOMC dates, earnings, day of week) are **known-future covariates**, and indicators made of 0s and 1s fit them naturally.
- Batched inference is cheap, so a whole universe fits in one call.

## Exploration results (scripts/explore_timestral.py)

Setup: SPY daily, 10 years of yfinance data, 28M model, 512-day context, horizon 5, 250 rolling origins (2025-11-14 to 2026-11-12), evaluated on 1-day-ahead log returns. Run on 2026-11-19 on a Windows laptop CPU.

| Model | Pinball loss (lower is better) | MAE (median) | 80% PI coverage | 80% PI width | Direction hit rate |
|---|---|---|---|---|---|
| Timestral, log-return input | 0.00222 | 0.00645 | 0.800 | 0.0196 | 51.2% |
| Timestral, price input | **0.00218** | 0.00633 | 0.812 | 0.0199 | 54.8% |
| Empirical quantiles of context | 0.00223 | 0.00643 | 0.804 | 0.0205 | 51.6% |
| Empirical, zero median | 0.00223 | 0.00642 | 0.796 | 0.0205 | n/a |
| Random walk (point) | 0.00321 | 0.00642 | 0 | 0 | n/a |

- **Speed:** a batch of 250 series × 512 steps takes ≈3.1 s, about 12 ms per series. Loading the model takes ≈5 s, including the first download.
- **Calibration:** coverage holds at ≈0.80 across horizons t+1 to t+5.
- **Direction:** the 54.8% hit rate on 250 samples is about 1.5 standard errors from 50%, so it is not significant.
- **Takeaway:** Timestral is well calibrated but has little directional edge on daily SPY, and only slightly beats empirical baselines. Use it mainly for **uncertainty and risk** (sizing, interval width), and test whether covariates add directional signal.
- **Not yet tested:** other tickers, other model sizes, multivariate input (several tickers as one series), and covariates (VIX, news).
