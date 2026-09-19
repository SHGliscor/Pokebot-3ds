@echo off
setlocal
cd /d "%~dp0"
set /p POKEBOT_3DS_IP=Enter 3DS IP: 
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 test_framebuffer.py "%POKEBOT_3DS_IP%"
) else (
  python test_framebuffer.py "%POKEBOT_3DS_IP%"
)
echo.
pause
