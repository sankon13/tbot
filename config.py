# -*- coding: utf-8 -*-
"""Конфигурация. Локально — из config_local.py (не в git), на хостинге — из переменных окружения."""
import os
import tempfile

try:
    from config_local import *  # noqa
except ImportError:
    pass

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", globals().get("TELEGRAM_BOT_TOKEN", ""))
GLM_API_KEY = os.environ.get("GLM_API_KEY", globals().get("GLM_API_KEY", ""))
GLM_MODEL = os.environ.get("GLM_MODEL", globals().get("GLM_MODEL", "glm-5.3-flash"))

_sa = os.environ.get("GOOGLE_CREDENTIALS")  # JSON сервисного аккаунта одной строкой
if _sa:
    f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
    f.write(_sa)
    f.close()
    GOOGLE_SERVICE_ACCOUNT_FILE = f.name
else:
    GOOGLE_SERVICE_ACCOUNT_FILE = os.environ.get("SA_FILE", globals().get("GOOGLE_SERVICE_ACCOUNT_FILE", "service_account.json"))

SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID", globals().get("SPREADSHEET_ID", ""))

USERS = globals().get("USERS", {})
for pair in os.environ.get("USERS", "").split(","):
    if ":" in pair:
        tid, name = pair.split(":", 1)
        USERS[int(tid)] = name

ADMIN_IDS = [int(x) for x in globals().get("ADMIN_IDS", [])]
ADMIN_IDS += [int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip().isdigit()]

# Webhook (облако): Telegram сам присылает обновления и будит спящий инстанс.
# Локально (без WEBHOOK_URL) бот работает на long polling как раньше.
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", globals().get("WEBHOOK_URL", ""))
WEBHOOK_PATH = os.environ.get("WEBHOOK_PATH", "/tgwebhook")
import secrets as _secrets
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET") or globals().get("WEBHOOK_SECRET") or _secrets.token_hex(16)
