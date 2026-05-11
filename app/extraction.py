from __future__ import annotations

import re
from dataclasses import dataclass

from .utils import (
    Vacancy,
    clean_text,
    clean_text_for_display,
    clean_vacancy_text,
    normalize_text_for_hashing,
    normalized_content_hash,
    sha256_text,
    truncate_text,
)


@dataclass(frozen=True)
class RoleGroup:
    label: str
    display: str
    terms: tuple[str, ...]


ROLE_GROUPS: tuple[RoleGroup, ...] = (
    RoleGroup(
        "waiter",
        "Waiter",
        (
            "waiter",
            "waitress",
            "официант",
            "официантка",
            "официанты",
            "официантов",
            "офіціант",
            "офіціантка",
            "офіціанти",
            "офіціантів",
        ),
    ),
    RoleGroup(
        "runner",
        "Runner / assistant waiter",
        (
            "runner",
            "раннер",
            "ранер",
            "раннеры",
            "ранеры",
            "помощник официанта",
            "помічник офіціанта",
        ),
    ),
    RoleGroup("hostess", "Hostess", ("hostess", "хостес")),
    RoleGroup(
        "bartender",
        "Bartender",
        ("bartender", "бармен", "бармены", "бармени", "барменов"),
    ),
    RoleGroup("barista", "Barista", ("barista", "бариста", "баристы", "баристи")),
    RoleGroup(
        "kitchen helper",
        "Kitchen helper",
        (
            "помощник кухни",
            "помічник кухаря",
            "кухонный работник",
            "кухонний працівник",
        ),
    ),
    RoleGroup(
        "restaurant staff",
        "Restaurant staff",
        (
            "restaurant staff",
            "cafe staff",
            "персонал ресторана",
            "персонал ресторану",
            "персонал кафе",
            "помощник в ресторан",
            "помічник в ресторан",
            "restaurant assistant",
            "менеджер ресторана",
            "менеджер ресторану",
            "администратор ресторана",
            "адміністратор ресторану",
            "service manager restaurant",
            "service manager cafe",
        ),
    ),
)

OTHER_ROLE_TERMS: tuple[str, ...] = (
    "заготовщица",
    "заготовщик",
    "повар",
    "повара",
    "кухар",
    "кухарі",
    "порто",
    "посудомой",
    "посудомий",
    "мойщ",
    "прибираль",
    "уборщ",
    "администратор",
    "адміністратор",
    "кассир",
    "касир",
    "продавец",
    "продавець",
    "сушист",
    "пиццайоло",
    "піца",
    "доставка",
    "курьер",
    "кур'єр",
)


def normalize_match_text(value: str) -> str:
    return (
        value.casefold()
        .replace("ё", "е")
        .replace("є", "е")
        .replace("і", "i")
        .replace("ї", "i")
    )


def _contains_phrase(text: str, phrase: str) -> bool:
    return normalize_match_text(phrase.strip()) in normalize_match_text(text)


def _matched_role_groups(text: str) -> list[tuple[RoleGroup, list[str]]]:
    matches: list[tuple[RoleGroup, list[str]]] = []
    for group in ROLE_GROUPS:
        terms = [term for term in group.terms if _contains_phrase(text, term)]
        if terms:
            matches.append((group, terms))
    return matches


def matched_role_keywords(text: str) -> list[str]:
    keywords: list[str] = []
    for _, terms in _matched_role_groups(text):
        keywords.extend(terms)
    return keywords


def detect_role_from_text(text: str) -> str:
    matches = _matched_role_groups(text)
    return matches[0][0].label if matches else "other"


def role_display(role: str) -> str:
    for group in ROLE_GROUPS:
        if group.label == role:
            return group.display
    return role or "not specified"


def _line_has_any_role(line: str) -> bool:
    if _matched_role_groups(line):
        return True
    return any(_contains_phrase(line, term) for term in OTHER_ROLE_TERMS)


def _looks_like_global_detail(line: str) -> bool:
    normalized = normalize_match_text(line)
    global_terms = (
        "тел",
        "phone",
        "контакт",
        "вопрос",
        "питання",
        "информац",
        "інформац",
        "адрес",
        "адреса",
        "улица",
        "ул.",
        "вул.",
        "переулок",
        "провулок",
        "питание",
        "харчування",
        "место",
        "локац",
        "район",
    )
    return bool(extract_contact(line) != "not specified" or any(term in normalized for term in global_terms))


