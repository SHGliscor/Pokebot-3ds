@echo off
setlocal
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" tools\capture_outcome_authority_test.py
) else (
  python tools\capture_outcome_authority_test.py
)
echo.
pause
