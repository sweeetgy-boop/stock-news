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
  0   정상 (휴장일 스킵도 0)
  1   파이프라인 자체 실패 (락 획득 실패는 0 이 아니라 3)
  2   부분 실패 — 한 단계 이상 실패했으나 완주함
  3   이중 실행 (락 점유 중)
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

from stocknews.joblock import JobLock                      # noqa: E402
from stocknews.store import Store                          # noqa: E402
from stocknews.trading_day import (market_status, now_kst,  # noqa: E402
                                   CLOSED_HOLIDAY, CLOSED_WEEKEND)

# 락 만료. 요구사항대로 2시간이다. 전 단계 합계가 이보다 길면 정상
# 실행 중인 잡의 락을 다음 트리거가 빼앗는다 — 실측 최악은 backfill 이
# 끼지 않는 한 40분 안쪽이다.
LOCK_TIMEOUT_SEC = 7200

# 쓰기 모드가 잔류 락에 걸려 exit 3 으로 죽지 않게 대기시킨다.
LOCK_WAIT = "600"

# 텔레그램을 실제로 보내는 모드. 토큰이 없으면 exit 4 로 끝나므로
# STOCKNEWS_SEND=1 이 아닐 때는 --dry-run 을 붙인다(afterclose.cmd 와 동일).
_SENDING = {"news", "daily", "exits"}


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

    log(f"---- {step.name} 종료 rc={rc} · {elapsed:.1f}초 · {state}"
        + (f" · {note}" if note else "") + " ----")
    return {"name": step.name, "rc": rc, "state": state, "note": note,
            "elapsed": elapsed, "json": payload}


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
    args = ap.parse_args(argv)

    started = now_kst()
    log = Log(REPO / "logs" / f"nightly_{started:%Y%m%d}.log")
    dry = os.getenv("STOCKNEWS_SEND") != "1"
    notify_on = not args.no_telegram

    log("=" * 62)
    log(f"nightly 시작 {started:%Y-%m-%d %H:%M:%S} KST "
        f"(발송 {'실제' if not dry else 'dry-run'})")

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
    try:
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
