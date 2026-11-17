#!/bin/bash
# Double-click to start AutoApply (macOS). First run sets everything up automatically.
cd "$(dirname "$0")"
URL="http://127.0.0.1:8765"

# Already running? Just open it.
if curl -s -o /dev/null "$URL/api/state"; then open "$URL"; exit 0; fi

PY=""
for c in python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v $c >/dev/null 2>&1 && $c -c 'import sys; sys.exit(0 if sys.version_info >= (3,9) else 1)' 2>/dev/null; then PY=$c; break; fi
done
if [ -z "$PY" ]; then
  osascript -e 'display dialog "AutoApply needs Python (free). Your browser will open the download page: install it, then double-click Start AutoApply again." buttons {"OK"} default button 1 with title "AutoApply"'
  open "https://www.python.org/downloads/macos/"; exit 1
fi

HASH=$(shasum requirements.txt | cut -d" " -f1)
if [ ! -x .venv/bin/python ] || [ "$(cat .venv/.req 2>/dev/null)" != "$HASH" ]; then
  echo ""; echo "  Setting up AutoApply (first time only, takes 1-3 minutes)..."; echo ""
  "$PY" -m venv .venv || { echo "Couldn't create the environment."; read -p "Press Enter to close"; exit 1; }
  .venv/bin/python -m pip install -q --upgrade pip
  .venv/bin/python -m pip install -q -r requirements.txt || { echo "Install failed. Check your internet connection."; read -p "Press Enter to close"; exit 1; }
  .venv/bin/python -m playwright install chromium || echo "  (Browser download failed; AutoApply will use Google Chrome if you have it.)"
  echo "$HASH" > .venv/.req
fi
.venv/bin/python server.py
