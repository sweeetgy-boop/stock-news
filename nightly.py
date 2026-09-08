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
  0   정상 (휴장일 스킵, '오늘 이미 완주' 스킵도 0)
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
from dataclasses import dataclass, field
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
_SENDING = {"news", "daily", "exits"}

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
    # 모드가 아직 없을 수 있는 단계. exit 64(인자 오류)면 미구현으로 본다.
    optional: bool = False


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
    Step("credit-kiwoom", ["--mode", "credit-kiwoom"], soft=frozenset({4})),
    # 수급 수집. 2026-09-01 조사 결론: 키움 ka10059 로 종목별은 가능하나
    # 전종목 합산은 유량 제한으로 매일 불가(약 3시간). 모드가 아직 없어서
    # optional 로 둔다 — 있으면 돌고 없으면 '미구현'으로 건너뛴다.
    Step("stock-flow", ["--mode", "stock-flow"], optional=True),
    Step("news", ["--mode", "news"]),      # 뉴스 + 뉴스 빈도
    Step("daily", ["--mode", "daily"]),    # 스캔 + 추천 + 섹터 지표
    Step("exits", ["--mode", "exits"]),
)


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
    if step.name in _SENDING and dry:
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

    # ── 판정 ──
    if rc is None:
        state, note = "fail", err[:120]
    elif rc == 0:
        state = "ok"
        note = "생략" if payload.get("skipped") else ""
        if payload.get("reason"):
            note = f"생략({payload['reason']})"
    elif rc == 2:
        # 부분 실패. 다음 잡은 계속한다(AGENTS 2장). 성공으로 세되 경고.
        state, note = "warn", f"부분 실패 (실패 {payload.get('failed', '?')})"
    elif rc == 64 and step.optional:
        state, note = "skip", "미구현 모드"
    elif rc in step.soft:
        state = "skip"
        note = f"기대된 스킵 rc={rc}"
        if payload.get("reason"):
            note += f" ({payload['reason']})"
    else:
        state, note = "fail", f"rc={rc}"
        if payload.get("reason"):
            note += f" {payload['reason']}"

    # ── 소스 장애는 '성공'이 아니다 ──
    # 2026-09-08 실측. update 가 rc=0 · 0.8초 · 적재 0건으로 끝났고
    # 파이프라인은 "실패 없음"으로 보고했다. 그런데 그 거래일의 시세는
    # 통째로 비어 있었고, 게다가 그날이 휴장일로 박혀 영구 소실될
    # 뻔했다. rc 만 보면 이 상태가 보이지 않는다.
    state, note = _health_override(step, state, note, payload)

    log(f"---- {step.name} 종료 rc={rc} · {elapsed:.1f}초 · {state}"
        + (f" · {note}" if note else "") + " ----")
    return {"name": step.name, "rc": rc, "state": state, "note": note,
            "elapsed": elapsed, "json": payload}


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


def _notify(text: str, log: Log, enabled: bool) -> None:
    """텔레그램 1건. 실패해도 파이프라인 결과를 바꾸지 않는다."""
    if not enabled:
        log("알림 생략 (--no-telegram)")
        return
    try:
        from stocknews.notify import TelegramNotConfigured, send_telegram
        try:
            ok = send_telegram(text)
            log("알림 발송 " + ("완료" if ok else "실패(재시도 소진)"))
        except TelegramNotConfigured:
            # 토큰이 없는 것은 설정 문제다. 파이프라인 실패가 아니다.
            log("알림 생략 — TELEGRAM_BOT_TOKEN / CHAT_ID 미설정")
    except Exception as exc:                      # noqa: BLE001
        log(f"알림 발송 중 오류 (무시): {type(exc).__name__}: {exc}")


