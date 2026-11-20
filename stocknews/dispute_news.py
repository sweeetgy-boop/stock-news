# -*- coding: utf-8 -*-
"""[S] 경영권 분쟁 "첫 보도" 감지 (2026-11-20 지시). 기록·표시 전용.

수집해 둔 기사(news · news_tickers)의 **제목**에서 경영권 분쟁 관련 표현을 찾는다
(요약은 수집 단계에서 비어 있어 쓰지 않는다). 종목 연결은 뉴스 정리 단계의 이름
매칭(news.map_tickers · 오탐 방지 규칙 포함) 결과를 그대로 쓴다.

  감지  기사 한 건이 ① 국내 기사이고 공시 목록(DART)에서 온 것이 아니며 ② 연결된 종목이
        정확히 1개이고 ③ 표현 목록(EXPRESSIONS)에 걸리고 ④ 제외 규칙(EXCLUDES)에 안 걸림.
  사건  종목별로 묶는다. 그 종목의 직전 관련 기사로부터 60일 안이면 같은 사건(기사 수만
        늘린다). 60일 넘게 조용했으면 새 사건.
  유형  first        = 새 사건이고, 직전 120거래일 동안 분쟁 공시(sig_dispute)가 없었음
        after_filing = 새 사건이지만 분쟁 공시가 이미 있었음("공시 뒤 보도" — 기록만)
  표시  first 이고, 운영 유니버스(그날 scans) 안이고, 그 종목을 60일 안에 표시한 적이 없고,
        아직 표시하지 않은 사건. 처음 켠 날 과거 기사에서 한꺼번에 잡힌 사건(첫 기사가 24시간
        보다 오래됨)은 '소급'으로 기록만 한다.

**점수·추천 선정·recos 기록에 닿지 않는다.** DISPUTE_NEWS_LINE / DISPUTE_NEWS_INSTANT 가
둘 다 OFF 면 아무것도 하지 않는다.
"""
from __future__ import annotations

import logging
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta

import numpy as np

from . import config as C
from . import events as E

log = logging.getLogger(__name__)

__all__ = ["line_on", "instant_on", "enabled", "classify", "detect", "maybe_detect",
           "collect_watch", "is_first_run",
           "to_show", "recent_first_tickers", "mark_shown", "link_filings",
           "EXPRESSIONS", "EXCLUDES", "WATCH_QUERIES", "KIND_FIRST", "KIND_AFTER"]

KIND_FIRST = "first"
KIND_AFTER = "after_filing"
SAME_EVENT_DAYS = 60          # 같은 사건으로 묶는 간격 · 다시 표시하지 않는 기간(달력일)
PRIOR_FILING_TDAYS = 120      # 분쟁 공시를 되돌아보는 거래일 수
BACKFILL_HOURS = 24
SCAN_DAYS = 70                # 수집일 기준으로 다시 훑는 범위
TITLE_MAX = 40

