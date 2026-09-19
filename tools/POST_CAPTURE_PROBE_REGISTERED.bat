@echo off
setlocal
cd /d "%~dp0"
echo.
echo POST-CAPTURE MAPPER - REGISTERED SPECIES TEST
echo.
echo 1. Catch a NORMAL species that is ALREADY registered in the Pokedex.
echo 2. Leave the game on the NICKNAME Yes/No screen with YES selected.
echo 3. Run this probe. It will map: NICKNAME -^> NO -^> BOX MESSAGE -^> FIELD.
echo.
python tools\post_capture_mapper_probe.py --expected registered
echo.
pause
