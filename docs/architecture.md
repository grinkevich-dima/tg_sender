# Архитектура

Один процесс Python: веб-сервер FastAPI, клиент Telethon и фоновый отправщик работают в одном цикле asyncio.

```
Браузер ──HTTP──▶ FastAPI (app/main.py) ──▶ SQLite (data/sender.db)
                        │                         ▲
                        ▼                         │
                  TgManager (app/tg.py) ◀──── Worker (app/worker.py)
                        │  MTProto                 цикл: выбрать сообщение → лимиты → отправить
                        ▼
                    Telegram  ──события──▶ NewMessage / MessageRead → статусы «ответил» / «прочитано»
```

## Модули

| Файл | Назначение |
|---|---|
| `app/config.py` | чтение `.env`, пути, часовой пояс, настройки по умолчанию |
| `app/db.py` | подключение к SQLite (WAL), схема, миграции колонок, настройки, журнал |
| `app/main.py` | маршруты панели, Basic-auth, защита от CSRF (Origin/Referer) и чужих хостов, одноразовые сообщения (cookie) |
| `app/tg.py` | `TgManager`: вход (код, 2FA, QR), импорт контактов, поиск получателей, обработчики событий |
| `app/worker.py` | фоновая отправка: лимиты, рабочие часы, паузы, обработка ошибок Telegram |
| `app/templating.py` | переменные `{…}` и спинтакс `{a\|b}` |
| `app/outreach.py` | списки xlsx: разбор колонок и ссылок, фильтры, постановка в очередь |
| `app/xlsx.py` | чтение `.xlsx` без сторонних библиотек (zip + XML) |
| `app/templates/*.html` | Jinja2-шаблоны страниц; стили — Pico CSS (`app/static`) |
| `tests/` | pytest с имитацией Telegram-клиента |

## Схема БД

| Таблица | Ключевые поля |
|---|---|
| `settings` | `key`, `value` — лимиты, паузы, рабочие часы, стоп-слова, `paused_until`, `warmup_start_date` |
| `contacts` | `tg_user_id`, `username`, `phone`, `first_name`, `last_name`, `extra` (JSON), `tags`, `opted_out` |
| `templates` | `name`, `body` |
| `campaigns` | `template_id`, `status` (draft / running / paused / done) |
| `messages` | `campaign_id`, `contact_id`, `status`, `text`, `tg_message_id`, `peer_id`, `error`, `sent_at`, `sent_day`, `read_at`, `replied_at` |
| `lists` | `name`, `filename`, `status` |
| `list_items` | `kind`, `title`, `peer_id` (marked ID), `username`, `topic_id`, `dialog` (файл), `real_dialog` (Telegram), `address`, `text`, `src_status`, `src_group`, `state`, `order_idx`, `error`, `tg_message_id`, `sent_*`/`read_at`/`replied_at` |
| `tg_dialogs` | `peer_id`, `title`, `kind` — снимок диалогов для поиска чатов по названию |
| `event_log` | `ts`, `level`, `text` |

Новые колонки добавляются в `db.conn()` через `ALTER TABLE … ADD COLUMN`, ошибка «колонка уже есть» игнорируется.

## Отправщик (`worker.tick`)

1. Аккаунт не авторизован → ждать 15 с.
2. Идёт «Найти получателей» → ждать 15 с. Действует `paused_until` (FloodWait / PEER_FLOOD) → ждать.
3. Закрыть кампании и списки без очереди (`done`).
4. Взять следующее: сначала `messages` запущенных кампаний, потом `list_items` запущенных списков (по `order_idx`).
5. Вне рабочих часов → ждать 60 с. Лимит дня исчерпан → ждать 5 мин.
6. Атомарно перевести строку в `sending` (если её уже взял другой запрос — пропустить), отправить, записать результат, выждать случайную паузу `delay_min…delay_max`.

Сон прерывается событием `worker.wake`: его выставляют запуск кампании, смена настроек и вход в аккаунт.

