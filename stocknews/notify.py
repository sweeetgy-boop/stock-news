# -*- coding: utf-8 -*-
"""알림 게이트 + 텔레그램 발송.

"너무 많아 또 보지도 못하잖아"를 해결하는 4중 게이트와 3계층 리듬.
핵심은 티어2(장마감 다이제스트)를 매일 고정 발송하는 것이다. 그러면
티어1 허들을 8.0점으로 높게 유지해도 놓치는 게 없다. "혹시 놓칠까 봐"
허들을 낮추는 게 알림 폭주의 진짜 원인이다.
"""
from __future__ import annotations

import html
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path

import requests

from .config import Config, DEFAULT
from .contracts import ScreenResult
from .trading_day import is_definitely_closed, trading_days_between

__all__ = ["AlertWindow", "WINDOWS", "AlertGate", "send_telegram", "now_kst",
           "TelegramNotConfigured", "DOW_KR", "reco_send_allowed",
           "reco_send_dow_label", "parse_chat_ids", "mask_chat_id",
           "SendFailure", "SendReport", "send_photo", "fit_caption",
           "send_album", "album_payload", "MEDIA_GROUP_MAX",
           "caption_units", "TELEGRAM_CAPTION_MAX"]

log = logging.getLogger("notify")


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


# `datetime.weekday()` 순서 (월=0 … 일=6)
DOW_KR = ("월", "화", "수", "목", "금", "토", "일")


def reco_send_dow_label(cfg: Config = DEFAULT) -> str:
    """설정된 발송 요일의 한글 이름. 사람이 읽는 문구에만 쓴다."""
    return DOW_KR[int(cfg.gate.reco_send_dow) % 7] + "요일"


def reco_send_allowed(now: datetime | None = None, cfg: Config = DEFAULT,
                      force: bool = False) -> tuple[bool, str]:
    """추천 10선을 오늘 내보내도 되는가. (허용여부, 사유코드).

    G5(요일). 기존 G1~G4 와 독립이며, 오직 요일만 본다.

    시각을 보지 않는 것이 의도다. `WINDOWS`/`AlertGate` 의 4대 시간창은
    장중 즉시 속보(flash)의 유량 조절 장치이고, 이 게이트는 장 마감 후
    배치(daily)의 발송 리듬이다. 둘을 한 판정에 섞으면 daily 를 몇 시에
    돌리느냐에 따라 주간 발송이 조용히 사라진다 — 16:05 는 어느 창에도
    속하지 않기 때문이다. 그래서 서로 참조하지 않는다.

    사유코드
      force     --force 로 요일 무시
      send_dow  오늘이 발송 요일
      off_dow   발송 요일이 아님 (보유 요약만 나간다)
    """
    now = now or now_kst()
    if force:
        return True, "force"
    if now.weekday() == int(cfg.gate.reco_send_dow) % 7:
        return True, "send_dow"
    return False, "off_dow"


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
# 일일 상한 = 아래 budget 의 합 (2+3+2+2 = 9). 별도의 전역 일일 예산은 없다.
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
    """G1 점수 · G2 시간창 · G3 종목 쿨다운 · G4 창별 예산 (합 = 일일 상한 9건)."""

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


# 재시도해도 낫지 않는 응답. 400 은 chat not found(오타이거나 상대가
# 봇에게 /start 를 누른 적이 없음), 403 은 차단·추방, 401 은 토큰 자체가
# 틀린 것이다. 셋 다 2초 뒤에 다시 두드린다고 달라지지 않는다.
_PERMANENT = frozenset({400, 401, 403, 404})


def mask_chat_id(chat_id: str) -> str:
    """로그·알림에 남길 표시용 ID. 마지막 4자리만 남긴다.

    채팅방 ID 는 그 자체로 발송 대상을 특정하는 식별자다. 로그 파일과
    텔레그램 요약문은 사람 눈에 그대로 노출되므로 전체를 적지 않는다.
    뒤 4자리만 있어도 "둘 중 어느 쪽이 막혔나"는 구분된다.
    """
    s = str(chat_id).strip()
    return f"****{s[-4:]}" if len(s) >= 4 else "****"


