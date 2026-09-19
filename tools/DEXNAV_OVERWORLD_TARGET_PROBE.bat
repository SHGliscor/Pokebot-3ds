@echo off
setlocal
cd /d "%~dp0.."
title Pokebot3DS-CFW - DexNav Overworld Target Probe

echo.
echo ============================================================================
echo  POKEBOT3DS-CFW - DEXNAV OVERWORLD TARGET PROBE
echo ============================================================================
echo.
echo Purpose: find the actual overworld DexNav Pokemon object/coordinates in RAM.
echo.
echo IMPORTANT:
echo - This probe is READ-ONLY and sends NO controller input.
echo - Use the tutorial Poochyena calibration save.
echo - Stage 1: before Poochyena is visible.
echo - Stage 2: Poochyena visible, player stationary.
echo - Stage 3: use the PHYSICAL Circle Pad to sneak into it.
echo - Do NOT use the D-pad once Poochyena is visible.
echo - Do NOT run the main Pokebot at the same time as this probe.
echo.
python tools\dexnav_overworld_target_probe.py

echo.
pause