# (표시 이름, 정규식) — 제목에 그대로 적용한다. 위에서부터 처음 걸린 것이 유형 이름이다.
EXPRESSIONS: tuple[tuple[str, str], ...] = (
    ("경영권 분쟁", r"경영권\s*(분쟁|다툼|싸움|갈등|공격|방어|위협|탈취|쟁탈|노리|노려|겨냥|흔들)"),
    ("적대적 M&A", r"적대적\s*(M\s*&\s*A|인수|합병|공개\s*매수)"),
    ("표 대결", r"표\s*(대결|싸움)|위임장\s*(대결|경쟁|확보전)|의결권\s*(대결|경쟁|확보전)"),
    ("지분 경쟁", r"지분\s*(경쟁(?!력)|싸움|전쟁|확보전|매집)"),
    ("공개매수", r"공개\s*매수"),
    ("주주제안", r"주주\s*제안"),
    ("임시주총 소집 요구",
     r"임시\s*주(주\s*)?총(회)?\s*(소집\s*)?(요구|청구|허가|신청)|주총\s*소집\s*(요구|청구|허가)"),
    ("행동주의",
     r"행동주의.{0,30}(지분|서한|요구|제안|캠페인|공세|압박|취득|매입|확보|타깃|겨냥|반대|개편)"
     r"|(지분|서한|요구|제안|압박).{0,14}행동주의"
     r"|(펀드|주주|얼라인|KCGI|트러스톤|차파트너스|플래쉬라이트|팰리서|엘리엇|밸류파트너스|안다자산).{0,14}(공개\s*)?서한"),
    ("경영 참여", r"경영\s*참여\s*(선언|목적|요구|나서|본격|으로\s*변경)"),
    ("최대주주 변경 시도",
     r"최대\s*주주\s*(변경|교체)\s*시도|이사\s*해임|이사회\s*장악|경영진\s*(교체|해임)\s*(요구|추진)"
     r"|대표(이사)?\s*해임\s*(요구|안건|추진)"),
    ("가처분", r"(주총|주주\s*총회|이사회|의결권|신주\s*발행).{0,10}가처분"),
)
# 다툼이 분명한 표현 — 아래 '우호적 거래' 제외를 이긴다.
_HARD = re.compile(r"분쟁|다툼|적대적|표\s*(대결|싸움)|해임|가처분|행동주의|주주\s*제안")
# (이유, 정규식, 다툼이 분명하면 무시하는가, 이 유형에만 적용(None = 전부))
EXCLUDES: tuple[tuple[str, str, bool, tuple[str, ...] | None], ...] = (
    ("우호적 거래·자금 조달",
     r"유상\s*증자|유증|제?\s*3\s*자\s*배정|양수도|경영권\s*(매각|인수|양도|이전|변경|프리미엄)"
     r"|인수\s*(완료|마침표|계약)|M\s*&\s*A\s*실탄", True, None),
    ("제도·정책 기사", r"의무\s*공개\s*매수|제도|법안|상법|개정안|국회|금융위|금감원|공적\s*기금"
                    r"|스튜어드십|차등\s*의결권|대통령|활성화|자본시장\s*개혁", False, None),
    ("자진 상장폐지·자기주식 공개매수",
     r"자진\s*상(장)?\s*폐(지)?|상폐|상장\s*폐지|자사주|자기\s*주식", False, ("공개매수",)),
    ("분쟁 종결 기사",
     r"분쟁\s*(종결|종식|마무리|일단락|해소|끝)|마침표|봉합|일단락|없던\s*일|악재\s*털|소\s*취하|취하\b"
     r"|합의\s*(로|해|했|에)|화해|철회", False, None),
    ("칼럼·시황", r"\[\s*(사설|칼럼|시론|기고|데스크|기자\s*수첩|마감|장중|시황|전망|주간|증시|오늘의|이번\s*주)",
     False, None),
)
_EXPR = tuple((lab, re.compile(rx, re.I)) for lab, rx in EXPRESSIONS)
_EXCL = tuple((why, re.compile(rx, re.I), soft, only) for why, rx, soft, only in EXCLUDES)
_SKIP_CATEGORIES = frozenset({"시황·전망", "국내시황", "해외시황"})
# 후보를 좁히는 SQL LIKE 토막(정규식보다 넓다)
_ANCHORS = ("경영권", "적대적", "대결", "위임장", "의결권", "지분", "공개매수", "공개 매수",
            "주주제안", "주주 제안", "주총", "주주총회", "행동주의", "서한", "경영참여",
            "경영 참여", "최대주주", "최대 주주", "해임", "이사회", "가처분")

# 장중 확인 잡과 뉴스 수집이 추가로 던지는 Google News 검색어 (검색어, region, 분류 힌트)
WATCH_QUERIES: tuple[tuple[str, str, str], ...] = (
    ("경영권 분쟁", "KR", "공시"),
    ("적대적 M&A 공개매수", "KR", "공시"),
    ("주주제안 표 대결", "KR", "공시"),
    ("행동주의 펀드 지분 서한", "KR", "공시"),
    ("임시주총 소집 요구 이사 해임", "KR", "공시"),
    ("경영 참여 선언 지분 매집", "KR", "공시"),
)


