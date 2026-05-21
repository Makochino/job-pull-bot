from __future__ import annotations

import html
import math
import re
from datetime import datetime
from typing import Any

from .extraction import OTHER_ROLE_TERMS, ROLE_GROUPS, normalize_match_text
from .utils import Vacancy, clean_text_for_display, clean_vacancy_text, truncate_text


TELEGRAM_MESSAGE_LIMIT = 3900


def _html(value: str | None) -> str:
    return html.escape(value or "not specified", quote=False)


def _format_date(value: str | None) -> str:
    if not value:
        return "not specified"
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    return parsed.strftime("%Y-%m-%d %H:%M")


def _clip_message(value: str) -> str:
    return truncate_text(value, TELEGRAM_MESSAGE_LIMIT)


def _display_original_text(value: str | None) -> str:
    if not value:
        return ""
    return value.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "").strip("\n")


def _split_long_raw_line(line: str, max_escaped_len: int) -> list[str]:
    chunks: list[str] = []
    current = ""
    for char in line:
        if current and len(_html(current + char)) > max_escaped_len:
            chunks.append(current)
            current = char
        else:
            current += char
    if current:
        chunks.append(current)
    return chunks


def _split_raw_text_for_html(value: str, max_escaped_len: int) -> list[str]:
    if not value:
        return [""]

    chunks: list[str] = []
    current = ""
    for line in value.splitlines(keepends=True):
        pieces = (
            _split_long_raw_line(line, max_escaped_len)
            if len(_html(line)) > max_escaped_len
            else [line]
        )
        for piece in pieces:
            if current and len(_html(current + piece)) > max_escaped_len:
                chunks.append(current.rstrip("\n"))
                current = piece
            else:
                current += piece
    if current or not chunks:
        chunks.append(current.rstrip("\n"))
    return chunks


def _format_text_messages(header: str, text: str | None, link: str | None) -> list[str]:
    display_text = _display_original_text(text)
    display_link = link or "not available"
    body_chunks = _split_raw_text_for_html(display_text, TELEGRAM_MESSAGE_LIMIT - 600)

    messages: list[str] = []
    last_index = len(body_chunks) - 1
    for index, chunk in enumerate(body_chunks):
        parts: list[str] = []
        if index == 0:
            parts.append(header)
            parts.append("")
            parts.append("Text:")
        if chunk:
            if parts:
                if index == 0:
                    parts[-1] = f"{parts[-1]}\n{_html(chunk)}"
                else:
                    parts.append(_html(chunk))
            else:
                parts.append(_html(chunk))
        if index == last_index:
            if parts:
                parts.append("")
            parts.append(f"Link:\n{_html(display_link)}")

        message = "\n".join(parts)
        if len(message) <= TELEGRAM_MESSAGE_LIMIT:
            messages.append(message)
            continue

        overflow_chunks = _split_raw_text_for_html(chunk, TELEGRAM_MESSAGE_LIMIT - 1000)
        for overflow_index, overflow_chunk in enumerate(overflow_chunks):
            overflow_parts: list[str] = []
            if index == 0 and overflow_index == 0:
                overflow_parts.append(header)
                overflow_parts.append("")
                overflow_parts.append(f"Text:\n{_html(overflow_chunk)}")
            else:
                overflow_parts.append(_html(overflow_chunk))
            if index == last_index and overflow_index == len(overflow_chunks) - 1:
                overflow_parts.append("")
                overflow_parts.append(f"Link:\n{_html(display_link)}")
            messages.append("\n".join(overflow_parts))
    return messages


def _truncate_display_text(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 3)].rstrip() + "..."


def _clean_field(value: str | None, limit: int = 220) -> str:
    value = clean_vacancy_text(value or "")
    if not value:
        return "not specified"
    return truncate_text(value, limit)


def _row_get(row: Any, key: str, default: Any = "") -> Any:
    try:
        value = row[key]
    except (KeyError, IndexError, TypeError):
        return default
    return default if value is None else value


def _known(value: str | None) -> bool:
    return bool(value and value != "not specified")


def _line_with_patterns(text: str, patterns: list[str], limit: int = 180) -> str:
    cleaned = clean_vacancy_text(text)
    lines = cleaned.splitlines() or [cleaned]
    for line in lines:
        if any(re.search(pattern, line, flags=re.IGNORECASE | re.UNICODE) for pattern in patterns):
            return truncate_text(line, limit)
    return ""