def _clean_join(lines: list[str]) -> str:
    return clean_text_for_display("\n".join(line for line in lines if line.strip()))


def split_vacancy_candidates(vacancy: Vacancy) -> list[Vacancy]:
    if vacancy.source_type != "telegram":
        annotate_vacancy_fields(vacancy)
        return [vacancy]

    lines = vacancy.text.splitlines()
    if len([line for line in lines if line.strip()]) < 2:
        annotate_vacancy_fields(vacancy)
        return [vacancy]

    markers: list[tuple[int, list[tuple[RoleGroup, list[str]]]]] = []
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        role_matches = _matched_role_groups(line)
        if role_matches or any(_contains_phrase(line, term) for term in OTHER_ROLE_TERMS):
            markers.append((index, role_matches))

    target_markers = [(index, matches) for index, matches in markers if matches]
    if not target_markers:
        annotate_vacancy_fields(vacancy)
        return [vacancy]

    target_labels = {
        group.label
        for _, matches in target_markers
        for group, _ in matches
    }
    has_mixed_roles = len(markers) > 1 or len(target_labels) > 1
    if not has_mixed_roles:
        matches = target_markers[0][1]
        group, terms = matches[0]
        vacancy.role = group.label
        vacancy.matched_role_keywords = terms
        annotate_vacancy_fields(vacancy)
        return [vacancy]

    marker_indexes = [index for index, _ in markers]
    first_marker = marker_indexes[0]
    last_marker = marker_indexes[-1]
    shared_prefix = [line for line in lines[:first_marker] if line.strip() and not _line_has_any_role(line)]
    global_trailing = [
        line
        for line in lines[last_marker + 1 :]
        if line.strip() and _looks_like_global_detail(line) and not _line_has_any_role(line)
    ]

    candidates: list[Vacancy] = []
    candidate_index = 0
    for marker_position, (start, matches) in enumerate(markers):
        if not matches:
            continue
        next_start = len(lines)
        if marker_position + 1 < len(markers):
            next_start = markers[marker_position + 1][0]

        role_segment = [line for line in lines[start:next_start] if line.strip()]
        for group, terms in matches:
            candidate_index += 1
            candidate_lines = list(shared_prefix)
            candidate_lines.extend(role_segment)
            for line in global_trailing:
                if line not in candidate_lines:
                    candidate_lines.append(line)
            candidate_text = _clean_join(candidate_lines) or vacancy.text
            exact_hash = sha256_text(
                "|".join(
                    [
                        vacancy.content_hash_exact or vacancy.content_hash,
                        "role",
                        group.label,
                        str(start),
                        str(candidate_index),
                    ]
                )
            )
            normalized_text = normalize_text_for_hashing(
                "\n".join([group.display, candidate_text, vacancy.link or ""])
            )
            candidate = Vacancy(
                source=vacancy.source,
                source_type=vacancy.source_type,
                title=group.display,
                text=candidate_text,
                link=vacancy.link,
                published_at=vacancy.published_at,
                score=vacancy.score,
                content_hash=exact_hash,
                content_hash_exact=exact_hash,
                content_hash_normalized=normalized_content_hash(normalized_text),
                content_normalized=normalized_text,
                parent_content_hash=vacancy.content_hash_exact or vacancy.content_hash,
                location=vacancy.location,
                vacancy_type=group.label,
                role=group.label,
                matched_role_keywords=terms,
                metadata={
                    **vacancy.metadata,
                    "parent_content_hash": vacancy.content_hash_exact or vacancy.content_hash,
                    "role_marker_line": start,
                    "role_candidate_index": candidate_index,
                    "split_from_multi_role_post": True,
                },
            )
            annotate_vacancy_fields(candidate, full_text=vacancy.text)
            candidates.append(candidate)

    if not candidates:
        annotate_vacancy_fields(vacancy)
        return [vacancy]
    return candidates


