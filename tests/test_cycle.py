"""A cycle writes SQLite, places one order, and will not stack a second while one is working."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from poc.ai.client import decide
from poc.ai.prompt import system_prompt
from poc.broker import StubBroker
from poc.config import Settings
from poc.cycle import clear_market_bars, flatten, run_cycle
from poc.market import Bar
from poc.models import Decision
from poc.store import Store

NOW = datetime(2026, 11, 11, 12, tzinfo=timezone.utc)
WEEKEND = datetime(2026, 11, 14, 12, tzinfo=timezone.utc)


def _bars() -> list[Bar]:
    start = datetime(2026, 11, 3, 10, tzinfo=timezone.utc)
    rows = []
    for index in range(8):
        price = 2395 + index
        rows.append(
            Bar(
                open_time=start + timedelta(minutes=15 * index),
                open=price,
                high=price + 2,
                low=price - 1,
                close=price + 1,
                tick_count=20,
            )
        )
    return rows


def _store(tmp_path) -> Store:
    return Store(tmp_path / "poc.sqlite")


def _decision(**over) -> Decision:
    body = {
        "action": "none",
        "side": None,
        "entry_price": None,
        "stop_loss": None,
        "take_profit": None,
        "confidence": 70,
        "reasoning": "nothing at the level",
        "alert_levels": [{"price": 2410, "type": "custom"}],
        "manage_existing": [],
    }
    body.update(over)
    return Decision.model_validate(body)


def test_sqlite_has_the_five_tables_and_a_quiet_cycle_writes_them(tmp_path):
    store = _store(tmp_path)
    factor = StubBroker()
    result = run_cycle(
        settings=Settings(),
        store=store,
        factor=factor,
        now=NOW,
        decide=lambda context: _decision(),
        candles=_bars(),
        price=2400.0,
    )
    assert set(store.tables()) >= {"trades", "comments", "logs", "errors", "state"}
    assert result.final == "none"
    assert store.count("comments") >= 1
    assert store.count("logs") >= 1
    assert store.count("trades") == 0
    assert factor.orders == []
    assert store.alerts() == [{"price": 2410.0, "type": "custom"}]


def test_flat_book_can_trade_again_and_the_close_is_a_result(tmp_path):
    store = _store(tmp_path)
    factor = StubBroker()
    decision = _decision(
        action="market",
        side="buy",
        stop_loss=2380,
        take_profit=2430,
        reasoning="price is at value",
    )
    seen: list[dict] = []

    def decide(context):
        seen.append(context)
        if context.get("recent_trades"):
            return _decision(
                action="market",
                side="buy",
                stop_loss=2390,
                take_profit=2434,
                reasoning="the last buy worked",
            )
        return decision

    first = run_cycle(
        settings=Settings(trading_enabled=True),
        store=store,
        factor=factor,
        now=NOW,
        decide=decide,
        candles=_bars(),
        price=2400.0,
    )
    assert first.final == "market"
    assert factor.orders[0]["payload"]["label"] == "POC"
    assert factor.orders[0]["payload"]["volume"] == 0.01
    assert seen[0]["recent_trades"] == []
    assert seen[0]["active_trade"] is False
    still_open = run_cycle(
        settings=Settings(trading_enabled=True),
        store=store,
        factor=factor,
        now=NOW + timedelta(hours=1),
        decide=decide,
        candles=_bars(),
        price=2400.0,
    )
    assert still_open.reason == "position_open"
    assert seen[1]["active_trade"] is True
    factor.positions.clear()
    second = run_cycle(
        settings=Settings(trading_enabled=True),
        store=store,
        factor=factor,
        now=NOW + timedelta(hours=2),
        decide=decide,
        candles=_bars(),
        price=2410.0,
    )
    assert second.final == "market"
    assert len(factor.orders) == 2
    closed = seen[2]["recent_trades"][0]
    assert closed["side"] == "buy"
    assert closed["entry"] == 2400.0
    assert closed["exit"] == 2410.0
    assert closed["pnl_usd"] == 10.0
    assert closed["result"] == "win"


def test_profile_uses_ctrader_tick_counts(tmp_path):
    store = _store(tmp_path)

    class BrokerBars(StubBroker):
        def list_trendbars(self, _since):
            return [
                {
                    "open_time": "2026-11-03T10:00:00Z",
                    "open": 100.0,
                    "high": 101.0,
                    "low": 100.0,
                    "close": 100.5,
                    "tick_count": 80,
                },
                {
                    "open_time": "2026-11-03T10:15:00Z",
                    "open": 200.0,
                    "high": 201.0,
                    "low": 200.0,
                    "close": 200.5,
                    "tick_count": 1,
                },
            ]

    run_cycle(
        settings=Settings(),
        store=store,
        factor=BrokerBars(),
        now=NOW,
        decide=lambda context: _decision(),
        candles=_bars(),
        price=150.0,
    )
    import json

    with store._connect() as conn:
        row = conn.execute(
            "SELECT detail FROM logs WHERE event = 'cycle' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    detail = json.loads(row["detail"])
    assert detail["profile"] == "ctrader"
    assert detail["prior"]["poc"] == 101.0


def test_ctrader_quote_buys_the_ask_and_sells_the_bid(tmp_path):
    class Quoted(StubBroker):
        def quote(self):
            return {"bid": 2399.0, "ask": 2401.0}

    buy_book = _store(tmp_path)
    buy_broker = Quoted()
    seen: list[dict] = []
    run_cycle(
        settings=Settings(trading_enabled=True),
        store=buy_book,
        factor=buy_broker,
        now=NOW,
        decide=lambda context: seen.append(context)
        or _decision(
            action="market",
            side="buy",
            stop_loss=2380,
            take_profit=2430,
            reasoning="buy the ask",
        ),
        candles=_bars(),
    )
    assert seen[0]["price"] == 2400
    assert seen[0]["bid"] == 2399
    assert seen[0]["ask"] == 2401
    assert buy_broker.orders[0]["payload"]["entry_price"] == 2401

    sell_broker = Quoted()
    run_cycle(
        settings=Settings(trading_enabled=True),
        store=_store(tmp_path),
        factor=sell_broker,
        now=NOW,
        decide=lambda context: _decision(
            action="market",
            side="sell",
            stop_loss=2420,
            take_profit=2370,
            reasoning="sell the bid",
        ),
        candles=_bars(),
    )
    assert sell_broker.orders[0]["payload"]["entry_price"] == 2399


def test_ctrader_bars_are_loaded_once_and_used_for_the_map(tmp_path):
    clear_market_bars()
    calls = {"n": 0}

    class Bars(StubBroker):
        def quote(self):
            return {"bid": 100.2, "ask": 100.4}

        def list_trendbars(self, since, until=None):
            calls["n"] += 1
            return [
                {
                    "open_time": "2026-11-03T10:00:00Z",
                    "open": 100.0,
                    "high": 101.0,
                    "low": 100.0,
                    "close": 100.5,
                    "tick_count": 80,
                },
                {
                    "open_time": "2026-11-03T10:15:00Z",
                    "open": 100.2,
                    "high": 101.2,
                    "low": 100.1,
                    "close": 100.8,
                    "tick_count": 10,
                },
            ]

    store = _store(tmp_path)
    broker = Bars()
    for _ in range(2):
        run_cycle(
            settings=Settings(history_days=21),
            store=store,
            factor=broker,
            now=NOW,
            decide=lambda context: _decision(),
        )
    assert calls["n"] == 1
    import json

    with store._connect() as conn:
        row = conn.execute(
            "SELECT detail FROM logs WHERE event = 'cycle' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    detail = json.loads(row["detail"])
    assert detail["candles"] == "ctrader"
    assert detail["profile"] == "ctrader"
    assert detail["quote"] == "ctrader"
    clear_market_bars()


def test_an_alert_refreshes_cached_bars(tmp_path):
    clear_market_bars()
    calls = {"n": 0}

    class Bars(StubBroker):
        def quote(self):
            return {"bid": 100.2, "ask": 100.4}

        def list_trendbars(self, since, until=None):
            calls["n"] += 1
            return [
                {
                    "open_time": "2026-10-21T12:00:00Z",
                    "open": 100.0,
                    "high": 101.0,
                    "low": 100.0,
                    "close": 100.5,
                    "tick_count": 80,
                }
            ]

    store = _store(tmp_path)
    broker = Bars()
    for trigger in ("manual", "alert"):
        run_cycle(
            settings=Settings(history_days=21),
            store=store,
            factor=broker,
            now=NOW,
            trigger=trigger,
            decide=lambda context: _decision(),
        )
    assert calls["n"] == 2
    clear_market_bars()


def test_notes_name_the_ctrader_order(tmp_path):
    notes: list[str] = []
    store = _store(tmp_path)
    factor = StubBroker()
    placed = _decision(
        action="pending",
        side="sell",
        entry_price=3871.5,
        stop_loss=3878.5,
        take_profit=3854,
        reasoning="sell the level",
    )
    run_cycle(
        settings=Settings(trading_enabled=True),
        store=store,
        factor=factor,
        now=NOW,
        decide=lambda context: placed,
        candles=_bars(),
        price=3860.0,
        notify=notes.append,
    )
    assert any(
        note.startswith("POC order ord-1 limit sell 3871.5\nSL 3878.5 TP 3854.0") for note in notes
    )
    factor.positions.append(
        {
            "position_id": "696288550",
            "side": "sell",
            "volume": 0.01,
            "entry_price": 3871.61,
            "stop_loss": 3878.5,
            "take_profit": 3854,
        }
    )
    run_cycle(
        settings=Settings(),
        store=store,
        factor=factor,
        now=NOW + timedelta(minutes=14),
        decide=lambda context: _decision(),
        candles=_bars(),
        price=3871.9,
        notify=notes.append,
    )
    assert notes[-1] == (
        "POC order ord-1 filled\n"
        "sell limit 3871.5\n"
        "SL 3878.5 TP 3854.0\n"
        "position 696288550"
    )
    factor.positions.clear()
    run_cycle(
        settings=Settings(),
        store=store,
        factor=factor,
        now=NOW + timedelta(minutes=43),
        decide=lambda context: _decision(),
        candles=_bars(),
        price=3851.04,
        notify=notes.append,
    )
    assert notes[-1] == (
        "POC order ord-1 is no longer open\n"
        "sell limit 3871.5\n"
        "SL 3878.5 TP 3854.0\n"
        "position 696288550\n"
        "estimate 20.46 from quote 3851.04"
    )
    close = store.closed_trades(limit=1)[0]
    assert close["order_id"] == "ord-1"
    assert close["position_id"] == "696288550"


def test_pending_limit_is_stored(tmp_path):
    store = _store(tmp_path)
    factor = StubBroker()
    decision = _decision(
        action="pending",
        side="buy",
        entry_price=2385,
        stop_loss=2365,
        take_profit=2420,
        reasoning="wait for value",
    )
    result = run_cycle(
        settings=Settings(trading_enabled=True),
        store=store,
        factor=factor,
        now=NOW,
        decide=lambda context: decision,
        candles=_bars(),
        price=2400.0,
    )
    assert result.final == "pending"
    assert factor.orders[0]["payload"]["order_type"] == "limit"
    pending = store.pending()
    assert pending is not None
    assert pending["entry_price"] == 2385


def test_missing_pending_order_is_cleared_and_a_new_one_can_be_placed(tmp_path):
    store = _store(tmp_path)
    store.set_pending(
        {
            "order_id": "1038604245",
            "side": "sell",
            "order_type": "limit",
            "volume": 0.01,
            "entry_price": 3988.46,
            "stop_loss": 3995.0,
            "take_profit": 3974.57,
        }
    )

    class Missing(StubBroker):
        def cancel(self, payload, run_id):
            self.cancelled.append({"run_id": run_id, "payload": payload})
            return {"success": False, "message": "order not found", "order_id": payload.get("order_id")}

    factor = Missing()
    decision = _decision(
        action="pending",
        side="buy",
        entry_price=2385,
        stop_loss=2365,
        take_profit=2420,
        reasoning="the old order is gone",
        manage_existing=[{"position_id": "1038604245", "action": "cancel"}],
    )
    result = run_cycle(
        settings=Settings(trading_enabled=True),
        store=store,
        factor=factor,
        now=NOW,
        decide=lambda context: decision,
        candles=_bars(),
        price=2400.0,
    )
    assert result.final == "pending"
    assert store.pending()["order_id"] != "1038604245"
    assert store.pending()["entry_price"] == 2385
    assert len(factor.orders) == 1


def test_cancel_failure_keeps_the_pending_order(tmp_path):
    store = _store(tmp_path)
    store.set_pending(
        {
            "order_id": "1038604245",
            "side": "sell",
            "order_type": "limit",
            "entry_price": 3988.46,
        }
    )

    class Broken(StubBroker):
        def cancel(self, payload, run_id):
            self.cancelled.append({"run_id": run_id, "payload": payload})
            return {"success": False, "message": "could not check the account: timeout", "order_id": payload.get("order_id")}

    factor = Broken()
    result = run_cycle(
        settings=Settings(),
        store=store,
        factor=factor,
        now=NOW,
        decide=lambda context: _decision(
            manage_existing=[{"position_id": "1038604245", "action": "cancel"}],
        ),
        candles=_bars(),
        price=2400.0,
    )
    assert result.final == "none"
    assert store.pending()["order_id"] == "1038604245"
    assert factor.orders == []


def _close_pnl(store: Store, when: datetime, pnl: float) -> None:
    with store._connect() as conn:
        conn.execute(
            """
            INSERT INTO trades (created_at, kind, status, side, volume, entry_price, detail)
            VALUES (?, 'close', 'closed', 'buy', 0.01, 2400, ?)
            """,
            (
                when.strftime("%Y-%m-%dT%H:%M:%SZ"),
                json.dumps({"pnl_usd": pnl, "result": "loss" if pnl < 0 else "win"}),
            ),
        )


def _entry(**over) -> Decision:
    return _decision(
        action="market",
        side="buy",
        stop_loss=2380,
        take_profit=2430,
        reasoning="buy the level",
        **over,
    )


def test_todays_loss_blocks_the_next_entry(tmp_path):
    store = _store(tmp_path)
    _close_pnl(store, NOW, -25)
    factor = StubBroker()
    result = run_cycle(
        settings=Settings(trading_enabled=True),
        store=store,
        factor=factor,
        now=NOW,
        decide=lambda context: _entry(),
        candles=_bars(),
        price=2400.0,
    )
    assert result.reason == "daily_loss"
    assert factor.orders == []


def test_a_win_does_not_offset_the_daily_loss(tmp_path):
    store = _store(tmp_path)
    _close_pnl(store, NOW, -30)
    _close_pnl(store, NOW, 40)
    factor = StubBroker()
    result = run_cycle(
        settings=Settings(trading_enabled=True),
        store=store,
        factor=factor,
        now=NOW,
        decide=lambda context: _entry(),
        candles=_bars(),
        price=2400.0,
    )
    assert result.reason == "daily_loss"
    assert factor.orders == []


def test_yesterdays_loss_does_not_count(tmp_path):
    store = _store(tmp_path)
    _close_pnl(store, NOW - timedelta(days=1), -40)
    factor = StubBroker()
    result = run_cycle(
        settings=Settings(trading_enabled=True),
        store=store,
        factor=factor,
        now=NOW,
        decide=lambda context: _entry(),
        candles=_bars(),
        price=2400.0,
    )
    assert result.final == "market"
    assert len(factor.orders) == 1


def test_flat_stores_the_realized_loss(tmp_path):
    store = _store(tmp_path)

    class Quoted(StubBroker):
        def quote(self):
            return {"bid": 2390.0, "ask": 2390.0}

    factor = Quoted()
    factor.positions.append(
        {
            "position_id": "7",
            "side": "buy",
            "volume": 0.01,
            "entry_price": 2400.0,
            "stop_loss": 2380.0,
            "take_profit": 2430.0,
        }
    )
    text = flatten(Settings(), store, factor)
    assert "close 7" in text
    assert store.realized_loss_usd(datetime.now(timezone.utc).date()) == 10.0


class _Listing(StubBroker):
    def __init__(self, ids: list[str], fail: bool = False) -> None:
        super().__init__()
        self.ids = ids
        self.fail = fail

    def list_pending_orders(self):
        if self.fail:
            raise RuntimeError("factor down")
        return [{"order_id": item} for item in self.ids]


def _stored_pending() -> dict:
    return {
        "order_id": "1038604245",
        "side": "sell",
        "order_type": "limit",
        "volume": 0.01,
        "entry_price": 3988.46,
        "stop_loss": 3995.0,
        "take_profit": 3974.57,
    }


def test_a_live_pending_order_survives_a_failed_cancel(tmp_path):
    store = _store(tmp_path)
    store.set_pending(_stored_pending())

    class Broken(_Listing):
        def cancel(self, payload, run_id):
            self.cancelled.append({"run_id": run_id, "payload": payload})
            return {
                "success": False,
                "message": "could not check the account: timeout",
                "order_id": payload.get("order_id"),
            }

    factor = Broken(["1038604245"])
    result = run_cycle(
        settings=Settings(),
        store=store,
        factor=factor,
        now=NOW,
        decide=lambda context: _decision(
            manage_existing=[{"position_id": "1038604245", "action": "cancel"}],
        ),
        candles=_bars(),
        price=2400.0,
    )
    assert result.final == "none"
    assert store.pending()["order_id"] == "1038604245"
    assert factor.orders == []


def test_a_missing_working_order_is_cleared(tmp_path):
    store = _store(tmp_path)
    store.set_pending(_stored_pending())
    notes: list[str] = []
    factor = _Listing([])
    result = run_cycle(
        settings=Settings(),
        store=store,
        factor=factor,
        now=NOW,
        decide=lambda context: _decision(),
        candles=_bars(),
        price=2400.0,
        notify=notes.append,
    )
    assert result.final == "none"
    assert store.pending() is None
    assert factor.orders == []
    assert "POC order 1038604245 is no longer working\nNo position was seen" in notes
    with store._connect() as conn:
        row = conn.execute(
            "SELECT kind, status, reason FROM trades ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert row["kind"] == "cancel"
    assert row["status"] == "cleared"
    assert row["reason"] == "not_working"


def test_a_gone_pending_order_can_be_replaced(tmp_path):
    store = _store(tmp_path)
    store.set_pending(_stored_pending())
    factor = _Listing([])
    result = run_cycle(
        settings=Settings(trading_enabled=True),
        store=store,
        factor=factor,
        now=NOW,
        decide=lambda context: _decision(
            action="pending",
            side="buy",
            entry_price=2385,
            stop_loss=2365,
            take_profit=2420,
            reasoning="the old order is gone",
        ),
        candles=_bars(),
        price=2400.0,
    )
    assert result.final == "pending"
    assert store.pending()["order_id"] != "1038604245"
    assert len(factor.orders) == 1


def test_a_failed_pending_list_keeps_the_order(tmp_path):
    store = _store(tmp_path)
    store.set_pending(_stored_pending())
    factor = _Listing([], fail=True)
    result = run_cycle(
        settings=Settings(trading_enabled=True),
        store=store,
        factor=factor,
        now=NOW,
        decide=lambda context: _entry(),
        candles=_bars(),
        price=2400.0,
    )
    assert result.reason == "pending_open"
    assert store.pending()["order_id"] == "1038604245"
    assert factor.orders == []
    with store._connect() as conn:
        row = conn.execute(
            "SELECT event FROM logs WHERE event = 'pending_orders'"
        ).fetchone()
    assert row is not None


def test_weekend_does_not_call_the_model(tmp_path):
    store = _store(tmp_path)
    called = []
    result = run_cycle(
        settings=Settings(),
        store=store,
        factor=StubBroker(),
        now=WEEKEND,
        decide=lambda context: called.append(context),
        candles=_bars(),
        price=2400.0,
    )
    assert called == []
    assert "weekend" in result.message
    assert store.count("comments") == 1


def test_prompt_mentions_the_map_and_alerts():
    text = system_prompt()
    assert "POC" in text
    assert "pending" in text
    assert "alert_levels" in text
    assert "One active trade" in text
    assert "recent_trades" in text
    assert "next hour" in text


_FLAT = (
    '{"action":"none","side":null,"entry_price":null,'
    '"stop_loss":null,"take_profit":null,"volume":null,'
    '"confidence":70,"reasoning":"flat",'
    '"alert_levels":[],"manage_existing":[]}'
)


def _gemini_body() -> dict:
    return {"candidates": [{"content": {"parts": [{"text": _FLAT}]}}]}


def _grok_body() -> dict:
    return {"choices": [{"message": {"content": _FLAT}}]}


def test_gemini_retries_a_503_once_then_returns():
    calls = {"n": 0}
    waits: list[float] = []

    def post(_payload):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("503 Service Unavailable")
        return _gemini_body()

    decision = decide(Settings(gemini_api_key="test"), {}, pause=waits.append, post=post)
    assert calls["n"] == 2
    assert waits == [20.0]
    assert decision.action == "none"


def test_timeout_retries_once_then_grok_names_the_decision():
    grok_calls = {"n": 0}
    models: list[str] = []
    notes = []
    waits: list[float] = []

    def post(payload):
        models.append(payload["model"])
        raise RuntimeError("The read operation timed out")

    def grok_post(payload):
        grok_calls["n"] += 1
        assert payload["model"] == "grok-4.7"
        return _grok_body()

    decision = decide(
        Settings(gemini_api_key="test", grok_api_key="test"),
        {"price": 3880},
        pause=waits.append,
        post=post,
        grok_post=grok_post,
        on_fallback=notes.append,
    )
    assert models == [
        "gemini-3.6-flash",
        "gemini-3.6-flash",
        "gemini-4-hydrogen",
        "gemini-4-hydrogen",
    ]
    assert grok_calls["n"] == 1
    assert waits == [20.0, 20.0]
    assert decision.action == "none"
    assert notes == [
        "gemini-3.6-flash failed twice. Last error: read timeout.\n"
        "gemini-4-hydrogen failed twice. Last error: read timeout.\n"
        "grok-4.7:\nleave the book\nflat"
    ]


def test_rate_limit_moves_to_the_next_model_then_grok():
    models: list[str] = []
    waits: list[float] = []
    notes: list[str] = []

    def post(payload):
        models.append(payload["model"])
        raise RuntimeError("429 Too Many Requests")

    text = (
        '{"action":"pending","side":"sell","entry_price":3924,'
        '"stop_loss":3935,"take_profit":3900,"volume":0.01,'
        '"confidence":70,"reasoning":"sell the resistance",'
        '"alert_levels":[],"manage_existing":[{"position_id":"103","action":"cancel"}]}'
    )

    decision = decide(
        Settings(gemini_api_key="test", grok_api_key="test"),
        {},
        pause=waits.append,
        post=post,
        grok_post=lambda _payload: {"choices": [{"message": {"content": text}}]},
        on_fallback=notes.append,
    )
    assert models == ["gemini-3.6-flash", "gemini-4-hydrogen"]
    assert waits == []
    assert decision.action == "pending"
    assert notes == [
        "gemini-3.6-flash failed once. Last error: 429 Too Many Requests.\n"
        "gemini-4-hydrogen failed once. Last error: 429 Too Many Requests.\n"
        "grok-4.7:\n"
        "pending sell 3924.0\n"
        "SL 3935.0 TP 3900.0\n"
        "manage cancel 103\n"
        "sell the resistance"
    ]


def test_hydrogen_decides_after_flash_is_rate_limited():
    models: list[str] = []
    notes: list[str] = []

    def post(payload):
        models.append(payload["model"])
        if payload["model"] == "gemini-3.6-flash":
            raise RuntimeError("429 Too Many Requests")
        return _gemini_body()

    decision = decide(
        Settings(gemini_api_key="test", grok_api_key="test"),
        {},
        pause=lambda _seconds: None,
        post=post,
        grok_post=lambda _payload: (_ for _ in ()).throw(AssertionError("grok should not run")),
        on_fallback=notes.append,
    )
    assert models == ["gemini-3.6-flash", "gemini-4-hydrogen"]
    assert decision.action == "none"
    assert notes == [
        "gemini-3.6-flash failed once. Last error: 429 Too Many Requests.\n"
        "gemini-4-hydrogen:\nleave the book\nflat"
    ]


def test_gemini_success_does_not_call_grok():
    def grok_post(_payload):
        raise AssertionError("grok should not run")

    decision = decide(
        Settings(gemini_api_key="test"),
        {},
        pause=lambda _seconds: None,
        post=lambda _payload: _gemini_body(),
        grok_post=grok_post,
        on_fallback=lambda _text: (_ for _ in ()).throw(AssertionError("no fallback")),
    )
    assert decision.action == "none"


def test_both_models_fail_with_a_short_note():
    def post(_payload):
        raise RuntimeError("503 for url 'https://example.test/v1?key=SECRET'")

    with pytest.raises(RuntimeError) as caught:
        decide(
            Settings(gemini_api_key="test", grok_api_key="test"),
            {},
            pause=lambda _seconds: None,
            post=post,
            grok_post=lambda _payload: (_ for _ in ()).throw(RuntimeError("The read operation timed out")),
        )
    message = str(caught.value)
    assert "SECRET" not in message
    assert "https://" not in message
    assert "gemini-3.6-flash failed twice. Last error: 503 for url ''." in message
    assert "gemini-4-hydrogen failed twice. Last error: 503 for url ''." in message
    assert "grok-4.7 failed. Last error: read timeout." in message
    assert message.endswith("No decision this check.")


def test_grok_waits_180_seconds():
    seen: dict[str, object] = {}

    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return _grok_body()

    def fake_post(url, *, json, headers, timeout):
        seen["url"] = url
        seen["timeout"] = timeout
        assert headers["Authorization"] == "Bearer test-grok"
        return Response()

    import poc.ai.client as client

    original = client.httpx.post
    client.httpx.post = fake_post
    try:
        decision = decide(
            Settings(gemini_api_key="test", grok_api_key="test-grok"),
            {},
            pause=lambda _seconds: None,
            post=lambda _payload: (_ for _ in ()).throw(RuntimeError("503 Service Unavailable")),
        )
    finally:
        client.httpx.post = original
    assert seen["timeout"] == 180.0
    assert str(seen["url"]).endswith("/chat/completions")
    assert decision.action == "none"


def test_missing_grok_key_is_named():
    with pytest.raises(RuntimeError) as caught:
        decide(
            Settings(gemini_api_key="test"),
            {},
            pause=lambda _seconds: None,
            post=lambda _payload: (_ for _ in ()).throw(RuntimeError("unavailable")),
        )
    assert "missing XAI_API_KEY" in str(caught.value)


def test_grok_key_accepts_either_env_name(monkeypatch):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    monkeypatch.delenv("GROK_MODEL", raising=False)
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    monkeypatch.delenv("GEMINI_FALLBACK_MODEL", raising=False)
    monkeypatch.delenv("GEMINI_ATTEMPTS", raising=False)
    monkeypatch.setenv("GROK_API_KEY", "from-grok")
    monkeypatch.setenv("GEMINI_API_KEY", "present")
    settings = Settings.from_env()
    assert settings.grok_api_key == "from-grok"
    assert settings.grok_model == "grok-4.7"
    assert settings.gemini_model == "gemini-3.6-flash"
    assert settings.gemini_fallback_model == "gemini-4-hydrogen"
    assert settings.gemini_attempts == 3
    assert settings.grok_timeout_sec == 180.0
    monkeypatch.setenv("XAI_API_KEY", "from-xai")
    assert Settings.from_env().grok_api_key == "from-xai"
