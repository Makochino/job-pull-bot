from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

from .utils import Vacancy, normalize_vacancy_content, normalized_content_hash, now_iso, word_similarity


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
        dedupe_key = str(metadata.get("dedupe_key") or metadata.get("source_key") or exact_hash)
        source_channel = str(metadata.get("source_channel") or metadata.get("username") or vacancy.source)
        source_chat_id = str(metadata.get("chat_id") or "") or None
        source_message_id = metadata.get("message_id")
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
                        vacancy.link,
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
        dedupe_key = str(metadata.get("dedupe_key") or metadata.get("source_key") or exact_hash)
        source_channel = str(metadata.get("source_channel") or metadata.get("username") or vacancy.source)
        source_chat_id = str(metadata.get("chat_id") or "") or None
        source_message_id = metadata.get("message_id")
        normalized_text = vacancy.content_normalized or normalize_vacancy_content(
            "\n".join([vacancy.title or "", vacancy.text or ""])
        )
        normalized_hash = vacancy.content_hash_normalized or normalized_content_hash(normalized_text)
        timestamp = now_iso()
        with self._connect() as connection:
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
                    vacancy.link,
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

    def like_vacancy(self, vacancy_id: int) -> bool:
        return self._set_review_state(vacancy_id, "liked")

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
        dedupe_key = str((vacancy.metadata or {}).get("dedupe_key") or (vacancy.metadata or {}).get("source_key") or exact_hash)
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
                    WHERE content_hash = ? OR content_hash_exact = ?
                    LIMIT 1
                    """,
                    (parent_hash, parent_hash),
                ).fetchone()
                if row:
                    return self._duplicate_result(row, vacancy, "parent-post")

                row = connection.execute(
                    """
                    SELECT id, source, source_type, 0 AS sent, 'rejected' AS review_state
                    FROM rejected_vacancies
                    WHERE content_hash = ? OR content_hash_exact = ?
                    LIMIT 1
                    """,
                    (parent_hash, parent_hash),
                ).fetchone()
                if row:
                    return self._duplicate_result(row, vacancy, "rejected-parent-post")

            if vacancy.source_type == "telegram":
                return None

            if normalized_hash:
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
