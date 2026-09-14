# -*- coding: utf-8 -*-
"""Стандарт коннекторов: коннектор принимает бриф, возвращает список Supplier."""
from dataclasses import dataclass


@dataclass
class Supplier:
    name: str          # заголовок результата как есть (чистит LLM-ранжирование)
    url: str
    description: str   # краткое описание из выдачи
    source: str        # ключ из SOURCES


# Доступные источники поиска. Новый источник = новый модуль-коннектор + строка здесь.
SOURCES: dict[str, str] = {
    "avito": "Авито",
    "yandex_uslugi": "Яндекс Услуги",
    "web": "Весь интернет",
}
