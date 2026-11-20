#!/bin/sh
# ==========================================================================
#  Nightly pipeline launcher for macOS / Linux. POSIX twin of nightly.cmd.
#
#  ASCII-only and builtins-only on purpose. Hermes cron and launchd start
#  jobs with a minimal PATH; this file must not depend on anything outside
#  the shell itself (no dirname, no readlink, no env). Korean docs live in
#  AGENTS.md and nightly.py.
#
#  This file only resolves the interpreter and hands off. All decisions
#  (holiday check, lock, done-marker, per-step exit codes, timing, telegram)
#  are in nightly.py -- keep it that way, exactly as nightly.cmd does.
#
#  Telegram: nightly.py passes --dry-run to sending modes unless
#  STOCKNEWS_SEND=1 is in the environment. This file never sets it.
#
#  Exit codes (from nightly.py):
#      0  ok, or holiday skip
#      1  pipeline failure
#      2  partial -- one or more steps failed but the run completed
#      3  already running (lock held)
#     90  cannot cd to repo
#     91  python not found
#     92  wrong Python version (3.12 required)
#
#  Log: logs/nightly_YYYYMMDD.log  (per job: logs/<job>_YYYYMMDD.log)
# ==========================================================================

case "$0" in
    */*) _SD=${0%/*} ;;
    *)   _SD=. ;;
esac
REPO=$(CDPATH= cd "$_SD/.." 2>/dev/null && pwd -P)
if [ -z "$REPO" ] || ! cd "$REPO" 2>/dev/null; then
    echo "[nightly] cannot cd to repo: $_SD/.." 1>&2
    exit 90
fi

# Entry marker. run_screen.py logs a warning when this is absent, so we
# can tell later who bypassed the pipeline and called a mode directly.
STOCKNEWS_ENTRY=nightly
PYTHONUTF8=1
PYTHONIOENCODING=utf-8
PYTHONDONTWRITEBYTECODE=1
export STOCKNEWS_ENTRY PYTHONUTF8 PYTHONIOENCODING PYTHONDONTWRITEBYTECODE

# --- resolve interpreter (same order as hermes/run.sh and nightly.cmd) ---
#  repo venv first, then a versioned python on PATH. 313 before 312 is
#  deliberate: a stray newer install must trip the guard below (exit 92),
#  not get skipped silently.
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
    echo "[nightly] python not found." 1>&2
    exit 91
fi

# --- interpreter version guard: must be Python 3.12 ---
#  Same rule and same reason as nightly.cmd (AGENTS.md 8-4). A wrapper that
#  hands work to the wrong interpreter is worse than one that refuses to
#  start. Refuse.
"$PYEXE" -c "import sys;raise SystemExit(0 if sys.version_info[:2]==(3,12) else 92)" >/dev/null 2>&1
if [ $? -ne 0 ]; then
    echo "[nightly] wrong Python: 3.12 required." 1>&2
    "$PYEXE" -c "import sys;print('[nightly] got Python '+sys.version.split()[0]+'  '+sys.executable)" 1>&2
    echo "[nightly] see AGENTS.md 1." 1>&2
    exit 92
fi

# No exec: keep the same process shape as `cmd /c nightly.cmd` (wrapper is
# the parent, python is the child, exit code passed through).
"$PYEXE" nightly.py "$@"
exit $?
