# YClients BI System

Мультитенантный аналитический сервис: синхронизирует данные YClients в PostgreSQL, отдаёт портал и продуктовые отчёты через FastAPI и обслуживает Vite-интерфейс.

## Формулы среднего чека

- Общий: положительные оплаты услуг завершённых визитов, товаров и пополнений личных счетов, делённые на сумму уникальных клиентов завершённых визитов, завершённых записей без клиента и уникальных товарных документов.
- По услугам: оплаченные услуги / завершённые записи.
- По товарам: оплаченные товары / количество проданных товарных единиц.
- По допуслугам: оплаченные допуслуги / количество оказанных допуслуг.

Доход общего среднего чека относится к дате платежа, клиентская часть знаменателя — к дате визита. До подтверждённой синхронизации детализированных финансовых транзакций API возвращает `average_check.source_status = "partial"`.

## Контракты dashboard

- В обзоре `visit_metrics.opz_qty` показывает количество ОПЗ, а `opz_pct` — долю от завершённых визитов. При одинаковых фильтрах итоговые `opz_qty` и `opz_pct` совпадают с фактом ОПЗ в «План/факт»; наличие плана у отдельных сотрудников не влияет на итог филиала или сети. Факт выбранного сотрудника также совпадает с его строкой в полном списке филиала: распределение ОПЗ между администраторами выполняется до применения фильтра сотрудника. Исключение — фильтр по администратору: расчётные ОПЗ в обзоре отнесены к барберу визита, а ручная добавка при фильтре по сотруднику не подмешивается ни в обзоре, ни в годовых фактах отчётов, поэтому строку администратора целиком показывает только «План/факт». Дневные поля `opz_qty` и `opz_pct` доступны в `revenue_daily`.
- Дополнительные ОПЗ (`POST /dashboard/plan/opz_fact`) вводятся администраторам по месяцу и прибавляются к расчётным ОПЗ в обзоре, План/факте и годовых фактах отчётов; дневные `opz_qty`/`opz_pct` в `revenue_daily` остаются чисто расчётными, поэтому сумма дневного графика меньше плитки обзора на величину добавок. Месячная добавка засчитывается в периоде, который покрывает месяц целиком, либо доходит до сегодня, начинаясь не позже 1-го числа текущего месяца — одинаково в обзоре и в План/факте; прошедшая неделя и день в середине месяца идут по чистому расчёту.
- Ручные факты (отзывы, дополнительные ОПЗ) привязаны к календарному месяцу: одна строка на месяц, филиал, сотрудника и метрику. Чтение раздвигает период до границ месяцев; подробности — в `AGENTS.md`.
- Процентные настройки планов API принимает и возвращает в диапазоне `1–100`; в PostgreSQL они продолжают храниться как доли `0–1`.
- Изменения меток услуг («дополнительная услуга») сохраняются атомарно через `PATCH /dashboard/services`: при ошибке любого элемента весь пакет откатывается.

## Что входит в проект

- `api.py` - FastAPI-приложение: подключает роутеры портала и дашборда, health-пробы, постановка sync в очередь и статус
- `auth_*.py` - аутентификация, сессии, роли и скоуп доступа по филиалам
- `onboarding_routes.py` / `yclients_credentials.py` - онбординг и зашифрованные credentials YClients
- `sync_pipeline.py` - ETL pipeline YClients -> PostgreSQL
- `sync_worker.py` - worker, который обрабатывает queued sync jobs
- `main.py` - ручной CLI-запуск синхронизации
- `migrate.py` - применение Alembic миграций
- `dashboard_service.py` / `dashboard_routes.py` / `dashboard_reports.py` — агрегаты и отчёты продуктового дашборда (JSON)
- `web/` - Vite MPA: дашборд, отчёты, auth, onboarding, settings и admin
- `deploy/nginx/` - nginx на VM: таблица маршрутов API (`routes.mjs`), из неё `scripts/nginx-allowlist.mjs` генерирует `api-allowlist.conf`; `static-headers.conf` - заголовки безопасности статики
- `docker-compose.yml` - запуск `api`, `worker` и PostgreSQL (на VM); локальная разработка идёт без Docker
- `sync.sh` - ручной запуск one-shot sync через Docker Compose