SEED_DAYS = 60                # 처음 켠 날 한 번 되돌아보는 검색 범위(일)
SEED_PER_QUERY = 100


def is_first_run(store) -> bool:
    """아직 한 번도 감지를 돌리지 않았는가(처리한 기사가 없다)."""
    with closing(_conn(store)) as con:
        return con.execute("SELECT COUNT(*) FROM sig_dispute_news_items").fetchone()[0] == 0


def collect_watch(days: int = 2, per_query: int = 12, queries=None, get=None, pause: float = 0.5
                  ) -> list[dict]:
    """분쟁 관련 Google News 검색(검색어 6개). news_sources.collect_google 과 같은 형식으로 돌려준다.

    평소에는 최근 2일 · 검색어당 12건. 처음 켠 날에는 60일 · 100건으로 한 번 받아, 이미
    진행 중인 분쟁이 "첫 보도"로 잡히지 않게 한다(그 기사들은 '소급'으로만 기록된다).
    """
    import time as _time
    from urllib.parse import quote_plus
    from . import news_sources as NS
    get = get or NS._get
    out: list[dict] = []
    for query, region, cat in (WATCH_QUERIES if queries is None else queries):
        res = get("https://news.google.com/rss/search?q="
                  f"{quote_plus(query)}+when:{int(days)}d&hl=ko&gl=KR&ceid=KR:ko")
        if res is None:
            continue
        for it in NS._parse_feed(res.text)[:int(per_query)]:
            title, source = it["title"], it.get("source") or ""
            head, sep, tail = title.rpartition(" - ")
            if sep and head and len(tail) <= 20:
                title, source = head, (source or tail)
            out.append({"title": title, "url": it["url"], "source": source or "GoogleNews",
                        "origin": "GOOGLE", "region": region, "category_hint": cat,
                        "published": it["published"], "lang": "ko"})
        if pause:
            _time.sleep(pause)
    return out


def line_on() -> bool:
    return bool(getattr(C, "DISPUTE_NEWS_LINE", False))


def instant_on() -> bool:
    return bool(getattr(C, "DISPUTE_NEWS_INSTANT", False))


def enabled() -> bool:
    return line_on() or instant_on()


def _conn(store) -> sqlite3.Connection:
    con = sqlite3.connect(str(store.path), timeout=30.0)
    con.row_factory = sqlite3.Row
    return con


def _now() -> datetime:
    from .trading_day import now_kst
    return now_kst().replace(tzinfo=None)


def _ts(*cands) -> datetime | None:
    """ISO 문자열(시간대가 붙어 있어도) → KST 기준 naive datetime. 못 읽으면 다음 후보."""
    for s in cands:
        if not s:
            continue
        try:
            t = datetime.fromisoformat(str(s).strip().replace("Z", "+00:00"))
        except ValueError:
            try:
                t = datetime.fromisoformat(str(s).strip()[:10])
            except ValueError:
                continue
        if t.tzinfo is not None:
            from .trading_day import KST
            t = t.astimezone(KST).replace(tzinfo=None)
        return t
    return None


def classify(title: str, origin: str = "", region: str = "KR", category: str = "",
             n_tickers: int = 1) -> tuple[str | None, str | None]:
    """기사 한 건 → (유형 이름, None) 또는 (None, 제외 이유). 표현이 없으면 (None, None)."""
    t = " ".join(str(title or "").split())
    label = next((lab for lab, rx in _EXPR if rx.search(t)), None)
    if label is None:
        return None, None
    if str(origin or "").upper() == "DART" or t.startswith("[공시]"):
        return None, "공시 목록 항목(보도 아님)"
    if str(region or "KR").upper() != "KR":
        return None, "해외 기사"
    if str(category or "") in _SKIP_CATEGORIES:
        return None, "시황 분류"
    hard = bool(_HARD.search(t))
    for why, rx, soft, only in _EXCL:
        if only is not None and label not in only:
            continue
        if rx.search(t) and not (soft and hard):
            return None, why
    if n_tickers != 1:
        return None, "연결된 종목이 없음" if n_tickers == 0 else "여러 종목을 나열"
    return label, None


