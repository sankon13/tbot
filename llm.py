# -*- coding: utf-8 -*-
"""Запросы к GLM: извлечение структурированных данных из сообщений."""
import json
import re

import httpx
from openai import OpenAI

import config

client = OpenAI(api_key=config.GLM_API_KEY, base_url="https://open.bigmodel.cn/api/paas/v4")

EXTRACT_SYSTEM = """Ты — парсер сообщений для бота учёта бизнеса. Пользователи: Александр (синонимы: Саня, Сашка, Александр) и Сергей (синонимы: Серёга, Серый, Сергей).
Верни ТОЛЬКО валидный JSON без markdown-обёртки.

Возможные значения "action":
- "expense" — расход/затрата/покупка ("потратил", "купил", "отдал", "расход", "затрата"...)
- "task" — задача ("задача", "нужно сделать", "напомни", "заказать", "позвонить"...)
- "answer" — это ответ пользователя на последний вопрос бота (см. контекст)
- "task_done" — сообщение о выполнении задачи (например "задача 5 сделана")
- "report" — запрос отчёта/сводки
- "edit" — правка уже существующей записи ("измени строку", "поправь расход", "исправь", "обнови")
- "none" — не относится к учёту

Схема для edit: {"action":"edit","target":"expense|task","find":{"object":"...|null","description_hint":"...|null","date":"...|null","amount":число|null},"changes":{"amount":число|null,"description":"...|null","category":"...|null","object":"...|null","assignee":"...|null","deadline":"YYYY-MM-DD|null"}}
В "find" — ВСЕ признаки, по которым пользователь ищет строку (адрес/объект, товар/описание, дата, прежняя сумма: "доски на 10000 для Парковой"). В "changes" — ТОЛЬКО новые значения полей, которые пользователь хочет изменить, остальные null.

Схема для expense: {"action":"expense","amount":число_руб,"object":"строка или null","description":"строка"}
Схема для task: {"action":"task","assignee":"Александр|Сергей|null","description":"строка","deadline":"YYYY-MM-DD или null"}
Схема для answer: {"action":"answer","value":"текст ответа"}
Схема для task_done: {"action":"task_done","task_number":число}
Схема для report/none: {"action":"..."}

Правила:
- Сумму приводи к рублям ("3 тысячи" = 3000, "5к" = 5000). Текущая дата: сентябрь 2026 — если год не указан, ставь 2026.
- "object" — это название объекта/проекта/адрес (например "Садовая", "склад", "офис"), а НЕ купленный товар. Если объект не упомянут — null.
- Исполнителя нормализуй к "Александр" или "Сергей"; если не указан или это не партнёр — null.
- Если в сообщении и задача, и расход — выбери расход и добавь "also_task": {...схема task...}.
- Если контекст диалога есть, приоритетно проверь, не является ли сообщение ответом на вопрос."""


def _parse_json(text: str) -> dict:
    text = text.strip()
    m = re.search(r"\{.*\}", text, re.S)
    if m:
        text = m.group(0)
    return json.loads(text)


def extract(user_text: str, dialog_context: str | None = None) -> dict:
    """Возвращает распарсенный JSON с action и полями."""
    content = f"Контекст диалога: {dialog_context or 'нет'}\nСообщение пользователя: {user_text}"
    resp = client.chat.completions.create(
        model=config.GLM_MODEL,
        messages=[
            {"role": "system", "content": EXTRACT_SYSTEM},
            {"role": "user", "content": content},
        ],
        temperature=0,
    )
    try:
        return _parse_json(resp.choices[0].message.content)
    except Exception:
        return {"action": "none"}


def match_object(user_text: str, known_objects: list[str]) -> str | None:
    """Подбирает известный объект по названию/синониму. None — совпадения нет."""
    if not known_objects:
        return None
    low = user_text.strip().lower()
    for obj in known_objects:
        if obj.lower() in low or low in obj.lower():
            return obj
    resp = client.chat.completions.create(
        model=config.GLM_MODEL,
        messages=[{
            "role": "user",
            "content": (
                f"Имеется список объектов: {json.dumps(known_objects, ensure_ascii=False)}. "
                f'Пользователь назвал объект: "{user_text}". '
                "Если это один из списка (в т.ч. с опечаткой или частично) — верни точное название из списка. "
                "Иначе верни слово NONE."
            ),
        }],
        temperature=0,
    )
    ans = resp.choices[0].message.content.strip()
    return None if ans.upper().startswith("NONE") else ans


