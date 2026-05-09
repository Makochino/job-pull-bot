from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any

from .utils import Vacancy, normalize_vacancy_content, normalized_content_hash, now_iso, word_similarity


logger = logging.getLogger(__name__)


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
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
                    sent INTEGER NOT NULL DEFAULT 0
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
                """
            )
        logger.info("SQLite database initialized: %s", self.path)

    def _migrate(self, connection: sqlite3.Connection) -> None:
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(vacancies)").fetchall()
        }
        migrations = {
            "content_hash_exact": "ALTER TABLE vacancies ADD COLUMN content_hash_exact TEXT",
            "content_hash_normalized": "ALTER TABLE vacancies ADD COLUMN content_hash_normalized TEXT",
            "content_normalized": "ALTER TABLE vacancies ADD COLUMN content_normalized TEXT",
            "sent_at": "ALTER TABLE vacancies ADD COLUMN sent_at TEXT",
        }
        for column, sql in migrations.items():
            if column not in columns:
                connection.execute(sql)

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
                    sent_at = CASE
                        WHEN sent = 1 AND sent_at IS NULL THEN created_at
                        ELSE sent_at
                    END
                WHERE id = ?
                """,
                (row["content_hash_normalized"] or normalized_content_hash(normalized), normalized, row["id"]),
            )

    def insert_vacancy(self, vacancy: Vacancy, sent: bool = False) -> bool:
        exact_hash = vacancy.content_hash_exact or vacancy.content_hash
        normalized_text = vacancy.content_normalized or normalize_vacancy_content(
            "\n".join([vacancy.title or "", vacancy.text or ""])
        )
        normalized_hash = vacancy.content_hash_normalized or normalized_content_hash(normalized_text)
        created_at = now_iso()
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO vacancies (
                        source, source_type, title, text, link, published_at,
                        score, created_at, content_hash, content_hash_exact,
                        content_hash_normalized, content_normalized, sent, sent_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        vacancy.source,
                        vacancy.source_type,
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
                    ),
                )
            return True
        except sqlite3.IntegrityError:
            return False

    def mark_sent(self, content_hash: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE vacancies SET sent = 1, sent_at = COALESCE(sent_at, ?) WHERE content_hash = ? OR content_hash_exact = ?",
                (now_iso(), content_hash, content_hash),
            )

    def mark_sent_by_hashes(self, exact_hash: str, normalized_hash: str = "", source_type: str = "") -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE vacancies
                SET sent = 1, sent_at = COALESCE(sent_at, ?)
                WHERE (content_hash = ?
                   OR content_hash_exact = ?
                   OR (? != '' AND content_hash_normalized = ?))
                  AND (? = '' OR source_type = ?)
                """,
                (now_iso(), exact_hash, exact_hash, normalized_hash, normalized_hash, source_type, source_type),
            )

    def find_duplicate(self, vacancy: Vacancy, similarity_threshold: float = 0.85) -> dict[str, Any] | None:
        exact_hash = vacancy.content_hash_exact or vacancy.content_hash
        normalized_text = vacancy.content_normalized or normalize_vacancy_content(
            "\n".join([vacancy.title or "", vacancy.text or ""])
        )
        normalized_hash = vacancy.content_hash_normalized or normalized_content_hash(normalized_text)

        with self._connect() as connection:
            if exact_hash:
                row = connection.execute(
                    """
                    SELECT id, source, source_type, sent
                    FROM vacancies
                    WHERE content_hash = ? OR content_hash_exact = ?
                    LIMIT 1
                    """,
                    (exact_hash, exact_hash),
                ).fetchone()
                if row:
                    return self._duplicate_result(row, vacancy, "exact")

            if normalized_hash:
                row = connection.execute(
                    """
                    SELECT id, source, source_type, sent
                    FROM vacancies
                    WHERE source_type = ?
                      AND content_hash_normalized = ?
                    LIMIT 1
                    """,
                    (vacancy.source_type, normalized_hash),
                ).fetchone()
                if row:
                    return self._duplicate_result(row, vacancy, "normalized")

            rows = connection.execute(
                """
                SELECT id, source, source_type, sent, content_normalized
                FROM vacancies
                WHERE source_type = ?
                  AND content_normalized IS NOT NULL
                ORDER BY id DESC
                LIMIT 1000
                """,
                (vacancy.source_type,),
            ).fetchall()

        for row in rows:
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
            "cross_channel": vacancy.source_type == "telegram" and row["source"] != vacancy.source,
            "source": row["source"],
        }

    def latest(self, limit: int = 10) -> list[sqlite3.Row]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, source, source_type, title, text, link, published_at,
                       score, created_at, sent
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
                    COALESCE(SUM(CASE WHEN sent = 0 THEN 1 ELSE 0 END), 0) AS unsent_saved
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
            }
        )
        return stats
