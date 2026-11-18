@echo off
rem Stops HypeClipper, its recordings, and frees the AI model's memory.
cd /d "%~dp0"
echo Stopping HypeClipper...
taskkill /F /T /FI "WINDOWTITLE eq HypeClipper*" >nul 2>nul
taskkill /F /T /IM streamlink.exe >nul 2>nul
where ollama >nul 2>nul && for /f "skip=1 tokens=1" %%m in ('ollama ps') do ollama stop %%m >nul 2>nul
echo Done. Double-click start.bat to start it again.
timeout /t 4 >nul
