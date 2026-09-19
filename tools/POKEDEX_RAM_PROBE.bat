@echo off
setlocal
cd /d "%~dp0"
python tools\pokedex_ram_probe.py
if errorlevel 1 (
  echo.
  echo Pokédex RAM probe failed.
)
echo.
pause
