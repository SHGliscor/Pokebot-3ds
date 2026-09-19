@echo off
setlocal
cd /d "%~dp0"
title Pokebot3DS-CFW Requirements
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 -m pip install -r requirements.txt
) else (
  python -m pip install -r requirements.txt
)
echo.
pause
