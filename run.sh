#!/usr/bin/env bash

PORT=5001
APP_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$APP_DIR"

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  CV Analyzer & Job Finder"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# Install deps if needed
python3 -m pip install -q --only-binary :all: cryptography 2>/dev/null || true
python3 -m pip install -q --no-deps pdfplumber pdfminer.six 2>/dev/null || true
python3 -m pip install -q -r requirements.txt 2>/dev/null || true

echo "→ Starting server at http://localhost:$PORT"
echo "  Press Ctrl+C to stop."
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# Auto-restart loop — keeps server alive if it crashes
while true; do
  python3 app.py
  echo "⚠  Server stopped — restarting in 2s…"
  sleep 2
done
