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
            "помощник в зал",
            "помічник у зал",
        ),
    ),
    RoleGroup("hostess", "Hostess", ("hostess", "хостес")),
)

OTHER_ROLE_TERMS: tuple[str, ...] = (
    "bartender",
    "barman",
    "бармен",
    "барменка",
    "бармены",
    "бармени",
    "барменов",
    "barista",
    "бариста",
    "баристы",
    "баристи",
    "cook",
    "chef",
    "kitchen helper",
    "kitchen assistant",
    "dishwasher",
    "cleaner",
    "maid",
    "housekeeper",
    "заготовщица",
    "заготовщик",
    "повар",
    "повара",
    "кухар",
    "кухарі",
    "помощник кухни",
    "помічник кухаря",
    "помощник повара",
    "помічник кухаря",
    "помощник повара",
    "кухонный работник",
    "кухонний працівник",
    "порто",
    "посудомой",
    "посудомойщик",
    "посудомий",
    "посудомийник",
    "мойщ",
    "мойщица",
    "прибираль",
    "уборщ",
    "уборщица",
    "покоївка",
    "администратор",
    "адміністратор",
    "administrator",
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
    "грузчик",
    "вантажник",
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
    "service manager restaurant",
    "service manager cafe",
)

EXPERIENCE_NOT_REQUIRED_PATTERNS: tuple[str, ...] = (
    r"\bбез\s+опыта\b",
    r"\bбез\s+досвiду\b",
    r"\bбез\s+досвіду\b",
    r"\bможно\s+без\s+опыта\b",
    r"\bможна\s+без\s+досвiду\b",
    r"\bможна\s+без\s+досвіду\b",
    r"\bможливо\s+без\s+досвiду\b",
    r"\bможливо\s+без\s+досвіду\b",
    r"\bопыт\s+не\s+обязател(?:ен|ьный|ьна|ьно)\b",
    r"\bопыт\s+работы\s+не\s+обязател(?:ен|ьный|ьна|ьно)\b",
    r"\bопыт\s+необязател(?:ен|ьный|ьна|ьно)\b",
    r"\bдосвiд\s+не\s+обов[ʼ'`’]?язков(?:ий|о|а)\b",
    r"\bдосвід\s+не\s+обов[ʼ'`’]?язков(?:ий|о|а)\b",
    r"\bдосвiд\s+роботи\s+не\s+обов[ʼ'`’]?язков(?:ий|о|а)\b",
    r"\bдосвід\s+роботи\s+не\s+обов[ʼ'`’]?язков(?:ий|о|а)\b",
    r"\bдосвiд\s+необов[ʼ'`’]?язков(?:ий|о|а)\b",
    r"\bдосвід\s+необов[ʼ'`’]?язков(?:ий|о|а)\b",
    r"\bno\s+experience\b",
    r"\bexperience\s+(?:is\s+)?not\s+required\b",
)

EXPERIENCE_SOFT_PATTERNS: tuple[str, ...] = (
    r"\bдосвiд\s+бажан(?:ий|о|а)\b",
    r"\bдосвід\s+бажан(?:ий|о|а)\b",
    r"\bбажано\s+з\s+досвiдом\b",
    r"\bбажано\s+з\s+досвідом\b",
    r"\bопыт\s+будет\s+плюсом\b",
    r"\bжелательно\s+с\s+опытом\b",
    r"\bбуде\s+перевагою\b",
    r"\bбудет\s+преимуществом\b",
    r"\bexperience\s+(?:is\s+)?(?:a\s+)?plus\b",
)

