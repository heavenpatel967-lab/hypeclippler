@echo off
cd /d "%~dp0"
rem stop an old HypeClipper that is still running (and its recordings) before starting
taskkill /F /T /FI "WINDOWTITLE eq HypeClipper*" >nul 2>nul
taskkill /F /T /IM streamlink.exe >nul 2>nul
title HypeClipper

where python >nul 2>nul
if errorlevel 1 (
  echo Python is not installed. Install Python 3.11 or 3.12 from https://www.python.org/downloads/
  echo IMPORTANT: tick "Add python.exe to PATH" in the installer, then run this file again.
  pause
  exit /b 1
)

where ffmpeg >nul 2>nul
if errorlevel 1 (
  echo ffmpeg not found, installing it with winget...
  winget install -e --id Gyan.FFmpeg --accept-source-agreements --accept-package-agreements
  echo.
  echo ffmpeg installed. CLOSE this window and double-click start.bat again.
  pause
  exit /b 0
)

if not exist ".venv\Scripts\python.exe" (
  echo First run: creating virtual environment and installing packages. This takes a few minutes...
  python -m venv .venv
)
call ".venv\Scripts\activate.bat"
python -m pip install --disable-pip-version-check -q --upgrade pip
python -m pip install --disable-pip-version-check -q -r requirements.txt
if errorlevel 1 (
  echo Package install failed. See the error above.
  pause
  exit /b 1
)

python run.py
pause
