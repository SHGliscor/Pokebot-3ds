@echo off
setlocal
cd /d "%~dp0"
echo.
echo POST-CAPTURE MAPPER - UNREGISTERED SPECIES TEST
echo.
echo 1. Catch a NORMAL species that is NOT registered in the Pokedex.
echo 2. Leave the game on the FIRST POKEDEX registration screen.
echo 3. Run this probe. It will map: POKEDEX -^> A -^> NICKNAME -^> NO -^> BOX MESSAGE -^> FIELD.
echo.
python tools\post_capture_mapper_probe.py --expected unregistered
echo.
pause
