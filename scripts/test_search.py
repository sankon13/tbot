# -*- coding: utf-8 -*-
"""Живая проверка LLM-парсера и поискового коннектора (без Telegram и Sheets).

Запуск из корня: python scripts/test_search.py "нужны исполнители по стяжке пола в казани" avito
"""
import json
import sys

import llm
from searchers import glm_web
from searchers.base import SOURCES

text = sys.argv[1] if len(sys.argv) > 1 else "нужны исполнители по стяжке пола в казани"
source = sys.argv[2] if len(sys.argv) > 2 else "avito"

brief = llm.parse_request(text)
print("БРИФ:", json.dumps(brief, ensure_ascii=False, indent=1))

query = glm_web.build_query(brief, source)
print(f"\nЗАПРОС [{SOURCES[source]}]: {query}")
results = glm_web.search_web(brief, source)
print(f"результатов: {len(results)}")
for s in results[:5]:
    print(f" - {s.name[:80]}\n   {s.url}\n   {(s.description or '')[:100]}")

ranked = llm.rank_results(brief, results)
print(f"\nПОСЛЕ РАНЖИРОВАНИЯ: {len(ranked)}")
for it in ranked:
    print(f" {it['i']}. {it['name']} — {it['why']}")
    print(f"    {results[it['i'] - 1].url}")
