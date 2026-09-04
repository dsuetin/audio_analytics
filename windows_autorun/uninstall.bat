@echo off
setlocal EnableExtensions
rem ================================================================
rem  uninstall.bat - Audio Analytics uninstaller
rem
rem  Steps:
rem    1. Kill client / gui processes;
rem    2. Clean Registry Run/RunOnce (HKCU + HKLM + WOW6432Node),
rem       Startup folders, Task Scheduler;
rem    3. Delete folders: .venv, __pycache__, logs.
rem
rem  Preserved: source code (.py, .bat, .vbs, .env, .env.example,
rem  requirements.txt, README.md, .gitignore).
rem ================================================================

set "INSTALL_DIR=%~dp0"
if "%INSTALL_DIR:~-1%"=="\" set "INSTALL_DIR=%INSTALL_DIR:~0,-1%"

echo.
echo ============================================================
echo  Audio Analytics - Uninstall
echo ============================================================
echo.
echo  Directory: %INSTALL_DIR%
echo.
echo  Will be removed:
echo    - Registry autostart entries;
echo    - Running processes.
echo.
echo  Preserved: source code files
echo.

rem ----------------------------------------------------------------
rem  Confirmation
rem ----------------------------------------------------------------
:ask_confirm
echo.
set "CONFIRM="
set /p CONFIRM="Proceed with uninstall? (y / Enter): "
if /i "%CONFIRM%"=="y" goto :do_uninstall
if "%CONFIRM%"=="" goto :do_uninstall
echo.
echo  Cancelled.
exit /b 0

:do_uninstall
echo.

rem ----------------------------------------------------------------
rem  Step 1 - Stop client and gui processes
rem ----------------------------------------------------------------
echo [1/3] Stopping client and gui processes...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'client\.py|gui\.py|aud_i' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
timeout /t 2 /nobreak >nul
echo        Done.

rem ----------------------------------------------------------------
rem  Step 2 - Clean Registry and autostart
rem ----------------------------------------------------------------
echo [2/3] Cleaning Registry and autostart...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$hives=@('HKCU:\Software\Microsoft\Windows\CurrentVersion\Run','HKLM:\Software\Microsoft\Windows\CurrentVersion\Run','HKLM:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Run','HKCU:\Software\Microsoft\Windows\CurrentVersion\RunOnce','HKLM:\Software\Microsoft\Windows\CurrentVersion\RunOnce'); foreach($p in $hives){ if(-not(Test-Path $p)){continue}; $props=(Get-ItemProperty $p).PSObject.Properties; foreach($prop in $props){ if($prop.Name -like 'PS*'){continue}; if($prop.Name -match 'AudioAnalytics' -or $prop.Value -match 'AudioAnalytics|audio_client|autostart_client|autostart_gui'){ try{ Remove-ItemProperty -Path $p -Name $prop.Name -ErrorAction Stop; Write-Output ('  Deleted: '+$p+' :: '+$prop.Name) }catch{ Write-Output ('  Error: '+$p+' :: '+$prop.Name) } } } }"
echo        Registry Run/RunOnce cleaned.

rem  Startup folder
if exist "%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\AudioAnalytics*.lnk" (
    del /q "%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\AudioAnalytics*.lnk"
    echo        Deleted AudioAnalytics shortcuts from Startup.
)
if exist "%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\audio_client.bat" (
    del /q "%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\audio_client.bat"
    echo        Deleted audio_client.bat from Startup.
)
for %%F in ("%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\*AudioAnalytics*") do (
    if exist "%%F" del /q "%%F"
)
for %%F in ("%ALLUSERSPROFILE%\Microsoft\Windows\Start Menu\Programs\Startup\*AudioAnalytics*") do (
    if exist "%%F" del /q "%%F"
)
echo        Startup folder cleaned.

rem  Task Scheduler
schtasks /End   /TN "AudioAnalyticsClient"   >nul 2>&1
schtasks /Delete /TN "AudioAnalyticsClient"  /F >nul 2>&1
schtasks /End   /TN "AudioAnalyticsGUI"      >nul 2>&1
schtasks /Delete /TN "AudioAnalyticsGUI"     /F >nul 2>&1
schtasks /End   /TN "AudioAnalyticsWatchdog" >nul 2>&1
schtasks /Delete /TN "AudioAnalyticsWatchdog" /F >nul 2>&1
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "Get-ScheduledTask | Where-Object { $_.TaskName -match 'AudioAnalytics|Audio' -and $_.TaskPath -notmatch '^\\Microsoft' } | ForEach-Object { Unregister-ScheduledTask -TaskName $_.TaskName -TaskPath $_.TaskPath -Confirm:$false -ErrorAction SilentlyContinue }"
echo        Task Scheduler tasks removed.

rem ----------------------------------------------------------------
rem  Step 3 - Delete folders: .venv, __pycache__, logs
rem ----------------------------------------------------------------
echo [3/3] Deleting folders...

if exist "%INSTALL_DIR%\.venv" (
    rmdir /s /q "%INSTALL_DIR%\.venv"
    echo        Deleted .venv
)
if exist "%INSTALL_DIR%\__pycache__" (
    rmdir /s /q "%INSTALL_DIR%\__pycache__"
    echo        Deleted __pycache__
)
if exist "%INSTALL_DIR%\logs" (
    rmdir /s /q "%INSTALL_DIR%\logs"
    echo        Deleted logs
)

echo.
echo ============================================================
echo  Uninstall complete.
echo  Audio Analytics service removed and cleaned up.
echo  Source files preserved in: %INSTALL_DIR%
echo ============================================================
echo.
echo  To reinstall: %INSTALL_DIR%\start.bat
echo  To delete source files manually - delete the folder.
echo.
exit /b 0
