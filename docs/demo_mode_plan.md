# План реализации: Демо-режим («Посмотреть демо»)

> **Статус:** завершённый исторический план реализации. Текущий контракт демо-режима и команды запуска описаны в `README.md`; актуальные проверки находятся в `tests/test_auth.py` и `tests/test_seed_demo.py`.

## Зафиксированные решения
- **Строго read-only**: демо видит дашборды; все мутации/sync/admin блокируются на бэкенде guard'ом.
- **Один общий тенант**: все посетители делят один `PortalAccount` + синтетику. Провижининга на визит нет.
- **Беспарольный вход**: кнопка → `POST /auth/demo-login` → штатная сессия (cookie+JWT+CSRF). Основной auth-путь не меняется.
- **Демо-юзер**: роль `owner`, флаг `is_demo=True`, email `demo@portal.local` (почта не шлётся — `is_deliverable_portal_email`).
- **Изоляция данных**: та же БД, демо-компании помечаются `source_type='demo'` (поле есть с миграции 0029). Владельцы демо не видят (скоуп по филиалам). Глобальные views / platform_admin / api-key видят — приемлемо для демо-стенда.
- **Историческая ветка реализации**: `feature/demo-mode`.

---

## Блок A — БД: флаг is_demo (миграция 0031)
**Файлы:** `alembic/versions/0031_demo_flag.py`, `models.py`
- `portal_users.is_demo BOOLEAN NOT NULL DEFAULT false` (+ server_default).
- `portal_accounts.is_demo BOOLEAN NOT NULL DEFAULT false` (+ server_default).
- В `models.py`: поля `is_demo` в `PortalUser` и `PortalAccount`.
- Проверить upgrade И downgrade (drop columns).
**Приёмка:** `migrate` проходит вверх и вниз; поля в схеме `system`.
**Зависимости:** нет. Блокирует B, C, D.

---

## Блок B — Провижининг демо (`scripts/seed_demo.py`)
**Файлы:** `scripts/seed_demo.py`
Идемпотентный скрипт, **вызывает `seed_companies()`+`seed_activity()` напрямую** (не `main()` — тот отказывается на непустой БД, а `--wipe` сносит всё):
1. `seed_companies()` → `refs`; извлечь `company.id` созданных филиалов.
2. Проставить этим компаниям `source_type='demo'`.
3. `seed_activity()` для наполнения.
4. `PortalAccount(label='Demo', is_demo=True)` — get-or-create.
5. `PortalBranch(portal_account_id, company_id)` для каждого демо-`company_id`.
6. `PortalUser(email='demo@portal.local', role='owner', is_demo=True, portal_account_id, is_active=True, email_verified_at=now)`, `password_hash=hash_password(generate_bootstrap_password())`.
7. Идемпотентно: повторный запуск не плодит дубли (по email / label / уже связанным company_id).
**Приёмка:** один демо-account, N branches, один демо-owner; повторный запуск не меняет счётчики; демо-компании `source_type='demo'`.
**Зависимости:** A.

---

## Блок C — Бэкенд: беспарольный вход
**Файлы:** `auth_routes.py`, `auth_deps.py`
- `OPEN_PATH_PREFIXES`: `+ '/auth/demo-login'`, `+ '/dashboard/auth/demo-login'`.
- `POST /demo-login` (без body): найти `PortalUser(is_demo=True, role='owner', is_active=True)` (первый по id); 503 если демо не засеян → `issue_session` → `set_auth_cookies` → payload как у `login`.
- `_user_payload`: добавить поле `is_demo` (нужно фронту).
- Мягкий rate-limit по IP (in-memory) — защита от массовой выдачи сессий; допустимо опустить в v1.
**Приёмка:** `POST /auth/demo-login` без заголовков → 200 + cookie; `GET /auth/me` → демо-юзер с `is_demo=true`; дашборды скоупятся на демо-филиалы.
**Зависимости:** A (B — для ручной проверки).

---

## Блок D — Бэкенд: read-only guard
**Файлы:** `auth_deps.py` + роуты в `api.py`, `dashboard_routes.py`, `onboarding_routes.py`, `auth_routes.py`
- `forbid_demo(user=Depends(get_current_user))` → `403 'Demo is read-only'` если `user.is_demo`.
- Навесить на все мутации:
  - `api.py`: `POST /sync/trigger` (+ прочие sync-контролы, если есть).
  - `dashboard_routes.py`: `POST /services/kpi_groups`, `/plan/settings`, `/plan/reviews_fact`.
  - `onboarding_routes.py`: `POST /credentials`, `/branches`.
  - `auth_routes.py`: `change-password`, `logout-all`, все `admin/*`, `yclients-credentials/*`.
- GET-дашборды НЕ трогаем.
**Приёмка:** под демо-сессией мутирующие/sync/admin → 403; GET-дашборды → 200.
**Зависимости:** A. Параллелится с C.

---

## Блок E — Frontend
**Файлы:** `web/login.html`, `web/src/login.js` (+ шелл дашборда, `web/locales/*`)
- Кнопка «Посмотреть демо» под формой → `POST /auth/demo-login` → редирект на `/` (не `/onboarding.html`).
- Баннер «Демо-режим · данные вымышленные» когда `user.is_demo`.
- Скрыть/задизейблить write-действия по `is_demo` (бэкенд страхует).
- i18n-строки.
**Приёмка:** из инкогнито клик открывает дашборд с демо-данными и баннером; write-кнопок нет.
**Зависимости:** C (поле `is_demo` в payload).

---

## Блок F — Тесты
**Файлы:** проверки реализованы в `tests/test_auth.py` и `tests/test_seed_demo.py`.
- `demo-login` выдаёт сессию без креденшелов.
- Демо-скоуп: только демо-филиалы.
- Guard: мутации/sync/admin → 403 под демо.
- `seed_demo` идемпотентен (интеграционный, если есть PG-фикстура).
**Приёмка:** `pytest tests/` зелёный.
**Зависимости:** B, C, D.

---

## Блок G — Ops / доки
**Файлы:** `.env.example` (+ `.env` mirror!), `README.md`, опц. cron
- Конфиг-флаги при необходимости.
- Опциональный еженедельный переген демо-тенанта (свежие даты).
- README: как засеять/включить/выключить демо.
**Приёмка:** доки описывают запуск демо; `.env.example` и `.env` синхронны.
**Зависимости:** все.

---

## Граф зависимостей / волны
```
A ─┬─> B ─┐
   ├─> C ─┼─> E
   └─> D ─┴─> F ─> G
```
- Волна 1: A · Волна 2: B, C, D (парал.) · Волна 3: E, F · Волна 4: G

## Протокол исполнения (на каждый блок)
1. **Pre-task** саб-агент (Opus, extra-high): подводные камни, edge-cases, проблемные места.
2. Реализация.
3. **Code-review**: 2 саб-агента (Opus, extra-high) параллельно — оценка 1–10 + список правок.
4. Цикл ревью, пока хоть один не даст ≥ 9.5/10.
5. Внести согласованные правки → commit → задача done → следующая.
