# -*- coding: utf-8 -*-
"""TBot — Telegram-бот учёта расходов и задач с извлечением данных через GLM."""
import asyncio
import logging
import os
from io import BytesIO

from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery
from aiogram.types.inline_keyboard_markup import InlineKeyboardMarkup
from aiogram.types.keyboard_button import KeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder

import config
import llm
from searchers import glm_web, yandex
from searchers.base import SOURCES
from sheets import Sheets
from web import dashboard
from zakup_sheets import ZakupSheets

logging.basicConfig(level=logging.INFO)

# Контекст диалога: user_id -> {"expect": "object|amount|assignee|...", "draft": {...}}
dialogs: dict[int, dict] = {}


def kb(buttons: list[tuple[str, str]], cols: int = 1) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for text, data in buttons:
        b.button(text=text, callback_data=data)
    b.adjust(cols)
    return b.as_markup()


class TBot:
    def __init__(self):
        self.bot = Bot(token=config.TELEGRAM_BOT_TOKEN)
        self.dp = Dispatcher()
        self.sheets = Sheets()
        self.zs = ZakupSheets()
        self.zs.ensure_structure()
        self.users = dict(config.USERS)
        self.users.update(self.sheets.load_users())
        self.users.update(self.zs.load_users())  # доступ общий: учёт + поиск
        self.pending: dict[int, dict] = {}   # ждущие решения заявки
        self.denied: set[int] = set()        # отклонённые (до перезапуска бота)
        self._register()

    def _register(self):
        self.dp.message(Command("start"))(self.cmd_start)
        self.dp.message(Command("tasks"))(self.cmd_tasks)
        self.dp.message(Command("zaprosy", "requests", "запросы"))(self.cmd_zaprosy)
        self.dp.callback_query(F.data.startswith("obj:"))(self.on_object_pick)
        self.dp.callback_query(F.data.startswith("edit:"))(self.on_edit_pick)
        self.dp.callback_query(F.data.startswith("voice:"))(self.on_voice_confirm)
        self.dp.callback_query(F.data.startswith("acc:"))(self.on_access_decision)
        self.dp.callback_query(F.data.startswith("go:"))(self.on_go)
        self.dp.message(F.voice)(self.on_voice)
        self.dp.message()(self.on_message)

    async def handle_update(self, request):
        from aiogram.types import Update
        if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != config.WEBHOOK_SECRET:
            return web.Response(status=403)
        update = Update.model_validate(await request.json(), context={"bot": self.bot})
        await self.dp.feed_update(self.bot, update)
        return web.Response(text="ok")

    async def run(self):
        app = web.Application()
        dashboard.add_routes(app, self.zs)
        app.router.add_get("/health", lambda r: web.Response(text="TBot is alive"))

        async def notify(request):
            # ops-канал: предупреждения в Telegram админам через сервер бота
            # (когда api.telegram.org недоступен с ПК пользователя — например, выключен VPN)
            if request.query.get("pass") != config.DASHBOARD_PASSWORD:
                return web.Response(status=403)
            text = request.query.get("text", "").strip()[:4000]
            if not text:
                return web.Response(text="no text")
            sent = 0
            for aid in config.ADMIN_IDS:
                try:
                    await self.bot.send_message(aid, f"⚠️ {text}")
                    sent += 1
                except Exception:
                    logging.exception("notify -> %s failed", aid)
            return web.Response(text=f"sent:{sent}")

        app.router.add_get("/notify", notify)
        if config.WEBHOOK_URL:
            app.router.add_post(config.WEBHOOK_PATH, self.handle_update)
            await self.bot.set_webhook(
                config.WEBHOOK_URL.rstrip("/") + config.WEBHOOK_PATH,
                secret_token=config.WEBHOOK_SECRET,
                allowed_updates=["message", "callback_query"],
                drop_pending_updates=False,
            )
            logging.info("webhook set: %s", config.WEBHOOK_URL)
        runner = web.AppRunner(app)
        await runner.setup()
        port = int(os.environ.get("PORT", 8080))
        await web.TCPSite(runner, "0.0.0.0", port).start()
        if not config.WEBHOOK_URL:
            await self.bot.delete_webhook(drop_pending_updates=True)
            await self.dp.start_polling(self.bot)
        else:
            await asyncio.Event().wait()  # работаем как веб-сервер

    # ---------- команды ----------
    async def cmd_start(self, m: Message):
        await m.answer(
            f"Привет, {config.USERS.get(m.from_user.id, m.from_user.first_name)}! "
            "Я умею три вещи:\n"
            "1. УЧЁТ — пиши расход («потратил 5000 на материалы для Садовой») или задачу "
            "(«Серёге позвонить поставщику до пятницы»), можно голосом. /tasks — открытые задачи.\n"
            "2. ПОИСК ПОСТАВЩИКОВ — напиши, кого найти: «найди исполнителей по стяжке пола в Казани», "
            "«где купить облицовочный кирпич». Предложу где искать (Авито, Яндекс Услуги, весь интернет) "
            "и пришлю список со ссылками. /zaprosy — последние запросы и статусы, "
            "веб-страница со всеми запросами — по паролю.\n"
            "Чего не хватит — спрошу.")

    async def cmd_tasks(self, m: Message):
        tasks = self.sheets.open_tasks()
        if not tasks:
            await m.answer("Открытых задач нет.")
            return
        lines = [f"№{r[0]} · {r[4]} · {r[5]}" + (f" · до {r[6]}" if r[6] else "") + f" · {r[7]}" for r in tasks]
        await m.answer("Открытые задачи:\n" + "\n".join(lines))

    async def cmd_zaprosy(self, m: Message):
        rows = [r for r in self.zs.all_requests() if r and r[0].strip()][-10:]
        if not rows:
            await m.answer("Поисковых запросов пока нет. Напиши, кого найти — например "
                           "«найди бригаду по стяжке пола в Казани».")
            return
        lines = [f"№{r[0]} · {r[1]} · {r[8] if len(r) > 8 else '?'} · {r[3][:60]}"
                 f" ({r[7] if len(r) > 7 else '?'}{', ' + r[9] + ' шт' if len(r) > 9 and r[9] not in ('', '0') else ''})"
                 for r in rows]
        await m.answer("Последние поисковые запросы:\n" + "\n".join(reversed(lines)))

    # ---------- выбор объекта кнопками ----------
    async def on_object_pick(self, q: CallbackQuery):
        await q.answer()
        d = dialogs.get(q.from_user.id)
        if not d:
            await q.message.edit_text("Диалог уже закрыт, начни заново.")
            return
        choice = q.data[4:]
        if choice == "new":
            d["expect"] = "object_new"
            await q.message.answer("Как называется новый объект?")
        else:
            d["draft"]["object"] = choice
            await q.message.edit_text(f"Объект: {choice}.")
            await self.finish_or_ask(q.from_user.id, q.message.answer)

    # ---------- правка строк ----------
    @staticmethod
    def row_text(v: list) -> str:
        return " | ".join(str(c) for c in v[:7])

    async def do_edit(self, uid, send, find, changes):
        rows = self.sheets.expense_rows()
        pick = llm.pick_expense_row(find, rows)
        by_row = {r["row"]: r["v"] for r in rows}
        if pick["type"] == "one":
            await self.apply_edit(uid, send, pick["row"], changes, by_row)
        elif pick["type"] == "many":
            dialogs[uid] = {"expect": "edit_pick", "changes": changes, "by_row": by_row}
            buttons = [(f"{by_row[n][0]} · {by_row[n][3]} · {by_row[n][4]} ₽", f"edit:{n}")
                       for n in pick.get("rows", []) if n in by_row]
            await send("Какую строку менять?", reply_markup=kb(buttons))
        else:
            await send("Не нашёл подходящую строку расходов. Уточни, пожалуйста (объект, дату или описание).")

    async def apply_edit(self, uid, send, row_no, changes, by_row):
        dialogs.pop(uid, None)
        old = by_row.get(row_no)
        await send(f"✏️ Меняю строку:\n{self.row_text(old) if old else row_no}")
        new = self.sheets.update_expense(row_no, changes)
        what = ", ".join(f"{k} → {v}" for k, v in changes.items() if v is not None)
        await send(f"✅ Изменил ({what}):\n{self.row_text(new)}")

    async def on_edit_pick(self, q: CallbackQuery):
        await q.answer()
        d = dialogs.get(q.from_user.id)
        if not d or d.get("expect") != "edit_pick":
            await q.message.edit_text("Правка уже закрыта, начни заново.")
            return
        await q.message.edit_text(f"Выбрана строка:\n{self.row_text(d['by_row'][int(q.data[5:])])}")
        await self.apply_edit(q.from_user.id, q.message.answer, int(q.data[5:]),
                              d["changes"], d["by_row"])

    # ---------- доступ ----------
    async def check_access(self, m: Message) -> bool:
        """True — доступ есть; False — обработка не нужна (заявка отправлена/отказ)."""
        uid = m.from_user.id
        if uid in self.users:
            return True
        if uid in self.denied:
            await m.answer("Доступ не одобрен. Обратитесь к Александру.")
            return False
        if uid in self.pending:
            await m.answer("Заявка уже отправлена Александру, ждите решения.")
            return False
        u = m.from_user
        who = u.full_name + (f" (@{u.username})" if u.username else "")
        self.pending[uid] = {"name": u.full_name}
        for aid in config.ADMIN_IDS:
            try:
                await self.bot.send_message(
                    aid,
                    f"🔔 Новый пользователь просит доступ к боту:\n{who}\nID: `{uid}`\n\nОткрыть доступ?",
                    reply_markup=kb([("✅ Да, открыть", f"acc:{uid}:yes"), ("❌ Нет", f"acc:{uid}:no")]),
                )
            except Exception:
                logging.exception("не удалось отправить заявку админу %s", aid)
        await m.answer("У вас пока нет доступа 🙁 Заявка отправлена Александру — как одобрит, бот вам ответит.")
        return False

    async def on_access_decision(self, q: CallbackQuery):
        await q.answer()
        try:
            _, uid_s, dec = q.data.split(":")
            uid = int(uid_s)
        except ValueError:
            return
        p = self.pending.pop(uid, None)
        if p is None:
            await q.message.edit_text("Заявка уже обработана.")
            return
        name = p.get("name") or f"Пользователь {uid}"
        if dec == "yes":
            self.sheets.add_user(uid, name)
            self.users[uid] = name
            await q.message.edit_text(f"✅ Доступ открыт: {name} ({uid}).")
            try:
                await self.bot.send_message(uid, "✅ Александр открыл вам доступ! Теперь пишите расходы и задачи — как текстом, так и голосом.")
            except Exception:
                pass
        else:
            self.denied.add(uid)
            await q.message.edit_text(f"❌ Отклонено: {name} ({uid}).")
            try:
                await self.bot.send_message(uid, "❌ В доступе отказано.")
            except Exception:
                pass

    # ---------- голосовые ----------
    async def on_voice(self, m: Message):
        if not await self.check_access(m):
            return
        note = await m.answer("🎤 Расшифровываю…")
        buf = BytesIO()
        await self.bot.download(m.voice, destination=buf)
        try:
            text = llm.transcribe(buf.getvalue(), getattr(m.voice, "file_name", None) or "voice.ogg")
        except Exception as e:
            logging.exception("transcribe failed")
            text = ""
        if not text:
            await note.edit_text("Не удалось расшифровать голосовое 🙁 Продиктуйте ещё раз или напишите текстом.")
            return
        dialogs[uid] = {"expect": "voice_confirm", "text": text}
        await note.edit_text(
            f"🎤 Распознал:\n«{text}»\n\nПродолжить?",
            reply_markup=kb([("✅ Да, продолжить", "voice:yes"), ("❌ Нет, отмена", "voice:no")]),
        )

    async def on_voice_confirm(self, q: CallbackQuery):
        await q.answer()
        d = dialogs.get(q.from_user.id)
        if not d or d.get("expect") != "voice_confirm":
            await q.message.edit_text("Расшифровка уже обработана — отправьте голосовое заново.")
            return
        if q.data == "voice:no":
            dialogs.pop(q.from_user.id, None)
            await q.message.edit_text("❌ Отменено. Продиктуйте заново или напишите текстом.")
            return
        text = d.get("text", "")
        dialogs.pop(q.from_user.id, None)
        await q.message.edit_text(f"🎤 Принято: «{text}»")
        await self.process_text(q.from_user.id,
                                self.users.get(q.from_user.id, config.USERS.get(q.from_user.id, "?")),
                                text, q.message.answer)

    # ---------- основная обработка ----------
    async def on_message(self, m: Message):
        if not await self.check_access(m):
            return
        uid = m.from_user.id
        text = (m.text or "").strip()
        if not text:
            return
        await self.process_text(uid, self.users.get(uid, config.USERS.get(uid, "?")), text, m.answer)

    async def process_text(self, uid, author, text, send):
        # пользователь прислал новое сообщение вместо нажатия кнопки — расшифровка отменяется
        if dialogs.get(uid, {}).get("expect") == "voice_confirm":
            dialogs.pop(uid, None)

        # открытый поиск (ждём кнопку источника): новый текст — либо новый поисковый
        # запрос (перезапуск брифа), либо обычное сообщение — обрабатываем ниже
        if dialogs.get(uid, {}).get("expect") == "brief":
            check = llm.extract(text, "бот показал бриф поиска и ждёт выбор источника кнопкой или уточнение текстом")
            if check.get("action") == "search":
                return await self.start_request(uid, author, text, send)
            dialogs.pop(uid, None)

        d = dialogs.get(uid)
        if d and d.get("expect") == "object_new":  # ждём название нового объекта
            check = llm.extract(text, "бот спросил у пользователя: как называется новый объект?")
            if check.get("action") in ("answer", "none"):
                # это действительно название — спрашивать больше не надо, пользователь уже подтвердил «Новый объект»
                d["draft"]["object"] = self.sheets.add_object(text)
                dialogs.pop(uid, None)
                await self.finish_or_ask(uid, send, author=author, final=True)
                return
            # пользователь начал другой диалог — новый объект НЕ добавляем, обрабатываем сообщение заново
            dialogs.pop(uid, None)
            d = None

        ctx = None
        if d and d.get("expect"):
            ctx = f"бот спросил у пользователя: {d['question']}"

        data = llm.extract(text, ctx)
        action = data.get("action")

        # если ждём ответ и LLM считает это ответом — подставляем
        if d and d.get("expect") and action in ("answer", "none", "expense", "task"):
            if action == "answer" or (action in ("expense", "task") and d.get("expect")):
                return await self.apply_answer(uid, author, data.get("value") or text, send)

        if action == "expense":
            dialogs.pop(uid, None)
            dialogs[uid] = {"expect": None, "draft": {
                "type": "expense", "author": author,
                "amount": data.get("amount"), "object": data.get("object"),
                "description": data.get("description") or text,
            }}
            await self.process_expense(uid, send)
        elif action == "task":
            dialogs.pop(uid, None)
            dialogs[uid] = {"expect": None, "draft": {
                "type": "task", "author": author,
                "assignee": data.get("assignee"), "description": data.get("description") or text,
                "deadline": data.get("deadline"),
            }}
            await self.process_task(uid, send)
        elif action == "task_done":
            ok = self.sheets.close_task(int(data.get("task_number") or 0))
            await send("Закрыл ✅" if ok else "Не нашёл такую задачу.")
        elif action == "edit":
            find = data.get("find") or {}
            changes = data.get("changes") or {}
            await self.do_edit(uid, send, find, changes)
        elif action == "report":
            await self.report(send)
        elif action == "search":
            await self.start_request(uid, author, text, send)
        else:
            # не расход, не задача, не команда — просто общаемся
            await send(llm.chat(author, text))

    # ---------- расход ----------
    async def process_expense(self, uid, send):
        d = dialogs[uid]
        dr = d["draft"]
        if not dr.get("amount"):
            d.update(expect="amount", question="Какая сумма в рублях?")
            return await send("На какую сумму расход (в рублях)?")
        if not dr.get("object"):
            return await self.ask_object(uid, send)
        await self.save_expense(uid, send)

    async def ask_object(self, uid, send):
        d = dialogs[uid]
        guess = d["draft"].get("object")
        if guess:
            known = llm.match_object(guess, self.sheets.objects())
            if known:
                d["draft"]["object"] = known
                return await self.save_expense(uid, send)
        d.update(expect="object", question="На какой объект отнести расход?")
        recent = self.sheets.recent_objects()
        buttons = [(o, f"obj:{o}") for o in recent] + [("➕ Новый объект", "obj:new")]
        await send("На какой объект отнести расход?", reply_markup=kb(buttons))

    async def save_expense(self, uid, send):
        d = dialogs.pop(uid)
        dr = d["draft"]
        category = llm.guess_category(
            dr["description"], dr["object"], self.sheets.similar_expenses(dr["description"]))
        self.sheets.add_expense(dr["author"], dr["object"], dr["amount"], category, dr["description"])
        await send(f"✅ Записал расход: {dr['amount']} ₽ · {dr['object']} · {category}\n«{dr['description']}»")

    # ---------- задача ----------
    async def process_task(self, uid, send):
        d = dialogs[uid]
        dr = d["draft"]
        if not dr.get("assignee"):
            d.update(expect="assignee", question="Для кого задача (Александр или Сергей)?")
            return await send("Для кого задача — Александр или Сергей?")
        no = self.sheets.add_task(dr["author"], dr["assignee"], dr["description"], dr.get("deadline"))
        dialogs.pop(uid, None)
        await send(f"✅ Задача №{no} для {dr['assignee']}: «{dr['description']}»")

    # ---------- ответы на вопросы ----------
    async def apply_answer(self, uid, author, value, send):
        d = dialogs.get(uid)
        if not d or not d.get("expect"):
            return
        exp = d["expect"]
        if exp == "amount":
            parsed = llm.extract(f"Сумма: {value}")
            amt = parsed.get("amount")
            if not amt:
                return await send("Не понял сумму. Напиши числом, например 5000.")
            d["draft"]["amount"] = amt
            d["expect"] = None
            await self.process_expense(uid, send)
        elif exp == "object":
            known = llm.match_object(value, self.sheets.objects())
            if known:
                d["draft"]["object"] = known
                d["expect"] = None
                await self.save_expense(uid, send)
            else:
                await self.ask_object(uid, send)
        elif exp == "assignee":
            who = llm.match_person(value)
            if not who:
                return await send("Не понял. Напиши: Александр или Сергей.")
            d["draft"]["assignee"] = who
            dialogs.pop(uid, None)
            d2 = {"draft": d["draft"]}
            no = self.sheets.add_task(d["draft"]["author"], who,
                                      d["draft"]["description"], d["draft"].get("deadline"))
            await send(f"✅ Задача №{no} для {who}: «{d['draft']['description']}»")

    # ---------- дозаполнение ----------
    async def finish_or_ask(self, uid, send, author=None, final=False):
        d = dialogs.get(uid)
        if not d:
            return
        dr = d["draft"]
        if dr["type"] == "expense":
            await self.process_expense(uid, send)
        else:
            await self.process_task(uid, send)

    # ---------- отчёт ----------
    async def report(self, send):
        expenses = self.sheets.all_expenses()[-30:]
        if not expenses:
            return await send("Расходов пока нет.")
        text = "\n".join(" | ".join(str(c) for c in r) for r in expenses)
        await send(llm.summarize("Сделай краткую сводку расходов в виде маркированного списка, "
                                 "итог по суммам в конце:\n" + text))

    # ---------- поиск поставщиков (ZAKUP) ----------
    async def start_request(self, uid: int, author: str, text: str, send):
        """Текст поискового запроса -> бриф + выбор источника."""
        brief = await asyncio.to_thread(llm.parse_request, text)
        if brief.get("chat"):
            dialogs.pop(uid, None)
            return await send(llm.chat(text))
        dialogs[uid] = {"expect": "brief", "draft": {"text": text, "author": author, "brief": brief}}
        city = brief.get("city") or "—"
        volume = brief.get("volume") or "—"
        if brief.get("source"):
            buttons = [(f"✅ Искать: {SOURCES[brief['source']]}", f"go:{brief['source']}"),
                       ("🌐 Весь интернет", "go:web"),
                       ("❌ Отмена", "go:cancel")]
        else:
            buttons = [("Авито", "go:avito"), ("Яндекс Услуги", "go:yandex_uslugi"),
                       ("🌐 Весь интернет", "go:web"), ("🌍 Все источники", "go:all"),
                       ("❌ Отмена", "go:cancel")]
        await send(
            "🔎 Запрос понят:\n"
            f"• Что: {brief['what']}\n"
            f"• Тип: {brief['type']}\n"
            f"• Город: {city}\n"
            f"• Объём: {volume}\n\n"
            "Где искать?",
            reply_markup=kb(buttons, cols=2))

    async def on_go(self, q: CallbackQuery):
        await q.answer()
        uid = q.from_user.id
        d = dialogs.get(uid)
        if not d or d.get("expect") != "brief":
            await q.message.edit_text("Диалог уже закрыт — напишите поисковый запрос заново.")
            return
        choice = q.data[3:]
        if choice == "cancel":
            dialogs.pop(uid, None)
            await q.message.edit_text("❌ Отменено. Напишите новый запрос, когда понадобится.")
            return
        sources = ["avito", "yandex_uslugi", "web"] if choice == "all" else [choice]
        labels = " + ".join(SOURCES[s] for s in sources)
        dialogs.pop(uid, None)
        draft = d["draft"]
        await q.message.edit_text(f"🔎 Ищу: {draft['brief']['what']} ({labels})…")
        result = await asyncio.to_thread(
            self._search_pipeline, uid, draft, choice, labels, sources)
        await self._send_results(q.message.edit_text, q.message.answer, *result)

    @staticmethod
    def search_source(brief: dict, source: str) -> list:
        """Основной поиск — Yandex Search API; нет ключа или ошибка -> веб-поиск GLM."""
        try:
            if yandex.configured():
                return yandex.search_yandex(brief, source)
        except Exception:
            logging.exception("yandex search failed, fallback to GLM")
        return glm_web.search_web(brief, source)

    def _search_pipeline(self, uid: int, draft: dict, choice: str, labels: str, sources: list[str]):
        brief = draft["brief"]
        no = self.zs.add_request(draft["author"], draft["text"], brief, labels)
        raw = []
        try:
            for src in sources:
                raw.extend(self.search_source(brief, src))
            ranked = llm.rank_results(brief, raw)
            final = [(raw[it["i"] - 1], it["name"], it["why"]) for it in ranked]
            seen, uniq = set(), []  # защитный дедуп по ссылке
            for sup, name, why in final:
                key = sup.url.rstrip("/").lower()
                if key not in seen:
                    seen.add(key)
                    uniq.append((sup, name, why))
            final = uniq
            self.zs.add_suppliers(no, [sup for sup, _, _ in final])
            status = "Есть результаты" if final else "Нет результатов"
            self.zs.update_request(no, status, found=len(final))
            return no, final, labels, None
        except Exception as e:
            logging.exception("search failed")
            self.zs.update_request(no, "Ошибка", note=str(e))
            return no, [], labels, str(e)

    async def _send_results(self, edit, send, no: int, final: list, labels: str, error: str | None):
        if error:
            await edit(f"⚠️ По запросу №{no} поиск не удался: {error[:200]}.\n"
                       "Попробуйте ещё раз — или я посмотрю логи.")
            return
        if not final:
            await edit(f"🤷 По запросу №{no} ничего подходящего не нашёл ({labels}).\n"
                       "Попробуйте переформулировать или выбрать другой источник.")
            return
        header = f"✅ Запрос №{no} — нашёл {len(final)} поставщиков ({labels}):\n\n"
        items = [f"{i}. {name}\n{why}\n{sup.url}" for i, (sup, name, why) in enumerate(final, 1)]
        chunks, cur = [], header  # склеиваем в сообщения <= 4000 символов
        for it in items:
            block = it + "\n\n"
            if len(cur) + len(block) > 4000:
                chunks.append(cur)
                cur = ""
            cur += block
        chunks.append(cur)
        await edit(chunks[0].rstrip())
        for c in chunks[1:]:
            await send(c.rstrip())


if __name__ == "__main__":
    asyncio.run(TBot().run())
