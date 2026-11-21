"""Risk rails: $25 on 0.01 lot, one position, and the gold clock."""

from datetime import datetime, timezone

from poc.config import Settings
from poc.models import Decision
from poc.risk import check_entry, entry_block

OPEN = datetime(2026, 11, 11, 12, tzinfo=timezone.utc)
WEEKEND = datetime(2026, 11, 14, 12, tzinfo=timezone.utc)
BREAK = datetime(2026, 11, 12, 21, 30, tzinfo=timezone.utc)
SETTINGS = Settings(trading_enabled=True)


def _decision(**over) -> Decision:
    body = {
        "action": "market",
        "side": "buy",
        "stop_loss": 2380.0,
        "take_profit": 2430.0,
        "confidence": 70,
        "reasoning": "buy the value low",
    }
    body.update(over)
    return Decision.model_validate(body)


def _check(decision: Decision, **over):
    args = {
        "settings": SETTINGS,
        "now": OPEN,
        "price": 2400.0,
        "positions": [],
        "profile_ready": True,
        "daily_loss": 0.0,
    }
    args.update(over)
    return check_entry(decision, **args)


def test_a_twenty_dollar_stop_passes():
    result = _check(_decision())
    assert result.ok
    assert result.volume == 0.01
    assert result.order_type == "market"
    assert result.entry == 2400


def test_stop_closer_than_five_dollars_is_rejected():
    result = _check(_decision(stop_loss=2397, take_profit=2410))
    assert result.reason == "stop_too_tight"


def test_loss_above_twenty_five_dollars_is_rejected():
    result = _check(_decision(stop_loss=2370, take_profit=2450))
    assert result.reason == "loss_cap"


def test_low_reward_is_rejected():
    result = _check(_decision(stop_loss=2390, take_profit=2408))
    assert result.reason == "rr"


def test_pending_buy_below_price_is_a_limit():
    result = _check(
        _decision(action="pending", entry_price=2385, stop_loss=2365, take_profit=2420)
    )
    assert result.ok
    assert result.order_type == "limit"
    assert result.entry == 2385


def test_pending_buy_above_price_is_a_stop():
    result = _check(
        _decision(action="pending", entry_price=2410, stop_loss=2390, take_profit=2440)
    )
    assert result.ok
    assert result.order_type == "stop"


def test_second_position_weekend_break_and_halt():
    assert _check(_decision(), positions=[{"position_id": "1"}]).reason == "position_open"
    assert _check(_decision(), now=WEEKEND).reason == "market_closed"
    assert _check(_decision(), now=BREAK).reason == "market_closed"
    pending = _check(
        _decision(action="pending", entry_price=2385, stop_loss=2365, take_profit=2420),
        now=BREAK,
    )
    assert pending.ok
    assert entry_block(
        halted=True, positions=[], pending=None, cancelling_pending=False
    ) == "halted"
    assert entry_block(
        halted=False, positions=[], pending=None, cancelling_pending=False
    ) is None
    assert entry_block(
        halted=False,
        positions=[],
        pending={"order_id": "9"},
        cancelling_pending=False,
    ) == "pending_open"
    assert (
        entry_block(
            halted=False,
            positions=[],
            pending=None,
            cancelling_pending=True,
        )
        is None
    )
