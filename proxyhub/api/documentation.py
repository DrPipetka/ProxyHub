"""Offline API reference: actual FastAPI parameters plus examples for raw bodies."""
from copy import deepcopy

from ..config import Settings


DESCRIPTIONS = {
    ("post", "/service/enable"): ("Включить работу прокси", "Возобновляет ядро, пул, фоновые проверки и автообновление подписок. Запуск ядра выполняется в фоне; готовность смотрите в /status. Выбор сохраняется после перезапуска."),
    ("post", "/service/disable"): ("Отключить работу прокси", "Закрывает SOCKS-подключения, сбрасывает аренды, останавливает ядро, фоновые проверки и автообновление подписок. Панель, библиотека и настройки остаются доступны. Выдача /proxy и /config возвращает 503 service_disabled. Выбор сохраняется после перезапуска."),
    ("get", "/health"): ("Доступность HTTP", "Публичная проверка HTTP-процесса. Готовность ядра смотрите в /status."),
    ("post", "/auth/login"): ("Войти в панель", "Создаёт UI-сессию на 12 часов. Для внешнего клиента используйте Bearer-токен из настроек."),
    ("post", "/auth/logout"): ("Завершить UI-сессию", "Удаляет текущую сессию панели."),
    ("get", "/status"): ("Состояние приложения", "Состояния конфигов, пул, очередь проверок, ресурсы и готовность Xray. operation.enabled — работа включена, operation.transitioning — переключение выполняется."),
    ("get", "/count"): ("Количество SOCKS5", "Число поднятых прокси; state фильтрует свободные или арендованные. format=text возвращает число текстом."),
    ("get", "/proxy"): ("Арендовать SOCKS5", "Выдаёт addr, host, port, метрики и срок аренды. Создаёт реальную аренду. Верните её через /proxy/release или /proxy/used. wait — ожидание до 120 секунд; geo — коды стран через запятую."),
    ("get", "/config"): ("Получить исходный конфиг", "Возвращает полный link и метрики. По умолчанию создаёт аренду; lease=0 — просмотр без аренды. format=text возвращает исходный конфиг."),
    ("get", "/proxies"): ("Список конфигов", "Пагинация, фильтры и сортировка. items[].link содержит полный сохранённый конфиг, включая учётные данные. q ищет по имени, серверу, источнику и ID."),
    ("post", "/proxies"): ("Импорт конфигов и подписок", "JSON с одним из полей: data, url или urls (массив от 1 до 500 URL), source — общая метка. Для urls пробелы по краям и пустые строки игнорируются, повторы пропускаются; каждая подписка обрабатывается независимо. Пакет возвращает HTTP 200, results с результатом или ошибкой каждого URL, subscriptions_total/succeeded/failed и duplicate_urls; счётчики конфигов суммируются по успешным подпискам. Подписка сохраняется; interval_minutes по умолчанию 60, 0 отключает автоматическое обновление. Повторный URL обновляет существующую подписку. Также принимаются text/plain, Clash YAML и sing-box JSON. Ответ: received, accepted, duplicates, unsupported, parse_errors, errors."),
    ("delete", "/proxies"): ("Удалить конфиги", "Требует confirm=yes. scope: all, invalid или source (с source). Закрывает SOCKS5, удаляет конфиги и историю. Расписания подписок сохраняются и могут повторно импортировать конфиги."),
    ("post", "/proxy/{action}"): ("Завершить или вернуть аренду", "action: used — отлежка, invalid — невалидный, release — вернуть без отлежки. В теле укажите ровно одно из id или addr; reason — причина отказа."),
    ("post", "/actions"): ("Массовое действие", "Применяет action к ids (до 500). results содержит состояние или ошибку для каждого ID."),
    ("get", "/proxies/{proxy_id}/link"): ("Полный конфиг", "Возвращает сохранённый исходный конфиг в поле link."),
    ("get", "/proxies/{proxy_id}/history"): ("История проверок", "Последние 20 проверок выбранного конфига."),
    ("get", "/settings"): ("Читать настройки", "values содержит текущие значения и API-токен, schema — ограничения, restart_fields — параметры, требующие перезапуска."),
    ("patch", "/settings"): ("Изменить настройки", "Передайте только изменяемые поля. ui_password меняет пароль и отзывает UI-сессии. Ограничения полей доступны в GET /settings. Ответ содержит restart_required."),
    ("post", "/settings/test-urls"): ("Проверить тестовые URL", "Выполняет прямые сетевые запросы к сохранённым ping/speed/IP URL. results содержит url и ok."),
    ("get", "/events"): ("Журнал событий", "Последние события; before — ID для загрузки более ранних записей."),
    ("get", "/ui/events-fragment"): ("HTML журнала", "HTML-фрагмент для панели. Текст событий экранирован."),
    ("get", "/subscriptions"): ("Подписки и расписание", "items содержит URL, interval_minutes, last_attempt_at, last_success_at, next_update_at (Unix-секунды), duration_ms, last_error, last_result и updating. Интервал отсчитывается от завершения попытки, в том числе неудачной."),
    ("patch", "/subscriptions/{subscription_id}"): ("Интервал обновления", "Интервал в минутах: 0 — только вручную, 1–525600 — автоматически. Применяется сразу, расписание сохраняется при перезапуске."),
    ("post", "/subscriptions/{subscription_id}/refresh"): ("Обновить подписку сейчас", "Скачивает и импортирует подписку, возвращает счётчики импорта. Добавляет новые конфиги, сохраняет существующие и аренды. Одновременное обновление возвращает 409."),
    ("delete", "/subscriptions/{subscription_id}"): ("Удалить подписку", "Удаляет URL и расписание. Уже импортированные конфиги сохраняются."),
}

