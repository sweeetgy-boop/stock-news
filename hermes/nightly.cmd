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
rem     92  wrong Python version (3.12 required)
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

rem --- interpreter version guard: must be Python 3.12 ---
rem
rem  On 2026-09-07 an agent ran verify_env.py with the Hermes bundled venv
rem  (Python 3.11), got "34/34 pins match" against that wrong environment,
rem  and lowered the numpy pin 2.5.2 -> 2.4.6 to fit it. numpy 2.5.x needs
rem  Python >= 3.12, so the "passing" check was self-consistent and wrong.
rem  A wrapper that hands work to the wrong interpreter is worse than one
rem  that refuses to start. Refuse.
rem
rem  Note the resolution order above still prefers 313 then 312. If you
rem  install another minor version, this guard fires instead of silently
rem  running on it -- bump REQUIRED_PY in verify_env.py and this check
rem  together with requirements.txt.
"%PYEXE%" %PYARG% -c "import sys;raise SystemExit(0 if sys.version_info[:2]==(3,12) else 92)" >nul 2>&1
if errorlevel 1 (
    echo [nightly] wrong Python: 3.12 required. 1>&2
    "%PYEXE%" %PYARG% -c "import sys;print('[nightly] got Python '+sys.version.split()[0]+'  '+sys.executable)" 1>&2
    echo [nightly] see AGENTS.md 1. 1>&2
    exit /b 92
)

"%PYEXE%" %PYARG% nightly.py %*
exit /b %errorlevel%