# ══════════════════════════ 감지 ══════════════════════════
def _prior_filing(con, ticker: str, at: datetime, cal: list[str]) -> str | None:
    """at 직전 120거래일 안의 분쟁 공시 접수일(가장 최근). 없으면 None."""
    d = at.strftime("%Y-%m-%d")
    i = E._pos(cal, d)
    lo = cal[max(0, i - PRIOR_FILING_TDAYS)] if cal and i >= PRIOR_FILING_TDAYS else \
        (at - timedelta(days=int(PRIOR_FILING_TDAYS * 1.5))).strftime("%Y-%m-%d")
    r = con.execute("SELECT MAX(rcept_dt) FROM sig_dispute WHERE ticker=? AND rcept_dt>=? AND rcept_dt<=?",
                    (ticker, lo, d)).fetchone()
    return r[0] if r and r[0] else None


def detect(store, now: datetime | None = None, scan_days: int | None = SCAN_DAYS,
           upto: datetime | None = None) -> dict:
    """아직 처리하지 않은 수집 기사에서 분쟁 보도를 찾아 사건에 묶는다. 멱등.

    upto 를 주면 그 시각까지의 기사만 본다(검증에서 날짜순으로 되짚을 때만 쓴다).

    scan_days=None 이면 수집 기사 전체를 본다(검증·처음 켠 날). 처음 켠 날(처리 기록이 하나도
    없을 때)은 자동으로 전체를 보고, 첫 기사가 24시간보다 오래된 사건을 '소급'으로 둔다.
    """
    now = now or _now()
    out = {"scanned": 0, "matched": 0, "new_events": 0, "first": 0, "after_filing": 0,
           "joined": 0, "rejected": {}}
    cal = E.trading_calendar(store)
    with closing(_conn(store)) as con:
        first_run = con.execute("SELECT COUNT(*) FROM sig_dispute_news_items").fetchone()[0] == 0
        like = " OR ".join("n.title LIKE ?" for _ in _ANCHORS)
        args: list = [f"%{a}%" for a in _ANCHORS]
        where = f"({like}) AND n.origin<>'DART' AND (n.region='KR' OR n.region IS NULL)"
        if scan_days is not None and not first_run:
            where += " AND n.d>=?"
            args.append((now - timedelta(days=int(scan_days))).strftime("%Y-%m-%d"))
        rows = con.execute(
            "SELECT n.id, n.d, n.published, n.collected, n.title, n.url, n.source, n.origin, "
            "n.region, n.category FROM news n LEFT JOIN sig_dispute_news_items i ON i.news_id=n.id "
            f"WHERE i.news_id IS NULL AND {where}", args).fetchall()
        out["scanned"] = len(rows)
        if not rows:
            return out
        ids = [r["id"] for r in rows]
        links: dict[str, list[tuple[str, str]]] = {}
        for k in range(0, len(ids), 400):
            part = ids[k:k + 400]
            for nid, t, nm in con.execute(
                    f"SELECT news_id, ticker, name FROM news_tickers WHERE news_id IN "
                    f"({','.join('?' * len(part))})", part).fetchall():
                links.setdefault(nid, []).append((str(t), nm))
        hits = []
        for r in rows:
            lk = links.get(r["id"], [])
            label, why = classify(r["title"], r["origin"], r["region"] or "KR", r["category"], len(lk))
            if label is None:
                if why:
                    out["rejected"][why] = out["rejected"].get(why, 0) + 1
                continue
            at = _ts(r["published"], r["collected"], r["d"])
            if at is None or (upto is not None and at > upto):
                continue
            hits.append((at, r["id"], r, lk[0], label))
        hits.sort(key=lambda x: (x[0], x[1]))
        out["matched"] = len(hits)
        stamp = now.isoformat(timespec="seconds")
        for at, nid, r, (ticker, name), label in hits:
            iso = at.isoformat(timespec="seconds")
            prev = con.execute(
                "SELECT event_id, published FROM sig_dispute_news_items WHERE ticker=? "
                "ORDER BY published DESC LIMIT 1", (ticker,)).fetchone()
            ev_id = None
            if prev is not None:
                gap = abs((at - _ts(prev["published"])).days)
                if gap <= SAME_EVENT_DAYS:
                    ev_id = prev["event_id"]
            if ev_id is not None:
                con.execute("UPDATE sig_dispute_news SET n_articles=n_articles+1, "
                            "last_at=MAX(last_at, ?) WHERE id=?", (iso, ev_id))
                out["joined"] += 1
            else:
                pf = _prior_filing(con, ticker, at, cal)
                kind = KIND_AFTER if pf else KIND_FIRST
                skip = None
                if kind == KIND_AFTER:
                    skip = "공시 뒤 보도"
                elif first_run and at < now - timedelta(hours=BACKFILL_HOURS):
                    skip = "소급"
                else:
                    last_shown = con.execute(
                        "SELECT MAX(shown_at) FROM sig_dispute_news WHERE ticker=? AND shown=1",
                        (ticker,)).fetchone()[0]
                    if last_shown and (at - _ts(last_shown)).days < SAME_EVENT_DAYS:
                        skip = "60일 안 표시"
                cur = con.execute(
                    "INSERT INTO sig_dispute_news(ticker,name,kind,first_news_id,first_at,title,"
                    "source,url,expr,n_articles,last_at,detected,prior_filing_dt,show_skip) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (ticker, name, kind, nid, iso, " ".join(str(r["title"]).split()), r["source"],
                     r["url"], label, 1, iso, stamp, pf, skip))
                ev_id = cur.lastrowid
                out["new_events"] += 1
                out[kind] += 1
            con.execute("INSERT OR IGNORE INTO sig_dispute_news_items(news_id,ticker,expr,"
                        "published,event_id,created) VALUES(?,?,?,?,?,?)",
                        (nid, ticker, label, iso, ev_id, stamp))
        con.commit()
    return out


