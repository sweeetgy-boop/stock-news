# -*- coding: utf-8 -*-
"""[S] 신호 후보 — 눌림목 · 경영권 분쟁 · 반토막 경고 (2026-11-20 지시). 기록·표시 전용.

**점수·추천 선정·recos 기록에 닿지 않는다.** 플래그 3개가 전부 OFF(기본)면 이 모듈의
어떤 함수도 계산·기록·표시를 하지 않는다 — 스캔·recos·발송 텍스트가 그대로다.

  PULLBACK_LIST   눌림목 후보: 매일 스캔 뒤 판정 → sig_pullback, 종이 추적, 표시
  DISPUTE_LIST    경영권 분쟁 공시: 뉴스 수집 때 감지 → sig_dispute, 종이 추적, 표시
  HALF_DROP_WARN  반토막 경고: 점수 방식(recos) 종목 중 고점 대비 50% 넘게 낮은 종목에 표시

눌림목 판정 (research/pullback 조사와 같은 정의 · 봉은 그 종목의 자기 봉)
  평균선 n = 20 · 40 · 60       최근 n봉(당일 포함) 종가 평균
  닿음   저가 ≤ 평균선 × 1.01     지킴   닿은 날 종가 ≥ 평균선
  닿음 묶음  연속으로 닿은 날들 = 1회. 묶음의 지킴 = 그 묶음의 모든 날 종가 ≥ 평균선
  잘 지키는 종목  직전 120봉 안에서 시작한 묶음 3회 이상 · 지킨 묶음 ÷ 묶음 ≥ 75%
                 (전날까지의 날만 보고 판정) · 오늘 평균선 > 20봉 전 평균선
  후보   잘 지키는 종목이 오늘 닿고 지킴. 운영 유니버스(그날 scans 에 기록된 종목) 안에서.
  제외   종가가 직전 250봉 고가 최고값보다 PULLBACK_MAX_DROP(15%) 넘게 낮은 종목
  여러 평균선에 해당하면 가장 짧은 평균선 하나. 순서 = 지킨 비율 → 닿은 횟수 →
  20봉 평균 거래대금. 하루 최대 PULLBACK_MAX_PER_DAY(5). 목록에 오른 종목은
  PULLBACK_COOLDOWN_DAYS(20) 거래일 동안 다시 올리지 않는다.
  ※ 2026-11-20 조사에서 이 정의의 사건은 전 종목·대조군 대비 차이가 확인되지
    않았다(12칸 중 통과 0). 표시는 '기록용 목록 · 검증 중'이다.

종이 추적 (sig_paper · sig_results — 기존 paper_* 표와 섞지 않는다)
  편입 = 신호일 다음 거래일 시가(events.score_events 와 같은 규약 · 상한가 시가는 편입 불가)
  눌림목     ① 20거래일 보유  ② 종가가 그 평균선 아래로 내려간 날의 다음 거래일 시가 종료
  경영권 분쟁 ① 20거래일 보유. 같은 종목은 첫 공시만(120거래일 안의 공시는 같은 분쟁).
  비용은 기존 추적과 같은 왕복 ROUND_TRIP_COST_PCT.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import time
from contextlib import closing

import numpy as np
import pandas as pd

from . import config as C
from . import events as E

log = logging.getLogger(__name__)

__all__ = ["pullback_on", "dispute_on", "halfdrop_on", "any_on", "pack_messages", "pullback_arrays",
           "high_drop", "load_bars", "scan_dates", "record_pullback", "maybe_daily",
           "update_tracking", "collect_disputes", "record_disputes",
           "maybe_collect_disputes", "dispute_event_day", "classify_dispute",
           "reco_block", "type_lines", "MAS", "KIND_PULLBACK", "KIND_DISPUTE"]

MAS = (20, 40, 60)
NEAR = 1.01           # 닿음: 저가 ≤ 평균선 × 1.01
WIN = 120             # 이력 창(자기 봉)
RISE = 20             # 평균선 상승 비교 간격
MIN_RUNS = 3
HIGH_WIN = 250        # 고점 창(당일 제외)
BARS = 440            # 종목당 읽는 봉 수 (60 + 120 + 250 여유)
KIND_PULLBACK = "pullback"
KIND_DISPUTE = "dispute"
KIND_NEWS = "dispute_news"
KIND_LABEL = {KIND_PULLBACK: "눌림목", KIND_DISPUTE: "경영권 분쟁 공시", KIND_NEWS: "분쟁 첫 보도"}
RULE_H20 = "h20"
RULE_MA = "ma_exit"
HOLD_DAYS = 20
# 유형별 보유 거래일(확정 성적 rule = h<n>)
KIND_HORIZONS = {KIND_PULLBACK: (20,), KIND_DISPUTE: (20,), KIND_NEWS: (5, 20, 60)}
DISPUTE_SAME_DAYS = 120       # 같은 종목의 이 거래일 안 공시는 같은 분쟁
DISPUTE_SHOW_DAYS = 5         # 표시: 최근 5거래일 안의 공시만
MINUS = "−"


def pullback_on() -> bool:
    return bool(getattr(C, "PULLBACK_LIST", False))


def dispute_on() -> bool:
    return bool(getattr(C, "DISPUTE_LIST", False))


def halfdrop_on() -> bool:
    return bool(getattr(C, "HALF_DROP_WARN", False))


def news_on() -> bool:
    return bool(getattr(C, "DISPUTE_NEWS_LINE", False))


def any_on() -> bool:
    return pullback_on() or dispute_on() or halfdrop_on() or news_on()


def _now() -> str:
    from .trading_day import now_kst
    return now_kst().replace(tzinfo=None).isoformat(timespec="seconds")


def _conn(store) -> sqlite3.Connection:
    con = sqlite3.connect(str(store.path), timeout=30.0)
    con.row_factory = sqlite3.Row
    return con


# ══════════════════════════ 눌림목 판정 ══════════════════════════
def pullback_arrays(l: np.ndarray, c: np.ndarray, n: int) -> dict | None:
    """한 종목의 자기 봉 → 날짜별 후보 여부와 근거. 봉이 n+121 개 미만이면 None.

    cand[i] 는 i 까지의 봉만 쓴다(미래 봉을 보지 않는다).
    runs/held 는 직전 120봉 안에서 시작한 묶음 수 / 그중 지킨 묶음 수.
    """
    m = len(c)
    if m < n + WIN + 1:
        return None
    idx = np.arange(m)
    cs = np.r_[0.0, np.cumsum(c)]
    s = np.full(m, np.nan)
    s[n - 1:] = cs[n:] - cs[:-n]                              # 평균선 × n
    dfn = idx >= n - 1
    with np.errstate(invalid="ignore"):
        touch = dfn & (l * 100.0 * n <= s * (NEAR * 100.0))
        below = dfn & (c * n < s)
    fail = touch & below
    hold = touch & ~below
    start = touch & ~np.r_[False, touch[:-1]]                 # 묶음 시작
    cs_start = np.cumsum(start)
    n_runs = int(cs_start[-1])
    rid = np.where(touch, cs_start, 0)
    big = m + 10
    first_fail = np.full(n_runs + 1, big, dtype=np.int64)     # 묶음별 첫 실패 위치
    fi = np.flatnonzero(fail)
    np.minimum.at(first_fail, rid[fi], fi)
    ffc = np.zeros(m)
    np.add.at(ffc, first_fail[1:][first_fail[1:] < big], 1)
    cum_ff = np.cumsum(ffc)
    i = np.arange(n + WIN, m)
    a, b = i - WIN, i - 1
    count = cs_start[b] - cs_start[a - 1]
    fa = first_fail[rid[a]]
    failed = cum_ff[b] - cum_ff[a - 1] - (touch[a] & touch[a - 1] & (fa >= a) & (fa <= b))
    rising = s[i] > s[i - RISE]
    well = rising & (count >= MIN_RUNS) & ((count - failed) * 4 >= count * 3)
    cand = np.zeros(m, dtype=bool)
    cand[i] = well & hold[i]
    runs = np.zeros(m, dtype=np.int64)
    held = np.zeros(m, dtype=np.int64)
    runs[i] = count
    held[i] = (count - failed).astype(np.int64)
    return {"cand": cand, "runs": runs, "held": held, "ma": s / n, "touch": touch,
            "hold": hold, "below": below}


def high_drop(h: np.ndarray, c: np.ndarray, win: int = HIGH_WIN) -> tuple[np.ndarray, np.ndarray]:
    """(직전 win봉 고가 최고값, 종가가 그보다 낮은 비율). 봉이 모자라면 NaN.
    신고가(종가가 직전 고점 위)면 비율은 0."""
    m = len(c)
    mh = np.full(m, np.nan)
    if m > win:
        mh = pd.Series(h).rolling(win).max().shift(1).to_numpy()
    with np.errstate(invalid="ignore", divide="ignore"):
        drop = np.where(np.isfinite(mh) & (mh > 0), np.maximum(0.0, 1.0 - c / mh), np.nan)
    return mh, drop


def _exceeds(drop: float, limit: float) -> bool:
    """'limit 넘게 낮다' — 정확히 limit 이면 넘지 않은 것."""
    return bool(np.isfinite(drop) and drop > float(limit) + 1e-9)


def load_bars(store, d: str, tickers=None, bars: int = BARS) -> tuple[dict, list[str]]:
    """d 까지의 최근 bars 거래일 시세를 한 번에 읽는다. (종목별 배열 dict, 거래일 목록).
    load_ohlcv 와 같이 거래량 0 인 날은 뺀다. 읽기만 한다."""
    d = str(d)[:10]
    with closing(_conn(store)) as con:
        dates = [r[0] for r in con.execute(
            "SELECT DISTINCT d FROM prices WHERE d<=? ORDER BY d DESC LIMIT ?",
            (d, int(bars))).fetchall()][::-1]
        if not dates:
            return {}, []
        df = pd.read_sql_query(
            "SELECT ticker,d,o,h,l,c,v,amt FROM prices WHERE d>=? AND d<=? AND v>0 "
            "ORDER BY ticker,d", con, params=(dates[0], d))
    if tickers is not None:
        df = df[df["ticker"].isin(set(tickers))]
    df = df[(df["c"] > 0) & (df["l"] > 0) & (df["h"] > 0)]
    out: dict = {}
    if df.empty:
        return out, dates
    tk = df["ticker"].to_numpy()
    cut = np.flatnonzero(tk[1:] != tk[:-1]) + 1
    bounds = np.r_[0, cut, len(tk)]
    cols = {k: df[k].to_numpy(dtype="float64") for k in ("o", "h", "l", "c", "v", "amt")}
    ds = df["d"].to_numpy()
    for a, b in zip(bounds[:-1], bounds[1:]):
        amt = cols["amt"][a:b]
        amt = np.where(np.isfinite(amt) & (amt > 0), amt, cols["c"][a:b] * cols["v"][a:b])
        out[str(tk[a])] = {"d": ds[a:b], "o": cols["o"][a:b], "h": cols["h"][a:b],
                           "l": cols["l"][a:b], "c": cols["c"][a:b], "amt": amt}
    return out, dates


def _state_of(b: dict) -> dict:
    """종목 묶음에 평균선별 배열과 고점 배열을 붙인다(한 번만 계산)."""
    if "_st" not in b:
        mh, drop = high_drop(b["h"], b["c"])
        b["_st"] = {"ma": {n: pullback_arrays(b["l"], b["c"], n) for n in MAS},
                    "mh": mh, "drop": drop}
    return b["_st"]


def _match_on(b: dict, d: str) -> dict | None:
    """그 종목이 d 에 후보인가. 가장 짧은 평균선 하나. 아니면 None."""
    k = int(np.searchsorted(b["d"], d))
    if k >= len(b["d"]) or b["d"][k] != d:
        return None
    st = _state_of(b)
    for n in MAS:
        arr = st["ma"][n]
        if arr is None or not arr["cand"][k]:
            continue
        runs, held = int(arr["runs"][k]), int(arr["held"][k])
        amt = b["amt"][max(0, k - 19):k + 1]
        return {"ma": n, "ma_value": float(arr["ma"][k]), "runs": runs, "held": held,
                "ratio": held / runs if runs else 0.0, "close": float(b["c"][k]),
                "low": float(b["l"][k]),
                "high250": float(st["mh"][k]) if np.isfinite(st["mh"][k]) else None,
                "drop": float(st["drop"][k]) if np.isfinite(st["drop"][k]) else None,
                "amt20": float(np.mean(amt)) if len(amt) else 0.0}
    return None


def _universe(store, d: str) -> dict[str, str]:
    """운영 유니버스 = 그날 scans 에 기록된 종목(유동성 필터·하드 배제 통과)."""
    with closing(_conn(store)) as con:
        return {str(r[0]): r[1] for r in con.execute(
            "SELECT ticker, name FROM scans WHERE d=?", (str(d)[:10],)).fetchall()}


def _rank_rows(rows: list[dict], cooling: set[str], max_drop: float, top: int) -> list[dict]:
    """순서를 매기고 제외 사유·목록 여부를 붙인다."""
    rows = sorted(rows, key=lambda r: (-r["ratio"], -r["runs"], -r["amt20"], r["ticker"]))
    k = 0
    for r in rows:
        r["listed"], r["rank"], r["skip"] = 0, None, None
        if r["drop"] is None:
            r["skip"] = "bars"
        elif _exceeds(r["drop"], max_drop):
            r["skip"] = "drop"
        elif r["ticker"] in cooling:
            r["skip"] = "cooldown"
        elif k >= top:
            r["skip"] = "max"
        else:
            k += 1
            r["listed"], r["rank"] = 1, k
    return rows


def scan_dates(store, dates: list[str], max_drop: float | None = None,
               top: int | None = None, cooldown: int | None = None,
               seed_cooling: dict | None = None) -> dict[str, list[dict]]:
    """여러 거래일을 순서대로 판정한다(검증·소급 확인용 · **쓰지 않는다**).

    재등재 제한은 이 호출 안에서 이어서 센다(seed_cooling = {종목: 마지막 등재일}).
    """
    dates = sorted(str(x)[:10] for x in dates)
    if not dates:
        return {}
    max_drop = float(C.PULLBACK_MAX_DROP if max_drop is None else max_drop)
    top = int(C.PULLBACK_MAX_PER_DAY if top is None else top)
    cooldown = int(C.PULLBACK_COOLDOWN_DAYS if cooldown is None else cooldown)
    with closing(_conn(store)) as con:
        n_extra = con.execute("SELECT COUNT(DISTINCT d) FROM prices WHERE d>=? AND d<=?",
                              (dates[0], dates[-1])).fetchone()[0]
    bars, cal = load_bars(store, dates[-1], bars=BARS + int(n_extra))
    pos = {x: i for i, x in enumerate(cal)}
    last = dict(seed_cooling or {})
    out: dict[str, list[dict]] = {}
    for d in dates:
        uni = _universe(store, d)
        rows = []
        for t, name in uni.items():
            b = bars.get(t)
            if b is None:
                continue
            r = _match_on(b, d)
            if r:
                rows.append({"d": d, "ticker": t, "name": name, **r})
        p = pos.get(d, -1)
        cooling = {t for t, ld in last.items()
                   if ld in pos and 0 < p - pos[ld] <= cooldown}
        rows = _rank_rows(rows, cooling, max_drop, top)
        for r in rows:
            if r["listed"]:
                last[r["ticker"]] = d
        out[d] = rows
    return out


def record_pullback(store, d: str) -> dict:
    """그 거래일의 눌림목 판정을 sig_pullback 에 기록한다(그날 행을 지우고 다시 쓴다).
    recos 는 읽지도 쓰지도 않는다."""
    t0 = time.perf_counter()
    d = str(d)[:10]
    with closing(_conn(store)) as con:
        cal = [r[0] for r in con.execute(
            "SELECT DISTINCT d FROM prices WHERE d<? ORDER BY d DESC LIMIT ?",
            (d, int(C.PULLBACK_COOLDOWN_DAYS))).fetchall()]
        seed = {}
        if cal:
            for r in con.execute(
                    "SELECT ticker, MAX(d) FROM sig_pullback WHERE listed=1 AND d<? AND d>=? "
                    "GROUP BY ticker", (d, cal[-1])).fetchall():
                seed[str(r[0])] = r[1]
    rows = scan_dates(store, [d], seed_cooling=seed).get(d, [])
    now = _now()
    with closing(_conn(store)) as con:
        con.execute("DELETE FROM sig_pullback WHERE d=?", (d,))
        con.executemany(
            "INSERT INTO sig_pullback(d,ticker,name,ma,ma_value,runs,held,ratio,close,low,"
            "high250,drop_pct,amt20,listed,rank,skip,created) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(d, r["ticker"], r["name"], r["ma"], r["ma_value"], r["runs"], r["held"],
              r["ratio"], r["close"], r["low"], r["high250"],
              None if r["drop"] is None else r["drop"] * 100.0, r["amt20"], r["listed"],
              r["rank"], r["skip"], now) for r in rows])
        con.commit()
    skips: dict = {}
    for r in rows:
        if r["skip"]:
            skips[r["skip"]] = skips.get(r["skip"], 0) + 1
    return {"d": d, "matched": len(rows), "listed": sum(r["listed"] for r in rows),
            "skipped": skips, "tickers": [r["ticker"] for r in rows if r["listed"]],
            "elapsed_sec": round(time.perf_counter() - t0, 2)}


def listed_pullbacks(store, d: str) -> list[dict]:
    with closing(_conn(store)) as con:
        return [dict(r) for r in con.execute(
            "SELECT * FROM sig_pullback WHERE d=? AND listed=1 ORDER BY rank",
            (str(d)[:10],)).fetchall()]


# ══════════════════════════ 경영권 분쟁 공시 ══════════════════════════
# 식별 규칙은 2026-11-20 조사(research/dispute)에서 목록 API 로 확인한 것만 둔다.
# 목록 API 의 상세 유형(pblntf_detail_ty)만 건다 — pblntf_ty 를 같이 주면 상세 유형이
# 무시되고 그 대분류 전체가 온다(2026-11-20 실측: D + D003 = D 전체 601건/7일).
DISPUTE_QUERIES = (
    "I001",     # 거래소 수시공시 — 소송등의제기·신청(경영권분쟁소송) (하루 약 100건)
    "D003",     # 의결권대리행사권유 (제출인 ≠ 회사 = 주주 측)
    "D004",     # 공개매수 (신고서만)
)
DISPUTE_LABEL = {"lawsuit": "경영권 분쟁 소송", "tender": "공개매수 신고",
                 "proxy": "의결권 대리행사 권유(주주 측)"}


def _norm(s) -> str:
    return "".join(str(s or "").split())


def _corp_norm(s) -> str:
    t = _norm(s).lower()
    for w in ("주식회사", "(주)", "㈜"):
        t = t.replace(w, "")
    return t


def _is_company(filer, corp) -> bool:
    """제출인이 그 회사 자신인가(이름이 같거나 한쪽이 다른 쪽을 포함)."""
    a, b = _corp_norm(filer), _corp_norm(corp)
    return bool(a and b and (a == b or a in b or b in a))


def classify_dispute(report_nm: str, flr_nm: str = "", corp_name: str = "") -> str | None:
    """공시 제목(과 제출인) → 유형. 해당 없으면 None.

    제목은 공백을 모두 뺀 뒤 본다. [기재정정]·[첨부추가] 같은 머리표가 붙은 것은 원본이
    아니라서 뺀다. 판결·결정(결과)과 공개매수 설명서·결과보고서도 뺀다.
    (research/dispute 조사의 분류와 같은 규칙)
    """
    import re
    t = _norm(report_nm)
    if not t or re.match(r"^\[[^\]]+\]", t):
        return None
    if t.startswith("소송등의제기") and "경영권분쟁" in t:
        return "lawsuit"
    if t == "공개매수신고서":
        return "tender"
    if t.startswith("의결권대리행사권유에관한의견표명서"):
        return "proxy"                    # 회사가 주주 측 권유에 낸 의견 = 주주 측 권유가 있다
    if t.startswith("의결권대리행사권유참고서류") and not _is_company(flr_nm, corp_name):
        return "proxy"
    return None


def dispute_event_day(rcept_dt: str, cal_next) -> str | None:
    """사건일. 목록 API 는 접수 시각을 주지 않는다 → 장 마감 뒤 접수로 보고
    **접수일 다음 거래일**을 사건일로 둔다(편입은 그 다음 거래일 시가).
    cal_next(d) = d 보다 뒤의 첫 거래일(없으면 None)."""
    return cal_next(str(rcept_dt)[:10])


def collect_disputes(days: int | None = None, pause: float = 0.3,
                     max_pages: int | None = None, get=None) -> tuple[list[dict], dict]:
    """Open DART 목록에서 최근 days 일의 경영권 분쟁 공시를 고른다. 키가 없으면 빈 목록.
    (항목 목록, {"requests": n, "error": ...}). 종목을 지정하지 않은 조회는 한 번에 3개월까지라
    85일씩 끊어 받는다."""
    key = os.getenv("DART_API_KEY")
    info: dict = {"requests": 0}
    if not key:
        info["skipped"] = "no_dart_key"
        return [], info
    import requests
    from datetime import timedelta
    from .trading_day import now_kst
    days = int(C.DISPUTE_LOOKBACK_DAYS if days is None else days)
    max_pages = int(C.DISPUTE_MAX_PAGES if max_pages is None else max_pages)
    end = now_kst()
    bgn = end - timedelta(days=days)
    get = get or (lambda params: requests.get(
        "https://opendart.fss.or.kr/api/list.json", params=params, timeout=(5, 20)).json())
    windows = []
    lo = bgn
    while lo <= end:
        hi = min(end, lo + timedelta(days=84))
        windows.append((lo, hi))
        lo = hi + timedelta(days=1)
    out: dict[str, dict] = {}
    for w_lo, w_hi in windows:
        for detail in DISPUTE_QUERIES:
            for page in range(1, max_pages + 1):
                try:
                    data = get({"crtfc_key": key, "bgn_de": w_lo.strftime("%Y%m%d"),
                                "end_de": w_hi.strftime("%Y%m%d"),
                                "pblntf_detail_ty": detail, "page_no": page, "page_count": 100})
                except Exception as exc:  # noqa: BLE001 - 수집은 뉴스 단계를 막지 않는다
                    info["error"] = f"{type(exc).__name__}"[:80]
                    break
                info["requests"] += 1
                status = str(data.get("status"))
                if status == "013":            # 조회된 데이터 없음
                    break
                if status != "000":
                    info["error"] = f"DART {status}"
                    break
                for it in data.get("list") or []:
                    code = str(it.get("stock_code") or "").strip()
                    kind = classify_dispute(it.get("report_nm"), it.get("flr_nm"),
                                            it.get("corp_name"))
                    if not kind or len(code) != 6:
                        continue
                    dt = str(it.get("rcept_dt") or "")
                    out[str(it.get("rcept_no"))] = {
                        "rcept_no": str(it.get("rcept_no")), "ticker": code,
                        "name": it.get("corp_name"), "kind": kind,
                        "rcept_dt": f"{dt[:4]}-{dt[4:6]}-{dt[6:8]}" if len(dt) == 8 else dt,
                        "title": " ".join(str(it.get("report_nm") or "").split()),
                        "filer": it.get("flr_nm")}
                if page >= int(data.get("total_page") or 1):
                    break
                if page >= max_pages:
                    info["truncated"] = info.get("truncated", 0) + 1
                time.sleep(pause)
    return list(out.values()), info


def record_disputes(store, items: list[dict], source: str = "dart") -> int:
    """sig_dispute 에 추가(접수번호가 이미 있으면 그대로 둔다)."""
    if not items:
        return 0
    now = _now()
    with closing(_conn(store)) as con:
        before = con.total_changes
        con.executemany(
            "INSERT OR IGNORE INTO sig_dispute(rcept_no,ticker,name,kind,rcept_dt,title,filer,"
            "source,detected) VALUES(?,?,?,?,?,?,?,?,?)",
            [(i["rcept_no"], i["ticker"], i.get("name"), i["kind"], i["rcept_dt"],
              i.get("title"), i.get("filer"), source, now) for i in items])
        con.commit()
        return con.total_changes - before


def _meta_get(store, k: str) -> str | None:
    with closing(_conn(store)) as con:
        r = con.execute("SELECT v FROM sig_meta WHERE k=?", (k,)).fetchone()
        return r[0] if r else None


def _meta_set(store, k: str, v: str) -> None:
    with closing(_conn(store)) as con:
        con.execute("INSERT INTO sig_meta(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                    (k, v))
        con.commit()


def maybe_collect_disputes(store) -> dict | None:
    """DISPUTE_LIST 가 켜져 있을 때만. **예외를 올리지 않는다** — 뉴스 수집을 죽이지 않는다.

    처음 켠 날 한 번은 DISPUTE_SEED_DAYS(190일)를 되돌아본다 — "첫 보도" 판정이 직전
    120거래일의 분쟁 공시를 보기 때문이다. 끝까지 받았을 때만 표시(sig_meta)를 남기고,
    중간에 끊기면 다음 수집 때 다시 한다.
    """
    if not dispute_on():
        return None
    try:
        seeded = _meta_get(store, "dispute_seed")
        if seeded:
            items, info = collect_disputes()
        else:
            items, info = collect_disputes(days=int(C.DISPUTE_SEED_DAYS), max_pages=120)
            info["seed_days"] = int(C.DISPUTE_SEED_DAYS)
        info["found"] = len(items)
        info["added"] = record_disputes(store, items, source="dart" if seeded else "seed")
        if not seeded and not info.get("error") and not info.get("skipped") \
                and not info.get("truncated"):
            _meta_set(store, "dispute_seed", _now())
            info["seeded"] = True
        return info
    except Exception as exc:  # noqa: BLE001 - 기록 전용
        log.exception("경영권 분쟁 공시 감지 실패 — 건너뜁니다: %s", exc)
        return {"error": f"{type(exc).__name__}: {exc}"[:200]}


def recent_disputes(store, d: str, cal: list[str] | None = None,
                    days: int = DISPUTE_SHOW_DAYS) -> list[dict]:
    """표시용: 접수일이 d 를 포함한 최근 days 거래일 안이고 d 의 운영 유니버스에 있는
    종목. 종목당 가장 최근 공시 하나."""
    d = str(d)[:10]
    cal = cal if cal is not None else E.trading_calendar(store)
    cal = [x for x in cal if x <= d]
    if not cal:
        return []
    lo = cal[max(0, len(cal) - days)]
    uni = _universe(store, d)
    with closing(_conn(store)) as con:
        rows = [dict(r) for r in con.execute(
            "SELECT * FROM sig_dispute WHERE rcept_dt>=? AND rcept_dt<=? "
            "ORDER BY rcept_dt DESC, rcept_no DESC", (lo, d)).fetchall()]
    seen, out = set(), []
    for r in rows:
        if r["ticker"] in seen or r["ticker"] not in uni:
            continue
        seen.add(r["ticker"])
        r["name"] = uni.get(r["ticker"]) or r.get("name")
        out.append(r)
    return out


# ══════════════════════════ 종이 추적 ══════════════════════════
def _paper_rows(con, where: str = "", args: tuple = ()) -> list[dict]:
    return [dict(r) for r in con.execute(
        f"SELECT * FROM sig_paper {where} ORDER BY signal_d, id", args).fetchall()]


def _enroll(store, cal: list[str], upto: str, stats: dict) -> None:
    pos = {x: i for i, x in enumerate(cal)}
    todo: list[dict] = []
    with closing(_conn(store)) as con:
        have = {(r["kind"], r["ticker"], r["signal_d"]) for r in _paper_rows(con)}
        if pullback_on():
            for r in con.execute("SELECT d,ticker,name,ma FROM sig_pullback WHERE listed=1 "
                                 "AND d<=? ORDER BY d, rank", (upto,)).fetchall():
                todo.append({"kind": KIND_PULLBACK, "ticker": str(r["ticker"]), "name": r["name"],
                             "signal_d": r["d"], "ref": str(int(r["ma"]))})
        if dispute_on():
            def nxt(x):
                i = E._pos(cal, x) + 1
                return cal[i] if 0 <= i < len(cal) else None
            # 처음 켠 날 한꺼번에 받아 둔 과거 공시(source=seed)는 편입하지 않는다 — 판정 근거로만 쓴다.
            for r in con.execute("SELECT * FROM sig_dispute WHERE COALESCE(source,'')<>'seed' "
                                 "ORDER BY rcept_dt, rcept_no").fetchall():
                sd = dispute_event_day(r["rcept_dt"], nxt)
                if sd is None or sd > upto or sd not in pos:
                    continue
                # 같은 종목의 앞선 공시가 120거래일 안에 있으면 같은 분쟁 — 추적 행을 만들지 않는다.
                lo = cal[max(0, pos[sd] - DISPUTE_SAME_DAYS)]
                if con.execute(
                        "SELECT 1 FROM sig_dispute WHERE ticker=? AND rcept_dt>=? AND "
                        "(rcept_dt<? OR (rcept_dt=? AND rcept_no<?)) LIMIT 1",
                        (r["ticker"], lo, r["rcept_dt"], r["rcept_dt"], r["rcept_no"])).fetchone():
                    continue
                todo.append({"kind": KIND_DISPUTE, "ticker": str(r["ticker"]), "name": r["name"],
                             "signal_d": sd, "ref": str(r["rcept_no"]), "sub": r["kind"]})
        from . import dispute_news as DN
        if DN.enabled():
            for r in con.execute("SELECT * FROM sig_dispute_news WHERE kind=? AND "
                                 "(show_skip IS NULL OR shown=1) ORDER BY detected, id",
                                 (DN.KIND_FIRST,)).fetchall():
                i = E._pos(cal, str(r["detected"])[:10])     # 감지한 날(휴장이면 그 전 거래일)
                if i < 0 or cal[i] > upto:
                    continue
                todo.append({"kind": KIND_NEWS, "ticker": str(r["ticker"]), "name": r["name"],
                             "signal_d": cal[i], "ref": str(r["id"]), "sub": r["expr"]})
    todo = [t for t in todo if (t["kind"], t["ticker"], t["signal_d"]) not in have
            and t["signal_d"] in pos and pos[t["signal_d"]] + 1 < len(cal)]
    if not todo:
        return
    first = min(t["signal_d"] for t in todo)
    panel = E.PricePanel.from_store(store, calendar=[x for x in cal if x >= cal[max(0, pos[first] - 5)]])
    rows = []
    for t in sorted(todo, key=lambda x: (x["signal_d"], x["kind"], x["ticker"])):
        key = (t["kind"], t["ticker"], t["signal_d"])
        if key in have:
            continue
        skip = None
        if t["kind"] == KIND_DISPUTE:
            if t["ticker"] not in _universe(store, t["signal_d"]):
                skip = "운영 유니버스 밖"
        elif t["kind"] == KIND_NEWS and t["ticker"] not in _universe(store, t["signal_d"]):
            skip = "운영 유니버스 밖"
        sc = E.score_events(pd.DataFrame([{"ticker": t["ticker"], "event_date": t["signal_d"]}]),
                            panel, horizons=(1,)).iloc[0]
        status = str(sc["entry_status"])
        ok = status == E.ENTRY_OK and skip is None
        from .paper import ENTRY_SKIP_TEXT
        rows.append((t["kind"], t["ticker"], t["name"], t["signal_d"], t["ref"], t.get("sub"),
                     sc.get("entry_date"), float(sc["entry_price"]) if ok else None, status,
                     1 if ok else 0,
                     None if ok else (skip or ENTRY_SKIP_TEXT.get(status, status)), _now()))
        have.add(key)
    with closing(_conn(store)) as con:
        con.executemany(
            "INSERT OR IGNORE INTO sig_paper(kind,ticker,name,signal_d,ref,sub,entry_date,"
            "entry_price,entry_status,enrolled,skip_reason,created) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            rows)
        con.commit()
    stats["enrolled"] = sum(1 for r in rows if r[9])
    stats["not_enrolled"] = sum(1 for r in rows if not r[9])


def _ma_exit(store, panel, r: dict, upto: str, cost: float) -> dict | None:
    """평균선 이탈 종료: 편입일부터 종가 < 그 평균선인 첫날의 다음 거래일 시가. 미정이면 None."""
    n = int(r["ref"])
    df = store.load_ohlcv(r["ticker"], days=BARS + 400)
    if df is None or df.empty:
        return None
    df = df[df.index <= pd.Timestamp(upto)]
    ds = [x.strftime("%Y-%m-%d") for x in df.index]
    if r["entry_date"] not in ds:
        return None
    k0 = ds.index(r["entry_date"])
    c = df["종가"].to_numpy(dtype="float64")
    o = df["시가"].to_numpy(dtype="float64")
    ma = pd.Series(c).rolling(n).mean().to_numpy()
    for k in range(k0, len(c)):
        if np.isfinite(ma[k]) and c[k] * n < float(np.sum(c[k - n + 1:k + 1])):
            if k + 1 >= len(c):
                return None                   # 다음 거래일 시가 미도래
            xd, px = ds[k + 1], float(o[k + 1])
            j = panel.col(r["ticker"])
            a, b = panel.pos.get(r["entry_date"]), panel.pos.get(xd)
            status = E.ST_OK
            if j is not None and a is not None and b is not None and panel.breaks_between(j, a, b) > 0:
                status = E.ST_ADJ_BREAK
            ret = (px / float(r["entry_price"]) - 1.0) * 100.0
            days = (b - a + 1) if a is not None and b is not None else None
            if status != E.ST_OK:
                return {"exit_date": xd, "exit_price": None, "status": status, "ret": None,
                        "net": None, "days": days}
            return {"exit_date": xd, "exit_price": px, "status": status, "ret": ret,
                    "net": ret - cost, "days": days}
    return None


def _score(store, cal: list[str], upto: str, stats: dict) -> None:
    with closing(_conn(store)) as con:
        pos_rows = _paper_rows(con, "WHERE enrolled=1 AND signal_d<=?", (upto,))
        done = {(r["paper_id"], r["rule"]) for r in con.execute(
            "SELECT paper_id, rule FROM sig_results").fetchall()}

    def _hs(r):
        return KIND_HORIZONS.get(r["kind"], (HOLD_DAYS,))
    need = [r for r in pos_rows if any((r["id"], f"h{h}") not in done for h in _hs(r))
            or (r["kind"] == KIND_PULLBACK and (r["id"], RULE_MA) not in done)]
    if not need:
        return
    pos = {x: i for i, x in enumerate(cal)}
    first = min(r["signal_d"] for r in need)
    panel = E.PricePanel.from_store(
        store, calendar=[x for x in cal if x >= cal[max(0, pos.get(first, 0) - 5)]])
    cost = float(C.ROUND_TRIP_COST_PCT)
    out = []
    all_h = sorted({h for r in need for h in _hs(r)})
    ev = pd.DataFrame([{"ticker": r["ticker"], "event_date": r["signal_d"], "_pid": r["id"],
                        "_kind": r["kind"]} for r in need])
    sc = E.score_events(ev, panel, horizons=tuple(all_h)) if len(ev) else pd.DataFrame()
    for r in sc.to_dict("records") if len(sc) else []:
        for h in KIND_HORIZONS.get(r["_kind"], (HOLD_DAYS,)):
            rule = f"h{h}"
            st = r.get(f"status_{h}")
            if (int(r["_pid"]), rule) in done or st in (E.ST_PENDING, None):
                continue
            net, ret, px = r.get(f"net_{h}"), r.get(f"ret_{h}"), r.get(f"exit_price_{h}")
            out.append((int(r["_pid"]), rule, r.get(f"exit_date_{h}"),
                        float(px) if px is not None and np.isfinite(px) else None, st,
                        float(ret) if ret is not None and np.isfinite(ret) else None,
                        float(net) if net is not None and np.isfinite(net) else None,
                        h, _now()))
    for r in need:
        if r["kind"] != KIND_PULLBACK or (r["id"], RULE_MA) in done:
            continue
        x = _ma_exit(store, panel, r, upto, cost)
        if x:
            out.append((r["id"], RULE_MA, x["exit_date"], x["exit_price"], x["status"],
                        x["ret"], x["net"], x["days"], _now()))
    if out:
        with closing(_conn(store)) as con:
            con.executemany(
                "INSERT OR IGNORE INTO sig_results(paper_id,rule,exit_date,exit_price,status,"
                "ret,net,days,created) VALUES(?,?,?,?,?,?,?,?,?)", out)
            con.commit()
    stats["results"] = len(out)


def update_tracking(store, upto: str) -> dict:
    """목록에 오른 눌림목·감지된 분쟁 공시·첫 보도 → 편입 → 확정 성적. 멱등(이미 있는 행은 그대로)."""
    upto = str(upto)[:10]
    cal = [x for x in E.trading_calendar(store) if x <= upto]
    stats: dict = {"upto": upto}
    if not cal:
        return stats
    _enroll(store, cal, upto, stats)
    _score(store, cal, upto, stats)
    from . import dispute_news as DN
    if DN.enabled():
        stats["news_filing_links"] = DN.link_filings(store)
    return stats


def maybe_daily(store, trade_date: str) -> dict | None:
    """daily 스캔 뒤에 부른다. 플래그가 전부 OFF 면 None(아무것도 하지 않는다).
    **예외를 올리지 않는다** — 스캔·추천 기록은 이미 커밋돼 있다. 한 부분이 실패해도
    나머지는 계속하고, 실패는 out["errors"] 에 남긴다(호출부가 관리자에게 한 줄 알린다)."""
    from . import dispute_news as DN
    if not (pullback_on() or dispute_on() or DN.enabled()) or not trade_date:
        return None
    out: dict = {}
    errors: list[str] = []
    t0 = time.perf_counter()
    for name, on, fn in (("pullback", pullback_on(), lambda: record_pullback(store, trade_date)),
                         ("tracking", True, lambda: update_tracking(store, trade_date))):
        if not on:
            continue
        try:
            out[name] = fn()
        except Exception as exc:  # noqa: BLE001 - 기록 전용
            log.exception("신호 후보 기록 실패(%s) — 건너뜁니다: %s", name, exc)
            errors.append(f"{name}: {type(exc).__name__}: {exc}"[:160])
    if errors:
        out["errors"] = errors
    out["elapsed_sec"] = round(time.perf_counter() - t0, 2)
    return out


# ══════════════════════════ 표시 ══════════════════════════
def _pct_drop(drop_pct) -> str:
    v = 0 if drop_pct is None or not np.isfinite(drop_pct) else int(round(float(drop_pct)))
    return f"고점 {MINUS}{max(0, v)}%"


def half_drops(store, d: str, tickers: list[str]) -> dict[str, float]:
    """recos 종목 중 종가가 직전 250봉 고가 최고값보다 HALF_DROP_LIMIT(50%) 넘게 낮은
    종목 → 낮은 비율(%)."""
    out: dict[str, float] = {}
    if not tickers:
        return out
    bars, _ = load_bars(store, d, tickers=set(map(str, tickers)), bars=HIGH_WIN + 10)
    lim = float(C.HALF_DROP_LIMIT)
    for t, b in bars.items():
        if b["d"][-1] != str(d)[:10]:
            continue
        _, drop = high_drop(b["h"], b["c"])
        if _exceeds(drop[-1], lim):
            out[t] = float(drop[-1] * 100.0)
    return out


MSG_LIMIT = 3900          # 한 메시지 상한(텔레그램 4,096 · notify 의 분할 기준 3,968 보다 작게)


def pack_messages(chunks: list[list[str]], limit: int = MSG_LIMIT) -> list[str]:
    """줄 묶음(제목 · 유형별 구역 · 맨 아래 줄)을 메시지로 나눈다.

    한 구역(유형 제목 + 내용)은 같은 메시지에 둔다. 구역 하나가 상한을 넘으면 그 구역만
    줄 단위로 나누고 유형 제목을 "(이어서)" 로 되풀이한다. 상한을 안 넘으면 메시지는 1개다.
    """
    def size(ls):
        return sum(len(x) + 1 for x in ls)
    msgs: list[list[str]] = []
    cur: list[str] = []
    for ch in chunks:
        if not ch:
            continue
        if size(ch) > limit:
            head, body = ch[0], ch[1:]
            if cur and size(cur) + len(head) + 1 + (len(body[0]) + 1 if body else 0) > limit:
                msgs.append(cur)
                cur = []
            part = cur + [head]
            n_body = 0
            for ln in body:
                if n_body and size(part) + len(ln) + 1 > limit:
                    msgs.append(part)
                    part, n_body = [f"{head} (이어서)"], 0
                part.append(ln)
                n_body += 1
            cur = part
            continue
        if cur and size(cur) + size(ch) > limit:
            msgs.append(cur)
            cur = []
        cur = cur + ch
    if cur:
        msgs.append(cur)
    return ["\n".join(m) for m in msgs]


def _dispute_note() -> str:
    mean = int(C.DISPUTE_NOTE_MEAN_PCT)
    sign = MINUS if mean < 0 else "+"
    return (f"(과거 {int(C.DISPUTE_NOTE_YEARS)}년: 공시 뒤 20일 평균 {sign}{abs(mean)}%, "
            f"10번 중 {int(C.DISPUTE_NOTE_UP_OF_10)}번 오름)")


def reco_block(store, d: str, rows: list[dict], note: str | None = None) -> tuple[list[str], dict]:
    """"오늘 기록된 추천" 합친 메시지(플래그가 하나라도 켜졌을 때).

    rows = 그날 recos (rank 순, paper_render.recorded_recos). recos 는 읽기만 한다.
    순서: 【점수】 → 【눌림목】 → 【경영권 분쟁 첫 보도】 → 【경영권 분쟁 공시 · 주의】 → 맨 아래 줄.
    **한 부분이 실패해도 나머지는 나간다.** 실패한 부분은 빼고 info["errors"] 에 남긴다 —
    【점수】 줄은 recos 만으로 만들어 항상 나간다.
    """
    from html import escape  # noqa: F401 - stock_label 이 이스케이프한다
    from . import dispute_news as DN
    from .renderer import stock_label
    d = str(d)[:10]
    info: dict = {}
    errors: list[str] = []

    def safe(name, fn, default):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - 표시는 발송을 막지 않는다
            log.exception("신호 표시 실패(%s) — 그 부분만 뺍니다: %s", name, exc)
            errors.append(f"{name}: {type(exc).__name__}: {exc}"[:160])
            return default

    pulls = safe("눌림목", lambda: listed_pullbacks(store, d), []) if pullback_on() else []
    pmap = {str(p["ticker"]): p for p in pulls}
    halves = (safe("반토막 경고", lambda: half_drops(store, d, [r["ticker"] for r in rows]), {})
              if halfdrop_on() else {})
    dis = safe("경영권 분쟁 공시", lambda: recent_disputes(store, d), []) if dispute_on() else []
    news_rows, news_lines, news_tag = [], [], set()
    if news_on():
        news_rows = safe("경영권 분쟁 첫 보도", lambda: DN.to_show(store, d), [])
        news_lines = safe("경영권 분쟁 첫 보도", lambda: DN.show_lines(store, d, news_rows), None)
        if news_lines is None:
            news_rows, news_lines = [], []
        news_tag = safe("경영권 분쟁 첫 보도", lambda: DN.recent_first_tickers(store, d), set()) \
            | {str(r["ticker"]) for r in news_rows}
    filing_tag = {str(x["ticker"]) for x in dis}

    def tags(t: str) -> str:
        return (" 🔔분쟁 보도" if t in news_tag else "") + (" ⚠분쟁 공시" if t in filing_tag else "")

    title = f"━━ 오늘 기록된 추천 (기준일 {d[5:]}) ━━"
    items = []
    for r in rows:
        s = stock_label(r["name"], r["ticker"])
        if r["ticker"] in halves:
            s += f" ⚠{_pct_drop(halves[r['ticker']])}"
        if r["ticker"] in pmap:
            s += f" (눌림목 {int(pmap[r['ticker']]['ma'])}일선)"
        items.append(s + tags(str(r["ticker"])))
    sections: list[list[str]] = [[f"【점수 {len(rows)}】", " · ".join(items) if items else "없음"]]
    if halfdrop_on():
        info["half_drop"] = sorted(halves)
    if pullback_on():
        sections.append([f"【눌림목 {len(pulls)}】",
                         *([f"{stock_label(p['name'], p['ticker'])} {int(p['ma'])}일선 · "
                            f"{_pct_drop(p['drop_pct'])}{tags(str(p['ticker']))}" for p in pulls]
                           or ["없음"])])
        info["pullback"] = [str(p["ticker"]) for p in pulls]
    if news_on():
        if news_rows:
            sections.append([f"【경영권 분쟁 첫 보도 {len(news_rows)}】", *news_lines])
        info["news"] = [str(r["ticker"]) for r in news_rows]
        info["news_ids"] = [int(r["id"]) for r in news_rows]
    if dispute_on():
        if dis:
            sections.append([f"【경영권 분쟁 공시 · 주의 {len(dis)}】", _dispute_note(),
                             *[f"{stock_label(x['name'], x['ticker'])} "
                               f"{DISPUTE_LABEL.get(x['kind'], x['kind'])} {str(x['rcept_dt'])[5:]}"
                               for x in dis]])
        info["dispute"] = [str(x["ticker"]) for x in dis]
    if errors:
        info["errors"] = errors
    chunks = [[title], *sections] + ([[note]] if note else [])
    info["messages"] = pack_messages(chunks)
    return [ln for ch in chunks for ln in ch], info


def _cell_line(label: str, g: pd.DataFrame, extra: str = "") -> str:
    c = E.stat_cell(g["net"] if len(g) else [])
    if not c.get("enough"):
        return f"• {label}: 표본 부족 (n={c.get('n', 0)})"
    return (f"• {label}: 평균 {c['mean']:+.2f}% · 중앙 {c['median']:+.2f}% · "
            f"승률 {c['win']:.0f}% (n={c['n']}){extra}")


def type_lines(store, upto: str, scope: str = "send") -> list[str]:
    """주간 성적표(scope="send")·주간 파일(scope="file")의 유형별 줄. 켜진 유형만.
    표본 20건 미만은 "표본 부족". 분쟁 첫 보도는 파일에만 싣는다."""
    from . import dispute_news as DN
    out: list[str] = []
    kinds = [k for k, on in ((KIND_PULLBACK, pullback_on()), (KIND_DISPUTE, dispute_on()),
                             (KIND_NEWS, scope == "file" and DN.line_on())) if on]
    if not kinds:
        return out
    upto = str(upto)[:10]
    with closing(_conn(store)) as con:
        res = pd.read_sql_query(
            "SELECT p.kind, r.rule, r.net, r.days, r.status FROM sig_results r "
            "JOIN sig_paper p ON p.id = r.paper_id WHERE r.exit_date<=?", con, params=(upto,))
        news_n = con.execute(
            "SELECT COUNT(*), SUM(shown), SUM(filing_after_dt IS NOT NULL) FROM sig_dispute_news "
            "WHERE kind='first' AND (show_skip IS NULL OR shown=1) AND substr(detected,1,10)<=?",
            (upto,)).fetchone() if KIND_NEWS in kinds else None
    res = res[res["status"].isin([E.ST_OK, E.ST_HALTED])] if len(res) else res
    for k in kinds:
        if k == KIND_NEWS:
            out.append(f"• {KIND_LABEL[k]} · 감지 {int(news_n[0] or 0)}건 (표시 {int(news_n[1] or 0)}건 · "
                       f"뒤에 분쟁 공시가 나온 것 {int(news_n[2] or 0)}건)")
        for h in KIND_HORIZONS[k]:
            g = res[(res["kind"] == k) & (res["rule"] == f"h{h}")] if len(res) else res
            out.append(_cell_line(f"{KIND_LABEL[k]} · {h}거래일 보유", g))
        if k == KIND_PULLBACK:
            g = res[(res["kind"] == k) & (res["rule"] == RULE_MA)] if len(res) else res
            extra = (f" · 평균 보유 {g['days'].astype(float).mean():.1f}거래일" if len(g) else "")
            out.append(_cell_line(f"{KIND_LABEL[k]} · 평균선 이탈 종료", g, extra))
    return out
