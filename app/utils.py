from __future__ import annotations

import hashlib
import logging
import os
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent.parent


class ConfigError(RuntimeError):
    """Raised when required runtime configuration is missing or invalid."""


@dataclass
class EnvSettings:
    telegram_api_id: int
    telegram_api_hash: str
    telegram_bot_token: str
    my_telegram_user_id: int


@dataclass
class Vacancy:
    source: str
    source_type: str
    title: str
    text: str
    link: str = ""
    published_at: str = ""
    score: int = 0
    content_hash: str = ""
    content_hash_exact: str = ""
    content_hash_normalized: str = ""
    content_normalized: str = ""
    parent_content_hash: str = ""
    location: str = "not specified"
    vacancy_type: str = "other"
    role: str = ""
    salary: str = ""
    schedule: str = ""
    contact: str = ""
    age_requirement: str = ""
    experience_requirement: str = ""
    gender_requirement: str = ""
    matched_role_keywords: list[str] = field(default_factory=list)
    filter_debug: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class SourceResult:
    vacancies: list[Vacancy] = field(default_factory=list)
    checked: int = 0
    cards_found: int = 0
    parsed: int = 0
    errors: int = 0
    debug_summaries: list[str] = field(default_factory=list)
    source_summaries: list[dict[str, Any]] = field(default_factory=list)


def setup_logging(base_dir: Path, log_file: str = "logs/app.log") -> None:
    log_path = base_dir / log_file
    log_path.parent.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    root.addHandler(console_handler)


def load_env_settings(base_dir: Path) -> EnvSettings:
    load_dotenv(base_dir / ".env")

    required = {
        "TELEGRAM_API_ID": os.getenv("TELEGRAM_API_ID"),
        "TELEGRAM_API_HASH": os.getenv("TELEGRAM_API_HASH"),
        "TELEGRAM_BOT_TOKEN": os.getenv("TELEGRAM_BOT_TOKEN"),
        "MY_TELEGRAM_USER_ID": os.getenv("MY_TELEGRAM_USER_ID"),
    }
    missing = [key for key, value in required.items() if not value]
    if missing:
        raise ConfigError(
            "Missing environment variables: "
            + ", ".join(missing)
            + ". Copy .env.example to .env and fill the values."
        )

    try:
        api_id = int(required["TELEGRAM_API_ID"] or "")
        owner_id = int(required["MY_TELEGRAM_USER_ID"] or "")
    except ValueError as exc:
        raise ConfigError(
            "TELEGRAM_API_ID and MY_TELEGRAM_USER_ID must be numbers."
        ) from exc

    return EnvSettings(
        telegram_api_id=api_id,
        telegram_api_hash=str(required["TELEGRAM_API_HASH"]),
        telegram_bot_token=str(required["TELEGRAM_BOT_TOKEN"]),
        my_telegram_user_id=owner_id,
    )


def load_yaml_file(path: Path, default: Any) -> Any:
    if not path.exists():
        logging.getLogger(__name__).warning("YAML file not found: %s", path)
        return default

    try:
        with path.open("r", encoding="utf-8") as file:
            data = yaml.safe_load(file)
    except yaml.YAMLError as exc:
        raise ConfigError(f"YAML error in {path}: {exc}") from exc

    return default if data is None else data


def load_app_config(path: Path) -> dict[str, Any]:
    data = load_yaml_file(path, default={})
    if not isinstance(data, dict):
        raise ConfigError("config.yaml must contain a YAML dictionary.")

    data.setdefault("database_path", "vacancies.db")
    data.setdefault("telethon_session", "telegram_user")
    data.setdefault("profile_name", "restaurant/cafe jobs in Odesa")
    data.setdefault("min_score", 5)
    data.setdefault("max_results_per_pull", 20)
    data.setdefault("auto_delete_messages_after_seconds", 600)
    data.setdefault("batch_size", 5)
    data.setdefault("debug_parsing", True)
    data.setdefault("notify_user_on_startup", True)
    data.setdefault("telegram_resend_latest_on_pull", False)
    data.setdefault("telegram_latest_limit", 10)
    data.setdefault("telegram_posts_per_channel", 30)
    data.setdefault("website_request_timeout", 15)
    data.setdefault("website_user_agent", "Mozilla/5.0 TelegramJobPullBot/1.0")
    data.setdefault("website_headers", {})
    data.setdefault("website_detail_pages_limit", data.get("max_results_per_pull", 20))
    data.setdefault("website_detail_delay_seconds", 0.7)

    filters = data.setdefault("filters", {})
    if not isinstance(filters, dict):
        raise ConfigError("filters in config.yaml must be a dictionary.")

    list_keys = (
        "locations",
        "keywords",
        "stop_words",
        "core_keywords",
        "restaurant_context_keywords",
        "bonus_keywords",
        "hard_reject_keywords",
        "female_only_reject_patterns",
        "scam_reject_patterns",
    )
    for key in list_keys:
        value = filters.setdefault(key, [])
        if not isinstance(value, list):
            raise ConfigError(f"filters.{key} must be a list.")

    if not isinstance(data["website_headers"], dict):
        raise ConfigError("website_headers in config.yaml must be a dictionary.")

    return data


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="ignore")).hexdigest()


