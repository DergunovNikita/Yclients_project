# Development Guidelines

## Core Principles
- **KISS** — простота превыше всего
- **DRY** — избегай дублирования
- **YAGNI** — не создавай лишнюю функциональность
- Приоритет: читаемость и понятность кода
- Заложить возможность масштабирования без overengineering

## Project Overview

ETL/BI система: синхронизация данных из YClients API → PostgreSQL → аналитические views для Metabase.

### Stack
- **Python 3.12**, FastAPI, SQLAlchemy 2.0, Alembic, Pydantic 2
- **PostgreSQL 16**, Docker Compose, Metabase
- **Зависимости**: pip + requirements.txt (не uv, не poetry)
- **Тесты**: pytest + httpx (TestClient)

### Structure
Модули в корне; фронтенд в `web/`, служебные скрипты в `scripts/`:
```
# ETL / данные
api.py              — FastAPI endpoints (данные + sync control + CSV export)
sync_pipeline.py    — основной ETL (extract → transform → load)
sync_orchestrator.py — оркестрация: логирование, lock, refresh views
sync_worker.py      — polling worker для очереди задач
sync_control.py     — advisory locks, sync state, run tracking
sync_jobs.py        — CRUD очереди (enqueue/claim/finish)
sync_parsing.py     — парсинг дат, нормализация данных
models.py           — SQLAlchemy модели (public + system schema)
database.py         — connection pooling, миграции
config.py           — переменные окружения (.env) + prod-валидация
setup_analytics.py  — SQL views для Metabase (20+ views)
yclients_api.py     — HTTP-клиент YClients с retry/throttle
seed_fake_data.py   — генератор синтетики; scripts/seed_demo.py — демо-стенд

# Портал (личные кабинеты, multi-tenant)
auth_routes.py      — эндпоинты аутентификации + администрирование пользователей
auth_deps.py        — FastAPI-зависимости: require_auth, роли, forbid_demo (демо read-only)
auth_service.py     — хэш паролей, JWT, email-токены, отправка почты
auth_sessions.py    — refresh-токены, cookie/CSRF, управление сессиями
auth_scope.py       — AccessContext, скоуп доступа по филиалам
onboarding_routes.py — онбординг владельца (источник данных, выбор филиалов)
dashboard_routes.py / dashboard_service.py — API и агрегации дашборда (Chart.js SPA)
web/                — Vite SPA (login, dashboard, admin, i18n ru/en/it)
```

## Code Style & Quality

### Python
- **PEP8** строго, проверять через `ruff check .`
- **Type hints** — где улучшает понимание, не ради формальности
- **Pydantic** — для API request/response schemas
- Максимальная длина строки — 120 символов

### Docstrings
- Английский язык
- Только для: публичных API, сложной логики, неочевидного поведения
- Формат: краткое описание + Args/Returns/Raises при необходимости
- НЕ дублировать информацию из сигнатуры функции

### Comments
- Только при максимальной необходимости
- Объясняй "почему", а не "что"
- Код должен быть self-explanatory

### Logging
- Текущий подход: `print()` + `TeeWriter` (stdout + файл в logs/)
- Логи лаконичные — ошибки, начало/завершение операций, критические решения
- Формат файлов: `sync_{timestamp}_{mode}_{trigger}.log`

## Running

```bash
docker compose up -d                    # postgres, api, worker, metabase
docker compose run --rm migrate         # alembic migrations
docker compose run --rm sync            # разовая синхронизация
docker compose --profile tools run --rm analytics  # обновить views
python -m scripts.seed_demo             # host: провижининг общего read-only демо-стенда (только на демо-базе)
```

- API: http://127.0.0.1:8000
- Metabase: http://127.0.0.1:3000
- PostgreSQL: 127.0.0.1:5432

## Database

- **Database name**: `yclients_db`
- **Schemas**: `public` (данные YClients) + `system` (sync state/jobs/runs + портал: аккаунты, пользователи, сессии, email-токены, филиалы)
- **ORM**: SQLAlchemy 2.0 — sync (`Session`) для ETL, async (`AsyncSession`) для API
- **Batch operations**: chunked по DB_BATCH_SIZE (1000)
- **Concurrency**: pg_advisory_lock, один sync за раз

### Migrations (Alembic)
- Нумерация: `0001_<description>`, `0002_<description>`
- Всегда проверять upgrade и downgrade
- Запуск: `docker compose run --rm migrate` или `python migrate.py`

