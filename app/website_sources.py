from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from .utils import (
    SourceResult,
    Vacancy,
    clean_text,
    clean_vacancy_text,
    first_line_title,
    load_yaml_file,
    normalize_vacancy_content,
    normalize_for_hash,
    normalized_content_hash,
    now_iso,
    sha256_text,
)


logger = logging.getLogger(__name__)


def load_sites(path: Path) -> list[dict[str, Any]]:
    data = load_yaml_file(path, default={"sites": []})
    if not isinstance(data, dict):
        logger.warning("sites.yaml must contain a dictionary with key 'sites'")
        return []

    sites = data.get("sites") or []
    if not isinstance(sites, list):
        logger.warning("sites.yaml key 'sites' must be a list")
        return []

    valid_sites: list[dict[str, Any]] = []
    for site in sites:
        if not isinstance(site, dict) or not (site.get("url") or site.get("urls") or site.get("pages")):
            logger.warning("Skipping invalid site config: %s", site)
            continue
        valid_sites.append(site)
    return valid_sites


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _site_urls(site: dict[str, Any]) -> list[str]:
    raw_urls = _as_list(site.get("urls")) or _as_list(site.get("pages")) or _as_list(site.get("url"))
    return [str(url).strip() for url in raw_urls if str(url).strip()]


def _select_text(card: Any, selector: str | list[str] | None) -> str:
    for current_selector in _as_list(selector):
        if not current_selector:
            continue
        current_selector = str(current_selector)
        element = card.select_one(current_selector)
        if not element and getattr(card, "name", "") == "a" and current_selector.startswith("a"):
            element = card
        if element:
            return clean_text(element.get_text(" ", strip=True))
    return ""


def _select_link(card: Any, selector: str | list[str] | None, base_url: str, page_url: str) -> str:
    for current_selector in _as_list(selector):
        if not current_selector:
            continue
        current_selector = str(current_selector)
        element = card.select_one(current_selector)
        if not element and getattr(card, "name", "") == "a" and current_selector.startswith("a"):
            element = card
        if not element:
            continue
        href = element.get("href")
        if href:
            return urljoin(base_url or page_url, href)
    return ""


def _build_headers(
    user_agent: str,
    default_headers: dict[str, Any] | None,
    site_headers: dict[str, Any] | None,
) -> dict[str, str]:
    headers: dict[str, str] = {
        "User-Agent": user_agent,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ru-RU,ru;q=0.9,uk-UA;q=0.8,uk;q=0.7,en;q=0.6",
    }
    for source in (default_headers or {}, site_headers or {}):
        for key, value in source.items():
            headers[str(key)] = str(value)
    return headers


def _zero_cards_reason(response: requests.Response, html: str) -> str:
    status_code = response.status_code
    if status_code in {401, 403, 429}:
        return f"site may be blocking requests (HTTP {status_code})"
    if len(html) < 8000:
        return "short HTML response; possible dynamic page, bot protection, or selector mismatch"
    lowered = html.casefold()
    if "captcha" in lowered or "cloudflare" in lowered:
        return "possible bot protection/captcha"
    if "__next_data__" in lowered or "ng-version" in lowered or "window.__" in lowered:
        return "possible dynamic page rendered by JavaScript"
    return "possible dynamic page or selector mismatch"


def _page_sample(soup: BeautifulSoup, html: str) -> str:
    title = clean_text(soup.title.get_text(" ", strip=True)) if soup.title else ""
    body = clean_text(soup.get_text(" ", strip=True))
    sample = f"{title} {body}".strip() or html[:300]
    return sample[:300]


def _detail_text(soup: BeautifulSoup) -> str:
    selectors = (
        "main",
        "article",
        "div.card",
        "div[class*='job']",
        "div[class*='vacancy']",
        "body",
    )
    for selector in selectors:
        element = soup.select_one(selector)
        if element:
            text = clean_vacancy_text(element.get_text("\n", strip=True))
            if len(text) > 80:
                return text
    return clean_vacancy_text(soup.get_text("\n", strip=True))