def maybe_detect(store) -> dict | None:
    """플래그가 켜져 있을 때만. **예외를 올리지 않는다** — 뉴스 수집을 죽이지 않는다."""
    if not enabled():
        return None
    try:
        return detect(store)
    except Exception as exc:  # noqa: BLE001 - 기록 전용
        log.exception("경영권 분쟁 첫 보도 감지 실패 — 건너뜁니다: %s", exc)
        return {"error": f"{type(exc).__name__}: {exc}"[:200]}


def link_filings(store) -> int:
    """사건 뒤에 실제로 분쟁 공시가 나왔는지 채운다(첫 기사일 이후 첫 공시)."""
    n = 0
    with closing(_conn(store)) as con:
        for r in con.execute("SELECT id, ticker, first_at FROM sig_dispute_news "
                             "WHERE filing_after_dt IS NULL").fetchall():
            f = con.execute("SELECT rcept_dt, rcept_no FROM sig_dispute WHERE ticker=? AND rcept_dt>=? "
                            "ORDER BY rcept_dt, rcept_no LIMIT 1",
                            (r["ticker"], str(r["first_at"])[:10])).fetchone()
            if f:
                con.execute("UPDATE sig_dispute_news SET filing_after_dt=?, filing_after_no=? WHERE id=?",
                            (f["rcept_dt"], f["rcept_no"], r["id"]))
                n += 1
        con.commit()
    return n


# ══════════════════════════ 표시 ══════════════════════════
def _universe(store, d: str) -> dict[str, str]:
    from .signals import _universe as u
    return u(store, d)


def to_show(store, d: str) -> list[dict]:
    """아직 표시하지 않은 첫 보도 중 운영 유니버스 안 종목. 첫 기사 시각 순."""
    uni = _universe(store, d)
    with closing(_conn(store)) as con:
        rows = [dict(r) for r in con.execute(
            "SELECT * FROM sig_dispute_news WHERE kind=? AND shown=0 AND show_skip IS NULL "
            "ORDER BY first_at, id", (KIND_FIRST,)).fetchall()]
    out, seen = [], set()
    for r in rows:
        if r["ticker"] not in uni or r["ticker"] in seen:
            continue
        seen.add(r["ticker"])
        r["name"] = uni.get(r["ticker"]) or r.get("name")
        out.append(r)
    return out


