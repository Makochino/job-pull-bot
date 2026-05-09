from __future__ import annotations

import html
import re
from datetime import datetime
from typing import Any

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


def _clean_field(value: str | None, limit: int = 220) -> str:
    value = clean_vacancy_text(value or "")
    if not value:
        return "not specified"
    return truncate_text(value, limit)


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
    text = truncate_text(clean_text_for_display(vacancy.text), 1900)
    link = vacancy.link or "not available"
    message = f"""⭐ <b>Relevance:</b> {vacancy.score}/10

<b>Text:</b>
{_html(text)}

<b>Link:</b>
{_html(link)}"""
    return _clip_message(message)


def format_website_vacancy(vacancy: Vacancy) -> str:
    title = _clean_field(vacancy.title or "Untitled", 220)
    text = clean_vacancy_text(vacancy.text)
    salary = extract_salary(text)
    schedule = extract_schedule(text)
    workplace = extract_location("\n".join([vacancy.title or "", text]))
    phone = extract_phone(text)
    link = vacancy.link or "not available"

    message = f"""🌐 <b>Website vacancy</b>

⭐ <b>Relevance:</b> {vacancy.score}/10

💼 <b>Title:</b>
{_html(title)}

💰 <b>Salary:</b>
{_html(salary)}

🕒 <b>Schedule:</b>
{_html(schedule)}

📍 <b>Workplace:</b>
{_html(workplace)}

☎️ <b>Phone:</b>
{_html(phone)}

🔗 <b>Link:</b>
{_html(link)}"""
    return _clip_message(message)


def format_latest(rows: list[Any]) -> str:
    if not rows:
        return (
            "😕 <b>No saved matching vacancies yet.</b>\n\n"
            "Use the buttons below or /pull_tg and /pull_sites to search."
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


def format_stats(stats: dict[str, Any]) -> str:
    return (
        "📊 <b>Stats</b>\n\n"
        f"Telegram vacancies saved: <b>{stats.get('telegram_saved', 0)}</b>\n"
        f"Website vacancies saved: <b>{stats.get('website_saved', 0)}</b>\n"
        f"Total saved: <b>{stats.get('total_saved', 0)}</b>\n"
        f"Sent: <b>{stats.get('sent_total', stats.get('sent_saved', 0))}</b>\n"
        f"Duplicates: <b>{stats.get('duplicates_total', 0)}</b>\n"
        f"Cross-channel duplicates: <b>{stats.get('cross_channel_duplicates_total', 0)}</b>\n"
        f"Already sent: <b>{stats.get('already_sent_total', 0)}</b>\n"
        f"Telegram pulls: <b>{stats.get('pull_tg_total', 0)}</b>\n"
        f"Website pulls: <b>{stats.get('pull_sites_total', 0)}</b>\n"
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
        f"max_results_per_pull: <b>{config.get('max_results_per_pull', 20)}</b>\n"
        f"batch_size: <b>{config.get('batch_size', 5)}</b>\n"
        f"telegram_resend_latest_on_pull: <b>{config.get('telegram_resend_latest_on_pull', False)}</b>\n"
        f"telegram_latest_limit: <b>{config.get('telegram_latest_limit', 10)}</b>\n"
        f"auto_delete_messages_after_seconds: <b>{config.get('auto_delete_messages_after_seconds', 600)}</b>\n"
        f"notify_user_on_startup: <b>{config.get('notify_user_on_startup', True)}</b>\n"
        f"website debug mode: <b>{bool(config.get('debug_parsing', True))}</b>\n"
        f"female-only rejection enabled: <b>{bool(filters.get('female_only_reject_patterns'))}</b>\n"
        f"hard reject patterns count: <b>{hard_reject_count}</b>\n\n"
        "<b>Core keywords:</b>\n"
        + _html(core)
    )


def format_no_more_vacancies() -> str:
    return "✅ <b>No more vacancies.</b>"


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
        f"Checked: <b>{posts_checked}</b>",
        f"Matched: <b>{matched}</b>",
        f"Rejected: <b>{hard_rejected}</b>",
        f"Duplicates: <b>{duplicates}</b>",
        f"Cross-channel duplicates: <b>{cross_channel_duplicates}</b>",
        f"Already sent: <b>{already_sent}</b>",
        f"Sent now: <b>{sent_now}</b>",
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
        f"Already sent: <b>{already_sent}</b>",
        f"New sendable: <b>{new_sendable}</b>",
        f"Sent now: <b>{sent_now}</b>",
        f"Pending in this batch: <b>{pending}</b>",
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
