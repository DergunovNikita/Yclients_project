#!/usr/bin/env bash
# Docker-first запуск синхронизации данных YClients -> PostgreSQL
# Запуск вручную:  ./sync.sh incremental manual cli

set -euo pipefail

MODE="${1:-incremental}"
TRIGGER="${2:-manual}"
INITIATOR="${3:-shell}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

cd "$SCRIPT_DIR"

exec docker compose run --rm sync \
  --mode "$MODE" \
  --trigger "$TRIGGER" \
  --initiator "$INITIATOR"
