# -*- coding: utf-8 -*-
"""잡 단위 파일 락.

왜 필요한가
----------
Hermes cron 에 여러 잡을 걸면 겹친다. update(15:40) 가 늦어지면
flags(15:50) 와 붙고, daily(18:00) 가 길어지면 exits(18:10) 와 충돌한다.
SQLite 는 WAL 모드에서도 쓰기끼리는 직렬화되므로 겹치면
`database is locked` 로 배치가 죽는다.

설계 원칙
--------
  1) 원자적 생성    os.open(O_CREAT|O_EXCL) 로 경합을 없앤다
  2) 만료 시각 명시  잡이 죽어서 락이 남으면 영구 차단된다. 락 파일에
                    '언제까지 유효한지'를 적어두고 지나면 빼앗는다
  3) 재시도 신호     이미 잡혀 있으면 예외가 아니라 False 를 돌려
                    호출부가 exit code 3(재시도 대상)으로 끝낼 수 있게 한다

PID 생존 확인은 하지 않는다. 윈도우에서 os.kill(pid, 0) 이 신뢰할 수
없어서, 만료 시각 방식이 더 이식성 있고 디버깅도 쉽다.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))

__all__ = ["JobLock", "LockBusy", "MODE_TIMEOUTS", "clear_locks"]

# 모드별 예상 최대 소요(초). 이 시간이 지난 락은 죽은 것으로 보고 빼앗는다.
# 실제 소요보다 넉넉히 준다. 짧으면 정상 실행 중인 잡의 락을 빼앗는다.
MODE_TIMEOUTS: dict[str, int] = {
    "backfill": 7200,     # 2시간 (2,800종목 x 0.35초 + 재시도)
    "daily": 1800,        # 30분
    "fib": 1800,
    "flash": 1200,
    "flags": 3600,        # DART 조회가 종목당 0.25초
    "weekly": 900,
    "news": 900,
    "brief-morning": 900,
    "brief-evening": 900,
    "brief-weekly": 600,
    "master": 900,
    "update": 900,
    "exits": 900,
    "credit": 300,
    "export": 600,
}
DEFAULT_TIMEOUT = 900


class LockBusy(RuntimeError):
    """다른 잡이 이미 실행 중."""

    def __init__(self, name: str, holder: dict):
        self.name = name
        self.holder = holder
        super().__init__(f"{name} 락이 이미 잡혀 있음: {holder}")


class JobLock:
    """DB 쓰기 잡을 직렬화하는 락.

    기본은 DB 단위 단일 락이다. 모드별로 락을 나누면 update 와 daily 가
    동시에 같은 DB 를 쓰게 되므로 의미가 없다.

    사용:
        with JobLock("quant", mode="daily") as lock:
            if not lock.acquired:
                return 3        # 재시도 대상
            ...
    """

    def __init__(self, name: str = "quant", mode: str = "",
                 lock_dir: str | Path = "data/locks",
                 timeout: int | None = None,
                 wait_seconds: int = 0):
        self.name = name
        self.mode = mode or name
        self.dir = Path(lock_dir)
        self.path = self.dir / f"{name}.lock"
        self.timeout = int(timeout if timeout is not None
                           else MODE_TIMEOUTS.get(self.mode, DEFAULT_TIMEOUT))
        self.wait_seconds = int(wait_seconds)
        self.acquired = False
        self.holder: dict = {}

    # ────────────────────────── 내부 ──────────────────────────
    def _payload(self) -> str:
        now = datetime.now(KST).replace(tzinfo=None)
        return json.dumps({
            "pid": os.getpid(),
            "mode": self.mode,
            "started": now.isoformat(timespec="seconds"),
            "expires": (now + timedelta(seconds=self.timeout)
                        ).isoformat(timespec="seconds"),
            "timeout_sec": self.timeout,
        }, ensure_ascii=False)

    def _read(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _try_create(self) -> bool:
        self.dir.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return False
        try:
            os.write(fd, self._payload().encode("utf-8"))
        finally:
            os.close(fd)
        return True

    def _expired(self, holder: dict) -> bool:
        exp = holder.get("expires")
        if not exp:
            return True                     # 형식이 깨진 락은 빼앗는다
        try:
            return datetime.now(KST).replace(tzinfo=None) > datetime.fromisoformat(exp)
        except ValueError:
            return True

    # ────────────────────────── 공개 ──────────────────────────
    def acquire(self) -> bool:
        deadline = time.time() + self.wait_seconds
        while True:
            if self._try_create():
                self.acquired = True
                return True

            holder = self._read()
            if self._expired(holder):
                log.warning("만료된 락 회수 (이전 잡 %s, 만료 %s)",
                            holder.get("mode"), holder.get("expires"))
                try:
                    self.path.unlink()
                except OSError:
                    pass
                continue

            self.holder = holder
            if time.time() >= deadline:
                return False
            time.sleep(min(2.0, max(0.2, deadline - time.time())))

    def release(self) -> None:
        if not self.acquired:
            return
        try:
            # 내가 잡은 락인지 확인하고 지운다. 만료 회수로 다른 잡이
            # 다시 잡았을 수 있으므로 pid 를 확인한다.
            holder = self._read()
            if holder.get("pid") in (os.getpid(), None):
                self.path.unlink(missing_ok=True)
        except OSError as exc:
            log.warning("락 해제 실패 %s: %s", self.path, exc)
        finally:
            self.acquired = False

    def __enter__(self) -> "JobLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.release()
        return False


def clear_locks(lock_dir: str | Path = "data/locks") -> list[str]:
    """모든 락을 강제 해제. 배치가 비정상 종료해 락이 남았을 때 쓴다."""
    d = Path(lock_dir)
    removed = []
    if not d.exists():
        return removed
    for p in d.glob("*.lock"):
        try:
            p.unlink()
            removed.append(p.name)
        except OSError:
            pass
    return removed
