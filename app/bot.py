from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

from .database import Database
from .extraction import annotate_vacancy_fields, split_vacancy_candidates
from .filters import score_vacancy
from .formatter import (
    format_deleted_saved_vacancy,
    format_latest,
    format_no_pending_review,
    format_rejected_vacancies,
    format_review_vacancy_messages,
    format_saved_vacancy_messages,
    format_saved_vacancies_page,
    format_settings,
    format_stats,
    format_tg_pull_report,
)
from .telegram_sources import fetch_telegram_vacancies, read_channels
from .utils import SourceResult, Vacancy, as_int, vacancy_identity_key, word_similarity


logger = logging.getLogger(__name__)

TELEGRAM_BUTTON = "🔎 Pull Telegram jobs"
REVIEW_BUTTON = "🧾 Review vacancies"
SAVED_BUTTON = "❤️ Saved vacancies"
DEBUG_BUTTON = "🗑 Rejected / Debug"
REVIEW_LIKE_BUTTON = "✅ Like"
REVIEW_DISLIKE_BUTTON = "❌ Dislike"
SAVED_OPEN_BUTTON = "🔍 Open by number"
SAVED_DELETE_BUTTON = "🗑 Delete by number"
SAVED_PREV_BUTTON = "⬅️ Previous"
SAVED_NEXT_BUTTON = "➡️ Next"
SAVED_EXIT_BUTTON = "🚪 Exit"
SAVED_BACK_BUTTON = "↩️ Back to saved vacancies"
SAVED_PAGE_SIZE = 5


@dataclass
class AppContext:
    owner_id: int
    config: dict[str, Any]
    database: Database
    telethon_client: object
    base_dir: Path
    pull_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_reports: dict[str, str] = field(default_factory=dict)


@dataclass
class FilteredBatch:
    matched: list[Vacancy]
    rejected: int = 0
    hard_rejected: int = 0


@dataclass
class FreshBatch:
    vacancies: list[Vacancy]
    duplicates: int = 0
    cross_channel_duplicates: int = 0
    already_sent: int = 0


class SavedVacancyStates(StatesGroup):
    browsing = State()
    waiting_open_number = State()
    waiting_delete_number = State()


class ReviewVacancyStates(StatesGroup):
    reviewing = State()


def _config_int(config: dict[str, Any], key: str, default: int) -> int:
    return as_int(config.get(key), default)


def _config_bool(config: dict[str, Any], key: str, default: bool) -> bool:
    value = config.get(key, default)
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip().casefold() not in {"0", "false", "no", "off", "null", "none"}
    return bool(value)


def _auto_delete_delay(context: AppContext) -> int:
    value = context.config.get("auto_delete_messages_after_seconds", 600)
    if value is None or value == "":
        return 0
    delay = as_int(value, 600)
    return max(0, delay)


def schedule_delete_message(bot: Bot, chat_id: int, message_id: int, delay: int) -> None:
    if delay <= 0:
        return
    asyncio.create_task(_delete_message_later(bot, chat_id, message_id, delay))


async def _delete_message_later(bot: Bot, chat_id: int, message_id: int, delay: int) -> None:
    await asyncio.sleep(delay)
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except TelegramAPIError as exc:
        logger.debug("Could not auto-delete Telegram message %s/%s: %s", chat_id, message_id, exc)
    except Exception:
        logger.debug("Unexpected auto-delete error for Telegram message %s/%s", chat_id, message_id, exc_info=True)


def _schedule_message_delete(message: Message, context: AppContext) -> None:
    bot = getattr(message, "bot", None)
    if bot is None:
        logger.debug("Cannot schedule deletion: message is not bound to a bot")
        return
    schedule_delete_message(bot, message.chat.id, message.message_id, _auto_delete_delay(context))


async def _answer(message: Message, context: AppContext, text: str, **kwargs: Any) -> Message:
    kwargs.setdefault("parse_mode", ParseMode.HTML)
    kwargs.setdefault("reply_markup", main_menu_keyboard())
    sent = await message.answer(text, **kwargs)
    _schedule_message_delete(sent, context)
    return sent


async def _is_authorized(message: Message, owner_id: int) -> bool:
    if not message.from_user or message.from_user.id != owner_id:
        await message.answer("⛔ Access denied.")
        return False
    return True


async def _prepare_command(message: Message, context: AppContext) -> bool:
    if not await _is_authorized(message, context.owner_id):
        return False
    _schedule_message_delete(message, context)
    return True


