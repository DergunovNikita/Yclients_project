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

# `.env` is written for docker compose's dotenv parser, which accepts values the shell does not
# (`SMTP_FROM=Portal <no-reply@x.com>`, unquoted spaces, `$word`). Sourcing it here would abort a
# deploy after the containers were already recreated, so the few keys needed are read one by one.
env_value() {
  [ -f .env ] || return 0
  local value
  # tr: a CRLF file would leave `\r` after a closing quote, and the quoted branch below would no longer match.
  value="$(sed -n "s/^$1=//p" .env | tail -n 1 | tr -d '\r')"
  case "$value" in
    \"*\") value="${value#\"}"; value="${value%\"}" ;;
    \'*\') value="${value#\'}"; value="${value%\'}" ;;
    *) value="${value%%[[:space:]]#*}"; value="${value%"${value##*[![:space:]]}"}" ;;
  esac
  printf '%s\n' "$value"
}

# Readiness, not liveness: the async engine is lazy, so /health answers 200 even when
# the database is unreachable and every dashboard route returns 500.
wait_for_api_ready() {
  local api_port="${API_PORT:-$(env_value API_PORT)}"
  local health_url="http://127.0.0.1:${api_port:-8000}/health/ready"
  local health_retries="${HEALTH_RETRIES:-$(env_value HEALTH_RETRIES)}"
  local health_interval_seconds="${HEALTH_INTERVAL_SECONDS:-$(env_value HEALTH_INTERVAL_SECONDS)}"
  health_retries="${health_retries:-30}"
  health_interval_seconds="${health_interval_seconds:-2}"

  for attempt in $(seq 1 "$health_retries"); do
    if curl -fsS "$health_url" >/dev/null; then
      echo "API health check passed"
      return 0
    fi
    if [ "$attempt" -eq "$health_retries" ]; then
      echo "API did not become healthy after $health_retries attempts: $health_url" >&2
      return 1
    fi
    echo "Waiting for API health ($attempt/$health_retries): $health_url"
    sleep "$health_interval_seconds"
  done
}

git fetch --quiet origin "$BRANCH"

local_rev="$(git rev-parse HEAD)"
remote_rev="$(git rev-parse "origin/$BRANCH")"

# What the containers actually serve, not what is checked out. The reset below happens
# before the build and the migration, so a deploy that dies in between leaves the tree
# at the target revision while api/worker still run the old image. Comparing the tree
# would then read "nothing to deploy" on every following tick and exit 0 forever, with
# the failure invisible and recoverable only by hand.
deployed_rev="$(docker compose exec -T api cat /app/REVISION 2>/dev/null | tr -d '[:space:]' || true)"

# The timer fires every 5 minutes. Rebuilding on every tick regardless of whether
# anything changed grew the build cache to 12.78GB and restarted api/worker ~288
# times a day. The revision is baked into the image, so a tick that wins the race against
# CI may have deployed it already and then failed the readiness probe: the revision reads as
# served from then on. Hence the probe runs here too, instead of reporting a broken API as
# "nothing to deploy" with exit 0 (and a green CI run).
if [ "$deployed_rev" = "$remote_rev" ] && [ "${FORCE_DEPLOY:-false}" != "true" ]; then
  echo "Already serving $remote_rev; checking it is healthy"
  wait_for_api_ready
  echo "Nothing to deploy"
  exit 0
fi

if [ "$local_rev" != "$remote_rev" ]; then
  echo "Deploying $local_rev -> $remote_rev"
  git reset --hard "origin/$BRANCH"
elif [ "${FORCE_DEPLOY:-false}" = "true" ]; then
  echo "FORCE_DEPLOY is set; rebuilding $local_rev"
else
  echo "Retrying deploy of $remote_rev; containers serve ${deployed_rev:-unknown}"
fi

# build retags the shared `:local` image before `migrate` runs. If the build or the migration
# fails the schema is still the old one, so put the tag back: anything that resolves it
# (`docker compose run --rm sync`, a recreated container) would otherwise boot the new code on it.
image_tag="yclients_bi_system-app:local"
previous_image="$(docker image inspect -f '{{.Id}}' "$image_tag" 2>/dev/null || true)"

if ! { APP_REVISION="$remote_rev" docker compose build api worker migrate && docker compose run --rm migrate; }; then
  echo "Build or migration failed" >&2
  if [ -n "$previous_image" ]; then
    docker tag "$previous_image" "$image_tag"
    echo "Restored $image_tag to $previous_image" >&2
  fi
  exit 1
fi
docker compose up -d api worker

wait_for_api_ready

# Create-only bootstrap: an existing user is skipped, so a deploy never rewrites a password an admin
# changed or re-enables a deactivated account. The password is not on the command line: the
# container reads PORTAL_ADMIN_PASSWORD from the same .env through compose's env_file.
ensure_portal_admin() {
  local email="$1"
  local full_name="$2"
  if [ -z "$email" ]; then
    return 0
  fi

  echo "Ensuring portal platform_admin (${email})..."
  docker compose run --rm --no-deps --entrypoint python api create_portal_admin.py \
    --email "$email" \
    --full-name "$full_name"
}

if [ -n "$(env_value PORTAL_ADMIN_PASSWORD)" ]; then
  admin_name="$(env_value PORTAL_ADMIN_NAME)"
  ensure_portal_admin "$(env_value PORTAL_ADMIN_EMAIL)" "${admin_name:-Portal Admin}"
  extra_admin_emails_value="$(env_value PORTAL_EXTRA_ADMIN_EMAILS)"
  if [ -n "$extra_admin_emails_value" ]; then
    extra_admin_name="$(env_value PORTAL_EXTRA_ADMIN_NAME)"
    IFS=',' read -ra extra_admin_emails <<< "$extra_admin_emails_value"
    for extra_admin_email in "${extra_admin_emails[@]}"; do
      extra_admin_email="${extra_admin_email#"${extra_admin_email%%[![:space:]]*}"}"
      extra_admin_email="${extra_admin_email%"${extra_admin_email##*[![:space:]]}"}"
      ensure_portal_admin "$extra_admin_email" "${extra_admin_name:-Portal Admin}"
    done
  fi
fi

echo "Deploy completed: $remote_rev"
exit 0
