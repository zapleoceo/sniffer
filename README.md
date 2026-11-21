# SnifferBot

Бесплатный каталог объявлений. Telegram-бот выдаёт ссылку на MCP; поиск и просмотр
работают в подключённом ассистенте, без подписки. Сбор и обработка объявлений
продолжаются независимо. [Подключение и инструменты MCP](docs/public-catalog-mcp.md).

Первый рынок — Нячанг: байки, апартаменты, комнаты.

- Архитектура: [docs/architecture.md](docs/architecture.md)
- **Спецификация v2: [docs/spec-v2.md](docs/spec-v2.md)** — агент поиска, проверяльщик, монетизация
- Паспорт запроса: [docs/passport.md](docs/passport.md)

## Как это устроено в одном абзаце

Юзербот на Telethon читает курируемый список сообществ. Каждое сообщение
проходит воронку «дёшево → дорого»: бесплатный regex-гейт отсеивает ~85–90%,
выжившие пачками уходят в LLM через [AIbroker](https://aib.zapleo.com) —
сначала на бинарный `prefilter`, затем на извлечение карточки по строгой
JSON-схеме. Карточки ложатся в Postgres. Отдельный бот на aiogram выдаёт URL каталога. MCP показывает только
публичные рыночные факты и оригинальные ссылки.

Бот не перепечатывает объявления — только ссылается на оригинал.

## Локальный запуск

```bash
cp .env.example .env
```

Заполнить в `.env` как минимум `BOT_TOKEN`, `TG_API_ID`, `TG_API_HASH`,
`TG_PHONE`, `BROKER_PROJECT_KEY` (чеклист — в конце
[architecture.md](docs/architecture.md)).

```bash
docker compose up -d postgres
```

```bash
uv sync --extra dev
```

```bash
uv run pytest
```

## Структура

```
docs/            архитектура и доменные описания
infra/sql/       цепочка миграций NNN_*.sql (деплой применяет её на каждом запуске)
infra/backup/    резервная копия БД: pg_dump -Fc в /var/backups/vera/sniffer, 03:00 UTC (ставит деплой)
src/sniffer/
  config.py      настройки из окружения
  runtime/       каркас процесса: лог, сигналы, ожидание конфигурации
  domain/        паспорт запроса, объявление
  broker/        клиент AIbroker (submit + поллинг)
  search/        текст → паспорт → план поиска → исполнение плана
  sources/       адаптеры источников, один метод search()
  collector/     Telethon: live-подписка и messages.search
  worker/        воронка обработки, очередь jobs
  notifier/      доставка из outbox
  pipeline/      гейт → prefilter → извлечение → дедуп
  verifier/      grounding, соответствие паспорту, живость лота
  matching/      паспорт → SQL-запрос, ранжирование
  bot/           aiogram: выдача ссылки MCP и поддержка прежних платежей
  catalog_mcp/   публичный read-only каталог (Streamable HTTP)
  dashboard/     FastAPI: интерфейс владельца на lbot.zapleo.com
tests/
```

Шесть процессов запускаются своими модулями — `python -m sniffer.bot`,
`.collector`, `.agent_app.collector`, `.worker`, `.notifier`, `.dashboard`.
Процесс, которому не хватает секрета, не падает: он пишет, чего ждёт, и
перепроверяет окружение раз в минуту
([architecture.md, 3.1](docs/architecture.md#31-точки-входа)).

Порт слушает только `dashboard` — 8005, и только на loopback: снаружи ходит
nginx ([dashboard.md](docs/dashboard.md)).

## Состояние

Работает интерфейс владельца: лог запросов с расходами на модель и временем по
этапам, переписка клиентов, состояние сессии юзербота с переавторизацией. Вход
через Telegram Login Widget, доступ только владельцу.

Заложены архитектура, схема БД, домен паспорта, клиент брокера, regex-гейт и
точки входа всех шести процессов. Реально ищет пока один источник: адаптер
`telegram_groups` и ингест — следующие.

Порядок этапов — [spec-v2.md](docs/spec-v2.md#8-этапы).
