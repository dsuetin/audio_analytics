@echo off
setlocal EnableExtensions EnableDelayedExpansion
rem ================================================================
rem  start.bat - Audio Analytics installer & launcher
rem
rem  Steps:
rem    1. Create .env (if missing);
rem    2. Find/install Python 3.11, create .venv, install deps;
rem    3. List audio devices (so user can set AUDIO_DEVICE_ID);
rem    4. Register autostart in Windows Registry;
rem    5. Launch client and GUI.
rem
rem  Usage:
rem    start.bat              install + launch (with pause)
rem    start.bat /UNATTENDED  install + launch (no pause)
rem ================================================================

set "_PAUSE=1"
for %%A in (%*) do if /i "%%A"=="/UNATTENDED" set "_PAUSE=0"

set "INSTALL_DIR=%~dp0"
if "%INSTALL_DIR:~-1%"=="\" set "INSTALL_DIR=%INSTALL_DIR:~0,-1%"

echo.
echo  ==========================================================
echo    Audio Analytics - Installation
echo  ==========================================================
echo.
echo  Install directory: %INSTALL_DIR%
echo  Required Python: 3.11 (x64)
echo.

rem ----------------------------------------------------------------
rem  Step 1 - .env
rem ----------------------------------------------------------------
if not exist "%INSTALL_DIR%\.env" goto env_missing
echo  [1/6] .env exists - skipping.
goto env_done

:env_missing
if not exist "%INSTALL_DIR%\.env.example" goto env_none
copy /Y "%INSTALL_DIR%\.env.example" "%INSTALL_DIR%\.env" >nul
echo  [1/6] Created .env from .env.example.
echo        Edit .env to set SERVER_IP, STORE_ID, WORKER_NAME
echo        and, if needed, AUDIO_DEVICE_ID.
type "%INSTALL_DIR%\.env"
goto env_done

:env_none
echo  [1/6] Error: no .env and no .env.example!
echo        Create .env file in %INSTALL_DIR%.
goto :fail

:env_done
echo.

rem ----------------------------------------------------------------
rem  Step 2 - Find Python 3.11
rem ----------------------------------------------------------------
set "PY_BIN="

rem  A) py launcher with version 3.11.
py -3.11 -c "import sys;print(sys.executable)" >"%TEMP%\aa_py.txt" 2>nul
if not errorlevel 1 set /p PY_BIN=<"%TEMP%\aa_py.txt"

rem  B) python3.11 on PATH.
if not defined PY_BIN (
    for /f "delims=" %%P in ('python3.11 -c "import sys;print(sys.executable)" 2^>nul') do set "PY_BIN=%%P"
)

rem  C) python on PATH, must be 3.11.
if not defined PY_BIN (
    for /f "delims=" %%P in ('python -c "import sys;print(sys.executable)" 2^>nul') do set "PY_BIN=%%P"
)

rem  Verify version is 3.11.
if defined PY_BIN (
    "%PY_BIN%" -c "import sys;raise SystemExit(0 if sys.version_info[:2]==(3,11) else 1)" >nul 2>&1
    if errorlevel 1 set "PY_BIN="
)

if not defined PY_BIN goto install_py311

echo  [2/6] Found Python 3.11: %PY_BIN%
for /f "delims=" %%V in ('"%PY_BIN%" --version 2^>nul') do echo        %%V
echo.
goto venv_check

rem ----------------------------------------------------------------
rem  Python 3.11 not found - auto-install
rem ----------------------------------------------------------------
:install_py311
echo  [2/6] Python 3.11 not found. Downloading and installing...
set "PY_URL=https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe"
set "PY_INSTALLER=%TEMP%\python-3.11.9-amd64.exe"
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ProgressPreference='SilentlyContinue'; Invoke-WebRequest -UseBasicParsing -Uri '%PY_URL%' -OutFile '%PY_INSTALLER%'"
if errorlevel 1 goto py_download_fail
echo        Running installer (silent, may take a few minutes)...
"%PY_INSTALLER%" /quiet InstallAllUsers=0 PrependPath=1 Include_test=0 Include_launcher=1 Include_pip=1 Include_doc=1 Include_tcltk=1 Include_dev=1 Include_exe=1 Shortcuts=0
if errorlevel 1 goto py_install_fail

set "PY_BIN="
py -3.11 -c "import sys;print(sys.executable)" >"%TEMP%\aa_py.txt" 2>nul
if not errorlevel 1 set /p PY_BIN=<"%TEMP%\aa_py.txt"
if not defined PY_BIN if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" set "PY_BIN=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
if not defined PY_BIN if exist "%ProgramFiles%\Python311\python.exe" set "PY_BIN=%ProgramFiles%\Python311\python.exe"

if defined PY_BIN (
    "%PY_BIN%" -c "import sys;raise SystemExit(0 if sys.version_info[:2]==(3,11) else 1)" >nul 2>&1
    if errorlevel 1 set "PY_BIN="
)

if not defined PY_BIN goto py_install_fail

echo        Python 3.11 installed: %PY_BIN%
for /f "delims=" %%V in ('"%PY_BIN%" --version 2^>nul') do echo        %%V
echo.
goto venv_check

:py_download_fail
echo  [2/6] Error: failed to download Python 3.11 installer.
echo        Check internet connection.
goto :fail

:py_install_fail
echo  [2/6] Error: Python 3.11 installation failed.
echo        Install manually from https://www.python.org/downloads/
echo        (enable "py launcher" and "Add python.exe to PATH")
echo        then re-run start.bat.
goto :fail

rem ----------------------------------------------------------------
rem  Step 2b - .venv
rem ----------------------------------------------------------------
:venv_check
if not exist "%INSTALL_DIR%\.venv\Scripts\python.exe" goto create_venv
if not exist "%INSTALL_DIR%\.venv\Scripts\pythonw.exe" goto create_venv

