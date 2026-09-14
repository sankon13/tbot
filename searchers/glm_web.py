# -*- coding: utf-8 -*-
"""Коннектор веб-поиска GLM (Web Search API bigmodel.cn).

Спецификация: POST https://open.bigmodel.cn/api/paas/v4/web_search
(search_engine=search_pro, search_query <=70 симв., search_domain_filter, count 1-50).
Позже сюда добавятся прямые коннекторы (парсинг выдачи Авито и т.п.) — интерфейс тот же.
"""
import httpx

import config

API_URL = "https://open.bigmodel.cn/api/paas/v4/web_search"

# Ограничение выдачи доменом для конкретных источников
DOMAIN_FILTER = {
    "avito": "avito.ru",
    "yandex_uslugi": "uslugi.yandex.ru",
}

# Уточнение запроса для поиска по всему интернету
WEB_HINT = {"услуги": "услуги исполнители заказать", "материалы": "поставщики купить цена"}


def build_query(brief: dict, source: str) -> str:
    """Бриф -> поисковый запрос (<=70 символов, как рекомендует API)."""
    parts = [brief.get("what") or "", brief.get("city") or ""]
    if source == "web":
        parts.append(WEB_HINT.get(brief.get("type"), ""))
    q = " ".join(p for p in parts if p).strip()
    return q[:70]


def search_web(brief: dict, source: str, count: int = 15) -> list:
    """Ищет через GLM Web Search и возвращает list[Supplier]."""
    from searchers.base import Supplier

    payload = {
        "search_engine": "search_pro",
        "search_query": build_query(brief, source),
        "search_intent": False,
        "count": count,
        "content_size": "high",
        "search_recency_filter": "noLimit",
    }
    domain = DOMAIN_FILTER.get(source)
    if domain:
        payload["search_domain_filter"] = domain

    r = httpx.post(
        API_URL,
        headers={"Authorization": f"Bearer {config.GLM_API_KEY}", "Content-Type": "application/json"},
        json=payload,
        timeout=60,
    )
    r.raise_for_status()
    out = []
    for item in r.json().get("search_result", []):
        url = item.get("link") or ""
        if not url.startswith("http"):
            continue
        out.append(Supplier(
            name=(item.get("title") or "").strip(),
            url=url,
            description=(item.get("content") or "").strip(),
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