def parse_chat_ids(raw: str | None) -> list[str]:
    """쉼표/세미콜론/공백으로 나열된 수신자 목록. 순서 유지, 중복 제거.

    중복을 지우는 이유는 같은 사람에게 같은 메시지가 두 번 가는 것이
    설정 오타의 흔한 결과이기 때문이다.
    """
    if not raw:
        return []
    out: list[str] = []
    for tok in re.split(r"[,;\s]+", raw.strip()):
        if tok and tok not in out:
            out.append(tok)
    return out


@dataclass(frozen=True)
class SendFailure:
    """수신자 한 명의 발송 실패."""

    chat: str            # 마스킹된 표시용 ID (****1234)
    status: int | None   # None = 응답을 받지 못함 (네트워크·타임아웃)
    error: str

    def label(self) -> str:
        return f"{self.chat}: {self.status or '무응답'}"


@dataclass(frozen=True)
class SendReport:
    """발송 결과.

    기존 호출부가 `ok = send_telegram(text)` 로 쓰고 있으므로 bool 로도
    동작하게 둔다(실패가 하나도 없을 때 참). 수신자별 상세는 .failures
    로 꺼낸다 — 이게 없으면 "몇 명 중 누가 왜 못 받았나"가 사라진다.
    """

    sent: int
    failures: tuple[SendFailure, ...] = ()
    # 앨범이 실패해 개별 사진으로 받은 수신자 (마스킹된 ID). 실패는 아니다.
    fallback: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return not self.failures

    def summary(self) -> str:
        """'발송 실패 2건 (수신자 ****1234: 400, ****5678: 403)'."""
        if not self.failures:
            return ""
        detail = ", ".join(f.label() for f in self.failures)
        return f"발송 실패 {len(self.failures)}건 (수신자 {detail})"

    def as_dicts(self) -> list[dict]:
        """--json 요약에 실을 형태. 마스킹된 ID 만 나간다."""
        return [{"chat": f.chat, "status": f.status, "error": f.error}
                for f in self.failures]


def _describe(res) -> str:
    """텔레그램이 준 description. JSON 이 아니면 본문 앞부분."""
    try:
        body = res.json()
    except ValueError:
        return res.text[:200].replace("\n", " ").strip()
    if isinstance(body, dict) and body.get("description"):
        return str(body["description"])[:200]
    return str(body)[:200]


def _retry_after(res, attempt: int) -> int:
    """429 가 지시한 대기 초. 없으면 지수 백오프로 떨어진다."""
    try:
        given = res.json().get("parameters", {}).get("retry_after")
    except (ValueError, AttributeError):
        given = None
    try:
        return int(given) + 1
    except (TypeError, ValueError):
        return 2 ** attempt + 1


def _deliver(who: str, what: str, post, retries: int) -> SendFailure | None:
    """요청 1건을 재시도 규칙대로 보낸다. 성공 None, 실패 SendFailure.

    post() 는 매 시도마다 새로 부른다 — 사진 업로드는 파일 핸들을 시도마다
    다시 열어야 하기 때문이다(한 번 읽힌 스트림은 재전송되지 않는다).

    응답 코드와 텔레그램이 준 description 을 그대로 로그에 남긴다 — 이게
    없으면 400(방을 못 찾음)과 403(차단됨)을 구분할 수 없고, 그 둘은
    사람이 해야 할 조치가 완전히 다르다.

    영구 실패는 즉시 포기한다. 세 번 더 두드려서 얻는 것이 없다.
    """
    status: int | None = None
    desc = ""
    for attempt in range(retries):
        try:
            res = post()
        except requests.RequestException as exc:
            status, desc = None, f"{type(exc).__name__}: {exc}"
            log.warning("텔레그램 %s 요청 실패 (%d/%d): %s",
                        who, attempt + 1, retries, desc)
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
            continue

        status = res.status_code
        if status == 200:
            log.info("텔레그램 %s 발송 200 (%s)", who, what)
            return None
        desc = _describe(res)
        if status == 429:
            wait = _retry_after(res, attempt)
            log.warning("텔레그램 %s 발송 429 — %d초 후 재시도: %s",
                        who, wait, desc)
            time.sleep(wait)
            continue
        log.error("텔레그램 %s 발송 실패 %d: %s", who, status, desc)
        if status in _PERMANENT:
            return SendFailure(who, status, desc)
        if attempt < retries - 1:
            time.sleep(2 ** attempt)
    # 재시도를 다 쓰고도 200 을 못 봤다.
    log.error("텔레그램 %s 재시도 소진 (마지막 %s)", who, status or "무응답")
    return SendFailure(who, status, desc or "재시도 소진")


