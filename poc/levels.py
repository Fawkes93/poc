"""Swing support and resistance, plus the weekly and daily trend."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from poc.market import Bar, TF_D1, TF_W1, aggregate, completed_before, trend


@dataclass
class Level:
    price: float
    source: str


@dataclass
class Zone:
    price: float
    sources: list[str] = field(default_factory=list)
    kind: str = "mid"
    distance: float = 0.0

    def view(self) -> dict:
        return {
            "price": round(self.price, 2),
            "kind": self.kind,
            "distance": round(self.distance, 2),
            "sources": self.sources[:4],
        }


def swings(bars: Sequence[Bar], source: str, wing: int = 2, limit: int = 24) -> list[Level]:
    found: list[Level] = []
    last = len(bars) - wing
    for index in range(wing, last):
        high = bars[index].high
        low = bars[index].low
        others = list(range(index - wing, index)) + list(range(index + 1, index + wing + 1))
        if all(high > bars[other].high for other in others):
            found.append(Level(high, source))
        if all(low < bars[other].low for other in others):
            found.append(Level(low, source))
    return found[-limit:]


def cluster(levels: Sequence[Level], width: float) -> list[Zone]:
    zones: list[Zone] = []
    for level in sorted(levels, key=lambda item: item.price):
        if zones and abs(level.price - zones[-1].price) <= width:
            zone = zones[-1]
            count = len(zone.sources)
            zone.price = (zone.price * count + level.price) / (count + 1)
            zone.sources.append(level.source)
        else:
            zones.append(Zone(price=level.price, sources=[level.source]))
    return zones


def annotate(zones: Sequence[Zone], price: float) -> list[Zone]:
    out: list[Zone] = []
    for zone in zones:
        kind = "support" if zone.price < price else "resistance" if zone.price > price else "mid"
        out.append(
            Zone(
                price=round(zone.price, 2),
                sources=list(zone.sources),
                kind=kind,
                distance=round(abs(zone.price - price), 2),
            )
        )
    return out


def nearest(zones: Sequence[Zone], price: float) -> tuple[Zone | None, Zone | None]:
    support = [zone for zone in zones if zone.price < price]
    resistance = [zone for zone in zones if zone.price > price]
    near_support = max(support, key=lambda zone: zone.price) if support else None
    near_resistance = min(resistance, key=lambda zone: zone.price) if resistance else None
    return near_support, near_resistance


def build_levels(bars: Sequence[Bar], now, price: float, width: float) -> dict:
    hourly = completed_before(aggregate(bars, 60), now, 60)
    h4 = completed_before(aggregate(bars, 240), now, 240)
    daily = completed_before(aggregate(bars, TF_D1), now, TF_D1)
    weekly = completed_before(aggregate(bars, TF_W1), now, TF_W1)
    found: list[Level] = []
    found.extend(swings(hourly, "h1"))
    found.extend(swings(h4, "h4"))
    found.extend(swings(daily, "d1"))
    zones = annotate(cluster(found, width), price)
    zones.sort(key=lambda zone: zone.distance)
    support, resistance = nearest(zones, price)
    prior_day = daily[-1] if daily else None
    prior_week = weekly[-1] if weekly else None
    return {
        "trend": {
            "w1": trend(weekly, minimum=4),
            "d1": trend(daily, minimum=5),
        },
        "nearest_support": None if support is None else support.price,
        "nearest_resistance": None if resistance is None else resistance.price,
        "zones": [zone.view() for zone in zones[:8]],
        "prior_day": None
        if prior_day is None
        else {"high": round(prior_day.high, 2), "low": round(prior_day.low, 2)},
        "prior_week_range": None
        if prior_week is None
        else {"high": round(prior_week.high, 2), "low": round(prior_week.low, 2)},
    }