## Architecture Patterns
- **API (FastAPI)**: полностью async — `AsyncSession` + `asyncpg`, `select()` вместо `db.query()`
- **ETL pipeline**: синхронный — `Session` + `psycopg2`, requests (не async)
- **Два engine**: async для API (`init_async_database`), sync для ETL (`init_database`)
- **Worker queue**: polling sync_jobs таблицы, без Celery/Redis
- **Advisory locks**: предотвращение параллельных sync
- **Multi-tenant портал**: `PortalAccount` = тенант, `PortalUser` с ролями (`platform_admin`, `owner`, `branch_admin`, `manager`, `viewer`); доступ скоупится по филиалам (`company_ids`). Аутентификация email+пароль (не OAuth)
- **API auth**: JWT-сессии в httpOnly-cookie (+ Bearer), refresh-токены с ротацией, CSRF для cookie-запросов. Альтернатива — глобальный `X-API-Key` (full-access) и `X-Sync-Token` для `/sync/*`. Скоуп/роли через `require_auth`/`get_dashboard_access`; демо-аккаунт read-only через `forbid_demo`
- **Prod fail-closed**: в production `config` требует сильные `AUTH_JWT_SECRET`/`SYNC_API_TOKEN`, `AUTH_REQUIRE_LOGIN`, `AUTH_COOKIE_SECURE=true` и выключенную публичную регистрацию (`AUTH_PUBLIC_REGISTRATION_ENABLED=false`)

## Testing

```bash
pytest tests/                           # все тесты
pytest tests/test_sync_parsing.py       # unit без БД
pytest tests/test_api.py                # API тесты (SQLite in-memory)

# Интеграционные (нужен PostgreSQL)
TEST_DATABASE_URL=postgresql+psycopg2://postgres:pass@localhost/test_db \
  pytest tests/test_postgres_integration.py
```

## Security

### Credentials & Secrets
- Все секреты через `.env` — НИКОГДА не коммитить
- `.gitignore` содержит: `.env`, `*.log`, `logs/`
- docker-compose: переменные через `env_file`, не hardcode
- Перед коммитом: убедиться, что нет секретов в diff

### API Authentication
- Модель: email+пароль → JWT в httpOnly-cookie + refresh-токен с ротацией + CSRF (double-submit); роли и скоуп по филиалам
- Дополнительно: глобальный `X-API-Key` (full-access) и `X-Sync-Token` для `/sync/*`
- Публичная регистрация — через `AUTH_PUBLIC_REGISTRATION_ENABLED` (в prod выключена)
- Конкретные значения токенов/ключей/cookie-доменов — только через `.env`; prod-настройки и операционный доступ держать в приватных доках
- Не документировать в публичном репозитории реальные токены, публичные хосты, правила роутинга и обход аутентификации

### SQL Safety
- Все запросы через SQLAlchemy ORM — параметризованные
- Raw SQL только в `setup_analytics.py` (DDL views, без user input)
- Параметры пагинации валидируются через FastAPI `Query(ge=, le=)`
- `/export/csv/{table_name}` — table_name проверяется по whitelist моделей

### Public Repository Hygiene
- Do not commit production hosts, IP addresses, DNS, routing diagrams or deployment runbooks
- Do not commit real Google Sheets IDs or URLs with business data
- Keep SSH, systemd, nginx and CI/CD operational details in private documentation

### Pre-commit Checks
- `ruff check .` — линтер
- CI запускает `gitleaks` с `.gitleaks.toml` для поиска секретов, production IP в `VM_HOST` / `VM_API_ORIGIN` и опубликованных Google Sheets URL
- Локально, если установлен `gitleaks`: `gitleaks git --config .gitleaks.toml --redact .`

## Common Patterns

### Добавление нового endpoint
1. Модель в `models.py` (SQLAlchemy)
2. Endpoint в `api.py` с `page_params`, `fetch_page`, `serialize_rows`
3. Тест в `tests/test_api.py`
4. Портальные/дашборд-эндпоинты — в `auth_routes.py` / `dashboard_routes.py` / `onboarding_routes.py` с зависимостями доступа (`get_dashboard_access`, роли); на мутации навешивать `forbid_demo`, чтобы демо оставался read-only

### Добавление нового шага синхронизации
1. Метод API в `yclients_api.py`
2. Функция парсинга в `sync_parsing.py` (если нужно)
3. Функция `sync_<entity>()` в `sync_pipeline.py`
4. Вызов в `execute_sync()` с обработкой ошибок

### Добавление новой аналитической view
1. SQL view в `setup_analytics.py` → `refresh_analytics_views()`
2. `CREATE OR REPLACE VIEW` паттерн
3. Запуск: `docker compose --profile tools run --rm analytics`
