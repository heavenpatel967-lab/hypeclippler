@echo off
rem Lets Windows grow the page file automatically. This stops "WinError 8 / Not enough
rem memory resources" when several recordings, Whisper and the AI model run at once.
rem Needs admin rights (Windows asks). Restart the PC afterwards.
net session >nul 2>nul
if errorlevel 1 (
  powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b
)
echo Turning on "Automatically manage paging file size"...
powershell -NoProfile -Command "$cs = Get-CimInstance Win32_ComputerSystem; Set-CimInstance -InputObject $cs -Property @{AutomaticManagedPagefile=$true}; (Get-CimInstance Win32_ComputerSystem).AutomaticManagedPagefile"
echo.
echo If it printed True: done. RESTART YOUR PC so Windows applies it.
echo Make sure drive C: has at least 30 GB free space for the page file.
pause
