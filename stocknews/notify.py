# -*- coding: utf-8 -*-
"""알림 게이트 + 텔레그램 발송.

"너무 많아 또 보지도 못하잖아"를 해결하는 4중 게이트와 3계층 리듬.
핵심은 티어2(장마감 다이제스트)를 매일 고정 발송하는 것이다. 그러면
티어1 허들을 8.0점으로 높게 유지해도 놓치는 게 없다. "혹시 놓칠까 봐"
허들을 낮추는 게 알림 폭주의 진짜 원인이다.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path

import requests

from .config import Config, DEFAULT
from .contracts import ScreenResult
from .trading_day import is_definitely_closed, trading_days_between

__all__ = ["AlertWindow", "WINDOWS", "AlertGate", "send_telegram", "now_kst",
           "TelegramNotConfigured"]


class TelegramNotConfigured(RuntimeError):
    """토큰/채팅방 미설정. 로직 실패가 아니라 설정 문제다.

    호출부가 exit 1(치명적) 대신 exit 4(전제조건)로 끝낼 수 있게
    별도 예외로 구분한다. .env 만 채우면 해결된다.
    """

TELEGRAM_MAX = 4096
KST = timezone(timedelta(hours=9))


def now_kst() -> datetime:
    """국내장 기준 현재 시각 (tz 정보 없음).

    호스트 타임존에 의존하면 안 된다. UTC 서버에서 datetime.now() 를 쓰면
    09:00 KST 시간창이 영원히 열리지 않고, 에러도 없이 조용히 무음이 된다.
    """
    return datetime.now(KST).replace(tzinfo=None)


@dataclass(frozen=True)
class AlertWindow:
    """발송 허용 시간창.

    창마다 목적이 다르므로 트랙과 예산을 따로 준다. 전역 예산 하나만
    쓰면 09시에 예산을 다 소진해 정작 최우선 창인 10시가 무음이 된다.
    """

    name: str
    start: dtime
    end: dtime
    tracks: tuple[str, ...]   # 이 창에서 발송을 허용할 트랙
    budget: int               # 이 창의 최대 발송 건수
    note: str

    def contains(self, t: dtime) -> bool:
        return self.start <= t <= self.end


# 4대 감시 시간대. 하루 중 자금이 이동하는 변곡점에 맞춘다.
#
#   09:00~09:35  D+2 반대매매가 동시호가에 하한가로 집행되는 순간.
#                투매를 '진행 중에' 잡아야 하므로 역추세(매집) 트랙만 본다.
#   10:00~10:25  ★ 최우선. 09시 휩쏘가 걷히고 외국인·기관 알고리즘의
#                당일 방향이 확정되는 시점. 전 트랙 허용 + 최대 예산.
#   14:00~14:25  단타 실망 매물로 멀쩡한 주가가 인위적으로 눌리는 구간.
#                스위칭 매수 준비이므로 매집 트랙.
#   15:20~15:35  종가 확정. 익일 갭을 노린 오버나이트 판단용으로 전 트랙.
WINDOWS: tuple[AlertWindow, ...] = (
    AlertWindow("반대매매", dtime(9, 0), dtime(9, 35),
                ("VALUE", "BOTH"), 2, "D+2 하한가 투매 진행 중"),
    AlertWindow("방향확정", dtime(10, 0), dtime(10, 25),
                ("VALUE", "TREND", "BOTH"), 3, "최우선 · 휩쏘 종료 후"),
    AlertWindow("오후눌림", dtime(14, 0), dtime(14, 25),
                ("VALUE", "BOTH"), 2, "실망매물 스위칭 준비"),
    AlertWindow("종가확정", dtime(15, 20), dtime(15, 35),
                ("VALUE", "TREND", "BOTH"), 2, "오버나이트 판단"),
)


class AlertGate:
    """G1 점수 · G2 시간창 · G3 종목 쿨다운 · G4 일일 예산."""

    def __init__(self, state_path: str | Path = "data/alert_state.json",
                 cfg: Config = DEFAULT, store=None):
        self.cfg = cfg
        self.path = Path(state_path)
        # store 를 주면 휴장일 판정과 거래일 기준 쿨다운이 활성된다.
        # 없으면 시각만 보고 판단하므로 토·일에도 창이 열린다.
        self.store = store
        self.state = self._load()

    def _load(self) -> dict:
        if self.path.exists():
            try:
                st = json.loads(self.path.read_text(encoding="utf-8"))
                st.setdefault("sent", {})
                st.setdefault("budget", {})
                st.setdefault("window_budget", {})
                return st
            except (json.JSONDecodeError, OSError):
                pass
        return {"sent": {}, "budget": {}, "window_budget": {}}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.state, ensure_ascii=False, indent=1),
                             encoding="utf-8")

    # ── G2 ──
    @staticmethod
    def current_window(now: datetime | None = None) -> AlertWindow | None:
        """지금이 어느 창인지. 창 밖이면 None."""
        now = now or now_kst()
        t = now.time()
        for w in WINDOWS:
            if w.contains(t):
                return w
        return None

    @staticmethod
    def in_window(now: datetime | None = None) -> bool:
        return AlertGate.current_window(now) is not None

    # ── G3 ──
    def _cooldown_blocked(self, r: ScreenResult, today: date) -> bool:
        rec = self.state["sent"].get(r.ticker)
        if not rec:
            return False
        last = date.fromisoformat(rec["date"])
        # 쿨다운은 거래일 기준이어야 한다. 달력일로 세면 주말이 끼었을 때
        # 실제로는 3거래일밖에 안 지났는데 5일 지난 것으로 오판한다.
        if self.store is not None:
            elapsed = trading_days_between(self.store, last, today)
        else:
            elapsed = (today - last).days
        if elapsed >= self.cfg.gate.cooldown_days:
            return False
        # 점수가 유의미하게 올랐으면 갱신 발송 허용
        best_now = max(r.value_score, r.trend_score)
        return best_now < rec["score"] + self.cfg.gate.rescore_delta

    # ── G4 ──
    def _window_budget_left(self, today: date, window: AlertWindow) -> int:
        """창별 잔여 예산.

        창마다 독립 예산을 준다. 이게 '10시 최우선' 요구의 구현이다.
        전역 예산 하나면 09시에 소진해 10시가 무음이 될 수 있다.
        """
        key = today.isoformat()
        used = (self.state["window_budget"].get(key, {})).get(window.name, 0)
        return max(0, window.budget - int(used))

    def filter_tier1(self, results: list[ScreenResult],
                     now: datetime | None = None,
                     ignore_window: bool = False,
                     window: AlertWindow | None = None) -> list[ScreenResult]:
        """즉시 속보 대상만 남긴다. 통과 못한 건은 다이제스트로 이월."""
        now = now or now_kst()
        today = now.date()

        # 휴장일이면 시각이 창 안이어도 발송하지 않는다. 토요일 10시에
        # 창이 열려 금요일 데이터로 알림이 나가는 것을 막는다.
        if not ignore_window and self.store is not None:
            if is_definitely_closed(self.store, now):
                return []

        win = window or self.current_window(now)
        if win is None:
            if not ignore_window:
                return []
            # 테스트용 강제 실행. 최우선 창 규격을 빌려 쓴다.
            win = WINDOWS[1]

        g = self.cfg.gate
        picked: list[ScreenResult] = []
        budget = self._window_budget_left(today, win)
        for r in results:
            if budget <= 0:
                break
            if r.excluded:
                continue
            # G1: 등급 A 이상 + 트랙별 하한 통과
            if r.grade not in ("S+", "S", "A"):
                continue
            if not (r.value_score >= g.value_threshold
                    or r.trend_score >= g.trend_threshold):
                continue
            # G2b: 이 창이 담당하는 트랙만. 창마다 목적이 다르다.
            if r.track not in win.tracks:
                continue
            if self._cooldown_blocked(r, today):
                continue
            picked.append(r)
            budget -= 1
        return picked

    def commit(self, sent: list[ScreenResult], now: datetime | None = None,
               window: AlertWindow | None = None) -> None:
        now = now or now_kst()
        today = now.date().isoformat()
        win = window or self.current_window(now) or WINDOWS[1]
        for r in sent:
            self.state["sent"][r.ticker] = {
                "date": now.date().isoformat(),
                "score": max(r.value_score, r.trend_score),
                "grade": r.grade,
                "window": win.name,
            }
        self.state["budget"][today] = self.state["budget"].get(today, 0) + len(sent)
        wb = self.state["window_budget"].setdefault(today, {})
        wb[win.name] = int(wb.get(win.name, 0)) + len(sent)
        self.save()


def _split(text: str, limit: int = TELEGRAM_MAX - 128) -> list[str]:
    """줄 단위로 안전 분할. 태그가 잘리지 않도록 줄 경계만 사용한다."""
    if len(text) <= limit:
        return [text]
    chunks, buf = [], ""
    for line in text.split("\n"):
        if len(buf) + len(line) + 1 > limit:
            if buf:
                chunks.append(buf)
            buf = line[:limit]
        else:
            buf = f"{buf}\n{line}" if buf else line
    if buf:
        chunks.append(buf)
    return chunks


def send_telegram(text: str, chat_id: str | None = None,
                  token: str | None = None, retries: int = 3) -> bool:
    """HTML 모드 발송. 429 재시도 및 4096자 분할 처리."""
    token = token or os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = chat_id or os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise TelegramNotConfigured(
            "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 미설정 — "
            ".env 를 만드십시오 (.env.example 참조)")

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    ok = True
    for chunk in _split(text):
        for attempt in range(retries):
            res = requests.post(
                url,
                json={"chat_id": chat_id, "text": chunk,
                      "parse_mode": "HTML", "disable_web_page_preview": True},
                timeout=10,
            )
            if res.status_code == 200:
                break
            if res.status_code == 429:
                wait = int(res.json().get("parameters", {})
                           .get("retry_after", 2 ** attempt)) + 1
                time.sleep(wait)
                continue
            if attempt == retries - 1:
                ok = False
            else:
                time.sleep(2 ** attempt)
        time.sleep(0.4)  # 연속 발송 시 flood 방지
    return ok
