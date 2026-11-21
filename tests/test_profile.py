"""Volume profile matches the Reaktor bin rule, and weeks start on Monday in Bucharest."""

from datetime import datetime, timezone

from poc.market import Bar
from poc.profile import Bin, build_profile, previous_week, value_area, with_location


def test_value_area_expands_toward_the_heavier_neighbor():
    bins = [
        Bin(low=0, high=1, mid=0.5, total=1),
        Bin(low=1, high=2, mid=1.5, total=10),
        Bin(low=2, high=3, mid=2.5, total=3),
        Bin(low=3, high=4, mid=3.5, total=1),
    ]
    poc, vah, val = value_area(bins, 15)
    assert poc == 1.5
    assert vah == 3
    assert val == 1


def test_ties_expand_down():
    bins = [
        Bin(low=0, high=1, mid=0.5, total=3),
        Bin(low=1, high=2, mid=1.5, total=10),
        Bin(low=2, high=3, mid=2.5, total=3),
    ]
    poc, vah, val = value_area(bins, 16)
    assert poc == 1.5
    assert val == 0
    assert vah == 2


def test_profile_uses_tick_count_and_skips_empty_bars():
    start = datetime(2026, 11, 2, tzinfo=timezone.utc)
    end = datetime(2026, 11, 9, tzinfo=timezone.utc)
    heavy = Bar(
        open_time=datetime(2026, 11, 4, 12, tzinfo=timezone.utc),
        open=100,
        high=101,
        low=100,
        close=101,
        tick_count=50,
    )
    light = Bar(
        open_time=datetime(2026, 11, 4, 13, tzinfo=timezone.utc),
        open=90,
        high=91,
        low=90,
        close=90,
        tick_count=1,
    )
    ignored = Bar(
        open_time=datetime(2026, 11, 4, 14, tzinfo=timezone.utc),
        open=200,
        high=201,
        low=200,
        close=200,
        tick_count=0,
        volume=999,
    )
    profile = with_location(build_profile([heavy, light, ignored], start, end), 100.5)
    assert profile.bars == 2
    assert profile.poc is not None
    assert profile.val is not None and profile.vah is not None
    assert profile.val <= profile.poc <= profile.vah
    assert abs(profile.poc - 100.5) < abs(profile.poc - 90.5)
    assert profile.location == "at_poc"


def test_previous_week_is_monday_to_monday_in_bucharest():
    now = datetime(2026, 11, 11, 12, tzinfo=timezone.utc)
    start, end = previous_week(now)
    assert start.isoformat() == "2026-11-02T00:00:00+02:00"
    assert end.isoformat() == "2026-11-09T00:00:00+02:00"
    assert start.astimezone(timezone.utc).hour == 22
    assert end.astimezone(timezone.utc).hour == 22
