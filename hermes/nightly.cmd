@echo off
rem ==========================================================================
rem  Nightly pipeline launcher. Registered in Windows Task Scheduler (21:30).
rem
rem  ASCII-only on purpose. cmd.exe reads a batch file with the *current*
rem  codepage; non-ASCII bytes here can desync the parser. Korean docs live
rem  in AGENTS.md and nightly.py.
rem
rem  This file only resolves the interpreter and hands off. All decisions
rem  (holiday check, lock, per-step exit codes, timing, telegram) are in
rem  nightly.py -- batch cannot do them without silently getting them wrong
rem  (%ERRORLEVEL% delayed expansion, locale-dependent %DATE%).
rem
rem  Telegram: passes --dry-run to sending modes unless STOCKNEWS_SEND=1.
rem      setx STOCKNEWS_SEND 1
rem
rem  Exit codes (from nightly.py):
rem      0  ok, or holiday skip
rem      1  pipeline failure
rem      2  partial -- one or more steps failed but the run completed
rem      3  already running (lock held)
rem     90  cannot cd to repo
rem     91  python not found
rem
rem  Log: logs\nightly_YYYYMMDD.log  (appended)
rem ==========================================================================
setlocal EnableExtensions

set "_SD=%~dp0"
for %%I in ("%_SD%..") do set "REPO=%%~fI"
cd /d "%REPO%" 2>nul
if errorlevel 1 (
    echo [nightly] cannot cd to repo: %REPO% 1>&2
    exit /b 90
)

rem Entry marker. run_screen.py logs a warning when this is absent, so we
rem can tell later who bypassed the pipeline and called a mode directly.
set "STOCKNEWS_ENTRY=nightly"

set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PYTHONDONTWRITEBYTECODE=1"

rem --- resolve interpreter (same order as hermes\run.cmd) ---
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
    echo [nightly] python not found. 1>&2
    exit /b 91
)

"%PYEXE%" %PYARG% nightly.py %*
exit /b %errorlevel%
