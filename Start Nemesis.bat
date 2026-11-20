@echo off
setlocal enabledelayedexpansion
rem Starts Nemesis and restarts it if it crashes (gives up after 5 crashes in a row).
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
set tries=0
call venv\Scripts\activate.bat
:loop
python nemesis.py %*
set code=!errorlevel!
if !code! EQU 0 exit /b 0
if !code! EQU 3 (
  echo.
  echo Nemesis is already running or its port is busy. Not restarting.
  pause
  exit /b 3
)
set /a tries+=1
if !tries! GEQ 5 (
  echo.
  echo Nemesis stopped 5 times in a row. Please read nemesis_error.log
  pause
  exit /b 1
)
echo.
echo Nemesis stopped with an error. Restarting in 5 seconds... close this window to cancel.
timeout /t 5 >nul
goto loop
