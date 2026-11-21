"""One check. The hourly schedule, a price alert, and --once all enter here."""

from __future__ import annotations

import json
import threading
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from uuid import uuid4

from poc.ai.client import decide as decide_with_gemini
from poc.candles import bars_from_trendbars, latest_close, load_recent
from poc.config import Settings
from poc.levels import build_levels
from poc.market import as_utc, session_name, weekend_closed
from poc.models import Decision, ManageItem
from poc.profile import build_profile, developing_week, previous_week, with_location
from poc.quote import latest_price, spot_quote
from poc.risk import check_entry, check_manage, entry_block, pnl_result, pnl_usd
from poc.store import Store

_LOCK = threading.Lock()
_BAR_CACHE_SEC = 50 * 60
_bar_lock = threading.Lock()
_bar_cache: dict[str, Any] = {"at": 0.0, "bars": None, "source": ""}


def clear_market_bars() -> None:
    with _bar_lock:
        _bar_cache["at"] = 0.0
        _bar_cache["bars"] = None
        _bar_cache["source"] = ""


def _cached_bars() -> tuple[list, str] | None:
    with _bar_lock:
        bars = _bar_cache["bars"]
        stored = _bar_cache["at"]
        source = _bar_cache["source"]
    if not bars or not stored:
        return None
    if time.monotonic() - stored > _BAR_CACHE_SEC:
        return None
    return list(bars), str(source)


def _store_bars(bars: list, source: str) -> None:
    with _bar_lock:
        _bar_cache["at"] = time.monotonic()
        _bar_cache["bars"] = list(bars)
        _bar_cache["source"] = source


def _side_price(side: str | None, bid: float | None, ask: float | None, price: float) -> float:
    if side == "buy" and ask is not None:
        return float(ask)
    if side == "sell" and bid is not None:
        return float(bid)
    return float(price)


@dataclass
class CycleResult:
    busy: bool = False
    run_id: str = ""
    final: str = "none"
    reason: str | None = None
    message: str = ""
    price: float | None = None


def _iso(now: datetime) -> str:
    return as_utc(now).strftime("%Y-%m-%dT%H:%M:%SZ")


def _public_position(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "position_id": str(row.get("position_id")),
        "side": row.get("side"),
        "volume": row.get("volume"),
        "entry_price": row.get("entry_price"),
        "stop_loss": row.get("stop_loss"),
        "take_profit": row.get("take_profit"),
        "unrealized_pnl": row.get("unrealized_pnl"),
    }


def _alerts_from(decision: Decision, price: float, limit: int) -> list[dict[str, Any]]:
    kept: list[dict[str, Any]] = []
    seen: list[float] = []
    for level in decision.alert_levels:
        if len(kept) >= limit:
            break
        if abs(level.price - price) < 1:
            continue
        if any(abs(level.price - previous) < 0.5 for previous in seen):
            continue
        seen.append(level.price)
        kept.append({"price": round(level.price, 2), "type": level.type})
    return kept


def _maybe_heartbeat(
    settings: Settings,
    store: Store,
    notify: Callable[[str], None] | None,
    text: str,
    now: datetime,
) -> None:
    raw = store.get("last_heartbeat", "")
    due = True
    if raw:
        try:
            last = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            due = now - last >= timedelta(hours=settings.heartbeat_hours)
        except ValueError:
            due = True
    if not due or notify is None:
        return
    notify(text)
    store.set("last_heartbeat", _iso(now))
    store.log("info", "heartbeat", text)


def _notify_fallback(
    store: Store,
    notify: Callable[[str], None] | None,
    run_id: str,
    text: str,
) -> None:
    store.log("warn", "model_fallback", text)
    store.comment(run_id, "system", text)
    if notify is not None:
        notify(f"POC\n{text}")


def _notify_error(store: Store, notify: Callable[[str], None] | None, message: str, now: datetime) -> None:
    if notify is None:
        return
    stamp = store.get("last_error_at", "")
    if store.get("last_error_sig") == message[:180] and stamp:
        try:
            last = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            if now - last < timedelta(hours=1):
                return
        except ValueError:
            pass
    notify(f"POC error\n{message[:500]}")
    store.set("last_error_sig", message[:180])
    store.set("last_error_at", _iso(now))


