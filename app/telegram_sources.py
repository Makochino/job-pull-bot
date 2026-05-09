from __future__ import annotations

import logging
from pathlib import Path

from telethon.errors import RPCError
from telethon.tl.custom import Message

from .utils import (
    SourceResult,
    Vacancy,
    clean_text_for_display,
    first_line_title,
    normalize_for_hash,
    normalize_text_for_hashing,
    normalized_content_hash,
    sha256_text,
)


logger = logging.getLogger(__name__)


def read_channels(path: Path) -> list[str]:
    if not path.exists():
        logger.warning("channels.txt not found: %s", path)
        return []

    channels: list[str] = []
    with path.open("r", encoding="utf-8") as file:
        for raw_line in file:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            channels.append(line)
    return channels


def _source_name(channel: str, entity: object) -> str:
    username = getattr(entity, "username", None)
    if username:
        return f"@{username}"
    return channel


def _post_link(channel: str, entity: object, message_id: int) -> str:
    username = getattr(entity, "username", None)
    if username:
        return f"https://t.me/{username}/{message_id}"
    if channel.startswith("@"):
        return f"https://t.me/{channel.lstrip('@')}/{message_id}"
    return ""


def _message_to_vacancy(channel: str, entity: object, message: Message) -> Vacancy | None:
    text = clean_text_for_display(message.message or "")
    if not text:
        return None

    source = _source_name(channel, entity)
    message_id = int(message.id)
    link = _post_link(channel, entity, message_id)
    published_at = message.date.isoformat(timespec="seconds") if message.date else ""
    if message_id:
        content_hash_exact = sha256_text(f"telegram|{source}|{message_id}")
    else:
        content_hash_exact = sha256_text(f"telegram|{source}|{normalize_for_hash(text)}")
    normalized_text = normalize_text_for_hashing(text)
    content_hash_normalized = normalized_content_hash(text)

    return Vacancy(
        source=source,
        source_type="telegram",
        title=first_line_title(text, fallback="Telegram post"),
        text=text,
        link=link,
        published_at=published_at,
        content_hash=content_hash_exact,
        content_hash_exact=content_hash_exact,
        content_hash_normalized=content_hash_normalized,
        content_normalized=normalized_text,
        metadata={"message_id": message_id},
    )


async def fetch_telegram_vacancies(
    client: object,
    channels: list[str],
    posts_per_channel: int,
) -> SourceResult:
    result = SourceResult()

    for channel in channels:
        try:
            logger.info("Reading Telegram channel: %s", channel)
            entity = await client.get_entity(channel)
            async for message in client.iter_messages(entity, limit=posts_per_channel):
                result.checked += 1
                vacancy = _message_to_vacancy(channel, entity, message)
                if vacancy:
                    result.vacancies.append(vacancy)
        except (ValueError, RPCError) as exc:
            result.errors += 1
            logger.warning("Telegram channel unavailable: %s | %s", channel, exc)
        except Exception:
            result.errors += 1
            logger.exception("Unexpected Telegram source error: %s", channel)

    logger.info(
        "Telegram fetch finished: checked=%s candidates=%s errors=%s",
        result.checked,
        len(result.vacancies),
        result.errors,
    )
    return result