EXPERIENCE_REQUIRED_PATTERNS: tuple[str, ...] = (
    r"\bопыт\s+работы\s+от\b",
    r"\bдосвiд\s+роботи\s+вiд\b",
    r"\bдосвід\s+роботи\s+від\b",
    r"\b(?:опыт|досвiд|досвід)\s+от\b",
    r"\b(?:досвiд|досвід)\s+вiд\b",
    r"\b(?:досвiд|досвід)\s+від\b",
    r"\b(?:от|від|вiд)\s*1\s*(?:года|год|лет|року|рік|рокiв|років)\b",
    r"\b(?:от|від|вiд)\s*\d+\s*(?:года|год|лет|року|рік|рокiв|років)\b",
    r"\b(?:от|від|вiд)\s*(?:года|року)\b",
    r"\bобязательный\s+опыт\b",
    r"\bопыт\s+обязател(?:ен|ьный|ьна|ьно)\b",
    r"\bопыт\s+работы\s+обязател(?:ен|ьный|ьна|ьно)\b",
    r"\bобов[ʼ'`’]?язков(?:ий|о|а)\s+досвiд\b",
    r"\bобов[ʼ'`’]?язков(?:ий|о|а)\s+досвід\b",
    r"\bдосвiд\s+обов[ʼ'`’]?язков(?:ий|о|а)\b",
    r"\bдосвід\s+обов[ʼ'`’]?язков(?:ий|о|а)\b",
    r"\bдосвiд\s+роботи\s+обов[ʼ'`’]?язков(?:ий|о|а)\b",
    r"\bдосвід\s+роботи\s+обов[ʼ'`’]?язков(?:ий|о|а)\b",
    r"\bс\s+опытом\b",
    r"\bз\s+досвiдом\b",
    r"\bз\s+досвідом\b",
)

AGE_17_ALLOWED_PATTERNS: tuple[str, ...] = (
    r"\b17\s*\+",
    r"\b(?:с|з|от|від|вiд)\s*17\s*(?:лет|років|рокiв|року)?\b",
    r"\b(?:можно|можна)\s+(?:с|з)\s*17\b",
)

AGE_18_REJECT_PATTERNS: tuple[str, ...] = (
    r"\b18\s*\+",
    r"\b(?:строго|тільки|тiльки|только|лише)?\s*(?:с|з|от|від|вiд)\s*18\s*(?:лет|років|рокiв|року)?\b",
)

FEMALE_ONLY_PATTERNS: tuple[str, ...] = (
    r"\b(?:только|тільки|тiльки|лише)\s+(?:девушк[аиу]|дівчин[аиу]|дiвчин[аиу]|дівчат[а]?|дiвчат[а]?|женщин[ау]?|жінк[ау]?|жiнк[ау]?)\b",
    r"\b(?:нужн[аы]?|требует(?:ся|ься)|потрібн[аiі]?|потрiбн[аiі]?|потрібні|потрiбнi|шукаємо|ищем)\s+(?:девушк[ауи]?|дівчин[ауи]?|дiвчин[ауи]?|дівчат|дiвчат|женщин[ау]?|жінк[ау]?|жiнк[ау]?)\b",
    r"\b(?:девушк[аиу]|дівчин[аиу]|дiвчин[аиу]|женщин[ау]?|жінк[ау]?|жiнк[ау]?)\s+(?:только|тільки|тiльки|лише)\b",
    r"\bженского\s+пола\b",
    r"\bжіночої\s+статі\b",
    r"\bжiночої\s+статi\b",
    r"\bженщина\b",
    r"\bжінка\b",
    r"\bжiнка\b",
    r"\bдевушка\b",
    r"\bдівчина\b",
    r"\bдiвчина\b",
)

MALE_OR_OPEN_GENDER_TERMS: tuple[str, ...] = (
    "парень",
    "парни",
    "хлопець",
    "хлопці",
    "хлопцi",
    "мужчина",
    "мужчины",
    "чоловік",
    "чоловiк",
    "чоловіки",
    "чоловiки",
    "мужской пол",
    "чоловіча стать",
    "чоловiча стать",
    "официант/официантка",
    "офіціант/офіціантка",
    "офiцiант/офiцiантка",
)