def _send_one(token: str, chat_id: str, chunks: list[str],
              retries: int) -> SendFailure | None:
    """수신자 한 명에게 분할된 청크를 순서대로 보낸다."""
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    who = mask_chat_id(chat_id)
    for idx, chunk in enumerate(chunks, 1):
        fail = _deliver(
            who, f"청크 {idx}/{len(chunks)}",
            lambda chunk=chunk: requests.post(
                url,
                json={"chat_id": chat_id, "text": chunk,
                      "parse_mode": "HTML",
                      "disable_web_page_preview": True},
                timeout=10,
            ),
            retries)
        if fail is not None:
            return fail
        time.sleep(0.4)  # 연속 발송 시 flood 방지
    return None


def _targets(chat_id: str | None, token: str | None) -> tuple[str, list[str]]:
    token = token or os.getenv("TELEGRAM_BOT_TOKEN")
    targets = parse_chat_ids(chat_id or os.getenv("TELEGRAM_CHAT_ID"))
    if not token or not targets:
        raise TelegramNotConfigured(
            "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 미설정 — "
            ".env 를 만드십시오 (.env.example 참조)")
    return token, targets


def send_telegram(text: str, chat_id: str | None = None,
                  token: str | None = None, retries: int = 3) -> SendReport:
    """HTML 모드 발송. 429 재시도 및 4096자 분할 처리.

    chat_id 는 쉼표로 여러 명을 나열할 수 있다. 한 명이 실패해도 나머지
    수신자에게는 계속 보낸다 — 한 사람의 차단이 전체 무음이 되면 안 된다.

    반환값은 SendReport 다. `if send_telegram(...)` 처럼 bool 로 써도
    되고, 수신자별 실패 상세가 필요하면 .failures 를 본다.
    """
    token, targets = _targets(chat_id, token)
    chunks = _split(text)
    sent = 0
    failures: list[SendFailure] = []
    for target in targets:
        fail = _send_one(token, target, chunks, retries)
        if fail is None:
            sent += 1
        else:
            failures.append(fail)
    if failures:
        log.error("텔레그램 %d명 중 %d명 실패", len(targets), len(failures))
    return SendReport(sent=sent, failures=tuple(failures))


# sendPhoto 캡션 상한. 텍스트 메시지(4096)와 달리 분할 발송이 없다 —
# 넘으면 400 "message caption is too long" 으로 사진째 실패한다.
TELEGRAM_CAPTION_MAX = 1024
_TAG = re.compile(r"<[^>]+>")


def caption_units(text: str) -> int:
    """텔레그램이 세는 방식에 가까운 길이. UTF-16 코드 유닛 수.

    텔레그램은 엔티티 파싱 뒤 글자 수를 UTF-16 으로 센다. 이모지(BMP 밖)는
    2로 세므로 len() 보다 길게 나온다. 태그까지 세므로 실제보다 보수적이다.
    """
    return len(str(text).encode("utf-16-le")) // 2


