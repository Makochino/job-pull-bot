from __future__ import annotations

import argparse
import sys
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE_DIR))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Clear vacancy, review, saved, rejected and stats rows without deleting schema or config."
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=None,
        help="Optional SQLite database path. Defaults to database_path from config.yaml.",
    )
    args = parser.parse_args()

    from app.database import Database
    from app.utils import load_app_config

    config = load_app_config(BASE_DIR / "config.yaml")
    database_path = args.database or BASE_DIR / str(config.get("database_path", "vacancies.db"))
    if not database_path.is_absolute():
        database_path = BASE_DIR / database_path

    database = Database(database_path)
    database.initialize()
    counts = database.reset_vacancy_state()
    database.initialize()

    print(f"Database: {database_path}")
    print(f"Deleted vacancy/review/saved rows: {counts['vacancies']}")
    print(f"Deleted rejected audit rows: {counts['rejected_vacancies']}")
    print(f"Deleted old stats rows: {counts['stats']}")
    print(f"Deleted total rows: {counts['deleted_total']}")
    print("Kept schema, config.yaml, .env, channels.txt, Telegram channel config, and project files.")


if __name__ == "__main__":
    main()