EXAMPLES = {
    ("post", "/auth/login"): {"password": ""},
    ("post", "/proxies"): {"url": "https://example.com/subscription", "source": "Основная подписка", "interval_minutes": 60},
    ("post", "/proxy/{action}"): {"id": 1},
    ("post", "/actions"): {"ids": [1], "action": "recheck"},
    ("patch", "/settings"): {"pool_size": 10},
    ("patch", "/subscriptions/{subscription_id}"): {"interval_minutes": 60},
}


def build_schema(app):
    schema = deepcopy(app.openapi())
    schema["info"]["description"] = "Авторизация: Authorization: Bearer <token> или UI-сессия. Ошибки: {error, message}. Время — Unix-секунды; длительность обновления — миллисекунды."
    schema.setdefault("components", {}).setdefault("securitySchemes", {})["BearerAuth"] = {"type": "http", "scheme": "bearer"}
    for path, methods in schema["paths"].items():
        for method, op in methods.items():
            title, description = DESCRIPTIONS.get((method, path), (op.get("summary", path), ""))
            op.update(summary=title, description=description)
            op["security"] = [] if path in ("/health", "/auth/login") else [{"BearerAuth": []}]
            op["responses"].pop("422", None)
            for code, message in {400: "Некорректные параметры", 401: "Требуется авторизация", 403: "Источник запроса запрещён", 404: "Не найдено или нет доступных прокси", 409: "Конфликт состояния", 413: "Слишком большое тело", 503: "Ядро или порты недоступны"}.items():
                op["responses"][str(code)] = {"description": message, "content": {"application/json": {"schema": {"type": "object", "properties": {"error": {"type": "string"}, "message": {"type": "string"}}}}}}
            example = EXAMPLES.get((method, path))
            if example is not None:
                body = op.setdefault("requestBody", {"required": True, "content": {"application/json": {"schema": {"type": "object"}}}})
                body["content"]["application/json"]["example"] = example
            if (method, path) == ("post", "/proxies"):
                op["requestBody"]["content"]["application/json"]["schema"] = {
                    "type": "object", "properties": {
                        "data": {"type": "string"}, "url": {"type": "string", "format": "uri"},
                        "urls": {"type": "array", "minItems": 1, "maxItems": 500, "items": {"type": "string"}, "example": ["https://example.com/sub1", "https://example.com/sub2"]},
                        "source": {"type": "string", "maxLength": 256},
                        "interval_minutes": {"type": "integer", "minimum": 0, "maximum": 525600, "default": 60},
                    }, "oneOf": [{"required": [key], "not": {"anyOf": [{"required": [other]} for other in ("data", "url", "urls") if other != key]}} for key in ("data", "url", "urls")],
                }
                op["requestBody"]["content"]["text/plain"] = {"schema": {"type": "string"}, "example": "trojan://PASSWORD@example.com:443"}
            if (method, path) == ("patch", "/settings"):
                body_schema = Settings.model_json_schema()
                body_schema["properties"].pop("ui_password_hash", None)
                body_schema["properties"]["ui_password"] = {"type": "string", "maxLength": 1024, "description": "Пусто — удалить пароль, иначе не менее 8 символов"}
                op["requestBody"]["content"]["application/json"]["schema"] = body_schema
            if path in ("/proxy", "/config", "/count"):
                op["responses"]["200"]["content"]["text/plain"] = {"schema": {"type": "string"}}
    return schema
