@echo off
rem ==========================================================================
rem  After-close batch chain (KST). Registered in Windows Task Scheduler.
rem
rem  ASCII-only on purpose. cmd.exe reads a batch file with the *current*
rem  codepage; non-ASCII bytes here can desync the parser. Korean docs live
rem  in AGENTS.md.
rem
rem  Why one sequential wrapper instead of five separate tasks:
rem    master / update / flags / daily / exits are all WRITE modes and take a
rem    file lock. Five tasks at fixed clock times can overlap - flags alone
rem    can run 5-10 minutes - and the loser exits 3 (locked). Running them
rem    in order removes that race entirely and keeps the dependency order
rem    required by AGENTS.md ch.3 (master -> update -> flags -> daily -> exits).
rem
rem  Telegram: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID are not set yet, so the
rem  sending modes would end with exit 4. We pass --dry-run until they are.
rem  To switch to real sending, set an environment variable:
rem      setx STOCKNEWS_SEND 1
rem  No need to edit this file.
rem
rem  Holidays/weekends are safe: every mode self-skips with exit 0 and still
rem  records a row in the runs table (AGENTS.md ch.8-2).
rem
rem  Log: logs\chain.log  (overwritten each run; history lives in the DB
rem  runs table, readable via --mode runs)
rem ==========================================================================
setlocal EnableExtensions

set "_SD=%~dp0"
for %%I in ("%_SD%..") do set "REPO=%%~fI"
cd /d "%REPO%" 2>nul
if errorlevel 1 (
    echo [chain] cannot cd to repo: %REPO% 1>&2
    exit /b 90
)
if not exist "logs" mkdir "logs"

set "LOG=%REPO%\logs\chain.log"
set "RUN=%_SD%run.cmd"

rem --dry-run unless the user opted into real sending.
set "DRY=--dry-run"
if "%STOCKNEWS_SEND%"=="1" set "DRY="

rem Wait for a stale lock instead of failing with exit 3.
set "WAIT=--lock-wait 600"

echo ============================================================ > "%LOG%"
echo [chain] start %DATE% %TIME%>> "%LOG%"
echo [chain] send mode: %DRY% (empty = real telegram)>> "%LOG%"

echo.>> "%LOG%"
echo ---- master ---->> "%LOG%"
call "%RUN%" --mode master %WAIT% --json >> "%LOG%" 2>&1
echo [chain] master rc=%ERRORLEVEL%>> "%LOG%"

echo.>> "%LOG%"
echo ---- update ---->> "%LOG%"
call "%RUN%" --mode update %WAIT% --json >> "%LOG%" 2>&1
echo [chain] update rc=%ERRORLEVEL%>> "%LOG%"

echo.>> "%LOG%"
echo ---- flags ---->> "%LOG%"
call "%RUN%" --mode flags --dart-limit 400 %WAIT% --json >> "%LOG%" 2>&1
echo [chain] flags rc=%ERRORLEVEL%>> "%LOG%"

echo.>> "%LOG%"
echo ---- daily ---->> "%LOG%"
call "%RUN%" --mode daily %DRY% %WAIT% --json >> "%LOG%" 2>&1
echo [chain] daily rc=%ERRORLEVEL%>> "%LOG%"

echo.>> "%LOG%"
echo ---- exits ---->> "%LOG%"
call "%RUN%" --mode exits %DRY% %WAIT% --json >> "%LOG%" 2>&1
echo [chain] exits rc=%ERRORLEVEL%>> "%LOG%"

rem Read-only. Reports schedule gaps as exit 2 - that is the signal that the
rem PC slept or the task did not fire.
echo.>> "%LOG%"
echo ---- runs ---->> "%LOG%"
call "%RUN%" --mode runs --json >> "%LOG%" 2>&1
set "RC_RUNS=%ERRORLEVEL%"
echo [chain] runs rc=%RC_RUNS%>> "%LOG%"

echo.>> "%LOG%"
echo [chain] done %DATE% %TIME%>> "%LOG%"
exit /b %RC_RUNS%
