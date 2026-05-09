from __future__ import annotations

import asyncio
import logging
import sys

from app.bot import run_bot
from app.database import Database
from app.telethon_client import TelethonAccountClient
from app.utils import BASE_DIR, ConfigError, load_app_config, load_env_settings, setup_logging


async def async_main() -> None:
    setup_logging(BASE_DIR)
    logger = logging.getLogger(__name__)
    logger.info("Starting Telegram Job Pull Bot")

    try:
        env = load_env_settings(BASE_DIR)
        config = load_app_config(BASE_DIR / "config.yaml")
    except ConfigError as exc:
        logger.error("%s", exc)
        print(f"\nConfiguration error:\n{exc}\n", file=sys.stderr)
        raise SystemExit(1) from exc

    database_path = BASE_DIR / str(config.get("database_path", "vacancies.db"))
    database = Database(database_path)
    database.initialize()

    telethon_session = str(config.get("telethon_session", "telegram_user"))
    telethon = TelethonAccountClient(
        session_name=str(BASE_DIR / telethon_session),
        api_id=env.telegram_api_id,
        api_hash=env.telegram_api_hash,
    )

    try:
        await telethon.start()
        await run_bot(
            bot_token=env.telegram_bot_token,
            owner_id=env.my_telegram_user_id,
            config=config,
            database=database,
            telethon_client=telethon.client,
            base_dir=BASE_DIR,
        )
    except RuntimeError as exc:
        logger.error("%s", exc)
        print(f"\nStartup error:\n{exc}\n", file=sys.stderr)
        raise SystemExit(1) from exc
    finally:
        await telethon.disconnect()
        logger.info("Bot stopped")


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
