from poc.config import Settings
from poc.notify import message_chats, send_message, subscribe_chat, unsubscribe_chat
from poc.store import Store


class _Http:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def post(self, url: str, json: dict) -> object:
        self.calls.append({"url": url, "json": json})

        class _Response:
            status_code = 200
            text = ""

        return _Response()


def test_owner_subscribe_is_one_chat(tmp_path):
    store = Store(tmp_path / "poc.sqlite")
    store.set("telegram_chat_id", "1001")
    text = subscribe_chat(store, "1001")
    assert text.startswith("Subscribed")
    settings = Settings(telegram_bot_token="token", telegram_chat_id="1001")
    assert message_chats(settings, store) == ["1001"]
    http = _Http()
    send_message(settings, store, "POC note", http=http)
    assert [call["json"]["chat_id"] for call in http.calls] == ["1001"]


def test_friend_gets_a_copy_and_can_leave(tmp_path):
    store = Store(tmp_path / "poc.sqlite")
    settings = Settings(telegram_bot_token="token", telegram_chat_id="100")
    assert subscribe_chat(store, "200").startswith("Subscribed")
    http = _Http()
    send_message(settings, store, "POC note", http=http)
    assert [call["json"]["chat_id"] for call in http.calls] == ["200", "100"]
    assert unsubscribe_chat(store, "200").startswith("Unsubscribed")
    assert message_chats(settings, store) == ["100"]
    assert subscribe_chat(store, "200").startswith("Subscribed")
    assert message_chats(settings, store) == ["200", "100"]