def _market_bars(factor: Any, settings: Settings, store: Store, now: datetime) -> tuple[list, str]:
    """One 15-minute load for the map. cTrader is cached so the price poll does not pull history."""
    cached = _cached_bars()
    if cached is not None:
        return cached
    loader = getattr(factor, "list_trendbars", None)
    if loader is not None:
        since = as_utc(now) - timedelta(days=settings.history_days)
        failed = False
        try:
            rows = loader(since)
            bars = bars_from_trendbars(rows if isinstance(rows, list) else [])
        except Exception as exc:
            failed = True
            store.log("warn", "profile_bars", str(exc)[:300])
            bars = []
        if bars:
            _store_bars(bars, "ctrader")
            return bars, "ctrader"
        if not failed:
            store.log("warn", "profile_bars", "cTrader returned no 15-minute bars")
    try:
        bars = load_recent(settings, now)
    except Exception as exc:
        store.log("warn", "profile_bars", str(exc)[:300])
        return [], "reaktor"
    return bars, "reaktor" if bars else "none"


def _profile_bars(
    factor: Any,
    settings: Settings,
    store: Store,
    now: datetime,
    candles: list,
) -> tuple[list, str]:
    """Value area uses cTrader 15-minute ticks. A failed load keeps the Reaktor bars."""
    loader = getattr(factor, "list_trendbars", None)
    if loader is None:
        return candles, "reaktor"
    since = as_utc(now) - timedelta(days=settings.history_days)
    try:
        rows = loader(since)
    except Exception as exc:
        store.log("warn", "profile_bars", str(exc)[:300])
        return candles, "reaktor"
    bars = bars_from_trendbars(rows if isinstance(rows, list) else [])
    if not bars:
        store.log("warn", "profile_bars", "cTrader returned no 15-minute bars")
        return candles, "reaktor"
    return bars, "ctrader"


def _load_positions(factor: Any) -> tuple[list[dict[str, Any]] | None, str | None]:
    try:
        rows = factor.list_positions()
    except Exception as exc:
        return None, str(exc)
    if not isinstance(rows, list):
        return None, "positions payload was not a list"
    return [row for row in rows if isinstance(row, dict)], None


def _number(value: object) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _side(value: object) -> str | None:
    text = str(value or "").strip().lower()
    if text in {"buy", "sell"}:
        return text
    return None


def _recent_results(store: Store, limit: int = 8) -> list[dict[str, Any]]:
    rows = []
    for row in store.closed_trades(limit):
        try:
            detail = json.loads(row.get("detail") or "{}")
        except json.JSONDecodeError:
            detail = {}
        if not isinstance(detail, dict):
            detail = {}
        result = detail.get("result")
        if result not in {"win", "loss", "flat"}:
            continue
        rows.append(
            {
                "closed_at": row.get("created_at"),
                "side": row.get("side"),
                "entry": row.get("entry_price"),
                "exit": detail.get("exit_price"),
                "pnl_usd": detail.get("pnl_usd"),
                "result": result,
            }
        )
    return rows


def _last_result(store: Store) -> str:
    rows = _recent_results(store, limit=1)
    if not rows:
        return "none"
    pnl = rows[0].get("pnl_usd")
    if pnl is None:
        return str(rows[0]["result"])
    return f"{rows[0]['result']} {pnl}"


def _level_line(stop: object, take_profit: object) -> str | None:
    if stop is None and take_profit is None:
        return None
    return f"SL {stop} TP {take_profit}"


def _close_text(
    *,
    order_id: object,
    position_id: str,
    side: str | None,
    order_type: object,
    entry: float | None,
    stop: object,
    take_profit: object,
    exit_price: float | None,
    pnl: float | None,
    reason: str,
) -> str:
    """Name the cTrader order. The dollar figure is the quote, not the broker fill."""
    if order_id and reason == "missing":
        lines = [f"POC order {order_id} is no longer open"]
    elif order_id:
        lines = [f"POC order {order_id} closed"]
    else:
        lines = [f"POC position {position_id} closed"]
    trade = " ".join(part for part in (side or "", str(order_type or "")) if part)
    if entry is not None:
        lines.append(f"{trade} {entry}".strip())
    levels = _level_line(stop, take_profit)
    if levels:
        lines.append(levels)
    if order_id:
        lines.append(f"position {position_id}")
    if pnl is not None and exit_price is not None:
        lines.append(f"estimate {pnl} from quote {exit_price}")
    return "\n".join(lines)


