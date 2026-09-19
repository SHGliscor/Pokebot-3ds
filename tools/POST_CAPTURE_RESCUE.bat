@echo off
setlocal
cd /d "%~dp0"
python tools\post_capture_rescue.py
set rc=%ERRORLEVEL%
echo.
if not "%rc%"=="0" echo Rescue did not complete. Upload the generated post_capture_rescue JSON + TXT.
pause
exit /b %rc%
