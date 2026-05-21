from __future__ import annotations

import tempfile
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

if "telethon" not in sys.modules:
    telethon = types.ModuleType("telethon")
    telethon_errors = types.ModuleType("telethon.errors")
    telethon_tl = types.ModuleType("telethon.tl")
    telethon_tl_custom = types.ModuleType("telethon.tl.custom")

    class RPCError(Exception):
        pass

    telethon_errors.RPCError = RPCError
    telethon_tl_custom.Message = object
    sys.modules.setdefault("telethon", telethon)
    sys.modules.setdefault("telethon.errors", telethon_errors)
    sys.modules.setdefault("telethon.tl", telethon_tl)
    sys.modules.setdefault("telethon.tl.custom", telethon_tl_custom)

if "yaml" not in sys.modules:
    yaml_module = types.ModuleType("yaml")

    class YAMLError(Exception):
        pass

    yaml_module.YAMLError = YAMLError
    yaml_module.safe_load = lambda file: {}
    sys.modules.setdefault("yaml", yaml_module)

if "dotenv" not in sys.modules:
    dotenv_module = types.ModuleType("dotenv")
    dotenv_module.load_dotenv = lambda *args, **kwargs: None
    sys.modules.setdefault("dotenv", dotenv_module)

from app.database import Database
from app.extraction import has_required_experience, split_vacancy_candidates
from app.filters import score_vacancy
from app.formatter import (
    format_review_vacancy,
    format_review_vacancy_messages,
    format_saved_vacancies_page,
)
from app.telegram_sources import fetch_telegram_vacancies
from app.utils import Vacancy, normalize_text_for_hashing, normalized_content_hash, sha256_text


TEST_CONFIG = {
    "min_score": 5,
    "filters": {
        "restaurant_context_keywords": [],
        "bonus_keywords": [],
        "locations": [],
        "hard_reject_keywords": [],
        "scam_reject_patterns": [],
        "female_only_reject_patterns": [],
    },
}


def make_vacancy(source_key: str, text: str, role: str = "waiter", source: str = "@jobs") -> Vacancy:
    exact_hash = sha256_text(source_key)
    normalized_text = normalize_text_for_hashing(text)
    message_id = next((part for part in reversed(source_key.split("|")) if part.isdigit()), "1")
    channel = source.lstrip("@") or "jobs"
    return Vacancy(
        source=source,
        source_type="telegram",
        title=text.splitlines()[0],
        text=text,
        link=f"https://t.me/{channel}/{message_id}",
        content_hash=exact_hash,
        content_hash_exact=exact_hash,
        content_hash_normalized=normalized_content_hash(normalized_text),
        content_normalized=normalized_text,
        role=role,
        vacancy_type=role,
        metadata={"source_key": source_key, "dedupe_key": source_key, "source_channel": source},
    )


class FakeEntity:
    id = 12345
    username = "jobs"


class FakeMessage:
    def __init__(self, message_id: int, text: str, date: datetime) -> None:
        self.id = message_id
        self.message = text
        self.date = date


class FakeClient:
    def __init__(self, messages: list[FakeMessage]) -> None:
        self.messages = messages

    async def get_entity(self, channel: str) -> FakeEntity:
        return FakeEntity()

    async def iter_messages(self, entity: object, limit: object = None) -> object:
        for message in self.messages:
            yield message


