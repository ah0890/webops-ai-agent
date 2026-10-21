#!/usr/bin/env bash
# One-time setup on macOS/Linux. Run from the project root:  bash scripts/setup.sh
set -euo pipefail
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m playwright install --with-deps chromium || .venv/bin/python -m playwright install chromium
[ -f .env ] || cp .env.example .env
[ -f config/websites.yaml ] || cp config/websites.example.yaml config/websites.yaml
[ -f config/workflows.yaml ] || cp config/workflows.example.yaml config/workflows.yaml
.venv/bin/python -m pytest -q
echo
echo "Setup complete. Next: put your real websites in config/websites.yaml, then:"
echo "  .venv/bin/python -m app.cli sync"
echo "  .venv/bin/python -m app.cli serve      # http://127.0.0.1:8000"
