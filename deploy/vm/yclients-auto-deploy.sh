#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/yclients_bi_system}"
BRANCH="${BRANCH:-main}"
LOCK_FILE="${LOCK_FILE:-/var/lock/yclients-auto-deploy.lock}"

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  echo "Deploy is already running"
  exit 0
fi

cd "$APP_DIR"

if [ ! -d .git ]; then
  echo "$APP_DIR is not a git repository"
  exit 1
fi

git fetch --quiet origin "$BRANCH"

local_rev="$(git rev-parse HEAD)"
remote_rev="$(git rev-parse "origin/$BRANCH")"

if [ "$local_rev" = "$remote_rev" ]; then
  echo "Already up to date: $local_rev"
  exit 0
fi

echo "Deploying $local_rev -> $remote_rev"
git reset --hard "origin/$BRANCH"

APP_REVISION="$remote_rev" docker compose build api worker migrate
docker compose run --rm migrate
docker compose --profile tools run --rm analytics
docker compose up -d api worker

if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi

health_url="http://127.0.0.1:${API_PORT:-8000}/health"
health_retries="${HEALTH_RETRIES:-30}"
health_interval_seconds="${HEALTH_INTERVAL_SECONDS:-2}"

for attempt in $(seq 1 "$health_retries"); do
  if curl -fsS "$health_url" >/dev/null; then
    echo "API health check passed"
    break
  fi
  if [ "$attempt" -eq "$health_retries" ]; then
    echo "API did not become healthy after $health_retries attempts: $health_url" >&2
    exit 1
  fi
  echo "Waiting for API health ($attempt/$health_retries): $health_url"
  sleep "$health_interval_seconds"
done

if [ -n "${PORTAL_ADMIN_EMAIL:-}" ] && [ -n "${PORTAL_ADMIN_PASSWORD:-}" ]; then
  echo "Ensuring portal super_admin (${PORTAL_ADMIN_EMAIL})..."
  docker compose run --rm --no-deps --entrypoint python api create_portal_admin.py \
    --email "$PORTAL_ADMIN_EMAIL" \
    --password "$PORTAL_ADMIN_PASSWORD" \
    --full-name "${PORTAL_ADMIN_NAME:-Portal Admin}" \
    --assign-all-branches
fi

echo "Deploy completed: $remote_rev"
exit 0