def _line_with_patterns(text: str, patterns: list[str], limit: int = 180) -> str:
    cleaned = clean_vacancy_text(text)
    lines = cleaned.splitlines() or [cleaned]
    for line in lines:
        if any(re.search(pattern, line, flags=re.IGNORECASE | re.UNICODE) for pattern in patterns):
            return truncate_text(line, limit)
    return ""


def extract_salary(text: str) -> str:
    lines = clean_vacancy_text(text).splitlines()
    salary_keyword = re.compile(
        r"\b(?:з/п|зарплата|оплата|salary|ставка|дохід|доход)\b",
        flags=re.IGNORECASE | re.UNICODE,
    )
    money_pattern = re.compile(
        r"(?:від|от|до)?\s*\d[\d\s]{2,8}\s*(?:грн|₴|uah)\b|\b\d{2,3}\s?\d{3}\b",
        flags=re.IGNORECASE | re.UNICODE,
    )
    for index, line in enumerate(lines):
        if not salary_keyword.search(line):
            continue
        if money_pattern.search(line):
            return truncate_text(line, 180)
        for next_line in lines[index + 1 : index + 3]:
            if money_pattern.search(next_line):
                return truncate_text(next_line, 180)

    patterns = [
        r"\b(?:з/п|зарплата|оплата|salary|ставка|дохід|доход)\b.{0,120}",
        r"(?:від|от|до)?\s*\d[\d\s]{2,8}\s*(?:грн|₴|uah)\b.{0,80}",
        r"\b\d{2,3}\s?\d{3}\b\s*(?:грн|₴|uah)?.{0,60}",
    ]
    found = _line_with_patterns(text, patterns)
    return found or "not specified"


def extract_schedule(text: str) -> str:
    lines = clean_vacancy_text(text).splitlines()
    schedule_keyword = re.compile(r"\b(?:график|графік|schedule|режим)\b", flags=re.IGNORECASE | re.UNICODE)
    schedule_value = re.compile(
        r"\b[2345]\s*/\s*[2345]\b|"
        r"\b(?:с|з)?\s*\d{1,2}\s*[:.]\s*\d{2}\s*(?:до|-|–|—)\s*\d{1,2}\s*[:.]\s*\d{2}\b|"
        r"\b(?:посменно|позмінно|смены|зміни|смена|зміна)\b",
        flags=re.IGNORECASE | re.UNICODE,
    )
    for index, line in enumerate(lines):
        if not schedule_keyword.search(line):
            continue
        if schedule_value.search(line):
            return truncate_text(line, 180)
        for next_line in lines[index + 1 : index + 3]:
            if schedule_value.search(next_line):
                return truncate_text(next_line, 180)

    patterns = [
        r"\b(?:график|графік|schedule|режим)\b.{0,140}",
        r"\b[2345]\s*/\s*[2345]\b.{0,100}",
        r"\b\d{1,2}\s*[:.]\s*\d{2}\s*[-–—]\s*\d{1,2}\s*[:.]\s*\d{2}\b.{0,80}",
        r"\b(?:с|з)\s*\d{1,2}\s*[:.]\s*\d{2}\s*(?:до|-|–|—)\s*\d{1,2}\s*[:.]\s*\d{2}\b.{0,80}",
        r"\b(?:полный день|неполный день|повний день|неповний день|part-time|part time)\b.{0,100}",
        r"\b(?:посменно|позмінно|смены|зміни|смена|зміна)\b.{0,120}",
    ]
    found = _line_with_patterns(text, patterns)
    return found or "not specified"


def extract_contact(text: str) -> str:
    phones = re.findall(
        r"(?:(?:\+?38[\s\-]?)?\(?0[\s\-]?\d{2}\)?[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2})",
        text,
        flags=re.UNICODE,
    )
    usernames = re.findall(r"@\w{3,}", text, flags=re.UNICODE)
    contacts = []
    for value in [*phones, *usernames]:
        cleaned = clean_text(value)
        if cleaned and cleaned not in contacts:
            contacts.append(cleaned)
    return truncate_text(", ".join(contacts), 160) if contacts else "not specified"


