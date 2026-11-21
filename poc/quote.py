"""Last XAU price from Twelve Data. A closed 15-minute bar can lag by one bar."""

from __future__ import annotations

import httpx

from poc.config import Settings

PRICE_URL = "https://api.twelvedata.com/price"


def latest_price(settings: Settings, http: httpx.Client | None = None) -> float:
    if not settings.twelvedata_api_key:
        raise RuntimeError("missing TWELVEDATA_API_KEY")
    client = http or httpx.Client(timeout=10)
    response = client.get(
        PRICE_URL,
        params={"symbol": settings.candle_symbol, "apikey": settings.twelvedata_api_key},
    )
    response.raise_for_status()
    body = response.json()
    if not isinstance(body, dict) or body.get("status") == "error":
        message = body.get("message") if isinstance(body, dict) else "twelvedata price failed"
        raise RuntimeError(str(message))
    price = float(body["price"])
    if price <= 0:
        raise RuntimeError("twelvedata price was not positive")
    return price
