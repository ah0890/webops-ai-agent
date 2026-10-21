#!/usr/bin/env bash
# End-to-end verification of the real browser pipeline against the public scraping sandbox
# (books.toscrape.com), in a separate database so it never mixes with your real supplier data.
set -euo pipefail
PY=${PY:-.venv/bin/python}; [ -x "$PY" ] || PY=.venv/Scripts/python
export DATABASE_URL="sqlite:///./data/verify.db"
export WEBSITES_CONFIG=tests/integration/toscrape.websites.yaml
export WORKFLOWS_CONFIG=tests/integration/toscrape.workflows.yaml
export PYTHONIOENCODING=utf-8
"$PY" -m app.cli sync
"$PY" -m app.cli run "Sandbox verification"
echo "Inspect it: DATABASE_URL=$DATABASE_URL WEBSITES_CONFIG=$WEBSITES_CONFIG WORKFLOWS_CONFIG=$WORKFLOWS_CONFIG $PY -m app.cli serve"
