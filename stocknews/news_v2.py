# -*- coding: utf-8 -*-
"""[G] 뉴스 v2 — 브리핑 표시 개편. 플래그 `config.NEWS_V2` (기본 OFF).

OFF 면 이 모듈은 불리지 않는다. 렌더러의 `render_morning_brief` /
`render_evening_brief` 가 플래그를 보고 여기로 넘길 뿐이고, 수집·적재·
중요도(`news.process_and_store`)는 ON/OFF 와 무관하게 그대로다.

ON 일 때 바뀌는 것 (브리핑 텍스트만)
------------------------------------
  1. 대상 종목   추천(최근 이벤트 기간 안) ∪ 추적 종목 → 그 이름이 들어간
                 기사를 맨 위 '⭐ 추천·추적 종목' 섹션으로 올린다.
  2. 사건 묶기   기존 3층 묶음(`renderer._group_events`) 위에 두 단계를 더한다.
                   a) DART 행 분리 — 대표와 종목이 겹치지 않는 공시는 떼어 낸다.
                      공시 제목은 서식이 같아 회사가 달라도 묶여 있었다
                      (실측: '[공시] 제주항공 영업(잠정)실적' + '[공시] 풍산 …').
                   b) 대표 제목끼리 문자 2-gram Jaccard >= NEWS_V2_TITLE_SIM
                      이면 합친다(DART 제외, 회차·종목 가드 유지).
                 화면에는 대표 1건 + "외 n건".
  3. 공시 블록   추천·추적 종목의 당일 DART 공시 — 제목·링크만, 뉴스 위.
  4. 밤사이 시장 아침 브리핑 맨 앞. 미국 3대 지수 마감 + 원/달러.
                 값마다 기준 시각을 붙이고, 못 가져온 줄은 뺀다. 전부 실패면
                 블록 자체를 생략한다.

뉴스에 점수를 매기지 않는다는 교리(news.py)는 그대로다. 위로 올리는 기준은
'내가 추천·추적 중인 종목인가' 하나뿐이고 호재/악재 판정은 하지 않는다.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import pandas as pd

from . import config as C

log = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))

__all__ = ["enabled", "Focus", "focus_tickers", "title_sim", "refine_groups",
           "fetch_dart_disclosures", "dart_block", "fetch_market",
           "market_block", "render_morning_v2", "render_evening_v2",
           "LAST_INFO"]

# 마지막 렌더의 요약(run_screen 이 --json SUMMARY 에 싣는다). 값만, 키 없음.
LAST_INFO: dict = {}


def enabled() -> bool:
    """호출 시점에 읽는다(스모크가 플래그를 바꿔 끼운다)."""
    return bool(getattr(C, "NEWS_V2", False))


def _ds(x) -> str:
    if isinstance(x, str):
        return x[:10]
    return pd.Timestamp(x).strftime("%Y-%m-%d")


# ══════════════════════════ 1. 대상 종목 ══════════════════════════
@dataclass
class Focus:
    """브리핑에서 위로 올릴 종목. reco / tracked 는 종목코드 집합."""

    reco: set = field(default_factory=set)
    tracked: set = field(default_factory=set)
    d_ref: str | None = None          # 기준 recos 거래일
    names: dict = field(default_factory=dict)
    source: str = "none"              # send_targets / events / none

    @property
    def all(self) -> set:
        return set(self.reco) | set(self.tracked)

    def label(self, code: str) -> str:
        a, b = code in self.reco, code in self.tracked
        return "추천·추적" if a and b else ("추천" if a else ("추적" if b else ""))


def focus_tickers(store, asof) -> Focus:
    """추천 ∪ 추적 종목.

    추천  `v2_hooks.send_targets(store, d_ref)` 가 None 이 아니면 그 목록
          (B 묶음의 발송·추적 대상). None 이면 recos 를 [0] 이벤트 판정
          (`events.assign_events`, EVENT_WINDOW_DAYS)에 넣어 d_ref 가 이벤트
          기간 안에 있는 종목.
    추적  `v2_hooks.tracked_tickers(store, d_ref)` (D 묶음. 지금은 빈 집합).
    d_ref 는 asof 날짜 이하의 최신 recos 거래일이다. 아침 브리핑(06:20)이면
    전 거래일, 저녁 브리핑(23:00, nightly 뒤)이면 그날이다.
    """
    from . import v2_hooks
    today = _ds(asof)
    f = Focus()
    try:
        rec = store.reco_history(days=max(int(C.EVENT_WINDOW_DAYS) * 2, 40))
    except Exception as exc:  # noqa: BLE001 - 대상이 없어도 브리핑은 나간다
        log.warning("뉴스v2: recos 조회 실패 — 추천 대상 없음으로 진행 (%s)", exc)
        rec = pd.DataFrame()
    if rec is not None and not rec.empty:
        rec = rec.copy()
        rec["d"] = rec["d"].map(_ds)
        rec["ticker"] = rec["ticker"].astype(str)
        rec = rec[rec["d"] <= today]
    if rec is not None and not rec.empty:
        f.d_ref = str(rec["d"].max())
        f.names.update(dict(zip(rec["ticker"], rec["name"].astype(str))))
        st = None
        try:
            st = v2_hooks.send_targets(store, f.d_ref)
        except Exception as exc:  # noqa: BLE001
            log.warning("뉴스v2: send_targets 훅 실패 — 이벤트 판정으로 대체 (%s)", exc)
        if st is not None:
            f.reco = {str(r["ticker"]) for r in st}
            f.source = "send_targets"
        else:
            f.reco = _active_event_tickers(store, rec, f.d_ref)
            f.source = "events"
    try:
        f.tracked = {str(t) for t in
                     (v2_hooks.tracked_tickers(store, f.d_ref or today) or ())}
    except Exception as exc:  # noqa: BLE001
        log.warning("뉴스v2: tracked_tickers 훅 실패 (%s)", exc)
        f.tracked = set()
    missing = [t for t in f.tracked if t not in f.names]
    if missing:
        try:
            act = store.active_tickers()
            f.names.update({t: act.get(t, t) for t in missing})
        except Exception:  # noqa: BLE001
            f.names.update({t: t for t in missing})
    return f


def _active_event_tickers(store, rec: pd.DataFrame, d_ref: str) -> set:
    from . import events as ev
    try:
        cal = set(ev.trading_calendar(store, since=str(rec["d"].min())))
    except Exception:  # noqa: BLE001 - 달력이 없으면 recos 날짜로 대신한다
        cal = set()
    # recos 날짜는 정의상 거래일이다. 부분 적재일이라 달력에서 빠져도 넣는다.
    cal = sorted(cal | set(rec["d"]))
    cols = [c for c in ("d", "ticker", "slot", "grade", "rank") if c in rec.columns]
    a = ev.assign_events(rec[cols], cal, window=int(C.EVENT_WINDOW_DAYS))
    if a.empty:
        return set()
    last = a.sort_values(["ticker", "d"]).groupby("ticker").tail(1)
    alive = last[(last["event_date"] <= d_ref)
                 & (last["window_end"].astype(str) >= d_ref)]
    return set(alive["ticker"].astype(str))


# ══════════════════════════ 2. 사건 묶기 ══════════════════════════
def _bigrams(s: str) -> set:
    s = (s or "").replace(" ", "")
    return {s[i:i + 2] for i in range(len(s) - 1)}


def title_sim(a: str, b: str) -> float:
    """정규화 제목의 문자 2-gram Jaccard. 조사·띄어쓰기 차이에 강하다.

    '6천629억원에 수주' / '6629억원 수주' 처럼 토큰이 어긋나 기존 3층이
    놓치는 쌍을 잡는다(실측 0.656).
    """
    x, y = _bigrams(a), _bigrams(b)
    if not x or not y:
        return 0.0
    i = len(x & y)
    return i / (len(x) + len(y) - i)


def _is_dart(r) -> bool:
    return str(r.get("origin") or "") == "DART"


def refine_groups(groups: list[list], codes_of: dict,
                  sim: float | None = None) -> tuple[list[list], dict]:
    """기존 묶음 위의 두 단계(DART 분리 → 대표 제목 유사도 병합).

    groups   `renderer._group_events` 결과. 각 묶음의 0번이 대표.
    codes_of {news_id: set(종목코드)}
    반환     (새 묶음 목록, {"dart_split": n, "merged": n})
    """
    from .news import event_serials, normalize_title, representative_rank
    th = float(C.NEWS_V2_TITLE_SIM if sim is None else sim)
    stats = {"dart_split": 0, "merged": 0}

    def codes(r) -> set:
        return set(codes_of.get(r["id"], ()))

    # a) DART 분리 — 대표나 구성원 중 한쪽이 공시인데 종목이 겹치지 않으면 뗀다.
    split: list[list] = []
    for g in groups:
        rep, keep, alone = g[0], [g[0]], []
        for r in g[1:]:
            if (_is_dart(r) or _is_dart(rep)) and not (codes(r) & codes(rep)):
                alone.append([r])
            else:
                keep.append(r)
        split.append(keep)
        split.extend(alone)
        stats["dart_split"] += len(alone)

    # b) 대표 제목 유사도 병합 (DART 대표 제외)
    n = len(split)
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    reps = [g[0] for g in split]
    tn = [str(r.get("title_norm") or normalize_title(str(r.get("title") or "")))
          for r in reps]
    ser = [event_serials(r.get("title")) for r in reps]
    cod = [codes(r) for r in reps]
    for i in range(n):
        if _is_dart(reps[i]):
            continue
        for j in range(i + 1, n):
            if _is_dart(reps[j]):
                continue
            if ser[i] and ser[j] and not (ser[i] & ser[j]):
                continue
            if cod[i] and cod[j] and not (cod[i] & cod[j]):
                continue
            if title_sim(tn[i], tn[j]) >= th:
                a, b = find(i), find(j)
                if a != b:
                    parent[max(a, b)] = min(a, b)
                    stats["merged"] += 1

    merged: dict[int, list] = {}
    for i in range(n):            # 순서 = 가장 앞선 구성 묶음의 위치
        merged.setdefault(find(i), []).extend(split[i])
    out = []
    for k in sorted(merged):
        g = merged[k]
        g.sort(key=lambda r: representative_rank(r, has_ticker=bool(codes(r))))
        out.append(g)
    return out, stats


def _focus_hit(g: list, codes_of: dict, focus: Focus) -> set:
    allc = focus.all
    hit: set = set()
    for r in g:
        hit |= set(codes_of.get(r["id"], ())) & allc
    return hit


def _assign(groups: list[list], priority: tuple) -> dict:
    """renderer._assign_sections 와 같은 배정 + 묶음 크기.

    priority = ((라벨, 판정함수(group), 상한), ...). 한 사건은 처음 걸린
    섹션 하나로만 간다. 반환 {라벨: [(대표, 매체 수, 기사 수), ...]}
    """
    from .news import canonical_source
    picked: dict = {label: [] for label, _, _ in priority}
    for g in groups:
        for label, match, cap in priority:
            if len(picked[label]) >= cap:
                continue
            if match(g):
                n_media = len({canonical_source(x.get("source"))
                               for x in g if x.get("source")})
                picked[label].append((g[0], n_media, len(g)))
                break
    return picked


def _line(rep, tm: dict, n_media: int, n_items: int) -> str:
    """기존 한 줄(`renderer._news_line`) + 첫 줄 끝에 '외 n건'."""
    from .renderer import _news_line
    s = _news_line(rep, tm, cluster_n=n_media)
    if n_items <= 1:
        return s
    head, sep, tail = s.partition("\n")
    return f"{head} <i>외 {n_items - 1}건</i>{sep}{tail}"


# ══════════════════════════ 3. DART 공시 블록 ══════════════════════════
_DART_LIST = "https://opendart.fss.or.kr/api/list.json"
_DART_VIEW = "https://dart.fss.or.kr/dsaf001/main.do?rcpNo={}"


def fetch_dart_disclosures(bgn: str, end: str, markets=None,
                           max_pages: int | None = None) -> dict | None:
    """Open DART 공시 목록(전 유형) bgn~end. 실패면 None.

    키는 환경변수 DART_API_KEY (news_sources·flags 와 같은 방식). 없으면 None.
    한 페이지라도 실패하면 None 이다 — 일부만 받은 목록을 '전부'처럼
    보이면 '공시 없음' 이 거짓이 된다. 페이지 상한에 걸리면 truncated=True.
    반환 {"rows": [{stock_code, corp_name, report_nm, rcept_no, rcept_dt}],
          "truncated": bool, "pages": n}
    """
    from .news_sources import _get
    key = os.getenv("DART_API_KEY")
    if not key:
        return None
    markets = tuple(markets) if markets is not None else tuple(C.DART_MARKETS)
    cap = int(C.NEWS_V2_DART_MAX_PAGES if max_pages is None else max_pages)
    rows: list[dict] = []
    truncated, pages = False, 0
    for market in markets:
        if market == "N":
            continue
        page = 1
        while True:
            res = _get(_DART_LIST, params={
                "crtfc_key": key, "bgn_de": bgn.replace("-", ""),
                "end_de": end.replace("-", ""), "corp_cls": market,
                "page_no": page, "page_count": 100})
            pages += 1
            if res is None:
                log.warning("뉴스v2: DART 공시 목록 실패 (corp_cls=%s p%d)", market, page)
                return None
            try:
                data = res.json()
            except ValueError:
                return None
            status = data.get("status")
            if status == "013":           # 조회 데이터 없음 — 정상
                break
            if status != "000":
                log.warning("뉴스v2: DART 응답 코드 %s (corp_cls=%s)", status, market)
                return None
            for it in data.get("list") or []:
                sc = str(it.get("stock_code") or "").strip()
                if not sc:
                    continue
                rows.append({"stock_code": sc.zfill(6),
                             "corp_name": str(it.get("corp_name") or ""),
                             "report_nm": " ".join(str(it.get("report_nm") or "").split()),
                             "rcept_no": str(it.get("rcept_no") or ""),
                             "rcept_dt": str(it.get("rcept_dt") or "")})
            total = int(data.get("total_page") or 1)
            if page >= total:
                break
            if page >= cap:
                truncated = True
                log.warning("뉴스v2: DART 페이지 상한 %d 도달 (corp_cls=%s, 전체 %d)",
                            cap, market, total)
                break
            page += 1
            time.sleep(0.3)
    return {"rows": rows, "truncated": truncated, "pages": pages}


def _dart_from_db(store, codes: set, bgn: str) -> list[dict]:
    """대체 경로 — 수집 배치가 news 에 넣은 DART 행(유형 필터를 거친 것)."""
    from contextlib import closing
    if not codes:
        return []
    marks = ",".join("?" * len(codes))
    q = ("SELECT n.title, n.url, n.published, nt.ticker FROM news n "
         "JOIN news_tickers nt ON nt.news_id=n.id "
         f"WHERE n.origin='DART' AND nt.ticker IN ({marks}) "
         "AND substr(COALESCE(n.published,n.collected),1,10)>=?")
    try:
        with closing(store._conn()) as con:
            got = con.execute(q, (*sorted(codes), bgn)).fetchall()
    except Exception as exc:  # noqa: BLE001
        log.warning("뉴스v2: DB 공시 대체 조회 실패 (%s)", exc)
        return []
    out = []
    for title, url, pub, tk in got:
        t = str(title or "")
        if t.startswith("[공시] "):
            t = t[5:]
        rno = str(url or "").rsplit("rcpNo=", 1)[-1] if url else ""
        out.append({"stock_code": str(tk), "corp_name": "", "report_nm": t,
                    "rcept_no": rno, "rcept_dt": str(pub or "")[:10].replace("-", ""),
                    "url": url, "_title_full": True})
    return out


def dart_block(store, focus: Focus, asof, fetch=None) -> tuple[list[str], dict]:
    """추천·추적 종목의 공시 블록. 대상 종목이 없으면 블록 없음.

    기간은 focus.d_ref(최신 recos 거래일) ~ asof 날짜. 아침 06:20 이면
    전 거래일 장 마감 뒤 공시까지, 저녁 23:00 이면 그날 하루다.
    fetch(bgn, end) 는 `fetch_dart_disclosures` 와 같은 규약(스모크 주입용).
    """
    from .renderer import _e
    codes = focus.all
    info = {"targets": len(codes), "rows": 0, "source": None}
    if not codes:
        return [], info
    end = _ds(asof)
    bgn = min(focus.d_ref or end, end)
    fetch = fetch or fetch_dart_disclosures
    try:
        res = fetch(bgn, end)
    except Exception as exc:  # noqa: BLE001
        log.warning("뉴스v2: DART 공시 조회 예외 (%s)", exc)
        res = None
    if res is not None:
        rows = [r for r in res.get("rows") or () if r.get("stock_code") in codes]
        info.update(source="dart_api", truncated=bool(res.get("truncated")))
    else:
        rows = _dart_from_db(store, codes, bgn)
        info.update(source="db_fallback")
    # 같은 종목·같은 보고서명은 한 줄(최신 접수번호) + "(n건)". 한 공시가
    # 두 종목에 걸려도 한 줄이다. 실측: 'LG씨엔에스 기업설명회(IR)개최' 3건.
    rows = sorted(rows, key=lambda r: (r.get("rcept_no") or ""), reverse=True)
    seen: dict = {}
    uniq = []
    for r in rows:
        k = (r.get("stock_code"), r.get("report_nm"))
        if k in seen:
            seen[k]["_n"] = seen[k].get("_n", 1) + 1
            continue
        seen[k] = r
        uniq.append(r)
    rows = uniq
    info["rows"] = len(rows)
    if res is not None and "pages" in res:
        info["pages"] = int(res["pages"])

    span = f"{bgn[5:].replace('-', '/')}" + (
        "" if bgn == end else f"~{end[5:].replace('-', '/')}")
    out = [f"━━ 📑 추천·추적 종목 공시 ({span}) ━━"]
    if not rows:
        if res is None:
            out.append("• DART 직접 조회 불가(키 없음·실패) · 수집분에도 해당 공시 없음")
        else:
            out.append(f"• 해당 공시 없음 (대상 {len(codes)}종목)")
        return out + [""], info
    cap = int(C.NEWS_V2_DART_MAX_LINES)
    for r in rows[:cap]:
        name = r.get("corp_name") or focus.names.get(r["stock_code"], r["stock_code"])
        title = (r["report_nm"] if r.get("_title_full")
                 else f"{name} {r['report_nm']}")
        url = r.get("url") or _DART_VIEW.format(r.get("rcept_no", ""))
        more = f" ({r['_n']}건)" if r.get("_n", 1) > 1 else ""
        out.append(f"• <a href=\"{_e(url)}\">{_e(title[:100])}</a>{more}")
    if len(rows) > cap:
        out.append(f"• 외 {len(rows) - cap}건")
    if res is None:
        out.append("(DART 직접 조회 불가(키 없음·실패) — 수집 배치가 넣은 공시만 표시)")
    elif res.get("truncated"):
        out.append("(DART 페이지 상한 도달 — 일부 누락 가능)")
    out.append("")
    return out, info


# ══════════════════════════ 4. 밤사이 시장 ══════════════════════════
def _fdr_reader(symbol: str, start: str, timeout: float = 20.0):
    """FinanceDataReader 호출을 데몬 스레드로 감싸 시간 제한을 건다.

    FDR 은 요청 타임아웃을 받지 않는다. 브리핑이 시세 하나 때문에 멈추면
    안 되므로 제한 시간이 지나면 버린다(데몬이라 종료를 막지 않는다).
    """
    box: dict = {}

    def run():
        try:
            import FinanceDataReader as fdr
            box["df"] = fdr.DataReader(symbol, start)
        except Exception as exc:  # noqa: BLE001
            box["err"] = exc

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        raise TimeoutError(f"{symbol} {timeout:.0f}초 초과")
    if "err" in box:
        raise box["err"]
    return box.get("df")


def _closes(df) -> pd.Series:
    if df is None or len(df) == 0 or "Close" not in df.columns:
        return pd.Series(dtype=float)
    s = pd.to_numeric(df["Close"], errors="coerce")
    s.index = [pd.Timestamp(i).date() for i in df.index]
    s = s[s > 0].dropna()
    return s[~s.index.duplicated(keep="last")].sort_index()


def fetch_market(asof, reader=None) -> list[dict]:
    """미국 3대 지수 마감 + 원/달러. 못 받은 항목은 빠진다(빈 목록 가능).

    asof   KST naive 또는 aware datetime (브리핑 시각)
    지수   뉴욕 날짜 기준 봉. 오늘(뉴욕) 봉은 16:00 ET + 버퍼 이후에만
           '마감'으로 쓰고, 그 전이면 버리고 직전 봉을 쓴다(장중 값 방지).
    환율   마지막 봉의 종가 = 조회 시점 시세(외환은 24시간). 기준 시각은
           조회 시각(KST)으로 표시한다. 등락은 직전 봉 종가 대비.
    """
    from zoneinfo import ZoneInfo
    reader = reader or _fdr_reader
    now = asof if asof.tzinfo else asof.replace(tzinfo=KST)
    now_kst = now.astimezone(KST)
    now_et = now.astimezone(ZoneInfo("America/New_York"))
    start = (now_kst - timedelta(days=14)).strftime("%Y-%m-%d")
    max_age = int(C.NEWS_V2_MARKET_MAX_AGE_DAYS)
    closed_after = (16 * 60 + int(C.NEWS_V2_US_CLOSE_BUFFER_MIN))
    out: list[dict] = []

    for label, sym in C.NEWS_V2_US_INDICES:
        try:
            s = _closes(reader(sym, start))
        except Exception as exc:  # noqa: BLE001
            log.warning("뉴스v2: 지수 %s(%s) 조회 실패: %s: %s",
                        label, sym, type(exc).__name__, exc)
            continue
        today_et = now_et.date()
        s = s[s.index <= today_et]
        if len(s) and s.index[-1] == today_et and \
                now_et.hour * 60 + now_et.minute < closed_after:
            s = s.iloc[:-1]                       # 진행 중인 세션
        if len(s) < 2:
            continue
        last_d = s.index[-1]
        if (today_et - last_d).days > max_age:
            continue
        v, p = float(s.iloc[-1]), float(s.iloc[-2])
        out.append({"label": label, "symbol": sym, "value": v,
                    "chg_pct": (v / p - 1.0) * 100.0, "kind": "index",
                    "date": last_d.isoformat(),
                    "base": f"{last_d:%m/%d} 뉴욕 마감"})

    label, sym = C.NEWS_V2_FX
    try:
        s = _closes(reader(sym, start))
        s = s[s.index <= now_kst.date()]
        if len(s) >= 2 and (now_kst.date() - s.index[-1]).days <= max_age:
            v, p = float(s.iloc[-1]), float(s.iloc[-2])
            out.append({"label": label, "symbol": sym, "value": v,
                        "chg_pct": (v / p - 1.0) * 100.0, "kind": "fx",
                        "date": s.index[-1].isoformat(),
                        "base": f"{now_kst:%m/%d %H:%M} KST 조회 시세"})
    except Exception as exc:  # noqa: BLE001
        log.warning("뉴스v2: 환율 %s 조회 실패: %s: %s", sym,
                    type(exc).__name__, exc)
    return out


def market_block(rows: list[dict]) -> list[str]:
    """빈 목록이면 블록 없음(빈 리스트)."""
    if not rows:
        return []
    out = ["━━ 🌐 밤사이 시장 ━━"]
    for r in rows:
        sign = "+" if r["chg_pct"] >= 0 else ""
        unit = "원" if r["kind"] == "fx" else ""
        out.append(f"• {r['label']} {r['value']:,.2f}{unit} "
                   f"({sign}{r['chg_pct']:.2f}%) · {r['base']}")
    out.append("")
    return out


# ══════════════════════════ 5. 렌더 ══════════════════════════
def _prepare(store, df, focus: Focus):
    """묶기 + 대상 우선 정렬. 반환 (tm, codes_of, groups, stats)."""
    from .renderer import _group_events, _tick_map
    rows = [r for _, r in df.iterrows()]
    tm = _tick_map(store, rows)
    codes_of: dict = {}
    m = store.news_ticker_map([r["id"] for r in rows])
    if m is not None and not m.empty:
        for nid, grp in m.groupby("news_id"):
            codes_of[nid] = set(grp["ticker"].astype(str))
    groups = _group_events(df, tm)
    groups, stats = refine_groups(groups, codes_of)
    # 대상 종목의 DART 공시는 공시 블록에 이미 있다 — 섹션에서 다시 찍지 않는다.
    before = len(groups)
    groups = [g for g in groups
              if not (_is_dart(g[0]) and _focus_hit(g, codes_of, focus))]
    stats["dart_in_block"] = before - len(groups)
    # 대상 종목 묶음을 앞으로(안정 정렬 — 나머지는 중요도 순서 유지).
    groups.sort(key=lambda g: 0 if _focus_hit(g, codes_of, focus) else 1)
    stats["focus_events"] = sum(1 for g in groups if _focus_hit(g, codes_of, focus))
    return tm, codes_of, groups, stats


def _focus_header(focus: Focus) -> str:
    return "⭐ 추천·추적 종목"


def render_morning_v2(store, asof, hours: int = 16, per_section: int = 5,
                      *, market_reader=None, dart_fetch=None) -> str:
    """아침 브리핑 v2. 밤사이 시장 → 공시 → 추천·추적 → 기존 섹션."""
    info: dict = {}
    focus = focus_tickers(store, asof)
    info.update(focus=len(focus.all), focus_source=focus.source, d_ref=focus.d_ref)
    mrows = fetch_market(asof, reader=market_reader)
    mk = market_block(mrows)
    info["market_lines"] = len(mrows)
    dart, dinfo = dart_block(store, focus, asof, fetch=dart_fetch)
    info["dart"] = dinfo

    df = store.news_since(hours=hours)
    head = [f"☀️ <b>[아침 브리핑]</b> {asof:%Y-%m-%d %H:%M}"]
    if df is None or df.empty:
        LAST_INFO.clear()
        LAST_INFO.update(info)
        return "\n".join(head + [""] + mk + dart
                         + ["• 수집된 뉴스가 없습니다. 수집 배치를 확인하십시오."])

    tm, codes_of, groups, stats = _prepare(store, df, focus)
    info.update(stats)
    n_ev = len(groups) + stats.get("dart_in_block", 0)
    out = head + [f"• 최근 {hours}시간 {len(df)}건 · 사건 {n_ev}건", ""]
    out += mk
    out += dart

    fl = _focus_header(focus)
    priority = (
        (fl, lambda g: bool(_focus_hit(g, codes_of, focus)), int(C.NEWS_V2_FOCUS_MAX)),
        ("🎯 내 종목·고중요도", lambda g: float(g[0].get("importance") or 0.0) >= 5.0, per_section),
        ("📄 주요 공시", lambda g: g[0].get("category") == "공시", per_section),
        ("🏛 매크로·정책", lambda g: g[0].get("category") in ("매크로", "정책"), per_section),
        ("🌏 밤사이 해외", lambda g: g[0].get("region") in ("US", "GLOBAL"), per_section),
    )
    display = (fl, "🌏 밤사이 해외", "🏛 매크로·정책", "🎯 내 종목·고중요도", "📄 주요 공시")
    picked = _assign(groups, priority)
    for label in display:
        rows = picked.get(label) or []
        if not rows:
            continue
        out.append(f"━━ {label} ({len(rows)}건) ━━")
        out += [_line(r, tm, nm, ni) for r, nm, ni in rows]
        out.append("")

    out.append("※ 뉴스에는 점수를 매기지 않습니다. 중요도는 '몇 개 매체가")
    out.append("  다뤘는가 + 내 종목인가'로만 정렬됩니다.")
    out.append("※ ⭐ 는 추천(최근 이벤트 기간)·추적 종목 이름이 든 기사입니다."
               " 검증 중 · 투자 판단 근거 아님.")
    LAST_INFO.clear()
    LAST_INFO.update(info)
    return "\n".join(out)


def render_evening_v2(store, asof, picks: list | None = None,
                      hours: int = 12, per_section: int = 6,
                      *, dart_fetch=None, foot_lines: list | None = None,
                      stock_block: str | None = None,
                      watchlist: bool = True) -> str:
    """저녁 브리핑 v2. 공시 → 추천·추적 → 기존 섹션 → 추천 연동 → 관심종목.

    foot_lines / stock_block / watchlist 는 renderer.render_evening_brief 와
    같은 뜻이다([B] 제외 한 줄 · [D·F] 종목 부분 ①②③④ · 관심종목 목록).
    """
    from .renderer import _e, render_watchlist
    info: dict = {}
    focus = focus_tickers(store, asof)
    info.update(focus=len(focus.all), focus_source=focus.source, d_ref=focus.d_ref)
    dart, dinfo = dart_block(store, focus, asof, fetch=dart_fetch)
    info["dart"] = dinfo

    df = store.news_since(hours=hours, region="KR")
    out = [f"🌙 <b>[저녁 뉴스 정리]</b> {asof:%Y-%m-%d %H:%M}"]
    if df is None or df.empty:
        out.append("")
        out += dart
        out.append("• 국내 뉴스 수집분이 없습니다.")
        if stock_block is not None:
            out += ["", stock_block, "", f"⏰ {asof:%Y-%m-%d %H:%M:%S}"]
        LAST_INFO.clear()
        LAST_INFO.update(info)
        return "\n".join(out)

    tm, codes_of, groups, stats = _prepare(store, df, focus)
    info.update(stats)
    n_ev = len(groups) + stats.get("dart_in_block", 0)
    out.append(f"• 최근 {hours}시간 {len(df)}건 · 사건 {n_ev}건")
    out.append("")
    out += dart

    themes = ("반도체", "2차전지", "방산조선", "바이오", "전력AI")
    fl = _focus_header(focus)
    priority = (
        (fl, lambda g: bool(_focus_hit(g, codes_of, focus)), int(C.NEWS_V2_FOCUS_MAX)),
        ("📄 공시", lambda g: g[0].get("category") == "공시", per_section),
        ("💧 수급", lambda g: g[0].get("category") == "수급", per_section),
        ("🏭 테마·업종", lambda g: g[0].get("category") in themes, per_section),
        ("📊 시황·실적", lambda g: g[0].get("category") in ("국내시황", "실적"), per_section),
    )
    picked = _assign(groups, priority)
    for label, _, _ in priority:
        rows = picked.get(label) or []
        if not rows:
            continue
        out.append(f"━━ {label} ({len(rows)}건) ━━")
        out += [_line(r, tm, nm, ni) for r, nm, ni in rows]
        out.append("")

    # 추천 10선과 뉴스 교차 — 기존 블록 그대로
    if stock_block is not None:
        out.append(stock_block)
        out.append("")
    elif picks:
        codes = {r.ticker for _, r in picks}
        m = store.news_ticker_map(df["id"].tolist())
        hit: dict = {}
        if m is not None and not m.empty:
            for _, r in m[m["ticker"].isin(codes)].iterrows():
                hit.setdefault(r["ticker"], 0)
                hit[r["ticker"]] += 1
        out.append("━━ 🔗 추천 10선 뉴스 연동 ━━")
        if hit:
            for _, r in picks:
                n = hit.get(r.ticker)
                if n:
                    out.append(f"• <b>{_e(r.name)}</b> 관련 뉴스 {n}건 "
                               f"· 매집 {r.value_score:.1f}/추세 {r.trend_score:.1f}")
        else:
            out.append("• 추천 종목 관련 뉴스 없음 (조용한 바닥일 수 있음)")
        if foot_lines:
            out += list(foot_lines)
        out.append("")
    elif foot_lines:
        out.append("━━ 🔗 추천 10선 뉴스 연동 ━━")
        out += list(foot_lines)
        out.append("")

    if watchlist:
        out.append(render_watchlist(store))
        out.append("")
    out.append(f"⏰ {asof:%Y-%m-%d %H:%M:%S}")
    LAST_INFO.clear()
    LAST_INFO.update(info)
    return "\n".join(out)
