@echo off
REM Wrapper invoked by the one-time SchwabFlow-Verify scheduled task.
REM Verifies the current ET day's captures and writes captures\verify_<date>.txt.
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
"%~dp0.venv\Scripts\python.exe" "%~dp0verify_captures.py" 1>> "%~dp0captures\verify.log" 2>&1
