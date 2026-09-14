# -*- coding: utf-8 -*-
"""Поиск поставщиков: Google Таблица «ZAKUP: поставщики и КП» (отдельная от учётной).

Листы: Запросы (№, Дата, Автор, Запрос, Тип, Что искать, Город, Источники, Статус, Найдено, Примечание),
Поставщики (№ запроса, Название, Описание, Ссылка, Источник, Статус КП, Заметки), Пользователи.
Статусы: Новый -> Поиск -> Есть результаты / Нет результатов / Ошибка. Этап 2 добавит КП-статусы
(колонка «Статус КП» уже есть).
"""
from datetime import datetime

import gspread

import config

HEADERS = {
    "Запросы": ["№", "Дата", "Автор", "Запрос", "Тип", "Что искать", "Город",
                 "Источники", "Статус", "Найдено", "Примечание"],
    "Поставщики": ["№ запроса", "Название", "Описание", "Ссылка", "Источник",
                    "Статус КП", "Заметки"],
    "Пользователи": ["Имя", "Telegram ID"],
}


class ZakupSheets:
    def __init__(self):
        gc = gspread.service_account(filename=config.GOOGLE_SERVICE_ACCOUNT_FILE)
        self.sh = gc.open_by_key(config.ZAKUP_SPREADSHEET_ID)

    def ensure_structure(self):
        existing = {w.title for w in self.sh.worksheets()}
        for title, headers in HEADERS.items():
            if title in existing:
                continue
            w = self.sh.add_worksheet(title=title, rows=1000, cols=len(headers) + 3)
            w.insert_row(headers, 1)
            w.format("1:1", {"textFormat": {"bold": True}})
        try:
            self.sh.worksheet("Лист1").delete()  # дефолтный пустой лист
        except Exception:
            pass

    def _ws(self, title):
        return self.sh.worksheet(title)

    # ---------- пользователи поиска (общий доступ = общие пользователи) ----------
    def load_users(self) -> dict[int, str]:
        users = {}
        for r in self._ws("Пользователи").get_all_values()[1:]:
            if len(r) >= 2 and str(r[1]).strip().isdigit():
                users[int(str(r[1]).strip())] = str(r[0]).strip()
        return users

    def add_user(self, uid: int, name: str):
        self._ws("Пользователи").append_row([name, uid])

    # ---------- запросы ----------
    def next_request_no(self) -> int:
        rows = self._ws("Запросы").get_all_values()[1:]
        if not rows:
            return 1
        try:
            return max(int(r[0]) for r in rows if r and r[0].strip().isdigit()) + 1
        except ValueError:
            return len(rows) + 1

    def add_request(self, author: str, text: str, brief: dict, source_label: str) -> int:
        no = self.next_request_no()
        now = datetime.now().strftime("%d.%m.%Y %H:%M")
        self._ws("Запросы").append_row([
            no, now, author, text,
            brief.get("type") or "", brief.get("what") or "",
            brief.get("city") or "", source_label, "Поиск", 0, "",
        ])
        return no

    def update_request(self, no: int, status: str, found: int | None = None, note: str | None = None):
        ws = self._ws("Запросы")
        cell = ws.find(str(no), in_column=1)
        if not cell:
            return
        ws.update_cell(cell.row, 9, status)
        if found is not None:
            ws.update_cell(cell.row, 10, found)
        if note:
            ws.update_cell(cell.row, 11, note[:500])

    def all_requests(self) -> list[list]:
        return self._ws("Запросы").get_all_values()[1:]

    def get_request(self, no: int) -> list | None:
        for r in self.all_requests():
            if r and r[0].strip() == str(no):
                return r
        return None

    # ---------- поставщики ----------
    def add_suppliers(self, req_no: int, suppliers: list):
        """suppliers: объекты searchers.base.Supplier."""
        if not suppliers:
            return
        ws = self._ws("Поставщики")
        rows = [[req_no, s.name[:200], (s.description or "")[:300], s.url, s.source, "", ""]
                for s in suppliers]
        ws.append_rows(rows)

    def suppliers_for(self, req_no: int) -> list[list]:
        return [r for r in self._ws("Поставщики").get_all_values()[1:]
                if r and r[0].strip() == str(no)]
