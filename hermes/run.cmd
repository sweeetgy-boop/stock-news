@echo off
rem ==========================================================================
rem  Hermes launcher for Windows.
rem
rem  This file is deliberately ASCII-only and does NOT call chcp.
rem  Reason: cmd.exe reads a batch file using the *current* codepage. If the
rem  file contains non-ASCII bytes and the script changes the codepage
rem  mid-run, the parser loses sync and starts executing comment text as
rem  commands. Keep this launcher ASCII. Korean docs live in AGENTS.md.
rem
rem  What it fixes:
rem    1. PATH "python" points at the Microsoft Store stub
rem       (WindowsApps\python.exe), which does nothing when run.
rem       We resolve a real interpreter explicitly.
rem    2. PYTHONUTF8=1 makes Python emit UTF-8 regardless of console codepage,
rem       so Korean output is not mangled when the agent captures stdout.
rem    3. Relative paths (data/quant.db) require cwd = repo root.
rem
rem  Usage:
rem     hermes\run.cmd --mode daily
rem     hermes\run.cmd --mode flash --json
rem     hermes\run.cmd smoke
rem     hermes\run.cmd verify
rem
rem  Exit codes: 0 ok / 1 fail / 2 partial / 3 locked / 4 precondition
rem              90 cannot cd to repo / 91 python not found
rem ==========================================================================
setlocal EnableExtensions

set "_SD=%~dp0"
for %%I in ("%_SD%..") do set "REPO=%%~fI"
cd /d "%REPO%" 2>nul
if errorlevel 1 (
    echo [run.cmd] cannot cd to repo: %REPO% 1>&2
    exit /b 90
)

set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PYTHONDONTWRITEBYTECODE=1"

rem --- resolve interpreter (PYEXE + optional PYARG for the py launcher) ---
set "PYEXE="
set "PYARG="
if exist "%REPO%\venv\Scripts\python.exe"  set "PYEXE=%REPO%\venv\Scripts\python.exe"
if not defined PYEXE if exist "%REPO%\.venv\Scripts\python.exe" set "PYEXE=%REPO%\.venv\Scripts\python.exe"
if not defined PYEXE if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" set "PYEXE=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if not defined PYEXE if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "PYEXE=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not defined PYEXE if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" set "PYEXE=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
if not defined PYEXE if exist "%ProgramFiles%\Python312\python.exe" set "PYEXE=%ProgramFiles%\Python312\python.exe"

if not defined PYEXE (
    where py >nul 2>&1
    if not errorlevel 1 (
        set "PYEXE=py"
        set "PYARG=-3"
    )
)

if not defined PYEXE (
    echo [run.cmd] python not found. 1>&2
    echo [run.cmd] install: winget install --id Python.Python.3.12 --scope user 1>&2
    exit /b 91
)

if /I "%~1"=="smoke"  goto :smoke
if /I "%~1"=="verify" goto :verify

"%PYEXE%" %PYARG% run_screen.py %*
exit /b %errorlevel%

:smoke
"%PYEXE%" %PYARG% smoke_test.py %2 %3 %4
exit /b %errorlevel%

:verify
"%PYEXE%" %PYARG% verify_env.py
exit /b %errorlevel%
