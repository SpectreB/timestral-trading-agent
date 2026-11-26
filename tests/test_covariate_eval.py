"""Measurement of the news covariates (D-019): scoring, summary, paired bootstrap, the rule."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
import pytest

from agent.config import AssetType
from agent.evaluation.covariate_eval import (
    BASE,
    MAX_COVERAGE_LOSS,
    PANEL_STOCKS,
    paired,
    panel_config,
    score,
    summarise,
    verdict,
)

LEVELS = (0.1, 0.5, 0.9)


def test_the_panel_is_the_etfs_plus_thirty_stocks_with_the_agents_five() -> None:
    cfg = panel_config()
    kinds = [a.type for a in cfg.universe]
    assert kinds.count(AssetType.ETF) == 10 and kinds.count(AssetType.STOCK) == 30
    assert len(set(cfg.symbols)) == 40 and cfg.news.enabled
    assert {"NVDA", "JPM", "XOM", "LLY", "AMZN"} <= {s for s, _, _ in PANEL_STOCKS}
    assert all(a.type is AssetType.ETF for a in cfg.universe[:10])  # ETFs first: the control


def test_a_forecast_is_scored_with_pinball_loss_coverage_width_and_sign() -> None:
    s = score(LEVELS, np.array([-1.0, 0.0, 1.0]), 0.0)
    assert s["pinball"] == pytest.approx((0.1 + 0.0 + 0.1) / 3)
    assert s["inside_80"] and s["width"] == 2.0 and not s["hit"]  # a zero move is no hit
    out = score(LEVELS, np.array([-1.0, 0.5, 1.0]), 2.0)
    assert not out["inside_80"] and out["hit"]
    assert out["pinball"] > s["pinball"]  # a miss costs more


def frame(variant_gain: float = 0.0, noise: float = 0.0, seed: int = 0) -> pd.DataFrame:
    """Prices-only and one variant on 40 origins x 3 symbols, per kind; the variant's pinball
    loss is lower by `variant_gain` on stocks (plus noise), identical on ETFs."""
    rng = np.random.default_rng(seed)
    rows = []
    for origin in range(40):
        for kind, symbols in (("stock", ("A", "B", "C")), ("etf", ("X", "Y", "Z"))):
            for sym in symbols:
                base = 0.02 + 0.001 * rng.standard_normal()
                gain = (variant_gain + noise * rng.standard_normal()) if kind == "stock" else 0.0
                for variant, loss in ((BASE, base), ("news", base - gain)):
                    rows.append(
                        {"origin": origin, "symbol": sym, "kind": kind, "variant": variant,
                         "pinball": loss, "inside_80": origin % 5 != 0, "width": 0.04,
                         "hit": origin % 2 == 0, "realised": 0.02 * (-1) ** origin}
                    )  # fmt: skip
    return pd.DataFrame(rows)


def test_the_summary_gives_calibration_loss_rms_ratio_and_hit_rate() -> None:
    rows = {(s["variant"], s["kind"]): s for s in summarise(frame())}
    s = rows[(BASE, "stock")]
    assert s["n"] == 120 and s["coverage_80"] == pytest.approx(0.8)
    assert s["hit_rate"] == pytest.approx(0.5)
    # realised |0.02| against sigma = 0.04 / 2.563: ratio 0.02 / 0.0156 = 1.28
    assert s["rms_ratio"] == pytest.approx(0.02 / (0.04 / 2.5631), rel=1e-3)
    assert set(rows) == {(BASE, "stock"), (BASE, "etf"), ("news", "stock"), ("news", "etf")}


def test_a_steady_gain_is_clear_and_pure_noise_is_not() -> None:
    steady = paired(frame(variant_gain=0.002), "news", "stock")
    assert steady["origins"] == 40 and steady["mean_diff"] == pytest.approx(0.002)
    assert steady["relative"] == pytest.approx(0.1, rel=0.1)
    assert steady["ci95"][0] > 0
    noise = paired(frame(variant_gain=0.0, noise=0.01, seed=3), "news", "stock")
    assert noise["ci95"][0] < 0 < noise["ci95"][1]
    # The ETF control is untouched by a news-only variant.
    assert paired(frame(variant_gain=0.002), "news", "etf")["mean_diff"] == pytest.approx(0.0)
    assert paired(frame(), "events", "stock")["mean_diff"] is None  # variant not run


def test_the_adoption_rule_separates_clear_weak_and_no() -> None:
    def decide(gain: float, noise: float = 0.0, **change: Any) -> dict[str, Any]:
        f = frame(variant_gain=gain, noise=noise, seed=3)
        summary = summarise(f)
        for s in summary:
            if s["variant"] == "news" and s["kind"] == "stock":
                s.update(change)
        pairs = [paired(f, "news", k) for k in ("stock", "etf")]
        return verdict(summary, pairs, "news")

    assert decide(0.002)["decision"] == "clear"
    weak = decide(0.001, noise=0.01)
    assert weak["improves_stocks"] and not weak["interval_excludes_zero"]
    assert weak["decision"] == "weak"
    assert decide(-0.002)["decision"] == "no"  # worse than prices alone
    drifted = decide(0.002, coverage_80=0.8 - MAX_COVERAGE_LOSS - 0.05)
    assert not drifted["calibration_kept"] and drifted["decision"] == "no"


def test_a_harmed_etf_control_blocks_adoption() -> None:
    f = frame(variant_gain=0.002)
    etf = (f["variant"] == "news") & (f["kind"] == "etf")
    f.loc[etf, "pinball"] += 0.002  # the "news" variant now hurts the control: not about news
    pairs = [paired(f, "news", k) for k in ("stock", "etf")]
    v = verdict(summarise(f), pairs, "news")
    assert not v["etf_control_unharmed"] and v["decision"] == "no"
