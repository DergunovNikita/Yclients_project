#!/usr/bin/env bash
# Run on the VM after portal-auth code is deployed (git pull + docker compose build).
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/yclients_bi_system}"
cd "$APP_DIR"

if [ ! -f .env ]; then
  echo "Missing $APP_DIR/.env" >&2
  exit 1
fi

set -a
# shellcheck disable=SC1091
. ./.env
set +a

echo "Building API image and applying migrations..."
docker compose build api worker migrate
docker compose run --rm migrate
docker compose up -d api worker

health_url="http://127.0.0.1:${API_PORT:-8000}/health"
for attempt in $(seq 1 30); do
  if curl -fsS "$health_url" >/dev/null; then
    echo "API healthy"
    break
  fi
  if [ "$attempt" -eq 30 ]; then
    echo "API health check failed: $health_url" >&2
    exit 1
  fi
  sleep 2
done

if [ -n "${PORTAL_ADMIN_EMAIL:-}" ] && [ -n "${PORTAL_ADMIN_PASSWORD:-}" ]; then
  echo "Ensuring portal super_admin (${PORTAL_ADMIN_EMAIL})..."
  docker compose run --rm --no-deps --entrypoint python api create_portal_admin.py \
    --email "$PORTAL_ADMIN_EMAIL" \
    --password "$PORTAL_ADMIN_PASSWORD" \
    --full-name "${PORTAL_ADMIN_NAME:-Portal Admin}" \
    --assign-all-branches
fi

if [ "${PORTAL_PROVISION_STAFF:-false}" = "true" ]; then
  echo "Provisioning staff portal accounts..."
  docker compose run --rm --no-deps --entrypoint python api scripts/provision_staff_accounts.py
fi

echo "Portal backend deploy finished."
