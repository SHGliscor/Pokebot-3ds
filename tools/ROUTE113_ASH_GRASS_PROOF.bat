@echo off
setlocal
cd /d "%~dp0"
title Pokebot3DS-CFW Route 113 Ash Grass Proof
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 tools\route113_ash_grass_proof.py
) else (
  python tools\route113_ash_grass_proof.py
)
echo.
pause
