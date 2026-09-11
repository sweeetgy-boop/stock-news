# -*- coding: utf-8 -*-
"""매일 밤 자동 수집 파이프라인.

왜 배치 파일이 아니라 파이썬인가
------------------------------
요구사항이 휴장일 판정(`trading_day` 재사용), 만료 있는 락, 단계별 소요
시간, 실패 목록 집계, 텔레그램 알림이다. cmd 배치로 하면 `%ERRORLEVEL%`
지연 확장과 날짜 포맷 로케일에 걸려 조용히 틀린다. 런처만 얇은 `.cmd`
(`hermes/nightly.cmd`) 로 두고 판단은 전부 여기서 한다.

설계 원칙 넷
-----------
  1) **한 단계가 실패해도 다음 단계는 돈다.** 순서 의존이 있는 구간
     (master -> update -> flags -> daily)도 마찬가지다. 앞이 실패하면
     뒤가 exit 4 로 끝나겠지만, 그 사실이 로그에 남는 게 중단보다 낫다.
     중단하면 '어디까지 됐는지' 를 다음날 사람이 추적해야 한다.
  2) **각 단계는 별도 프로세스다.** 모드 하나가 예외로 죽어도 드라이버는
     산다. 종료 코드가 계약이므로(AGENTS 2장) 그걸 그대로 읽는다.
  3) **조용히 죽지 않는다.** 파이프라인 자체가 시작조차 못 해도 알림이
     나가야 한다. 그래서 최상위를 try/except 로 감싸고 실패 알림을 낸다.
  4) **이중 실행 금지.** Hermes 이중 트리거로 브리핑이 두 번 나간 적이
     있다. 락이 있으면 즉시 종료한다.

종료 코드
--------
  0   정상 (휴장일 스킵, '오늘 이미 실행' 스킵도 0)
  1   파이프라인 자체 실패 (락 획득 실패는 0 이 아니라 3)
  2   부분 실패 — 한 단계 이상 실패했으나 완주함
  3   이중 실행 (락 점유 중)

이 파일이 유일한 진입점이다
--------------------------
2026-09-07 까지는 `hermes/afterclose.cmd` 가 같은 단계들(master -> update
-> flags -> daily -> exits)을 따로 돌리는 두 번째 진입점이었다. 둘 다
작업 스케줄러에 등록돼 있어서, catch-up 으로 동시에 뜨면 서로의 락을 물고
늘어졌다. 그날 아침 update 가 12.6분 대기 끝에 rc=3 으로 죽어 exit 2 가
났고, 장중에 돌아버린 update 들이 09-02/03/04 시세를 부분 적재로 만들었다.
afterclose 는 스케줄에서 내렸다. 파이프라인 진입점을 늘리지 마십시오.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))

from stocknews.env import load_env                          # noqa: E402
from stocknews.joblock import JobLock                      # noqa: E402
from stocknews.store import Store                          # noqa: E402
from stocknews.trading_day import (market_status, now_kst,  # noqa: E402
                                   CLOSED_HOLIDAY, CLOSED_WEEKEND)

# 락 만료. 요구사항대로 2시간이다. 전 단계 합계가 이보다 길면 정상
# 실행 중인 잡의 락을 다음 트리거가 빼앗는다 — 실측 최악은 backfill 이
# 끼지 않는 한 40분 안쪽이다.
LOCK_TIMEOUT_SEC = 7200

# 자식 단계의 락 대기(초). 0 = 기다리지 않고 즉시 exit 3.
#
# 예전 값은 600 이었다. 의도는 '잔류 락에 걸려 죽지 말자' 였는데 실제로는
# 정반대로 작동했다. 2026-09-07 09:02, afterclose 와 nightly 가 catch-up 으로
# 동시에 뜨면서 update 단계가 12.6분을 기다린 끝에 rc=3 으로 죽었고
# 파이프라인이 exit 2 가 됐다. 기다려서 얻은 것이 없다.
#
# 중복 진입점을 없앤 지금, 락이 잡혀 있다는 건 '다른 잡이 정말로 돌고 있다'는
# 뜻이다. 그러면 기다릴 게 아니라 즉시 비켜야 한다. 잔류(만료) 락은
# JobLock.acquire 가 대기 없이도 회수하므로 원래 의도했던 방어는 그대로다.
LOCK_WAIT = "0"

# 텔레그램을 실제로 보내는 모드. 토큰이 없으면 exit 4 로 끝나므로
# STOCKNEWS_SEND=1 이 아닐 때는 --dry-run 을 붙인다.
_SENDING = {"news", "daily", "exits", "brief-morning", "brief-evening",
            "weekly", "brief-weekly", "flash"}

# 잡별 dry-run. STOCKNEWS_SEND=1 이어도 여기 적힌 잡은 --dry-run 으로 돈다.
#
#   setx STOCKNEWS_DRY_JOBS flash          (콤마 구분: "flash,weekly")
#
# 왜 필요한가: STOCKNEWS_SEND 는 전역이라 flash 를 첫날 로그만 보고 싶어도
# 그날 nightly·저녁 브리핑까지 같이 dry-run 이 된다. 잡 하나만 끄는 스위치가
# 없어서 flash 를 켜지 못하고 있었다 (2026-09-09).
DRY_JOBS_ENV = "STOCKNEWS_DRY_JOBS"

# flash 에 추가로 넘길 인자. 켤 때 결정한다 — 기본은 아무것도 안 넘긴다.
#
#   ("--no-update",)   당일 시세 적재를 건너뛰고 전일 봉으로 스캔한다.
#                      KRX 전종목 스냅샷이 404 인 동안 fetch_day 가 종목별
#                      폴백(2,400종목 x 0.3s ≈ 12~22분)으로 떨어져 cron
#                      타임아웃 900초를 넘긴다. 이걸 끄면 회차당 30초 안팎.
#                      대신 '장중 즉시 속보'가 전일 종가 기준이 된다.
# 이 값을 바꾸면 smoke 의 [hermes-jobs] 검사가 인자를 그대로 대조한다.
FLASH_EXTRA_ARGS: tuple[str, ...] = ()

# 오늘 이미 완주했는지 기록하는 마커.
#
# 왜 락만으로는 부족한가
# ---------------------
# 락은 '지금 돌고 있는가'에만 답한다. Windows 작업 스케줄러의
# StartWhenAvailable 은 놓친 실행을 PC 가 깨어난 시점에 몰아서 띄우는데,
# 앞선 실행이 이미 끝나 락을 놓은 뒤면 두 번째가 그대로 통과한다.
# 2026-09-07 이 그랬다 — 09:02 에 한 번(exit 2), 20:45 에 또 한 번 돌았다.
#
# 전종목 스캔을 하루 두 번 도는 건 낭비고, 장중에 도는 쪽은 시세가 아직
# 없어서 부분 적재를 남긴다(그날 09-02/03/04 가 이렇게 망가졌다). 그래서
# '오늘 완주했다'를 파일로 남기고 다음 기동을 스킵한다. 다시 돌리려면
# --force 를 준다.
DONE_MARKER = "data/nightly_done.json"


@dataclass
class Step:
    """파이프라인 1단계."""

    name: str
    args: list[str]
    # 이 종료 코드는 '실패'가 아니라 '기대된 스킵'으로 센다.
    # credit-kiwoom 은 앱키가 없거나 대상이 0종목이면 exit 4 를 낸다.
    soft: frozenset = field(default_factory=frozenset)
    # 비어 있지 않으면 soft 코드라도 --json 의 reason 이 여기 있을 때만
    # 스킵이다. credit-kiwoom 은 전 종목 조회 실패(8050 인증 실패 등)도
    # 같은 exit 4 로 끝나서, 코드만 보면 실패가 스킵으로 삼켜진다.
    soft_reasons: frozenset = field(default_factory=frozenset)
    # 모드가 아직 없을 수 있는 단계. exit 64(인자 오류)면 미구현으로 본다.
    optional: bool = False
    # STOCKNEWS_DRY_JOBS 로 이 잡이 지목됐다. STOCKNEWS_SEND 와 무관하게
    # --dry-run 을 붙인다. main() 이 apply_dry_jobs() 로 세운다.
    force_dry: bool = False


# 순서는 AGENTS 3장의 의존 관계를 따른다.
#   master -> update -> flags -> daily -> exits
# 사용자 요청 순서(가격 -> credit-kiwoom -> stock_flow -> 지표 -> daily
# -> exits)를 그 안에 끼워 넣었다. credit-kiwoom 을 daily 앞에 둔 것은
# 신용잔고가 LPS 채점에 들어가므로 실측값이 먼저 있는 편이 낫기 때문이다.
# 대상 선정은 이전 스캔 이력을 쓰므로 앞에 둬도 동작한다.
#
# 섹터 지표는 daily 안에서, 뉴스 빈도는 news 안에서 수집된다. 별도 모드가
# 아니다 — 별도 단계로 부르려 하면 exit 64 가 난다.
STEPS: tuple[Step, ...] = (
    Step("master", ["--mode", "master"]),
    Step("update", ["--mode", "update"]),
    Step("flags", ["--mode", "flags", "--dart-limit", "400"]),
    # exit 4 중 '키 없음'·'대상 0건'만 스킵. 2026-09-03~10 은 매일 300/300
    # 종목이 8050(지정단말기 인증 실패)이었는데 스킵으로 세어져 "실패 없음".
    Step("credit-kiwoom", ["--mode", "credit-kiwoom"], soft=frozenset({4}),
         soft_reasons=frozenset({"no_credentials", "no_targets"})),
    # 수급 수집. 2026-09-01 조사 결론: 키움 ka10059 로 종목별은 가능하나
    # 전종목 합산은 유량 제한으로 매일 불가(약 3시간). 모드가 아직 없어서
    # optional 로 둔다 — 있으면 돌고 없으면 '미구현'으로 건너뛴다.
    Step("stock-flow", ["--mode", "stock-flow"], optional=True),
    Step("news", ["--mode", "news"]),      # 뉴스 + 뉴스 빈도
    Step("daily", ["--mode", "daily"]),    # 스캔 + 추천 + 섹터 지표
    Step("exits", ["--mode", "exits"]),
)


@dataclass(frozen=True)
class Job:
    """스케줄 단위 하나. `--job` 으로 고른다.

    Hermes cron 잡이 여러 개가 되면서 필요해졌다. 마커·락·휴장 판정·
    알림·조용한 실패 집계는 nightly 에서 이미 검증된 것이라 그대로 쓰고,
    다른 것은 '어떤 단계를 도느냐' 뿐이다. 드라이버를 새로 쓰지 않는다.

    once_per_day
        '오늘 이미 돌았으면 스킵' 마커를 쓸 것인가. flash 는 5분마다
        돌아야 하므로 False 다. 마커를 켜면 하루 한 번만 돌고 끝난다.
    quiet_ok
        정상/스킵일 때 알림을 보내지 않는다. flash 전용이다 — 5분마다
        "스킵" 을 보내면 하루 84건이 나간다. 실패는 항상 보낸다.
    """

    name: str
    label: str
    emoji: str
    steps: tuple[Step, ...]
    once_per_day: bool = True
    quiet_ok: bool = False
    marker: str = ""

    @property
    def marker_path(self) -> str:
        return self.marker or f"data/cron_done_{self.name}.json"


def _one(name: str, args: list[str], **kw) -> tuple[Step, ...]:
    return (Step(name, args, **kw),)


JOBS: dict[str, Job] = {
    "nightly": Job("nightly", "nightly", "\U0001f319", STEPS,
                   marker=DONE_MARKER),
    # 수집 전용. 발송하지 않으므로 중복이 나가도 사람에게 보이지는
    # 않지만, 같은 소스를 하루 두 번 긁을 이유가 없다.
    "news": Job("news", "news", "\U0001f4f0", _one("news", ["--mode", "news"])),
    # --no-collect: 06:00 news 가 이미 수집했다. 수집과 발송을 분리해야
    # 한 소스가 느려도 브리핑 시각이 밀리지 않는다.
    "brief-morning": Job("brief-morning", "아침 브리핑", "\U0001f305",
                         _one("brief-morning",
                              ["--mode", "brief-morning", "--no-collect"])),
    "brief-evening": Job("brief-evening", "저녁 브리핑", "\U0001f303",
                         _one("brief-evening", ["--mode", "brief-evening"])),
    "weekly": Job("weekly", "주간 리포트", "\U0001f4c8",
                  _one("weekly", ["--mode", "weekly"])),
    # 장중 5분 간격. 마커도 완료 알림도 없다.
    "flash": Job("flash", "flash", "\u26a1",
                 _one("flash", ["--mode", "flash", *FLASH_EXTRA_ARGS]),
                 once_per_day=False, quiet_ok=True),
}


def dry_jobs_from_env(value: str | None) -> set[str]:
    """STOCKNEWS_DRY_JOBS 파싱. 콤마/세미콜론 구분, 대소문자·공백 무시."""
    if not value:
        return set()
    return {tok.strip().lower() for tok in value.replace(";", ",").split(",")
            if tok.strip()}


def apply_dry_jobs(job: Job, dry_jobs: set[str]) -> Job:
    """지목된 잡이면 모든 단계에 force_dry 를 세운 **사본**을 돌려준다."""
    if job.name.lower() not in dry_jobs:
        return job
    steps = tuple(replace(s, force_dry=True) for s in job.steps)
    return replace(job, steps=steps)


class Log:
    """파일 + stderr 동시 기록.

    stdout 을 쓰지 않는 이유는 이 스크립트가 스케줄러에서 돌고, stdout 은
    나중에 `--json` 같은 기계 계약에 쓸 여지를 남겨두기 위함이다.
    """

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.fh = open(path, "a", encoding="utf-8")

    def __call__(self, msg: str = "") -> None:
        line = f"{now_kst():%Y-%m-%d %H:%M:%S} {msg}" if msg else ""
        self.fh.write(line + "\n")
        self.fh.flush()
        print(line, file=sys.stderr, flush=True)

    def raw(self, text: str) -> None:
        """자식 프로세스 출력. 시각을 덧붙이지 않는다."""
        if not text:
            return
        self.fh.write(text.rstrip() + "\n")
        self.fh.flush()

    def close(self) -> None:
        try:
            self.fh.close()
        except OSError:
            pass


def _read_done(path: Path) -> dict:
    """완주 마커 읽기. 없거나 깨졌으면 빈 dict (= 아직 안 돎)."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _mark_done(path: Path, day: str, rc: int, reason: str) -> None:
    """오늘 완주 기록. 기록 실패가 파이프라인 결과를 바꾸지는 않는다."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "date": day,
            "finished": now_kst().replace(
                tzinfo=None).isoformat(timespec="seconds"),
            "exit": rc,
            "reason": reason,
        }, ensure_ascii=False), encoding="utf-8")
    except OSError as exc:
        log_fallback = f"완주 마커 기록 실패 (무시): {exc}"
        print(log_fallback, file=sys.stderr)


def _python() -> list[str]:
    """자식 프로세스로 쓸 인터프리터. 지금 돌고 있는 것을 그대로 쓴다.

    `hermes/run.cmd` 가 이미 실물 인터프리터를 골라 이 스크립트를 띄웠으니
    같은 것을 쓰면 된다. PATH 의 `python` 은 Microsoft Store 스텁일 수
    있어서 절대 쓰지 않는다(AGENTS 1장).
    """
    return [sys.executable]


def _child_env() -> dict:
    """자식 환경. 한글 출력이 cp949 로 깨지지 않게 UTF-8 을 강제한다."""
    env = dict(os.environ)
    # 진입점 표시. run_screen.py 가 이걸 보고 '정상 경로로 불렸다'를 안다.
    # nightly.cmd 에도 같은 값을 넣어두지만, `python nightly.py` 로 직접
    # 띄우는 경우까지 덮으려면 여기가 필요하다.
    env["STOCKNEWS_ENTRY"] = "nightly"
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _build_args(step: Step, dry: bool) -> list[str]:
    args = list(step.args)
    if step.name in _SENDING and (dry or step.force_dry):
        args.append("--dry-run")
    if step.name != "stock-flow":
        args += ["--lock-wait", LOCK_WAIT]
    args.append("--json")
    return args


def _run_step(step: Step, dry: bool, log: Log, timeout: int) -> dict:
    """단계 1개 실행. (결과 dict) 반환. 예외를 올리지 않는다."""
    cmd = _python() + [str(REPO / "run_screen.py")] + _build_args(step, dry)
    log(f"---- {step.name} 시작 ----")
    t0 = time.monotonic()
    rc, out, err = None, "", ""
    try:
        p = subprocess.run(cmd, cwd=str(REPO), env=_child_env(),
                           capture_output=True, timeout=timeout)
        rc = p.returncode
        out = p.stdout.decode("utf-8", errors="replace")
        err = p.stderr.decode("utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        rc = None
        err = f"시간 초과 ({timeout}초)"
    except Exception as exc:                      # noqa: BLE001
        rc = None
        err = f"{type(exc).__name__}: {exc}"

    elapsed = time.monotonic() - t0
    log.raw(err)
    log.raw(out)

    payload = {}
    for line in reversed(out.splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                payload = json.loads(line)
            except ValueError:
                payload = {}
            break

    state, note, detail = _judge(step, rc, err, payload)

    # ── 소스 장애는 '성공'이 아니다 ──
    # 2026-09-08 실측. update 가 rc=0 · 0.8초 · 적재 0건으로 끝났고
    # 파이프라인은 "실패 없음"으로 보고했다. 그런데 그 거래일의 시세는
    # 통째로 비어 있었고, 게다가 그날이 휴장일로 박혀 영구 소실될
    # 뻔했다. rc 만 보면 이 상태가 보이지 않는다.
    state, note = _health_override(step, state, note, payload)

    log(f"---- {step.name} 종료 rc={rc} · {elapsed:.1f}초 · {state}"
        + (f" · {note}" if note else "") + " ----")
    return {"name": step.name, "rc": rc, "state": state, "note": note,
            "detail": detail, "elapsed": elapsed, "json": payload}


def _judge(step: Step, rc: int | None, err: str,
           payload: dict) -> tuple[str, str, str]:
    """종료 코드 + --json 으로 (state, note, detail) 판정.

    detail 은 실패 원인 코드별 건수("8050 × 300건")다. 없으면 빈 문자열.
    """
    reason = payload.get("reason")
    if rc is None:
        return "fail", err[:120], ""
    if rc == 0:
        note = "생략" if payload.get("skipped") else ""
        if reason:
            note = f"생략({reason})"
        return "ok", note, ""
    if rc == 2:
        # 부분 실패. 다음 잡은 계속한다(AGENTS 2장). 성공으로 세되 경고.
        return "warn", f"부분 실패 (실패 {payload.get('failed', '?')})", ""
    if rc == 64 and step.optional:
        return "skip", "미구현 모드", ""
    if rc in step.soft and (not step.soft_reasons
                            or reason in step.soft_reasons):
        note = f"기대된 스킵 rc={rc}"
        if reason:
            note += f" ({reason})"
        return "skip", note, ""
    detail = _codes_detail(payload)
    if detail:
        return "fail", detail, detail
    return "fail", f"rc={rc}" + (f" {reason}" if reason else ""), ""


def _codes_detail(payload: dict) -> str:
    """`failure_codes` -> "8050 × 300건". 코드를 못 뽑은 실패는 '기타'."""
    codes = payload.get("failure_codes") or {}
    if not codes:
        return ""
    parts = [f"{c} × {n}건"
             for c, n in sorted(codes.items(), key=lambda kv: -int(kv[1]))]
    rest = int(payload.get("failed") or 0) - sum(int(n) for n in codes.values())
    if rest > 0:
        parts.append(f"기타 × {rest}건")
    return ", ".join(parts)


def _outcome(results: list[dict]) -> str:
    """마커·알림에 쓸 결과: '완주' / '부분 완료' / '부분 실패'.

    '부분 완료'는 실패 단계는 없지만 update 가 시세를 한 건도 적재하지
    않은 밤이다. 2026-09-08 은 update 가 rc=0 · 0.8초 · 0건으로 끝났는데
    마커도 알림도 '완주'였다. 모든 단계를 돌긴 했어도 그날 시세는 없다.
    """
    if any(r["state"] == "fail" for r in results):
        return "부분 실패"
    upd = next((r for r in results if r["name"] == "update"), None)
    if upd and not (upd.get("json") or {}).get("rows"):
        return "부분 완료"
    return "완주"


def _health_override(step: Step, state: str, note: str,
                     payload: dict) -> tuple[str, str]:
    """종료 코드가 놓치는 '조용한 실패'를 실패로 되돌린다.

    두 가지를 본다.

      1. `source_outage` — 거래일인데 시세를 못 받은 날짜. run_screen 이
         휴장일로 기록하지 않고 올려보낸다. 다음 실행이 재요청하지만,
         그 사실은 사람에게 보여야 한다.
      2. update 가 아무것도 안 했는데 그럴 근거도 없는 경우. 휴장일
         건너뜀도, 새 휴장일 기록도, 판정 보류도 없이 0건이면 이상하다.
    """
    if state not in ("ok", "warn"):
        return state, note

    outage = payload.get("source_outage") or []
    if outage:
        shown = ", ".join(outage[:3]) + ("..." if len(outage) > 3 else "")
        return "fail", f"소스 장애 {len(outage)}일 ({shown})"

    if step.name == "update" and not payload.get("rows"):
        grounds = (payload.get("holidays_skipped")
                   or payload.get("marked_non_trading")
                   or payload.get("judge_pending"))
        if not grounds:
            return "fail", "적재 0건 · 휴장 근거 없음"

    return state, note


def _notify(text: str, log: Log, enabled: bool,
            why: str = "--no-telegram") -> None:
    """텔레그램 1건. 실패해도 파이프라인 결과를 바꾸지 않는다."""
    if not enabled:
        log(f"알림 생략 ({why})")
        return
    try:
        from stocknews.notify import TelegramNotConfigured, send_telegram
        try:
            rep = send_telegram(text)
            log("알림 발송 " + ("완료" if rep else rep.summary()))
        except TelegramNotConfigured:
            # 토큰이 없는 것은 설정 문제다. 파이프라인 실패가 아니다.
            log("알림 생략 — TELEGRAM_BOT_TOKEN / CHAT_ID 미설정")
    except Exception as exc:                      # noqa: BLE001
        log(f"알림 발송 중 오류 (무시): {type(exc).__name__}: {exc}")


def _summary(job: Job, started: datetime, finished: datetime,
             results: list[dict], dry: bool) -> str:
    """텔레그램 한 줄 요약."""
    ok = [r for r in results if r["state"] in ("ok", "warn")]
    fails = [r for r in results if r["state"] == "fail"]
    skips = [r for r in results if r["state"] == "skip"]
    mins = (finished - started).total_seconds() / 60.0

    word = "부분 완료" if _outcome(results) == "부분 완료" else "완료"
    head = (f"{job.emoji} {job.label} {word} {started:%H:%M}~{finished:%H:%M} "
            f"({mins:.0f}분) | 성공 {len(ok)}/{len(results)}")
    if skips:
        head += f" | 건너뜀 {len(skips)}"
    if fails:
        # 코드별 건수(detail)가 있는 단계는 이름만 적고 아래 줄에 따로 쓴다.
        head += " | 실패: " + ", ".join(
            r["name"] if r.get("detail") else f"{r['name']}({r['note']})"
            for r in fails)
    else:
        head += " | 실패 없음"

    # 시세 적재량을 항상 노출한다. "실패 없음"만 보고 안심하는 일이
    # 없도록, 그 밤에 실제로 며칠치가 들어왔는지를 같은 줄에서 본다.
    upd = next((r for r in results if r["name"] == "update"), None)
    if upd:
        j = upd.get("json") or {}
        head += (f"\n시세 {j.get('rows', '?')}건 · "
                 f"최신 거래일 {j.get('last_price_date', '?')}")
        if j.get("marked_non_trading"):
            head += f" · 휴장 기록 {', '.join(j['marked_non_trading'])}"
        if j.get("source_outage"):
            head += f" · 미적재 {', '.join(j['source_outage'])}"
    for r in fails:
        if r.get("detail"):
            head += f"\n{r['name']} 실패: {r['detail']}"
    # 발송 실패는 rc=2 로 드러나긴 하지만, 어느 수신자가 왜 막혔는지는
    # 자식의 --json 안에만 있다. 수신자가 여러 명이면 "누가 못 받았나"가
    # 곧 조치 대상이므로 요약문에 끌어올린다.
    sf = _send_failures(results)
    if sf:
        detail = ", ".join(
            f"{f.get('chat', '????')}: {f.get('status') or '무응답'}"
            for f in sf)
        head += f"\n발송 실패 {len(sf)}건 (수신자 {detail})"
    if dry:
        head += ("\n※ --dry-run (STOCKNEWS_SEND=1 로 실발송 전환 · "
                 f"잡별 예외는 {DRY_JOBS_ENV})")
    return head


def _send_failures(results: list[dict]) -> list[dict]:
    """모든 단계의 --json 에서 수신자별 발송 실패를 모은다."""
    out: list[dict] = []
    for r in results:
        out += ((r.get("json") or {}).get("send_failures") or [])
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="스케줄 잡 드라이버 (기본: 매일 밤 자동 수집 파이프라인)")
    ap.add_argument("--job", choices=tuple(JOBS), default="nightly",
                    help="실행할 잡. 마커·락·로그 이름이 여기서 갈린다.")
    ap.add_argument("--db", default="data/quant.db")
    ap.add_argument("--check-date", default=None,
                    help="휴장 판정에 쓸 날짜 (YYYY-MM-DD). 테스트용")
    ap.add_argument("--force", action="store_true",
                    help="휴장일에도 실행")
    ap.add_argument("--no-telegram", action="store_true")
    ap.add_argument("--step-timeout", type=int, default=5400,
                    help="단계별 시간 상한(초). 기본 90분")
    ap.add_argument("--lock-dir", default="data/locks")
    ap.add_argument("--done-marker", default=None,
                    help="'오늘 완주' 마커 경로. 기본은 잡별 경로. --force 로 무시")
    args = ap.parse_args(argv)

    job = JOBS[args.job]
    started = now_kst()
    log = Log(REPO / "logs" / f"{job.name}_{started:%Y%m%d}.log")
    dry = os.getenv("STOCKNEWS_SEND") != "1"
    dry_jobs = dry_jobs_from_env(os.getenv(DRY_JOBS_ENV))
    job = apply_dry_jobs(job, dry_jobs)
    job_dry = dry or job.name.lower() in dry_jobs
    notify_on = not args.no_telegram

    log("=" * 62)
    log(f"{job.name} 시작 {started:%Y-%m-%d %H:%M:%S} KST "
        f"(발송 {'실제' if not job_dry else 'dry-run'}"
        + (f" · {DRY_JOBS_ENV}={','.join(sorted(dry_jobs))}" if dry_jobs else "")
        + ")")

    # .env 를 환경변수로 올린다. 이게 없으면 _notify 가 토큰을 못 찾아
    # 조용히 생략된다 — 실패 알림까지 안 나간다.
    #
    # 2026-09-07 실측: 20:45 에 파이프라인이 완주했는데도 로그에
    # "알림 생략 — TELEGRAM_BOT_TOKEN / CHAT_ID 미설정" 이 찍혔다.
    # .env 에는 토큰이 있었다. run_screen.py 는 자기 main() 에서
    # load_env 를 부르는데 nightly.py 는 부르지 않아서, 이 드라이버가
    # 내는 알림만 전부 사라지고 있었다. 자식 단계는 각자 로드하므로
    # 그쪽 알림은 정상이었고, 그래서 눈에 띄지 않았다.
    env_rep = load_env()
    if not env_rep["exists"]:
        log(f".env 없음 ({env_rep['path']}) — OS 환경변수만 사용")

    # ── 이중 실행 방지 ──
    # wait_seconds=0 이라 즉시 판정한다. 요구사항이 '존재하면 즉시 종료'다.
    lock = JobLock(job.name, mode=job.name, lock_dir=args.lock_dir,
                   timeout=LOCK_TIMEOUT_SEC, wait_seconds=0)
    if not lock.acquire():
        h = lock.holder
        log(f"이미 실행 중 — 즉시 종료 (pid={h.get('pid')} "
            f"시작={h.get('started')} 만료={h.get('expires')})")
        log.close()
        return 3
    log(f"락 획득 {lock.path} (만료 {LOCK_TIMEOUT_SEC // 3600}시간)")

    rc_final = 0
    day = f"{started:%Y-%m-%d}"
    marker = REPO / (args.done_marker or job.marker_path)
    try:
        # ── 오늘 이미 돌았으면 스킵 ──
        # 반드시 락을 잡은 뒤에 본다. 순서를 뒤집으면 동시에 뜬 두
        # 프로세스가 같은 마커를 읽고 둘 다 '아직 안 돌았다'로 판정한다.
        #
        # Hermes cron 의 catch-up 이 이 방어를 필요하게 만든다. PC 가
        # 꺼져 있던 동안의 회차를 부팅 직후 몰아서 띄우는데, 앞 실행이
        # 이미 끝나 락을 놓았으면 두 번째가 그대로 통과한다. 브리핑이
        # 두 번 나가면 그건 사람에게 그대로 보인다.
        done = _read_done(marker) if job.once_per_day else {}
        if done.get("date") == day and not args.force:
            log(f"오늘({day}) 이미 실행 — 스킵 "
                f"(완료 {done.get('finished')} · exit {done.get('exit')} "
                f"· {done.get('reason')})")
            log("다시 돌리려면 --force")
            # 휴장 스킵과 대칭으로 알림을 낸다. 스킵이 조용하면 '오늘 안
            # 돌았나?' 를 사람이 확인하러 가게 되고, 그 확인이 수동 재실행
            # 으로 이어진다. 스킵도 결과다 — 결과는 보고한다.
            # '이미 완주'라고 쓰지 않는다. reason 이 '부분 완료'·'부분 실패'
            # 일 수 있다.
            _notify(f"{job.emoji} {job.label} 스킵 {started:%m/%d %H:%M} | "
                    f"오늘 이미 실행({done.get('reason')})",
                    log, notify_on and not job.quiet_ok,
                    why="--no-telegram" if not notify_on else "quiet_ok 잡")
            log(f"{job.name} 종료 {now_kst():%H:%M:%S} · exit 0")
            return 0

        # ── 휴장일 판정 ──
        when = args.check_date or started
        try:
            store = Store(args.db)
            status = market_status(store, when)
        except Exception as exc:                  # noqa: BLE001
            # 판정 자체가 실패하면 멈추지 않고 진행한다. 각 모드가 자기
            # 휴장 가드를 다시 갖고 있으므로(AGENTS 8-2장) 이중 방어다.
            log(f"거래일 판정 실패 — 진행합니다 ({type(exc).__name__}: {exc})")
            status = "UNKNOWN"

        # 날짜만 찍는다. datetime 을 그대로 넣으면 마이크로초까지 나와
        # 로그가 읽기 어려워진다.
        when_s = (when if isinstance(when, str)
                  else f"{when:%Y-%m-%d}")
        log(f"거래일 판정 {when_s} -> {status}")
        if status in (CLOSED_WEEKEND, CLOSED_HOLIDAY) and not args.force:
            log(f"휴장 — 스킵 ({status})")
            # 휴장 판정도 '오늘 할 일을 끝냈다'이다. 기록해 두지 않으면
            # catch-up 이 뜰 때마다 같은 판정을 반복하고 알림도 반복된다.
            if job.once_per_day:
                _mark_done(marker, day, 0, f"휴장({status})")
            log(f"{job.name} 종료 {now_kst():%H:%M:%S} · exit 0")
            _notify(f"{job.emoji} {job.label} 스킵 {started:%m/%d %H:%M} | "
                    f"휴장({status})", log, notify_on and not job.quiet_ok,
                    why="--no-telegram" if not notify_on else "quiet_ok 잡")
            return 0

        # ── 단계 실행 ──
        results: list[dict] = []
        for step in job.steps:
            results.append(_run_step(step, dry, log, args.step_timeout))

        finished = now_kst()
        log("")
        log("단계별 결과:")
        for r in results:
            log(f"  {r['state']:<5} {r['name']:<15} rc={str(r['rc']):<5} "
                f"{r['elapsed']:7.1f}초  {r['note']}")
        total = (finished - started).total_seconds()
        log(f"합계 {total:.1f}초 ({total / 60:.1f}분)")

        fails = [r for r in results if r["state"] == "fail"]
        rc_final = 2 if fails else 0
        # 부분 실패(exit 2)·부분 완료도 마커는 남긴다. nightly.py 의 설계
        # 원칙 1 이 '한 단계가 실패해도 다음 단계는 돈다'이므로, 여기
        # 도달했다는 건 모든 단계를 한 번씩 시도했다는 뜻이다. 재시도는
        # 사람이 --force 로 판단한다 — 자동 재시도는 장중 재실행으로 이어져
        # 부분 적재를 만든다. reason 은 _outcome 이 정한다.
        if job.once_per_day:
            _mark_done(marker, day, rc_final, _outcome(results))
        text = _summary(job, started, finished, results, job_dry)
        log(f"{job.name} 종료 {finished:%H:%M:%S} · exit {rc_final}")
        # quiet_ok 잡은 실패일 때만 알린다. flash 는 5분마다 돌아서
        # 정상 보고를 매번 보내면 하루 84건이 나간다.
        _notify(text, log, notify_on and (bool(fails) or not job.quiet_ok),
                why="--no-telegram" if not notify_on
                    else "quiet_ok 잡 · 실패 없음")
        return rc_final

    except BaseException as exc:                  # noqa: BLE001
        # 파이프라인이 시작조차 못 한 경우도 알림이 나가야 한다.
        # 조용히 죽는 게 최악이다.
        import traceback
        log("파이프라인 실패:")
        log.raw(traceback.format_exc())
        _notify(f"🚨 {job.label} 실패 {started:%m/%d %H:%M} | "
                f"{type(exc).__name__}: {str(exc)[:160]}", log, notify_on)
        return 1
    finally:
        lock.release()
        log(f"락 해제 {lock.path}")
        log("=" * 62)
        log.close()


if __name__ == "__main__":
    sys.exit(main())