def main_menu_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=TELEGRAM_BUTTON)],
            [KeyboardButton(text=REVIEW_BUTTON), KeyboardButton(text=SAVED_BUTTON)],
            [KeyboardButton(text=DEBUG_BUTTON)],
        ],
        resize_keyboard=True,
        is_persistent=True,
        one_time_keyboard=False,
        input_field_placeholder="Choose an action",
    )


def _reply_keyboard() -> ReplyKeyboardMarkup:
    return main_menu_keyboard()


def review_menu_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=REVIEW_LIKE_BUTTON), KeyboardButton(text=REVIEW_DISLIKE_BUTTON)],
            [KeyboardButton(text=SAVED_EXIT_BUTTON)],
        ],
        resize_keyboard=True,
        is_persistent=True,
        one_time_keyboard=False,
        input_field_placeholder="Review this vacancy",
    )


def saved_menu_keyboard(has_next: bool, has_prev: bool) -> ReplyKeyboardMarkup:
    navigation = []
    if has_prev:
        navigation.append(KeyboardButton(text=SAVED_PREV_BUTTON))
    if has_next:
        navigation.append(KeyboardButton(text=SAVED_NEXT_BUTTON))

    keyboard = [
        [KeyboardButton(text=SAVED_OPEN_BUTTON), KeyboardButton(text=SAVED_DELETE_BUTTON)],
    ]
    if navigation:
        keyboard.append(navigation)
    keyboard.append([KeyboardButton(text=SAVED_EXIT_BUTTON)])

    return ReplyKeyboardMarkup(
        keyboard=keyboard,
        resize_keyboard=True,
        is_persistent=True,
        one_time_keyboard=False,
        input_field_placeholder="Manage saved vacancies",
    )


def number_input_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=SAVED_BACK_BUTTON)],
            [KeyboardButton(text=SAVED_EXIT_BUTTON)],
        ],
        resize_keyboard=True,
        is_persistent=True,
        one_time_keyboard=False,
        input_field_placeholder="Enter saved vacancy number",
    )


def _start_text() -> str:
    return (
        "👋 <b>Job Pull Bot</b>\n\n"
        "🍽 <b>Profile:</b>\n"
        "Telegram-only vacancy review for waiter, runner and hostess roles in Odesa.\n\n"
        "Use the buttons below to control the bot.\n\n"
        "<b>Commands:</b>\n"
        "• /pull_tg — search Telegram channels\n"
        "• /review — continue vacancy review\n"
        "• /saved — manage liked vacancies\n"
        "• /rejected — recent rejected vacancies\n"
        "• /last_report — latest detailed Telegram pull report"
    )


def _help_text() -> str:
    return (
        "❓ <b>Help</b>\n\n"
        f"• <b>{TELEGRAM_BUTTON}</b> or /pull_tg — search Telegram channels.\n"
        f"• <b>{REVIEW_BUTTON}</b> or /review — continue reviewing pending vacancies one by one.\n"
        f"• <b>{SAVED_BUTTON}</b>, /saved, /liked or /latest — manage liked vacancies.\n"
        f"• <b>{DEBUG_BUTTON}</b> or /rejected — inspect recent rejected vacancies.\n"
        "• <b>/last_report</b> — show the latest detailed pull report.\n\n"
        "Telegram cannot remove the text input field completely, but the persistent buttons below stay available for quick control.\n\n"
        "The filter accepts only target roles and rejects clear experience-required vacancies."
    )