def normalize_match_text(value: str) -> str:
    return (
        value.casefold()
        .replace("ё", "е")
        .replace("є", "е")
        .replace("і", "i")
        .replace("ї", "i")
        .replace("’", "'")
        .replace("ʼ", "'")
        .replace("`", "'")
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


def vacancy_filter_text(vacancy: Vacancy) -> str:
    return str(vacancy.metadata.get("filter_text") or vacancy.text or "")


def has_target_role(text: str) -> bool:
    return bool(_matched_role_groups(text))


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


def _line_has_other_role_for_group(line: str, group: RoleGroup) -> bool:
    role_matches = _matched_role_groups(line)
    if any(matched_group.label != group.label for matched_group, _ in role_matches):
        return True
    return any(_contains_phrase(line, term) for term in OTHER_ROLE_TERMS)


def _candidate_role_segment_lines(lines: list[str], group: RoleGroup) -> list[str]:
    candidate_lines: list[str] = []
    for line in lines:
        if _line_has_other_role_for_group(line, group):
            if any(_contains_phrase(line, term) for term in group.terms):
                candidate_lines.append(group.display)
            continue
        candidate_lines.append(line)
    return candidate_lines


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
    non_empty_lines = [line for line in lines if line.strip()]
    if len(non_empty_lines) < 2 and not (
        non_empty_lines
        and _matched_role_groups(non_empty_lines[0])
        and any(_contains_phrase(non_empty_lines[0], term) for term in OTHER_ROLE_TERMS)
    ):
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
    has_mixed_roles = (
        len(markers) > 1
        or len(target_labels) > 1
        or any(
            matches and any(_line_has_other_role_for_group(lines[index], group) for group, _ in matches)
            for index, matches in target_markers
        )
    )
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
            candidate_lines.extend(_candidate_role_segment_lines(role_segment, group))
            for line in global_trailing:
                if line not in candidate_lines:
                    candidate_lines.append(line)
            candidate_text = _clean_join(candidate_lines) or vacancy.text
            source_key = str(vacancy.metadata.get("source_key") or vacancy.content_hash_exact or vacancy.content_hash)
            dedupe_key = "|".join([source_key, "role", group.label])
            exact_hash = sha256_text(
                dedupe_key
            )
            normalized_text = normalize_text_for_hashing(
                "\n".join([group.display, candidate_text, vacancy.link or ""])
            )
            candidate = Vacancy(
                source=vacancy.source,
                source_type=vacancy.source_type,
                title=group.display,
                text=vacancy.text,
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
                    "source_key": dedupe_key,
                    "dedupe_key": dedupe_key,
                    "filter_text": candidate_text,
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


def _first_pattern_match(text: str, patterns: tuple[str, ...] | list[str], limit: int = 180) -> str:
    normalized = normalize_match_text(text)
    for pattern in patterns:
        match = re.search(pattern, normalized, flags=re.IGNORECASE | re.UNICODE)
        if match:
            return truncate_text(text[match.start() : match.end()].strip(), limit)
    return ""


def _line_has_schedule_time(line: str) -> bool:
    normalized = normalize_match_text(line)
    return bool(
        re.search(r"\b\d{1,2}\s*[:.]\s*\d{2}\b", normalized)
        or re.search(r"\b(?:с|з)\s*\d{1,2}\s*(?:до|-|–|—)\s*\d{1,2}\b", normalized)
        or re.search(r"\bграфик|графiк|графік|schedule|смен", normalized)
    )


def _experience_line_status(line: str) -> str:
    normalized = normalize_match_text(line)
    has_negation = any(re.search(pattern, normalized, flags=re.IGNORECASE | re.UNICODE) for pattern in EXPERIENCE_NOT_REQUIRED_PATTERNS)
    has_soft_requirement = any(re.search(pattern, normalized, flags=re.IGNORECASE | re.UNICODE) for pattern in EXPERIENCE_SOFT_PATTERNS)
    has_required = any(re.search(pattern, normalized, flags=re.IGNORECASE | re.UNICODE) for pattern in EXPERIENCE_REQUIRED_PATTERNS)
    if has_required and not has_negation:
        if has_soft_requirement:
            return "soft"
        return "required"
    if has_negation:
        return "not_required"
    if has_soft_requirement:
        return "soft"
    return "unknown"


def experience_requirement_status(text: str) -> str:
    saw_not_required = False
    for line in clean_vacancy_text(text).splitlines() or [text]:
        status = _experience_line_status(line)
        if status == "required":
            return "required"
        if status == "not_required":
            saw_not_required = True
    return "not_required" if saw_not_required else "unknown"


def has_required_experience(text: str) -> bool:
    return experience_requirement_status(text) == "required"


def age_allows_17(text: str) -> bool:
    for line in clean_vacancy_text(text).splitlines() or [text]:
        if _line_has_schedule_time(line):
            continue
        normalized = normalize_match_text(line)
        if any(re.search(pattern, normalized, flags=re.IGNORECASE | re.UNICODE) for pattern in AGE_17_ALLOWED_PATTERNS):
            return True
    return False


def has_age_18_restriction(text: str) -> bool:
    if age_allows_17(text):
        return False
    for line in clean_vacancy_text(text).splitlines() or [text]:
        if _line_has_schedule_time(line):
            continue
        normalized = normalize_match_text(line)
        if any(re.search(pattern, normalized, flags=re.IGNORECASE | re.UNICODE) for pattern in AGE_18_REJECT_PATTERNS):
            return True
    return False


def has_female_only_requirement(text: str) -> bool:
    for line in clean_vacancy_text(text).splitlines() or [text]:
        normalized = normalize_match_text(line)
        if any(normalize_match_text(term) in normalized for term in MALE_OR_OPEN_GENDER_TERMS):
            continue
        if any(re.search(pattern, normalized, flags=re.IGNORECASE | re.UNICODE) for pattern in FEMALE_ONLY_PATTERNS):
            return True
    return False


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
    patterns = (
        r"\b1[678]\s*\+",
        r"\b(?:от|від|вiд)\s*1[678]\s*(?:лет|років|рокiв|року)?\b",
        r"\b(?:до)\s*[2-6]\d\s*(?:лет|років|рокiв|року)\b",
        r"\b(?:от|від|вiд)\s*\d{2}\s*(?:до|-|–|—)\s*\d{2}\s*(?:лет|років|рокiв|року)?\b",
        r"\b(?:возраст|вiк|вік)\b.{0,80}",
    )
    for line in clean_vacancy_text(text).splitlines() or [text]:
        if _line_has_schedule_time(line):
            continue
        found = _first_pattern_match(line, patterns)
        if found:
            return found
    return "not specified"


def extract_experience_requirement(text: str) -> str:
    lines = clean_vacancy_text(text).splitlines() or [text]
    for line in lines:
        if _experience_line_status(line) == "required":
            return truncate_text(line, 180)
    for line in lines:
        if _experience_line_status(line) == "not_required":
            return truncate_text(line, 180)
    return "not specified"


def extract_gender_requirement(text: str) -> str:
    patterns = (
        r"\b(?:только|лише|тільки)\s+(?:девушк[аи]|дівчин[аи]|дівчата|парн[иия]|хлопц[іiя]|хлопець|мужчин[аы]|жінк[аи])\b",
        r"\b(?:нужн[аы]?|требует(?:ся|ься)|потрібн[аiі]?|шукаємо|ищем)\s+(?:девушк[ауи]?|дівчин[ауи]?|дівчат|парн[яеи]?|хлопц[яіi]?|хлопець|мужчин[ау]?|женщин[ау]?|жінк[ау]?)\b",
        r"\((?:девушк[аи]|дівчин[аи]|дівчата|парн[иия]|хлопц[іiя]|хлопець|мужчин[аы]|жінк[аи])\)",
    )
    for line in clean_vacancy_text(text).splitlines() or [text]:
        found = _first_pattern_match(line, patterns, limit=80)
        if found:
            return found.strip("() ")
    return "not specified"


def annotate_vacancy_fields(vacancy: Vacancy, full_text: str | None = None) -> Vacancy:
    role_text = "\n".join([vacancy.title or "", vacancy_filter_text(vacancy)])
    full_text = full_text or vacancy.text or role_text

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