## Стек

- Python
- FastAPI
- SQLAlchemy
- Alembic
- PostgreSQL
- Docker Compose

## Дашборд и импорт

Каталог [`web/`](web/) содержит Vite MPA: основной Chart.js dashboard/reports и отдельные страницы auth, onboarding, settings и admin. В локальной разработке Vite проксирует запросы в FastAPI; production-сборка раздаётся nginx на VM и ходит в API тем же origin под `/api`.


## Быстрый старт

Ниже — запуск через Docker Compose. Повседневная локальная разработка идёт на полной копии
production-БД (`./scripts/local-db-refresh.sh`, PostgreSQL на `127.0.0.1:5433`, без Docker) —
порядок описан в `AGENTS.md`, раздел «Локальная разработка на копии прода».

### 1. Подготовить окружение

```bash
cp .env.example .env
```

Заполните минимум:

- `APP_ENV=local` для локального запуска
- `DB_PASSWORD`
- `SYNC_API_TOKEN`
- стабильный `AUTH_JWT_SECRET` длиной не менее 32 символов
- стабильный `PORTAL_CREDENTIALS_ENCRYPTION_KEY` длиной не менее 32 символов
- `AUTH_COOKIE_SECURE=false` и `AUTH_CONSOLE_EMAIL=true` для локального HTTP без SMTP

Учётные данные YClients в `.env` не задаются: они сохраняются в зашифрованном виде через onboarding/admin.

### 2. Применить миграции

```bash
docker compose run --rm migrate
```

### 3. Поднять сервисы

```bash
docker compose up -d postgres api worker
```

После запуска будут доступны:

- API: `http://127.0.0.1:8000`

### 4. Запустить frontend

```bash
source scripts/dev-env.sh
cd web
npm ci
npm run dev
```

Frontend будет доступен на `http://127.0.0.1:5173`. Зарегистрируйте владельца через `register.html`; при локальном `AUTH_CONSOLE_EMAIL=true` ссылка подтверждения появится в логах API. Затем onboarding сохранит credentials, предложит выбрать филиалы и поставит начальную синхронизацию в очередь.

## Полезные команды

Проверка контейнеров:

```bash
docker compose ps
```

Ручной one-shot sync:

```bash
./sync.sh incremental manual cli
./sync.sh refresh manual cli      # перечитать последние SYNC_REFRESH_DAYS с очисткой окна
./sync.sh full manual cli         # то же по всей истории
```

Логи worker:

```bash
docker compose logs -f worker
```

Smoke check local API:

```bash
curl http://127.0.0.1:8000/health
```

## Демо-режим

Общий read-only демо-стенд с синтетическими данными — чтобы показывать дашборд
потенциальным клиентам без реальных данных YClients. Кнопка «View demo» на странице
входа делает беспарольный вход (`POST /auth/demo-login`) и открывает дашборд с
демо-баннером; любой запрос на запись под демо-сессией возвращает `403`. Если стенд
не провижинен, `/auth/demo-login` отвечает `503` — кнопка показывает ошибку.

Демо живёт в **основной базе как отдельный portal tenant**:
`PortalAccount(is_demo=True)` с синтетическими филиалами `source_type='demo'`.
Это не отдельный сайт и не отдельная БД.

### Запуск в основной БД

Создать или переиспользовать встроенный read-only демо-тенант:

```bash
python -m scripts.seed_demo
```

Скрипт можно запускать рядом с реальными tenant'ами: он создаёт только demo account,
demo owner и demo companies. Реальные компании не удаляются и не перезаписываются.

### Что создаётся

- N синтетических компаний с `source_type='demo'` и активностью (клиенты, услуги, записи, продажи);
- один `PortalAccount(is_demo=True)` и по одному `PortalBranch` на компанию;
- беспарольный демо-владелец `demo@portal.local` (`is_demo=True`).

