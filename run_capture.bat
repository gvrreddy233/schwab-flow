@echo off
REM Wrapper invoked by Windows Task Scheduler.
REM cd to project root so chain_capture.py finds .env / schwab_token.json / chains.db.
cd /d "%~dp0"
REM Force UTF-8 stdout so Unicode (Δ etc.) doesn't crash under Task Scheduler's cp1252 default.
set PYTHONIOENCODING=utf-8
"%~dp0.venv\Scripts\python.exe" "%~dp0chain_capture.py" 1>> "%~dp0captures\capture.log" 2>&1
