"""Baseline portfolio from Timestral's uncertainty (D-004, sub-question 1b). Weights are inversely
proportional to the 80% interval width; the median is ignored (no measured directional skill)."""

from __future__ import annotations

from collections.abc import Mapping

from agent.config import AgentConfig
from agent.inputs.forecast import AssetForecast


def baseline_weights(forecasts: Mapping[str, AssetForecast], cfg: AgentConfig) -> dict[str, float]:
    if set(forecasts) != set(cfg.symbols):
        raise ValueError("baseline needs a forecast for every asset in the universe")
    horizon = cfg.forecast.horizon
    inverse = {s: 1.0 / forecasts[s].width(horizon) for s in cfg.symbols}
    total = sum(inverse.values())
    weights = {s: cfg.risk.max_invested * v / total for s, v in inverse.items()}

    weights = {s: min(w, cfg.max_weight(s)) for s, w in weights.items()}

    groups: dict[str, list[str]] = {}
    for a in cfg.universe:
        groups.setdefault(a.group, []).append(a.symbol)
    for members in groups.values():
        group_total = sum(weights[s] for s in members)
        if group_total > cfg.risk.max_weight_group:
            factor = cfg.risk.max_weight_group / group_total
            for s in members:
                weights[s] *= factor
    return weights