def fit_caption(text: str, limit: int = TELEGRAM_CAPTION_MAX
                ) -> tuple[str, str | None]:
    """캡션을 상한 안으로 줄인다. (캡션, parse_mode).

    줄 단위로 뒤에서부터 버린다. 태그가 줄을 넘지 않게 조판하므로 줄을
    통째로 버리면 HTML 이 깨지지 않는다. 첫 줄 하나가 이미 상한을 넘는
    극단적인 경우만 태그를 벗기고 평문으로 자른다 — 태그나 `&amp;` 중간을
    자르면 400 "can't parse entities" 로 사진까지 못 보낸다.
    """
    text = str(text or "")
    if caption_units(text) <= limit:
        return text, "HTML"
    lines = text.split("\n")
    more = "…"
    while len(lines) > 1:
        lines.pop()
        cand = "\n".join(lines + [more])
        if caption_units(cand) <= limit:
            return cand, "HTML"
    plain = html.unescape(_TAG.sub("", lines[0]))
    while plain and caption_units(plain + more) > limit:
        plain = plain[:-1]
    return plain + more, None


def send_photo(path, caption: str = "", chat_id: str | None = None,
               token: str | None = None, retries: int = 3) -> SendReport:
    """사진 1장 + 캡션. 수신자·재시도·실패 기록은 send_telegram 과 같다.

    캡션은 1,024자(UTF-16) 상한이라 `fit_caption` 으로 줄 단위로 줄인다.
    파일이 없으면 FileNotFoundError — 네트워크에 가기 전에 멈춘다.
    """
    token, targets = _targets(chat_id, token)
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"사진 파일 없음: {path}")
    cap, mode = fit_caption(caption)
    if cap != (caption or ""):
        log.warning("캡션 %d -> %d (상한 %d) — 뒷줄을 잘랐습니다",
                    caption_units(caption), caption_units(cap),
                    TELEGRAM_CAPTION_MAX)
    sent = 0
    failures: list[SendFailure] = []
    for target in targets:
        fail = _deliver(mask_chat_id(target), f"사진 {path.name}",
                        _photo_post(token, target, path, cap, mode), retries)
        if fail is None:
            sent += 1
        else:
            failures.append(fail)
        time.sleep(0.4)  # 연속 발송 시 flood 방지
    if failures:
        log.error("텔레그램 사진 %d명 중 %d명 실패", len(targets), len(failures))
    return SendReport(sent=sent, failures=tuple(failures))


def _photo_post(token: str, target: str, path: Path, cap: str,
                mode: str | None):
    """sendPhoto 요청 함수. 부를 때마다 파일을 새로 연다 (재시도 대비)."""
    url = f"https://api.telegram.org/bot{token}/sendPhoto"
    data = {"chat_id": target, "caption": cap}
    if mode:
        data["parse_mode"] = mode

    def post():
        with path.open("rb") as fh:
            return requests.post(url, data=data,
                                 files={"photo": (path.name, fh, "image/png")},
                                 timeout=30)
    return post


# 앨범 상한. 텔레그램 sendMediaGroup 은 2~10장을 받는다.
MEDIA_GROUP_MAX = 10
# 앨범이 이 코드로 실패하면 개별 발송으로 폴백하지 않는다. 토큰(401)·
# 차단(403)·방 없음(404)은 수신자 단위 문제라 사진을 한 장씩 보내도 같다.
# 400 은 폴백한다 — 앨범 구성·캡션 파싱처럼 요청 모양 탓일 수 있다.
_NO_FALLBACK = frozenset({401, 403, 404})


def album_payload(items: list[tuple[Path, str, str | None]]
                  ) -> tuple[list[dict], list[str]]:
    """sendMediaGroup 의 media 배열과 첨부 이름. 네트워크 없음 (dry-run 이 쓴다).

    items : [(경로, 캡션, parse_mode)] — 캡션은 이미 fit_caption 을 거친 것.
    사진마다 캡션을 단다. 앨범에서는 넘겨 볼 때 사진별 캡션이 보인다.
    """
    media, names = [], []
    for i, (_path, cap, mode) in enumerate(items):
        name = f"photo{i}"
        m = {"type": "photo", "media": f"attach://{name}", "caption": cap}
        if mode:
            m["parse_mode"] = mode
        media.append(m)
        names.append(name)
    return media, names


