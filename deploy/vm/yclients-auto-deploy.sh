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

# The timer fires every 5 minutes. Rebuilding on every tick regardless of whether
# anything changed grew the build cache to 12.78GB and restarted api/worker ~288
# times a day. CI reaches this script through `systemctl start`, and at that point
# the VM has not pulled yet, so a real deploy still passes the check below.
if [ "$local_rev" = "$remote_rev" ] && [ "${FORCE_DEPLOY:-false}" != "true" ]; then
  echo "Repository already at $local_rev; nothing to deploy"
  exit 0
fi

if [ "$local_rev" = "$remote_rev" ]; then
  echo "FORCE_DEPLOY is set; rebuilding $local_rev"
else
  echo "Deploying $local_rev -> $remote_rev"
  git reset --hard "origin/$BRANCH"
fi

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
    break
  fi
  if [ "$attempt" -eq "$health_retries" ]; then
    echo "API did not become healthy after $health_retries attempts: $health_url" >&2
    exit 1
  fi
  echo "Waiting for API health ($attempt/$health_retries): $health_url"
  sleep "$health_interval_seconds"
done

ensure_portal_admin() {
  local email="$1"
  local full_name="$2"
  if [ -z "$email" ]; then
    return 0
  fi

  echo "Ensuring portal platform_admin (${email})..."
  docker compose run --rm --no-deps --entrypoint python api create_portal_admin.py \
    --email "$email" \
    --password "$PORTAL_ADMIN_PASSWORD" \
    --full-name "$full_name"
}

if [ -n "${PORTAL_ADMIN_PASSWORD:-}" ]; then
  ensure_portal_admin "${PORTAL_ADMIN_EMAIL:-}" "${PORTAL_ADMIN_NAME:-Portal Admin}"
  if [ "${PORTAL_CREATE_DERGUNOV_ADMIN:-true}" = "true" ]; then
    ensure_portal_admin \
      "${PORTAL_DERGUNOV_ADMIN_EMAIL:-dergunovnt@yandex.ru}" \
      "${PORTAL_DERGUNOV_ADMIN_NAME:-Nikita Super Admin}"
  fi
  if [ -n "${PORTAL_EXTRA_ADMIN_EMAILS:-}" ]; then
    IFS=',' read -ra extra_admin_emails <<< "$PORTAL_EXTRA_ADMIN_EMAILS"
    for extra_admin_email in "${extra_admin_emails[@]}"; do
      extra_admin_email="${extra_admin_email#"${extra_admin_email%%[![:space:]]*}"}"
      extra_admin_email="${extra_admin_email%"${extra_admin_email##*[![:space:]]}"}"
      ensure_portal_admin "$extra_admin_email" "${PORTAL_EXTRA_ADMIN_NAME:-Portal Admin}"
    done
  fi
fi

echo "Deploy completed: $remote_rev"
exit 0
