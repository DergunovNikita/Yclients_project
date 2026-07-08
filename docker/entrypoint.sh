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
  bootstrap-db)
    shift
    exec python -m scripts.bootstrap_db "$@"
    ;;
  seed-demo)
    shift
    exec python -m scripts.seed_demo "$@"
    ;;
  setup-analytics)
    shift
    exec python setup_analytics.py "$@"
    ;;
  import-sheets)
    shift
    exec python import_sheets.py "$@"
    ;;
  shell)
    shift
    exec sh "$@"
    ;;
  *)
    exec "$@"
    ;;
esac