def extract_salary(text: str) -> str:
    patterns = [
        r"\b(?:з/п|зарплата|оплата|salary|ставка|дохід|доход)\b.{0,120}",
        r"(?:від|от|до)?\s*\d[\d\s]{2,8}\s*(?:грн|₴|uah)\b.{0,80}",
        r"\b\d{2,3}\s?\d{3}\b\s*(?:грн|₴|uah)?.{0,60}",
    ]
    found = _line_with_patterns(text, patterns)
    return found or "not specified"


def extract_schedule(text: str) -> str:
    patterns = [
        r"\b(?:график|графік|schedule|режим)\b.{0,140}",
        r"\b[2345]/[2345]\b.{0,100}",
        r"\b\d{1,2}[:.]\d{2}\s*[-–—]\s*\d{1,2}[:.]\d{2}\b.{0,80}",
        r"\b(?:с|з)\s*\d{1,2}[:.]\d{2}\s*(?:до|-|–|—)\s*\d{1,2}[:.]\d{2}\b.{0,80}",
        r"\b(?:повний день|неповний день|полный день|неполный день|part-time|part time)\b.{0,100}",
        r"\b(?:посменно|позмінно|смены|зміни|смена|зміна)\b.{0,120}",
    ]
    found = _line_with_patterns(text, patterns)
    return found or "not specified"


def extract_phone(text: str) -> str:
    pattern = re.compile(
        r"(?:(?:\+?38[\s\-]?)?\(?0[\s\-]?\d{2}\)?[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2})",
        flags=re.UNICODE,
    )
    match = pattern.search(text)
    if not match:
        return "not specified"
    return truncate_text(match.group(0).strip(), 80)


def extract_location(text: str) -> str:
    patterns = [
        r"\b(?:Одесса|Одеса|Odesa|Odessa)\b.{0,140}",
        r"\b(?:центр|район|поселок|селище)\b.{0,140}",
        r"\b(?:улица|ул\.|вулиця|вул\.|адрес|адреса|провулок|проспект)\b.{0,160}",
        r"\b(?:ресторан|кафе|coffee|кав'ярня)\b.{0,120}(?:Одесса|Одеса|Odesa|Odessa|центр|район|улица|вулиця|адрес|адреса).{0,120}",
    ]
    found = _line_with_patterns(text, patterns)
    return found or "not specified"


def format_vacancy(vacancy: Vacancy) -> str:
    if vacancy.source_type == "telegram":
        return format_telegram_vacancy(vacancy)
    return format_website_vacancy(vacancy)


def format_telegram_vacancy(vacancy: Vacancy) -> str:
    return _format_text_card("🧾 <b>Vacancy</b>", vacancy.text, vacancy.link)


def format_website_vacancy(vacancy: Vacancy) -> str:
    return _format_text_card("🧾 <b>Vacancy</b>", vacancy.text, vacancy.link)


def format_latest(rows: list[Any]) -> str:
    if not rows:
        return (
            "😕 <b>No saved matching vacancies yet.</b>\n\n"
            "Use the buttons below or /pull_tg to search Telegram channels."
        )

    lines = ["📌 <b>Latest saved vacancies</b>"]
    for index, row in enumerate(rows, start=1):
        title = _html(truncate_text(str(row["title"] or "Untitled"), 90))
        source = _html(str(row["source"]))
        source_type = _html(str(row["source_type"]))
        score = row["score"]
        created_at = _html(_format_date(row["created_at"]))
        link = _html(row["link"] or "no link")
        sent = "sent" if int(row["sent"]) else "saved, not sent"
        lines.append(
            f"\n<b>{index}. {title}</b>\n"
            f"Source: {source} ({source_type})\n"
            f"Relevance: {score}/10\n"
            f"Status: {sent}\n"
            f"Saved: {created_at}\n"
            f"{link}"
        )
    return _clip_message("\n".join(lines))


def _format_text_card(header: str, text: str, link: str | None, text_limit: int | None = None) -> str:
    display_link = link or "not available"
    if text_limit is None:
        text_limit = max(200, TELEGRAM_MESSAGE_LIMIT - len(header) - len(display_link) - 40)
    display_text = _truncate_display_text(_display_original_text(text), text_limit)
    return _clip_message(
        f"{header}\n\n"
        f"{_html(display_text)}\n\n"
        f"<b>Link:</b>\n{_html(display_link)}"
    )


