@echo off
rem ==========================================================================
rem  First full pipeline run on real data.
rem
rem  ASCII-only on purpose (same reason as run.cmd: cmd.exe parses batch files
rem  with the current codepage and loses sync on non-ASCII bytes).
rem
rem  Runs, in order:
rem     backfill  (resumable; safe to re-run until remaining = 0)
rem     flags     (DART: admin issues / capital impairment / offerings)
rem     daily     (full scan + top 10)          -- dry-run, no Telegram needed
rem     exits     (position exit signals)       -- dry-run
rem     kiwoom-plan --write-targets             (pick credit targets)
rem     credit-kiwoom                           (Kiwoom REST ka10013)
rem     runs      (batch history + schedule gaps)
rem
rem  Each step writes its JSON to logs\<step>.json and its log to
rem  logs\<step>.log so results survive a dead terminal.
rem
rem  Telegram tokens are not set, so daily/exits use --dry-run. Drop the
rem  DRY variable below once TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID are filled.
rem
rem  Usage:  hermes\first_run.cmd
rem ==========================================================================
setlocal EnableExtensions

set "_SD=%~dp0"
for %%I in ("%_SD%..") do set "REPO=%%~fI"
cd /d "%REPO%" 2>nul
if errorlevel 1 (
    echo [first_run] cannot cd to repo: %REPO% 1>&2
    exit /b 90
)

set "LOGS=%REPO%\logs"
if not exist "%LOGS%" mkdir "%LOGS%"

set "RUN=%REPO%\hermes\run.cmd"
set "DRY=--dry-run"

rem Wait for the job lock instead of failing with exit 3. Without this the
rem whole chain dies the moment another job (or a leftover backfill) holds
rem the lock -- which is exactly what happened on the first attempt.
set "WAIT=--lock-wait 1800"

echo [first_run] start %DATE% %TIME%

rem --- backfill: repeat until remaining = 0 (checkpointed, resumes) ---
for /L %%N in (1,1,8) do (
    echo [first_run] backfill pass %%N
    call "%RUN%" --mode backfill %WAIT% --json  > "%LOGS%\backfill.json" 2> "%LOGS%\backfill.log"
    findstr /R /C:"\"remaining\":[ ]*0[,}]" "%LOGS%\backfill.json" >nul 2>&1
    if not errorlevel 1 goto :bf_done
)
echo [first_run] WARNING backfill still has work left after 8 passes
:bf_done
echo [first_run] backfill done

call :step flags        --mode flags --dart-limit 400 %WAIT% --json
call :step daily        --mode daily %DRY% %WAIT% --json
call :step exits        --mode exits %DRY% %WAIT% --json
call :step kiwoomplan   --mode kiwoom-plan --write-targets data/kiwoom_targets.txt --json
call :step creditkiwoom --mode credit-kiwoom %WAIT% --json
call :step runs         --mode runs --runs-days 1 --json

echo [first_run] all steps finished %DATE% %TIME%
echo [first_run] results in %LOGS%
exit /b 0

:step
set "NAME=%1"
shift
set "ARGS="
:collect
if "%1"=="" goto :fire
set "ARGS=%ARGS% %1"
shift
goto :collect
:fire
echo [first_run] %NAME% ...
call "%RUN%" %ARGS% > "%LOGS%\%NAME%.json" 2> "%LOGS%\%NAME%.log"
echo [first_run] %NAME% exit=%errorlevel%
exit /b 0
