from __future__ import annotations

import asyncio
import logging
from collections import Counter
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
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
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
    format_no_more_vacancies,
    format_pagination_prompt,
    format_pagination_stopped,
    format_rejected_vacancies,
    format_review_vacancy,
    format_saved_vacancies,
    format_settings,
    format_sites_pull_report,
    format_stats,
    format_tg_pull_report,
    format_vacancy,
)
from .telegram_sources import fetch_telegram_vacancies, read_channels
from .utils import SourceResult, Vacancy, as_int, word_similarity
from .website_sources import fetch_website_vacancies, load_sites


logger = logging.getLogger(__name__)

TELEGRAM_BUTTON = "🔎 Telegram jobs"
WEBSITE_BUTTON = "🌐 Website jobs"
SAVED_BUTTON = "❤️ Saved vacancies"
STATS_BUTTON = "📊 Stats"
SETTINGS_BUTTON = "⚙️ Settings"
HELP_BUTTON = "❓ Help"
SAVED_DELETE_BUTTON = "🗑 Delete"
SAVED_EXIT_BUTTON = "🚪 Exit"


@dataclass
class PaginationSession:
    source_type: str
    vacancies: list[Vacancy]
    offset: int = 0
    batch_size: int = 5
    message_delay: float = 0.35


@dataclass
class AppContext:
    owner_id: int
    config: dict[str, Any]
    database: Database
    telethon_client: object
    base_dir: Path
    pull_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    pagination_sessions: dict[int, PaginationSession] = field(default_factory=dict)


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
    waiting_delete_number = State()


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


def _batch_size(context: AppContext) -> int:
    return max(1, _config_int(context.config, "batch_size", 5))


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
    kwargs.setdefault("reply_markup", _reply_keyboard())
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


async def _is_authorized_callback(callback: CallbackQuery, context: AppContext) -> bool:
    if callback.from_user.id != context.owner_id:
        await callback.answer("Access denied.", show_alert=True)
        return False
    return True


def _reply_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=TELEGRAM_BUTTON), KeyboardButton(text=WEBSITE_BUTTON)],
            [KeyboardButton(text=SAVED_BUTTON), KeyboardButton(text=STATS_BUTTON)],
            [KeyboardButton(text=SETTINGS_BUTTON), KeyboardButton(text=HELP_BUTTON)],
        ],
        resize_keyboard=True,
        is_persistent=True,
        one_time_keyboard=False,
        input_field_placeholder="Choose an action",
    )


def _saved_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=SAVED_DELETE_BUTTON), KeyboardButton(text=SAVED_EXIT_BUTTON)],
        ],
        resize_keyboard=True,
        is_persistent=True,
        one_time_keyboard=False,
        input_field_placeholder="Manage saved vacancies",
    )


def _review_keyboard(vacancy_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Like", callback_data=f"review:like:{vacancy_id}"),
                InlineKeyboardButton(text="❌ Dislike", callback_data=f"review:dislike:{vacancy_id}"),
            ]
        ]
    )


def _pagination_keyboard(source_type: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Next 5", callback_data=f"page:next:{source_type}"),
                InlineKeyboardButton(text="Stop", callback_data=f"page:stop:{source_type}"),
            ]
        ]
    )


def _start_text() -> str:
    return (
        "👋 <b>Job Pull Bot</b>\n\n"
        "🍽 <b>Profile:</b>\n"
        "Restaurant / cafe jobs in Odesa\n\n"
        "Use the buttons below to control the bot.\n\n"
        "<b>Commands:</b>\n"
        "• /pull_tg — search Telegram channels\n"
        "• /pull_sites — search websites\n"
        "• /review — continue vacancy review\n"
        "• /saved — manage liked vacancies\n"
        "• /stats — statistics\n"
        "• /settings — current filters\n"
        "• /help — help"
    )


