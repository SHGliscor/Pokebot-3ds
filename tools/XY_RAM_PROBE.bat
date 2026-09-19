@echo off
setlocal
cd /d "%~dp0.."
if "%~1"=="" (
  set /p IP=3DS IP address: 
) else (
  set IP=%~1
)
python tools\xy_ram_probe.py %IP%
pause
