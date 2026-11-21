"""Weekly volume profile. Same bin rule as the Reaktor chart.

Weight is tick_count, then volume. The value area is 70% of that weight,
expanded from the heaviest bin toward the heavier neighbor. Ties expand down.
Weeks are Monday 00:00 to Monday 00:00 in Europe/Bucharest.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Sequence
from zoneinfo import ZoneInfo

from poc.market import Bar, as_utc

VALUE_AREA = 0.70
TARGET_BINS = 64
BUCHAREST = ZoneInfo("Europe/Bucharest")


@dataclass
class Bin:
    low: float
    high: float
    mid: float
    total: float


@dataclass
class Profile:
    start: datetime | None
    end: datetime | None
    poc: float | None
    vah: float | None
    val: float | None
    bars: int
    location: str = "unclear"

    def view(self) -> dict:
        return {
            "from": None if self.start is None else self.start.isoformat(),
            "to": None if self.end is None else self.end.isoformat(),
            "poc": _round(self.poc),
            "vah": _round(self.vah),
            "val": _round(self.val),
            "bars": self.bars,
            "location": self.location,
        }


def _round(value: float | None) -> float | None:
    if value is None:
        return None
    return round(value, 2)


def bar_weight(bar: Bar) -> float:
    if bar.tick_count is not None:
        return max(0.0, float(bar.tick_count))
    if bar.volume is not None:
        return max(0.0, float(bar.volume))
    return 0.0


def monday_start(moment: datetime, tz: ZoneInfo = BUCHAREST) -> datetime:
    """Local Monday 00:00 of the week that contains moment."""
    local = as_utc(moment).astimezone(tz)
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    monday = midnight - timedelta(days=midnight.weekday())
    return monday.replace(hour=0, minute=0, second=0, microsecond=0)


def previous_week(now: datetime, tz: ZoneInfo = BUCHAREST) -> tuple[datetime, datetime]:
    this_monday = monday_start(now, tz)
    previous = (this_monday - timedelta(days=7)).astimezone(tz)
    previous = previous.replace(hour=0, minute=0, second=0, microsecond=0)
    return previous, this_monday


def developing_week(now: datetime, tz: ZoneInfo = BUCHAREST) -> tuple[datetime, datetime]:
    return monday_start(now, tz), as_utc(now)


def choose_step(lo: float, hi: float, precision: int = 2) -> float:
    span = hi - lo
    min_move = 10 ** -max(0, precision)
    if not span > 0:
        return min_move
    raw = span / TARGET_BINS
    if not raw > 0:
        return min_move
    exp = int(math.floor(math.log10(raw)))
    base = 10 ** exp
    frac = raw / base
    nice = 1 if frac <= 1 else 2 if frac <= 2 else 5 if frac <= 5 else 10
    step = nice * base
    if step < min_move:
        step = min_move
    return step


def value_area(bins: Sequence[Bin], total: float) -> tuple[float | None, float | None, float | None]:
    """POC mid, VAH (top of the upper bin), VAL (bottom of the lower bin)."""
    if not bins or not total > 0:
        return None, None, None
    poc_i = max(range(len(bins)), key=lambda index: bins[index].total)
    need = total * VALUE_AREA
    acc = bins[poc_i].total
    lo = poc_i
    hi = poc_i
    while acc < need and (lo > 0 or hi < len(bins) - 1):
        below = bins[lo - 1].total if lo > 0 else -1
        above = bins[hi + 1].total if hi < len(bins) - 1 else -1
        if above > below:
            hi += 1
            acc += bins[hi].total
        elif below >= 0:
            lo -= 1
            acc += bins[lo].total
        else:
            break
    return bins[poc_i].mid, bins[hi].high, bins[lo].low


def locate(price: float, poc: float | None, vah: float | None, val: float | None) -> str:
    if poc is None or vah is None or val is None:
        return "unclear"
    if price > vah:
        return "above_vah"
    if price < val:
        return "below_val"
    if abs(price - poc) <= 2:
        return "at_poc"
    if price >= poc:
        return "upper_value"
    return "lower_value"


def build_profile(bars: Sequence[Bar], start: datetime, end: datetime) -> Profile:
    start = as_utc(start)
    end = as_utc(end)
    rows: list[tuple[Bar, float]] = []
    lo = None
    hi = None
    for bar in bars:
        opened = as_utc(bar.open_time)
        if opened < start or opened >= end:
            continue
        weight = bar_weight(bar)
        if not weight > 0:
            continue
        rows.append((bar, weight))
        lo = bar.low if lo is None else min(lo, bar.low)
        hi = bar.high if hi is None else max(hi, bar.high)
    if not rows or lo is None or hi is None:
        return Profile(start=start, end=end, poc=None, vah=None, val=None, bars=0)
    step = choose_step(lo, hi)
    min_p = math.floor(lo / step) * step
    max_p = math.ceil(hi / step) * step
    if max_p <= min_p:
        max_p = min_p + step
    count = max(1, round((max_p - min_p) / step))
    if count > 160:
        step = (max_p - min_p) / 160
        count = 160
        min_p = lo
        max_p = min_p + step * count
    bins = [
        Bin(low=min_p + index * step, high=min_p + (index + 1) * step, mid=min_p + (index + 0.5) * step, total=0.0)
        for index in range(count)
    ]

    def bin_index(price: float) -> int:
        index = int((price - min_p) / step)
        if index < 0:
            return 0
        if index >= count:
            return count - 1
        return index

    for bar, weight in rows:
        i0 = bin_index(bar.low)
        i1 = bin_index(bar.high)
        if i1 < i0:
            i0, i1 = i1, i0
        share = weight / (i1 - i0 + 1)
        for index in range(i0, i1 + 1):
            bins[index].total += share
    total = sum(item.total for item in bins)
    poc, vah, val = value_area(bins, total)
    return Profile(start=start, end=end, poc=poc, vah=vah, val=val, bars=len(rows))


def with_location(profile: Profile, price: float | None) -> Profile:
    if price is None:
        return profile
    profile.location = locate(price, profile.poc, profile.vah, profile.val)
    return profile
