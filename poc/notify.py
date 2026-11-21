"""Telegram pushes and /start /subscribe /status /halt /resume /flat."""

from __future__ import annotations

from typing import Any, Callable

import httpx

from poc.config import Settings
from poc.store import Store


def chat_id(settings: Settings, store: Store) -> str:
    saved = store.get("telegram_chat_id", "")
    return saved or settings.telegram_chat_id


def claim_chat(store: Store, chat: str) -> str:
    current = store.get("telegram_chat_id", "")
    if current and current != chat:
        return "Send /subscribe to get POC messages in this chat."
    if not current:
        store.set("telegram_chat_id", chat)
        return "POC will send notes and trade messages here."
    return "POC is already linked to this chat."


def message_chats(settings: Settings, store: Store) -> list[str]:
    """Owner chat plus chats that sent /subscribe. The owner is listed once."""
    found: list[str] = []
    found.extend(store.telegram_chats())
    owner = chat_id(settings, store)
    if owner:
        found.append(str(owner))
    seen: set[str] = set()
    chats: list[str] = []
    for item in found:
        key = str(item).strip()
        if not key or key in seen:
            continue
        seen.add(key)
        chats.append(key)
    return chats


def subscribe_chat(store: Store, chat: str) -> str:
    number = _chat_text(chat)
    if number is None:
        return "This chat cannot be subscribed."
    store.add_telegram_chat(number)
    return "Subscribed. This chat gets POC messages."


def unsubscribe_chat(store: Store, chat: str) -> str:
    number = _chat_text(chat)
    if number is None:
        return "This chat cannot be unsubscribed."
    store.remove_telegram_chat(number)
    return "Unsubscribed. This chat will no longer get POC messages."


def _chat_text(chat: str) -> str | None:
    text = str(chat).strip()
    digits = text[1:] if text.startswith("-") else text
    if not digits.isdigit():
        return None
    return text


def allowed(settings: Settings, store: Store, chat: str) -> bool:
    current = chat_id(settings, store)
    return bool(current) and current == chat


def send_message(
    settings: Settings,
    store: Store,
    text: str,
    http: httpx.Client | None = None,
) -> None:
    token = settings.telegram_bot_token
    chats = message_chats(settings, store)
    if not token or not chats:
        store.log("info", "telegram_skipped", text[:500])
        return
    client = http or httpx.Client(timeout=20)
    for chat in chats:
        response = client.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat, "text": text[:4000]},
        )
        if response.status_code >= 400:
            store.log("error", "telegram", f"{chat} {response.text[:300]}")
        else:
            store.log("info", "telegram", f"{chat} {text[:500]}")


def start_bot(
    settings: Settings,
    store: Store,
    *,
    on_status: Callable[[], str],
    on_halt: Callable[[], str],
    on_resume: Callable[[], str],
    on_flat: Callable[[], str],
) -> Any:
    from telegram.ext import Application, CommandHandler

    def _chat(update: Any) -> str | None:
        chat = getattr(update, "effective_chat", None)
        message = getattr(update, "message", None)
        if chat is None or message is None:
            return None
        return str(chat.id)

    async def start(update, context) -> None:
        chat = _chat(update)
        if not chat:
            return
        await update.message.reply_text(claim_chat(store, chat)[:4000])

    async def subscribe(update, context) -> None:
        chat = _chat(update)
        if not chat:
            return
        await update.message.reply_text(subscribe_chat(store, chat)[:4000])

    async def unsubscribe(update, context) -> None:
        chat = _chat(update)
        if not chat:
            return
        await update.message.reply_text(unsubscribe_chat(store, chat)[:4000])

    async def _guarded(update, action: Callable[[], str]) -> None:
        chat = _chat(update)
        if not chat:
            return
        if not allowed(settings, store, chat):
            await update.message.reply_text("Not allowed.")
            return
        text = await _in_thread(action)
        await update.message.reply_text(text[:4000])

    async def status(update, context) -> None:
        await _guarded(update, on_status)

    async def halt(update, context) -> None:
        await _guarded(update, on_halt)

    async def resume(update, context) -> None:
        await _guarded(update, on_resume)

    async def flat(update, context) -> None:
        await _guarded(update, on_flat)

    app = (
        Application.builder()
        .token(settings.telegram_bot_token)
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("subscribe", subscribe))
    app.add_handler(CommandHandler("unsubscribe", unsubscribe))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("halt", halt))
    app.add_handler(CommandHandler("resume", resume))
    app.add_handler(CommandHandler("flat", flat))
    return app


async def _in_thread(action: Callable[[], str]) -> str:
    import asyncio

    return await asyncio.to_thread(action)