class RegressionTests(unittest.TestCase):
    def test_main_keyboard_has_no_website_button(self) -> None:
        bot_source = Path("app/bot.py").read_text(encoding="utf-8")

        self.assertIn('TELEGRAM_BUTTON = "🔎 Pull Telegram jobs"', bot_source)
        self.assertNotIn("WEBSITE_BUTTON", bot_source)
        self.assertNotIn("Pull Website jobs", bot_source)
        self.assertIn("def review_menu_keyboard() -> ReplyKeyboardMarkup", bot_source)
        self.assertIn("def saved_menu_keyboard(has_next: bool, has_prev: bool) -> ReplyKeyboardMarkup", bot_source)
        self.assertNotIn("InlineKeyboardMarkup", bot_source)
        self.assertNotIn("callback_query", bot_source)

    def test_experience_required_detection(self) -> None:
        required_cases = [
            "Офіціант-бармен\nДосвід роботи від 1 року\nЗ/п 250 грн ставка + 4% від каси + чайові",
            "Вимоги: Досвід роботи від року",
            "Опыт работы от 1 года",
            "Досвід роботи обов'язковий",
        ]
        for text in required_cases:
            with self.subTest(text=text):
                self.assertTrue(has_required_experience(text))

        allowed_cases = [
            "Офіціант\nДосвід роботи не обов'язковий\nЗ/п 1000 грн",
            "Офіціант\nМожна без досвіду",
            "Официант\nМожно без опыта работы",
            "Хостес\nДосвід бажаний\nГрафік 2/2",
            "Раннер\nЖелательно с опытом",
        ]
        for text in allowed_cases:
            with self.subTest(text=text):
                self.assertFalse(has_required_experience(text))

    def test_target_roles_and_red_flags(self) -> None:
        accepted = [
            "Официант\nГрафик 2/2",
            "Офіціант\nДосвід роботи не обов'язковий\nЗ/п 1000 грн",
            "Официантка\nСтавка + чай",
            "Официант-бармен\nСтавка + чайові",
            "waiter\npart time",
            "waitress\npart time",
            "Runner\npart time",
            "Раннер\npart time",
            "ранер\npart time",
            "Помощник официанта\nГрафик 2/2",
            "Помічник офіціанта\nГрафік 2/2",
            "Помощник в зал\nвечерние смены",
            "Помічник у зал\nвечірні зміни",
            "hostess\nвечерние смены",
            "Хостес\nвечерние смены",
        ]
        for text in accepted:
            with self.subTest(text=text):
                self.assertTrue(score_vacancy(make_vacancy(f"telegram|ok|{text}", text), TEST_CONFIG).accepted)

        rejected = [
            "Повар\nСтавка 1200 грн",
            "Кухар\nСтавка 1200 грн",
            "cook\nshift",
            "chef\nshift",
            "kitchen helper\nshift",
            "Помічник кухаря\nБез досвіду",
            "Помощник повара\nБез опыта",
            "Помічник кухаря\nБез досвіду",
            "Заготовщица\nСмена 2/2",
            "Заготовщик\nСмена 2/2",
            "Кафе «Штрудель» запрошує до команди 💛\n📍 Академічна, 11\n🔹 Помічник кухаря\n📞 +380 67 308 69 55",
            "Бармен\nпотрібен хлопець",
            "bartender\nshift",
            "barman\nshift",
            "Бариста\nДосвід бажаний\nГрафік 2/2",
            "barista\nshift",
            "Посудомойщик\nСмена 2/2",
            "Посудомийник\nЗміна",
            "Мойщица\nСмена 2/2",
            "Уборщица\nСмена 2/2",
            "Покоївка\nЗміна",
            "Администратор ресторана\nСтавка",
            "administrator\nrestaurant",
            "Грузчик\nСмена",
            "Курьер\nСмена",
            "Офіціант-бармен\nДосвід роботи від 1 року\nЗ/п 250 грн ставка + чайові",
            "Офіціант\n18+",
            "Офіціантка\nтільки дівчина",
        ]
        for text in rejected:
            with self.subTest(text=text):
                self.assertFalse(score_vacancy(make_vacancy(f"telegram|bad|{text}", text), TEST_CONFIG).accepted)

        age_allowed = [
            "Офіціант\n17+",
            "Хостес\nможна з 17",
            "Раннер\nможна з 17\n18+",
        ]
        for text in age_allowed:
            with self.subTest(text=text):
                self.assertTrue(score_vacancy(make_vacancy(f"telegram|age|{text}", text), TEST_CONFIG).accepted)

    def test_review_formatter_preserves_and_splits_original_text(self) -> None:
        original = "🍽 Офіціант\n\n• ставка 1000 грн\n• чайові\n\nТелефон: +380 50 111 22 33"
        message = format_review_vacancy({"text": original, "link": "https://t.me/jobs/1"}, left_count=12)

        self.assertIn("Vacancy review", message)
        self.assertIn("Vacancies left", message)
        self.assertIn("Text:\n🍽 Офіціант", message)
        self.assertIn("🍽 Офіціант\n\n• ставка 1000 грн\n• чайові", message)
        self.assertIn("\nLink:\nhttps://t.me/jobs/1", message)
        self.assertIn("https://t.me/jobs/1", message)
        self.assertNotIn("Relevance:", message)
        self.assertNotIn("Role:", message)
        self.assertNotIn("Salary:", message)

        long_original = (
            "🍽 Офіціант\n\n"
            "• ставка 1000 грн\n"
            "• графік 2/2\n"
            "• чайові\n\n"
            "Телефон: +380 50 111 22 33\n\n"
            + ("z" * 8000)
        )
        chunks = format_review_vacancy_messages(
            {"text": long_original, "link": "https://t.me/jobs/2"},
            left_count=1,
        )
        self.assertGreater(len(chunks), 1)
        rendered = "\n".join(chunks)
        self.assertIn("Text:\n🍽 Офіціант\n\n• ставка 1000 грн", rendered)
        self.assertIn("Телефон: +380 50 111 22 33", rendered)
        self.assertIn("\nLink:\nhttps://t.me/jobs/2", rendered)
        self.assertEqual(rendered.count("z"), 8000)
        self.assertTrue(all(len(chunk) <= 3900 for chunk in chunks))

    def test_database_dedupes_by_telegram_source_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "vacancies.db")
            database.initialize()

            vacancy = make_vacancy("telegram|12345|10|role|waiter", "Офіціант\nМожна без досвіду")
            self.assertTrue(database.insert_vacancy(vacancy))
            self.assertFalse(database.insert_vacancy(vacancy))
            self.assertIsNotNone(database.find_duplicate(vacancy))

            repost = make_vacancy(
                "telegram|67890|10|role|waiter",
                "Офіціант\nМожна без досвіду",
                source="@other_jobs",
            )
            self.assertFalse(database.insert_vacancy(repost))
            duplicate = database.find_duplicate(repost)
            self.assertIsNotNone(duplicate)
            self.assertTrue(duplicate["cross_channel"])

            rejected = make_vacancy("telegram|12345|11|role|waiter", "Официант\nДосвід роботи від 1 року")
            database.record_rejected_vacancy(rejected, "target role requires experience", hard_rejected=True)
            self.assertFalse(database.insert_vacancy(rejected))
            self.assertEqual(database.find_duplicate(rejected)["review_state"], "rejected")

    def test_split_candidates_keep_original_text_for_display(self) -> None:
        original = "🍽 Команда\n\nОфіціант\n• ставка 1000\n\nБармен\nДосвід роботи від 1 року"
        parent = make_vacancy("telegram|777|42", original)

        candidates = split_vacancy_candidates(parent)
        waiter = next(candidate for candidate in candidates if candidate.role == "waiter")

        self.assertEqual(waiter.text, original)
        self.assertIn("Офіціант", waiter.metadata["filter_text"])
        self.assertNotIn("Бармен", waiter.metadata["filter_text"])
        self.assertEqual([candidate.role for candidate in candidates], ["waiter"])

    def test_mixed_post_creates_only_waiter_candidate(self) -> None:
        original = (
            "Вакансия: Повар , Бармен, Официант\n"
            "📍 Кафе Френчи 29 (Одесса)\n"
            "Кафе Frenchi 29 приглашает в свою команду:\n"
            "-питание"
        )
        parent = make_vacancy("telegram|999|44", original)

        candidates = split_vacancy_candidates(parent)

        self.assertEqual([candidate.role for candidate in candidates], ["waiter"])
        self.assertEqual(candidates[0].text, original)
        self.assertTrue(score_vacancy(candidates[0], TEST_CONFIG).accepted)

    def test_single_line_mixed_role_does_not_inherit_other_role_experience(self) -> None:
        original = "Требуются: Повар с опытом, Официант"
        parent = make_vacancy("telegram|888|43", original)

        candidates = split_vacancy_candidates(parent)
        waiter = next(candidate for candidate in candidates if candidate.role == "waiter")

        self.assertEqual(waiter.text, original)
        self.assertEqual(waiter.metadata["filter_text"], "Waiter")
        self.assertTrue(score_vacancy(waiter, TEST_CONFIG).accepted)

    def test_saved_vacancies_compact_list(self) -> None:
        rows = [
            {"text": "В ресторан Mangal Meat House в Аркадии требуются:\n-Официанты", "link": "https://t.me/jobs/1"},
            {"text": "Офіціант\nСтавка 1000 грн", "link": "https://t.me/jobs/2"},
        ]

        message = format_saved_vacancies_page(rows, page=0, page_size=5)

        self.assertIn("#1 — Mangal Meat House", message)
        self.assertIn("https://t.me/jobs/1", message)
        self.assertIn("#2 — No info", message)
        self.assertNotIn("Ставка 1000 грн", message)


class TelegramSourceTests(unittest.IsolatedAsyncioTestCase):
    async def test_fetch_telegram_vacancies_scans_last_48_hours(self) -> None:
        now = datetime.now(timezone.utc)
        client = FakeClient(
            [
                FakeMessage(10, "Офіціант\nМожна без досвіду", now - timedelta(days=1)),
                FakeMessage(9, "Раннер\nМожна без досвіду", now - timedelta(hours=49)),
                FakeMessage(8, "Офіціант\nold", now - timedelta(days=4)),
            ]
        )

        result = await fetch_telegram_vacancies(client, ["@jobs"], hours_back=48)

        self.assertEqual(result.checked, 1)
        self.assertEqual([vacancy.metadata["message_id"] for vacancy in result.vacancies], [10])
        self.assertEqual(result.vacancies[0].metadata["dedupe_key"], "telegram-link|https://t.me/jobs/10")


if __name__ == "__main__":
    unittest.main()