def recent_first_tickers(store, d: str, cal: list[str] | None = None, days: int = 5) -> set[str]:
    """최근 days 거래일 안에 첫 기사가 난 첫 보도 종목(표시했든 아니든 · 소급 제외)."""
    cal = cal if cal is not None else E.trading_calendar(store)
    cal = [x for x in cal if x <= str(d)[:10]]
    if not cal:
        return set()
    lo = cal[max(0, len(cal) - days)]
    with closing(_conn(store)) as con:
        return {str(r[0]) for r in con.execute(
            "SELECT ticker FROM sig_dispute_news WHERE kind=? AND first_at>=? "
            "AND (show_skip IS NULL OR shown=1)", (KIND_FIRST, lo)).fetchall()}


def mark_shown(store, ids: list[int], when: datetime | None = None) -> int:
    if not ids:
        return 0
    stamp = (when or _now()).isoformat(timespec="seconds")
    with closing(_conn(store)) as con:
        con.executemany("UPDATE sig_dispute_news SET shown=1, shown_at=? WHERE id=? AND shown=0",
                        [(stamp, int(i)) for i in ids])
        con.commit()
        return con.total_changes


def _short(title: str, n: int = TITLE_MAX) -> str:
    t = " ".join(str(title or "").split())
    t = re.sub(r"\s+-\s+https?://\S*$", "", t)            # Google 이 붙인 매체 주소 꼬리
    return t if len(t) <= n else t[:n - 1].rstrip() + "…"


def returns_for(store, d: str, tickers: list[str]) -> dict[str, dict]:
    """종목별 (오늘 등락률 %, 20봉 수익률 %). 못 구하면 그 키가 없다."""
    from .signals import load_bars
    out: dict[str, dict] = {}
    if not tickers:
        return out
    bars, _ = load_bars(store, d, tickers=set(map(str, tickers)), bars=30)
    for t, b in bars.items():
        c = b["c"]
        if b["d"][-1] != str(d)[:10]:
            continue
        rec = {}
        if len(c) >= 2 and c[-2] > 0:
            rec["day"] = float((c[-1] / c[-2] - 1.0) * 100.0)
        if len(c) >= 21 and c[-21] > 0:
            rec["d20"] = float((c[-1] / c[-21] - 1.0) * 100.0)
        out[t] = rec
    return out


def _pct(v: float) -> str:
    """+1.2% / −3.4% (빼기 기호는 메시지의 다른 숫자와 같은 U+2212)."""
    return f"{v:+.1f}%".replace("-", "\u2212")


def show_lines(store, d: str, rows: list[dict], with_returns: bool = True,
               ctx: dict | None = None) -> list[str]:
    """종목마다 두 줄: 종목명(종목코드) "기사 제목(링크)" MM-DD HH:MM · 오늘 ±x%
                       1일 ±a · 3일 ±b · 5일 ±c · 10일 ±d
    ctx = pricectx.context 결과(없으면 여기서 읽는다). with_returns=False 면 첫 줄만."""
    from html import escape
    from . import pricectx as PX
    from .renderer import stock_label
    if with_returns and ctx is None:
        ctx = PX.context(store, d, [r["ticker"] for r in rows])
    lines = []
    for r in rows:
        title = escape(_short(r["title"]), quote=False)
        url = str(r.get("url") or "").strip()
        if url.startswith(("http://", "https://")):
            title = f'<a href="{escape(url, quote=True)}">{title}</a>'
        at = _ts(r["first_at"])
        s = f'{stock_label(r["name"], r["ticker"])} "{title}"'
        if at is not None:
            s += f" {at:%m-%d %H:%M}"
        if with_returns:
            rec = (ctx or {}).get(str(r["ticker"]))
            s += f" · 오늘 {PX.pct((rec or {}).get('r1'))}"
            lines.append(s)
            lines.append(PX.ret_line(rec))
        else:
            lines.append(s)
    return lines