def _record_close(
    store: Store,
    *,
    position_id: str,
    exit_price: float | None,
    reason: str,
    run_id: str | None,
    oz_per_lot: float,
    notify: Callable[[str], None] | None,
    position: dict[str, Any] | None = None,
) -> None:
    fill = store.latest_fill(position_id) or {}
    source = position or {}
    side = _side(fill.get("side")) or _side(source.get("side"))
    entry = _number(fill.get("entry_price"))
    if entry is None:
        entry = _number(source.get("entry_price"))
    volume = _number(fill.get("volume"))
    if volume is None:
        volume = _number(source.get("volume"))
    pnl = None
    result = None
    if side and entry is not None and exit_price is not None and volume is not None:
        pnl = pnl_usd(side, entry, exit_price, volume, oz_per_lot)
        result = pnl_result(pnl)
    stop = _number(fill.get("stop_loss")) or _number(source.get("stop_loss"))
    take_profit = _number(fill.get("take_profit")) or _number(source.get("take_profit"))
    order_id = fill.get("order_id")
    store.trade(
        run_id=run_id,
        kind="close",
        status="closed",
        reason=reason,
        side=side,
        order_type=fill.get("order_type"),
        volume=volume,
        entry_price=entry,
        stop_loss=stop,
        take_profit=take_profit,
        position_id=position_id,
        order_id=None if order_id is None else str(order_id),
        detail={"exit_price": exit_price, "pnl_usd": pnl, "result": result},
    )
    if notify is not None:
        notify(
            _close_text(
                order_id=order_id,
                position_id=position_id,
                side=side,
                order_type=fill.get("order_type"),
                entry=entry,
                stop=stop,
                take_profit=take_profit,
                exit_price=exit_price,
                pnl=pnl,
                reason=reason,
            )
        )


def _reconcile(
    store: Store,
    positions: list[dict[str, Any]],
    price: float | None,
    now: datetime,
    notify: Callable[[str], None] | None,
    oz_per_lot: float,
) -> list[str]:
    current = {str(row.get("position_id")) for row in positions if row.get("position_id")}
    previous = set(store.open_ids())
    closed = sorted(previous - current)
    for position_id in closed:
        _record_close(
            store,
            position_id=position_id,
            exit_price=price,
            reason="missing",
            run_id=None,
            oz_per_lot=oz_per_lot,
            notify=notify,
        )
    appeared = current - previous
    pending = store.pending()
    if pending and appeared:
        store.set_pending(None)
        store.set("last_fill_day", as_utc(now).date().isoformat())
        store.trade(
            kind="fill",
            status="filled",
            reason="pending_filled",
            side=pending.get("side"),
            order_type=pending.get("order_type"),
            volume=pending.get("volume"),
            entry_price=pending.get("entry_price"),
            stop_loss=pending.get("stop_loss"),
            take_profit=pending.get("take_profit"),
            order_id=pending.get("order_id"),
            position_id=sorted(appeared)[0],
        )
        if notify is not None:
            position_id = sorted(appeared)[0]
            notify(
                "\n".join(
                    line
                    for line in (
                        f"POC order {pending.get('order_id')} filled",
                        " ".join(
                            part
                            for part in (
                                str(pending.get("side") or ""),
                                str(pending.get("order_type") or ""),
                                str(pending.get("entry_price") or ""),
                            )
                            if part
                        ),
                        _level_line(pending.get("stop_loss"), pending.get("take_profit")),
                        f"position {position_id}",
                    )
                    if line
                )
            )
    store.set_open_ids(sorted(current))
    return closed


def _heartbeat_text(
    *,
    price: float | None,
    prior: dict[str, Any],
    trend: dict[str, Any],
    positions: list[dict[str, Any]] | None,
    pending: dict[str, Any] | None,
    last_result: str,
    note: str,
) -> str:
    pos = "unknown" if positions is None else ("flat" if not positions else str(positions[0].get("position_id")))
    working = "none" if not pending else f"{pending.get('side')} {pending.get('entry_price')}"
    return (
        f"POC {price}\n"
        f"week POC {prior.get('poc')} VAH {prior.get('vah')} VAL {prior.get('val')} ({prior.get('location')})\n"
        f"trend W1 {trend.get('w1')} D1 {trend.get('d1')}\n"
        f"position {pos} pending {working}\n"
        f"last {last_result}\n"
        f"{note}"
    )


