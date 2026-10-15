from __future__ import annotations

import logging

log = logging.getLogger(__name__)


class Notifier:
    def send(self, message: str) -> None:
        log.info("[NOTIFY] %s", message)


class TelegramNotifier(Notifier):
    def __init__(self, bot_token: str, chat_id: str):
        self.url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        self.chat_id = chat_id

    def send(self, message: str) -> None:
        super().send(message)
        try:
            import httpx

            httpx.post(self.url, json={"chat_id": self.chat_id, "text": message}, timeout=10)
        except Exception as exc:  # noqa: BLE001
            log.warning("Telegram-Nachricht fehlgeschlagen: %s", exc)
