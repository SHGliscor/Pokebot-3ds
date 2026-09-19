@echo off
setlocal
cd /d "%~dp0\.."
python tools\oras_touch_profile_calibrator.py
if errorlevel 1 pause
