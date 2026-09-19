@echo off
setlocal
cd /d "%~dp0"
title Pokebot3DS-CFW - Console 2
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 run_qt_live.py --console-profile "Console 2"
) else (
  python run_qt_live.py --console-profile "Console 2"
)
if errorlevel 1 pause
