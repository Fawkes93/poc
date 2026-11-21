"""Checks land each hour, and a crossed alert wakes one cycle."""

from datetime import datetime, timezone

from poc.config import Settings
from poc.scheduler import crossed, next_check, poll_price_alerts
from poc.store import Store


def test_next_check_is_every_hour_at_minute_five():
    assert next_check(datetime(2026, 11, 11, 10, 0, tzinfo=timezone.utc)) == datetime(
        2026, 11, 11, 10, 5, tzinfo=timezone.utc
    )
    assert next_check(datetime(2026, 11, 11, 10, 6, tzinfo=timezone.utc)) == datetime(
        2026, 11, 11, 11, 5, tzinfo=timezone.utc
    )
    assert next_check(datetime(2026, 11, 11, 11, 0, tzinfo=timezone.utc)) == datetime(
        2026, 11, 11, 11, 5, tzinfo=timezone.utc
    )
    assert next_check(datetime(2026, 11, 11, 23, 10, tzinfo=timezone.utc)) == datetime(
        2026, 11, 12, 0, 5, tzinfo=timezone.utc
    )


def test_cross_detects_a_level_between_two_prints():
    assert crossed(99, 101, 100)
    assert crossed(101, 99, 100)
    assert not crossed(100, 100, 100)
    assert not crossed(98, 99, 100)


class _Result:
    busy = False


def test_alert_poll_wakes_once_then_waits(tmp_path):
    store = Store(tmp_path / "poc.sqlite")
    store.set_alerts([{"price": 100, "type": "custom"}])
    calls = []
    settings = Settings(alert_debounce_sec=600)
    state: dict = {}
    now = datetime(2026, 11, 11, 12, tzinfo=timezone.utc)

    def run(**kwargs):
        calls.append(kwargs)
        return _Result()

    assert poll_price_alerts(settings, store, None, lambda: 99, state, run, now) is False
    assert calls == []
    later = datetime(2026, 11, 11, 12, 1, tzinfo=timezone.utc)
    assert poll_price_alerts(settings, store, None, lambda: 101, state, run, later) is True
    assert calls[0]["trigger"] == "alert"
    again = datetime(2026, 11, 11, 12, 2, tzinfo=timezone.utc)
    assert poll_price_alerts(settings, store, None, lambda: 102, state, run, again) is False
    assert len(calls) == 1
