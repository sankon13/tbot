# Скопируйте в config.py и заполните
TELEGRAM_BOT_TOKEN = "123456:ABC-токен-от-BotFather"

# API-ключ GLM (https://open.bigmodel.cn)
GLM_API_KEY = "your-glm-api-key"
GLM_MODEL = "glm-4-flash"  # быстрая и дешёвая модель, можно заменить на glm-4-plus

# Сервисный аккаунт Google (JSON-ключ), доступ к таблице
GOOGLE_SERVICE_ACCOUNT_FILE = "service_account.json"

# ID Google Таблицы из ссылки:
# https://docs.google.com/spreadsheets/d/<ВОТ_ЭТОТ_ID>/edit
SPREADSHEET_ID = ""

# Белый список: telegram_id -> имя
USERS = {
    # 123456789: "Александр",
    # 987654321: "Сергей",
}

# Команды
CMD_TASKS = "задачи"       # список открытых задач
CMD_WEEK =  "неделя"       # сводка расходов за 7 дней
