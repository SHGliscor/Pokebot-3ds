@echo off
setlocal
cd /d "%~dp0"
set /p POKEBOT_PROFILE=Enter console profile name (example: O3DS or N3DS-XL): 
if "%POKEBOT_PROFILE%"=="" exit /b 1
title Pokebot3DS-CFW - %POKEBOT_PROFILE%
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 run_qt_live.py --console-profile "%POKEBOT_PROFILE%"
) else (
  python run_qt_live.py --console-profile "%POKEBOT_PROFILE%"
)
if errorlevel 1 pause
