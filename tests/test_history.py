"""Saved 15-minute bars. A restart asks cTrader only for the gap."""

from datetime import timedelta

from poc.config import Settings
from poc.cycle import clear_market_bars
from poc.history import missing_ranges, sync_bars
from poc.market import Bar
from poc.store import Store
from tests.test_cycle import NOW, StubBroker, _decision, _store, run_cycle


def test_missing_ranges_start_with_the_newest_slice():
    end = NOW
    start = NOW - timedelta(days=50)
    ranges = missing_ranges(start, end, None, None, slice_days=21)
    assert ranges[0] == (end - timedelta(days=21), end)
    assert ranges[-1][0] == start
    assert ranges[-1][1] == end - timedelta(days=42)


def test_a_full_archive_asks_only_for_the_tail():
    start = NOW - timedelta(days=120)
    newest = NOW - timedelta(hours=1)
    ranges = missing_ranges(start, NOW, start, newest)
    assert ranges == [(newest, NOW)]


def test_bars_round_trip_and_prune(tmp_path):
    store = Store(tmp_path / "poc.sqlite")
    store.save_bars(
        [
            Bar(NOW - timedelta(days=200), 1, 2, 0.5, 1.2, 4, None),
            Bar(NOW - timedelta(hours=2), 10, 11, 9, 10.5, 8, None),
        ]
    )
    store.prune_bars(NOW - timedelta(days=120))
    kept = store.load_bars(NOW - timedelta(days=120))
    assert len(kept) == 1
    assert kept[0].close == 10.5
    assert kept[0].tick_count == 8


def test_restart_fetches_only_the_gap(tmp_path):
    clear_market_bars()
    store = _store(tmp_path)
    start = NOW - timedelta(days=120)
    newest = NOW - timedelta(hours=1)
    store.save_bars(
        [
            Bar(start, 100, 101, 99, 100.4, 20, None),
            Bar(newest, 100.2, 101.2, 100.1, 100.8, 10, None),
        ]
    )
    calls: list[tuple] = []

    class Bars(StubBroker):
        def quote(self):
            return {"bid": 100.2, "ask": 100.4}

        def list_trendbars(self, since, until=None):
            calls.append((since, until))
            return [
                {
                    "open_time": newest.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "open": 100.2,
                    "high": 101.4,
                    "low": 100.1,
                    "close": 101.0,
                    "tick_count": 15,
                }
            ]

    run_cycle(
        settings=Settings(history_days=120),
        store=store,
        factor=Bars(),
        now=NOW,
        decide=lambda context: _decision(),
    )
    assert calls == [(newest, NOW)]
    saved = store.load_bars(start)
    assert saved[-1].close == 101.0
    assert saved[-1].tick_count == 15
    clear_market_bars()


def test_sync_stops_when_an_older_slice_fails(tmp_path):
    store = Store(tmp_path / "bars.sqlite")
    calls: list[tuple] = []

    class Bars:
        def list_trendbars(self, since, until=None):
            calls.append((since, until))
            if len(calls) > 1:
                raise RuntimeError("blocked")
            return [
                {
                    "open_time": (NOW - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "open": 1,
                    "high": 2,
                    "low": 0.5,
                    "close": 1.5,
                    "tick_count": 3,
                }
            ]

    bars = sync_bars(Bars(), Settings(history_days=50), store, NOW)
    assert len(calls) == 2
    assert bars[-1].close == 1.5
    assert store.load_bars(NOW - timedelta(days=50))[-1].tick_count == 3
