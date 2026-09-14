# -*- coding: utf-8 -*-
"""Коннектор официального Yandex Search API v2 — основной поиск по русскому интернету.

Спецификация (по proto yandex-cloud/cloudapi, service WebSearchService):
POST https://searchapi.api.cloud.yandex.net/v2/web/search, Authorization: Api-Key <key>,
тело: query{queryText, searchType, familyMode}, groupSpec{groupMode, groupsOnPage, docsInGroup},
maxPassages 1-5, response_format FORMAT_XML. Ответ: WebSearchResponse.raw_data — XML.
Ограничение по сайту — оператор языка запросов site:<домен>.

Ключ и folder_id — в config (YANDEX_SEARCH_API_KEY, YANDEX_FOLDER_ID); пробный лимит ~500 запросов/сутки.
"""
import xml.etree.ElementTree as ET

import httpx

import config
from searchers.base import Supplier

API_URL = "https://searchapi.api.cloud.yandex.net/v2/web/search"

SITE_FILTER = {
    "avito": "site:avito.ru",
    "yandex_uslugi": "site:uslugi.yandex.ru",
}


def build_query(brief: dict, source: str) -> str:
    """Бриф -> строка запроса Яндекса с site:-фильтром (если источник ограничен)."""
    site = SITE_FILTER.get(source, "")
    words = [brief.get("what") or "", brief.get("city") or ""]
    q = " ".join(w for w in words if w).strip()
    return f"{site} {q}".strip()


def configured() -> bool:
    return bool(config.YANDEX_SEARCH_API_KEY)


def search_yandex(brief: dict, source: str, count: int = 15) -> list[Supplier]:
    """Ищет через Yandex Search API и возвращает list[Supplier]."""
    body = {
        "query": {
            "searchType": "RU",
            "queryText": build_query(brief, source),
            "familyMode": "MODERATE",
        },
        "groupSpec": {
            "groupMode": "GROUP_MODE_FLAT",  # один документ = одна группа
            "groupsOnPage": count,
            "docsInGroup": 1,
        },
        "maxPassages": 5,
        "response_format": "FORMAT_XML",
    }
    if config.YANDEX_FOLDER_ID:
        body["folder_id"] = config.YANDEX_FOLDER_ID
    r = httpx.post(
        API_URL,
        headers={"Authorization": f"Api-Key {config.YANDEX_SEARCH_API_KEY}"},
        json=body,
        timeout=60,
    )
    r.raise_for_status()
    root = ET.fromstring(r.text)
    out = []
    for doc in root.iter("doc"):
        url = (doc.findtext("url") or "").strip()
        if not url.startswith("http"):
            continue
        title = " ".join("".join(el.itertext()) for el in doc.findall("title")).strip()
        passages = [" ".join("".join(p.itertext()).split()) for p in doc.iter("passage")]
        out.append(Supplier(
            name=title or url,
            url=url,
            description=" ".join(passages)[:400],
            source=source,
        ))
    # дубли одного и того же документа
    seen, uniq = set(), []
    for s in out:
        key = s.url.rstrip("/").lower()
        if key not in seen:
            seen.add(key)
            uniq.append(s)
    return uniq