def _format_db_vacancy_card(row: Any, header: str, text_limit: int | None = None) -> str:
    raw_text = str(_row_get(row, "text", ""))
    link = str(_row_get(row, "link", "") or "not available")
    return _format_text_card(header, raw_text, link, text_limit=text_limit)


def format_review_vacancy(row: Any, left_count: int) -> str:
    return format_review_vacancy_messages(row, left_count)[0]


def format_review_vacancy_messages(row: Any, left_count: int) -> list[str]:
    header = f"🧾 Vacancy review\nVacancies left: {left_count}"
    raw_text = str(_row_get(row, "text", ""))
    link = str(_row_get(row, "link", "") or "not available")
    return _format_text_messages(header, raw_text, link)


def format_saved_vacancy_messages(row: Any, number: int) -> list[str]:
    header = f"❤️ <b>Saved vacancy #{number}</b>"
    raw_text = str(_row_get(row, "text", ""))
    link = str(_row_get(row, "link", "") or "not available")
    return _format_text_messages(header, raw_text, link)


def _strip_place_name(value: str) -> str:
    value = re.sub(r"\s+", " ", value.strip(" «»\"'“”„:,-"))
    value = re.split(
        r"\s+\b(?:требуется|требуются|потрібн[аіi]?|потрiбн[аіi]?|шукаємо|ищем|набираем|у|в|на)\b",
        value,
        maxsplit=1,
        flags=re.IGNORECASE | re.UNICODE,
    )[0]
    return truncate_text(value.strip(" «»\"'“”„:,-"), 60)


def detect_establishment_name(text: str | None) -> str:
    raw_text = _display_original_text(text)
    if not raw_text:
        return ""

    patterns = (
        r"\b(?:ресторан|кафе|бар|готель|отель|restaurant|cafe|hotel)\s+[«\"“„']([^»\"“”']{2,80})[»\"“”']",
        r"\b(?:в|у)\s+(?:ресторан|кафе|бар|готель|отель|restaurant|cafe|hotel)\s+([A-ZА-ЯІЇЄҐ][^\n,.;:]{1,80})",
        r"\b(?:ресторан|кафе|бар|готель|отель|restaurant|cafe|hotel)\s+([A-ZА-ЯІЇЄҐ][^\n,.;:]{1,80})",
    )
    for pattern in patterns:
        match = re.search(pattern, raw_text, flags=re.IGNORECASE | re.UNICODE)
        if match:
            name = _strip_place_name(match.group(1))
            if name:
                return name

    role_terms = tuple(term for group in ROLE_GROUPS for term in group.terms) + OTHER_ROLE_TERMS
    blocked_terms = (
        "требуется",
        "требуются",
        "потріб",
        "потрiб",
        "ищем",
        "шукаємо",
        "зарплата",
        "з/п",
        "ставка",
        "график",
        "графік",
        "тел",
        "адрес",
        "вакан",
    )
    for raw_line in raw_text.splitlines()[:6]:
        line = _strip_place_name(re.sub(r"^[^\wА-Яа-яІіЇїЄєҐґ]+", "", raw_line))
        if not (2 <= len(line) <= 60):
            continue
        normalized = normalize_match_text(line)
        if any(term in normalized for term in blocked_terms):
            continue
        if any(normalize_match_text(term) in normalized for term in role_terms):
            continue
        if re.search(r"[A-ZА-ЯІЇЄҐ]", line) or "." in line:
            return line
    return ""


def format_saved_vacancies_page(rows: list[Any], page: int, page_size: int = 5) -> str:
    if not rows:
        return (
            "❤️ <b>No saved vacancies yet.</b>\n\n"
            "Like vacancies during review and they will appear here."
        )

    total = len(rows)
    page_size = max(1, page_size)
    page_count = max(1, math.ceil(total / page_size))
    page = min(max(0, page), page_count - 1)
    start = page * page_size
    end = min(total, start + page_size)

    lines = ["❤️ <b>Saved vacancies</b>"]
    if page_count > 1:
        lines.append(f"Page <b>{page + 1}</b>/<b>{page_count}</b>")

    for index, row in enumerate(rows[start:end], start=start + 1):
        title = truncate_text(str(_row_get(row, "title", "") or "").strip(), 80)
        name = detect_establishment_name(str(_row_get(row, "text", ""))) or title or "No info"
        link = str(_row_get(row, "link", "") or "not available")
        lines.append(f"\n#{index} — {_html(name)}\n{_html(link)}")
    return _clip_message("\n".join(lines))