Флаги `seed_demo`: `--companies`, `--days`, `--seed`, `--clients-per-company`,
`--staff-per-company`, `--goods-per-company`,
`--appointments-per-day-min/--appointments-per-day-max`.
Скрипт идемпотентен: повторный запуск переиспользует стенд и **не** перегенерирует данные.

### Свежие даты (пересев)

`seed_demo` при наличии демо-данных пропускает генерацию, поэтому повторный запуск
**не** сдвигает даты. Чтобы обновить, удалите старые demo data в отдельной
maintenance-задаче и затем снова выполните `python -m scripts.seed_demo`.

## Тесты

Локальное окружение проекта уже содержит Python tooling и Node.js в `.venv/bin`
(dev-инструменты — `pytest`, `ruff` и т. д. — ставятся из `requirements-dev.txt`;
`requirements.txt` — только то, что нужно рантайму и Docker-образу).
Для ручных команд в текущем shell:

```bash
source scripts/dev-env.sh
node --version
npm --version
pytest --version
```

Быстрый гейт перед коммитом (ruff + syntax + fast pytest + gitleaks):

```bash
./scripts/preflight.sh
```

Security checks для CI и ручного triage:

```bash
./scripts/security-check.sh
./scripts/security-check.sh --strict
```

В `.github/workflows/deploy-vm.yml` перед деплоем обязательно проходят `ruff check .`, `pytest`
и frontend-проверки (`npm test`, `scan:auth-tokens`, `build`, `scan:auth-build`).

В CI отдельный workflow `.github/workflows/security.yml` запускает:

- `gitleaks` остаётся в deploy workflow как full-history secrets gate;
- `pip-audit` и `npm audit --audit-level=high` делают коммит красным при известных уязвимостях, но деплой не блокируют:
  `deploy-vm.yml` от `security.yml` не зависит;
- `Semgrep` и `Checkov` пока работают как report/SARIF checks для triage, чтобы не блокировать deploy на существующих IaC findings без явного suppressions review.

`pip-audit` падает на любой известной Python vulnerability: у Python advisory
severity доступна не всегда, поэтому high/critical threshold применяется явно
только к `npm audit`.

Frontend audit — штатный `npm audit --audit-level=high`: любой high/critical в зависимостях
(включая dev) делает проверку красной. Allowlist исключений не ведётся; если понадобится первая suppression,
добавлять её отдельным решением после risk review.

Локальный `./scripts/security-check.sh --strict` делает Semgrep/Checkov blocking намеренно:
это режим для ручной подготовки к ужесточению CI после triage.

Краткий разбор последнего sync-лога (top slowest шаги + ошибки):

```bash
python scripts/sync-log-summary.py            # последний лог
python scripts/sync-log-summary.py --last 5   # сводка по последним 5 запускам
```

Постановка sync-джобы без ручного curl (авто-читает `.env`):

```bash
python scripts/enqueue-sync.py --tenant 1 --mode incremental
python scripts/enqueue-sync.py --tenant 1 --mode refresh          # перечитать последние SYNC_REFRESH_DAYS
python scripts/enqueue-sync.py --global --mode full --dry-run   # печатает curl
python scripts/enqueue-sync.py --status                          # текущий статус
```

Полная проверка backend + frontend build:

```bash
./scripts/check.sh
```

Локальные unit/API тесты:

```bash
./scripts/check.sh tests/test_sync_parsing.py tests/test_api.py tests/test_dashboard_api.py
```

Postgres integration tests:

```bash
TEST_DATABASE_URL=postgresql+psycopg2://postgres:changeme@127.0.0.1:5432/yclients_test \
./scripts/check.sh tests/test_postgres_integration.py
```

## Примечания

- предметные BI-таблицы пересобираются миграциями и последующим full sync
- `system.*` таблицы состояния и истории запусков сохраняются отдельно
- production deployment details intentionally live outside this public repository
