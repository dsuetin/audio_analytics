@echo off
setlocal EnableExtensions EnableDelayedExpansion

title Audio Analytics - Installation

echo ==========================================================
echo Audio Analytics - Installation
echo ==========================================================
echo.

REM ==========================================================
REM CONFIG
REM ==========================================================

set "INSTALL_DIR=%~dp0"
if "%INSTALL_DIR:~-1%"=="\" set "INSTALL_DIR=%INSTALL_DIR:~0,-1%"

set "PYTHON_VERSION=3.11"
set "PYTHON_INSTALLER_VERSION=3.11.9"
set "PYTHON_EXE=%ProgramFiles%\Python311\python.exe"
set "PYTHON_INSTALLER=%TEMP%\python-3.11.9-amd64.exe"

echo Install directory: %INSTALL_DIR%
echo Required Python: 3.11 (x64)
echo.

REM ==========================================================
REM [0/6] ADMIN CHECK
REM ==========================================================

echo [0/6] Checking administrator privileges...

net session >nul 2>&1
if errorlevel 1 (
    echo.
    echo [ERROR] This installer must be run as Administrator.
    echo.
    echo Right-click start.bat and select:
    echo "Run as administrator"
    echo.
    pause
    exit /b 1
)

echo        Administrator privileges: OK
echo.

REM ==========================================================
REM [1/6] ENVIRONMENT
REM ==========================================================

echo [1/6] Checking .env...

if exist "%INSTALL_DIR%\.env" (
    echo        .env found.
) else (
    if exist "%INSTALL_DIR%\.env.example" (
        echo        [WARNING] .env not found.
        echo        Please create .env from .env.example before running.
    ) else (
        echo        [WARNING] .env.example not found.
        echo        Continuing...
    )
)

echo.

REM ==========================================================
REM [2/6] PYTHON 3.11
REM ==========================================================

echo [2/6] Checking Python 3.11...

set "PYTHON_OK="

REM ----------------------------------------------------------
REM First: check the expected official all-users installation
REM ----------------------------------------------------------

if exist "%PYTHON_EXE%" (
    echo        Found:
    echo        %PYTHON_EXE%

    "%PYTHON_EXE%" -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3,11) else 1)" >nul 2>&1

    if not errorlevel 1 (
        set "PYTHON_OK=1"
        echo        Python 3.11: OK
        goto python_found
    )

    echo        [WARNING] This Python is NOT version 3.11.
    echo        Required Python 3.11 will be installed.
)

REM ----------------------------------------------------------
REM Try Python launcher, but only accept Python 3.11
REM ----------------------------------------------------------

where py >nul 2>&1
if not errorlevel 1 (
    py -3.11 -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3,11) else 1)" >nul 2>&1

    if not errorlevel 1 (
        for /f "delims=" %%P in ('py -3.11 -c "import sys; print(sys.executable)" 2^>nul') do (
            set "PYTHON_EXE=%%P"
        )

        if defined PYTHON_EXE (
            if exist "!PYTHON_EXE!" (
                set "PYTHON_OK=1"
                echo        Python 3.11 found via Python Launcher:
                echo        !PYTHON_EXE!
                goto python_found
            )
        )
    )
)

REM ----------------------------------------------------------
REM Try python.exe from PATH, but ONLY if it is really 3.11
REM ----------------------------------------------------------

where python >nul 2>&1
if not errorlevel 1 (
    python -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3,11) else 1)" >nul 2>&1

    if not errorlevel 1 (
        for /f "delims=" %%P in ('python -c "import sys; print(sys.executable)" 2^>nul') do (
            set "PYTHON_EXE=%%P"
        )

        if defined PYTHON_EXE (
            if exist "!PYTHON_EXE!" (
                set "PYTHON_OK=1"
                echo        Python 3.11 found in PATH:
                echo        !PYTHON_EXE!
                goto python_found
            )
        )
    )
)

REM ==========================================================
REM Python 3.11 is missing or wrong version
REM Install official Python 3.11.9 x64
REM ==========================================================

:install_python

echo.
echo        Python 3.11 is missing or the installed version is wrong.
echo        Installing Python %PYTHON_INSTALLER_VERSION% x64...
echo.

if exist "%PYTHON_INSTALLER%" (
    del /f /q "%PYTHON_INSTALLER%" >nul 2>&1
)

echo        Downloading Python installer...

powershell.exe -NoProfile -ExecutionPolicy Bypass -Command ^
    "$ProgressPreference='SilentlyContinue'; " ^
    "Invoke-WebRequest -Uri 'https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe' -OutFile '%PYTHON_INSTALLER%'"

if errorlevel 1 (
    echo.
    echo [ERROR] Failed to download Python 3.11.9.
    echo.
    pause
    exit /b 1
)

