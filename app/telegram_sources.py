from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from telethon.errors import RPCError
from telethon.tl.custom import Message

from .utils import (
    SourceResult,
    Vacancy,
    content_hash,
    first_line_title,
    normalize_for_hash,
    normalize_vacancy_link,
    normalize_text_for_hashing,
    preserve_original_text,
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
    text = preserve_original_text(message.message or "")
    if not text:
        return None

    source = _source_name(channel, entity)
    message_id = int(message.id)
    link = normalize_vacancy_link(_post_link(channel, entity, message_id))
    published_at = message.date.isoformat(timespec="seconds") if message.date else ""
    username = getattr(entity, "username", None)
    chat_id = getattr(entity, "id", None)
    chat_identifier = str(chat_id or username or source or channel)
    if link:
        source_key = f"telegram-link|{link}"
    elif message_id:
        source_key = f"telegram-message|{chat_identifier}|{message_id}"
    else:
        source_key = f"telegram-content|{normalize_for_hash(text)}"
    content_hash_exact = sha256_text(source_key)
    normalized_text = normalize_text_for_hashing(text)
    content_hash_normalized = content_hash(text)

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
        metadata={
            "source_key": source_key,
            "dedupe_key": source_key,
            "source_channel": source,
            "message_id": message_id,
            "chat_id": chat_id,
            "username": username,
        },
    )


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


async def fetch_telegram_vacancies(
    client: object,
    channels: list[str],
    days_back: int | None = None,
    max_messages_per_channel: int = 0,
    hours_back: int | None = None,
) -> SourceResult:
    result = SourceResult()
    scan_hours = max(1, int(hours_back)) if hours_back is not None else max(1, int(days_back or 2)) * 24
    cutoff = datetime.now(timezone.utc) - timedelta(hours=scan_hours)

    for channel in channels:
        try:
            logger.info("Reading Telegram channel: %s", channel)
            entity = await client.get_entity(channel)
            scanned_for_channel = 0
            async for message in client.iter_messages(entity, limit=None):
                message_date = _as_utc(getattr(message, "date", None))
                if message_date and message_date < cutoff:
                    break
                scanned_for_channel += 1
                if max_messages_per_channel > 0 and scanned_for_channel > max_messages_per_channel:
                    logger.warning(
                        "Telegram scan limit reached for %s: %s messages",
                        channel,
                        max_messages_per_channel,
                    )
                    break
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
        "Telegram fetch finished: hours_back=%s checked=%s candidates=%s errors=%s",
        scan_hours,
        result.checked,
        len(result.vacancies),
        result.errors,
    )
    return result
