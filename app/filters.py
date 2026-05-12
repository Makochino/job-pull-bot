from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .extraction import (
    detect_role_from_text,
    has_age_18_restriction,
    has_female_only_requirement,
    has_required_experience,
    matched_role_keywords,
    vacancy_filter_text,
)
from .utils import Vacancy, as_int


DEFAULT_FEMALE_TERMS = (
    "девушка",
    "девушки",
    "девушек",
    "дівчина",
    "дівчата",
    "дівчат",
    "женщина",
    "жінка",
)

DEFAULT_FEMALE_JOB_TERMS = (
    "официант",
    "официантка",
    "офіціант",
    "офіціантка",
    "хостес",
)

DEFAULT_MALE_OR_NEUTRAL_TERMS = (
    "парень",
    "парни",
    "мужчина",
    "мужчины",
    "хлопець",
    "хлопці",
    "юноша",
    "официант/официантка",
    "официант или официантка",
    "офіціант/офіціантка",
)

HIRING_WORDS = (
    "требуется",
    "требуются",
    "нужна",
    "нужны",
    "ищем",
    "потрібна",
    "потрібні",
    "шукаємо",
)

AGE_LIMIT_TERMS = (
    "до 35",
    "до 35 лет",
    "від 18 до 35",
    "от 18 до 35",
)

SUSPICIOUS_MASSAGE_CONTEXT = (
    "девушка",
    "девушки",
    "дівчина",
    "дівчата",
    "интим",
    "інтим",
    "эскорт",
    "escort",
    "18+",
    "высокий доход",
)

SUSPICIOUS_HOUSING_TERMS = (
    "жильё иногородним",
    "жилье иногородним",
    "переезд и жильё",
    "переезд и жилье",
)

SUSPICIOUS_INCOME_TERMS = (
    "высокий доход",
    "доход с первых дней",
    "офис без опыта",
    "работа в офисе",
    "800$",
    "$800",
    "1000$",
    "$1000",
)


@dataclass
class FilterResult:
    accepted: bool
    score: int = 0
    location: str = "not specified"
    vacancy_type: str = "other"
    matched_core_keywords: list[str] = field(default_factory=list)
    matched_context_keywords: list[str] = field(default_factory=list)
    matched_bonus_keywords: list[str] = field(default_factory=list)
    hard_rejected: bool = False
    reject_reason: str = ""


def _normalize(value: str) -> str:
    return (
        value.casefold()
        .replace("ё", "е")
        .replace("є", "е")
        .replace("і", "i")
        .replace("ї", "i")
    )


def _find_matches(text: str, phrases: list[str] | tuple[str, ...]) -> list[str]:
    normalized_text = _normalize(text)
    matches: list[str] = []
    for phrase in phrases:
        normalized_phrase = _normalize(str(phrase).strip())
        if normalized_phrase and normalized_phrase in normalized_text:
            matches.append(str(phrase))
    return matches


def _has_any(text: str, phrases: list[str] | tuple[str, ...]) -> bool:
    return bool(_find_matches(text, phrases))


def _detect_hard_reject(text: str, filters: dict[str, Any]) -> str:
    hard_keywords = [str(item) for item in filters.get("hard_reject_keywords", [])]
    scam_patterns = [str(item) for item in filters.get("scam_reject_patterns", [])]

    matches = _find_matches(text, hard_keywords)
    if matches:
        return f"hard keyword: {matches[0]}"

    matches = _find_matches(text, scam_patterns)
    if matches:
        return f"scam pattern: {matches[0]}"

    if has_age_18_restriction(text):
        return "age restriction 18+"

    if has_female_only_requirement(text):
        return "female-only requirement"

    if _has_any(text, ("массаж", "massage")) and _has_any(text, SUSPICIOUS_MASSAGE_CONTEXT):
        return "suspicious massage/adult wording"

    if _has_any(text, SUSPICIOUS_HOUSING_TERMS) and _has_any(text, SUSPICIOUS_INCOME_TERMS):
        return "suspicious housing/high-income wording"

    return ""


def detect_vacancy_type(matches: list[str]) -> str:
    if not matches:
        return "other"

    return detect_role_from_text("\n".join(matches)) or matches[0]


def score_vacancy(vacancy: Vacancy, config: dict[str, Any]) -> FilterResult:
    filters = config.get("filters", {})
    min_score = as_int(config.get("min_score", filters.get("min_score", 5)), 5)

    context_keywords = [str(item) for item in filters.get("restaurant_context_keywords", [])]
    bonus_keywords = [str(item) for item in filters.get("bonus_keywords", [])]
    locations = [str(item) for item in filters.get("locations", [])]

    text = "\n".join([vacancy.title or "", vacancy_filter_text(vacancy)])
    matched_core = matched_role_keywords(text)
    if not matched_core:
        return FilterResult(accepted=False, reject_reason="no allowed target role keyword")

    reject_reason = _detect_hard_reject(text, filters)
    if reject_reason:
        return FilterResult(
            accepted=False,
            hard_rejected=True,
            reject_reason=reject_reason,
            matched_core_keywords=matched_core,
            vacancy_type=detect_vacancy_type(matched_core),
        )

    if has_required_experience(text):
        return FilterResult(
            accepted=False,
            hard_rejected=True,
            reject_reason="target role requires experience",
            matched_core_keywords=matched_core,
            vacancy_type=detect_vacancy_type(matched_core),
        )

    matched_context = _find_matches(text, context_keywords)
    matched_bonus = _find_matches(text, bonus_keywords)
    matched_locations = _find_matches(text, locations)

    score = 5
    if matched_locations:
        score += 2
    if matched_context:
        score += 1
    if matched_bonus:
        score += min(2, len(set(matched_bonus)))

    score = max(0, min(10, score))
    location = matched_locations[0] if matched_locations else "not specified"

    return FilterResult(
        accepted=score >= min_score,
        score=score,
        location=location,
        vacancy_type=detect_vacancy_type(matched_core),
        matched_core_keywords=matched_core,
        matched_context_keywords=matched_context,
        matched_bonus_keywords=matched_bonus,
    )
