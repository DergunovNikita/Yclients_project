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
    echo "Deploy completed: $remote_rev"
    exit 0
  fi
  echo "Waiting for API health ($attempt/$health_retries): $health_url"
  sleep "$health_interval_seconds"
done

echo "API did not become healthy after $health_retries attempts: $health_url" >&2
exit 1
