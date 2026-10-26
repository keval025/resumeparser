@echo off
REM Start the Resume Parser web app: http://127.0.0.1:8000 (falls back to 8001 if 8000 is busy)
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Virtual environment not found. Run these first:
    echo     py -3 -m venv .venv
    echo     .venv\Scripts\python -m pip install -r requirements.txt
    exit /b 1
)
".venv\Scripts\python.exe" serve.py %*