def register_handlers(dispatcher: Dispatcher, context: AppContext) -> None:
    router = Router()

    @router.message(Command("start"))
    async def start_handler(message: Message) -> None:
        if not await _prepare_command(message, context):
            return
        await _answer(message, context, _start_text(), reply_markup=_reply_keyboard())

    @router.message(Command("help"))
    async def help_handler(message: Message) -> None:
        if not await _prepare_command(message, context):
            return
        await _answer(message, context, _help_text())

    @router.message(Command("settings"))
    async def settings_handler(message: Message) -> None:
        if not await _prepare_command(message, context):
            return
        await _send_settings(message, context)

    @router.message(Command("review"))
    async def review_handler(message: Message, state: FSMContext) -> None:
        if not await _prepare_command(message, context):
            return
        await show_review_screen(message, context, state)

    @router.message(F.text == REVIEW_BUTTON)
    async def review_button_handler(message: Message, state: FSMContext) -> None:
        if not await _prepare_command(message, context):
            return
        await show_review_screen(message, context, state)

    @router.message(Command("latest", "liked", "saved"))
    async def saved_command_handler(message: Message, state: FSMContext) -> None:
        if not await _prepare_command(message, context):
            return
        await _open_saved_vacancies(message, context, state)

    @router.message(F.text == SAVED_BUTTON)
    async def saved_button_handler(message: Message, state: FSMContext) -> None:
        if not await _prepare_command(message, context):
            return
        await _open_saved_vacancies(message, context, state)

    @router.message(Command("rejected", "rejected_last"))
    async def rejected_handler(message: Message) -> None:
        if not await _prepare_command(message, context):
            return
        await _send_rejected(message, context)

    @router.message(F.text == DEBUG_BUTTON)
    async def rejected_button_handler(message: Message) -> None:
        if not await _prepare_command(message, context):
            return
        await _send_rejected(message, context)

    @router.message(Command("last_report", "debug_report", "telegram_report"))
    async def last_report_handler(message: Message) -> None:
        if not await _prepare_command(message, context):
            return
        await _send_last_report(message, context)

    @router.message(Command("stats"))
    async def stats_handler(message: Message) -> None:
        if not await _prepare_command(message, context):
            return
        await _send_stats(message, context)

    @router.message(Command("pull"))
    async def pull_handler(message: Message) -> None:
        if not await _prepare_command(message, context):
            return
        await _answer(
            message,
            context,
            "Use <b>/pull_tg</b> to scan Telegram channels.",
        )

    @router.message(Command("pull_tg"))
    async def pull_tg_handler(message: Message) -> None:
        if not await _prepare_command(message, context):
            return
        await _start_tg_pull(message, context)

    @router.message(F.text == TELEGRAM_BUTTON)
    async def pull_tg_button_handler(message: Message) -> None:
        if not await _prepare_command(message, context):
            return
        await _start_tg_pull(message, context)

    @router.message(ReviewVacancyStates.reviewing, F.text == REVIEW_LIKE_BUTTON)
    async def review_like_handler(message: Message, state: FSMContext) -> None:
        if not await _prepare_command(message, context):
            return
        await _handle_review_action(message, context, state, "like")

    @router.message(ReviewVacancyStates.reviewing, F.text == REVIEW_DISLIKE_BUTTON)
    async def review_dislike_handler(message: Message, state: FSMContext) -> None:
        if not await _prepare_command(message, context):
            return
        await _handle_review_action(message, context, state, "dislike")

    @router.message(ReviewVacancyStates.reviewing, F.text == SAVED_EXIT_BUTTON)
    async def review_exit_handler(message: Message, state: FSMContext) -> None:
        if not await _prepare_command(message, context):
            return
        await state.clear()
        await _answer(message, context, "Exited review mode.", reply_markup=main_menu_keyboard())

    @router.message(ReviewVacancyStates.reviewing)
    async def review_fallback_handler(message: Message) -> None:
        if not await _prepare_command(message, context):
            return
        await _answer(
            message,
            context,
            "Use the review buttons below.",
            reply_markup=review_menu_keyboard(),
        )

    @router.message(SavedVacancyStates.browsing, F.text == SAVED_OPEN_BUTTON)
    async def saved_open_button_handler(message: Message, state: FSMContext) -> None:
        if not await _prepare_command(message, context):
            return
        await state.set_state(SavedVacancyStates.waiting_open_number)
        await _answer(
            message,
            context,
            "Enter the saved vacancy number to open, or press Exit.",
            reply_markup=number_input_keyboard(),
        )

    @router.message(SavedVacancyStates.browsing, F.text == SAVED_DELETE_BUTTON)
    async def saved_delete_button_handler(message: Message, state: FSMContext) -> None:
        if not await _prepare_command(message, context):
            return
        await state.set_state(SavedVacancyStates.waiting_delete_number)
        await _answer(
            message,
            context,
            "Enter the saved vacancy number to delete, or press Exit.",
            reply_markup=number_input_keyboard(),
        )

    @router.message(SavedVacancyStates.browsing, F.text == SAVED_PREV_BUTTON)
    async def saved_prev_handler(message: Message, state: FSMContext) -> None:
        if not await _prepare_command(message, context):
            return
        page = max(0, await _saved_page_from_state(state) - 1)
        await _send_saved_vacancies(message, context, state, page=page)

    @router.message(SavedVacancyStates.browsing, F.text == SAVED_NEXT_BUTTON)
    async def saved_next_handler(message: Message, state: FSMContext) -> None:
        if not await _prepare_command(message, context):
            return
        page = await _saved_page_from_state(state) + 1
        await _send_saved_vacancies(message, context, state, page=page)

    @router.message(SavedVacancyStates.browsing, F.text == SAVED_EXIT_BUTTON)
    async def saved_exit_handler(message: Message, state: FSMContext) -> None:
        if not await _prepare_command(message, context):
            return
        await state.clear()
        await _answer(message, context, "Exited saved-vacancies mode.", reply_markup=main_menu_keyboard())

    @router.message(SavedVacancyStates.waiting_open_number, F.text == SAVED_BACK_BUTTON)
    @router.message(SavedVacancyStates.waiting_delete_number, F.text == SAVED_BACK_BUTTON)
    async def saved_back_handler(message: Message, state: FSMContext) -> None:
        if not await _prepare_command(message, context):
            return
        await state.set_state(SavedVacancyStates.browsing)
        await _send_saved_vacancies(message, context, state)

    @router.message(SavedVacancyStates.waiting_open_number, F.text == SAVED_EXIT_BUTTON)
    async def saved_open_exit_handler(message: Message, state: FSMContext) -> None:
        if not await _prepare_command(message, context):
            return
        await state.clear()
        await _answer(message, context, "Exited saved-vacancies mode.", reply_markup=main_menu_keyboard())

    @router.message(SavedVacancyStates.waiting_delete_number, F.text == SAVED_EXIT_BUTTON)
    async def saved_delete_exit_handler(message: Message, state: FSMContext) -> None:
        if not await _prepare_command(message, context):
            return
        await state.clear()
        await _answer(message, context, "Exited saved-vacancies mode.", reply_markup=main_menu_keyboard())

    @router.message(SavedVacancyStates.waiting_open_number)
    async def saved_open_number_handler(message: Message, state: FSMContext) -> None:
        if not await _prepare_command(message, context):
            return
        await _handle_saved_open_number(message, context, state)

    @router.message(SavedVacancyStates.waiting_delete_number)
    async def saved_delete_number_handler(message: Message, state: FSMContext) -> None:
        if not await _prepare_command(message, context):
            return
        await _handle_saved_delete_number(message, context, state)

    @router.message(SavedVacancyStates.browsing)
    async def saved_browsing_fallback(message: Message, state: FSMContext) -> None:
        if not await _prepare_command(message, context):
            return
        rows = context.database.liked_vacancies()
        page = _clamp_saved_page(len(rows), await _saved_page_from_state(state))
        has_prev, has_next = _saved_page_flags(len(rows), page)
        await _answer(
            message,
            context,
            "Use the saved-vacancy buttons below.",
            reply_markup=saved_menu_keyboard(has_next=has_next, has_prev=has_prev),
        )

    @router.message()
    async def unknown_message_handler(message: Message) -> None:
        if not await _prepare_command(message, context):
            return
        await _answer(message, context, "Use the buttons below to control the bot.")

    dispatcher.include_router(router)


