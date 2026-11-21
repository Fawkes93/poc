"""Factor HTTP calls. Each action in one run gets its own idempotency key."""

import pytest

from poc.broker import HttpBroker
from poc.config import Settings


class _Response:
    def __init__(self, body: dict, status: int = 200) -> None:
        self.status_code = status
        self.text = str(body)
        self._body = body

    def json(self) -> dict:
        return self._body


class _Http:
    def __init__(self, body: dict | None = None, status: int = 200) -> None:
        self.body = {"success": True, "message": "ok", "order_id": "1"} if body is None else body
        self.status = status
        self.posts: list[str] = []
        self.gets: list[tuple] = []

    def post(self, url: str, json: dict, headers: dict) -> _Response:
        self.posts.append(headers["Idempotency-Key"])
        return _Response({"success": True, "message": "ok", "order_id": "1"})

    def get(self, url: str, params: dict, headers: dict) -> _Response:
        self.gets.append((url, params))
        return _Response(self.body, self.status)


def _broker(http: _Http) -> HttpBroker:
    return HttpBroker(
        Settings(factor_api_token="token", factor_base_url="http://factor", factor_account_id="7"),
        http,
    )


def test_each_action_has_its_own_idempotency_key():
    http = _Http()
    broker = _broker(http)
    broker.cancel({"order_id": "9"}, "run")
    broker.place({"side": "buy"}, "run")
    broker.manage({"position_id": "3", "action": "close"}, "run")
    broker.manage({"position_id": "4", "action": "close"}, "run")
    assert http.posts == [
        "run:cancel:9",
        "run:place",
        "run:manage:3:close",
        "run:manage:4:close",
    ]


def test_pending_orders_come_from_the_orders_list():
    http = _Http({"success": True, "orders": [{"order_id": "9"}]})
    rows = _broker(http).list_pending_orders()
    assert rows == [{"order_id": "9"}]
    assert http.gets[0][0] == "http://factor/api/ai/orders"
    assert http.gets[0][1]["account_id"] == "7"


def test_a_failed_pending_list_raises():
    http = _Http({"success": False, "orders": [], "message": "down"})
    with pytest.raises(RuntimeError, match="down"):
        _broker(http).list_pending_orders()