Обработка ошибок:
- `FloodWaitError` → `paused_until = now + N`;
- `PeerFloodError` → пауза 24 ч, все кампании и списки `paused`;
- ошибки получателя и любые непредвиденные → `failed` с причиной (очередь не блокируется);
- сетевой сбой → строка возвращается в прежнее состояние, повтор позже;
- строки, оставшиеся в `sending` после падения процесса, при старте помечаются `failed` («прервано…») — повторно не отправляются, чтобы не было дублей.

Telethon создаётся с `flood_sleep_threshold=0`, поэтому FloodWait обрабатывает сам отправщик. Во время «Найти получателей» порог временно поднимается до 300 с; поиск держит `tg.lock`, поэтому отправка в это время не идёт.

## Поиск получателей (`TgManager.prepare_list`)

1. `iter_dialogs()` — Telethon кэширует `access_hash` всех собеседников в сессии; диалоги пишутся в `tg_dialogs`; для людей из списка выставляется `real_dialog`.
2. Для людей, которых нет в кэше, — `iter_participants()` по чатам из списка.

Разрешение адресата (`resolve_item`): `peer_id` → альтернативная запись ID (`-100…` ↔ `-…`) → `username` → для чатов поиск по названию в `tg_dialogs`.

## События Telegram

- `NewMessage(incoming, private)` → статус «ответил» у сообщений этому человеку; стоп-слово → `opted_out` и «пропущено» в очередях; затем вызываются хуки `incoming_hooks`.
- `MessageRead(outbox)` → «прочитано» до `max_id`.
- Досинхронизация прочтений (`refresh_reads`) — раз в 30 мин и по кнопке.

## Точка расширения: ИИ-ответы

`app/tg.py` → список `incoming_hooks`. Каждая функция `async def hook(event, contact_row)` вызывается на каждое входящее личное сообщение. Здесь можно подключить автоответчик (например, Claude API):

```python
from app.tg import incoming_hooks

async def ai_reply(event, contact):
    if contact is None:          # отвечать только известным контактам
        return
    text = await ask_llm(event.raw_text)
    await event.reply(text)

incoming_hooks.append(ai_reply)
```

Альтернатива без риска для рассылки — отдельный Telegram Business-бот (нужен Premium), подключённый к аккаунту.

## Маршруты

| Метод | Путь | Назначение |
|---|---|---|
| GET | `/` | дашборд |
| GET/POST | `/auth`, `/auth/phone`, `/auth/code`, `/auth/password`, `/auth/logout` | вход по коду |
| GET | `/auth/qr`, `/auth/qr/status`, `/auth/qr/done` | вход по QR |
| GET/POST | `/contacts`, `/contacts/import-tg`, `/contacts/import-csv`, `/contacts/{id}/optout`, `/contacts/delete` | контакты |
| GET/POST | `/templates`, `/templates/save`, `/templates/{id}/test`, `/templates/{id}/delete` | шаблоны |
| GET/POST | `/campaigns`, `/campaigns/create`, `/campaigns/{id}`, `/campaigns/{id}/{start\|pause\|retry\|delete}`, `/campaigns/{id}/export.csv` | кампании |
| GET/POST | `/lists`, `/lists/upload`, `/lists/{id}`, `/lists/{id}/enqueue`, `/lists/{id}/{start\|pause\|unqueue\|prepare\|delete}`, `/lists/{id}/prepare-status`, `/lists/{id}/export.csv` | списки xlsx |
| POST | `/lists/item/{id}/send`, `/lists/item/{id}/{skip\|reset}` | действия со строкой |
| POST | `/reads/refresh` | обновить прочтения |
| GET/POST | `/settings` | прогрев и лимиты |
| GET | `/log` | журнал |

Все POST-формы после обработки перенаправляют (Post/Redirect/Get). Сообщение об успехе или ошибке передаётся одноразовой cookie `flash`, а не в адресе страницы.
