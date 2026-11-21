"""Run one check, or the hourly schedule, the price alerts, and Telegram.

    python -m poc --once
    python -m poc
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv

from poc.broker import build_broker
from poc.config import ROOT, Settings
from poc.cycle import flatten, run_cycle, status_text
from poc.notify import send_message, start_bot
from poc.scheduler import next_check, poll_price_alerts
from poc.store import Store


def _notify(settings: Settings, store: Store):
    def send(text: str) -> None:
        print(text, flush=True)
        try:
            send_message(settings, store, text)
        except Exception as exc:
            store.error(str(exc))
            print(f"telegram send failed: {exc}", flush=True)

    return send


def _once(settings: Settings, store: Store) -> int:
    factor = build_broker(settings)
    result = run_cycle(
        settings=settings,
        store=store,
        factor=factor,
        trigger="manual",
        notify=_notify(settings, store),
    )
    print(result.message or result.reason or result.final, flush=True)
    return 0


async def _serve(settings: Settings, store: Store) -> None:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    factor = build_broker(settings)
    notify = _notify(settings, store)
    alert_state: dict = {}

    def cycle(trigger: str = "schedule", now=None):
        return run_cycle(
            settings=settings,
            store=store,
            factor=factor,
            trigger=trigger,
            now=now,
            notify=notify,
        )

    async def analysis() -> None:
        await asyncio.to_thread(cycle, "schedule")

    async def alerts() -> None:
        from poc.quote import latest_price

        while True:
            try:
                await asyncio.to_thread(
                    poll_price_alerts,
                    settings,
                    store,
                    factor,
                    lambda: latest_price(settings),
                    alert_state,
                    cycle,
                )
            except Exception as exc:
                store.error(str(exc))
                print(f"alert poll failed: {exc}", flush=True)
            await asyncio.sleep(settings.alert_poll_sec)

    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(analysis, "cron", id="check", hour="*", minute=settings.check_minute)
    scheduler.start()
    upcoming = next_check(datetime.now(timezone.utc))
    print(
        f"POC up mode={settings.factor_mode} trading={settings.trading_enabled} "
        f"label={settings.label} next={upcoming.strftime('%Y-%m-%d %H:%M')}Z",
        flush=True,
    )
    alert_task = asyncio.create_task(alerts())

    if settings.telegram_bot_token:

        def on_status() -> str:
            return status_text(settings, store, factor)

        def on_halt() -> str:
            store.set("halted", "1")
            store.log("info", "halt", "entries stopped")
            return "POC halted. Open positions stay. /resume allows a new entry."

        def on_resume() -> str:
            store.set("halted", "0")
            store.log("info", "resume", "entries allowed")
            return "POC resumed."

        def on_flat() -> str:
            text = flatten(settings, store, factor)
            print(text, flush=True)
            return text

        app = start_bot(
            settings,
            store,
            on_status=on_status,
            on_halt=on_halt,
            on_resume=on_resume,
            on_flat=on_flat,
        )
        await app.initialize()
        await app.start()
        await app.updater.start_polling()
        print("telegram commands: /start /subscribe /unsubscribe /status /halt /resume /flat", flush=True)
    else:
        print("telegram token missing — pushes and commands wait for TELEGRAM_BOT_TOKEN", flush=True)

    try:
        await asyncio.Event().wait()
    finally:
        alert_task.cancel()
        scheduler.shutdown(wait=False)


def main(argv: list[str] | None = None) -> None:
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description="Weekly value-area XAUUSD bot")
    parser.add_argument("--once", action="store_true", help="run one cycle and exit")
    args = parser.parse_args(argv)
    settings = Settings.from_env()
    settings.require_runtime()
    if settings.factor_mode == "http" and not settings.factor_base_url:
        print("FACTOR_BASE_URL is empty — order calls have no host", flush=True)
    if settings.factor_mode == "http" and not settings.factor_api_token:
        print("FACTOR_API_TOKEN is empty — order calls will be rejected", flush=True)
    store = Store(settings.database_path())
    if args.once:
        sys.exit(_once(settings, store))
    asyncio.run(_serve(settings, store))


if __name__ == "__main__":
    main()
