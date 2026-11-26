"""Forecast scoring (D-013): did Timestral's forecasts match what actually happened?"""

from __future__ import annotations

import argparse
import math
from collections.abc import Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pandas as pd

from agent.config import AgentConfig
from agent.inputs.data import NEW_YORK, MarketData, completed_closes

if TYPE_CHECKING:
    from agent.execution.audit import FileAuditStore

WINDOW = 12  # sessions per observation: covers the 5 horizons plus margin for holidays


def record_observation(
    store: FileAuditStore, market: MarketData, cfg: AgentConfig, now: datetime
) -> str | None:
    """Record the latest completed session window; None if it is already recorded."""
    end = now - timedelta(minutes=16)  # free data plan: consolidated feed delayed by 15 min
    raw = market.daily_closes(list(cfg.symbols), end - timedelta(days=30), end)
    closes = completed_closes(raw, now.astimezone(NEW_YORK).date(), market.is_market_open())
    closes = closes[list(cfg.symbols)].iloc[-WINDOW:]
    name: str = pd.Timestamp(closes.index[-1]).date().isoformat()
    if store.has_observation(name):
        return None
    window: dict[str, dict[str, float | None]] = {}
    for i, ts in enumerate(closes.index):
        row = closes.iloc[i]
        window[pd.Timestamp(ts).date().isoformat()] = {
            str(sym): (None if math.isnan(v) else float(v)) for sym, v in row.items()
        }
    store.write_observation(name, {"as_of": name, "closes": window})
    return name


def score_forecasts(
    runs: Sequence[dict[str, Any]], observations: Sequence[dict[str, Any]]
) -> list[dict[str, Any]]:
    """One row per (run, asset, horizon) whose outcome is known."""
    ordered = sorted(observations, key=lambda o: o["as_of"])
    rows: list[dict[str, Any]] = []
    for run in runs:
        d0 = run["data"]["last_session"]
        for symbol, horizons in run["forecasts"].items():
            for key, q in horizons.items():
                h = int(key)
                found = realised_return(ordered, d0, h, symbol)
                if found is None:
                    continue
                dh, realised = found
                median = q["median"]
                rows.append(
                    {
                        "run_id": run["run_id"],
                        "last_session": d0,
                        "symbol": symbol,
                        "horizon": h,
                        "realised_session": dh,
                        "q10": q["q10"],
                        "median": median,
                        "q90": q["q90"],
                        "width_80": q["width_80"],
                        "realised": realised,
                        "inside_80": q["q10"] <= realised <= q["q90"],
                        "direction_hit": (
                            None if median == 0 or realised == 0 else (median > 0) == (realised > 0)
                        ),
                        "abs_error_median": abs(realised - median),
                    }
                )
    return rows


def realised_return(
    observations: Sequence[dict[str, Any]], d0: str, h: int, symbol: str
) -> tuple[str, float] | None:
    """(session, return) of `symbol` from the close of d0 to the close h sessions later, from the
    earliest observation (sorted by date) that contains both; None if unknown."""
    for obs in observations:
        sessions = sorted(obs["closes"])
        later = [d for d in sessions if d > d0]
        if d0 not in sessions or len(later) < h:
            continue
        dh = later[h - 1]
        start, end = obs["closes"][d0][symbol], obs["closes"][dh][symbol]
        if start is None or end is None:
            return None
        return dh, float(end / start - 1)
    return None


def calibration(scores: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per horizon: how many forecasts are scored and how they did."""
    out = []
    for h in sorted({s["horizon"] for s in scores}):
        rows = [s for s in scores if s["horizon"] == h]
        directed = [s for s in rows if s["direction_hit"] is not None]
        out.append(
            {
                "horizon": h,
                "n": len(rows),
                "inside_80": sum(s["inside_80"] for s in rows) / len(rows),
                "direction_hit": (
                    sum(s["direction_hit"] for s in directed) / len(directed) if directed else None
                ),
                "abs_error_median": sum(s["abs_error_median"] for s in rows) / len(rows),
            }
        )
    return out


def main() -> None:  # pragma: no cover - CLI wiring, runs in the trade workflow
    import os
    from datetime import UTC

    from dotenv import load_dotenv

    from agent.execution.audit import FileAuditStore
    from agent.inputs.data import AlpacaMarketData

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit-dir", default="audit", type=Path)
    args = parser.parse_args()

    load_dotenv()
    market = AlpacaMarketData(os.environ["ALPACA_API_KEY"], os.environ["ALPACA_SECRET_KEY"])
    name = record_observation(
        FileAuditStore(args.audit_dir), market, AgentConfig(), datetime.now(UTC)
    )
    print(f"observation recorded: {name}" if name else "observation already recorded")


if __name__ == "__main__":  # pragma: no cover
    main()