def status_text(settings: Settings, store: Store, factor: Any) -> str:
    positions, error = _load_positions(factor)
    pending = store.pending()
    lines = [
        f"trading {settings.trading_enabled} mode {settings.factor_mode} label {settings.label}",
        f"halted {store.get('halted', '0') == '1'}",
        f"last result {_last_result(store)}",
        f"pending {pending or 'none'}",
        f"alerts {len(store.alerts())}",
    ]
    if error:
        lines.append(f"positions {error}")
    elif not positions:
        lines.append("positions none")
    else:
        for row in positions:
            lines.append(
                f"position {row.get('position_id')} {row.get('side')} {row.get('entry_price')}"
            )
    return "\n".join(lines)


def flatten(settings: Settings, store: Store, factor: Any) -> str:
    """Close POC positions and cancel the working pending order."""
    lines: list[str] = []
    run_id = uuid4().hex
    positions, error = _load_positions(factor)
    if error:
        store.error(error, run_id=run_id)
        lines.append(f"positions {error}")
    else:
        closed_ids: list[str] = []
        for row in positions or []:
            payload = {
                "account_id": settings.account_id_int(),
                "position_id": row.get("position_id"),
                "action": "close",
                "label": settings.label,
            }
            sent = factor.manage(payload, run_id)
            lines.append(f"close {row.get('position_id')} {sent.get('message')}")
            store.trade(
                run_id=run_id,
                kind="close",
                status="closed" if sent.get("success") else "rejected",
                reason=sent.get("message"),
                position_id=str(row.get("position_id")),
                side=row.get("side"),
            )
            if sent.get("success"):
                closed_ids.append(str(row.get("position_id")))
        store.set_open_ids([item for item in store.open_ids() if item not in closed_ids])
    pending = store.pending()
    if pending and pending.get("order_id"):
        sent = factor.cancel(
            {
                "order_id": pending["order_id"],
                "label": settings.label,
                "account_id": settings.account_id_int(),
            },
            run_id,
        )
        lines.append(f"cancel {pending['order_id']} {sent.get('message')}")
        if sent.get("success"):
            store.set_pending(None)
            store.trade(
                run_id=run_id,
                kind="cancel",
                status="cancelled",
                order_id=str(pending["order_id"]),
                side=pending.get("side"),
            )
    if not lines:
        return "POC flat, nothing open"
    store.log("info", "flat", "\n".join(lines))
    return "\n".join(lines)


def run_cycle(
    *,
    settings: Settings,
    store: Store,
    factor: Any,
    now: datetime | None = None,
    trigger: str = "manual",
    decide: Callable[..., Decision] | None = None,
    candles: list | None = None,
    price: float | None = None,
    notify: Callable[[str], None] | None = None,
) -> CycleResult:
    if not _LOCK.acquire(blocking=False):
        return CycleResult(busy=True, message="busy")
    now = as_utc(now or datetime.now(timezone.utc))
    run_id = uuid4().hex
    result = CycleResult(run_id=run_id)
    try:
        result = _run(
            settings=settings,
            store=store,
            factor=factor,
            now=now,
            trigger=trigger,
            decide=decide,
            candles=candles,
            price=price,
            notify=notify,
            run_id=run_id,
        )
        return result
    except Exception as exc:
        detail = traceback.format_exc()
        store.error(str(exc), detail, run_id)
        store.log("error", "cycle_failed", str(exc))
        _notify_error(store, notify, str(exc), now)
        result.reason = "error"
        result.message = str(exc)
        return result
    finally:
        _LOCK.release()


