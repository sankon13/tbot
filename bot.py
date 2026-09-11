# -*- coding: utf-8 -*-
"""TBot — Telegram-бот учёта расходов и задач с извлечением данных через GLM."""
import asyncio
import logging
import os

from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery
from aiogram.types.inline_keyboard_markup import InlineKeyboardMarkup
from aiogram.types.keyboard_button import KeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder

import config
import llm
from sheets import Sheets

logging.basicConfig(level=logging.INFO)

# Контекст диалога: user_id -> {"expect": "object|amount|assignee|...", "draft": {...}}
dialogs: dict[int, dict] = {}


def kb(buttons: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for text, data in buttons:
        b.button(text=text, callback_data=data)
    b.adjust(1)
    return b.as_markup()


class TBot:
    def __init__(self):
        self.bot = Bot(token=config.TELEGRAM_BOT_TOKEN)
        self.dp = Dispatcher()
        self.sheets = Sheets()
        self._register()

    def _register(self):
        self.dp.message(Command("start"))(self.cmd_start)
        self.dp.message(Command("tasks"))(self.cmd_tasks)
        self.dp.callback_query(F.data.startswith("obj:"))(self.on_object_pick)
        self.dp.callback_query(F.data.startswith("edit:"))(self.on_edit_pick)
        self.dp.message()(self.on_message)

    async def run(self):
        # health-эндпоинт для бесплатного хостинга (и keep-alive пингов)
        app = web.Application()
        app.router.add_get("/", lambda r: web.Response(text="TBot is alive"))
        runner = web.AppRunner(app)
        await runner.setup()
        port = int(os.environ.get("PORT", 8080))
        await web.TCPSite(runner, "0.0.0.0", port).start()
        await self.bot.delete_webhook(drop_pending_updates=True)
        await self.dp.start_polling(self.bot)

    # ---------- команды ----------
    async def cmd_start(self, m: Message):
        await m.answer(
            f"Привет, {config.USERS.get(m.from_user.id, m.from_user.first_name)}! "
            "Пиши расход («потратил 5000 на материалы для Садовой») "
            "или задачу («Серёге позвонить поставщику до пятницы»). "
            "Чего не хватит — спрошу. /tasks — открытые задачи."
        )

    async def cmd_tasks(self, m: Message):
        tasks = self.sheets.open_tasks()
        if not tasks:
            await m.answer("Открытых задач нет.")
            return
        lines = [f"№{r[0]} · {r[4]} · {r[5]}" + (f" · до {r[6]}" if r[6] else "") + f" · {r[7]}" for r in tasks]
        await m.answer("Открытые задачи:\n" + "\n".join(lines))

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

    # ---------- основная обработка ----------
    async def on_message(self, m: Message):
        uid = m.from_user.id
        if uid not in config.USERS:
            await m.answer("Нет доступа. Сообщите свой ID администратору: " f"`{uid}`")
            return
        author = config.USERS[uid]
        text = (m.text or "").strip()
        if not text:
            return

        d = dialogs.get(uid)
        if d and d.get("expect") == "object_new":  # ждём название нового объекта
            d["draft"]["object"] = self.sheets.add_object(text)
            dialogs.pop(uid, None)
            await self.finish_or_ask(uid, m.answer, author=author, final=True)
            return

        ctx = None
        if d and d.get("expect"):
            ctx = f"бот спросил у пользователя: {d['question']}"

        data = llm.extract(text, ctx)
        action = data.get("action")

        # если ждём ответ и LLM считает это ответом — подставляем
        if d and d.get("expect") and action in ("answer", "none", "expense", "task"):
            if action == "answer" or (action in ("expense", "task") and d.get("expect")):
                return await self.apply_answer(uid, author, data.get("value") or text, m.answer)

        if action == "expense":
            dialogs.pop(uid, None)
            dialogs[uid] = {"expect": None, "draft": {
                "type": "expense", "author": author,
                "amount": data.get("amount"), "object": data.get("object"),
                "description": data.get("description") or text,
            }}
            await self.process_expense(uid, m.answer)
        elif action == "task":
            dialogs.pop(uid, None)
            dialogs[uid] = {"expect": None, "draft": {
                "type": "task", "author": author,
                "assignee": data.get("assignee"), "description": data.get("description") or text,
                "deadline": data.get("deadline"),
            }}
            await self.process_task(uid, m.answer)
        elif action == "task_done":
            ok = self.sheets.close_task(int(data.get("task_number") or 0))
            await m.answer("Закрыл ✅" if ok else "Не нашёл такую задачу.")
        elif action == "edit":
            find = data.get("find") or {}
            changes = data.get("changes") or {}
            await self.do_edit(uid, m.answer, find, changes)
        elif action == "report":
            await self.report(m.answer)
        else:
            await m.answer("Не понял, это расход или задача? Уточни, пожалуйста.")

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


if __name__ == "__main__":
    asyncio.run(TBot().run())
