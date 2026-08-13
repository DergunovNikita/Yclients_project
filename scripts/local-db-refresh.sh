#!/usr/bin/env bash
# Пересобирает локальную БД из боевой. Прод только читается: pg_dump стримится
# по SSH (~26 МБ), локальная копия создаётся заново — поэтому её схема всегда
# ровно та, к которой CI будет применять миграции.
#
# Реквизиты доступа в репозитории не хранятся, их место в .env.local:
#   VM_TARGET=user@host
#   VM_SSH_KEY=~/.ssh/<ключ>
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PG_BIN="${PG_BIN:-/opt/homebrew/opt/postgresql@16/bin}"

for env_file in "$ROOT_DIR/.env" "$ROOT_DIR/.env.local"; do
  if [ -f "$env_file" ]; then
    set -a
    # shellcheck disable=SC1090
    . "$env_file"
    set +a
  fi
done

: "${VM_TARGET:?VM_TARGET не задан — добавьте его в .env.local}"
VM_SSH_KEY="${VM_SSH_KEY:-$HOME/.ssh/id_ed25519}"
VM_PG_CONTAINER="${VM_PG_CONTAINER:-yclients_bi_system-postgres-1}"
DUMP_DIR="${DUMP_DIR:-$HOME/Desktop/projects/yclients_dumps}"
DB_HOST="${DB_HOST:-localhost}"
DB_PORT="${DB_PORT:-5433}"
DB_NAME="${DB_NAME:-yclients_db}"
DB_USER="${DB_USER:-postgres}"

# Скрипт дропает базу, на которую указывает. Если .env вдруг смотрит наружу,
# это должно упасть здесь, а не после DROP DATABASE.
case "$DB_HOST" in
  localhost | 127.0.0.1 | ::1) ;;
  *)
    echo "DB_HOST=$DB_HOST — не локальный адрес, отказываюсь пересоздавать базу" >&2
    exit 1
    ;;
esac

mkdir -p "$DUMP_DIR"
dump="$DUMP_DIR/prod_$(date +%Y%m%d_%H%M).dump"

# Стриминг pg_dump напрямую через SSH приезжал обрезанным: TOC читался, а данные
# обрывались на середине. Поэтому дамп сначала кладётся файлом на VM, копируется
# и сверяется по sha256 — молча битый дамп хуже, чем упавший скрипт.
remote_dump="/tmp/yclients_refresh_$$.dump"
echo "1/3 Снимаю дамп прода -> $dump"
ssh -i "$VM_SSH_KEY" -o BatchMode=yes "$VM_TARGET" \
  "docker exec $VM_PG_CONTAINER pg_dump -U postgres -Fc $DB_NAME > $remote_dump"
remote_sum="$(ssh -i "$VM_SSH_KEY" -o BatchMode=yes "$VM_TARGET" "sha256sum $remote_dump | cut -d' ' -f1")"
scp -q -i "$VM_SSH_KEY" -o BatchMode=yes "$VM_TARGET:$remote_dump" "$dump"
ssh -i "$VM_SSH_KEY" -o BatchMode=yes "$VM_TARGET" "rm -f $remote_dump"

local_sum="$(shasum -a 256 "$dump" | cut -d' ' -f1)"
if [ "$remote_sum" != "$local_sum" ]; then
  echo "Дамп повреждён при копировании (sha256 не совпал), база не тронута" >&2
  exit 1
fi
echo "    $(du -h "$dump" | cut -f1), sha256 совпал"

echo "2/3 Пересоздаю локальную $DB_NAME на порту $DB_PORT"
# --force отцепляет живые сессии: локальный API держит пул открытым,
# и без этого скрипт падал бы каждый раз, когда он запущен.
"$PG_BIN/dropdb" -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" --if-exists --force "$DB_NAME"
"$PG_BIN/createdb" -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" "$DB_NAME"

echo "3/3 Восстанавливаю"
"$PG_BIN/pg_restore" -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" \
  --no-owner --no-privileges "$dump"

"$PG_BIN/psql" -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -tAc "
  SELECT 'схема:       ' || version_num FROM alembic_version;
  SELECT 'размер:      ' || pg_size_pretty(pg_database_size('$DB_NAME'));
  SELECT 'записей:     ' || count(*) FROM appointments;"
echo "Готово."