if not exist "%PYTHON_INSTALLER%" (
    echo.
    echo [ERROR] Python installer was not downloaded.
    echo.
    pause
    exit /b 1
)

echo        Installing Python 3.11.9 for all users...

"%PYTHON_INSTALLER%" /quiet InstallAllUsers=1 ^
    PrependPath=1 ^
    Include_test=0 ^
    Include_launcher=1 ^
    Include_pip=1 ^
    Include_doc=0 ^
    Include_tcltk=1 ^
    Include_dev=1 ^
    Include_exe=1 ^
    Shortcuts=0

if errorlevel 1 (
    echo.
    echo [ERROR] Python installation failed.
    echo.
    pause
    exit /b 1
)

echo        Python installer finished.

REM ----------------------------------------------------------
REM Verify the expected installation
REM ----------------------------------------------------------

set "PYTHON_EXE=%ProgramFiles%\Python311\python.exe"

if not exist "%PYTHON_EXE%" (
    echo.
    echo [ERROR] Python 3.11 executable was not found after installation:
    echo        %PYTHON_EXE%
    echo.
    pause
    exit /b 1
)

"%PYTHON_EXE%" -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3,11) else 1)" >nul 2>&1

if errorlevel 1 (
    echo.
    echo [ERROR] Installed Python is still not Python 3.11.
    echo.
    echo Detected executable:
    echo %PYTHON_EXE%
    echo.
    pause
    exit /b 1
)

echo        Python 3.11 installation verified.
echo.

:python_found

echo.
echo        Using Python:
echo        %PYTHON_EXE%

for /f "delims=" %%V in ('"%PYTHON_EXE%" -c "import sys; print(sys.version)" 2^>nul') do (
    set "PY_VERSION_FULL=%%V"
)

echo        Version:
echo        !PY_VERSION_FULL!
echo.

REM ==========================================================
REM [3/6] VIRTUAL ENVIRONMENT
REM ==========================================================

echo [3/6] Creating virtual environment...

if exist "%INSTALL_DIR%\.venv" (
    echo        Existing .venv found.
    echo        Checking its Python version...

    if exist "%INSTALL_DIR%\.venv\Scripts\python.exe" (
        "%INSTALL_DIR%\.venv\Scripts\python.exe" -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3,11) else 1)" >nul 2>&1

        if not errorlevel 1 (
            echo        Existing .venv uses Python 3.11.
            goto venv_ready
        )

        echo        Existing .venv uses another Python version.
        echo        Recreating .venv...
    )

    rmdir /s /q "%INSTALL_DIR%\.venv"
)

"%PYTHON_EXE%" -m venv "%INSTALL_DIR%\.venv"

if errorlevel 1 (
    echo.
    echo [ERROR] Failed to create virtual environment.
    echo.
    pause
    exit /b 1
)

:venv_ready

set "VENV_PYTHON=%INSTALL_DIR%\.venv\Scripts\python.exe"
set "VENV_PYTHONW=%INSTALL_DIR%\.venv\Scripts\pythonw.exe"

if not exist "%VENV_PYTHON%" (
    echo.
    echo [ERROR] Virtual environment Python not found:
    echo        %VENV_PYTHON%
    echo.
    pause
    exit /b 1
)

if not exist "%VENV_PYTHONW%" (
    echo.
    echo [ERROR] Virtual environment pythonw.exe not found:
    echo        %VENV_PYTHONW%
    echo.
    pause
    exit /b 1
)

"%VENV_PYTHON%" -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3,11) else 1)" >nul 2>&1

if errorlevel 1 (
    echo.
    echo [ERROR] Virtual environment is not using Python 3.11.
    echo.
    pause
    exit /b 1
)

echo        .venv Python 3.11: OK
echo.

REM ==========================================================
REM [4/6] DEPENDENCIES
REM ==========================================================

echo [4/6] Installing Python dependencies...

if exist "%INSTALL_DIR%\requirements.txt" (
    "%VENV_PYTHON%" -m pip install --upgrade pip
    if errorlevel 1 (
        echo.
        echo [ERROR] Failed to upgrade pip.
        echo.
        pause
        exit /b 1
    )

    "%VENV_PYTHON%" -m pip install -r "%INSTALL_DIR%\requirements.txt"
    if errorlevel 1 (
        echo.
        echo [ERROR] Failed to install Python dependencies.
        echo.
        pause
        exit /b 1
    )
) else (
    echo        [WARNING] requirements.txt not found.
    echo        Skipping dependency installation.
)

echo.
echo        Dependencies: OK
echo.

REM ==========================================================
REM [5/6] AUDIO DEVICE CHECK
REM ==========================================================

