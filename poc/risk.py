"""Entry checks. Size is fixed. Loss on 0.01 lot stays inside the dollar cap."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from poc.config import Settings
from poc.market import gold_market_open, weekend_closed
from poc.models import Decision, ManageItem


@dataclass
class Check:
    ok: bool
    reason: str | None = None
    volume: float | None = None
    entry: float | None = None
    stop: float | None = None
    take_profit: float | None = None
    order_type: str | None = None


def pending_order_type(side: str, entry: float, price: float) -> str:
    if side == "buy":
        return "limit" if entry < price else "stop"
    return "limit" if entry > price else "stop"


def loss_usd(entry: float, stop: float, volume: float, oz_per_lot: float) -> float:
    return abs(entry - stop) * volume * oz_per_lot


def allowed_loss(settings: Settings, volume: float) -> float:
    unit = settings.volume_lots if settings.volume_lots > 0 else 0.01
    return settings.max_loss_usd * (volume / unit)


def pnl_usd(side: str, entry: float, exit_price: float, volume: float, oz_per_lot: float) -> float:
    diff = (exit_price - entry) if side == "buy" else (entry - exit_price)
    return round(diff * volume * oz_per_lot, 2)


def pnl_result(pnl: float) -> str:
    if pnl > 0:
        return "win"
    if pnl < 0:
        return "loss"
    return "flat"


def entry_block(
    *,
    halted: bool,
    positions: list[dict[str, Any]],
    pending: dict[str, Any] | None,
    cancelling_pending: bool,
) -> str | None:
    if halted:
        return "halted"
    if positions:
        return "position_open"
    if pending and not cancelling_pending:
        return "pending_open"
    return None


def check_entry(
    decision: Decision,
    *,
    settings: Settings,
    now: datetime,
    price: float,
    positions: list[dict[str, Any]],
    profile_ready: bool,
    daily_loss: float,
) -> Check:
    if decision.action == "none":
        return Check(False, "no_trade")
    if not settings.trading_enabled:
        return Check(False, "trading_disabled")
    if not profile_ready:
        return Check(False, "no_profile")
    if decision.side not in {"buy", "sell"}:
        return Check(False, "bad_side")
    if weekend_closed(now):
        return Check(False, "market_closed")
    if decision.action == "market" and not gold_market_open(now):
        return Check(False, "market_closed")
    if decision.action == "pending" and decision.entry_price is None:
        return Check(False, "missing_entry")
    if decision.stop_loss is None or decision.take_profit is None:
        return Check(False, "missing_stop_or_target")
    if decision.confidence < settings.min_confidence:
        return Check(False, "low_confidence")
    if positions:
        return Check(False, "position_open")
    if daily_loss >= settings.max_daily_loss_usd:
        return Check(False, "daily_loss")

    entry = price if decision.action == "market" else float(decision.entry_price)  # type: ignore[arg-type]
    stop = float(decision.stop_loss)
    target = float(decision.take_profit)
    if abs(entry - price) > settings.max_entry_distance:
        return Check(False, "entry_far")
    if decision.side == "buy":
        risk = entry - stop
        reward = target - entry
    else:
        risk = stop - entry
        reward = entry - target
    if risk <= 0 or reward <= 0:
        return Check(False, "stop_side")
    if risk < settings.min_stop_usd:
        return Check(False, "stop_too_tight")
    volume = settings.volume_lots
    if loss_usd(entry, stop, volume, settings.oz_per_lot) > allowed_loss(settings, volume) + 1e-9:
        return Check(False, "loss_cap")
    if reward / risk < settings.min_rr:
        return Check(False, "rr")
    order_type = "market" if decision.action == "market" else pending_order_type(decision.side, entry, price)
    return Check(
        True,
        volume=volume,
        entry=entry,
        stop=stop,
        take_profit=target,
        order_type=order_type,
    )


def widens(side: str, current: object, new_sl: float) -> bool:
    if current is None:
        return False
    try:
        old = float(current)
    except (TypeError, ValueError):
        return False
    if side == "buy":
        return new_sl < old
    return new_sl > old


def check_manage(item: ManageItem, position: dict[str, Any] | None, *, trading_enabled: bool) -> tuple[bool, str | None, bool]:
    """Return ok, reason, and whether the action should be sent."""
    if item.action == "leave":
        return True, None, False
    if not trading_enabled and item.action != "close":
        return False, "trading_disabled", False
    if position is None:
        return False, "unknown_position", False
    if item.action == "close":
        return True, None, True
    if item.action == "move_sl_to_breakeven":
        entry = position.get("entry_price")
        if entry is None:
            return False, "missing_entry", False
        if widens(str(position.get("side")), position.get("stop_loss"), float(entry)):
            return False, "widen_stop", False
        return True, None, True
    return False, "bad_action", False
