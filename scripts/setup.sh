#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ "${1:-}" != "" && "${1:-}" != "--demo" ]]; then
  echo "Usage: bash scripts/setup.sh [--demo]" >&2
  exit 2
fi
if [[ ! -x .venv/bin/python ]]; then
  python3 -m venv .venv || python3 -m venv --without-pip .venv
fi
if .venv/bin/python -m pip --version >/dev/null 2>&1; then
  .venv/bin/python -m pip install -r requirements-dev.txt
else
  python3 -m pip --python .venv/bin/python install -r requirements-dev.txt
fi
.venv/bin/python manage.py migrate --noinput
if [[ "${1:-}" == "--demo" ]]; then
  .venv/bin/python manage.py seed_demo
fi
echo "Setup complete. Start with: bash scripts/start.sh"
