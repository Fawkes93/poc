"""The log page filters info, warnings, and errors."""

import json
from urllib.request import urlopen

from poc.config import Settings
from poc.monitor import start_monitor
from poc.store import Store


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
    finally:
        monitor.shutdown()


def test_log_server_stays_off_when_disabled(tmp_path):
    store = Store(tmp_path / "poc.sqlite")
    assert start_monitor(Settings(log_http=False), store) is None
