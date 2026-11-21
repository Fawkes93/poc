"""Hourly checks, plus a price poll that wakes a check when an alert is crossed."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from poc.market import as_utc, gold_market_open


def next_check(now: datetime, minute: int = 5) -> datetime:
    """Next UTC hour at the given minute. 00:05, 01:05, 02:05, and so on."""
    now = as_utc(now)
    slot = now.replace(minute=minute, second=0, microsecond=0)
    if now < slot:
        return slot
    return slot + timedelta(hours=1)


def crossed(previous: float, current: float, level: float) -> bool:
    return (previous - level) * (current - level) <= 0 and previous != current


def poll_price_alerts(
    settings: Any,
    store: Any,
    factor: Any,
    quote: Callable[[], float],
    state: dict[str, Any],
    run: Callable[..., Any],
    now: datetime | None = None,
) -> bool:
    """Run one cycle when the live price crosses an alert the model left."""
    now = as_utc(now or datetime.now(timezone.utc))
    if not gold_market_open(now):
        return False
    last = state.get("last_alert_at")
    if (
        isinstance(last, datetime)
        and (now - last).total_seconds() < settings.alert_debounce_sec
        and not state.get("pending")
    ):
        return False
    try:
        price = float(quote())
    except Exception as exc:
        store.error(str(exc))
        store.log("error", "alert_quote", str(exc))
        return False
    previous = state.get("last_price")
    state["last_price"] = price
    if previous is None:
        return False
    hits = [alert for alert in store.alerts() if crossed(float(previous), price, float(alert["price"]))]
    if not hits and not state.get("pending"):
        return False
    result = run(trigger="alert", now=now)
    if getattr(result, "busy", False):
        state["pending"] = True
        return False
    state["pending"] = False
    state["last_alert_at"] = now
    store.log("info", "alert", {"price": price, "hits": hits})
    return True
