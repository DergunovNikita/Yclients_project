#!/usr/bin/env sh
set -eu

command="${1:-api}"

case "$command" in
  api)
    shift
    exec uvicorn api:app --host "${API_HOST:-0.0.0.0}" --port "${API_PORT:-8000}" "$@"
    ;;
  sync)
    shift
    exec python main.py "$@"
    ;;
  worker)
    shift
    exec python sync_worker.py "$@"
    ;;
  migrate)
    shift
    exec python migrate.py "$@"
    ;;
  shell)
    shift
    exec sh "$@"
    ;;
  *)
    exec "$@"
    ;;
esac
