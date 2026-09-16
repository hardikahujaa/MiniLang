@echo off
REM ===========================================================================
REM  MiniLang Compiler Visualizer - one-click launcher (Windows)
REM
REM  Double-click this file, or run "run.bat" from a terminal.
REM
REM  It exists because "uvicorn app.main:app --reload" is fragile on a machine
REM  with several Python installations: this PC has a Python on PATH that
REM  belongs to Inkscape and has no pip, plus a second uvicorn belonging to the
REM  system Python. Running the wrong one gives a confusing ModuleNotFoundError.
REM  This script always uses the project's own virtual environment.
REM ===========================================================================

setlocal
cd /d "%~dp0"

set "VENV_PY=.venv\Scripts\python.exe"

echo(
echo  ============================================
echo   MiniLang Compiler Visualizer
echo  ============================================
echo(

REM --- 1. Make sure the virtual environment exists --------------------------
if not exist "%VENV_PY%" (
    echo  [setup] No virtual environment found. Creating one...

    set "BASE_PY="
    for %%P in (
        "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
        "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
        "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
    ) do (
        if not defined BASE_PY if exist %%P set "BASE_PY=%%~P"
    )

    if not defined BASE_PY (
        py -3 -c "import sys; print(sys.executable)" >"%TEMP%\mlpy.txt" 2>nul
        if not errorlevel 1 set /p BASE_PY=<"%TEMP%\mlpy.txt"
        del "%TEMP%\mlpy.txt" >nul 2>&1
    )

    if not defined BASE_PY (
        echo  [ERROR] Could not find a usable Python 3.10+ installation.
        echo          Install Python from https://www.python.org/downloads/
        echo          and make sure to tick "Add Python to PATH".
        goto :fail
    )

    echo  [setup] Using "%BASE_PY%"
    "%BASE_PY%" -m venv .venv
    if errorlevel 1 (
        echo  [ERROR] Failed to create the virtual environment.
        goto :fail
    )
)

REM --- 2. Make sure dependencies are installed ------------------------------
"%VENV_PY%" -c "import fastapi, uvicorn" >nul 2>&1
if errorlevel 1 (
    echo  [setup] Installing dependencies. This happens once...
    "%VENV_PY%" -m pip install --upgrade pip --quiet
    "%VENV_PY%" -m pip install -r requirements-dev.txt --quiet
    if errorlevel 1 (
        echo  [ERROR] Dependency installation failed.
        goto :fail
    )
    echo  [setup] Done.
)

REM --- 3. Pick a free port --------------------------------------------------
set "PORT=8000"
netstat -ano | findstr /R /C:":8000 .*LISTENING" >nul 2>&1
if not errorlevel 1 (
    echo  [warn] Port 8000 is already in use. Using 8010 instead.
    set "PORT=8010"
)

REM --- 4. Launch ------------------------------------------------------------
echo(
echo   Server starting on http://127.0.0.1:%PORT%
echo   Opening your browser...
echo(
echo   Press CTRL+C in this window to stop the server.
echo(

start "" "http://127.0.0.1:%PORT%"
"%VENV_PY%" -m uvicorn app.main:app --reload --host 127.0.0.1 --port %PORT%

goto :eof

:fail
echo(
echo  Startup failed. The message above says why.
echo(
pause
exit /b 1