def _help_text() -> str:
    return (
        "❓ <b>Help</b>\n\n"
        f"• <b>{TELEGRAM_BUTTON}</b> or /pull_tg — search Telegram channels.\n"
        f"• <b>{WEBSITE_BUTTON}</b> or /pull_sites — search websites.\n"
        "• <b>/review</b> — continue reviewing pending vacancies one by one.\n"
        f"• <b>{SAVED_BUTTON}</b>, /saved, /liked or /latest — manage liked vacancies.\n"
        f"• <b>{STATS_BUTTON}</b> or /stats — show counters.\n"
        f"• <b>{SETTINGS_BUTTON}</b> or /settings — show active filters.\n\n"
        "Telegram cannot remove the text input field completely, but the persistent buttons below stay available for quick control.\n\n"
        "The filter is strict: only restaurant/cafe/hospitality vacancies in Odesa are accepted."
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

    @router.message(F.text == HELP_BUTTON)
    async def help_button_handler(message: Message) -> None:
        if not await _prepare_command(message, context):
            return
        await _answer(message, context, _help_text())

    @router.message(Command("settings"))
    async def settings_handler(message: Message) -> None:
        if not await _prepare_command(message, context):
            return
        await _send_settings(message, context)

    @router.message(F.text == SETTINGS_BUTTON)
    async def settings_button_handler(message: Message) -> None:
        if not await _prepare_command(message, context):
            return
        await _send_settings(message, context)

    @router.message(Command("review"))
    async def review_handler(message: Message) -> None:
        if not await _prepare_command(message, context):
            return
        await _send_next_review_vacancy(message, context)

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

    @router.message(Command("stats"))
    async def stats_handler(message: Message) -> None:
        if not await _prepare_command(message, context):
            return
        await _send_stats(message, context)

    @router.message(F.text == STATS_BUTTON)
    async def stats_button_handler(message: Message) -> None:
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
            "Use <b>/pull_tg</b> for Telegram channels or <b>/pull_sites</b> for websites.",
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

    @router.message(Command("pull_sites"))
    async def pull_sites_handler(message: Message) -> None:
        if not await _prepare_command(message, context):
            return
        await _start_sites_pull(message, context)

    @router.message(F.text == WEBSITE_BUTTON)
    async def pull_sites_button_handler(message: Message) -> None:
        if not await _prepare_command(message, context):
            return
        await _start_sites_pull(message, context)

    @router.message(SavedVacancyStates.browsing, F.text == SAVED_DELETE_BUTTON)
    async def saved_delete_handler(message: Message, state: FSMContext) -> None:
        if not await _prepare_command(message, context):
            return
        rows = context.database.liked_vacancies()
        if not rows:
            await _answer(message, context, "❤️ <b>No saved vacancies to delete.</b>", reply_markup=_saved_keyboard())
            return
        await state.set_state(SavedVacancyStates.waiting_delete_number)
        await _answer(
            message,
            context,
            "🗑 Send the number of the saved vacancy you want to delete.",
            reply_markup=_saved_keyboard(),
        )

    @router.message(SavedVacancyStates.browsing, F.text == SAVED_EXIT_BUTTON)
    async def saved_exit_handler(message: Message, state: FSMContext) -> None:
        if not await _prepare_command(message, context):
            return
        await state.clear()
        await _answer(message, context, "Exited saved-vacancies mode.", reply_markup=_reply_keyboard())

    @router.message(SavedVacancyStates.waiting_delete_number, F.text == SAVED_EXIT_BUTTON)
    async def saved_delete_exit_handler(message: Message, state: FSMContext) -> None:
        if not await _prepare_command(message, context):
            return
        await state.clear()
        await _answer(message, context, "Exited saved-vacancies mode.", reply_markup=_reply_keyboard())

    @router.message(SavedVacancyStates.waiting_delete_number)
    async def saved_delete_number_handler(message: Message, state: FSMContext) -> None:
        if not await _prepare_command(message, context):
            return
        await _handle_saved_delete_number(message, context, state)

    @router.message(SavedVacancyStates.browsing)
    async def saved_browsing_fallback(message: Message) -> None:
        if not await _prepare_command(message, context):
            return
        await _answer(message, context, "Use Delete or Exit in saved-vacancies mode.", reply_markup=_saved_keyboard())

    @router.callback_query(F.data.startswith("review:"))
    async def review_callback(callback: CallbackQuery) -> None:
        if not await _is_authorized_callback(callback, context):
            return
        await _handle_review_callback(callback, context)

    @router.callback_query(F.data.startswith("page:"))
    async def pagination_callback(callback: CallbackQuery) -> None:
        if not await _is_authorized_callback(callback, context):
            return
        await _handle_pagination(callback, context)

    @router.message()
    async def unknown_message_handler(message: Message) -> None:
        if not await _prepare_command(message, context):
            return
        await _answer(message, context, "Use the buttons below to control the bot.")

    dispatcher.include_router(router)


async def _send_latest(message: Message, context: AppContext) -> None:
    rows = context.database.latest(limit=10)
    await _answer(message, context, format_latest(rows), disable_web_page_preview=True)


async def _send_next_review_vacancy(message: Message, context: AppContext) -> None:
    row = context.database.next_pending_vacancy()
    if row is None:
        await _answer(message, context, format_no_pending_review())
        return

    left_count = context.database.pending_review_count()
    await _answer(
        message,
        context,
        format_review_vacancy(row, left_count),
        reply_markup=_review_keyboard(int(row["id"])),
        disable_web_page_preview=True,
    )


async def _handle_review_callback(callback: CallbackQuery, context: AppContext) -> None:
    data = callback.data or ""
    parts = data.split(":")
    if len(parts) != 3:
        await callback.answer()
        return

    _, action, raw_id = parts
    try:
        vacancy_id = int(raw_id)
    except ValueError:
        await callback.answer()
        return

    if action == "like":
        changed = context.database.like_vacancy(vacancy_id)
        await callback.answer("Saved" if changed else "Already reviewed")
    elif action == "dislike":
        changed = context.database.dislike_vacancy(vacancy_id)
        await callback.answer("Disliked" if changed else "Already reviewed")
    else:
        await callback.answer()
        return

    if isinstance(callback.message, Message):
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except TelegramAPIError:
            logger.debug("Could not remove review buttons from message %s", callback.message.message_id)
        if not changed:
            return
        await _send_next_review_vacancy(callback.message, context)


async def _open_saved_vacancies(message: Message, context: AppContext, state: FSMContext) -> None:
    await state.set_state(SavedVacancyStates.browsing)
    await _send_saved_vacancies(message, context)


async def _send_saved_vacancies(message: Message, context: AppContext) -> None:
    chunks = format_saved_vacancies(context.database.liked_vacancies())
    for chunk in chunks:
        await _answer(
            message,
            context,
            chunk,
            reply_markup=_saved_keyboard(),
            disable_web_page_preview=True,
        )


async def _handle_saved_delete_number(message: Message, context: AppContext, state: FSMContext) -> None:
    raw_value = (message.text or "").strip()
    if not raw_value.isdigit():
        await _answer(
            message,
            context,
            "Send a plain number, for example <b>3</b>, or press Exit.",
            reply_markup=_saved_keyboard(),
        )
        return

    rows = context.database.liked_vacancies()
    if not rows:
        await state.set_state(SavedVacancyStates.browsing)
        await _answer(message, context, "❤️ <b>No saved vacancies to delete.</b>", reply_markup=_saved_keyboard())
        return

    number = int(raw_value)
    if number < 1 or number > len(rows):
        await _answer(
            message,
            context,
            f"Number out of range. Send a number from <b>1</b> to <b>{len(rows)}</b>, or press Exit.",
            reply_markup=_saved_keyboard(),
        )
        return

    row = rows[number - 1]
    deleted = context.database.delete_liked_vacancy(int(row["id"]))
    await state.set_state(SavedVacancyStates.browsing)
    if not deleted:
        await _answer(
            message,
            context,
            "That saved vacancy was already changed. Here is the updated saved list.",
            reply_markup=_saved_keyboard(),
        )
    else:
        await _answer(
            message,
            context,
            format_deleted_saved_vacancy(row, number),
            reply_markup=_saved_keyboard(),
            disable_web_page_preview=True,
        )
    await _send_saved_vacancies(message, context)


async def _send_rejected(message: Message, context: AppContext) -> None:
    await _answer(
        message,
        context,
        format_rejected_vacancies(context.database.latest_rejected(limit=10)),
        disable_web_page_preview=True,
    )


async def _send_stats(message: Message, context: AppContext) -> None:
    await _answer(message, context, format_stats(context.database.get_stats()))


async def _send_settings(message: Message, context: AppContext) -> None:
    await _answer(message, context, format_settings(context.config))


async def _start_tg_pull(message: Message, context: AppContext) -> None:
    if context.pull_lock.locked():
        await _answer(message, context, "⏳ <b>A search is already running.</b>\nPlease wait.")
        return

    await _answer(message, context, "🔎 <b>Searching Telegram channels...</b>")
    async with context.pull_lock:
        await _run_pull_tg(message, context)


async def _start_sites_pull(message: Message, context: AppContext) -> None:
    if context.pull_lock.locked():
        await _answer(message, context, "⏳ <b>A search is already running.</b>\nPlease wait.")
        return

    await _answer(message, context, "🌐 <b>Searching websites...</b>")
    async with context.pull_lock:
        await _run_pull_sites(message, context)


async def _run_pull_tg(message: Message, context: AppContext) -> None:
    logger.info("Manual /pull_tg started by user_id=%s", context.owner_id)

    try:
        channels = read_channels(context.base_dir / "channels.txt")
        channels_empty = not channels
        posts_per_channel = _config_int(context.config, "telegram_posts_per_channel", 30)
        max_results = _config_int(context.config, "max_results_per_pull", 20)
        latest_limit = _config_int(context.config, "telegram_latest_limit", 10)
        resend_latest = _config_bool(context.config, "telegram_resend_latest_on_pull", False)

        telegram_result = (
            await fetch_telegram_vacancies(
                context.telethon_client,
                channels,
                posts_per_channel=posts_per_channel,
            )
            if channels
            else SourceResult()
        )

        filtered = _filter_candidates(telegram_result.vacancies, context.config, context.database)
        if resend_latest:
            mode = f"latest {latest_limit} matching posts"
            unique = _prepare_current_pull_vacancies(_sort_latest(filtered.matched))
            queued = unique.vacancies[:latest_limit]
            duplicates = unique.duplicates
            cross_channel_duplicates = unique.cross_channel_duplicates
            already_sent = 0
        else:
            mode = "new offers only"
            fresh = _prepare_fresh_vacancies(context.database, filtered.matched)
            queued = fresh.vacancies[:max_results]
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

        await _answer(
            message,
            context,
            format_tg_pull_report(
                mode=mode,
                posts_checked=telegram_result.checked,
                matched=len(filtered.matched),
                hard_rejected=filtered.rejected,
                duplicates=duplicates,
                cross_channel_duplicates=cross_channel_duplicates,
                already_sent=already_sent,
                sent_now=queued_now,
                pending=pending,
                source_errors=telegram_result.errors,
                channels_empty=channels_empty,
            ),
        )
        if queued_now:
            await _send_next_review_vacancy(message, context)
    except Exception:
        logger.exception("Critical /pull_tg error")
        await _answer(message, context, "⚠️ <b>/pull_tg failed.</b>\nDetails were written to logs/app.log.")


async def _run_pull_sites(message: Message, context: AppContext) -> None:
    logger.info("Manual /pull_sites started by user_id=%s", context.owner_id)

    try:
        sites = load_sites(context.base_dir / "sites.yaml")
        request_timeout = _config_int(context.config, "website_request_timeout", 15)
        max_results = _config_int(context.config, "max_results_per_pull", 20)
        user_agent = str(context.config.get("website_user_agent", "Mozilla/5.0 TelegramJobPullBot/1.0"))
        website_headers = dict(context.config.get("website_headers") or {})
        debug_parsing = bool(context.config.get("debug_parsing", True))
        detail_pages_limit = _config_int(context.config, "website_detail_pages_limit", max_results)
        detail_delay_seconds = float(context.config.get("website_detail_delay_seconds", 0.7))

        loop = asyncio.get_running_loop()
        website_result = await loop.run_in_executor(
            None,
            fetch_website_vacancies,
            sites,
            request_timeout,
            user_agent,
            website_headers,
            detail_pages_limit,
            detail_delay_seconds,
        )

        filtered = _filter_candidates(website_result.vacancies, context.config, context.database)
        _log_website_match_counts(website_result, filtered.matched)
        fresh = _prepare_fresh_vacancies(context.database, filtered.matched)
        queued = fresh.vacancies[:max_results]
        new_sendable = len(fresh.vacancies)
        detail_pages_fetched = _website_detail_pages_fetched(website_result)
        queued_now = _save_pending_vacancies(context.database, queued)
        insert_duplicates = max(0, len(queued) - queued_now)
        fresh.duplicates += insert_duplicates
        pending = context.database.pending_review_count()

        context.database.increment_stats(
            {
                "pull_sites_total": 1,
                "sites_checked_total": website_result.checked,
                "site_cards_found_total": website_result.cards_found,
                "site_parsed_total": website_result.parsed,
                "matched_sites_total": len(filtered.matched),
                "rejected_total": filtered.rejected,
                "rejected_sites_total": filtered.rejected,
                "hard_rejected_total": filtered.hard_rejected,
                "hard_rejected_sites_total": filtered.hard_rejected,
                "duplicates_total": fresh.duplicates,
                "duplicates_sites_total": fresh.duplicates,
                "cross_channel_duplicates_total": fresh.cross_channel_duplicates,
                "cross_channel_duplicates_sites_total": fresh.cross_channel_duplicates,
                "already_sent_total": fresh.already_sent,
                "already_sent_sites_total": fresh.already_sent,
                "queued_total": queued_now,
                "queued_sites_total": queued_now,
                "source_errors_sites_total": website_result.errors,
            }
        )

        logger.info(
            "/pull_sites finished: checked=%s cards=%s parsed=%s matched=%s rejected=%s hard_rejected=%s duplicates=%s already_sent=%s queued_now=%s pending=%s errors=%s",
            website_result.checked,
            website_result.cards_found,
            website_result.parsed,
            len(filtered.matched),
            filtered.rejected,
            filtered.hard_rejected,
            fresh.duplicates,
            fresh.already_sent,
            queued_now,
            pending,
            website_result.errors,
        )

        debug_lines = _website_debug_lines(website_result) if debug_parsing else _website_dynamic_notice(website_result)
        await _answer(
            message,
            context,
            format_sites_pull_report(
                websites_checked=website_result.checked,
                vacancy_cards_found=website_result.cards_found,
                parsed_cards=website_result.parsed,
                matched=len(filtered.matched),
                hard_rejected=filtered.rejected,
                detail_pages_fetched=detail_pages_fetched,
                duplicates=fresh.duplicates,
                already_sent=fresh.already_sent,
                new_sendable=new_sendable,
                sent_now=queued_now,
                pending=pending,
                source_errors=website_result.errors,
                debug_summaries=debug_lines,
            ),
        )
        if queued_now:
            await _send_next_review_vacancy(message, context)
    except Exception:
        logger.exception("Critical /pull_sites error")
        await _answer(message, context, "⚠️ <b>/pull_sites failed.</b>\nDetails were written to logs/app.log.")


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
            if bool(duplicate.get("sent")) or state in {"liked", "disliked", "deleted"}:
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
    for existing in fresh:
        if vacancy.content_hash_exact and vacancy.content_hash_exact == existing.content_hash_exact:
            return existing
        if vacancy.content_hash_normalized and vacancy.content_hash_normalized == existing.content_hash_normalized:
            return existing
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


async def _start_paginated_delivery(
    message: Message,
    context: AppContext,
    source_type: str,
    vacancies: list[Vacancy],
    message_delay: float,
) -> int:
    if not vacancies:
        context.pagination_sessions.pop(context.owner_id, None)
        return 0

    session = PaginationSession(
        source_type=source_type,
        vacancies=vacancies,
        offset=0,
        batch_size=_batch_size(context),
        message_delay=message_delay,
    )
    context.pagination_sessions[context.owner_id] = session
    sent_now = await _send_next_batch(message, context, session)
    if session.offset >= len(session.vacancies):
        context.pagination_sessions.pop(context.owner_id, None)
        await _answer(message, context, format_no_more_vacancies())
    else:
        await _answer(
            message,
            context,
            format_pagination_prompt(session.offset, len(session.vacancies)),
            reply_markup=_pagination_keyboard(source_type),
        )
    return sent_now


async def _send_next_batch(message: Message, context: AppContext, session: PaginationSession) -> int:
    batch = session.vacancies[session.offset : session.offset + session.batch_size]
    sent = 0
    for vacancy in batch:
        try:
            await _answer(message, context, format_vacancy(vacancy), disable_web_page_preview=True)
            context.database.mark_sent_by_hashes(
                vacancy.content_hash_exact or vacancy.content_hash,
                vacancy.content_hash_normalized,
                vacancy.source_type,
            )
            sent += 1
            session.offset += 1
            await asyncio.sleep(session.message_delay)
        except TelegramAPIError:
            logger.exception("Failed to send vacancy to Telegram: %s", vacancy.content_hash)
    return sent


async def _handle_pagination(callback: CallbackQuery, context: AppContext) -> None:
    data = callback.data or ""
    parts = data.split(":")
    if len(parts) != 3:
        await callback.answer()
        return

    _, action, source_type = parts
    session = context.pagination_sessions.get(callback.from_user.id)
    if not session or session.source_type != source_type:
        await callback.answer("No active batch.", show_alert=False)
        return

    if not isinstance(callback.message, Message):
        await callback.answer()
        return

    if action == "stop":
        remaining = max(0, len(session.vacancies) - session.offset)
        context.pagination_sessions.pop(callback.from_user.id, None)
        await callback.answer("Stopped")
        await _answer(callback.message, context, format_pagination_stopped(remaining))
        return

    if action != "next":
        await callback.answer()
        return

    await callback.answer()
    sent_now = await _send_next_batch(callback.message, context, session)
    if sent_now:
        context.database.increment_stats(
            {
                "sent_total": sent_now,
                f"sent_{'tg' if source_type == 'telegram' else 'sites'}_total": sent_now,
            }
        )

    if session.offset >= len(session.vacancies):
        context.pagination_sessions.pop(callback.from_user.id, None)
        await _answer(callback.message, context, format_no_more_vacancies())
    else:
        await _answer(
            callback.message,
            context,
            format_pagination_prompt(session.offset, len(session.vacancies)),
            reply_markup=_pagination_keyboard(source_type),
        )


def _log_website_match_counts(result: SourceResult, matched: list[Vacancy]) -> None:
    matched_by_page = Counter(str(vacancy.metadata.get("page_url", "")) for vacancy in matched)
    for summary in result.source_summaries:
        url = str(summary.get("url", ""))
        matched_count = matched_by_page.get(url, 0)
        summary["matched"] = matched_count
        logger.info(
            "Website matched: name=%s url=%s final_url=%s status=%s length=%s cards=%s parsed=%s matched=%s zero_reason=%s",
            summary.get("name"),
            url,
            summary.get("final_url"),
            summary.get("status_code"),
            summary.get("content_length"),
            summary.get("cards_found"),
            summary.get("parsed"),
            matched_count,
            summary.get("zero_reason"),
        )


def _website_debug_lines(result: SourceResult) -> list[str]:
    lines: list[str] = []
    for summary in result.source_summaries:
        name = summary.get("name", "Website")
        cards = int(summary.get("cards_found") or 0)
        matched = int(summary.get("matched") or 0)
        parsed = int(summary.get("parsed") or 0)
        zero_reason = summary.get("zero_reason")
        status = summary.get("status_code") or "?"
        if cards == 0:
            reason = "likely dynamic/bot-protected page" if "Robota.ua" in str(name) else (zero_reason or "likely dynamic page or selector mismatch")
            lines.append(f"• {name}: 0 cards — {reason} (status {status})")
        else:
            details = int(summary.get("details_fetched") or 0)
            skipped = int(summary.get("details_skipped_by_limit") or 0)
            suffix = f", {details} details fetched"
            if skipped:
                suffix += f", {skipped} details skipped by limit"
            lines.append(f"• {name}: {cards} cards, {parsed} parsed, {matched} matched{suffix}")
    return lines[:15]


def _website_detail_pages_fetched(result: SourceResult) -> int:
    return sum(int(summary.get("details_fetched") or 0) for summary in result.source_summaries)


def _website_dynamic_notice(result: SourceResult) -> list[str] | None:
    if any(bool(summary.get("possible_dynamic")) for summary in result.source_summaries):
        return [
            "• Some pages returned 0 vacancy cards — possible dynamic page or selector mismatch. "
            "Set debug_parsing: true for details."
        ]
    return None


async def _set_command_menu(bot: Bot) -> None:
    try:
        await bot.set_my_commands(
            [
                BotCommand(command="start", description="Open bot buttons"),
                BotCommand(command="pull_tg", description="Search Telegram channels"),
                BotCommand(command="pull_sites", description="Search job websites"),
                BotCommand(command="review", description="Review pending vacancies"),
                BotCommand(command="saved", description="Manage liked vacancies"),
                BotCommand(command="rejected", description="Recent rejected vacancies"),
                BotCommand(command="stats", description="Statistics"),
                BotCommand(command="settings", description="Current filters"),
                BotCommand(command="help", description="Help"),
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
