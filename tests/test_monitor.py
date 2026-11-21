"""The log page filters info, warnings, and errors, and today, yesterday, and last week."""

import json
import re
from datetime import datetime, timedelta, timezone
from urllib.request import urlopen

from poc.config import Settings
from poc.market import as_utc
from poc.monitor import format_when, start_monitor
from poc.store import Store, log_cutoff


def test_log_page_filters_info_warnings_and_errors(tmp_path):
    store = Store(tmp_path / "poc.sqlite")
    store.log("info", "cycle", "hourly check")
    store.log("warn", "quote", "slow quote")
    store.log("error", "positions", "positions down")
    store.error("broker rejected <id>")

    monitor = start_monitor(Settings(log_http=True, log_http_host="127.0.0.1", log_http_port=0), store)
    assert monitor is not None
    try:
        with urlopen(monitor.url + "logs.json?level=warning") as response:
            warning = json.load(response)
        assert [row["event"] for row in warning["rows"]] == ["quote"]
        assert warning["level"] == "warn"

        page = urlopen(monitor.url + "?level=error").read().decode()
        assert "positions down" in page
        assert "broker rejected &lt;id&gt;" in page
        assert "hourly check" not in page
        assert "slow quote" not in page
        assert 'href="/?level=error"' in page

        info = urlopen(monitor.url + "?level=info").read().decode()
        assert "hourly check" in info
        assert "slow quote" not in info
        assert "positions down" not in info
        assert re.search(r"\d{1,2} [A-Z][a-z]{2} \d{2}:\d{2}", info)
        assert "T" not in info.split("hourly check")[0].split("<td class=\"when\">")[-1][:20]
    finally:
        monitor.shutdown()


def test_log_page_filters_today_yesterday_and_last_week(tmp_path):
    now = as_utc(datetime.now(timezone.utc))
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    this_monday = today - timedelta(days=today.weekday())
    stamps = {
        "today row": today + timedelta(hours=9),
        "yesterday row": today - timedelta(hours=5),
        "last week row": this_monday - timedelta(days=3, hours=-4),
        "old row": log_cutoff(now) - timedelta(days=2),
    }
    store = Store(tmp_path / "poc.sqlite")
    with store._connect() as conn:
        for detail, moment in stamps.items():
            conn.execute(
                "INSERT INTO logs (created_at, level, event, detail) VALUES (?, 'info', 'cycle', ?)",
                (moment.strftime("%Y-%m-%dT%H:%M:%SZ"), detail),
            )
        conn.commit()
    assert format_when("2026-11-21T18:05:02Z") == "21 Nov 18:05"

    monitor = start_monitor(Settings(log_http=True, log_http_host="127.0.0.1", log_http_port=0), store)
    assert monitor is not None
    try:
        yesterday = urlopen(monitor.url + "?when=yesterday").read().decode()
        assert "yesterday row" in yesterday
        assert "today row" not in yesterday
        assert "last week row" not in yesterday
        assert "old row" not in yesterday

        week = urlopen(monitor.url + "?when=week").read().decode()
        assert "last week row" in week
        assert "yesterday row" not in week
        assert "today row" not in week

        kept = {row["detail"] for row in store.recent_events()}
        assert "old row" not in kept
        assert "today row" in kept
    finally:
        monitor.shutdown()


def test_log_server_stays_off_when_disabled(tmp_path):
    store = Store(tmp_path / "poc.sqlite")
    assert start_monitor(Settings(log_http=False), store) is None
