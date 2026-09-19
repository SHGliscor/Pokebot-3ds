@echo off
setlocal
cd /d "%~dp0\.."
python tools\offline_regression.py || goto :fail
python tools\release_integrity.py || goto :fail
echo.
echo Pokebot3DS-CFW offline release checks: PASS
pause
exit /b 0
:fail
echo.
echo Pokebot3DS-CFW offline release checks: FAIL
pause
exit /b 1
