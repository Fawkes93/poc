"""Read XAU/USD 15-minute candles from Reaktor. This process does not write them."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import httpx

from poc.config import Settings
from poc.market import Bar, bar_from_row


class CandleError(RuntimeError):
    pass


def load_candles(
    settings: Settings,
    since: datetime,
    http: httpx.Client | None = None,
) -> list[Bar]:
    if not settings.reaktor_supabase_url or not settings.reaktor_supabase_key:
        raise CandleError("missing reaktor supabase")
    own_client = http is None
    client = http or httpx.Client(timeout=60)
    try:
        return _read_candles(settings, since, client)
    finally:
        if own_client:
            client.close()


def _read_candles(settings: Settings, since: datetime, client: httpx.Client) -> list[Bar]:
    bars: list[Bar] = []
    offset = 0
    page = 1000
    headers = {
        "apikey": settings.reaktor_supabase_key,
        "Authorization": f"Bearer {settings.reaktor_supabase_key}",
    }
    while True:
        response = client.get(
            f"{settings.reaktor_supabase_url}/rest/v1/candles",
            params={
                "select": "open_time,open,high,low,close,tick_count,volume",
                "symbol": f"eq.{settings.candle_symbol}",
                "timeframe_min": "eq.15",
                "open_time": f"gte.{since.isoformat()}",
                "order": "open_time.asc",
                "limit": str(page),
                "offset": str(offset),
            },
            headers=headers,
        )
        if response.status_code >= 400:
            raise CandleError(f"{response.status_code} {response.text[:200]}")
        rows = response.json()
        if not isinstance(rows, list):
            raise CandleError("candles payload was not a list")
        for row in rows:
            if isinstance(row, dict):
                bar = bar_from_row(row)
                if bar is not None:
                    bars.append(bar)
        if len(rows) < page:
            break
        offset += page
    return bars


def load_recent(settings: Settings, now: datetime, http: httpx.Client | None = None) -> list[Bar]:
    since = now - timedelta(days=settings.history_days)
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    return load_candles(settings, since, http)


def bars_from_trendbars(rows: list) -> list[Bar]:
    """cTrader 15-minute bars. tick_count is that bar's tick volume."""
    bars: list[Bar] = []
    for row in rows:
        if isinstance(row, dict):
            bar = bar_from_row(row)
            if bar is not None:
                bars.append(bar)
    bars.sort(key=lambda bar: bar.open_time)
    return bars


def latest_close(bars: list[Bar]) -> float | None:
    if not bars:
        return None
    return max(bars, key=lambda bar: bar.open_time).close