def _summary(started: datetime, finished: datetime,
             results: list[dict], dry: bool) -> str:
    """텔레그램 한 줄 요약."""
    ok = [r for r in results if r["state"] in ("ok", "warn")]
    fails = [r for r in results if r["state"] == "fail"]
    skips = [r for r in results if r["state"] == "skip"]
    mins = (finished - started).total_seconds() / 60.0

    head = (f"🌙 nightly 완료 {started:%H:%M}~{finished:%H:%M} "
            f"({mins:.0f}분) | 성공 {len(ok)}/{len(results)}")
    if skips:
        head += f" | 건너뜀 {len(skips)}"
    if fails:
        head += " | 실패: " + ", ".join(
            f"{r['name']}({r['note']})" for r in fails)
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
    if dry:
        head += "\n※ --dry-run (STOCKNEWS_SEND=1 로 실발송 전환)"
    return head


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="매일 밤 자동 수집 파이프라인")
    ap.add_argument("--db", default="data/quant.db")
    ap.add_argument("--check-date", default=None,
                    help="휴장 판정에 쓸 날짜 (YYYY-MM-DD). 테스트용")
    ap.add_argument("--force", action="store_true",
                    help="휴장일에도 실행")
    ap.add_argument("--no-telegram", action="store_true")
    ap.add_argument("--step-timeout", type=int, default=5400,
                    help="단계별 시간 상한(초). 기본 90분")
    ap.add_argument("--lock-dir", default="data/locks")
    ap.add_argument("--done-marker", default=DONE_MARKER,
                    help="'오늘 완주' 마커 경로. --force 로 무시")
    args = ap.parse_args(argv)

    started = now_kst()
    log = Log(REPO / "logs" / f"nightly_{started:%Y%m%d}.log")
    dry = os.getenv("STOCKNEWS_SEND") != "1"
    notify_on = not args.no_telegram

    log("=" * 62)
    log(f"nightly 시작 {started:%Y-%m-%d %H:%M:%S} KST "
        f"(발송 {'실제' if not dry else 'dry-run'})")

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
    lock = JobLock("nightly", mode="nightly", lock_dir=args.lock_dir,
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
    marker = REPO / args.done_marker
    try:
        # ── 오늘 이미 완주했으면 스킵 ──
        # 반드시 락을 잡은 뒤에 본다. 순서를 뒤집으면 동시에 뜬 두
        # 프로세스가 같은 마커를 읽고 둘 다 '아직 안 돌았다'로 판정한다.
        done = _read_done(marker)
        if done.get("date") == day and not args.force:
            log(f"오늘({day}) 이미 완주 — 스킵 "
                f"(완료 {done.get('finished')} · exit {done.get('exit')} "
                f"· {done.get('reason')})")
            log("다시 돌리려면 --force")
            # 휴장 스킵과 대칭으로 알림을 낸다. 스킵이 조용하면 '오늘 안
            # 돌았나?' 를 사람이 확인하러 가게 되고, 그 확인이 수동 재실행
            # 으로 이어진다. 스킵도 결과다 — 결과는 보고한다.
            _notify(f"🌙 nightly 스킵 {started:%m/%d %H:%M} | "
                    f"오늘 이미 완주({done.get('reason')})", log, notify_on)
            log(f"nightly 종료 {now_kst():%H:%M:%S} · exit 0")
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
            _mark_done(marker, day, 0, f"휴장({status})")
            log(f"nightly 종료 {now_kst():%H:%M:%S} · exit 0")
            _notify(f"🌙 nightly 스킵 {started:%m/%d %H:%M} | 휴장({status})",
                    log, notify_on)
            return 0

        # ── 단계 실행 ──
        results: list[dict] = []
        for step in STEPS:
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
        # 부분 실패(exit 2)도 '완주'다. nightly.py 의 설계 원칙 1 이
        # '한 단계가 실패해도 다음 단계는 돈다'이므로, 여기 도달했다는 건
        # 모든 단계를 한 번씩 시도했다는 뜻이다. 재시도는 사람이 --force 로
        # 판단한다 — 자동 재시도는 장중 재실행으로 이어져 부분 적재를 만든다.
        _mark_done(marker, day, rc_final,
                   "완주" if rc_final == 0 else "부분 실패")
        text = _summary(started, finished, results, dry)
        log(f"nightly 종료 {finished:%H:%M:%S} · exit {rc_final}")
        _notify(text, log, notify_on)
        return rc_final

    except BaseException as exc:                  # noqa: BLE001
        # 파이프라인이 시작조차 못 한 경우도 알림이 나가야 한다.
        # 조용히 죽는 게 최악이다.
        import traceback
        log("파이프라인 실패:")
        log.raw(traceback.format_exc())
        _notify(f"🚨 nightly 실패 {started:%m/%d %H:%M} | "
                f"{type(exc).__name__}: {str(exc)[:160]}", log, notify_on)
        return 1
    finally:
        lock.release()
        log(f"락 해제 {lock.path}")
        log("=" * 62)
        log.close()


if __name__ == "__main__":
    sys.exit(main())