async def _send_latest(message: Message, context: AppContext) -> None:
    rows = context.database.latest(limit=10)
    await _answer(message, context, format_latest(rows), disable_web_page_preview=True)


async def show_review_screen(message: Message, context: AppContext, state: FSMContext) -> None:
    await state.set_state(ReviewVacancyStates.reviewing)
    row = context.database.next_pending_vacancy()
    if row is None:
        await state.update_data(current_review_id=None)
        await _answer(
            message,
            context,
            format_no_pending_review(),
            reply_markup=review_menu_keyboard(),
        )
        return

    await state.update_data(current_review_id=int(row["id"]))
    left_count = context.database.pending_review_count()
    chunks = format_review_vacancy_messages(row, left_count)
    for index, chunk in enumerate(chunks):
        is_last = index == len(chunks) - 1
        await _answer(
            message,
            context,
            chunk,
            reply_markup=review_menu_keyboard(),
            disable_web_page_preview=True,
        )


async def _handle_review_action(
    message: Message,
    context: AppContext,
    state: FSMContext,
    action: str,
) -> None:
    data = await state.get_data()
    vacancy_id = as_int(data.get("current_review_id"), 0)
    if vacancy_id <= 0:
        await show_review_screen(message, context, state)
        return

    if action == "like":
        status = context.database.like_vacancy_status(vacancy_id)
        if status in {"duplicate_saved", "already_saved"}:
            await _answer(
                message,
                context,
                "⚠️ Эта вакансия уже добавлена в сохранённые.",
                reply_markup=review_menu_keyboard(),
            )
        elif status not in {"saved"}:
            await _answer(message, context, "This vacancy was already reviewed.", reply_markup=review_menu_keyboard())
    elif action == "dislike":
        changed = context.database.dislike_vacancy(vacancy_id)
        if not changed:
            await _answer(message, context, "This vacancy was already reviewed.", reply_markup=review_menu_keyboard())
    else:
        return

    await show_review_screen(message, context, state)


