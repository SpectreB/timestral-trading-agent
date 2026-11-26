"""Baseline portfolio from Timestral's uncertainty (D-004, sub-question 1b).

Weight of each asset is proportional to 1 / (width of its 80% forecast interval at the
decision horizon): the more uncertain Timestral is about an asset, the smaller its
position. The forecast direction (median) is deliberately ignored: we measured no
directional skill.

Steps: inverse-width weights scaled to the maximum invested share, then per-asset and
per-group caps. Weight removed by a cap goes to cash, so the baseline always satisfies
the same invariants as the final check.
"""

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