def _run(
    *,
    settings: Settings,
    store: Store,
    factor: Any,
    now: datetime,
    trigger: str,
    decide: Callable[..., Decision] | None,
    candles: list | None,
    price: float | None,
    notify: Callable[[str], None] | None,
    run_id: str,
) -> CycleResult:
    result = CycleResult(run_id=run_id)
    quote_source = "given"
    candle_source = "given"
    bid: float | None = None
    ask: float | None = None
    if candles is None:
        candles, candle_source = _market_bars(factor, settings, store, now)
    if price is None:
        spot = None
        try:
            spot = spot_quote(factor)
        except Exception as exc:
            store.log("warn", "quote", str(exc)[:300])
        if spot is not None:
            bid = spot["bid"]
            ask = spot["ask"]
            price = (bid + ask) / 2
            quote_source = "ctrader"
        else:
            try:
                price = latest_price(settings)
                quote_source = "twelvedata"
            except Exception as exc:
                price = latest_close(candles)
                quote_source = "candle_close"
                store.log("warn", "quote", str(exc)[:300])
    result.price = price
    if price is None:
        result.reason = "no_price"
        result.message = "no price"
        store.error("no price", run_id=run_id)
        _notify_error(store, notify, "no price", now)
        return result

    week_start, week_end = previous_week(now)
    dev_start, dev_end = developing_week(now)
    if candle_source == "given":
        profile_bars, profile_source = _profile_bars(factor, settings, store, now, candles)
    else:
        profile_bars, profile_source = candles, candle_source
    prior = with_location(build_profile(profile_bars, week_start, week_end), price)
    developing = with_location(build_profile(profile_bars, dev_start, dev_end), price)
    levels = build_levels(candles, now, price, settings.sr_cluster_usd)
    positions, position_error = _load_positions(factor)
    if position_error:
        store.error(position_error, run_id=run_id)
        store.log("error", "positions", position_error)
    else:
        _reconcile(store, positions or [], price, now, notify, settings.oz_per_lot)
        positions, position_error = _load_positions(factor)
    pending = store.pending()
    halted = store.get("halted", "0") == "1"
    store.log(
        "info",
        "cycle",
        {
            "run_id": run_id,
            "trigger": trigger,
            "price": price,
            "quote": quote_source,
            "candles": candle_source,
            "profile": profile_source,
            "prior": prior.view(),
            "developing": developing.view(),
            "trend": levels["trend"],
        },
    )

    if weekend_closed(now):
        note = "weekend, book left with its stop"
        result.message = note
        store.comment(run_id, "system", note)
        _maybe_heartbeat(
            settings,
            store,
            notify,
            _heartbeat_text(
                price=price,
                prior=prior.view(),
                trend=levels["trend"],
                positions=positions,
                pending=pending,
                last_result=_last_result(store),
                note=note,
            ),
            now,
        )
        return result

    context = {
        "now": _iso(now),
        "session": session_name(now),
        "weekend": False,
        "market_open": session_name(now) != "closed",
        "price": price,
        "bid": price if bid is None else bid,
        "ask": price if ask is None else ask,
        "prior_week": prior.view(),
        "developing_week": developing.view(),
        "trend": levels["trend"],
        "nearest_support": levels["nearest_support"],
        "nearest_resistance": levels["nearest_resistance"],
        "zones": levels["zones"],
        "prior_day": levels["prior_day"],
        "prior_week_range": levels["prior_week_range"],
        "position": None if not positions else _public_position(positions[0]),
        "positions_unknown": position_error is not None,
        "pending": pending,
        "active_trade": bool(positions) or pending is not None,
        "recent_trades": _recent_results(store),
        "halted": halted,
        "max_loss_usd": settings.max_loss_usd,
        "min_stop_usd": settings.min_stop_usd,
        "min_rr": settings.min_rr,
        "volume_lots": settings.volume_lots,
        "min_confidence": settings.min_confidence,
    }
    def _choose(ctx: dict[str, Any]) -> Decision:
        return decide_with_gemini(
            settings,
            ctx,
            on_fallback=lambda text: _notify_fallback(store, notify, run_id, text),
        )

    chooser = decide or _choose
    decision = chooser(context)
    store.comment(run_id, "model", decision.reasoning or decision.action)
    store.set_alerts(_alerts_from(decision, price, settings.max_alerts))

    cancelling = _cancel_pending(
        settings, store, factor, decision, pending, run_id, notify, now
    )
    if cancelling:
        pending = store.pending()

    note = _manage_positions(
        settings, store, factor, decision, positions or [], run_id, notify, price
    )
    if positions is not None:
        positions, position_error = _load_positions(factor)

    block = entry_block(
        halted=halted,
        positions=positions or [],
        pending=pending,
        cancelling_pending=cancelling and store.pending() is None,
    )
    if position_error:
        block = block or "positions_unavailable"
    check = check_entry(
        decision,
        settings=settings,
        now=now,
        price=_side_price(decision.side, bid, ask, price),
        positions=positions or [],
        profile_ready=prior.poc is not None,
        daily_loss=0.0,
    )
    if decision.action == "none":
        result.final = "none"
        result.message = note or "none"
    elif block:
        result.final = "rejected"
        result.reason = block
        result.message = block
        store.trade(
            run_id=run_id,
            kind="reject",
            status="rejected",
            reason=block,
            side=decision.side,
            entry_price=decision.entry_price or price,
            stop_loss=decision.stop_loss,
            take_profit=decision.take_profit,
        )
    elif not check.ok:
        result.final = "rejected"
        result.reason = check.reason
        result.message = check.reason or "rejected"
        store.trade(
            run_id=run_id,
            kind="reject",
            status="rejected",
            reason=check.reason,
            side=decision.side,
            entry_price=check.entry or price,
            stop_loss=decision.stop_loss,
            take_profit=decision.take_profit,
        )
    else:
        placed = _place(settings, store, factor, check, decision, run_id, now, notify)
        result.final = placed[0]
        result.reason = placed[1]
        result.message = placed[2]

    store.comment(run_id, "system", result.message)
    store.set("last_note", result.message)
    _maybe_heartbeat(
        settings,
        store,
        notify,
        _heartbeat_text(
            price=price,
            prior=prior.view(),
            trend=levels["trend"],
            positions=positions,
            pending=store.pending(),
            last_result=_last_result(store),
            note=result.message,
        ),
        now,
    )
    return result