def clean_text(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"\s+", " ", value).strip()


def compact_multiline_text(value: str | None) -> str:
    if not value:
        return ""
    return clean_vacancy_text(value)


def clean_vacancy_text(value: str | None) -> str:
    if not value:
        return ""
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    value = re.sub(r"([^\w\s])\1{3,}", r"\1\1", value)

    cleaned_lines: list[str] = []
    previous = ""
    for raw_line in value.splitlines():
        line = clean_text(raw_line)
        if not line or line == previous:
            continue
        cleaned_lines.append(line)
        previous = line
    return "\n".join(cleaned_lines)


def clean_text_for_display(value: str | None) -> str:
    """Gentle cleaning for Telegram messages; keeps employer formatting readable."""
    if not value:
        return ""
    value = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    value = re.sub(r"([^\w\s])\1{4,}", r"\1\1\1", value)

    cleaned_lines: list[str] = []
    empty_count = 0
    previous_non_empty = ""
    for raw_line in value.splitlines():
        line = raw_line.strip()
        line = re.sub(r"[ \t]{2,}", " ", line)
        if not line:
            empty_count += 1
            if empty_count <= 2:
                cleaned_lines.append("")
            continue

        empty_count = 0
        if line == previous_non_empty:
            continue
        cleaned_lines.append(line)
        previous_non_empty = line

    return "\n".join(cleaned_lines).strip()


def normalize_for_hash(value: str | None) -> str:
    if not value:
        return ""
    normalized = value.casefold().replace("ё", "е").replace("є", "е")
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized.strip()


def normalize_text_for_hashing(value: str | None) -> str:
    return normalize_vacancy_content(value)


def normalize_vacancy_content(value: str | None) -> str:
    if not value:
        return ""
    value = value.casefold().replace("ё", "е").replace("є", "е")
    value = re.sub(r"https?://\S+", " ", value)
    value = re.sub(r"(?:t\.me|telegram\.me)/\S+", " ", value)
    value = re.sub(r"@\w+", " ", value)
    value = re.sub(r"([^\w\s])\1{2,}", r"\1", value)

    kept_chars: list[str] = []
    for char in value:
        category = unicodedata.category(char)
        if category.startswith("S") and char not in {"₴", "$"}:
            kept_chars.append(" ")
        elif category.startswith("P") and char not in {"+", "/", ":", "-", "₴", "$"}:
            kept_chars.append(" ")
        else:
            kept_chars.append(char)

    normalized = "".join(kept_chars)
    normalized = re.sub(
        r"\b(?:канал|подписаться|підписатися|вакансия|вакансія|работа|робота|репост)\b",
        " ",
        normalized,
        flags=re.IGNORECASE,
    )
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized.strip()


def normalized_content_hash(value: str | None) -> str:
    return sha256_text(normalize_vacancy_content(value))


def word_similarity(left: str, right: str) -> float:
    left_words = {
        word for word in re.findall(r"[a-zа-яіїєґ0-9+:/-]+", normalize_vacancy_content(left))
        if len(word) >= 3 or any(char.isdigit() for char in word)
    }
    right_words = {
        word for word in re.findall(r"[a-zа-яіїєґ0-9+:/-]+", normalize_vacancy_content(right))
        if len(word) >= 3 or any(char.isdigit() for char in word)
    }
    if not left_words or not right_words:
        return 0.0
    return len(left_words & right_words) / len(left_words | right_words)


def truncate_text(value: str, limit: int = 1200) -> str:
    value = value.strip()
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 3)].rstrip() + "..."


def first_line_title(text: str, fallback: str = "Untitled") -> str:
    for line in text.splitlines():
        line = clean_text(line)
        if line:
            return truncate_text(line, 120)
    return fallback


def as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
