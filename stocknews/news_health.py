# -*- coding: utf-8 -*-
"""[G] 뉴스 소스 건강 — 소스별 수집 건수 기록 · 연속 0건 관리자 알림 · 브리핑 한 줄.

2026-09-09~10 에 네이버가 finance.naver.com/news 를 SPA 로 옮긴 뒤 63일 동안
네이버 수집이 0건이었는데, news 단계는 매일 rc 0 이었다. 합계 건수만 보면
다른 소스가 그 구멍을 가려서 아무도 몰랐다. 그래서 소스별로 센다.

기록 위치 — runs.note 의 `news_sources=naver:60,google:212,yahoo:48,dart:31`
------------------------------------------------------------------------
새 컬럼 대신 note 를 쓴다. (1) 배치 이력은 main() 이 한 곳에서 쓴다는 원칙
(AGENTS 10장)을 그대로 지킨다 — 모드는 SUMMARY["news_sources"] 에 남기고
main() 의 `_run_note` 가 싣는다. (2) `--mode runs` 가 note 를 그대로 보여주므로
사람이 이력만 보고 바로 읽는다. (3) runs 스키마·log_run 시그니처를 건드리지
않아 병합 면이 작다. 끈 소스는 키 자체가 없다(0 과 구분).

연속 0건 판정 — 상태 변화 때만 알린다
------------------------------------
'수집 회차' = 수집을 실제로 한 실행(news 잡 · nightly news 단계 · 수집하는
브리핑). 평일이 아니거나 휴장일(trading_day.is_definitely_closed)인 회차는
세지도 끊지도 않는다. 그 소스를 끈 회차(키 없음)도 마찬가지다.

  직전까지의 연속 < N ≤ 이번 포함 연속   → '0건 N회 연속' 1회
  직전까지의 연속 ≥ N  그리고 이번 > 0  → '복구' 1회

알림 여부를 따로 저장하지 않고 이력에서 매번 다시 계산한다. 같은 사태가
이어지는 동안(N+1, N+2 …회째)은 조건이 거짓이라 반복 알림이 없고, 상태
파일이 없으니 어긋날 것도 없다. 대가는 '알림 발송이 실패한 회차' 를 다시
보내지 않는다는 점이다 — 그 경우는 SUMMARY 에 error 로 남는다.
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta

log = logging.getLogger(__name__)

__all__ = ["NOTE_KEY", "SOURCE_NAMES", "format_counts", "parse_counts",
           "load_rounds", "zero_streak", "evaluate", "alert_text",
           "check_and_alert", "source_line", "append_line", "remove_line"]

NOTE_KEY = "news_sources"
SOURCE_NAMES = {"naver": "네이버", "google": "Google News",
                "yahoo": "Yahoo Finance", "dart": "DART"}
_NOTE_RE = re.compile(r"(?:^|\s)news_sources=(\S+)")


# ── runs.note 직렬화 ──
def format_counts(counts: dict) -> str:
    """{'naver': 60, ...} → 'naver:60,...'. 켜진 소스가 없으면 '-'."""
    return ",".join(f"{k}:{int(v)}" for k, v in counts.items()) or "-"


def parse_counts(note) -> dict | None:
    """note 에서 소스별 건수. 수집 회차가 아니면(키 없음) None."""
    m = _NOTE_RE.search(str(note or ""))
    if not m:
        return None
    raw = m.group(1)
    out: dict = {}
    if raw == "-":
        return out
    for part in raw.split(","):
        k, sep, v = part.partition(":")
        if sep and k and v.isdigit():
            out[k] = int(v)
    return out


def load_rounds(store, days: int | None = None) -> list[tuple[datetime, dict]]:
    """runs 에서 수집 회차를 오래된 순으로. [(started naive KST, counts)]."""
    from .config import NEWS_SOURCE_HISTORY_DAYS
    days = NEWS_SOURCE_HISTORY_DAYS if days is None else days
    df = store.run_history(days=days, limit=5000)
    rows: list[tuple[datetime, dict]] = []
    if df is None or df.empty:
        return rows
    for _, r in df.iloc[::-1].iterrows():         # rowid 오름차순
        c = parse_counts(r.get("note"))
        if c is None:
            continue
        try:
            t = datetime.fromisoformat(str(r.get("started"))[:19])
        except ValueError:
            continue
        rows.append((t, c))
    rows.sort(key=lambda x: x[0])                 # 안정 정렬 — 같은 시각은 rowid 순
    return rows


def _counted_fn(store):
    """평일·비휴장 판정(날짜별 캐시). store 없으면 요일만 본다."""
    cache: dict[date, bool] = {}

    def counted(t) -> bool:
        d = t.date() if isinstance(t, datetime) else t
        if d not in cache:
            ok = d.weekday() < 5
            if ok and store is not None:
                try:
                    from .trading_day import is_definitely_closed
                    ok = not is_definitely_closed(store, d)
                except Exception:  # noqa: BLE001 - 판정 실패면 평일로 센다
                    ok = True
            cache[d] = ok
        return cache[d]
    return counted


# ── 연속 0건 ──
def zero_streak(rounds, src: str, counted) -> int:
    """끝에서부터 센 연속 0건 회차 수. 세지 않는 회차는 건너뛴다."""
    n = 0
    for t, c in reversed(rounds):
        if not counted(t) or src not in c:
            continue
        if int(c[src]) > 0:
            break
        n += 1
    return n


def evaluate(prev_rounds, current: dict, when: datetime, counted,
             threshold: int) -> list[dict]:
    """이번 회차로 상태가 바뀐 소스만. 세지 않는 회차면 []."""
    if not counted(when):
        return []
    allr = list(prev_rounds) + [(when, current)]
    events: list[dict] = []
    for src, cnt in current.items():
        prev = zero_streak(prev_rounds, src, counted)
        cur = zero_streak(allr, src, counted)
        if prev < threshold <= cur:
            events.append({"source": src, "kind": "down", "streak": cur})
        elif int(cnt) > 0 and prev >= threshold:
            events.append({"source": src, "kind": "up", "after": prev,
                           "count": int(cnt)})
    return events


def alert_text(events: list[dict], warns: dict | None = None) -> str:
    """관리자 채팅용. 사건마다 한 줄."""
    warns = warns or {}
    lines = []
    for e in events:
        name = SOURCE_NAMES.get(e["source"], e["source"])
        if e["kind"] == "down":
            line = (f"⚠️ 뉴스 소스 {name} 0건 {e['streak']}회 연속 "
                    "(평일 수집 회차)")
            why = warns.get(e["source"])
            if why:
                line += f" · 원인: {why[:200]}"
        else:
            line = (f"✅ 뉴스 소스 {name} 복구 — {e['count']}건 "
                    f"(0건 {e['after']}회 뒤)")
        lines.append(line)
    return "\n".join(lines)


def check_and_alert(store, counts: dict, dry_run: bool,
                    now: datetime | None = None,
                    warns: dict | None = None) -> dict:
    """이번 수집 회차를 이력과 합쳐 판정하고, 상태가 바뀐 소스만 알린다.

    실패해도 예외를 내지 않는다 — 수집·브리핑을 막으면 본말전도다.
    반환은 SUMMARY 용 요약({} = 알릴 것 없음).
    """
    from .config import NEWS_SOURCE_ZERO_STREAK
    from .trading_day import now_kst
    info: dict = {}
    try:
        when = now or now_kst()
        rounds = load_rounds(store)
        events = evaluate(rounds, dict(counts), when, _counted_fn(store),
                          int(NEWS_SOURCE_ZERO_STREAK))
        if not events:
            return info
        text = alert_text(events, warns)
        info = {"events": events, "text": text}
        from .notify import send_admin
        rep = send_admin(text, dry_run=dry_run)
        info["sent"] = None if rep is None else bool(rep)
    except Exception as exc:  # noqa: BLE001 - 알림 실패가 수집을 막지 않게
        log.warning("뉴스 소스 알림 실패: %s", exc)
        info["error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
    return info


# ── 브리핑 맨 아래 한 줄 ──
def source_line(store, enabled, hours: float, current: dict | None = None,
                now: datetime | None = None) -> tuple[str, dict]:
    """'뉴스 소스 n개 중 m개 수집'.

    n = 켜진 소스 수(--no-* 로 끈 소스·키 없는 DART 제외).
    m = 그중 '브리핑 창'(최근 hours 시간) 안에 시작한 수집 회차들(+ 이번
        실행이 수집했다면 그 회차)에서 한 번이라도 1건 이상 수집된 소스 수.
        건수는 중복 제거 전 원본이다. 창 안에 수집 회차가 없으면 m = 0.
    news 테이블의 published 로 세지 않는 이유: DART 는 접수일 00:00 이
    발행 시각이라 아침 16시간 창 밖으로 밀려, 수집이 정상이어도 매일
    '4개 중 3개' 가 된다.
    """
    from .trading_day import now_kst
    now = now or now_kst()
    since = now - timedelta(hours=float(hours))
    window: list[dict] = []
    try:
        window = [c for t, c in load_rounds(store) if t >= since]
    except Exception as exc:  # noqa: BLE001 - 한 줄 때문에 브리핑을 막지 않는다
        log.warning("뉴스 소스 이력 조회 실패: %s", exc)
    if current is not None:
        window.append(current)
    enabled = list(enabled)
    got = [s for s in enabled if any(int(c.get(s, 0)) > 0 for c in window)]
    line = f"뉴스 소스 {len(enabled)}개 중 {len(got)}개 수집"
    return line, {"n": len(enabled), "m": len(got),
                  "missing": [s for s in enabled if s not in got],
                  "rounds": len(window)}


def append_line(text: str, line: str) -> str:
    """브리핑 맨 아래에 한 줄. 마지막 줄이 '⏰ 시각' 이면 그 바로 위."""
    lines = str(text).rstrip("\n").split("\n")
    if lines and lines[-1].startswith("⏰"):
        lines.insert(len(lines) - 1, line)
        return "\n".join(lines)
    return "\n".join(lines) + "\n\n" + line


_LINE_RE = re.compile(r"^뉴스 소스 \d+개 중 \d+개 수집$")


def remove_line(text: str) -> str:
    """append_line 을 되돌린다(검사용: '그 한 줄 외에는 그대로인가')."""
    lines = str(text).split("\n")
    if len(lines) >= 2 and lines[-1].startswith("⏰") and _LINE_RE.match(lines[-2]):
        return "\n".join(lines[:-2] + lines[-1:])
    if len(lines) >= 3 and _LINE_RE.match(lines[-1]) and lines[-2] == "":
        return "\n".join(lines[:-2])
    return str(text)