def _pending_gone(message: str) -> bool:
    text = message.strip().lower()
    return text == "order not found" or text.startswith("order is ")


def _pending_filled(message: str) -> bool:
    text = message.strip().lower()
    return "filled" in text or "closed" in text


def _cancel_pending(
    settings: Settings,
    store: Store,
    factor: Any,
    decision: Decision,
    pending: dict[str, Any] | None,
    run_id: str,
    notify: Callable[[str], None] | None,
    now: datetime,
) -> bool:
    if not pending or not pending.get("order_id"):
        return False
    wanted = any(
        item.action == "cancel" and item.position_id == str(pending["order_id"])
        for item in decision.manage_existing
    )
    if not wanted:
        return False
    sent = factor.cancel(
        {
            "order_id": pending["order_id"],
            "label": settings.label,
            "account_id": settings.account_id_int(),
        },
        run_id,
    )
    message = str(sent.get("message") or "")
    if not sent.get("success") and _pending_gone(message):
        store.set_pending(None)
        if _pending_filled(message):
            store.set("last_fill_day", now.date().isoformat())
        store.trade(
            run_id=run_id,
            kind="cancel",
            status="cleared",
            reason=message,
            order_id=str(pending["order_id"]),
            side=pending.get("side"),
        )
        if notify is not None:
            notify(f"POC pending {pending['order_id']} is not on the account.\n{message}")
        return True
    if not sent.get("success"):
        store.trade(
            run_id=run_id,
            kind="cancel",
            status="rejected",
            reason=message,
            order_id=str(pending["order_id"]),
        )
        return False
    store.set_pending(None)
    store.trade(
        run_id=run_id,
        kind="cancel",
        status="cancelled",
        order_id=str(pending["order_id"]),
        side=pending.get("side"),
    )
    if notify is not None:
        notify(f"POC cancelled pending {pending['order_id']}")
    return True


