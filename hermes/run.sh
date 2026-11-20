#!/bin/sh
# ==========================================================================
#  Hermes launcher for macOS / Linux. POSIX twin of run.cmd.
#
#  ASCII-only and builtins-only on purpose (minimal PATH under Hermes cron
#  and launchd). Korean docs live in AGENTS.md.
#
#  What it fixes:
#    1. PATH "python3" is the system stub (3.9 on macOS) or whatever venv
#       the caller happens to have active. We resolve the repo interpreter
#       explicitly.
#    2. PYTHONUTF8=1 keeps Korean output UTF-8 regardless of locale.
#    3. Relative paths (data/quant.db) require cwd = repo root.
#
#  Usage:
#     hermes/run.sh --mode daily
#     hermes/run.sh --mode flash --json
#     hermes/run.sh smoke
#     hermes/run.sh verify
#
#  There is NO "--mode nightly". The nightly pipeline is a separate
#  driver:   hermes/nightly.sh
#
#  Exit codes: 0 ok / 1 fail / 2 partial / 3 locked / 4 precondition
#              64 usage / 90 cannot cd to repo / 91 python not found
#              92 wrong Python version (3.12 required)
# ==========================================================================

case "$0" in
    */*) _SD=${0%/*} ;;
    *)   _SD=. ;;
esac
REPO=$(CDPATH= cd "$_SD/.." 2>/dev/null && pwd -P)
if [ -z "$REPO" ] || ! cd "$REPO" 2>/dev/null; then
    echo "[run.sh] cannot cd to repo: $_SD/.." 1>&2
    exit 90
fi

PYTHONUTF8=1
PYTHONIOENCODING=utf-8
PYTHONDONTWRITEBYTECODE=1
export PYTHONUTF8 PYTHONIOENCODING PYTHONDONTWRITEBYTECODE

# --- resolve interpreter (same order as hermes/nightly.sh and run.cmd) ---
PYEXE=
if [ -x "$REPO/venv/bin/python" ]; then
    PYEXE="$REPO/venv/bin/python"
elif [ -x "$REPO/.venv/bin/python" ]; then
    PYEXE="$REPO/.venv/bin/python"
else
    for _c in python3.13 python3.12 python3.11 python3; do
        _p=$(command -v "$_c" 2>/dev/null)
        if [ -n "$_p" ]; then
            PYEXE=$_p
            break
        fi
    done
fi

if [ -z "$PYEXE" ]; then
    echo "[run.sh] python not found." 1>&2
    echo "[run.sh] create the repo venv: uv venv --python 3.12 venv" 1>&2
    exit 91
fi

# --- interpreter version guard: must be Python 3.12 ---
#  Same rule and same reason as run.cmd (AGENTS.md 8-4).
"$PYEXE" -c "import sys;raise SystemExit(0 if sys.version_info[:2]==(3,12) else 92)" >/dev/null 2>&1
if [ $? -ne 0 ]; then
    echo "[run.sh] wrong Python: 3.12 required." 1>&2
    "$PYEXE" -c "import sys;print('[run.sh] got Python '+sys.version.split()[0]+'  '+sys.executable)" 1>&2
    echo "[run.sh] see AGENTS.md 1." 1>&2
    exit 92
fi

case "$1" in
    smoke|SMOKE|Smoke)
        shift
        "$PYEXE" smoke_test.py "$@"
        exit $?
        ;;
    verify|VERIFY|Verify)
        "$PYEXE" verify_env.py
        exit $?
        ;;
esac

"$PYEXE" run_screen.py "$@"
exit $?
