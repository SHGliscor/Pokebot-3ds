@echo off
setlocal
cd /d "%~dp0"
set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
%PY% tools\capture_trace_diff.py %*
set RC=%ERRORLEVEL%
echo.
echo Capture Trace Diff finished with code %RC%.
pause
exit /b %RC%
