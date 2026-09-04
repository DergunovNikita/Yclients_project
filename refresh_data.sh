#!/usr/bin/env bash

set -euo pipefail

MODE="${1:-incremental}"
TRIGGER="${2:-manual}"
INITIATOR="${3:-cli}"

case "$MODE" in
  incremental|refresh|full) ;;
  *)
    echo "Неверный режим sync: $MODE" >&2
    echo "Использование: $0 [incremental|refresh|full] [manual|scheduled] [initiator]" >&2
    exit 64
    ;;
esac

case "$TRIGGER" in
  manual|scheduled) ;;
  *)
    echo "Неверный trigger: $TRIGGER" >&2
    echo "Использование: $0 [incremental|refresh|full] [manual|scheduled] [initiator]" >&2
    exit 64
    ;;
esac

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

DOCKER_BIN="${DOCKER_BIN:-docker}"
if ! command -v "$DOCKER_BIN" >/dev/null 2>&1; then
  if [ -x "/Applications/Docker.app/Contents/Resources/bin/docker" ]; then
    DOCKER_BIN="/Applications/Docker.app/Contents/Resources/bin/docker"
  else
    echo "Docker CLI не найден. Установите Docker и/или добавьте docker в PATH." >&2
    exit 127
  fi
fi

echo "==> Поднимаю PostgreSQL"
"$DOCKER_BIN" compose up -d postgres

echo "==> Применяю миграции"
"$DOCKER_BIN" compose run --rm migrate

echo "==> Запускаю синхронизацию: mode=$MODE trigger=$TRIGGER initiator=$INITIATOR"
"$DOCKER_BIN" compose run --rm sync \
  --mode "$MODE" \
  --trigger "$TRIGGER" \
  --initiator "$INITIATOR"

echo "==> Список таблиц"
"$DOCKER_BIN" compose exec -T postgres sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "\dt"'

echo "==> Список views"
"$DOCKER_BIN" compose exec -T postgres sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "\dv"'
