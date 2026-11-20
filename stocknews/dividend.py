# -*- coding: utf-8 -*-
"""[S] 고배당 줄 (DIVIDEND_LINE · 2026-11-20 지시). 기록·표시 전용.

**점수·추천 선정·recos 기록에 닿지 않는다.** 플래그가 OFF(기본)면 아무것도 받지 않는다.

자료  네이버 금융 종목별 연간 재무 API (m.stock.naver.com/api/stock/<코드>/finance/annual).
      한 종목 1요청. 확정 사업연도 3개(추정 열 제외)의 영업이익(억원) · EPS(원) ·
      주당배당금(원)을 받는다. 키·로그인 없음.
      배당수익률 = 최근 연도 주당 배당금 ÷ 기준일 종가.
      배당성향   = 최근 연도 주당 배당금 ÷ 최근 연도 EPS  (이 API 에 배당성향 행이 없어 계산한다 —
                   우선주가 있는 회사는 공시 배당성향과 조금 다를 수 있다).
      하루 한 번(daily 스캔 뒤) 받아 sig_dividend_data · sig_dividend_list 에 저장한다.
      응답이 비거나 형식이 바뀌어 유니버스의 DIVIDEND_MIN_COVERAGE(50%) 미만만 읽히면
      그날은 "자료 없음"으로 적는다(조용히 0건으로 두지 않는다 — 줄에 표시하고 관리자에게 알린다).

조건 (전부 통과)
  ① 운영 배제 규칙 통과 = 그날 scans 에 기록된 종목(운영 유니버스)
  ② 최근 3개 사업연도 연속 현금배당(셋 다 > 0)이고 주당 배당금을 줄인 적 없음(최근 ≥ 1년 전 ≥ 2년 전)
  ③ 배당성향 ≤ DIVIDEND_MAX_PAYOUT(80%) — EPS 가 0 이하면 탈락
  ④ 최근 연간 영업이익 > 0
  ⑤ 종가 ≥ 250거래일 최고가(직전 250봉 고가 최고값 · 당일 제외) × DIVIDEND_MIN_HIGH_RATIO(0.5)
목록  배당수익률 높은 순 상위 DIVIDEND_TOP(10). 한 업종(tickers.sector)이 DIVIDEND_SECTOR_SHARE
      (30% = 3종목)를 넘으면 건너뛰고 다음 순위로 채운다.
그만 사는 신호  오늘 목록에 있거나 최근 DIVIDEND_STOP_DAYS(60일) 안에 목록에 있던 종목이
      배당 감액·무배당 / 영업이익 적자 / 배당성향 100% 초과 / 고점 대비 50% 미만일 때.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timedelta

import numpy as np

from . import config as C

log = logging.getLogger(__name__)

API = "https://m.stock.naver.com/api/stock/{code}/finance/annual"
ROWS = ("영업이익", "EPS", "주당배당금")
META = "dividend:{d}"


def _conn(store) -> sqlite3.Connection:
    con = sqlite3.connect(str(store.path), timeout=30.0)
    con.row_factory = sqlite3.Row
    return con


def _now() -> str:
    from .trading_day import now_kst
    return now_kst().replace(tzinfo=None).isoformat(timespec="seconds")


def _num(v) -> float | None:
    s = str(v if v is not None else "").replace(",", "").strip()
    if s in ("", "-", "N/A"):
        return None
    try:
        x = float(s)
    except ValueError:
        return None
    return x if np.isfinite(x) else None


def parse_annual(j) -> dict | None:
    """API 응답 → {"fy", "dps": [최근, 1년 전, 2년 전], "eps", "op"}. 형식이 다르면 None.
    값이 "-" 인 칸은 None(주당 배당금이 None 이면 그해는 무배당으로 본다)."""
    try:
        fi = j["financeInfo"]
        keys = sorted(str(t["key"]) for t in fi["trTitleList"] if str(t.get("isConsensus")) == "N")
        rows = {str(r["title"]): r["columns"] for r in fi["rowList"]}
        if not keys or any(k not in rows for k in ROWS):
            return None
        last = keys[-3:][::-1]                         # 최근 → 과거

        def val(title, k):
            return _num((rows[title].get(k) or {}).get("value"))
        dps = [val("주당배당금", k) for k in last] + [None] * (3 - len(last))
        return {"fy": last[0], "years": len(last), "dps": dps,
                "eps": val("EPS", last[0]), "op": val("영업이익", last[0])}
    except (KeyError, TypeError, AttributeError, IndexError):
        return None


def _http_get(code: str, timeout: float):
    import requests
    r = requests.get(API.format(code=code), headers={"User-Agent": "Mozilla/5.0"}, timeout=timeout)
    r.raise_for_status()
    return r.json()


def fetch_all(tickers: list[str], get=None, workers: int | None = None,
              timeout: float | None = None) -> tuple[dict[str, dict], dict]:
    """종목별 자료를 받는다. (읽힌 것 dict, 집계). 실패한 종목은 빠진다(예외를 올리지 않는다)."""
    from concurrent.futures import ThreadPoolExecutor
    timeout = float(C.DIVIDEND_TIMEOUT) if timeout is None else timeout
    workers = max(1, int(C.DIVIDEND_WORKERS if workers is None else workers))
    get = get or (lambda code: _http_get(code, timeout))
    t0 = time.perf_counter()
    errs: dict[str, int] = {}
    # 전체 시간 상한 — 네이버가 느리거나 막혀도 daily 단계를 오래 붙잡지 않는다.
    deadline = t0 + float(getattr(C, "DIVIDEND_BUDGET_SEC", 60.0))

    def one(code):
        if time.perf_counter() > deadline:
            return code, None, "시간 상한"
        try:
            rec = parse_annual(get(code))
            if rec is None:
                return code, None, "형식"
            return code, rec, None
        except Exception as exc:  # noqa: BLE001 - 종목 하나의 실패
            return code, None, type(exc).__name__
    out: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for code, rec, err in ex.map(one, list(tickers)):
            if rec is not None:
                out[code] = rec
            else:
                errs[err or "?"] = errs.get(err or "?", 0) + 1
    return out, {"requested": len(tickers), "parsed": len(out), "errors": errs,
                 "fetch_sec": round(time.perf_counter() - t0, 1)}


def evaluate(rec: dict | None, close: float | None, high: float | None) -> dict:
    """한 종목 판정. {"ok", "fail", "yield_pct", "payout_pct", "stop"}"""
    out = {"ok": False, "fail": None, "yield_pct": None, "payout_pct": None, "stop": None}
    if rec is None:
        out["fail"] = "자료 없음"
        return out
    d0, d1, d2 = [(x if x is not None else 0.0) for x in rec["dps"][:3]]
    eps, op = rec.get("eps"), rec.get("op")
    if close and close > 0 and d0 > 0:
        out["yield_pct"] = d0 / close * 100.0
    if eps is not None and eps > 0 and d0 > 0:
        out["payout_pct"] = d0 / eps * 100.0
    low = (high is not None and close is not None
           and close < high * float(C.DIVIDEND_MIN_HIGH_RATIO) - 1e-9)
    stops = []
    if d0 <= 0:
        stops.append("무배당")
    elif d0 < d1:
        stops.append("배당 감액")
    if op is not None and op <= 0:
        stops.append("영업이익 적자")
    if d0 > 0 and eps is not None and (eps <= 0 or d0 / eps * 100.0 > float(C.DIVIDEND_STOP_PAYOUT)):
        stops.append("배당성향 100% 초과")
    if low:
        stops.append("고점 대비 50% 미만")
    out["stop"] = " · ".join(stops) or None
    if min(d0, d1, d2) <= 0:
        out["fail"] = "최근 3년 중 무배당"
    elif d0 < d1 or d1 < d2:
        out["fail"] = "배당 감액"
    elif eps is None or eps <= 0:
        out["fail"] = "순이익 적자(배당성향 계산 불가)"
    elif out["payout_pct"] > float(C.DIVIDEND_MAX_PAYOUT) + 1e-9:
        out["fail"] = "배당성향 80% 초과"
    elif op is None:
        out["fail"] = "영업이익 자료 없음"
    elif op <= 0:
        out["fail"] = "영업이익 적자"
    elif high is None or close is None:
        out["fail"] = "250거래일 미만"
    elif low:
        out["fail"] = "고점 대비 50% 미만"
    else:
        out["ok"] = True
    return out


def _status(store, d: str) -> dict | None:
    with closing(_conn(store)) as con:
        r = con.execute("SELECT v FROM sig_meta WHERE k=?", (META.format(d=d),)).fetchone()
    try:
        return json.loads(r[0]) if r else None
    except ValueError:
        return None


def _recent_listed(con, d: str, days: int) -> dict[str, str]:
    lo = (datetime.fromisoformat(d) - timedelta(days=int(days))).strftime("%Y-%m-%d")
    return {str(r[0]): r[1] for r in con.execute(
        "SELECT ticker, name FROM sig_dividend_list WHERE d>=? AND d<=?", (lo, d)).fetchall()}


def record(store, d: str, get=None, fetched: dict | None = None, force: bool = False) -> dict:
    """그날 자료를 받아 판정·저장한다. 하루 한 번(이미 적었으면 그 결과를 돌려준다).
    fetched = 이미 받은 자료(검증·되짚기용). 자료가 모자라면 status="no_data"."""
    from . import pricectx as PX
    from .signals import _universe
    d = str(d)[:10]
    prev = _status(store, d)
    if prev and prev.get("status") == "ok" and not force:
        return {**prev, "cached": True}
    uni = _universe(store, d)
    with closing(_conn(store)) as con:
        recent = _recent_listed(con, d, int(C.DIVIDEND_STOP_DAYS))
        sectors = {str(r[0]): (r[1] or "") for r in con.execute(
            "SELECT ticker, sector FROM tickers").fetchall()}
    names = {**recent, **uni}
    want = sorted(names)
    info: dict = {"d": d, "universe": len(uni), "status": "no_data"}
    if not uni:
        info["reason"] = "운영 유니버스 없음(그날 스캔 기록 없음)"
        return _save_status(store, d, info)
    if fetched is None:
        fetched, finfo = fetch_all(want, get=get)
        info.update(finfo)
    else:
        info.update({"requested": len(want), "parsed": sum(1 for t in want if t in fetched)})
    got_uni = sum(1 for t in uni if t in fetched)
    info["coverage"] = round(got_uni / len(uni), 3)
    if got_uni < len(uni) * float(C.DIVIDEND_MIN_COVERAGE):
        info["reason"] = f"자료를 읽은 종목 {got_uni}/{len(uni)}"
        return _save_status(store, d, info)
    ctx = PX.context(store, d, want)
    rows = []
    for t in want:
        rec = fetched.get(t)
        px = ctx.get(t) or {}
        ev = evaluate(rec, px.get("close"), px.get("high"))
        in_uni = t in uni
        if not in_uni and ev["ok"]:
            ev["ok"], ev["fail"] = False, "운영 유니버스 밖"
        elif not in_uni and ev["fail"] is None:
            ev["fail"] = "운영 유니버스 밖"
        rows.append({"ticker": t, "name": names.get(t), "sector": sectors.get(t, ""),
                     "close": px.get("close"), "high": px.get("high"), "rec": rec, **ev})
    cand = sorted((r for r in rows if r["ok"] and r["yield_pct"] is not None),
                  key=lambda r: (-r["yield_pct"], r["ticker"]))
    top = int(C.DIVIDEND_TOP)
    cap = max(1, int(top * float(C.DIVIDEND_SECTOR_SHARE) + 1e-9))
    picked, per = [], {}
    for r in cand:
        if len(picked) >= top:
            r["fail"] = "순위 밖"
            continue
        sec = r["sector"] or f"?{r['ticker']}"
        if per.get(sec, 0) >= cap:
            r["fail"] = "업종 한도(30%)"
            continue
        per[sec] = per.get(sec, 0) + 1
        picked.append(r)
    with closing(_conn(store)) as con:
        con.execute("DELETE FROM sig_dividend_data WHERE d=?", (d,))
        con.execute("DELETE FROM sig_dividend_list WHERE d=?", (d,))
        con.executemany(
            "INSERT INTO sig_dividend_data(d,ticker,name,sector,close,high250,fy,dps0,dps1,dps2,"
            "eps,op,yield_pct,payout_pct,ok,fail,stop) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(d, r["ticker"], r["name"], r["sector"], r["close"], r["high"],
              (r["rec"] or {}).get("fy"), *((r["rec"] or {}).get("dps") or [None, None, None])[:3],
              (r["rec"] or {}).get("eps"), (r["rec"] or {}).get("op"),
              r["yield_pct"], r["payout_pct"], 1 if r["ok"] else 0, r["fail"], r["stop"])
             for r in rows])
        con.executemany(
            "INSERT INTO sig_dividend_list(d,rank,ticker,name,yield_pct,sector) VALUES(?,?,?,?,?,?)",
            [(d, i, r["ticker"], r["name"], r["yield_pct"], r["sector"])
             for i, r in enumerate(picked, 1)])
        con.commit()
    info.update({"status": "ok", "passed": len(cand), "listed": len(picked)})
    return _save_status(store, d, info)


def _save_status(store, d: str, info: dict) -> dict:
    info = {**info, "at": _now()}
    with closing(_conn(store)) as con:
        con.execute("INSERT INTO sig_meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                    (META.format(d=d), json.dumps(info, ensure_ascii=False)))
        con.commit()
    return info


def block(store, d: str) -> dict:
    """추천 메시지의 【고배당 n】 구역. {"lines", "map"(종목→수익률 %), "info", "error"}.
    그날 자료가 없으면 "자료 없음" 한 줄과 error(관리자 알림용)를 돌려준다."""
    from .renderer import stock_label
    d = str(d)[:10]
    st = _status(store, d)
    if not st or st.get("status") != "ok":
        why = (st or {}).get("reason") or "그날 받은 자료가 없음"
        return {"lines": ["【고배당 0】", "자료 없음"], "map": {},
                "info": {"status": "no_data", "reason": why}, "error": f"자료 없음 — {why}"}
    with closing(_conn(store)) as con:
        today = [dict(r) for r in con.execute(
            "SELECT * FROM sig_dividend_list WHERE d=? ORDER BY rank", (d,)).fetchall()]
        pd_ = con.execute("SELECT MAX(d) FROM sig_dividend_list WHERE d<?", (d,)).fetchone()[0]
        prev = [dict(r) for r in con.execute(
            "SELECT * FROM sig_dividend_list WHERE d=? ORDER BY rank", (pd_,)).fetchall()] if pd_ else []
        data = {str(r["ticker"]): dict(r) for r in con.execute(
            "SELECT * FROM sig_dividend_data WHERE d=?", (d,)).fetchall()}
        recent = _recent_listed(con, d, int(C.DIVIDEND_STOP_DAYS))

    def lab(t, name=None):
        return stock_label(name or (data.get(t) or {}).get("name") or recent.get(t) or t, t)
    lines = [f"【고배당 {len(today)}】",
             " · ".join(f"{stock_label(r['name'], r['ticker'])} {r['yield_pct']:.1f}%" for r in today)
             or "없음"]
    now_set = {str(r["ticker"]) for r in today}
    info: dict = {"status": "ok", "tickers": [str(r["ticker"]) for r in today]}
    if pd_:
        prev_set = {str(r["ticker"]) for r in prev}
        new = [r for r in today if str(r["ticker"]) not in prev_set]
        gone = [r for r in prev if str(r["ticker"]) not in now_set]
        if new:
            lines.append("↳ 새로 들어옴: " + ", ".join(lab(str(r["ticker"]), r["name"]) for r in new))
            info["new"] = [str(r["ticker"]) for r in new]
        if gone:
            lines.append("↳ 빠짐: " + ", ".join(
                f"{lab(str(r['ticker']), r['name'])} "
                f"{(data.get(str(r['ticker'])) or {}).get('fail') or '운영 유니버스 밖'}" for r in gone))
            info["gone"] = [str(r["ticker"]) for r in gone]
    stops = [(t, data[t]["stop"]) for t in sorted(set(recent) | now_set)
             if t in data and data[t].get("stop")]
    if stops:
        lines.append("↳ ⚠그만 사는 신호: " + ", ".join(f"{lab(t)} {why}" for t, why in stops))
        info["stop"] = [t for t, _ in stops]
    return {"lines": lines, "map": {str(r["ticker"]): float(r["yield_pct"]) for r in today},
            "info": info, "error": None}
