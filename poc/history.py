"""Local 15-minute bars. cTrader is asked only for the slices SQLite does not have."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from poc.candles import bars_from_trendbars
from poc.config import Settings
from poc.market import Bar, as_utc
from poc.store import Store

SLICE_DAYS = 21


def missing_ranges(
    start: datetime,
    end: datetime,
    oldest: datetime | None,
    newest: datetime | None,
    slice_days: int = SLICE_DAYS,
) -> list[tuple[datetime, datetime]]:
    """Gaps in [start, end], newest first. Each slice is at most slice_days long."""
    start = as_utc(start)
    end = as_utc(end)
    if end <= start:
        return []
    span = timedelta(days=slice_days)
    if oldest is None or newest is None:
        return _slices(start, end, span)
    ranges: list[tuple[datetime, datetime]] = []
    newest = as_utc(newest)
    oldest = as_utc(oldest)
    if newest < end:
        ranges.extend(_slices(newest, end, span))
    if oldest > start:
        ranges.extend(_slices(start, oldest, span))
    return ranges


def _slices(start: datetime, end: datetime, span: timedelta) -> list[tuple[datetime, datetime]]:
    ranges: list[tuple[datetime, datetime]] = []
    cursor = end
    while cursor > start:
        begin = max(start, cursor - span)
        ranges.append((begin, cursor))
        cursor = begin
    return ranges


def sync_bars(factor: Any, settings: Settings, store: Store, now: datetime) -> list[Bar]:
    """Save 15-minute bars through history_days. Fetch only the missing slices."""
    start = as_utc(now) - timedelta(days=settings.history_days)
    end = as_utc(now)
    have = store.load_bars(start)
    oldest = have[0].open_time if have else None
    newest = have[-1].open_time if have else None
    loader = getattr(factor, "list_trendbars", None)
    if loader is None:
        return have
    ranges = missing_ranges(start, end, oldest, newest)
    for index, (begin, finish) in enumerate(ranges):
        try:
            rows = loader(begin, until=finish)
        except TypeError:
            rows = loader(begin)
        except Exception as exc:
            store.log("warn", "profile_bars", str(exc)[:300])
            break
        got = bars_from_trendbars(rows if isinstance(rows, list) else [])
        if not got:
            tail = newest is not None and begin >= newest
            if not (have and index == 0 and tail):
                store.log("warn", "profile_bars", "cTrader returned no 15-minute bars")
            break
        store.save_bars(got)
        have = store.load_bars(start)
    store.prune_bars(start)
    return store.load_bars(start)
