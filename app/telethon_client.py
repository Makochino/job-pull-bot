from __future__ import annotations

import logging

from telethon import TelegramClient


logger = logging.getLogger(__name__)


class TelethonAccountClient:
    def __init__(self, session_name: str, api_id: int, api_hash: str) -> None:
        self.client = TelegramClient(session_name, api_id, api_hash)

    async def start(self) -> None:
        try:
            logger.info("Starting Telethon user client")
            await self.client.start()
        except EOFError as exc:
            raise RuntimeError(
                "Telethon needs first-time authorization, but the console is not interactive. "
                "Run python main.py manually, enter your phone number, Telegram code and 2FA "
                "password if enabled. After that the session file will be saved and future "
                "starts will not need interactive login."
            ) from exc
        except Exception as exc:
            raise RuntimeError(
                "Could not start Telethon. Check TELEGRAM_API_ID, TELEGRAM_API_HASH, "
                "internet connection and Telegram account authorization."
            ) from exc

        if not await self.client.is_user_authorized():
            raise RuntimeError(
                "Telethon is not authorized. Run python main.py in a normal console and "
                "complete Telegram account login."
            )

        me = await self.client.get_me()
        username = getattr(me, "username", None) or getattr(me, "id", "unknown")
        logger.info("Telethon authorized as %s", username)

    async def disconnect(self) -> None:
        if self.client.is_connected():
            await self.client.disconnect()