def _fetch_detail_text(
    session: requests.Session,
    link: str,
    headers: dict[str, str],
    timeout: int,
    name: str,
) -> tuple[str, dict[str, Any]]:
    detail: dict[str, Any] = {
        "detail_url": link,
        "detail_final_url": "",
        "detail_status_code": None,
        "detail_content_length": 0,
        "detail_error": "",
    }
    try:
        response = session.get(link, headers=headers, timeout=timeout)
        detail["detail_final_url"] = response.url
        detail["detail_status_code"] = response.status_code
        detail["detail_content_length"] = len(response.text)
        logger.info(
            "Website detail response: name=%s url=%s status=%s final_url=%s content_length=%s",
            name,
            link,
            response.status_code,
            response.url,
            len(response.text),
        )
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        if "robota.ua" in link:
            logger.info("Robota.ua detail sample: %s", _page_sample(soup, response.text))
        return _detail_text(soup), detail
    except requests.RequestException as exc:
        detail["detail_error"] = str(exc)
        logger.warning("Website detail request failed: name=%s url=%s error=%s", name, link, exc)
    except Exception:
        detail["detail_error"] = "unexpected detail parser error"
        logger.exception("Unexpected website detail parser error: name=%s url=%s", name, link)
    return "", detail


def fetch_website_vacancies(
    sites: list[dict[str, Any]],
    timeout: int,
    user_agent: str,
    default_headers: dict[str, Any] | None = None,
    detail_pages_limit: int = 20,
    detail_delay_seconds: float = 0.7,
) -> SourceResult:
    result = SourceResult()
    session = requests.Session()
    detail_pages_fetched = 0

    for site in sites:
        name = str(site.get("name") or site.get("url") or "Website")
        base_url = str(site.get("base_url") or site.get("url") or "")
        vacancy_selectors = [str(item) for item in _as_list(site.get("vacancy_selector")) if str(item).strip()]
        headers = _build_headers(user_agent, default_headers, site.get("headers") or {})

        if not vacancy_selectors:
            result.errors += 1
            summary = f"{name}: missing vacancy_selector"
            result.debug_summaries.append(summary)
            logger.warning("Site %s has no vacancy_selector, skipping", name)
            continue

        for url in _site_urls(site):
            page_summary: dict[str, Any] = {
                "name": name,
                "url": url,
                "final_url": "",
                "status_code": None,
                "content_length": 0,
                "cards_found": 0,
                "parsed": 0,
                "matched": 0,
                "possible_dynamic": False,
                "zero_reason": "",
            }
            result.source_summaries.append(page_summary)

            try:
                logger.info("Website request: name=%s url=%s", name, url)
                result.checked += 1
                response = session.get(url, headers=headers, timeout=timeout)
                page_summary["status_code"] = response.status_code
                page_summary["final_url"] = response.url
                page_summary["content_length"] = len(response.text)
                logger.info(
                    "Website response: name=%s url=%s status=%s final_url=%s content_length=%s",
                    name,
                    url,
                    response.status_code,
                    response.url,
                    len(response.text),
                )
                response.raise_for_status()

                soup = BeautifulSoup(response.text, "html.parser")
                if "robota.ua" in url:
                    logger.info("Robota.ua page sample: %s", _page_sample(soup, response.text))
                cards: list[Any] = []
                used_selector = ""
                for vacancy_selector in vacancy_selectors:
                    cards = soup.select(vacancy_selector)
                    if cards:
                        used_selector = vacancy_selector
                        break
                cards_found = len(cards)
                page_summary["cards_found"] = cards_found
                result.cards_found += cards_found
                logger.info(
                    "Website cards found: name=%s url=%s selector=%s cards=%s",
                    name,
                    url,
                    used_selector or ",".join(vacancy_selectors),
                    cards_found,
                )

                if not cards:
                    page_summary["possible_dynamic"] = True
                    zero_reason = _zero_cards_reason(response, response.text)
                    page_summary["zero_reason"] = zero_reason
                    result.debug_summaries.append(
                        f"{name}: 0 cards at {url}. {zero_reason}."
                    )
                    logger.info(
                        "Website zero cards: name=%s url=%s final_url=%s status=%s content_length=%s reason=%s",
                        name,
                        url,
                        response.url,
                        response.status_code,
                        len(response.text),
                        zero_reason,
                    )
                    continue

                parsed_count = 0
                page_detail_count = 0
                page_detail_skipped = 0
                parsed_links: list[str] = []
                for card in cards:
                    title = _select_text(card, site.get("title_selector"))
                    link_selector = site.get("link_selector") or site.get("title_selector")
                    link = _select_link(card, link_selector, base_url, url)
                    description = _select_text(card, site.get("description_selector"))
                    if not description:
                        description = clean_vacancy_text(card.get_text("\n", strip=True))

                    text_parts = [part for part in (title, description) if part]
                    text = clean_vacancy_text("\n".join(text_parts))
                    if not text and not link:
                        continue

                    title = title or first_line_title(text, fallback=name)
                    full_text = text
                    detail_data: dict[str, Any] = {}
                    if link and detail_pages_fetched < detail_pages_limit:
                        if detail_pages_fetched > 0 and detail_delay_seconds > 0:
                            time.sleep(detail_delay_seconds)
                        detail_text, detail_data = _fetch_detail_text(
                            session,
                            link,
                            headers,
                            timeout,
                            name,
                        )
                        detail_pages_fetched += 1
                        page_detail_count += 1
                        if detail_text:
                            full_text = clean_vacancy_text("\n".join([text, detail_text]))
                    elif link:
                        page_detail_skipped += 1

                    hash_base = (
                        f"website|{name}|{link}"
                        if link
                        else f"website|{name}|{normalize_for_hash(title)}|{normalize_for_hash(full_text)}"
                    )
                    exact_hash = sha256_text(hash_base)
                    normalized_text = normalize_vacancy_content("\n".join([title, full_text]))
                    normalized_hash = normalized_content_hash(normalized_text)

                    result.vacancies.append(
                        Vacancy(
                            source=name,
                            source_type=str(site.get("source_type") or "website"),
                            title=title,
                            text=full_text,
                            link=link,
                            published_at=now_iso(),
                            content_hash=exact_hash,
                            content_hash_exact=exact_hash,
                            content_hash_normalized=normalized_hash,
                            content_normalized=normalized_text,
                            metadata={
                                "site_name": name,
                                "page_url": url,
                                **detail_data,
                            },
                        )
                    )
                    if link:
                        parsed_links.append(link)
                    parsed_count += 1

                page_summary["parsed"] = parsed_count
                page_summary["parsed_links"] = parsed_links[:10]
                page_summary["details_fetched"] = page_detail_count
                page_summary["details_skipped_by_limit"] = page_detail_skipped
                page_summary["detail_limit_reached"] = page_detail_skipped > 0
                result.parsed += parsed_count
                logger.info(
                    "Website parsed: name=%s url=%s parsed=%s links=%s",
                    name,
                    url,
                    parsed_count,
                    parsed_links[:10],
                )
                if parsed_count == 0:
                    page_summary["zero_reason"] = "cards found but selectors did not produce vacancy text/link"
                    result.debug_summaries.append(
                        f"{name}: cards found but 0 vacancies parsed at {url}. Check title/link/description selectors."
                    )
            except requests.RequestException as exc:
                result.errors += 1
                if page_summary.get("status_code") in {401, 403, 429}:
                    page_summary["zero_reason"] = f"site may be blocking requests (HTTP {page_summary['status_code']})"
                else:
                    page_summary["zero_reason"] = "HTTP request failed"
                result.debug_summaries.append(f"{name}: request failed for {url}: {exc}")
                logger.warning("Website request failed: name=%s url=%s error=%s", name, url, exc)
            except Exception:
                result.errors += 1
                page_summary["zero_reason"] = "unexpected parser error"
                result.debug_summaries.append(f"{name}: unexpected parser error for {url}")
                logger.exception("Unexpected website source error: name=%s url=%s", name, url)

    logger.info(
        "Website fetch finished: checked=%s cards=%s parsed=%s candidates=%s errors=%s",
        result.checked,
        result.cards_found,
        result.parsed,
        len(result.vacancies),
        result.errors,
    )
    return result
