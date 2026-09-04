@echo off
rem ============================================================
rem Stops the Audio Analytics client.
rem Safe to run from ANY directory and repeatedly.
rem
rem The client is normally started automatically via the registry Run key
rem (VBS + pythonw.exe). This script stops the running client.py process.
rem If only "stop" is used, nothing re-enables the client until start.bat
rem (or a logon restart) starts it again.
rem ============================================================
setlocal EnableExtensions

echo Stopping Audio Analytics client ...

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'client\.py' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"

echo.
echo Client stopped.
echo To run the client again:  %~dp0start.bat
exit /b 0
