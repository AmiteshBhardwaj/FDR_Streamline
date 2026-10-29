#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ ! -x .venv/bin/python ]]; then
  echo "Run bash scripts/setup.sh first." >&2
  exit 1
fi
worker_pid=""
web_pid=""
cleanup() {
  trap - EXIT INT TERM
  [[ -z "$worker_pid" ]] || kill "$worker_pid" 2>/dev/null || true
  [[ -z "$web_pid" ]] || kill "$web_pid" 2>/dev/null || true
  wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM
.venv/bin/python manage.py monitor_deadlines --interval 60 &
worker_pid=$!
.venv/bin/python manage.py runserver 127.0.0.1:8000 --noreload &
web_pid=$!
echo "Portal: http://127.0.0.1:8000 — web and deadline worker started. Ctrl-C stops both."
wait -n "$worker_pid" "$web_pid"
