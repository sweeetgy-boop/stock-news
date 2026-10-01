# -*- coding: utf-8 -*-
"""Hermes cron -> stock-news nightly 파이프라인. LLM 없음.

이 파일은 저장소의 정본이고, 실제로 도는 사본은
`%LOCALAPPDATA%\\hermes\\scripts\\stocknews_nightly.py` 에 있습니다.
Hermes 의 `--script` 는 `~/.hermes/scripts/` 아래만 받습니다. 고칠 때는
양쪽을 같이 고치십시오.

왜 --no-agent 인가
-----------------
Hermes cron 은 잡을 두 형태로 돌립니다. LLM 에이전트에게 프롬프트를 주는
방식과, 스크립트를 그냥 실행하고 stdout 만 전달하는 방식(--no-agent).
여기서는 후자를 씁니다.

에이전트 잡으로 두면 '재시도하지 마라 / 수동 재실행하지 마라 /
--force 쓰지 마라' 를 **프롬프트로 부탁**하는 구조가 됩니다. 2026-09-07
밤에 확인된 것은 그 부탁이 지켜지지 않는다는 사실입니다. 작업 스케줄러를
비우고 Hermes 를 단독 주체로 바꾼 직후에도 개별 모드가 직접 호출됐습니다
(23:04 news / 23:05 brief-evening / 23:06 daily / 23:07 flash).

LLM 을 경로에서 빼면 그 판단 자체가 존재하지 않습니다. 부탁이 아니라
구조로 막습니다.

계약
----
- `nightly.cmd` 를 **정확히 1회** 호출한다.
- 종료 코드가 무엇이든 재시도하지 않는다. 다음 기회는 내일 21:30 이다.
- `--force` 를 붙이지 않는다. 완주 마커를 우회하는 유일한 수단이므로,
  자동 경로에서는 절대 쓰지 않는다.
- 알림은 `nightly.py` 가 직접 텔레그램으로 보낸다(완주 · 스킵 · 실패).
  그래서 이 스크립트는 stdout 을 거의 쓰지 않는다 — 잡은 `--deliver local`
  로 등록돼 있고, 여기서 떠들면 같은 내용이 두 번 나간다.
"""
import os
import subprocess
import sys
from datetime import datetime

NIGHTLY = r"C:\Users\sweee\Documents\stock-news\hermes\nightly.cmd"
REPO = r"C:\Users\sweee\Documents\stock-news"

# nightly 실측 최악은 backfill 이 끼지 않는 한 40분 안쪽이다. 3시간은
# '이건 매달린 것이다' 를 뜻하는 값이지 정상 상한이 아니다.
TIMEOUT_SEC = 10800


# Hermes 런타임이 자식으로 흘리는 변수. 반드시 지우고 넘긴다.
#
# Hermes 는 이 스크립트를 자기 번들 파이썬(cpython-3.11)으로 돌리면서
# PYTHONPATH 에 자기 site-packages 를 얹는다. 그 환경 그대로
# nightly.cmd 를 띄우면, cmd 가 고른 Python 3.12 가 3.11 용으로 빌드된
# numpy 바이너리를 집어 든다. 2026-09-07 실측 오류:
#
#   Importing the numpy C-extensions failed.
#     * _multiarray_umath.cp311-win_amd64.pyd
#   The Python version is: Python 3.12 ...
#   Original error was: No module named 'numpy._core._multiarray_umath'
#
# 같은 명령이 사람 셸에서는 멀쩡히 돌기 때문에 원인이 잘 안 보인다.
# 파이프라인은 자기 인터프리터를 스스로 고른다(nightly.cmd). 부모의
# 파이썬 환경은 물려주면 안 된다.
_LEAKY = ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONEXECUTABLE",
          "VIRTUAL_ENV", "CONDA_PREFIX", "PYTHONNOUSERSITE")


def _clean_env() -> dict:
    env = {k: v for k, v in os.environ.items() if k not in _LEAKY}
    return env


def main() -> int:
    started = datetime.now()
    dropped = [k for k in _LEAKY if k in os.environ]
    if dropped:
        print(f"부모 파이썬 환경 제거: {', '.join(dropped)}", flush=True)
    try:
        proc = subprocess.run(
            ["cmd", "/c", NIGHTLY],
            cwd=REPO,
            capture_output=True,
            timeout=TIMEOUT_SEC,
            env=_clean_env(),
        )
        rc = proc.returncode
    except subprocess.TimeoutExpired:
        # 죽이고 끝낸다. 재시도하지 않는다.
        print(f"nightly 시간 초과 ({TIMEOUT_SEC}초) — 종료", flush=True)
        return 1
    except Exception as exc:                          # noqa: BLE001
        print(f"nightly 기동 실패: {type(exc).__name__}: {exc}", flush=True)
        return 1

    mins = (datetime.now() - started).total_seconds() / 60.0
    # 정상 경로는 한 줄만 남긴다. 상세는 logs\nightly_YYYYMMDD.log 와 DB
    # runs 테이블에 있고, 사람에게 가는 알림은 nightly.py 가 이미 보냈다.
    print(f"nightly rc={rc} · {mins:.0f}분", flush=True)

    # 실패했으면 자식의 출력을 그대로 올린다. 처음 판에서는 이걸 버렸는데,
    # Hermes 에서 rc=1 이 났을 때 원인을 볼 방법이 없었다. 진단을 삼키는
    # 래퍼는 없느니만 못하다.
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
