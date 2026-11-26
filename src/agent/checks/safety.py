"""Independent final check before any order is sent (D-009, defence in depth). It shares no code
with the guardrails, so a bug there cannot reach the broker."""

from __future__ import annotations

import math
from collections.abc import Mapping

from agent.config import AgentConfig

TOLERANCE = 1e-6


class UnsafePortfolioError(RuntimeError):
    def __init__(self, violations: list[str]) -> None:
        super().__init__("; ".join(violations))
        self.violations = violations


def final_check(weights: Mapping[str, float], cash: float, cfg: AgentConfig) -> None:
    violations: list[str] = []
    universe = set(cfg.symbols)

    for symbol, w in weights.items():
        if symbol not in universe:
            violations.append(f"{symbol}: not in the universe")
            continue
        if not math.isfinite(w):
            violations.append(f"{symbol}: weight is not a finite number")
            continue
        if w < 0:
            violations.append(f"{symbol}: short position ({w:.4f})")
        if w > cfg.max_weight(symbol) + TOLERANCE:
            violations.append(f"{symbol}: {w:.4f} above its cap {cfg.max_weight(symbol):.2f}")

    group_totals: dict[str, float] = {}
    for asset in cfg.universe:
        w = weights.get(asset.symbol, 0.0)
        if math.isfinite(w):
            group_totals[asset.group] = group_totals.get(asset.group, 0.0) + w
    for group, total in group_totals.items():
        if total > cfg.risk.max_weight_group + TOLERANCE:
            violations.append(f"group {group}: {total:.4f} above its cap")

    invested = sum(w for w in weights.values() if math.isfinite(w))
    if invested > cfg.risk.max_invested + TOLERANCE:
        violations.append(f"invested {invested:.4f}: leverage or cash floor breached")
    if not math.isfinite(cash) or cash < cfg.risk.min_cash - TOLERANCE:
        violations.append(f"cash {cash}: below the {cfg.risk.min_cash:.0%} floor")
    elif abs(invested + cash - 1.0) > TOLERANCE:
        violations.append(f"weights + cash = {invested + cash:.6f}, not 100%")

    if violations:
        raise UnsafePortfolioError(violations)
