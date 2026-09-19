@echo off
setlocal
cd /d "%~dp0"
title Pokebot3DS-CFW - DexNav Tutorial Poochyena Probe
echo.
echo ================================================================
echo  DEXNAV TUTORIAL POOCHYENA MAPPER PROBE
echo ================================================================
echo.
echo This maps the Route 101 tutorial text and encounter BEFORE it is
echo wired into the Pokebot3DS-CFW Hunt UI.
echo.
echo IMPORTANT:
echo - Use a save from BEFORE the tutorial Poochyena is consumed.
echo - The probe can send dialogue buttons.
echo - Once Poochyena appears, mark P in the probe.
echo - Then press S and use the PHYSICAL Circle Pad to sneak.
echo - Do NOT use the D-pad after Poochyena appears.
echo.
python tools\dexnav_tutorial_poochyena_probe.py
echo.
pause
