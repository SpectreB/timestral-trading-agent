from __future__ import annotations

import json
import math
from datetime import datetime
from typing import Any

from agent.config import AgentConfig
from agent.decision.baseline import baseline_weights
from agent.decision.decide import LLMClient
from agent.execution.execute import Broker, preflight
from agent.inputs.data import MarketData, load_context
from agent.inputs.forecast import QuantileModel, forecast_assets

PING_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}},
    "required": ["ok"],
    "additionalProperties": False,
}


class SmokeError(RuntimeError): ...


def smoke_check(
    *,
    now: datetime,
    cfg: AgentConfig,
    market: MarketData,
    broker: Broker,
    model: QuantileModel,
    llm: LLMClient,
) -> dict[str, Any]:
    account = preflight(broker)
    closes = load_context(market, cfg, now)
    baseline_weights(forecast_assets(closes, model, cfg), cfg)

    prices = broker.latest_prices(cfg.symbols)
    bad = [s for s in cfg.symbols if not math.isfinite(prices.get(s, math.nan)) or prices[s] <= 0]
    if bad:
        raise SmokeError(f"invalid live prices for {bad}")

    response = llm.complete(
        [{"role": "user", "content": 'Reply with the JSON object {"ok": true}.'}], PING_SCHEMA
    )
    if response.model != cfg.llm.model:
        raise SmokeError(f"asked for {cfg.llm.model}, the API answered with {response.model}")
    try:
        ok = json.loads(response.text)["ok"]
    except (ValueError, KeyError, TypeError):
        raise SmokeError(f"unparseable LLM answer: {response.text[:80]!r}") from None
    if ok is not True:
        raise SmokeError(f"unexpected LLM answer: {response.text[:80]!r}")

    return {
        "equity": account.equity,
        "last_session": closes.index[-1].date().isoformat(),
        "n_prices": len(prices),
        "llm_model": response.model,
        "market_open": broker.is_market_open(),
    }


def main() -> None:  # pragma: no cover - CLI wiring, runs in the smoke workflow
    import os
    from datetime import UTC

    from dotenv import load_dotenv

    from agent.decision.decide import MistralLLM
    from agent.execution.execute import AlpacaBroker
    from agent.inputs.data import AlpacaMarketData
    from agent.inputs.forecast import load_model

    load_dotenv()
    cfg = AgentConfig()
    key, secret = os.environ["ALPACA_API_KEY"], os.environ["ALPACA_SECRET_KEY"]
    summary = smoke_check(
        now=datetime.now(UTC),
        cfg=cfg,
        market=AlpacaMarketData(key, secret),
        broker=AlpacaBroker(key, secret),
        model=load_model(cfg.forecast.model_id),
        llm=MistralLLM(os.environ["MISTRAL_API_KEY"], cfg),
    )
    print("SMOKE TEST PASSED", json.dumps(summary))


if __name__ == "__main__":  # pragma: no cover
    main()
