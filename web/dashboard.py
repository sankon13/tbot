# -*- coding: utf-8 -*-
"""Веб-дашборд: список запросов со статусами и карточка запроса со ссылками на поставщиков.

Живёт в одном процессе с ботом (aiohttp). Вход по паролю из config.DASHBOARD_PASSWORD.
"""
import hashlib
import hmac
from pathlib import Path

from aiohttp import web
from jinja2 import Environment, FileSystemLoader

import config
from searchers.base import SOURCES

TEMPLATES = Path(__file__).parent / "templates"
AUTH_COOKIE = "zakup_auth"

REQ_FIELDS = ["no", "date", "author", "text", "type", "what", "city",
              "sources", "status", "found", "note"]
SUP_FIELDS = ["req_no", "name", "description", "url", "source", "kp_status", "note"]

BADGE_CLS = {
    "Есть результаты": "ok",
    "Поиск": "run",
    "Новый": "",
    "Нет результатов": "warn",
    "Ошибка": "err",
}

_env = Environment(loader=FileSystemLoader(str(TEMPLATES)), autoescape=True)


def _render(tpl: str, **ctx) -> str:
    return _env.get_template(tpl).render(**ctx)


def _auth_token() -> str:
    return hashlib.sha256(f"zakup|{config.DASHBOARD_PASSWORD}".encode()).hexdigest()


def _row(fields: list[str], row: list) -> dict:
    return {f: (row[i] if i < len(row) else "") for i, f in enumerate(fields)}


def _req_view(row: list) -> dict:
    r = _row(REQ_FIELDS, row)
    r["cls"] = BADGE_CLS.get(r["status"], "")
    return r


@web.middleware
async def auth_middleware(request, handler):
    if request.path == "/login":
        return await handler(request)
    cookie = request.cookies.get(AUTH_COOKIE, "")
    if hmac.compare_digest(cookie, _auth_token()):
        return await handler(request)
    raise web.HTTPFound("/login")


async def login_get(request):
    return web.Response(text=_render("login.html", error=False), content_type="text/html")


async def login_post(request):
    form = await request.post()
    if hmac.compare_digest(str(form.get("password", "")), config.DASHBOARD_PASSWORD):
        resp = web.HTTPFound("/")
        resp.set_cookie(AUTH_COOKIE, _auth_token(), max_age=60 * 60 * 24 * 30,
                        httponly=True, samesite="Lax")
        return resp
    return web.Response(text=_render("login.html", error=True), content_type="text/html")


async def logout(request):
    resp = web.HTTPFound("/login")
    resp.del_cookie(AUTH_COOKIE)
    return resp


async def index(request):
    sheets = request.app["sheets"]
    rows = [_req_view(r) for r in sheets.all_requests() if r and r[0].strip()]
    rows.sort(key=lambda r: r["no"].strip().isdigit() and int(r["no"]) or 0, reverse=True)
    return web.Response(text=_render("requests.html", items=rows), content_type="text/html")


async def request_page(request):
    sheets = request.app["sheets"]
    no = request.match_info["no"]
    row = sheets.get_request(no)
    if not row:
        return web.HTTPNotFound(text="Запрос не найден")
    suppliers = []
    for s in sheets.suppliers_for(no):
        v = _row(SUP_FIELDS, s)
        v["source_label"] = SOURCES.get(v["source"], v["source"])
        suppliers.append(v)
    return web.Response(
        text=_render("request.html", req=_req_view(row), suppliers=suppliers),
        content_type="text/html")


def add_routes(app: web.Application, sheets):
    """Подключает дашборд к основному приложению."""
    app["sheets"] = sheets
    app.middlewares.append(auth_middleware)
    app.router.add_get("/login", login_get)
    app.router.add_post("/login", login_post)
    app.router.add_get("/logout", logout)
    app.router.add_get("/", index)
    app.router.add_get("/request/{no}", request_page)