echo [5/6] Checking audio device...

if exist "%INSTALL_DIR%\list_devices.py" (
    "%VENV_PYTHON%" "%INSTALL_DIR%\list_devices.py"

    if errorlevel 1 (
        echo.
        echo [WARNING] Audio device check returned an error.
        echo        Installation will continue.
    )
) else (
    echo        [WARNING] list_devices.py not found.
    echo        Skipping audio device check.
)

echo.

REM ==========================================================
REM [6/6] AUTOSTART
REM ==========================================================

echo [6/6] Configuring Windows autostart...

REM ----------------------------------------------------------
REM Give normal Windows users access to application directory.
REM ----------------------------------------------------------

icacls "%INSTALL_DIR%" /grant "Users:(OI)(CI)M" /T /C >nul 2>&1

REM ----------------------------------------------------------
REM IMPORTANT:
REM HKLM Run executes at logon for every interactive user.
REM This is needed because installation is performed by Admin,
REM while the seller logs in using another Windows account.
REM ----------------------------------------------------------

if not exist "%INSTALL_DIR%\autostart_client.vbs" (
    echo [ERROR] autostart_client.vbs not found.
    echo.
    pause
    exit /b 1
)

if not exist "%INSTALL_DIR%\autostart_gui.vbs" (
    echo [ERROR] autostart_gui.vbs not found.
    echo.
    pause
    exit /b 1
)

REM Remove old per-user autostart entries.
reg delete "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" ^
    /v "AudioAnalyticsClient" /f >nul 2>&1

reg delete "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" ^
    /v "AudioAnalyticsGUI" /f >nul 2>&1

REM Remove old machine entries first.
reg delete "HKLM\Software\Microsoft\Windows\CurrentVersion\Run" ^
    /v "AudioAnalyticsClient" /f >nul 2>&1

reg delete "HKLM\Software\Microsoft\Windows\CurrentVersion\Run" ^
    /v "AudioAnalyticsGUI" /f >nul 2>&1

REM ----------------------------------------------------------
REM Use cmd /c with a fully quoted command.
REM ----------------------------------------------------------

reg add "HKLM\Software\Microsoft\Windows\CurrentVersion\Run" ^
    /v "AudioAnalyticsClient" ^
    /t REG_SZ ^
    /d "wscript.exe \"%INSTALL_DIR%\autostart_client.vbs\"" ^
    /f >nul

if errorlevel 1 (
    echo [ERROR] Failed to register AudioAnalyticsClient autostart.
    echo.
    pause
    exit /b 1
)

reg add "HKLM\Software\Microsoft\Windows\CurrentVersion\Run" ^
    /v "AudioAnalyticsGUI" ^
    /t REG_SZ ^
    /d "wscript.exe \"%INSTALL_DIR%\autostart_gui.vbs\"" ^
    /f >nul

if errorlevel 1 (
    echo [ERROR] Failed to register AudioAnalyticsGUI autostart.
    echo.
    pause
    exit /b 1
)

echo        Machine-wide autostart: OK
echo.

REM ==========================================================
REM Verify registry
REM ==========================================================

echo        Registered startup entries:

reg query "HKLM\Software\Microsoft\Windows\CurrentVersion\Run" ^
    /v "AudioAnalyticsClient"

reg query "HKLM\Software\Microsoft\Windows\CurrentVersion\Run" ^
    /v "AudioAnalyticsGUI"

echo.

REM ==========================================================
REM FINISH
REM ==========================================================

echo ==========================================================
echo Installation completed successfully.
echo ==========================================================
echo.
echo Install directory:
echo   %INSTALL_DIR%
echo.
echo Python:
echo   %PYTHON_EXE%
echo.
echo Virtual environment:
echo   %INSTALL_DIR%\.venv
echo.
echo Application Python:
echo   %VENV_PYTHON%
echo.
echo GUI Python:
echo   %VENV_PYTHONW%
echo.
echo Autostart:
echo   HKLM\Software\Microsoft\Windows\CurrentVersion\Run
echo.
echo IMPORTANT:
echo   The client and GUI will start automatically when a
echo   Windows user logs in.
echo.
echo   Installation was performed as Administrator, but the
echo   application itself will run as the logged-in user.
echo.
echo ==========================================================
echo.

REM ==========================================================
REM Start immediately for the CURRENT user.
REM This is only for convenience after installation.
REM The normal seller autostart happens on next user logon.
REM ==========================================================

echo Starting Audio Analytics for the current user...
echo.

start "" wscript.exe "%INSTALL_DIR%\autostart_client.vbs"
start "" wscript.exe "%INSTALL_DIR%\autostart_gui.vbs"

echo.
echo Done.
echo.
pause
exit /b 0