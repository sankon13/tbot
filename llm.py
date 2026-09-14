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
- "search" — просьба НАЙТИ поставщиков/исполнителей/материалы ("найди поставщиков", "нужны исполнители по стяжке", "где купить кирпич", "ищу бригаду", "кто продает цемент"). НЕ search, если покупка уже совершена (это expense) или это поручение партнёру (это task)
- "answer" — это ответ пользователя на последний вопрос бота (см. контекст)
- "task_done" — сообщение о выполнении задачи (например "задача 5 сделана")
- "report" — запрос отчёта/сводки
- "edit" — правка уже существующей записи ("измени строку", "поправь расход", "исправь", "обнови")
- "none" — не относится к учёту

Схема для search: {"action":"search","query":"исходный текст запроса как есть"}

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


CHAT_SYSTEM = """Ты — дружелюбный ассистент внутри Telegram-бота «Мегастрой», через который Александр и Сергей ведут учёт бизнеса:
записывают расходы и задачи (те сразу попадают в Google Таблицу), голосом и текстом, а также ищут поставщиков
(исполнителей работ и продавцов материалов) — бот выдаёт список со ссылками, история на веб-странице.
Правила:
- Отвечай по-русски, кратко и по делу (1–4 предложения), живым тоном, можно лёгкий юмор.
- Ты не записал что-то в таблицу — так и скажи, если спросит.
- Если сообщение похоже на расход или задачу, но ты не уверен, что оно уже записано, — подскажи, как записать: просто написать «потратил…» или «задача…».
- Если просят найти поставщиков/исполнителей/материал — подскажи написать запрос поиском, например: «найди бригаду по стяжке пола в Казани».
- Общие вопросы (приветствия, «как дела», советы, пояснения по учёту и поиску) — отвечай сам, понятно и полезно."""


def chat(user_name: str, text: str) -> str:
    """Свободное общение для сообщений, не являющихся расходом/задачей/командой."""
    resp = client.chat.completions.create(
        model=config.GLM_MODEL,
        messages=[
            {"role": "system", "content": CHAT_SYSTEM},
            {"role": "user", "content": f"{user_name} пишет: {text}"},
        ],
        temperature=0.6,
    )
    return resp.choices[0].message.content.strip()


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


# ---------- поиск поставщиков (ZAKUP) ----------

PARSE_SYSTEM = """Ты — парсер запросов для бота поиска поставщиков. Александр и Сергей просят найти поставщиков: исполнителей работ/услуг или продавцов материалов.
Верни ТОЛЬКО валидный JSON без markdown-обёртки:
{"type":"услуги","what":"стяжка пола","city":"Казань","volume":"200 м2","source":null}

Поля:
- "type": "услуги" — нужны исполнители работ/услуг (ремонт, отделка, стяжка, перевозка, электрика...); "материалы" — нужны поставщики товаров (кирпич, цемент, доски...).
- "what" — суть поиска короткой фразой, 2-5 слов ("стяжка пола", "кирпич облицовочный", "бригада отделочников").
- "city" — город/регион или null.
- "volume" — объём/количество/срок из запроса или null.
- "source" — ТОЛЬКО если пользователь прямо назвал площадку: "авито" -> "avito"; "яндекс услуги", "яндекс.услуги" -> "yandex_uslugi"; "в интернете", "по всему интернету", "везде" -> "web". Иначе null.
Если сообщение вообще не запрос на поиск (приветствие, «спасибо», «как дела», вопрос про бота) — верни {"chat": true}."""


def parse_request(text: str) -> dict:
    """Текст поискового запроса -> бриф. При ошибке — безопасный дефолт."""
    try:
        resp = client.chat.completions.create(
            model=config.GLM_MODEL,
            messages=[
                {"role": "system", "content": PARSE_SYSTEM},
                {"role": "user", "content": text},
            ],
            temperature=0,
        )
        data = _parse_json(resp.choices[0].message.content)
    except Exception:
        data = {}
    return {
        "chat": bool(data.get("chat")),
        "type": data.get("type") if data.get("type") in ("услуги", "материалы") else "услуги",
        "what": (data.get("what") or text)[:100],
        "city": data.get("city") or None,
        "volume": data.get("volume") or None,
        "source": data.get("source") if data.get("source") in ("avito", "yandex_uslugi", "web") else None,
    }


RANK_SYSTEM = """Ты — редактор выдачи для бота поиска поставщиков. Пользователь ищет поставщиков по запросу, тебе дают результаты веб-поиска.
Отбери подходящие результаты и верни ТОЛЬКО валидный JSON без markdown-обёртки:
{"items":[{"i":3,"name":"ООО «СтяжкаПро»","why":"бригада по стяжке пола в Казани, есть отзывы"}]}

Правила:
- Отбрасывай: новости, статьи, отзывы, форумы, несоответствующие запросу страницы, дубли одной компании.
- Оставляй максимум 10 лучших, лучшие вперёд.
- "i" — номер результата в списке.
- "name" — чистое название компании или исполнителя из заголовка: без цены, без «| Avito», без «Объявления на тему...».
- "why" — одна короткая фраза (до 12 слов), что это за поставщик и почему подходит."""


def rank_results(brief: dict, suppliers: list) -> list[dict]:
    """Фильтрует и ранжирует результаты поиска. Возвращает [{"i": N, "name": ..., "why": ...}]."""
    if not suppliers:
        return []
    listing = "\n".join(
        f"{n}. {s.name} | {(s.description or '')[:150]} | {s.url}"
        for n, s in enumerate(suppliers, 1)
    )
    ask = (f"Запрос: тип={brief.get('type')}, что={brief.get('what')}, "
           f"город={brief.get('city') or 'не указан'}, объём={brief.get('volume') or 'не указан'}.\n\n"
           f"Результаты поиска:\n{listing}")
    try:
        resp = client.chat.completions.create(
            model=config.GLM_MODEL,
            messages=[
                {"role": "system", "content": RANK_SYSTEM},
                {"role": "user", "content": ask},
            ],
            temperature=0.2,
        )
        items = _parse_json(resp.choices[0].message.content).get("items", [])
    except Exception:
        return []
    valid = []
    for it in items:
        try:
            i = int(it.get("i"))
            if 1 <= i <= len(suppliers):
                valid.append({"i": i, "name": (it.get("name") or suppliers[i - 1].name)[:200],
                              "why": (it.get("why") or "")[:300]})
        except (TypeError, ValueError):
            continue
    return valid[:10]