def format_saved_vacancies(rows: list[Any]) -> list[str]:
    return [format_saved_vacancies_page(rows, page=0)]


def format_deleted_saved_vacancy(row: Any, number: int) -> str:
    title = _clean_field(str(_row_get(row, "title", "Untitled")), 120)
    return f"🗑 Deleted saved vacancy #{number}: <b>{_html(title)}</b>"


def format_no_pending_review() -> str:
    return "✅ <b>No more vacancies left to review.</b>"


def format_rejected_vacancies(rows: list[Any]) -> str:
    if not rows:
        return "🧾 <b>No rejected vacancies have been recorded yet.</b>"

    lines = ["🧾 <b>Recently rejected vacancies</b>"]
    for index, row in enumerate(rows, start=1):
        role = str(_row_get(row, "extracted_role", "") or "other")
        reason = _clean_field(str(_row_get(row, "reject_reason", "")), 180)
        text = truncate_text(clean_text_for_display(str(_row_get(row, "text", ""))), 420)
        score = int(_row_get(row, "score", 0) or 0)
        source = _clean_field(str(_row_get(row, "source", "")), 90)
        matched = _clean_field(str(_row_get(row, "matched_role_keywords", "[]")), 160)
        seen_count = int(_row_get(row, "seen_count", 1) or 1)
        link = str(_row_get(row, "link", "") or "not available")
        lines.append(
            f"\n<b>{index}. {role}</b>\n"
            f"Reason: {_html(reason)}\n"
            f"Score: <b>{score}/10</b>\n"
            f"Matched: {_html(matched)}\n"
            f"Source: {_html(source)} | Seen: <b>{seen_count}</b>\n"
            f"Link: {_html(link)}\n"
            f"{_html(text)}"
        )
    return _clip_message("\n".join(lines))


def format_stats(stats: dict[str, Any]) -> str:
    return (
        "📊 <b>Stats</b>\n\n"
        f"Telegram vacancies saved: <b>{stats.get('telegram_saved', 0)}</b>\n"
        f"Total saved: <b>{stats.get('total_saved', 0)}</b>\n"
        f"Pending review: <b>{stats.get('pending_review', 0)}</b>\n"
        f"Liked / saved: <b>{stats.get('liked_saved', 0)}</b>\n"
        f"Disliked / reviewed: <b>{stats.get('disliked_reviewed', 0)}</b>\n"
        f"Deleted saved: <b>{stats.get('deleted_saved', 0)}</b>\n"
        f"Rejected audit rows: <b>{stats.get('rejected_saved', 0)}</b>\n"
        f"Sent by old flow: <b>{stats.get('sent_total', stats.get('sent_saved', 0))}</b>\n"
        f"Queued for review: <b>{stats.get('queued_total', 0)}</b>\n"
        f"Duplicates: <b>{stats.get('duplicates_total', 0)}</b>\n"
        f"Cross-channel duplicates: <b>{stats.get('cross_channel_duplicates_total', 0)}</b>\n"
        f"Already sent: <b>{stats.get('already_sent_total', 0)}</b>\n"
        f"Telegram pulls: <b>{stats.get('pull_tg_total', 0)}</b>\n"
        f"Hard rejected: <b>{stats.get('hard_rejected_total', 0)}</b>"
    )


