#!/usr/bin/env sh
# Start the Resume Parser web app: http://127.0.0.1:8000 (falls back to 8001 if 8000 is busy)
cd "$(dirname "$0")" || exit 1
if [ -x ".venv/Scripts/python.exe" ]; then PY=".venv/Scripts/python.exe"      # Windows (Git Bash)
elif [ -x ".venv/bin/python" ]; then PY=".venv/bin/python"                  # macOS / Linux
else
  echo "Virtual environment not found. Run: python -m venv .venv && .venv/bin/pip install -r requirements.txt"
  exit 1
fi
exec "$PY" serve.py "$@"
