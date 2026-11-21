"""Last XAU price from Twelve Data. A closed 15-minute bar can lag by one bar."""

from __future__ import annotations

import httpx

from poc.config import Settings

PRICE_URL = "https://api.twelvedata.com/price"


def spot_quote(factor: object) -> dict[str, float] | None:
    """cTrader bid and ask from Factor. None when this broker has no quote call."""
    reader = getattr(factor, "quote", None)
    if reader is None:
        return None
    raw = reader()
    if not isinstance(raw, dict):
        raise RuntimeError("quote payload was not an object")
    bid = float(raw["bid"])
    ask = float(raw["ask"])
    if bid <= 0 or ask <= 0:
        raise RuntimeError("quote was not positive")
    if ask < bid:
        bid, ask = ask, bid
    return {"bid": bid, "ask": ask}


def live_mid(settings: Settings, factor: object, http: httpx.Client | None = None) -> float:
    """Mid price for an alert. cTrader first, then Twelve Data."""
    spot_error: Exception | None = None
    try:
        spot = spot_quote(factor)
    except Exception as exc:
        spot = None
        spot_error = exc
    if spot is not None:
        return (spot["bid"] + spot["ask"]) / 2
    try:
        return latest_price(settings, http)
    except Exception as exc:
        if spot_error is not None:
            raise RuntimeError(f"{spot_error}; {exc}") from exc
        raise


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
