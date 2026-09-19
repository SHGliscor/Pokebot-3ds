@echo off
setlocal
cd /d "%~dp0"
title Pokebot3DS-CFW v0p43EJ HF94 DexNav Fang + Accidental Encounter Reset
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 run_qt_live.py
) else (
  python run_qt_live.py
)
if errorlevel 1 pause