async def _open_saved_vacancies(message: Message, context: AppContext, state: FSMContext) -> None:
    await state.set_state(SavedVacancyStates.browsing)
    await state.update_data(saved_page=0)
    await _send_saved_vacancies(message, context, state, page=0)


def _clamp_saved_page(total: int, page: int) -> int:
    if total <= 0:
        return 0
    page_count = max(1, (total + SAVED_PAGE_SIZE - 1) // SAVED_PAGE_SIZE)
    return min(max(0, page), page_count - 1)


def _saved_page_flags(total: int, page: int) -> tuple[bool, bool]:
    page_count = max(1, (total + SAVED_PAGE_SIZE - 1) // SAVED_PAGE_SIZE)
    has_prev = page > 0
    has_next = page + 1 < page_count
    return has_prev, has_next


async def _saved_page_from_state(state: FSMContext) -> int:
    data = await state.get_data()
    return as_int(data.get("saved_page"), 0)


async def _send_saved_vacancies(
    message: Message,
    context: AppContext,
    state: FSMContext,
    page: int | None = None,
    prefix: str = "",
) -> None:
    rows = context.database.liked_vacancies()
    if page is None:
        page = await _saved_page_from_state(state)
    page = _clamp_saved_page(len(rows), page)
    await state.update_data(saved_page=page)
    has_prev, has_next = _saved_page_flags(len(rows), page)
    text = format_saved_vacancies_page(rows, page=page, page_size=SAVED_PAGE_SIZE)
    if prefix:
        text = f"{prefix}\n\n{text}"
    await _answer(
        message,
        context,
        text,
        reply_markup=saved_menu_keyboard(has_next=has_next, has_prev=has_prev),
        disable_web_page_preview=True,
    )


async def _handle_saved_open_number(message: Message, context: AppContext, state: FSMContext) -> None:
    raw_value = (message.text or "").strip()
    if not raw_value.isdigit():
        await _answer(
            message,
            context,
            "Send a plain number, for example <b>3</b>, or press Exit.",
            reply_markup=number_input_keyboard(),
        )
        return

    rows = context.database.liked_vacancies()
    if not rows:
        await state.set_state(SavedVacancyStates.browsing)
        await _send_saved_vacancies(message, context, state, page=0)
        return

    number = int(raw_value)
    if number < 1 or number > len(rows):
        await _answer(
            message,
            context,
            f"Number out of range. Send a number from <b>1</b> to <b>{len(rows)}</b>, or press Exit.",
            reply_markup=number_input_keyboard(),
        )
        return

    await state.set_state(SavedVacancyStates.browsing)
    row = rows[number - 1]
    chunks = format_saved_vacancy_messages(row, number)
    saved_page = await _saved_page_from_state(state)
    has_prev, has_next = _saved_page_flags(len(rows), saved_page)
    for index, chunk in enumerate(chunks):
        await _answer(
            message,
            context,
            chunk,
            reply_markup=saved_menu_keyboard(has_next=has_next, has_prev=has_prev),
            disable_web_page_preview=True,
        )


async def _handle_saved_delete_number(message: Message, context: AppContext, state: FSMContext) -> None:
    raw_value = (message.text or "").strip()
    if not raw_value.isdigit():
        await _answer(
            message,
            context,
            "Send a plain number, for example <b>3</b>, or press Exit.",
            reply_markup=number_input_keyboard(),
        )
        return

    rows = context.database.liked_vacancies()
    if not rows:
        await state.set_state(SavedVacancyStates.browsing)
        await _send_saved_vacancies(message, context, state, page=0)
        return

    number = int(raw_value)
    if number < 1 or number > len(rows):
        await _answer(
            message,
            context,
            f"Number out of range. Send a number from <b>1</b> to <b>{len(rows)}</b>, or press Exit.",
            reply_markup=number_input_keyboard(),
        )
        return

    row = rows[number - 1]
    deleted = context.database.delete_liked_vacancy(int(row["id"]))
    await state.set_state(SavedVacancyStates.browsing)
    page = _clamp_saved_page(len(rows) - 1, await _saved_page_from_state(state))
    if not deleted:
        prefix = "That saved vacancy was already changed. Here is the updated saved list."
    else:
        prefix = format_deleted_saved_vacancy(row, number)
    await _send_saved_vacancies(message, context, state, page=page, prefix=prefix)


async def _send_rejected(message: Message, context: AppContext) -> None:
    await _answer(
        message,
        context,
        format_rejected_vacancies(context.database.latest_rejected(limit=10)),
        disable_web_page_preview=True,
    )


async def _send_last_report(message: Message, context: AppContext) -> None:
    command = (message.text or "").split(maxsplit=1)[0].lstrip("/").casefold()
    if command == "telegram_report":
        report = context.last_reports.get("telegram")
    else:
        report = context.last_reports.get("last")

    await _answer(
        message,
        context,
        report or "No detailed pull report is available yet.",
        disable_web_page_preview=True,
    )


async def _send_stats(message: Message, context: AppContext) -> None:
    await _answer(message, context, format_stats(context.database.get_stats()))


async def _send_settings(message: Message, context: AppContext) -> None:
    await _answer(message, context, format_settings(context.config))


def _store_report(context: AppContext, source: str, report: str) -> None:
    context.last_reports[source] = report
    context.last_reports["last"] = report


def _pull_summary(
    queued_now: int,
    pending_total: int,
    *,
    channels_empty: bool = False,
) -> str:
    if channels_empty:
        return "channels.txt is empty. Add Telegram channels first."
    if queued_now > 0:
        return f"Added <b>{queued_now}</b> vacancies to review queue."
    return "No new vacancies found."


async def _start_tg_pull(message: Message, context: AppContext) -> None:
    if context.pull_lock.locked():
        await _answer(message, context, "⏳ <b>A search is already running.</b>\nPlease wait.")
        return

    await _answer(
        message,
        context,
        "🔎 <b>Searching Telegram vacancies from the last 48 hours only.</b>",
        reply_markup=main_menu_keyboard(),
    )
    async with context.pull_lock:
        await _run_pull_tg(message, context)


async def _run_pull_tg(message: Message, context: AppContext) -> None:
    logger.info("Manual /pull_tg started by user_id=%s", context.owner_id)

    try:
        channels = read_channels(context.base_dir / "channels.txt")
        channels_empty = not channels
        scan_hours = 48
        max_messages_per_channel = _config_int(context.config, "telegram_scan_max_messages_per_channel", 0)

        telegram_result = (
            await fetch_telegram_vacancies(
                context.telethon_client,
                channels,
                max_messages_per_channel=max_messages_per_channel,
                hours_back=scan_hours,
            )
            if channels
            else SourceResult()
        )

        filtered = _filter_candidates(telegram_result.vacancies, context.config, context.database)
        mode = f"last {scan_hours} hours only"
        fresh = _prepare_fresh_vacancies(context.database, filtered.matched)
        queued = fresh.vacancies
        duplicates = fresh.duplicates
        cross_channel_duplicates = fresh.cross_channel_duplicates
        already_sent = fresh.already_sent

        queued_now = _save_pending_vacancies(context.database, queued)
        insert_duplicates = max(0, len(queued) - queued_now)
        duplicates += insert_duplicates
        pending = context.database.pending_review_count()

        context.database.increment_stats(
            {
                "pull_tg_total": 1,
                "tg_checked_total": telegram_result.checked,
                "matched_tg_total": len(filtered.matched),
                "rejected_total": filtered.rejected,
                "rejected_tg_total": filtered.rejected,
                "hard_rejected_total": filtered.hard_rejected,
                "hard_rejected_tg_total": filtered.hard_rejected,
                "duplicates_total": duplicates,
                "duplicates_tg_total": duplicates,
                "cross_channel_duplicates_total": cross_channel_duplicates,
                "cross_channel_duplicates_tg_total": cross_channel_duplicates,
                "already_sent_total": already_sent,
                "already_sent_tg_total": already_sent,
                "queued_total": queued_now,
                "queued_tg_total": queued_now,
                "source_errors_tg_total": telegram_result.errors,
            }
        )

        logger.info(
            "/pull_tg finished: mode=%s checked=%s matched=%s rejected=%s hard_rejected=%s duplicates=%s cross_channel=%s already_sent=%s queued_now=%s pending=%s errors=%s",
            mode,
            telegram_result.checked,
            len(filtered.matched),
            filtered.rejected,
            filtered.hard_rejected,
            duplicates,
            cross_channel_duplicates,
            already_sent,
            queued_now,
            pending,
            telegram_result.errors,
        )

        report = format_tg_pull_report(
            mode=mode,
            posts_checked=telegram_result.checked,
            matched=len(filtered.matched),
            hard_rejected=filtered.hard_rejected,
            duplicates=duplicates,
            cross_channel_duplicates=cross_channel_duplicates,
            already_sent=already_sent,
            sent_now=queued_now,
            pending=pending,
            source_errors=telegram_result.errors,
            channels_empty=channels_empty,
        )
        _store_report(context, "telegram", report)
        await _answer(message, context, _pull_summary(queued_now, pending, channels_empty=channels_empty))
    except Exception:
        logger.exception("Critical /pull_tg error")
        await _answer(message, context, "⚠️ <b>/pull_tg failed.</b>\nDetails were written to logs/app.log.")


def _filter_candidates(candidates: list[Vacancy], config: dict[str, Any], database: Database | None = None) -> FilteredBatch:
    matched: list[Vacancy] = []
    rejected = 0
    hard_rejected = 0
    for vacancy in candidates:
        for candidate in split_vacancy_candidates(vacancy):
            result = score_vacancy(candidate, config)
            candidate.filter_debug = (
                f"accepted={result.accepted}; score={result.score}; "
                f"core={result.matched_core_keywords}; context={result.matched_context_keywords}; "
                f"bonus={result.matched_bonus_keywords}; reason={result.reject_reason}"
            )
            if result.hard_rejected:
                rejected += 1
                hard_rejected += 1
                annotate_vacancy_fields(candidate)
                if database:
                    database.record_rejected_vacancy(
                        candidate,
                        result.reject_reason or "hard rejected",
                        score=result.score,
                        hard_rejected=True,
                    )
                logger.info(
                    "Hard rejected vacancy: source=%s type=%s reason=%s title=%s",
                    candidate.source,
                    candidate.source_type,
                    result.reject_reason,
                    candidate.title,
                )
                continue
            if not result.accepted:
                rejected += 1
                annotate_vacancy_fields(candidate)
                if database:
                    database.record_rejected_vacancy(
                        candidate,
                        result.reject_reason or "below relevance threshold",
                        score=result.score,
                        hard_rejected=False,
                    )
                continue
            candidate.score = result.score
            candidate.location = candidate.location if candidate.location != "not specified" else result.location
            candidate.vacancy_type = candidate.role or result.vacancy_type
            if not candidate.role or candidate.role == "other":
                candidate.role = result.vacancy_type
            if not candidate.matched_role_keywords:
                candidate.matched_role_keywords = result.matched_core_keywords
            annotate_vacancy_fields(candidate)
            matched.append(candidate)
    return FilteredBatch(matched=matched, rejected=rejected, hard_rejected=hard_rejected)


def _prepare_fresh_vacancies(database: Database, vacancies: list[Vacancy]) -> FreshBatch:
    fresh: list[Vacancy] = []
    duplicates = 0
    cross_channel_duplicates = 0
    already_sent = 0

    for vacancy in vacancies:
        duplicate = database.find_duplicate(vacancy)
        if duplicate:
            state = str(duplicate.get("review_state") or "")
            if bool(duplicate.get("sent")) or state in {"liked", "disliked", "deleted", "duplicate", "rejected"}:
                already_sent += 1
            else:
                duplicates += 1
            if bool(duplicate.get("cross_channel")):
                cross_channel_duplicates += 1
            logger.info(
                "Duplicate vacancy skipped: kind=%s sent=%s state=%s cross_channel=%s source=%s title=%s",
                duplicate.get("kind"),
                duplicate.get("sent"),
                state,
                duplicate.get("cross_channel"),
                vacancy.source,
                vacancy.title,
            )
            continue

        current_duplicate = _find_current_batch_duplicate(vacancy, fresh)
        if current_duplicate:
            duplicates += 1
            if vacancy.source_type == "telegram" and current_duplicate.source != vacancy.source:
                cross_channel_duplicates += 1
            logger.info(
                "Current batch duplicate skipped: source=%s duplicate_source=%s title=%s",
                vacancy.source,
                current_duplicate.source,
                vacancy.title,
            )
            continue

        fresh.append(vacancy)

    return FreshBatch(
        vacancies=fresh,
        duplicates=duplicates,
        cross_channel_duplicates=cross_channel_duplicates,
        already_sent=already_sent,
    )


def _prepare_current_pull_vacancies(vacancies: list[Vacancy]) -> FreshBatch:
    fresh: list[Vacancy] = []
    duplicates = 0
    cross_channel_duplicates = 0

    for vacancy in vacancies:
        current_duplicate = _find_current_batch_duplicate(vacancy, fresh)
        if current_duplicate:
            duplicates += 1
            if vacancy.source_type == "telegram" and current_duplicate.source != vacancy.source:
                cross_channel_duplicates += 1
            logger.info(
                "Current pull duplicate skipped: source=%s duplicate_source=%s title=%s",
                vacancy.source,
                current_duplicate.source,
                vacancy.title,
            )
            continue
        fresh.append(vacancy)

    return FreshBatch(
        vacancies=fresh,
        duplicates=duplicates,
        cross_channel_duplicates=cross_channel_duplicates,
    )


def _sort_latest(vacancies: list[Vacancy]) -> list[Vacancy]:
    return sorted(vacancies, key=lambda vacancy: vacancy.published_at or "", reverse=True)


def _find_current_batch_duplicate(vacancy: Vacancy, fresh: list[Vacancy]) -> Vacancy | None:
    identity_key = vacancy_identity_key(vacancy)
    for existing in fresh:
        if identity_key and identity_key == vacancy_identity_key(existing):
            return existing
        if vacancy.content_hash_exact and vacancy.content_hash_exact == existing.content_hash_exact:
            return existing
        if vacancy.content_hash_normalized and vacancy.content_hash_normalized == existing.content_hash_normalized:
            return existing
        if (
            vacancy.parent_content_hash
            and vacancy.parent_content_hash == existing.parent_content_hash
        ):
            return existing
        if vacancy.source_type == "telegram" or existing.source_type == "telegram":
            continue
        if vacancy.source_type == existing.source_type:
            similarity = word_similarity(vacancy.content_normalized or vacancy.text, existing.content_normalized or existing.text)
            if similarity >= 0.85:
                return existing
    return None


def _save_pending_vacancies(database: Database, vacancies: list[Vacancy]) -> int:
    inserted = 0
    for vacancy in vacancies:
        if database.insert_vacancy(vacancy, sent=False, review_state="pending"):
            inserted += 1
    return inserted


async def _set_command_menu(bot: Bot) -> None:
    try:
        await bot.set_my_commands(
            [
                BotCommand(command="start", description="Open bot buttons"),
                BotCommand(command="pull_tg", description="Search Telegram channels"),
                BotCommand(command="review", description="Review pending vacancies"),
                BotCommand(command="saved", description="Manage liked vacancies"),
                BotCommand(command="rejected", description="Recent rejected vacancies"),
                BotCommand(command="last_report", description="Latest detailed Telegram report"),
                BotCommand(command="settings", description="Current filters"),
            ]
        )
    except TelegramAPIError as exc:
        logger.debug("Could not set Telegram command menu: %s", exc)


async def _send_startup_notification(bot: Bot, context: AppContext) -> None:
    if not _config_bool(context.config, "notify_user_on_startup", True):
        return
    try:
        sent = await bot.send_message(
            chat_id=context.owner_id,
            text="✅ <b>Job Pull Bot is online</b>\n\nUse the buttons below to control the bot.",
            parse_mode=ParseMode.HTML,
            reply_markup=_reply_keyboard(),
        )
        schedule_delete_message(bot, sent.chat.id, sent.message_id, _auto_delete_delay(context))
    except TelegramAPIError as exc:
        logger.warning("Could not send startup notification: %s", exc)
    except Exception:
        logger.exception("Unexpected startup notification error")


async def run_bot(
    bot_token: str,
    owner_id: int,
    config: dict[str, Any],
    database: Database,
    telethon_client: object,
    base_dir: Path,
) -> None:
    bot = Bot(token=bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dispatcher = Dispatcher(storage=MemoryStorage())
    context = AppContext(
        owner_id=owner_id,
        config=config,
        database=database,
        telethon_client=telethon_client,
        base_dir=base_dir,
    )
    register_handlers(dispatcher, context)

    logger.info("Starting aiogram polling")
    try:
        await _set_command_menu(bot)
        await _send_startup_notification(bot, context)
        await dispatcher.start_polling(bot)
    finally:
        await bot.session.close()