def guess_category(description: str, object_name: str | None,
                   similar_rows: list[list]) -> str:
    """Подбирает категорию по описанию и похожим прошлым расходам (rows: дата|день|автор|описание|сумма|объект|категория)."""
    examples = "\n".join(" | ".join(str(c) for c in r[:7]) for r in similar_rows[-20:])
    known = sorted({str(r[6]) for r in similar_rows if len(r) > 6 and r[6] and str(r[6]) != "Категория"})
    resp = client.chat.completions.create(
        model=config.GLM_MODEL,
        messages=[{
            "role": "user",
            "content": (
                f"Описание расхода: {description}\nОбъект: {object_name}\n\n"
                f"Похожие прошлые расходы (дата|день|автор|описание|сумма|объект|категория):\n{examples or 'нет'}\n\n"
                f"Существующие категории: {known or 'нет'}\n"
                "Верни одну категорию: существующую из списка (если подходит) или новую, 1-2 слова. "
                "Только категорию, без пояснений."
            ),
        }],
        temperature=0,
    )
    return resp.choices[0].message.content.strip().strip('"').strip(".")


def transcribe(audio: bytes, filename: str = "voice.ogg") -> str:
    """Расшифровка голосового (GLM-ASR). Возвращает текст."""
    r = httpx.post(
        "https://open.bigmodel.cn/api/paas/v4/audio/transcriptions",
        headers={"Authorization": f"Bearer {config.GLM_API_KEY}"},
        files={"file": (filename, audio, "audio/ogg")},
        data={"model": "glm-asr-2512"},
        timeout=120,
    )
    r.raise_for_status()
    return (r.json().get("text") or "").strip()


def match_person(text: str) -> str | None:
    """Нормализует имя («Саня» -> Александр). None — не понял."""
    low = text.lower()
    if any(s in low for s in ("александр", "саня", "сашк", "саш", "мне", "мне,")):
        return "Александр"
    if any(s in low for s in ("сергей", "серёг", "серег", "серы", "серый")):
        return "Сергей"
    return None


def pick_expense_row(find: dict, rows: list[dict]) -> dict:
    """Выбирает строку расхода для правки. rows: [{"row": N_excel, "v": [...]}].
    Возвращает {"type":"one","row":N} | {"type":"many","rows":[N..]} | {"type":"none"}."""
    if not rows:
        return {"type": "none"}
    listing = "\n".join(
        f"#{r['row']} | {r['v'][0]} {r['v'][1]} | {r['v'][2]} | {r['v'][3]} | {r['v'][4]} руб | {r['v'][5]} | {r['v'][6] if len(r['v'])>6 else ''}"
        for r in rows)
    resp = client.chat.completions.create(
        model=config.GLM_MODEL,
        messages=[{
            "role": "user",
            "content": (
                f'Критерий поиска: {json.dumps(find, ensure_ascii=False)}\n\n'
                f"Строки расходов (№строки | дата день | автор | описание | сумма | объект | категория):\n{listing}\n\n"
                "Подходящая строка должна одновременно удовлетворять КАЖДОМУ непустому критерию поиска "
                "(объект И описание И дата И сумма — конъюнкция всех названных признаков). "
                "Верни ТОЛЬКО JSON: если подходит ровно одна строка — "
                '{"type":"one","row":<№>}; если несколько — {"type":"many","rows":[<№>,...]} '
                '(не более 6, ближайшие сверху); если ни одной — {"type":"none"}.'
            ),
        }],
        temperature=0,
    )
    try:
        return _parse_json(resp.choices[0].message.content)
    except Exception:
        return {"type": "none"}


def summarize(text: str) -> str:
    """Краткая сводка произвольного текста (для отчётов)."""
    resp = client.chat.completions.create(
        model=config.GLM_MODEL,
        messages=[{"role": "user", "content": text}],
        temperature=0.3,
    )
    return resp.choices[0].message.content.strip()
