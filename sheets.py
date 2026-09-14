# -*- coding: utf-8 -*-
"""Работа с Google Таблицей через gspread (сервисный аккаунт)."""
from datetime import datetime

import gspread

import config

WD = ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс']


def weekday(datestr: str) -> str:
    try:
        return WD[datetime.strptime(datestr.split()[0], "%d.%m.%Y").weekday()]
    except ValueError:
        return ""


HEADERS = {
    "Расходы": ["Дата", "День недели", "Автор", "Описание", "Сумма", "Объект", "Категория"],
    "Задачи": ["№", "Дата", "День недели", "Автор", "Исполнитель", "Задача", "Срок", "Статус", "Объект", "Закрыта"],
    "Объекты": ["Название", "Создан", "Последнее упоминание", "Записей"],
    "Категории": ["Название", "Последнее упоминание", "Записей"],
    "Пользователи": ["Имя", "Telegram ID"],
    "Сводка": [],
}


class Sheets:
    def __init__(self):
        gc = gspread.service_account(filename=config.GOOGLE_SERVICE_ACCOUNT_FILE)
        self.sh = gc.open_by_key(config.SPREADSHEET_ID)

    # ---------- структура ----------
    def ensure_structure(self):
        existing = {w.title for w in self.sh.worksheets()}
        for title, headers in HEADERS.items():
            if title in existing:
                continue
            w = self.sh.add_worksheet(title=title, rows=1000, cols=10)
            if headers:
                w.insert_row(headers, 1)
                w.format("1:1", {"textFormat": {"bold": True}})
        try:
            self.sh.del_worksheet(self.sh.worksheet("Лист1"))  # дефолтный пустой лист
        except Exception:
            pass

    def _ws(self, title):
        return self.sh.worksheet(title)

    # ---------- пользователи ----------
    def load_users(self) -> dict[int, str]:
        users = {}
        for r in self._ws("Пользователи").get_all_values()[1:]:
            if len(r) >= 2 and str(r[1]).strip().isdigit():
                users[int(str(r[1]).strip())] = str(r[0]).strip()
        return users

    def add_user(self, uid: int, name: str):
        self._ws("Пользователи").append_row([name, uid])

    # ---------- объекты ----------
    def objects(self) -> list[str]:
        return [r[0] for r in self._ws("Объекты").get_all_values()[1:] if r and r[0]]

    def recent_objects(self, limit=8) -> list[str]:
        """Объекты, отсортированные по последнему упоминанию (свежие в начале)."""
        rows = [r for r in self._ws("Объекты").get_all_values()[1:] if r and r[0]]
        rows.sort(key=lambda r: r[2] if len(r) > 2 and r[2] else "", reverse=True)
        return [r[0] for r in rows[:limit]]

    def add_object(self, name: str) -> str:
        name = name.strip()
        now = datetime.now().strftime("%d.%m.%Y")
        self._ws("Объекты").append_row([name, now, now, 1])
        return name

    def touch_object(self, name: str):
        try:
            ws = self._ws("Объекты")
            cell = ws.find(name, in_column=1)
            if cell:
                ws.update_cell(cell.row, 3, datetime.now().strftime("%d.%m.%Y"))
                cur = ws.cell(cell.row, 4).value
                try:
                    ws.update_cell(cell.row, 4, int(cur or 0) + 1)
                except ValueError:
                    pass
        except Exception:
            pass

    # ---------- расходы ----------
    def add_expense(self, author, obj, amount, category, description):
        now = datetime.now().strftime("%d.%m.%Y %H:%M")
        self._ws("Расходы").append_row([
            now, weekday(now), author, description, amount, obj, category,
        ])
        self.touch_object(obj)

    def all_expenses(self) -> list[list]:
        return self._ws("Расходы").get_all_values()[1:]

    def expense_rows(self) -> list[dict]:
        """[(excel-номер строки, значения)] по последним 15 расходам, свежие в конце."""
        return [{"row": i + 2, "v": r} for i, r in enumerate(self.all_expenses())][-15:]

    COL_EXPENSE = {"date": 1, "description": 4, "amount": 5, "object": 6, "category": 7}

    def update_expense(self, excel_row: int, changes: dict) -> list:
        """Меняет поля строки расхода, возвращает новую версию строки."""
        ws = self._ws("Расходы")
        for field, col in self.COL_EXPENSE.items():
            if changes.get(field) is not None:
                ws.update_cell(excel_row, col, changes[field])
        if changes.get("date") is not None:
            ws.update_cell(excel_row, 2, weekday(str(changes["date"])))
        return [ws.cell(excel_row, c).value for c in range(1, 8)]

    def similar_expenses(self, description: str, limit=20) -> list[list]:
        """Последние расходы — как примеры для подбора категории."""
        return self.all_expenses()[-limit:]

    # ---------- задачи ----------
    def next_task_no(self) -> int:
        rows = self._ws("Задачи").get_all_values()[1:]
        if not rows:
            return 1
        try:
            return max(int(r[0]) for r in rows if r and r[0].isdigit()) + 1
        except ValueError:
            return len(rows) + 1

    def add_task(self, author, assignee, description, deadline=None, obj=None):
        no = self.next_task_no()
        now = datetime.now().strftime("%d.%m.%Y")
        self._ws("Задачи").append_row([
            no, now, weekday(now), author, assignee,
            description, deadline or "", "Новая", obj or "", "",
        ])
        return no

    def open_tasks(self) -> list[list]:
        return [r for r in self._ws("Задачи").get_all_values()[1:]
                if r and len(r) > 7 and r[7] != "Сделана"]

    def close_task(self, task_no: int) -> bool:
        try:
            ws = self._ws("Задачи")
            cell = ws.find(str(task_no), in_column=1)
            if not cell:
                return False
            ws.update_cell(cell.row, 8, "Сделана")
            ws.update_cell(cell.row, 10, datetime.now().strftime("%d.%m.%Y"))
            return True
        except Exception:
            return False