def _album_post(token: str, target: str,
                items: list[tuple[Path, str, str | None]]):
    url = f"https://api.telegram.org/bot{token}/sendMediaGroup"
    media, names = album_payload(items)
    data = {"chat_id": target, "media": json.dumps(media, ensure_ascii=False)}

    def post():
        from contextlib import ExitStack
        with ExitStack() as stack:
            files = {n: (p.name, stack.enter_context(p.open("rb")), "image/png")
                     for n, (p, _c, _m) in zip(names, items)}
            # 10장 x 약 110KB. 사진 1장(30초)보다 넉넉히 준다.
            return requests.post(url, data=data, files=files, timeout=90)
    return post


def _one_by_one(token: str, target: str,
                items: list[tuple[Path, str, str | None]],
                retries: int) -> SendFailure | None:
    """앨범 폴백. 사진을 순서대로 한 장씩. 한 장이라도 실패하면 거기서 멈춘다.

    멈추는 이유: 네트워크가 죽었으면 남은 사진도 장당 재시도 x 타임아웃을
    다 쓰고 같은 결과가 된다. 몇 장까지 갔는지는 실패 사유에 남긴다.
    """
    who = mask_chat_id(target)
    for i, (path, cap, mode) in enumerate(items, 1):
        fail = _deliver(who, f"개별 {i}/{len(items)} {path.name}",
                        _photo_post(token, target, path, cap, mode), retries)
        if fail is not None:
            return SendFailure(who, fail.status,
                               f"개별 발송 {i - 1}/{len(items)}장 후 실패: "
                               f"{fail.error}")
        time.sleep(0.4)
    return None


def send_album(items: list[tuple], chat_id: str | None = None,
               token: str | None = None, retries: int = 3,
               fallback: bool = True) -> SendReport:
    """사진 여러 장을 앨범(sendMediaGroup)으로. 10장이 넘으면 앨범을 나눈다.

    items : [(경로, 캡션)] 순서대로. 캡션은 장마다 1,024자로 줄인다.
    수신자·재시도·실패 기록은 send_telegram 과 같다. 앨범이 실패한
    수신자에게만 send_photo 방식으로 한 장씩 다시 보낸다 — 앨범을 이미
    받은 수신자에게 같은 사진이 두 번 가면 안 된다. 폴백으로 받은 수신자는
    실패가 아니고 `.fallback` 에 남는다. 1장뿐인 묶음은 sendPhoto 로 간다
    (sendMediaGroup 은 2장 이상만 받는다).
    """
    token, targets = _targets(chat_id, token)
    prepared = []
    for path, caption in items:
        path = Path(path)
        if not path.is_file():
            raise FileNotFoundError(f"사진 파일 없음: {path}")
        cap, mode = fit_caption(caption)
        prepared.append((path, cap, mode))
    if not prepared:
        return SendReport(sent=0)
    groups = [prepared[i:i + MEDIA_GROUP_MAX]
              for i in range(0, len(prepared), MEDIA_GROUP_MAX)]

    sent = 0
    failures: list[SendFailure] = []
    used_fallback: list[str] = []
    for target in targets:
        who = mask_chat_id(target)
        fail = None
        for g in groups:
            if len(g) == 1:
                p, c, m = g[0]
                fail = _deliver(who, f"사진 {p.name}",
                                _photo_post(token, target, p, c, m), retries)
            else:
                fail = _deliver(who, f"앨범 {len(g)}장",
                                _album_post(token, target, g), retries)
                if (fail is not None and fallback
                        and fail.status not in _NO_FALLBACK):
                    log.warning("텔레그램 %s 앨범 실패 (%s) — 개별 발송으로 폴백",
                                who, fail.status or "무응답")
                    fail = _one_by_one(token, target, g, retries)
                    if fail is None and who not in used_fallback:
                        used_fallback.append(who)
            if fail is not None:
                break
            time.sleep(0.4)
        if fail is None:
            sent += 1
        else:
            failures.append(fail)
    if failures:
        log.error("텔레그램 앨범 %d명 중 %d명 실패", len(targets), len(failures))
    return SendReport(sent=sent, failures=tuple(failures),
                      fallback=tuple(used_fallback))