# ══════════════════════════ 즉시 알림 (DISPUTE_NEWS_INSTANT) ══════════════════════════
# 거래일 09:00~15:30 은 30분 잡(dispute-watch), 그 밖에는 뉴스 수집 회차에서 부른다.
# 사건(첫 보도)당 한 번 · 같은 종목은 60일 안에 한 번 · 하루 DISPUTE_INSTANT_MAX_PER_DAY 건.
# 켜기 전에 감지된 것은 보내지 않는다(sig_meta.instant_since 이후에 감지된 것만).
# 보냈다고 '표시함'(shown)으로 적지 않는다 — 그날 밤 추천 메시지에는 그대로 실린다.
INSTANT_SINCE_KEY = "instant_since"
INSTANT_FRESH_HOURS = 24          # 감지된 지 이 시간이 지난 것은 즉시 알림으로 보내지 않는다
QUOTE_URL = "https://polling.finance.naver.com/api/realtime/domestic/stock/{code}"


def _meta(store, k: str, v: str | None = None) -> str | None:
    with closing(_conn(store)) as con:
        if v is not None:
            con.execute("INSERT INTO sig_meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE "
                        "SET v=excluded.v", (k, v))
            con.commit()
            return v
        r = con.execute("SELECT v FROM sig_meta WHERE k=?", (k,)).fetchone()
        return r[0] if r else None


def instant_candidates(store, d: str | None, now: datetime | None = None) -> tuple[list[dict], dict]:
    """지금 즉시 알림을 보낼 첫 보도(첫 기사 시각 순)와 집계. 플래그가 꺼져 있으면 ([], {})."""
    if not instant_on():
        return [], {}
    now = now or _now()
    info: dict = {"candidates": 0}
    since = _meta(store, INSTANT_SINCE_KEY)
    if not since:
        _meta(store, INSTANT_SINCE_KEY, now.isoformat(timespec="seconds"))
        info["since_set"] = True          # 켠 뒤 첫 회차 — 과거분은 보내지 않는다
        return [], info
    if not d:
        return [], info
    # 운영 유니버스 = d 이하의 가장 최근 스캔일(밤 news 단계는 그날 스캔보다 먼저 돈다).
    with closing(_conn(store)) as con:
        sd = con.execute("SELECT MAX(d) FROM scans WHERE d<=?", (str(d)[:10],)).fetchone()[0]
    uni = _universe(store, sd) if sd else {}
    lo = max(since, (now - timedelta(hours=INSTANT_FRESH_HOURS)).isoformat(timespec="seconds"))
    day = now.strftime("%Y-%m-%d")
    cut = (now - timedelta(days=SAME_EVENT_DAYS)).isoformat(timespec="seconds")
    with closing(_conn(store)) as con:
        rows = [dict(r) for r in con.execute(
            "SELECT * FROM sig_dispute_news WHERE kind=? AND show_skip IS NULL AND detected>=? "
            "AND id NOT IN (SELECT event_id FROM sig_dispute_instant) ORDER BY first_at, id",
            (KIND_FIRST, lo)).fetchall()]
        recent = {str(r[0]) for r in con.execute(
            "SELECT ticker FROM sig_dispute_instant WHERE sent_at>=?", (cut,)).fetchall()}
        today = con.execute("SELECT COUNT(*) FROM sig_dispute_instant WHERE day=?",
                            (day,)).fetchone()[0]
    out, seen = [], set(recent)
    for r in rows:
        t = str(r["ticker"])
        if t not in uni or t in seen:
            continue
        seen.add(t)
        r["name"] = uni.get(t) or r.get("name")
        out.append(r)
    info["candidates"] = len(out)
    room = max(0, int(getattr(C, "DISPUTE_INSTANT_MAX_PER_DAY", 5)) - int(today))
    if len(out) > room:
        info["capped"] = len(out) - room
    info["sent_today"] = int(today)
    return out[:room], info


