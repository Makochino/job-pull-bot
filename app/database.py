from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

from .utils import (
    Vacancy,
    normalize_vacancy_content,
    normalize_vacancy_link,
    normalized_content_hash,
    now_iso,
    vacancy_identity_key,
    word_similarity,
)


logger = logging.getLogger(__name__)


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> bool:
        result = super().__exit__(exc_type, exc_value, traceback)
        self.close()
        return bool(result)


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, factory=ClosingConnection)
        connection.row_factory = sqlite3.Row
        return connection

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS vacancies (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source TEXT NOT NULL,
                    source_type TEXT NOT NULL,
                    source_channel TEXT,
                    source_chat_id TEXT,
                    source_message_id INTEGER,
                    dedupe_key TEXT,
                    title TEXT,
                    text TEXT NOT NULL,
                    link TEXT,
                    published_at TEXT,
                    score INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    content_hash TEXT NOT NULL UNIQUE,
                    content_hash_exact TEXT,
                    content_hash_normalized TEXT,
                    content_normalized TEXT,
                    sent_at TEXT,
                    sent INTEGER NOT NULL DEFAULT 0,
                    review_state TEXT NOT NULL DEFAULT 'pending',
                    reviewed_at TEXT,
                    saved_at TEXT,
                    deleted_at TEXT,
                    parent_content_hash TEXT,
                    extracted_role TEXT,
                    extracted_salary TEXT,
                    extracted_schedule TEXT,
                    extracted_location TEXT,
                    extracted_contact TEXT,
                    extracted_age_requirement TEXT,
                    extracted_experience_requirement TEXT,
                    extracted_gender_requirement TEXT,
                    matched_role_keywords TEXT,
                    filter_debug TEXT
                );

                CREATE TABLE IF NOT EXISTS rejected_vacancies (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source TEXT NOT NULL,
                    source_type TEXT NOT NULL,
                    source_channel TEXT,
                    source_chat_id TEXT,
                    source_message_id INTEGER,
                    dedupe_key TEXT,
                    title TEXT,
                    text TEXT NOT NULL,
                    link TEXT,
                    published_at TEXT,
                    score INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    seen_count INTEGER NOT NULL DEFAULT 1,
                    content_hash TEXT NOT NULL UNIQUE,
                    content_hash_exact TEXT,
                    content_hash_normalized TEXT,
                    content_normalized TEXT,
                    parent_content_hash TEXT,
                    extracted_role TEXT,
                    extracted_salary TEXT,
                    extracted_schedule TEXT,
                    extracted_location TEXT,
                    extracted_contact TEXT,
                    extracted_age_requirement TEXT,
                    extracted_experience_requirement TEXT,
                    extracted_gender_requirement TEXT,
                    matched_role_keywords TEXT,
                    reject_reason TEXT NOT NULL,
                    hard_rejected INTEGER NOT NULL DEFAULT 0,
                    filter_debug TEXT
                );

                CREATE TABLE IF NOT EXISTS stats (
                    key TEXT PRIMARY KEY,
                    value INTEGER NOT NULL DEFAULT 0
                );
                """
            )
            self._migrate(connection)
            connection.executescript(
                """
                CREATE INDEX IF NOT EXISTS idx_vacancies_created_at
                    ON vacancies(created_at);

                CREATE INDEX IF NOT EXISTS idx_vacancies_sent
                    ON vacancies(sent);

                CREATE INDEX IF NOT EXISTS idx_vacancies_exact_hash
                    ON vacancies(content_hash_exact);

                CREATE INDEX IF NOT EXISTS idx_vacancies_normalized_hash
                    ON vacancies(content_hash_normalized);

                CREATE INDEX IF NOT EXISTS idx_vacancies_review_state
                    ON vacancies(review_state);

                CREATE INDEX IF NOT EXISTS idx_vacancies_saved_at
                    ON vacancies(saved_at);

                CREATE INDEX IF NOT EXISTS idx_vacancies_parent_hash
                    ON vacancies(parent_content_hash);

                CREATE INDEX IF NOT EXISTS idx_vacancies_dedupe_key
                    ON vacancies(dedupe_key);

                CREATE INDEX IF NOT EXISTS idx_rejected_last_seen_at
                    ON rejected_vacancies(last_seen_at);

                CREATE INDEX IF NOT EXISTS idx_rejected_dedupe_key
                    ON rejected_vacancies(dedupe_key);
                """
            )
            self._create_unique_index(
                connection,
                "ux_vacancies_content_hash_exact",
                """
                CREATE UNIQUE INDEX IF NOT EXISTS ux_vacancies_content_hash_exact
                    ON vacancies(content_hash_exact)
                    WHERE content_hash_exact IS NOT NULL
                """,
            )
            self._create_unique_index(
                connection,
                "ux_rejected_content_hash_exact",
                """
                CREATE UNIQUE INDEX IF NOT EXISTS ux_rejected_content_hash_exact
                    ON rejected_vacancies(content_hash_exact)
                    WHERE content_hash_exact IS NOT NULL
                """,
            )
            self._create_unique_index(
                connection,
                "ux_vacancies_dedupe_key",
                """
                CREATE UNIQUE INDEX IF NOT EXISTS ux_vacancies_dedupe_key
                    ON vacancies(dedupe_key)
                    WHERE dedupe_key IS NOT NULL AND dedupe_key != ''
                """,
            )
            self._create_unique_index(
                connection,
                "ux_rejected_dedupe_key",
                """
                CREATE UNIQUE INDEX IF NOT EXISTS ux_rejected_dedupe_key
                    ON rejected_vacancies(dedupe_key)
                    WHERE dedupe_key IS NOT NULL AND dedupe_key != ''
                """,
            )
            self._create_unique_index(
                connection,
                "ux_vacancies_telegram_link",
                """
                CREATE UNIQUE INDEX IF NOT EXISTS ux_vacancies_telegram_link
                    ON vacancies(link)
                    WHERE source_type = 'telegram'
                      AND link IS NOT NULL
                      AND link != ''
                """,
            )
            self._create_unique_index(
                connection,
                "ux_rejected_telegram_link",
                """
                CREATE UNIQUE INDEX IF NOT EXISTS ux_rejected_telegram_link
                    ON rejected_vacancies(link)
                    WHERE source_type = 'telegram'
                      AND link IS NOT NULL
                      AND link != ''
                """,
            )
            self._create_unique_index(
                connection,
                "ux_vacancies_telegram_channel_message",
                """
                CREATE UNIQUE INDEX IF NOT EXISTS ux_vacancies_telegram_channel_message
                    ON vacancies(source_channel, source_message_id)
                    WHERE source_type = 'telegram'
                      AND source_channel IS NOT NULL
                      AND source_channel != ''
                      AND source_message_id IS NOT NULL
                """,
            )
            self._create_unique_index(
                connection,
                "ux_rejected_telegram_channel_message",
                """
                CREATE UNIQUE INDEX IF NOT EXISTS ux_rejected_telegram_channel_message
                    ON rejected_vacancies(source_channel, source_message_id)
                    WHERE source_type = 'telegram'
                      AND source_channel IS NOT NULL
                      AND source_channel != ''
                      AND source_message_id IS NOT NULL
                """,
            )
            self._create_unique_index(
                connection,
                "ux_vacancies_telegram_normalized_hash",
                """
                CREATE UNIQUE INDEX IF NOT EXISTS ux_vacancies_telegram_normalized_hash
                    ON vacancies(content_hash_normalized)
                    WHERE source_type = 'telegram'
                      AND content_hash_normalized IS NOT NULL
                      AND content_hash_normalized != ''
                """,
            )
            self._create_unique_index(
                connection,
                "ux_rejected_telegram_normalized_hash",
                """
                CREATE UNIQUE INDEX IF NOT EXISTS ux_rejected_telegram_normalized_hash
                    ON rejected_vacancies(content_hash_normalized)
                    WHERE source_type = 'telegram'
                      AND content_hash_normalized IS NOT NULL
                      AND content_hash_normalized != ''
                """,
            )
        logger.info("SQLite database initialized: %s", self.path)

    def _create_unique_index(self, connection: sqlite3.Connection, name: str, sql: str) -> None:
        try:
            connection.execute(sql)
        except sqlite3.IntegrityError:
            logger.warning("Could not create unique index %s because duplicate historical rows already exist", name)

    def _migrate(self, connection: sqlite3.Connection) -> None:
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(vacancies)").fetchall()
        }
        had_review_state = "review_state" in columns
        migrations = {
            "source_channel": "ALTER TABLE vacancies ADD COLUMN source_channel TEXT",
            "source_chat_id": "ALTER TABLE vacancies ADD COLUMN source_chat_id TEXT",
            "source_message_id": "ALTER TABLE vacancies ADD COLUMN source_message_id INTEGER",
            "dedupe_key": "ALTER TABLE vacancies ADD COLUMN dedupe_key TEXT",
            "content_hash_exact": "ALTER TABLE vacancies ADD COLUMN content_hash_exact TEXT",
            "content_hash_normalized": "ALTER TABLE vacancies ADD COLUMN content_hash_normalized TEXT",
            "content_normalized": "ALTER TABLE vacancies ADD COLUMN content_normalized TEXT",
            "sent_at": "ALTER TABLE vacancies ADD COLUMN sent_at TEXT",
            "review_state": "ALTER TABLE vacancies ADD COLUMN review_state TEXT NOT NULL DEFAULT 'pending'",
            "reviewed_at": "ALTER TABLE vacancies ADD COLUMN reviewed_at TEXT",
            "saved_at": "ALTER TABLE vacancies ADD COLUMN saved_at TEXT",
            "deleted_at": "ALTER TABLE vacancies ADD COLUMN deleted_at TEXT",
            "parent_content_hash": "ALTER TABLE vacancies ADD COLUMN parent_content_hash TEXT",
            "extracted_role": "ALTER TABLE vacancies ADD COLUMN extracted_role TEXT",
            "extracted_salary": "ALTER TABLE vacancies ADD COLUMN extracted_salary TEXT",
            "extracted_schedule": "ALTER TABLE vacancies ADD COLUMN extracted_schedule TEXT",
            "extracted_location": "ALTER TABLE vacancies ADD COLUMN extracted_location TEXT",
            "extracted_contact": "ALTER TABLE vacancies ADD COLUMN extracted_contact TEXT",
            "extracted_age_requirement": "ALTER TABLE vacancies ADD COLUMN extracted_age_requirement TEXT",
            "extracted_experience_requirement": "ALTER TABLE vacancies ADD COLUMN extracted_experience_requirement TEXT",
            "extracted_gender_requirement": "ALTER TABLE vacancies ADD COLUMN extracted_gender_requirement TEXT",
            "matched_role_keywords": "ALTER TABLE vacancies ADD COLUMN matched_role_keywords TEXT",
            "filter_debug": "ALTER TABLE vacancies ADD COLUMN filter_debug TEXT",
        }
        for column, sql in migrations.items():
            if column not in columns:
                connection.execute(sql)

        rejected_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(rejected_vacancies)").fetchall()
        }
        rejected_migrations = {
            "source_channel": "ALTER TABLE rejected_vacancies ADD COLUMN source_channel TEXT",
            "source_chat_id": "ALTER TABLE rejected_vacancies ADD COLUMN source_chat_id TEXT",
            "source_message_id": "ALTER TABLE rejected_vacancies ADD COLUMN source_message_id INTEGER",
            "dedupe_key": "ALTER TABLE rejected_vacancies ADD COLUMN dedupe_key TEXT",
            "content_hash_exact": "ALTER TABLE rejected_vacancies ADD COLUMN content_hash_exact TEXT",
            "content_hash_normalized": "ALTER TABLE rejected_vacancies ADD COLUMN content_hash_normalized TEXT",
            "content_normalized": "ALTER TABLE rejected_vacancies ADD COLUMN content_normalized TEXT",
            "parent_content_hash": "ALTER TABLE rejected_vacancies ADD COLUMN parent_content_hash TEXT",
            "extracted_role": "ALTER TABLE rejected_vacancies ADD COLUMN extracted_role TEXT",
            "extracted_salary": "ALTER TABLE rejected_vacancies ADD COLUMN extracted_salary TEXT",
            "extracted_schedule": "ALTER TABLE rejected_vacancies ADD COLUMN extracted_schedule TEXT",
            "extracted_location": "ALTER TABLE rejected_vacancies ADD COLUMN extracted_location TEXT",
            "extracted_contact": "ALTER TABLE rejected_vacancies ADD COLUMN extracted_contact TEXT",
            "extracted_age_requirement": "ALTER TABLE rejected_vacancies ADD COLUMN extracted_age_requirement TEXT",
            "extracted_experience_requirement": "ALTER TABLE rejected_vacancies ADD COLUMN extracted_experience_requirement TEXT",
            "extracted_gender_requirement": "ALTER TABLE rejected_vacancies ADD COLUMN extracted_gender_requirement TEXT",
            "matched_role_keywords": "ALTER TABLE rejected_vacancies ADD COLUMN matched_role_keywords TEXT",
            "filter_debug": "ALTER TABLE rejected_vacancies ADD COLUMN filter_debug TEXT",
        }
        for column, sql in rejected_migrations.items():
            if column not in rejected_columns:
                connection.execute(sql)

        if not had_review_state:
            connection.execute(
                """
                UPDATE vacancies
                SET review_state = CASE
                        WHEN sent = 1 THEN 'disliked'
                        ELSE 'pending'
                    END,
                    reviewed_at = CASE
                        WHEN sent = 1 THEN COALESCE(sent_at, created_at)
                        ELSE reviewed_at
                    END
                """
            )

        rows = connection.execute(
            """
            SELECT id, title, text, content_hash, content_hash_exact,
                   content_hash_normalized, content_normalized, sent, sent_at
            FROM vacancies
            WHERE content_hash_exact IS NULL
               OR content_hash_normalized IS NULL
               OR content_normalized IS NULL
               OR (sent = 1 AND sent_at IS NULL)
            """
        ).fetchall()
        for row in rows:
            normalized = row["content_normalized"] or normalize_vacancy_content(
                "\n".join([str(row["title"] or ""), str(row["text"] or "")])
            )
            connection.execute(
                """
                UPDATE vacancies
                SET content_hash_exact = COALESCE(content_hash_exact, content_hash),
                    content_hash_normalized = COALESCE(content_hash_normalized, ?),
                    content_normalized = COALESCE(content_normalized, ?),
                    dedupe_key = COALESCE(dedupe_key, content_hash_exact, content_hash),
                    source_channel = COALESCE(source_channel, source),
                    sent_at = CASE
                        WHEN sent = 1 AND sent_at IS NULL THEN created_at
                        ELSE sent_at
                    END
                WHERE id = ?
                """,
                (row["content_hash_normalized"] or normalized_content_hash(normalized), normalized, row["id"]),
            )

        connection.execute(
            """
            UPDATE vacancies
            SET dedupe_key = COALESCE(dedupe_key, content_hash_exact, content_hash),
                source_channel = COALESCE(source_channel, source)
            WHERE dedupe_key IS NULL OR dedupe_key = '' OR source_channel IS NULL
            """
        )
        connection.execute(
            """
            UPDATE rejected_vacancies
            SET dedupe_key = COALESCE(dedupe_key, content_hash_exact, content_hash),
                source_channel = COALESCE(source_channel, source)
            WHERE dedupe_key IS NULL OR dedupe_key = '' OR source_channel IS NULL
            """
        )

    @staticmethod
    def _json_list(values: list[Any] | tuple[Any, ...] | None) -> str:
        return json.dumps(list(values or []), ensure_ascii=False)

    def insert_vacancy(self, vacancy: Vacancy, sent: bool = False, review_state: str = "pending") -> bool:
        if self.find_duplicate(vacancy):
            return False

        exact_hash = vacancy.content_hash_exact or vacancy.content_hash
        metadata = vacancy.metadata or {}
        dedupe_key = vacancy_identity_key(vacancy)
        source_channel = str(metadata.get("source_channel") or metadata.get("username") or vacancy.source)
        source_chat_id = str(metadata.get("chat_id") or "") or None
        source_message_id = metadata.get("message_id")
        link = normalize_vacancy_link(vacancy.link) if vacancy.source_type == "telegram" else vacancy.link
        normalized_text = vacancy.content_normalized or normalize_vacancy_content(
            "\n".join([vacancy.title or "", vacancy.text or ""])
        )
        normalized_hash = vacancy.content_hash_normalized or normalized_content_hash(normalized_text)
        created_at = now_iso()
        reviewed_at = created_at if review_state in {"liked", "disliked", "deleted"} else None
        saved_at = created_at if review_state == "liked" else None
        deleted_at = created_at if review_state == "deleted" else None
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO vacancies (
                        source, source_type, source_channel, source_chat_id,
                        source_message_id, dedupe_key, title, text, link, published_at,
                        score, created_at, content_hash, content_hash_exact,
                        content_hash_normalized, content_normalized, sent, sent_at,
                        review_state, reviewed_at, saved_at, deleted_at,
                        parent_content_hash, extracted_role, extracted_salary,
                        extracted_schedule, extracted_location, extracted_contact,
                        extracted_age_requirement, extracted_experience_requirement,
                        extracted_gender_requirement, matched_role_keywords, filter_debug
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        vacancy.source,
                        vacancy.source_type,
                        source_channel,
                        source_chat_id,
                        source_message_id,
                        dedupe_key,
                        vacancy.title,
                        vacancy.text,
                        link,
                        vacancy.published_at,
                        vacancy.score,
                        created_at,
                        exact_hash,
                        exact_hash,
                        normalized_hash,
                        normalized_text,
                        1 if sent else 0,
                        created_at if sent else None,
                        review_state,
                        reviewed_at,
                        saved_at,
                        deleted_at,
                        vacancy.parent_content_hash,
                        vacancy.role or vacancy.vacancy_type,
                        vacancy.salary,
                        vacancy.schedule,
                        vacancy.location,
                        vacancy.contact,
                        vacancy.age_requirement,
                        vacancy.experience_requirement,
                        vacancy.gender_requirement,
                        self._json_list(vacancy.matched_role_keywords),
                        vacancy.filter_debug,
                    ),
                )
            return True
        except sqlite3.IntegrityError:
            return False

    def mark_sent(self, content_hash: str) -> None:
        timestamp = now_iso()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE vacancies
                SET sent = 1,
                    sent_at = COALESCE(sent_at, ?),
                    review_state = CASE WHEN review_state = 'pending' THEN 'disliked' ELSE review_state END,
                    reviewed_at = CASE WHEN review_state = 'pending' THEN COALESCE(reviewed_at, ?) ELSE reviewed_at END
                WHERE content_hash = ? OR content_hash_exact = ?
                """,
                (timestamp, timestamp, content_hash, content_hash),
            )

    def mark_sent_by_hashes(self, exact_hash: str, normalized_hash: str = "", source_type: str = "") -> None:
        timestamp = now_iso()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE vacancies
                SET sent = 1,
                    sent_at = COALESCE(sent_at, ?),
                    review_state = CASE WHEN review_state = 'pending' THEN 'disliked' ELSE review_state END,
                    reviewed_at = CASE WHEN review_state = 'pending' THEN COALESCE(reviewed_at, ?) ELSE reviewed_at END
                WHERE (content_hash = ?
                   OR content_hash_exact = ?
                   OR (? != '' AND content_hash_normalized = ?))
                  AND (? = '' OR source_type = ?)
                """,
                (timestamp, timestamp, exact_hash, exact_hash, normalized_hash, normalized_hash, source_type, source_type),
            )

    def record_rejected_vacancy(
        self,
        vacancy: Vacancy,
        reject_reason: str,
        score: int = 0,
        hard_rejected: bool = False,
    ) -> None:
        exact_hash = vacancy.content_hash_exact or vacancy.content_hash
        metadata = vacancy.metadata or {}
        dedupe_key = vacancy_identity_key(vacancy)
        source_channel = str(metadata.get("source_channel") or metadata.get("username") or vacancy.source)
        source_chat_id = str(metadata.get("chat_id") or "") or None
        source_message_id = metadata.get("message_id")
        link = normalize_vacancy_link(vacancy.link) if vacancy.source_type == "telegram" else vacancy.link
        normalized_text = vacancy.content_normalized or normalize_vacancy_content(
            "\n".join([vacancy.title or "", vacancy.text or ""])
        )
        normalized_hash = vacancy.content_hash_normalized or normalized_content_hash(normalized_text)
        timestamp = now_iso()
        with self._connect() as connection:
            if self._touch_rejected_duplicate(
                connection,
                timestamp=timestamp,
                exact_hash=exact_hash,
                dedupe_key=dedupe_key,
                normalized_hash=normalized_hash,
                source_type=vacancy.source_type,
                score=score,
                reject_reason=reject_reason,
                hard_rejected=hard_rejected,
                matched_role_keywords=self._json_list(vacancy.matched_role_keywords),
                filter_debug=vacancy.filter_debug,
            ):
                return
            connection.execute(
                """
                INSERT INTO rejected_vacancies (
                    source, source_type, source_channel, source_chat_id,
                    source_message_id, dedupe_key, title, text, link, published_at, score,
                    created_at, last_seen_at, content_hash, content_hash_exact,
                    content_hash_normalized, content_normalized, parent_content_hash,
                    extracted_role, extracted_salary, extracted_schedule,
                    extracted_location, extracted_contact, extracted_age_requirement,
                    extracted_experience_requirement, extracted_gender_requirement,
                    matched_role_keywords, reject_reason, hard_rejected, filter_debug
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(content_hash) DO UPDATE SET
                    last_seen_at = excluded.last_seen_at,
                    seen_count = seen_count + 1,
                    score = excluded.score,
                    reject_reason = excluded.reject_reason,
                    hard_rejected = excluded.hard_rejected,
                    dedupe_key = COALESCE(rejected_vacancies.dedupe_key, excluded.dedupe_key),
                    matched_role_keywords = excluded.matched_role_keywords,
                    filter_debug = excluded.filter_debug
                """,
                (
                    vacancy.source,
                    vacancy.source_type,
                    source_channel,
                    source_chat_id,
                    source_message_id,
                    dedupe_key,
                    vacancy.title,
                    vacancy.text,
                    link,
                    vacancy.published_at,
                    score,
                    timestamp,
                    timestamp,
                    exact_hash,
                    exact_hash,
                    normalized_hash,
                    normalized_text,
                    vacancy.parent_content_hash,
                    vacancy.role or vacancy.vacancy_type,
                    vacancy.salary,
                    vacancy.schedule,
                    vacancy.location,
                    vacancy.contact,
                    vacancy.age_requirement,
                    vacancy.experience_requirement,
                    vacancy.gender_requirement,
                    self._json_list(vacancy.matched_role_keywords),
                    reject_reason,
                    1 if hard_rejected else 0,
                    vacancy.filter_debug,
                ),
            )

    def _touch_rejected_duplicate(
        self,
        connection: sqlite3.Connection,
        *,
        timestamp: str,
        exact_hash: str,
        dedupe_key: str,
        normalized_hash: str,
        source_type: str,
        score: int,
        reject_reason: str,
        hard_rejected: bool,
        matched_role_keywords: str,
        filter_debug: str,
    ) -> bool:
        cursor = connection.execute(
            """
            UPDATE rejected_vacancies
            SET last_seen_at = ?,
                seen_count = seen_count + 1,
                score = ?,
                reject_reason = ?,
                hard_rejected = ?,
                matched_role_keywords = ?,
                filter_debug = ?
            WHERE content_hash = ?
               OR content_hash_exact = ?
               OR (? != '' AND dedupe_key = ?)
               OR (? != '' AND source_type = ? AND content_hash_normalized = ?)
            """,
            (
                timestamp,
                score,
                reject_reason,
                1 if hard_rejected else 0,
                matched_role_keywords,
                filter_debug,
                exact_hash,
                exact_hash,
                dedupe_key,
                dedupe_key,
                normalized_hash,
                source_type,
                normalized_hash,
            ),
        )
        return cursor.rowcount > 0

    def pending_review_count(self) -> int:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM vacancies
                WHERE review_state = 'pending'
                  AND source_type = 'telegram'
                """
            ).fetchone()
        return int(row["count"])

    def next_pending_vacancy(self) -> sqlite3.Row | None:
        with self._connect() as connection:
            return connection.execute(
                """
                SELECT *
                FROM vacancies
                WHERE review_state = 'pending'
                  AND source_type = 'telegram'
                ORDER BY datetime(created_at) ASC, id ASC
                LIMIT 1
                """
            ).fetchone()

    def get_vacancy(self, vacancy_id: int) -> sqlite3.Row | None:
        with self._connect() as connection:
            return connection.execute(
                "SELECT * FROM vacancies WHERE id = ?",
                (vacancy_id,),
            ).fetchone()

    def _set_review_state(self, vacancy_id: int, state: str) -> bool:
        timestamp = now_iso()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE vacancies
                SET review_state = ?,
                    reviewed_at = COALESCE(reviewed_at, ?),
                    saved_at = CASE WHEN ? = 'liked' THEN COALESCE(saved_at, ?) ELSE saved_at END,
                    deleted_at = CASE WHEN ? = 'deleted' THEN COALESCE(deleted_at, ?) ELSE deleted_at END
                WHERE id = ?
                  AND review_state = 'pending'
                """,
                (state, timestamp, state, timestamp, state, timestamp, vacancy_id),
            )
        return cursor.rowcount > 0

    def like_vacancy_status(self, vacancy_id: int) -> str:
        timestamp = now_iso()
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM vacancies WHERE id = ?", (vacancy_id,)).fetchone()
            if row is None:
                return "missing"

            state = str(row["review_state"] or "")
            if state == "liked":
                return "already_saved"
            if state != "pending":
                return "already_reviewed"

            if self._has_liked_duplicate(connection, row):
                connection.execute(
                    """
                    UPDATE vacancies
                    SET review_state = 'duplicate',
                        reviewed_at = COALESCE(reviewed_at, ?)
                    WHERE id = ?
                      AND review_state = 'pending'
                    """,
                    (timestamp, vacancy_id),
                )
                return "duplicate_saved"

            cursor = connection.execute(
                """
                UPDATE vacancies
                SET review_state = 'liked',
                    reviewed_at = COALESCE(reviewed_at, ?),
                    saved_at = COALESCE(saved_at, ?)
                WHERE id = ?
                  AND review_state = 'pending'
                """,
                (timestamp, timestamp, vacancy_id),
            )
        return "saved" if cursor.rowcount > 0 else "already_reviewed"

    def _has_liked_duplicate(self, connection: sqlite3.Connection, row: sqlite3.Row) -> bool:
        dedupe_key = str(row["dedupe_key"] or "")
        exact_hash = str(row["content_hash_exact"] or row["content_hash"] or "")
        normalized_hash = str(row["content_hash_normalized"] or "")
        parent_hash = str(row["parent_content_hash"] or "")
        source_type = str(row["source_type"] or "")
        duplicate = connection.execute(
            """
            SELECT id
            FROM vacancies
            WHERE id != ?
              AND review_state = 'liked'
              AND source_type = ?
              AND (
                    (? != '' AND dedupe_key = ?)
                 OR (? != '' AND (content_hash = ? OR content_hash_exact = ?))
                 OR (? != '' AND content_hash_normalized = ?)
                 OR (? != '' AND (parent_content_hash = ? OR content_hash = ? OR content_hash_exact = ?))
                 OR (parent_content_hash IS NOT NULL AND parent_content_hash != '' AND parent_content_hash IN (?, ?))
              )
            LIMIT 1
            """,
            (
                row["id"],
                source_type,
                dedupe_key,
                dedupe_key,
                exact_hash,
                exact_hash,
                exact_hash,
                normalized_hash,
                normalized_hash,
                parent_hash,
                parent_hash,
                parent_hash,
                parent_hash,
                exact_hash,
                parent_hash,
            ),
        ).fetchone()
        return duplicate is not None

    def like_vacancy(self, vacancy_id: int) -> bool:
        return self.like_vacancy_status(vacancy_id) == "saved"

    def dislike_vacancy(self, vacancy_id: int) -> bool:
        return self._set_review_state(vacancy_id, "disliked")

    def liked_vacancies(self) -> list[sqlite3.Row]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM vacancies
                WHERE review_state = 'liked'
                  AND source_type = 'telegram'
                ORDER BY datetime(COALESCE(saved_at, reviewed_at, created_at)) DESC, id DESC
                """
            ).fetchall()
        return list(rows)

    def delete_liked_vacancy(self, vacancy_id: int) -> bool:
        timestamp = now_iso()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE vacancies
                SET review_state = 'deleted',
                    deleted_at = COALESCE(deleted_at, ?)
                WHERE id = ?
                  AND review_state = 'liked'
                """,
                (timestamp, vacancy_id),
            )
        return cursor.rowcount > 0

    def reset_vacancy_state(self) -> dict[str, int]:
        with self._connect() as connection:
            vacancy_count = self._table_count(connection, "vacancies")
            rejected_count = self._table_count(connection, "rejected_vacancies")
            stats_count = self._table_count(connection, "stats")

            connection.execute("DELETE FROM vacancies")
            connection.execute("DELETE FROM rejected_vacancies")
            connection.execute("DELETE FROM stats")
            connection.execute(
                """
                DELETE FROM sqlite_sequence
                WHERE name IN ('vacancies', 'rejected_vacancies')
                """
            )
        return {
            "vacancies": vacancy_count,
            "rejected_vacancies": rejected_count,
            "stats": stats_count,
            "deleted_total": vacancy_count + rejected_count + stats_count,
        }

    def _table_count(self, connection: sqlite3.Connection, table_name: str) -> int:
        row = connection.execute(
            "SELECT COUNT(*) AS count FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table_name,),
        ).fetchone()
        if int(row["count"]) == 0:
            return 0
        count_row = connection.execute(f"SELECT COUNT(*) AS count FROM {table_name}").fetchone()
        return int(count_row["count"])

    def latest_rejected(self, limit: int = 10) -> list[sqlite3.Row]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM rejected_vacancies
                ORDER BY datetime(last_seen_at) DESC, id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return list(rows)

    def find_duplicate(self, vacancy: Vacancy, similarity_threshold: float = 0.85) -> dict[str, Any] | None:
        exact_hash = vacancy.content_hash_exact or vacancy.content_hash
        dedupe_key = vacancy_identity_key(vacancy)
        normalized_text = vacancy.content_normalized or normalize_vacancy_content(
            "\n".join([vacancy.title or "", vacancy.text or ""])
        )
        normalized_hash = vacancy.content_hash_normalized or normalized_content_hash(normalized_text)

        with self._connect() as connection:
            if dedupe_key:
                row = connection.execute(
                    """
                    SELECT id, source, source_type, sent, review_state
                    FROM vacancies
                    WHERE dedupe_key = ?
                    LIMIT 1
                    """,
                    (dedupe_key,),
                ).fetchone()
                if row:
                    return self._duplicate_result(row, vacancy, "dedupe-key")

                row = connection.execute(
                    """
                    SELECT id, source, source_type, 0 AS sent, 'rejected' AS review_state
                    FROM rejected_vacancies
                    WHERE dedupe_key = ?
                    LIMIT 1
                    """,
                    (dedupe_key,),
                ).fetchone()
                if row:
                    return self._duplicate_result(row, vacancy, "rejected-dedupe-key")

            if exact_hash:
                row = connection.execute(
                    """
                    SELECT id, source, source_type, sent, review_state
                    FROM vacancies
                    WHERE content_hash = ? OR content_hash_exact = ?
                    LIMIT 1
                    """,
                    (exact_hash, exact_hash),
                ).fetchone()
                if row:
                    return self._duplicate_result(row, vacancy, "exact")

                row = connection.execute(
                    """
                    SELECT id, source, source_type, 0 AS sent, 'rejected' AS review_state
                    FROM rejected_vacancies
                    WHERE content_hash = ? OR content_hash_exact = ?
                    LIMIT 1
                    """,
                    (exact_hash, exact_hash),
                ).fetchone()
                if row:
                    return self._duplicate_result(row, vacancy, "rejected-exact")

            parent_hash = vacancy.parent_content_hash or str(vacancy.metadata.get("parent_content_hash", ""))
            if parent_hash:
                row = connection.execute(
                    """
                    SELECT id, source, source_type, sent, review_state
                    FROM vacancies
                    WHERE content_hash = ?
                       OR content_hash_exact = ?
                       OR parent_content_hash = ?
                    LIMIT 1
                    """,
                    (parent_hash, parent_hash, parent_hash),
                ).fetchone()
                if row:
                    return self._duplicate_result(row, vacancy, "parent-post")

                row = connection.execute(
                    """
                    SELECT id, source, source_type, 0 AS sent, 'rejected' AS review_state
                    FROM rejected_vacancies
                    WHERE content_hash = ?
                       OR content_hash_exact = ?
                       OR parent_content_hash = ?
                    LIMIT 1
                    """,
                    (parent_hash, parent_hash, parent_hash),
                ).fetchone()
                if row:
                    return self._duplicate_result(row, vacancy, "rejected-parent-post")

            if normalized_hash:
                if vacancy.source_type == "telegram":
                    row = connection.execute(
                        """
                        SELECT id, source, source_type, sent, review_state
                        FROM vacancies
                        WHERE content_hash_normalized = ?
                        LIMIT 1
                        """,
                        (normalized_hash,),
                    ).fetchone()
                else:
                    row = connection.execute(
                        """
                        SELECT id, source, source_type, sent, review_state
                        FROM vacancies
                        WHERE source_type = ?
                          AND content_hash_normalized = ?
                        LIMIT 1
                        """,
                        (vacancy.source_type, normalized_hash),
                    ).fetchone()
                if row:
                    return self._duplicate_result(row, vacancy, "normalized")

                if vacancy.source_type == "telegram":
                    row = connection.execute(
                        """
                        SELECT id, source, source_type, 0 AS sent, 'rejected' AS review_state
                        FROM rejected_vacancies
                        WHERE content_hash_normalized = ?
                        LIMIT 1
                        """,
                        (normalized_hash,),
                    ).fetchone()
                else:
                    row = connection.execute(
                        """
                        SELECT id, source, source_type, 0 AS sent, 'rejected' AS review_state
                        FROM rejected_vacancies
                        WHERE source_type = ?
                          AND content_hash_normalized = ?
                        LIMIT 1
                        """,
                        (vacancy.source_type, normalized_hash),
                    ).fetchone()
                if row:
                    return self._duplicate_result(row, vacancy, "rejected-normalized")

            if vacancy.source_type == "telegram":
                return None

            rows = list(connection.execute(
                """
                SELECT id, source, source_type, sent, review_state, content_normalized,
                       parent_content_hash, extracted_role
                FROM vacancies
                WHERE source_type = ?
                  AND content_normalized IS NOT NULL
                ORDER BY id DESC
                LIMIT 1000
                """,
                (vacancy.source_type,),
            ).fetchall())
            rows.extend(
                connection.execute(
                    """
                    SELECT id, source, source_type, 0 AS sent, 'rejected' AS review_state,
                           content_normalized, parent_content_hash, extracted_role
                    FROM rejected_vacancies
                    WHERE source_type = ?
                      AND content_normalized IS NOT NULL
                    ORDER BY id DESC
                    LIMIT 1000
                    """,
                    (vacancy.source_type,),
                ).fetchall()
            )

        for row in rows:
            vacancy_parent = vacancy.parent_content_hash or str(vacancy.metadata.get("parent_content_hash", ""))
            vacancy_role = vacancy.role or vacancy.vacancy_type
            row_parent = row["parent_content_hash"] if "parent_content_hash" in row.keys() else ""
            row_role = row["extracted_role"] if "extracted_role" in row.keys() else ""
            if vacancy_parent and row_parent == vacancy_parent and vacancy_role and row_role and vacancy_role != row_role:
                continue
            similarity = word_similarity(normalized_text, row["content_normalized"] or "")
            if similarity >= similarity_threshold:
                result = self._duplicate_result(row, vacancy, "similar")
                result["similarity"] = similarity
                return result

        return None

    def is_duplicate_vacancy(self, vacancy: Vacancy) -> bool:
        return self.find_duplicate(vacancy) is not None

    @staticmethod
    def _duplicate_result(row: sqlite3.Row, vacancy: Vacancy, kind: str) -> dict[str, Any]:
        return {
            "id": row["id"],
            "kind": kind,
            "sent": bool(row["sent"]),
            "review_state": row["review_state"] if "review_state" in row.keys() else "",
            "cross_channel": vacancy.source_type == "telegram" and row["source"] != vacancy.source,
            "source": row["source"],
        }

    def latest(self, limit: int = 10) -> list[sqlite3.Row]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM vacancies
                ORDER BY datetime(created_at) DESC, id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return list(rows)

    def increment_stats(self, values: dict[str, int]) -> None:
        if not values:
            return
        with self._connect() as connection:
            for key, value in values.items():
                connection.execute(
                    """
                    INSERT INTO stats(key, value)
                    VALUES(?, ?)
                    ON CONFLICT(key) DO UPDATE SET value = value + excluded.value
                    """,
                    (key, int(value)),
                )

    def get_stats(self) -> dict[str, Any]:
        with self._connect() as connection:
            stat_rows = connection.execute("SELECT key, value FROM stats").fetchall()
            vacancy_counts = connection.execute(
                """
                SELECT
                    COUNT(*) AS total_saved,
                    COALESCE(SUM(sent), 0) AS sent_saved,
                    COALESCE(SUM(CASE WHEN sent = 0 THEN 1 ELSE 0 END), 0) AS unsent_saved,
                    COALESCE(SUM(CASE WHEN review_state = 'pending' THEN 1 ELSE 0 END), 0) AS pending_review,
                    COALESCE(SUM(CASE WHEN review_state = 'liked' THEN 1 ELSE 0 END), 0) AS liked_saved,
                    COALESCE(SUM(CASE WHEN review_state = 'disliked' THEN 1 ELSE 0 END), 0) AS disliked_reviewed,
                    COALESCE(SUM(CASE WHEN review_state = 'deleted' THEN 1 ELSE 0 END), 0) AS deleted_saved
                FROM vacancies
                """
            ).fetchone()
            source_counts = connection.execute(
                """
                SELECT source_type, COUNT(*) AS saved
                FROM vacancies
                GROUP BY source_type
                """
            ).fetchall()
            rejected_count = connection.execute(
                "SELECT COUNT(*) AS count FROM rejected_vacancies"
            ).fetchone()

        stats = {row["key"]: row["value"] for row in stat_rows}
        stats["telegram_saved"] = 0
        stats["website_saved"] = 0
        for row in source_counts:
            source_type = str(row["source_type"])
            if source_type == "telegram":
                stats["telegram_saved"] += int(row["saved"])
            else:
                stats["website_saved"] += int(row["saved"])
        stats.update(
            {
                "total_saved": int(vacancy_counts["total_saved"]),
                "sent_saved": int(vacancy_counts["sent_saved"]),
                "unsent_saved": int(vacancy_counts["unsent_saved"]),
                "pending_review": int(vacancy_counts["pending_review"]),
                "liked_saved": int(vacancy_counts["liked_saved"]),
                "disliked_reviewed": int(vacancy_counts["disliked_reviewed"]),
                "deleted_saved": int(vacancy_counts["deleted_saved"]),
                "rejected_saved": int(rejected_count["count"]),
            }
        )
        return stats