def format_settings(config: dict[str, Any]) -> str:
    filters = config.get("filters", {})
    core_keywords = filters.get("core_keywords", [])
    hard_reject_count = (
        len(filters.get("hard_reject_keywords", []))
        + len(filters.get("female_only_reject_patterns", []))
        + len(filters.get("scam_reject_patterns", []))
    )

    core = ", ".join(str(item) for item in core_keywords[:30]) if core_keywords else "not configured"
    if len(core_keywords) > 30:
        core += f", ... (+{len(core_keywords) - 30})"

    return _clip_message(
        "⚙️ <b>Current settings</b>\n\n"
        f"Profile: <b>{_html(str(config.get('profile_name', 'restaurant/cafe jobs in Odesa')))}</b>\n"
        f"min_score: <b>{config.get('min_score', 5)}</b>\n"
        "telegram_scan_window: <b>last 48 hours only</b>\n"
        f"telegram_scan_max_messages_per_channel: <b>{config.get('telegram_scan_max_messages_per_channel', 0)}</b>\n"
        f"auto_delete_messages_after_seconds: <b>{config.get('auto_delete_messages_after_seconds', 600)}</b>\n"
        f"notify_user_on_startup: <b>{config.get('notify_user_on_startup', True)}</b>\n"
        "female-only rejection enabled: <b>True</b>\n"
        f"hard reject patterns count: <b>{hard_reject_count}</b>\n\n"
        "<b>Core keywords:</b>\n"
        + _html(core)
    )


def format_no_more_vacancies() -> str:
    return "✅ <b>No more vacancies left to review.</b>"


def format_pagination_prompt(sent: int, total: int) -> str:
    remaining = max(0, total - sent)
    return (
        "📦 <b>More vacancies available.</b>\n\n"
        f"Sent now: <b>{sent}</b>\n"
        f"Remaining: <b>{remaining}</b>"
    )


def format_pagination_stopped(remaining: int) -> str:
    return f"Stopped. Remaining vacancies were not sent: <b>{max(0, remaining)}</b>."


def format_tg_pull_report(
    mode: str,
    posts_checked: int,
    matched: int,
    hard_rejected: int,
    duplicates: int,
    cross_channel_duplicates: int,
    already_sent: int,
    sent_now: int,
    pending: int,
    source_errors: int,
    channels_empty: bool,
) -> str:
    lines = [
        "📊 <b>Telegram report</b>",
        "",
        f"Mode: <b>{_html(mode)}</b>",
        "Searching Telegram vacancies from the last 48 hours only.",
        f"Checked: <b>{posts_checked}</b>",
        f"Matched: <b>{matched}</b>",
        f"Rejected: <b>{hard_rejected}</b>",
        f"Duplicates: <b>{duplicates}</b>",
        f"Cross-channel duplicates: <b>{cross_channel_duplicates}</b>",
        f"Already known/reviewed: <b>{already_sent}</b>",
        f"Queued now: <b>{sent_now}</b>",
        f"Errors: <b>{source_errors}</b>",
    ]
    if channels_empty:
        lines.append("\nchannels.txt is empty. Add Telegram channels first.")
    elif matched == 0:
        lines.append("\n😕 <b>No matching Telegram posts found.</b>")
    elif sent_now == 0 and pending == 0:
        lines.append("\n😕 <b>No new Telegram vacancies found.</b>")
    return "\n".join(lines)


def format_sites_pull_report(
    websites_checked: int,
    vacancy_cards_found: int,
    parsed_cards: int,
    matched: int,
    hard_rejected: int,
    detail_pages_fetched: int,
    duplicates: int,
    already_sent: int,
    new_sendable: int,
    sent_now: int,
    pending: int,
    source_errors: int,
    debug_summaries: list[str] | None = None,
) -> str:
    lines = [
        "📊 <b>Website report</b>",
        "",
        f"Websites checked: <b>{websites_checked}</b>",
        f"Cards found: <b>{vacancy_cards_found}</b>",
        f"Parsed cards: <b>{parsed_cards}</b>",
        f"Matched by filter: <b>{matched}</b>",
        f"Rejected: <b>{hard_rejected}</b>",
        f"Detail pages fetched: <b>{detail_pages_fetched}</b>",
        f"Duplicates: <b>{duplicates}</b>",
        f"Already known/reviewed: <b>{already_sent}</b>",
        f"New sendable: <b>{new_sendable}</b>",
        f"Queued now: <b>{sent_now}</b>",
        f"Pending review in this batch: <b>{pending}</b>",
        f"Errors: <b>{source_errors}</b>",
    ]
    if matched == 0:
        lines.append("\n😕 <b>No matching website vacancies found.</b>")
    elif new_sendable == 0:
        lines.append("\n✅ <b>No new website vacancies.</b>\n\nAll matching vacancies were already sent or duplicated.")
    if debug_summaries:
        lines.append("")
        lines.append("🌐 <b>Website debug:</b>")
        lines.extend(_html(line) for line in debug_summaries[:15])
    return _clip_message("\n".join(lines))
