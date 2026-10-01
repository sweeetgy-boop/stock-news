# -*- coding: utf-8 -*-
"""Hermes cron -> stock-news 잡 드라이버. LLM 없음. 표준 라이브러리만.

이 파일은 저장소의 **정본**이고, 실제로 도는 사본은
`%LOCALAPPDATA%\\hermes\\scripts\\stocknews_<잡>.py` 에 있습니다.
Hermes 의 `--script` 는 그 디렉터리 아래만 받습니다. 고칠 때는 정본과
사본 전부를 같이 고치십시오.

잡 이름은 **파일명에서 나옵니다.**

    stocknews_news.py           -> --job news
    stocknews_brief_morning.py  -> --job brief-morning
    stocknews_flash.py          -> --job flash

그래서 사본 5개가 전부 같은 바이트입니다. 파일마다 다른 상수를 박아두면
하나만 고치고 나머지를 잊습니다 — 2026-09-07 에 정본/사본이 갈라진 것이
그런 식이었습니다.

왜 --no-agent 인가
-----------------
Hermes cron 은 잡을 두 형태로 돌립니다. LLM 에이전트에게 프롬프트를 주는
방식과, 스크립트를 그냥 실행하고 stdout 만 전달하는 방식(--no-agent).
여기서는 후자를 씁니다. 에이전트 잡으로 두면 '재시도하지 마라 / 수동
재실행하지 마라 / --force 쓰지 마라' 를 **프롬프트로 부탁**하는 구조가
됩니다. 2026-09-07 밤에 확인된 것은 그 부탁이 지켜지지 않는다는
사실입니다. LLM 을 경로에서 빼면 그 판단 자체가 존재하지 않습니다.

계약
----
- `nightly.cmd --job <잡>` 을 **정확히 1회** 호출한다.
- 종료 코드가 무엇이든 재시도하지 않는다. 다음 기회는 다음 트리거다.
- `--force` 를 붙이지 않는다. 완주 마커를 우회하는 유일한 수단이므로,
  자동 경로에서는 절대 쓰지 않는다.
- 알림은 `nightly.py` 가 직접 텔레그램으로 보낸다(완주 · 스킵 · 실패).
  그래서 이 스크립트는 stdout 을 거의 쓰지 않는다 — 잡은 `--deliver
  local` 로 등록돼 있고, 여기서 떠들면 같은 내용이 두 번 나갑니다.
"""
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

REPO = r"C:\Users\sweee\Documents\stock-news"
LAUNCHER = REPO + r"\hermes\nightly.cmd"

# 잡별 시간 상한(초). '이건 매달린 것이다' 를 뜻하는 값이지 정상 상한이
# 아니다. nightly 실측 최악은 backfill 이 끼지 않는 한 40분 안쪽이고,
# 단일 모드 잡은 훨씬 짧다.
TIMEOUT_SEC = {
    "nightly": 10800,
    "news": 1800,
    "brief-morning": 1800,
    "brief-evening": 1800,
    "weekly": 1800,
    "flash": 900,
}
DEFAULT_TIMEOUT = 1800

# Hermes 런타임이 자식으로 흘리는 변수. 반드시 지우고 넘긴다.
#
# Hermes 는 이 스크립트를 자기 번들 파이썬(cpython-3.11)으로 돌리면서
# PYTHONPATH 에 자기 site-packages 를 얹는다. 그 환경 그대로 래퍼를
# 띄우면, cmd 가 고른 Python 3.12 가 3.11 용으로 빌드된 numpy 바이너리를
# 집어 든다. 2026-09-07 실측 오류:
#
#   Importing the numpy C-extensions failed.
#     * _multiarray_umath.cp311-win_amd64.pyd
#   The Python version is: Python 3.12 ...
#
# 같은 명령이 사람 셸에서는 멀쩡히 돌기 때문에 원인이 잘 안 보인다.
# 파이프라인은 자기 인터프리터를 스스로 고른다(nightly.cmd 가 3.12 인지
# 확인하고 아니면 exit 92). 부모의 파이썬 환경은 물려주면 안 된다.
_LEAKY = ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONEXECUTABLE",
          "VIRTUAL_ENV", "CONDA_PREFIX", "PYTHONNOUSERSITE")


def job_name() -> str:
    """파일명에서 잡 이름을 뽑는다. stocknews_brief_morning -> brief-morning."""
    stem = Path(__file__).stem
    for prefix in ("stocknews_", "cron_"):
        if stem.startswith(prefix):
            stem = stem[len(prefix):]
            break
    return stem.replace("_", "-")


def _clean_env() -> dict:
    return {k: v for k, v in os.environ.items() if k not in _LEAKY}


def main() -> int:
    job = job_name()
    if job in ("stocknews", ""):
        # 정본을 그대로 돌린 경우다. 사본은 파일명에 잡이 들어 있다.
        print("이 파일은 정본입니다. ~/.hermes/scripts/stocknews_<잡>.py "
              "사본으로 등록해 쓰십시오.", flush=True)
        return 64

    started = datetime.now()
    dropped = [k for k in _LEAKY if k in os.environ]
    if dropped:
        print(f"부모 파이썬 환경 제거: {', '.join(dropped)}", flush=True)
    try:
        proc = subprocess.run(
            ["cmd", "/c", LAUNCHER, "--job", job],
            cwd=REPO,
            capture_output=True,
            timeout=TIMEOUT_SEC.get(job, DEFAULT_TIMEOUT),
            env=_clean_env(),
        )
        rc = proc.returncode
    except subprocess.TimeoutExpired:
        # 죽이고 끝낸다. 재시도하지 않는다.
        print(f"{job} 시간 초과 "
              f"({TIMEOUT_SEC.get(job, DEFAULT_TIMEOUT)}초) — 종료", flush=True)
        return 1
    except Exception as exc:                          # noqa: BLE001
        print(f"{job} 기동 실패: {type(exc).__name__}: {exc}", flush=True)
        return 1

    mins = (datetime.now() - started).total_seconds() / 60.0
    # 정상 경로는 한 줄만 남긴다. 상세는 logs\<잡>_YYYYMMDD.log 와 DB
    # runs 테이블에 있고, 사람에게 가는 알림은 nightly.py 가 이미 보냈다.
    print(f"{job} rc={rc} · {mins:.0f}분", flush=True)

    # 실패했으면 자식의 출력을 그대로 올린다. 처음 판에서는 이걸 버렸는데,
    # Hermes 에서 rc=1 이 났을 때 원인을 볼 방법이 없었다. 진단을 삼키는
    # 래퍼는 없느니만 못하다. 92 는 인터프리터 가드다 — 반드시 보여야 한다.
    if rc not in (0, 2, 3):
        for label, blob in (("stderr", proc.stderr), ("stdout", proc.stdout)):
            text = (blob or b"").decode("utf-8", "replace").strip()
            if text:
                tail = text.splitlines()[-25:]
                print(f"--- {label} (마지막 {len(tail)}줄) ---", flush=True)
                for line in tail:
                    print(line, flush=True)
    return 0 if rc in (0, 2, 3) else rc


if __name__ == "__main__":
    sys.exit(main())
