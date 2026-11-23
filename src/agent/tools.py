"""Timestral as a set of tools the LLM calls before deciding (D-018).

Three tools, each a question the LLM may ask:

- `get_forecasts`: Timestral's quantile forecast of chosen assets at a chosen horizon (1 to 5
  trading days), with the volatility implied by the 80% interval.
- `evaluate_portfolio`: what a candidate allocation would look like. Two views of its risk:
  1. **Timestral forecasts the portfolio itself.** The value history of the candidate holdings
     is rebuilt over the context window and given to Timestral as one more series. Its quantiles
     describe the portfolio's own distribution, correlations included, with no normal
     assumption: probability of a loss, bad-case loss (5% quantile).
  2. The risk decomposition of D-014 (Timestral volatilities x shrunk historical correlation):
     forecast volatility and each asset's share of the risk.
  It compares the allocation with the portfolio held now (points traded, change in forecast
  volatility and in bad case), so a change that trades a lot for little risk
  benefit is visible (D-018, calm-market finding). It also says whether the guardrails would
  accept the allocation as it is, using the production guardrails themselves, so the LLM can
  correct itself before answering.
- `get_baseline`: the rule-based portfolio (weights inversely proportional to the forecast
  interval width), as a reference the LLM may use or ignore.

Everything the LLM sends is untrusted: arguments are validated, a bad call returns an error the
LLM can read (never an exception), and the number of calls is capped. Every call and its result
is recorded for the audit log.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from typing import Any

import numpy as np
import pandas as pd

from agent.config import AgentConfig
from agent.forecast import AssetForecast, QuantileModel, _to_numpy, forecast_assets
from agent.guardrails import apply_guardrails
from agent.news import FAMILIES, NewsCovariates, forecast_kwargs
from agent.risk import correlation, covariance, forecast_vols, portfolio_vol, risk_contributions

LEVELS_SHOWN = {"q05": 0.05, "q10": 0.1, "q25": 0.25, "median": 0.5, "q75": 0.75, "q90": 0.9,
                "q95": 0.95}  # fmt: skip


def _r(x: float, digits: int = 5) -> float:
    return round(float(x), digits)


def _quantiles(levels: tuple[float, ...], values: np.ndarray) -> dict[str, float]:
    lv = np.asarray(levels)
    return {k: _r(values[int(np.argmin(np.abs(lv - v)))]) for k, v in LEVELS_SHOWN.items()}


def _probability_below_zero(levels: tuple[float, ...], values: np.ndarray) -> float:
    """P(return < 0) read from the quantile function by linear interpolation."""
    lv = np.asarray(levels, dtype=float)
    if values[0] >= 0:
        return _r(lv[0], 3)
    if values[-1] <= 0:
        return _r(lv[-1], 3)
    return _r(float(np.interp(0.0, values, lv)), 3)


class ToolError(ValueError):
    """An invalid tool call: its message is returned to the LLM."""


class TimestralTools:
    """The tools of one decision. `forecasts` is the batched Timestral run of the cycle."""

    def __init__(
        self,
        *,
        forecasts: Mapping[str, AssetForecast],
        closes: pd.DataFrame,
        model: QuantileModel,
        baseline: Mapping[str, float],
        current: Mapping[str, float],
        drawdown: float,
        cfg: AgentConfig,
        news: NewsCovariates | None = None,
    ) -> None:
        self.forecasts = forecasts
        self.news = news  # D-019: present only for an agent with news covariates
        self.closes = closes
        self.model = model
        self.baseline = dict(baseline)
        self.current = dict(current)
        self.drawdown = drawdown
        self.cfg = cfg
        self.calls: list[dict[str, Any]] = []
        self._corr = correlation(closes)
        self._variants: dict[tuple[str, ...], Mapping[str, AssetForecast]] = {(): forecasts}

    # ------------------------------------------------------------------ schemas

    def specs(self) -> list[dict[str, Any]]:
        specs = self._base_specs()
        if not self.cfg.news.enabled:
            return specs  # the agent without news keeps exactly its tools
        covariates = {
            "type": "array",
            "items": {"type": "string", "enum": list(FAMILIES)},
            "description": (
                'Information Timestral receives besides prices: "news" (volume and sentiment '
                'of each stock\'s recent news, past days only), "events" (announced earnings, '
                "macro releases and company events, also over the forecast days). Empty: "
                "prices only."
            ),
        }
        for spec in specs:
            fn = spec["function"]
            if fn["name"] in ("get_forecasts", "evaluate_portfolio"):
                fn["parameters"]["properties"]["covariates"] = covariates
                fn["parameters"]["required"].append("covariates")
        specs[0]["function"]["description"] += (
            " With covariates, each asset also shows how the forecast differs from the "
            "prices-only one."
        )
        specs[1]["function"]["description"] += (
            " Covariates apply to the per-asset risk decomposition; the forecast of the "
            "portfolio's own value uses prices only."
        )
        specs.append(
            {
                "type": "function",
                "function": {
                    "name": "get_news_signals",
                    "description": (
                        "What the analysed news says, as numbers and dates only: for each stock, "
                        "the number of relevant articles and their average sentiment (-1 to +1) "
                        "over the last sessions; for every asset, the events announced over the "
                        "forecast days. ETFs have no company news (control group)."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "symbols": {
                                "type": "array",
                                "items": {"type": "string", "enum": list(self.cfg.symbols)},
                            }
                        },
                        "required": ["symbols"],
                        "additionalProperties": False,
                    },
                },
            }
        )
        return specs

    def _base_specs(self) -> list[dict[str, Any]]:
        symbols = list(self.cfg.symbols)
        horizon = {
            "type": "integer",
            "description": f"Trading days ahead, 1 to {self.cfg.forecast.horizon}.",
        }
        return [
            {
                "type": "function",
                "function": {
                    "name": "get_forecasts",
                    "description": (
                        "Timestral's forecast of the cumulative return of each asset from the "
                        "last close to the given horizon: quantiles q05 to q95, the width of the "
                        "80% interval and the implied volatility. An empty list means all assets."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "symbols": {
                                "type": "array",
                                "items": {"type": "string", "enum": symbols},
                            },
                            "horizon": horizon,
                        },
                        "required": ["symbols", "horizon"],
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "evaluate_portfolio",
                    "description": (
                        "Evaluate a candidate allocation (weights as fractions of the portfolio; "
                        "omitted assets are 0, the rest is cash). Returns Timestral's forecast of "
                        "the portfolio's own value (quantiles, probability of a loss), its "
                        "forecast volatility and each asset's share of the risk, how it compares "
                        "with the portfolio held now (points traded, change in risk), and whether "
                        "the guardrails would accept it as it is."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "weights": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "symbol": {"type": "string", "enum": symbols},
                                        "weight": {"type": "number"},
                                    },
                                    "required": ["symbol", "weight"],
                                    "additionalProperties": False,
                                },
                            },
                            "horizon": horizon,
                        },
                        "required": ["weights", "horizon"],
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "get_baseline",
                    "description": (
                        "The rule-based reference portfolio: weights inversely proportional to "
                        "the width of each asset's 80% forecast interval at the decision "
                        "horizon, within every cap. A reference, not an obligation."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {},
                        "additionalProperties": False,
                    },
                },
            },
        ]

    # ------------------------------------------------------------------ dispatch

    @property
    def budget_left(self) -> int:
        return self.cfg.tools.max_calls - len(self.calls)

    def call(self, name: str, arguments: str) -> str:
        """Run one tool call from the LLM and return its JSON result (errors included)."""
        if self.budget_left <= 0:
            result: dict[str, Any] = {
                "error": "Tool budget exhausted: give your final decision now."
            }
        else:
            try:
                args = json.loads(arguments) if arguments.strip() else {}
                if not isinstance(args, dict):
                    raise ToolError("arguments must be a JSON object")
                cov = args.get("covariates", [])
                if name == "get_forecasts":
                    result = self.get_forecasts(args.get("symbols", []), args.get("horizon"), cov)
                elif name == "evaluate_portfolio":
                    result = self.evaluate_portfolio(args.get("weights"), args.get("horizon"), cov)
                elif name == "get_baseline":
                    result = self.get_baseline()
                elif name == "get_news_signals" and self.cfg.news.enabled:
                    result = self.get_news_signals(args.get("symbols", []))
                else:
                    raise ToolError(f"unknown tool {name!r}")
            except json.JSONDecodeError:
                result = {"error": "arguments are not valid JSON"}
            except ToolError as error:
                result = {"error": str(error)}
        self.calls.append({"name": name, "arguments": arguments, "result": result})
        return json.dumps(result)

    # ------------------------------------------------------------------ validation

    def _horizon(self, h: Any) -> int:
        if isinstance(h, bool) or not isinstance(h, int | float) or h != int(h):
            raise ToolError("horizon must be an integer")
        if not 1 <= int(h) <= self.cfg.forecast.horizon:
            raise ToolError(f"horizon must be between 1 and {self.cfg.forecast.horizon}")
        return int(h)

    def _symbols(self, symbols: Any) -> list[str]:
        if not isinstance(symbols, list) or not all(isinstance(s, str) for s in symbols):
            raise ToolError("symbols must be a list of strings")
        unknown = sorted(set(symbols) - set(self.cfg.symbols))
        if unknown:
            raise ToolError(f"not in the universe: {', '.join(unknown)}")
        return [s for s in self.cfg.symbols if s in symbols] if symbols else list(self.cfg.symbols)

    def _families(self, covariates: Any) -> tuple[str, ...]:
        if covariates in (None, []):
            return ()
        if not isinstance(covariates, list) or not all(isinstance(c, str) for c in covariates):
            raise ToolError("covariates must be a list of strings")
        unknown = sorted(set(covariates) - set(FAMILIES))
        if unknown:
            raise ToolError(f"unknown covariates: {', '.join(unknown)} (use {list(FAMILIES)})")
        if not self.cfg.news.enabled or self.news is None:
            raise ToolError("news covariates are not available in this run")
        return tuple(f for f in FAMILIES if f in covariates)

    def _forecasts(self, families: tuple[str, ...]) -> Mapping[str, AssetForecast]:
        """Timestral's forecasts with the chosen covariates, computed once per combination."""
        if families not in self._variants:
            kwargs = forecast_kwargs(self.news, families, list(self.closes.columns))
            self._variants[families] = forecast_assets(self.closes, self.model, self.cfg, kwargs)
        return self._variants[families]

    def _weights(self, weights: Any) -> dict[str, float]:
        if not isinstance(weights, list):
            raise ToolError("weights must be a list of {symbol, weight}")
        out = dict.fromkeys(self.cfg.symbols, 0.0)
        seen: set[str] = set()
        for item in weights:
            if not isinstance(item, dict):
                raise ToolError("each weight must be an object {symbol, weight}")
            s, w = item.get("symbol"), item.get("weight")
            if s not in out:
                raise ToolError(f"not in the universe: {s}")
            if s in seen:
                raise ToolError(f"{s} appears twice")
            if isinstance(w, bool) or not isinstance(w, int | float) or not math.isfinite(w):
                raise ToolError(f"weight of {s} must be a finite number")
            if w < 0:
                raise ToolError(f"weight of {s} is negative: the portfolio is long-only")
            seen.add(s)
            out[s] = float(w)
        if sum(out.values()) > 1 + 1e-9:
            raise ToolError("weights sum to more than 1: no leverage")
        return out

    # ------------------------------------------------------------------ tools

    def get_forecasts(self, symbols: Any, horizon: Any, covariates: Any = ()) -> dict[str, Any]:
        h = self._horizon(horizon)
        chosen = self._symbols(symbols)
        families = self._families(covariates)
        fc = self._forecasts(families)
        vols = forecast_vols({s: fc[s] for s in chosen}, h)
        base = forecast_vols({s: self.forecasts[s] for s in chosen}, h)

        def view(s: str) -> dict[str, Any]:
            out: dict[str, Any] = {
                **_quantiles(fc[s].levels, fc[s].cum_returns[h - 1]),
                "width_80": _r(fc[s].width(h)),
                "volatility": _r(vols[s]),
            }
            if families:  # what the information changed, against the prices-only forecast
                out["vs_prices_only"] = {
                    "width_80_change": _r(fc[s].width(h) - self.forecasts[s].width(h)),
                    "median_change": _r(fc[s].median(h) - self.forecasts[s].median(h)),
                    "volatility_change": _r(vols[s] - base[s]),
                }
            return out

        result: dict[str, Any] = {
            "horizon": h,
            "last_close_date": next(iter(self.forecasts.values())).last_date.isoformat(),
        }
        if self.cfg.news.enabled:
            result["covariates"] = list(families)
        result["assets"] = {s: view(s) for s in chosen}
        return result

    def portfolio_series(self, weights: Mapping[str, float]) -> np.ndarray:
        """Value history of the candidate holdings, scaled to 1 at the last close.

        Holdings are those the weights give today (shares bought at the last close), so the
        series is what holding this portfolio would have been worth on each past day."""
        values = self.closes[list(self.cfg.symbols)].to_numpy(dtype=np.float64)
        w = np.array([weights[s] for s in self.cfg.symbols])
        cash = 1.0 - float(w.sum())
        return cash + (values / values[-1]) @ w  # type: ignore[no-any-return]

    def _portfolio_forecast(self, w: Mapping[str, float], h: int) -> dict[str, float]:
        """Timestral's forecast of the portfolio's own value series (all cash: no risk)."""
        if sum(w.values()) <= 0:
            return {**dict.fromkeys(LEVELS_SHOWN, 0.0), "probability_of_loss": 0.0}
        series = self.portfolio_series(w)
        out = self.model.forecast(series[None, :], prediction_length=self.cfg.forecast.horizon)
        levels = tuple(float(v) for v in _to_numpy(out.quantile_levels))
        q = _to_numpy(out.quantiles).reshape(self.cfg.forecast.horizon, len(levels))
        cum = np.maximum.accumulate(q[h - 1] / series[-1] - 1.0)  # quantiles must not cross
        return {
            **_quantiles(levels, cum),
            "probability_of_loss": _probability_below_zero(levels, cum),
        }

    def evaluate_portfolio(
        self, weights: Any, horizon: Any, covariates: Any = ()
    ) -> dict[str, Any]:
        h = self._horizon(horizon)
        w = self._weights(weights)
        families = self._families(covariates)
        invested = sum(w.values())

        # 1. Timestral forecasts the portfolio's own value series.
        forecast = self._portfolio_forecast(w, h)

        # 2. Risk decomposition from the asset forecasts (D-014).
        cov = covariance(forecast_vols(self._forecasts(families), h), self._corr)
        shares = risk_contributions(w, cov)
        top = sorted(((s, x) for s, x in shares.items() if w[s] > 0), key=lambda t: -t[1])[:5]
        vol = portfolio_vol(w, cov)

        # 3. Against the portfolio held now: what the change trades, and what it buys in risk.
        #    Makes churn visible: many points traded for a negligible change in risk. Only measures
        #    of dispersion are compared: the probability of a loss moves with the median, which has
        #    no measured skill, so comparing it would reward guessing the direction.
        held = self._portfolio_forecast(self.current, h)
        versus_current = {
            "points_traded": _r(100 * sum(abs(w[s] - self.current[s]) for s in w), 2),
            "forecast_volatility_change": _r(vol - portfolio_vol(self.current, cov)),
            "bad_case_q05_change": _r(forecast["q05"] - held["q05"]),
        }

        # 4. Would the guardrails accept it as it is?
        proposal = {
            "analysis": "tool check",
            "positions": [
                {"symbol": s, "target_weight": v, "rationale": "tool check", "confidence": 0.5}
                for s, v in w.items()
            ],
            "cash_weight": 1.0 - invested,
        }
        check = apply_guardrails(proposal, self.baseline, self.current, self.drawdown, self.cfg)
        breaches = [
            f"{c.rule} {c.symbol or ''}: {c.before:.4f} -> {c.after:.4f}".replace("  ", " ")
            for c in check.corrections
            if c.counted
        ]
        result: dict[str, Any] = {"horizon": h}
        if self.cfg.news.enabled:
            result["covariates"] = list(families)
        return result | {
            "invested": _r(invested, 4),
            "cash": _r(1 - invested, 4),
            "timestral_portfolio_forecast": forecast,
            "forecast_volatility": _r(vol),
            "risk_share_top5": {s: _r(x, 3) for s, x in top},
            "versus_current_portfolio": versus_current,
            "guardrails": {
                "would_be": check.status,
                "rejection_reason": check.rejection_reason,
                "breaches": breaches,
            },
        }

    def get_news_signals(self, symbols: Any) -> dict[str, Any]:
        chosen = self._symbols(symbols)
        if self.news is None:
            raise ToolError("news covariates are not available in this run")
        return {
            "coverage_start": self.news.coverage_start.isoformat(),
            "forecast_days": [d.isoformat() for d in self.news.future],
            "assets": {s: self.news.signals(s) for s in chosen},
        }

    def get_baseline(self) -> dict[str, Any]:
        return {
            "horizon": self.cfg.forecast.horizon,
            "weights": {s: _r(w, 4) for s, w in self.baseline.items()},
            "cash": _r(1 - sum(self.baseline.values()), 4),
        }
