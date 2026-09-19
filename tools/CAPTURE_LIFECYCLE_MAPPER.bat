@echo off
setlocal
cd /d "%~dp0"
set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
%PY% tools\capture_lifecycle_mapper.py
set RC=%ERRORLEVEL%
echo.
echo Capture Lifecycle Mapper finished with code %RC%.
pause
exit /b %RC%