"%INSTALL_DIR%\.venv\Scripts\python.exe" -c "import sys;raise SystemExit(0 if sys.version_info[:2]==(3,11) else 1)" >nul 2>&1
if not errorlevel 1 (
    echo  [2/6] .venv exists and works with Python 3.11 - reusing.
    goto venv_install_deps
)

echo  [2/6] .venv was created with wrong Python - recreating...
rmdir /S /Q "%INSTALL_DIR%\.venv" >nul 2>&1

:create_venv
if not exist "%INSTALL_DIR%\requirements.txt" goto venv_no_req
echo  [2/6] Creating virtual environment .venv (Python 3.11)...
"%PY_BIN%" -m venv "%INSTALL_DIR%\.venv"
if errorlevel 1 goto venv_fail
echo        .venv created.

rem ----------------------------------------------------------------
rem  Step 2c - Dependencies
rem ----------------------------------------------------------------
:venv_install_deps
echo  [2/6] Checking dependencies in .venv...
"%INSTALL_DIR%\.venv\Scripts\python.exe" -c "import grpc, numpy, sounddevice, aiokafka, dotenv, google.protobuf" >nul 2>&1
if not errorlevel 1 (
    echo        Dependencies already installed - skipping pip.
    goto venv_done
)
echo  [2/6] Installing dependencies from requirements.txt...
"%INSTALL_DIR%\.venv\Scripts\python.exe" -m pip install --upgrade pip >nul 2>&1
"%INSTALL_DIR%\.venv\Scripts\python.exe" -m pip install -r "%INSTALL_DIR%\requirements.txt"
if errorlevel 1 goto venv_pip_fail
echo        Dependencies installed.
goto venv_done

:venv_no_req
echo  [2/6] Error: requirements.txt not found.
goto :fail

:venv_fail
echo  [2/6] Error: failed to create .venv.
echo        Check Python 3.11 installation and py launcher.
goto :fail

:venv_pip_fail
echo  [2/6] Error: failed to install dependencies.
echo        Check internet connection and try again.
goto :fail

:venv_done

rem ----------------------------------------------------------------
rem  Step 3 - List audio devices
rem ----------------------------------------------------------------
echo.
echo  [3/6] Available audio devices:
echo  ----------------------------------------------------------
"%INSTALL_DIR%\.venv\Scripts\python.exe" "%INSTALL_DIR%\list_devices.py"
echo  ----------------------------------------------------------
echo.
echo  If you want to use a specific external USB microphone,
echo  edit .env and set AUDIO_DEVICE_ID with the hardware id above.
echo  Press any key to continue...
pause >nul
echo.

rem ----------------------------------------------------------------
rem  Step 4 - Register autostart in Registry
rem ----------------------------------------------------------------
echo  [4/6] Registering autostart in Registry (VBS launchers)...
if not exist "%INSTALL_DIR%\autostart_client.vbs" goto reg_novbs
if not exist "%INSTALL_DIR%\autostart_gui.vbs" goto reg_novbs
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$vbs1='wscript.exe \"%INSTALL_DIR%\autostart_client.vbs\"'; $vbs2='wscript.exe \"%INSTALL_DIR%\autostart_gui.vbs\"'; New-ItemProperty -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' -Name 'AudioAnalyticsClient' -PropertyType String -Value $vbs1 -Force | Out-Null; New-ItemProperty -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' -Name 'AudioAnalyticsGUI' -PropertyType String -Value $vbs2 -Force | Out-Null"
if errorlevel 1 goto reg_fail
echo        Done: autostart registered for current user.
goto reg_done

:reg_fail
echo  [4/6] Error: failed to write to Registry.
goto :fail

:reg_novbs
echo  [4/6] Error: launcher files autostart_client.vbs /
echo        autostart_gui.vbs not found in %INSTALL_DIR%.
goto :fail

:reg_done

rem ----------------------------------------------------------------
rem  Step 5 - Launch client and GUI
rem ----------------------------------------------------------------
if not exist "%INSTALL_DIR%\autostart_client.vbs" goto launch_novbs
if not exist "%INSTALL_DIR%\autostart_gui.vbs" goto launch_novbs
echo  [5/6] Launching client and GUI...
start "" wscript.exe "%INSTALL_DIR%\autostart_client.vbs"
start "" wscript.exe "%INSTALL_DIR%\autostart_gui.vbs"
echo        Done. Audio Analytics will start automatically on Windows login.

rem ----------------------------------------------------------------
rem  Step 6 - Summary
rem ----------------------------------------------------------------
echo.
echo  ==========================================================
echo    Installation complete.
echo    Service runs via Python 3.11 in .venv.
echo    Check:  tasklist /FI "IMAGENAME eq pythonw.exe"
echo  ==========================================================
echo.
echo  Client start requested (hidden). Watch the log:
echo    powershell -NoProfile -Command "Get-Content '%INSTALL_DIR%\logs\client.log' -Wait -Tail 20"
echo  Live status (mic/grpc/kafka/purchases) is shown in the GUI via shared memory
echo.
if "%_PAUSE%"=="1" (
    echo  Press any key to close...
    pause >nul
)
exit /b 0

:launch_novbs
echo  [5/6] Error: launcher files autostart_client.vbs /
echo        autostart_gui.vbs not found in %INSTALL_DIR%.
goto :fail

:fail
echo.
echo  **************************************************************
echo    Installation failed. Fix the error above and re-run
echo    start.bat.
echo  **************************************************************
echo.
if "%_PAUSE%"=="1" (
    echo  Press any key to close...
    pause >nul
)
exit /b 1
