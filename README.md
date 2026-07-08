# YClients BI System

Сервис синхронизации данных YClients в PostgreSQL, публикации BI-таблиц через FastAPI и подготовки аналитических `views` для Metabase.

## Формулы среднего чека

- Общий: положительные оплаты услуг завершённых визитов, товаров и пополнений личных счетов, делённые на сумму уникальных клиентов завершённых визитов, завершённых записей без клиента и уникальных товарных документов.
- По услугам: оплаченные услуги / завершённые записи.
- По товарам: оплаченные товары / количество проданных товарных единиц.
- По допуслугам: оплаченные допуслуги / количество оказанных допуслуг.

Доход общего среднего чека относится к дате платежа, клиентская часть знаменателя — к дате визита. До подтверждённой синхронизации детализированных финансовых транзакций API возвращает `average_check.source_status = "partial"`.

Для Metabase используется `v_average_check_components`: за выбранный период показатель считается как `SUM(revenue) / COUNT(DISTINCT denominator_key)`, исключая компонент `unclassified_income` из числителя.

## Контракты dashboard

- В обзоре `visit_metrics.opz_qty` показывает количество ОПЗ, а `opz_pct` — долю от завершённых визитов. При одинаковых фильтрах итоговые `opz_qty` и `opz_pct` совпадают с фактом ОПЗ в «План/факт»; наличие плана у отдельных сотрудников не влияет на итог филиала или сети. Факт выбранного сотрудника также совпадает с его строкой в полном списке филиала: распределение ОПЗ между администраторами выполняется до применения фильтра сотрудника. Дневные поля `opz_qty` и `opz_pct` доступны в `revenue_daily`.
- Ручной факт отзывов хранится одним итогом на администратора и выбранный период. Старые дневные строки суммируются при чтении и заменяются итоговой строкой при сохранении.
- Процентные настройки планов API принимает и возвращает в диапазоне `1–100`; в PostgreSQL они продолжают храниться как доли `0–1`.

## Что входит в проект

- `api.py` - FastAPI API для чтения данных, постановки sync в очередь и просмотра статуса
- `sync_pipeline.py` - ETL pipeline YClients -> PostgreSQL
- `sync_worker.py` - worker, который обрабатывает queued sync jobs
- `main.py` - ручной CLI-запуск синхронизации
- `migrate.py` - применение Alembic миграций
- `setup_analytics.py` - создание аналитических `views` в PostgreSQL
- `dashboard_service.py` / `dashboard_routes.py` — агрегаты для продуктового дашборда (JSON, без Metabase)
- `docker-compose.yml` - локальный запуск `api`, `worker`, PostgreSQL и Metabase
- `sync.sh` - ручной запуск one-shot sync через Docker Compose

## Стек

- Python
- FastAPI
- SQLAlchemy
- Alembic
- PostgreSQL
- Docker Compose
- Metabase (опционально, для внутренней BI; клиентский дашборд — через `web/` + `/dashboard/*`)

## Дашборд и импорт

Каталог [`web/`](web/) содержит Vite + Chart.js frontend для локальной разработки и сборки статического интерфейса. Backend отдает JSON-данные через FastAPI.


## Быстрый старт

### 1. Подготовить окружение

```bash
cp .env.example .env
```

Заполните минимум:

- `PARTNER_TOKEN`
- `YCLIENTS_LOGIN`
- `YCLIENTS_PASSWORD`
- `DB_PASSWORD`
- `SYNC_API_TOKEN`

### 2. Применить миграции

```bash
docker compose run --rm migrate
```

### 3. Поднять сервисы

```bash
docker compose up -d postgres api worker metabase
```

После запуска будут доступны:

- API: `http://127.0.0.1:8000`
- Metabase: `http://127.0.0.1:3000`

### 4. Пересоздать аналитические представления

```bash
docker compose run --rm analytics
```

## Полезные команды

Проверка контейнеров:

```bash
docker compose ps
```

Ручной one-shot sync:

```bash
./sync.sh incremental manual cli
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

Демо живёт в **отдельной, выделенной под демо базе**: `seed_demo` намеренно отказывается
работать, если в базе есть хоть одна не-демо компания (иначе id-схема генератора
переполняет int32 на реальных id, а демо-данные попадают в боевую аналитику). Поэтому
рядом с продом демо разворачивают **отдельным стеком**, а не в боевой БД.

### Отдельный демо-инстанс

Изолированный стек (свой Postgres + API) из `docker-compose.demo.yml`:

```bash
cp .env.demo.example .env.demo   # затем задать секреты внутри (.env.demo в .gitignore)
docker compose -f docker-compose.demo.yml --env-file .env.demo run --rm bootstrap-db
docker compose -f docker-compose.demo.yml --env-file .env.demo run --rm seed-demo
docker compose -f docker-compose.demo.yml --env-file .env.demo up -d api
```

Демо-API поднимется на `127.0.0.1:${DEMO_API_PORT:-8001}`. Направьте демо-SPA
(`VITE_API_BASE`) или кнопку «View demo» на этот origin.

- `bootstrap-db` создаёт текущую схему на пустой БД и делает Alembic `stamp head`
  (обычный `alembic upgrade head` с нуля здесь не проходит: baseline `0001` строит
  таблицы по текущим моделям, и поздние `add_column`-миграции конфликтуют).
- `seed-demo` создаёт демо-тенант и обновляет аналитические views.

### Что создаётся

- N синтетических компаний с `source_type='demo'` и активностью (клиенты, услуги, записи, продажи);
- один `PortalAccount(is_demo=True)` и по одному `PortalBranch` на компанию;
- беспарольный демо-владелец `demo@portal.local` (`is_demo=True`).

Флаги `seed_demo`: `--companies`, `--days`, `--seed`, `--clients-per-company`,
`--staff-per-company`, `--goods-per-company`,
`--appointments-per-day-min/--appointments-per-day-max`, `--skip-refresh-views`.
Скрипт идемпотентен: повторный запуск переиспользует стенд и **не** перегенерирует данные.

### Локально (одна dev-БД)

На пустой локальной БД без Docker:

```bash
python -m scripts.bootstrap_db          # схема + stamp head (для свежей БД)
python -m scripts.seed_demo             # демо-тенант + views
```

### Свежие даты (пересев)

`seed_demo` при наличии демо-данных пропускает генерацию, поэтому повторный запуск
**не** сдвигает даты. Чтобы обновить — пересоздайте демо-БД и пересейте:

```bash
docker compose -f docker-compose.demo.yml --env-file .env.demo down -v
docker compose -f docker-compose.demo.yml --env-file .env.demo run --rm bootstrap-db
docker compose -f docker-compose.demo.yml --env-file .env.demo run --rm seed-demo
docker compose -f docker-compose.demo.yml --env-file .env.demo up -d api
```

## Тесты

Локальное окружение проекта уже содержит Python tooling и Node.js в `.venv/bin`.
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

Краткий разбор последнего sync-лога (top slowest шаги + ошибки):

```bash
python scripts/sync-log-summary.py            # последний лог
python scripts/sync-log-summary.py --last 5   # сводка по последним 5 запускам
```

Постановка sync-джобы без ручного curl (авто-читает `.env`):

```bash
python scripts/enqueue-sync.py --tenant 1 --mode incremental
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