def fetch_now(ticker: str, get=None, timeout: float = 5.0) -> dict | None:
    """그 종목 하나의 현재가(네이버 금융 실시간 조회 1건). 실패하면 None — 알림은 그대로 보낸다."""
    try:
        if get is None:
            import requests
            r = requests.get(QUOTE_URL.format(code=str(ticker)),
                             headers={"User-Agent": "Mozilla/5.0"}, timeout=timeout)
            r.raise_for_status()
            j = r.json()
        else:
            j = get(str(ticker))
        x = (j.get("datas") or [None])[0] or {}
        pct = float(str(x.get("fluctuationsRatioRaw") or x.get("fluctuationsRatio")).replace(",", ""))
        price = float(str(x.get("closePriceRaw") or x.get("closePrice")).replace(",", ""))
        if not np.isfinite(pct) or not np.isfinite(price) or price <= 0:
            return None
        return {"price": price, "pct": pct}
    except Exception as exc:  # noqa: BLE001 - 부가 정보
        log.warning("현재가 조회 실패(%s) — '지금' 항목 없이 보냅니다: %s", ticker, type(exc).__name__)
        return None


def _filing_text(store, ticker: str, now: datetime) -> str:
    from .signals import DISPUTE_LABEL
    lo = (now - timedelta(days=190)).strftime("%Y-%m-%d")       # 약 120거래일
    with closing(_conn(store)) as con:
        r = con.execute("SELECT rcept_dt, kind FROM sig_dispute WHERE ticker=? AND rcept_dt>=? "
                        "ORDER BY rcept_dt DESC LIMIT 1", (str(ticker), lo)).fetchone()
    if not r:
        return "분쟁 공시 없음"
    return f"분쟁 공시 있음({str(r[0])[5:]} {DISPUTE_LABEL.get(r[1], r[1])})"


def instant_text(store, d: str | None, r: dict, quote: dict | None, now: datetime | None = None,
                 ctx: dict | None = None) -> str:
    """즉시 알림 본문. quote 가 없으면 "지금" 줄만 뺀다."""
    from html import escape
    from . import pricectx as PX
    from .renderer import stock_label
    now = now or _now()
    title = escape(_short(r["title"], 80), quote=False)
    url = str(r.get("url") or "").strip()
    if url.startswith(("http://", "https://")):
        title = f'<a href="{escape(url, quote=True)}">{title}</a>'
    at = _ts(r["first_at"])
    src = escape(str(r.get("source") or "").strip(), quote=False)
    when = " · ".join(x for x in (src, f"{at:%m-%d %H:%M}" if at is not None else "") if x)
    lines = ["🔔 <b>경영권 분쟁 첫 보도</b>", stock_label(r["name"], r["ticker"]), f'"{title}"']
    if when:
        lines.append(when)
    lines.append(f"관련 기사 {int(r.get('n_articles') or 1)}건 · {_filing_text(store, r['ticker'], now)}")
    if quote:
        lines.append(f"지금 {PX.pct(quote['pct'])} ({now:%H:%M} 조회)")
    if d:
        if ctx is None:
            ctx = PX.context(store, d, [r["ticker"]])
        lines.append(PX.ret_line(ctx.get(str(r["ticker"]))).strip() + f" ({str(d)[5:]} 종가까지)")
    mean = int(C.DISPUTE_NOTE_MEAN_PCT)
    lines.append(f"과거 {int(C.DISPUTE_NOTE_YEARS)}년: 분쟁 공시 뒤에 샀을 때 20일 평균 "
                 f"{'−' if mean < 0 else '+'}{abs(mean)}%")
    lines.append("첫 보도 기준 성적은 기록 중 · 검증 중")
    return "\n".join(lines)


def record_instant(store, r: dict, when: datetime | None = None, via: str = "",
                   price_ok: bool = False) -> None:
    when = when or _now()
    with closing(_conn(store)) as con:
        con.execute("INSERT OR IGNORE INTO sig_dispute_instant(event_id,ticker,name,sent_at,day,"
                    "via,price_ok) VALUES(?,?,?,?,?,?,?)",
                    (int(r["id"]), str(r["ticker"]), r.get("name"),
                     when.isoformat(timespec="seconds"), when.strftime("%Y-%m-%d"), via,
                     1 if price_ok else 0))
        con.commit()
