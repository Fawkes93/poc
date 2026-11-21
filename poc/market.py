"""Candle aggregation, trend, and the gold session clock. Times are UTC."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Sequence


@dataclass
class Bar:
    open_time: datetime
    open: float
    high: float
    low: float
    close: float
    tick_count: float | None = None
    volume: float | None = None


def as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def parse_time(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return as_utc(value)
    if value is None:
        return None
    try:
        return as_utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))
    except ValueError:
        return None


def bar_from_row(row: dict) -> Bar | None:
    opened = parse_time(row.get("open_time"))
    if opened is None:
        return None
    try:
        tick = row.get("tick_count")
        volume = row.get("volume")
        return Bar(
            open_time=opened,
            open=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
            tick_count=None if tick is None else float(tick),
            volume=None if volume is None else float(volume),
        )
    except (KeyError, TypeError, ValueError):
        return None


def gold_market_open(now: datetime) -> bool:
    """Sunday 22:00 UTC through Friday 21:00 UTC, with a 21:00–22:00 break."""
    now = as_utc(now)
    weekday = now.weekday()
    minutes = now.hour * 60 + now.minute
    if weekday == 5:
        return False
    if weekday == 6:
        return minutes >= 22 * 60
    if 21 * 60 <= minutes < 22 * 60:
        return False
    if weekday == 4 and minutes >= 21 * 60:
        return False
    return True


def weekend_closed(now: datetime) -> bool:
    """Friday 21:00 UTC through Sunday 22:00 UTC. No new order, including pending."""
    now = as_utc(now)
    weekday = now.weekday()
    minutes = now.hour * 60 + now.minute
    if weekday == 5:
        return True
    if weekday == 6:
        return minutes < 22 * 60
    return weekday == 4 and minutes >= 21 * 60


def session_name(now: datetime) -> str:
    now = as_utc(now)
    if not gold_market_open(now):
        return "closed"
    if now.weekday() == 6:
        return "asia"
    hour = now.hour
    if hour < 7:
        return "asia"
    if hour < 12:
        return "london"
    if hour < 16:
        return "overlap"
    return "new_york"


def bucket_start(dt: datetime, timeframe_min: int) -> datetime:
    dt = as_utc(dt)
    if timeframe_min == 1440:
        return dt.replace(hour=0, minute=0, second=0, microsecond=0)
    if timeframe_min == 10080:
        monday = dt - timedelta(days=dt.weekday())
        return monday.replace(hour=0, minute=0, second=0, microsecond=0)
    ts = int(dt.timestamp())
    step = timeframe_min * 60
    floored = ts - (ts % step)
    return datetime.fromtimestamp(floored, tz=timezone.utc)


def aggregate(bars: Sequence[Bar], timeframe_min: int) -> list[Bar]:
    grouped: dict[datetime, list[Bar]] = {}
    order: list[datetime] = []
    for bar in bars:
        key = bucket_start(bar.open_time, timeframe_min)
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append(bar)
    out: list[Bar] = []
    for key in order:
        chunk = grouped[key]
        out.append(
            Bar(
                open_time=key,
                open=chunk[0].open,
                high=max(item.high for item in chunk),
                low=min(item.low for item in chunk),
                close=chunk[-1].close,
            )
        )
    return out


def completed_before(bars: Sequence[Bar], now: datetime, timeframe_min: int) -> list[Bar]:
    now = as_utc(now)
    current = bucket_start(now, timeframe_min)
    return [bar for bar in bars if bar.open_time < current]


def trend(bars: Sequence[Bar], *, minimum: int) -> str:
    if len(bars) < minimum:
        return "insufficient"
    window = bars[-10:] if len(bars) >= 10 else bars
    mean = sum(item.close for item in window) / len(window)
    last = bars[-1].close
    ago = bars[-minimum].close
    if last > mean and last > ago:
        return "up"
    if last < mean and last < ago:
        return "down"
    return "flat"


TF_H1 = 60
TF_H4 = 240
TF_D1 = 1440
TF_W1 = 10080