def extract_location(text: str) -> str:
    lines = clean_vacancy_text(text).splitlines()
    address_pattern = re.compile(
        r"(?:\bулица\b|\bул\.|\bвулиця\b|\bвул\.|\bадрес\b|\bадреса\b|\bпереулок\b|\bпровулок\b|\bпроспект\b).{0,160}",
        flags=re.IGNORECASE | re.UNICODE,
    )
    district_pattern = re.compile(
        r"\b(?:центр(?:е)?(?: города)?|Аркади[яи]|Таирова|Таїрова|Черемушки|Фонтан|Молдаванка|Котовского|Котовського)\b",
        flags=re.IGNORECASE | re.UNICODE,
    )
    for line in lines:
        if address_pattern.search(line):
            return truncate_text(line, 180)
    for line in lines:
        match = district_pattern.search(line)
        if match:
            return truncate_text(match.group(0), 80)

    patterns = [
        r"\b(?:Одесса|Одеса|Одеська|Odesa|Odessa)\b.{0,140}",
        r"\b(?:центр|Аркади[яи]|Таирова|Таїрова|Черемушки|Фонтан|Молдаванка|Котовского|Котовського|район|поселок|селище)\b.{0,140}",
        r"\b(?:улица|ул\.|вулиця|вул\.|адрес|адреса|переулок|провулок|проспект)\b.{0,160}",
        r"\b(?:ресторан|кафе|coffee|кав'ярня)\b.{0,120}(?:Одесса|Одеса|Odesa|Odessa|центр|район|улица|вулиця|адрес|адреса|Аркади[яи]).{0,120}",
    ]
    found = _line_with_patterns(text, patterns)
    return found or "not specified"


def extract_age_requirement(text: str) -> str:
    patterns = [
        r"\b(?:от|від)\s*1[678]\b.{0,80}",
        r"\b(?:до)\s*[2-6]\d\b.{0,80}",
        r"\b1[678]\s*\+.{0,80}",
        r"\b(?:от|від)\s*\d{2}\s*(?:до|-|–|—)\s*\d{2}\b.{0,80}",
    ]
    found = _line_with_patterns(text, patterns)
    return found or "not specified"


def extract_experience_requirement(text: str) -> str:
    patterns = [
        r"\b(?:опыт|досвід|experience|стаж)\b.{0,140}",
        r"\b(?:без опыта|без досвіду|no experience)\b.{0,120}",
    ]
    found = _line_with_patterns(text, patterns)
    return found or "not specified"


def extract_gender_requirement(text: str) -> str:
    patterns = [
        r"\b(?:девушка|девушки|девушек|дівчина|дівчата|дівчат|женщина|жінка)\b.{0,120}",
        r"\b(?:парень|парни|мужчина|мужчины|хлопець|хлопці|юноша)\b.{0,120}",
        r"\b(?:только девушки|тільки дівчата|только парни|тільки хлопці)\b.{0,120}",
    ]
    found = _line_with_patterns(text, patterns)
    return found or "not specified"


def annotate_vacancy_fields(vacancy: Vacancy, full_text: str | None = None) -> Vacancy:
    role_text = "\n".join([vacancy.title or "", vacancy.text or ""])
    full_text = full_text or role_text

    if not vacancy.role:
        vacancy.role = detect_role_from_text(role_text)
    if not vacancy.vacancy_type or vacancy.vacancy_type == "other":
        vacancy.vacancy_type = vacancy.role or "other"
    if not vacancy.matched_role_keywords:
        vacancy.matched_role_keywords = matched_role_keywords(role_text)

    vacancy.salary = vacancy.salary or extract_salary(role_text)
    vacancy.schedule = vacancy.schedule or extract_schedule(role_text)
    vacancy.contact = vacancy.contact or extract_contact(role_text)
    if vacancy.contact == "not specified":
        vacancy.contact = extract_contact(full_text)

    vacancy.location = vacancy.location if vacancy.location != "not specified" else extract_location(role_text)
    if vacancy.location == "not specified":
        vacancy.location = extract_location(full_text)

    vacancy.age_requirement = vacancy.age_requirement or extract_age_requirement(role_text)
    vacancy.experience_requirement = vacancy.experience_requirement or extract_experience_requirement(role_text)
    vacancy.gender_requirement = vacancy.gender_requirement or extract_gender_requirement(role_text)
    return vacancy