def _manage_positions(
    settings: Settings,
    store: Store,
    factor: Any,
    decision: Decision,
    positions: list[dict[str, Any]],
    run_id: str,
    notify: Callable[[str], None] | None,
    price: float | None,
) -> str:
    by_id = {str(row.get("position_id")): row for row in positions}
    notes: list[str] = []
    for item in decision.manage_existing:
        if item.action == "cancel":
            continue
        position = by_id.get(item.position_id)
        ok, reason, send = check_manage(item, position, trading_enabled=settings.trading_enabled)
        if not ok:
            notes.append(reason or "manage_rejected")
            continue
        if not send or position is None:
            continue
        payload = _manage_payload(settings, item, position)
        sent = factor.manage(payload, run_id)
        if sent.get("success"):
            notes.append(item.action)
            if item.action == "close":
                _record_close(
                    store,
                    position_id=item.position_id,
                    exit_price=price,
                    reason="model",
                    run_id=run_id,
                    oz_per_lot=settings.oz_per_lot,
                    notify=notify,
                    position=position,
                )
                store.set_open_ids(
                    [open_id for open_id in store.open_ids() if open_id != item.position_id]
                )
        else:
            notes.append(sent.get("message") or "manage_rejected")
    return ", ".join(notes)


def _manage_payload(settings: Settings, item: ManageItem, position: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "account_id": settings.account_id_int(),
        "position_id": item.position_id,
        "action": item.action,
        "label": settings.label,
    }
    if item.action == "move_sl_to_breakeven":
        payload["new_sl"] = position.get("entry_price")
    return payload


def _place(
    settings: Settings,
    store: Store,
    factor: Any,
    check: Any,
    decision: Decision,
    run_id: str,
    now: datetime,
    notify: Callable[[str], None] | None,
) -> tuple[str, str | None, str]:
    payload = {
        "account_id": settings.account_id_int(),
        "symbol": settings.broker_symbol,
        "side": decision.side,
        "volume": check.volume,
        "order_type": check.order_type,
        "entry_price": check.entry,
        "stop_loss": check.stop,
        "take_profit": check.take_profit,
        "label": settings.label,
        "comment": f"POC {decision.side} {check.order_type}"[:512],
    }
    sent = factor.place(payload, run_id)
    if not sent.get("success"):
        reason = str(sent.get("message") or "broker_rejected")
        store.trade(
            run_id=run_id,
            kind="reject",
            status="rejected",
            reason=reason,
            side=decision.side,
            order_type=check.order_type,
            volume=check.volume,
            entry_price=check.entry,
            stop_loss=check.stop,
            take_profit=check.take_profit,
            account_id=settings.account_id_int(),
        )
        store.error(reason, run_id=run_id)
        _notify_error(store, notify, reason, now)
        return "rejected", reason, reason
    order_id = str(sent.get("order_id") or "")
    position_id = sent.get("position_id")
    if check.order_type == "market":
        store.set("last_fill_day", now.date().isoformat())
        ids = store.open_ids()
        if position_id:
            ids.append(str(position_id))
        store.set_open_ids(ids)
        store.trade(
            run_id=run_id,
            kind="fill",
            status="filled",
            side=decision.side,
            order_type="market",
            volume=check.volume,
            entry_price=check.entry,
            stop_loss=check.stop,
            take_profit=check.take_profit,
            position_id=None if position_id is None else str(position_id),
            order_id=order_id,
            account_id=settings.account_id_int(),
        )
        text = (
            f"POC order {order_id} market {decision.side} {check.entry}\n"
            f"SL {check.stop} TP {check.take_profit}\n"
            f"{(decision.reasoning or '')[:400]}"
        )
        if notify is not None:
            notify(text)
        return "market", None, f"market {decision.side}"
    store.set_pending(
        {
            "order_id": order_id,
            "side": decision.side,
            "order_type": check.order_type,
            "volume": check.volume,
            "entry_price": check.entry,
            "stop_loss": check.stop,
            "take_profit": check.take_profit,
            "placed_at": _iso(now),
        }
    )
    store.trade(
        run_id=run_id,
        kind="pending",
        status="working",
        side=decision.side,
        order_type=check.order_type,
        volume=check.volume,
        entry_price=check.entry,
        stop_loss=check.stop,
        take_profit=check.take_profit,
        order_id=order_id,
        account_id=settings.account_id_int(),
    )
    text = (
        f"POC order {order_id} {check.order_type} {decision.side} {check.entry}\n"
        f"SL {check.stop} TP {check.take_profit}\n"
        f"{(decision.reasoning or '')[:400]}"
    )
    if notify is not None:
        notify(text)
    return "pending", None, f"pending {decision.side} {check.entry}"
