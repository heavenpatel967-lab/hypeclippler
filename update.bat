@echo off
rem Updates HypeClipper IN THIS FOLDER to the newest version from GitHub.
rem Keeps your data (settings, streamers, YouTube/Instagram logins, cover, clips) and sounds.
cd /d "%~dp0"
title HypeClipper update
echo Stopping HypeClipper...
powershell -NoProfile -Command "try { Invoke-WebRequest -UseBasicParsing -Method Post http://127.0.0.1:8000/api/shutdown -TimeoutSec 5 | Out-Null } catch {}"
timeout /t 3 >nul
for /f "tokens=5" %%a in ('netstat -ano ^| findstr :8000 ^| findstr LISTENING') do taskkill /F /T /PID %%a >nul 2>nul
echo Downloading the newest version...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ErrorActionPreference='Stop';" ^
  "$tmp = Join-Path $env:TEMP 'hypeclipper_update';" ^
  "if (Test-Path $tmp) { Remove-Item $tmp -Recurse -Force };" ^
  "New-Item -ItemType Directory $tmp | Out-Null;" ^
  "Invoke-WebRequest -UseBasicParsing 'https://github.com/heavenpatel967-lab/hypeclippler/archive/refs/heads/main.zip' -OutFile (Join-Path $tmp 'u.zip');" ^
  "Expand-Archive (Join-Path $tmp 'u.zip') $tmp -Force;" ^
  "$src = Get-ChildItem $tmp -Directory | Select-Object -First 1;" ^
  "Get-ChildItem $src.FullName -Force | Where-Object { $_.Name -notin @('data','sfx','.venv') } | Copy-Item -Destination '%~dp0' -Recurse -Force;" ^
  "Remove-Item $tmp -Recurse -Force"
if errorlevel 1 (
  echo.
  echo Update failed. Check your internet and try again.
  pause
  exit /b 1
)
echo Updated. Starting HypeClipper...
start "" "%~dp0start.bat"
