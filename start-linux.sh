#!/bin/bash
cd "$(dirname "$0")"
URL="http://127.0.0.1:8765"
if curl -s -o /dev/null "$URL/api/state"; then xdg-open "$URL"; exit 0; fi
HASH=$(sha1sum requirements.txt | cut -d" " -f1)
if [ ! -x .venv/bin/python ] || [ "$(cat .venv/.req 2>/dev/null)" != "$HASH" ]; then
  echo "Setting up AutoApply (first time only)..."
  python3 -m venv .venv && .venv/bin/python -m pip install -q -r requirements.txt || exit 1
  .venv/bin/python -m playwright install chromium || echo "(Browser download failed; will use Chrome if installed.)"
  echo "$HASH" > .venv/.req
fi
exec .venv/bin/python server.py
