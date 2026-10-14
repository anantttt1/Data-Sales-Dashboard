@echo off
setlocal
cd /d "%~dp0"
title SalesScope - local dashboard
set PORT=5001

where python >nul 2>nul
if errorlevel 1 (
  echo Python was not found. Install Python 3 from https://www.python.org/downloads/ and enable "Add python.exe to PATH".
  pause
  exit /b 1
)

if not exist .venv\Scripts\python.exe (
  echo Setting up SalesScope environment...
  python -m venv .venv
  if errorlevel 1 goto failed
)

echo Checking dashboard dependencies...
.venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 goto failed

echo.
echo SalesScope will open in your browser when the server is ready.
echo Keep this window open while using the dashboard.
echo Press Ctrl+C here to stop the server.
echo.
.venv\Scripts\python.exe app.py
pause
exit /b 0

:failed
echo.
echo Setup failed. Check the error above, then try again.
pause
exit /b 1
