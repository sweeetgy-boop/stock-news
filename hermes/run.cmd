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
rem  There is NO "--mode nightly". The nightly pipeline is a separate
rem  driver:   hermes\nightly.cmd
rem  A scheduled task was once registered as `run.cmd --mode nightly
rem  --json`; argparse rejects the choice and the task dies with exit 64,
rem  silently skipping the whole night. For the pipeline, call nightly.cmd.
rem
rem  Exit codes: 0 ok / 1 fail / 2 partial / 3 locked / 4 precondition
rem              90 cannot cd to repo / 91 python not found
rem              92 wrong Python version (3.12 required)
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
    echo [run.cmd] wrong Python: 3.12 required. 1>&2
    "%PYEXE%" %PYARG% -c "import sys;print('[run.cmd] got Python '+sys.version.split()[0]+'  '+sys.executable)" 1>&2
    echo [run.cmd] see AGENTS.md 1. 1>&2
    exit /b 92
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
