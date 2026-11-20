# -*- coding: utf-8 -*-
"""[C] 지수 확정값 — 직전 N거래일 지수를 KRX 공식 값으로 다시 받아 확정한다.

왜 필요한가
-----------
nightly 의 지수(KOSPI·KOSDAQ)는 FinanceDataReader 가 주는데, FDR 0.9.202 의
KS11/KQ11 은 KRX 직접 조회가 아니라 GitHub 캐시 CSV 다. 21:30~22:30 회차에
그 캐시는 ① 갱신되지 않았거나(고정값 — `data.load_index(asof=)` 신선도
가드가 막는다) ② 잠정값이 올라와 있다가 나중에 정정된다(2026-09-01·
11-05·11-09(KOSDAQ)·11-10 — 날짜가 맞아 가드로 못 막는다). 그래서 그날 밤
기록은 **잠정**이고, 공식 값으로 다시 확인해야 확정이다.

무엇을 하는가 (daily 단계 안, 국면 기록 전)
-----------------------------------------
  1. 거래일 달력에서 기준일 **직전** `INDEX_CONFIRM_DAYS`(5) 거래일을 고른다.
     KRX 오픈API 는 D일분을 익영업일 08:00 이후 주므로 당일은 받지 않는다.
  2. 날짜마다 `idx/kospi_dd_trd`·`idx/kosdaq_dd_trd` 를 불러 IDX_NM 이
     '코스피'·'코스닥' 인 행의 CLSPRC_IDX 를 공식 종가로 쓴다(10회/일).
  3. FDR 히스토리에 공식 값(이번에 받은 것 + 예전에 확정해 market_context
     에 남은 것)을 덮어쓴 시계열로 market_context 행을 다시 계산하고
     is_final=1 · index_source='KRX' 로 표시한다. 그날 행이 없으면 만든다.
  4. 같은 시계열로 market_regime 의 그 날짜 행을 다시 판정한다
     (`regime.apply_confirmed`). 잠정 판정과 달라지면 WARNING 한 줄.
  5. 당일 market_context 행은 is_final=0 · index_source='FDR잠정'.
     당일 market_regime 행은 뒤이은 `record_daily` 가 'FDR잠정' 으로 쓴다.
     그때 지수는 `IndexConfirm.fetch` — 공식 값을 덮어쓴 FDR 프레임이다.

생략 조건 (갱신하지 않고 경고만 — daily 종료 코드 무관)
  · KRX_API_KEY 없음 (status NOKEY)
  · 호출 실패 1건이라도 (status ERROR — 부분 갱신하지 않는다)
  · 그 날짜가 빈 응답(아직 미제공) / 한 지수만 있음 → 그 날짜만 잠정 유지
  · 공식 값 시계열에 달력상 빈 거래일이 있음(FDR 캐시가 오래 묵음) →
    MA200·수익률을 믿을 수 없어 그 날짜 생략

과거 틀린 행 일괄 재기록은 하지 않는다(사용자 승인 대기 —
tools/v2_a_fix_market_context.py). 매 nightly 직전 5거래일만이다.
추천 시점 국면 스냅샷(reco_regime)과 recos 는 건드리지 않는다.
"""
from __future__ import annotations

import logging
import math
import os
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass, field
from typing import Callable

import pandas as pd

from . import config as C

log = logging.getLogger(__name__)

__all__ = ["IndexConfirm", "confirm_recent", "fetch_official",
           "stored_official", "overlay_frame", "mark_provisional",
           "SRC_KRX", "SRC_PROV"]

SRC_KRX = "KRX"
SRC_PROV = "FDR잠정"

_MKT = ("kospi_close", "kospi_ma200", "kospi_above_ma200", "kospi_ret_5d",
        "kospi_ret_20d", "kosdaq_close", "kosdaq_ret_5d")


def _now() -> str:
    from .trading_day import now_kst
    return now_kst().strftime("%Y-%m-%d %H:%M:%S")


def _ds(x) -> str:
    if isinstance(x, str):
        return x[:10]
    return pd.Timestamp(x).strftime("%Y-%m-%d")


def _num(x) -> float | None:
    try:
        v = float(str(x).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) and v > 0 else None


# ══════════════════════════ KRX 호출 ══════════════════════════
def _http_get(service: str, bas_dd: str, key: str) -> list[dict]:
    """서비스 1회 호출 → OutBlock_1 행 목록. 실패는 예외 (data._krx_get 원칙)."""
    import requests
    res = requests.get(C.INDEX_CONFIRM_API_BASE + service,
                       params={"basDd": bas_dd}, headers={"AUTH_KEY": key},
                       timeout=float(C.INDEX_CONFIRM_TIMEOUT_SEC))
    if res.status_code != 200:
        # 본문에 키 값은 없다 (401 "Unauthorized Key" 등)
        raise RuntimeError(f"HTTP {res.status_code}: {res.text[:120]}")
    rows = res.json().get("OutBlock_1")
    if not isinstance(rows, list):
        raise ValueError(f"OutBlock_1 없음: {res.text[:120]}")
    return rows


def fetch_official(dates, key: str | None = None,
                   http_get: Callable | None = None) -> tuple[dict, dict]:
    """날짜별 KOSPI·KOSDAQ 공식 종가.

    반환 (vals, rep)
      vals = {d: {"KOSPI": 종가, "KOSDAQ": 종가}}  — 두 지수가 다 있는 날만
      rep  = {"status": OK|NOKEY|ERROR, "calls": n,
              "by_date": {d: DATA|EMPTY|PARTIAL}, "error": ...}
    http_get : 테스트용 주입구. (service, basDd, key) -> 행 목록 (실패는 예외).
    """
    from .data import KRX_API_KEY_ENV
    key = key or os.getenv(KRX_API_KEY_ENV, "").strip()
    rep: dict = {"status": None, "calls": 0, "by_date": {}}
    if not key:
        rep["status"] = "NOKEY"
        return {}, rep
    get = http_get or _http_get
    vals: dict[str, dict] = {}
    for d in dates:
        d = _ds(d)
        got: dict[str, float | None] = {}
        for name, (svc, nm) in C.INDEX_CONFIRM_SERVICES.items():
            try:
                rows = get(svc, d.replace("-", ""), key)
            except Exception as exc:  # noqa: BLE001
                rep.update({"status": "ERROR", "error":
                            f"{svc} {d}: {type(exc).__name__}: {exc}"[:200]})
                return {}, rep
            finally:
                rep["calls"] += 1
            row = next((r for r in rows or [] if r.get("IDX_NM") == nm), None)
            got[name] = None if row is None else _num(row.get("CLSPRC_IDX"))
        have = [v is not None for v in got.values()]
        if all(have):
            vals[d] = got
            rep["by_date"][d] = "DATA"
        elif any(have):
            rep["by_date"][d] = "PARTIAL"
        else:
            rep["by_date"][d] = "EMPTY"
    rep["status"] = "OK"
    return vals, rep


# ══════════════════════════ 저장값 ══════════════════════════
def stored_official(store) -> dict[str, dict[str, float]]:
    """market_context 에 KRX 확정으로 남은 종가 {"KOSPI": {d: v}, "KOSDAQ": {...}}."""
    out: dict[str, dict[str, float]] = {"KOSPI": {}, "KOSDAQ": {}}
    try:
        with closing(sqlite3.connect(store.path, timeout=30.0)) as con:
            rows = con.execute(
                "SELECT scan_date, kospi_close, kosdaq_close FROM market_context "
                "WHERE is_final=1 AND index_source=?", (SRC_KRX,)).fetchall()
    except sqlite3.Error:
        return out
    for d, ks, kq in rows:
        if _num(ks) is not None:
            out["KOSPI"][str(d)] = float(ks)
        if _num(kq) is not None:
            out["KOSDAQ"][str(d)] = float(kq)
    return out


def _mkt_row(store, d: str) -> dict | None:
    with closing(sqlite3.connect(store.path, timeout=30.0)) as con:
        con.row_factory = sqlite3.Row
        r = con.execute("SELECT * FROM market_context WHERE scan_date=?",
                        (d,)).fetchone()
    return None if r is None else dict(r)


def mark_provisional(store, d: str) -> int:
    """그날 market_context 행을 잠정으로 표시(이미 확정이면 그대로)."""
    with closing(sqlite3.connect(store.path, timeout=30.0)) as con:
        cur = con.execute(
            "UPDATE market_context SET is_final=0, index_source=?, "
            "confirmed_at=NULL WHERE scan_date=? AND COALESCE(is_final,0)=0 "
            "AND (index_source IS NULL OR index_source<>?)",
            (SRC_PROV, _ds(d), SRC_PROV))
        con.commit()
        return int(cur.rowcount or 0)


def _upsert_final(store, row: dict, confirmed_at: str) -> None:
    cols = ("scan_date",) + _MKT
    sets = ",".join(f"{c}=excluded.{c}" for c in _MKT)
    with closing(sqlite3.connect(store.path, timeout=30.0)) as con:
        con.execute(
            f"INSERT INTO market_context({','.join(cols)},created_at,"
            f"is_final,index_source,confirmed_at) "
            f"VALUES({','.join('?' * len(cols))},?,1,?,?) "
            f"ON CONFLICT(scan_date) DO UPDATE SET {sets},is_final=1,"
            f"index_source=excluded.index_source,"
            f"confirmed_at=excluded.confirmed_at",
            (*[row.get(c) for c in cols], confirmed_at, SRC_KRX,
             confirmed_at))
        con.commit()


def _same(a, b, tol: float = 1e-9) -> bool:
    na = a is None or (isinstance(a, float) and math.isnan(a))
    nb = b is None or (isinstance(b, float) and math.isnan(b))
    if na or nb:
        return na and nb
    return abs(float(a) - float(b)) <= tol


# ══════════════════════════ 시계열 ══════════════════════════
def overlay_frame(df, official: dict | None):
    """FDR 지수 프레임의 Close 에 공식 값을 덮어쓴다(없는 날짜는 행 추가).

    `attrs["krx_confirmed"]` 에 덮어쓴 날짜를 남긴다 — `regime.record_daily`
    가 그 날짜를 보충할 때 확정(KRX)으로 표시한다. df 가 None 이면 None.
    """
    if df is None or len(df) == 0 or not official or "Close" not in df.columns:
        return df
    out = df.copy()
    idx = pd.DatetimeIndex(out.index)
    if idx.tz is not None:
        idx = idx.tz_localize(None)
    out.index = idx
    for d, v in official.items():
        if _num(v) is not None:
            out.loc[pd.Timestamp(_ds(d)), "Close"] = float(v)
    out = out.sort_index()
    out.attrs["krx_confirmed"] = sorted(_ds(d) for d in official)
    return out


def _gap(s: pd.Series | None, cal: list[str], d: str, bars: int) -> list[str]:
    """d 까지 달력 마지막 `bars` 거래일 중 시계열에 없는 날짜."""
    if s is None:
        return [d]
    win = [x for x in cal if x <= d][-int(bars):]
    have = set(s.index)
    return [x for x in win if x not in have]


# ══════════════════════════ 본체 ══════════════════════════
@dataclass
class IndexConfirm:
    """confirm_recent 결과. `fetch` 는 record_daily 에 넘기는 지수 조회."""
    summary: dict
    official: dict = field(default_factory=dict)   # {"KOSPI": {d: v}, ...}
    base_fetch: Callable | None = None

    def fetch(self, name: str, bars: int):
        """FDR 프레임 + 공식 값. 확정이 없으면 base_fetch 결과 그대로."""
        df = self.base_fetch(name, bars) if self.base_fetch else None
        ov = self.official.get(str(name).upper())
        return overlay_frame(df, ov) if ov else df


def _base(fetch):
    if fetch is not None:
        return fetch
    from .data import load_index
    return lambda name, bars: load_index(name, bars=bars)


def confirm_recent(store, trade_date: str, fetch=None, http_get=None,
                   key: str | None = None, days: int | None = None,
                   min_n: int | None = None) -> IndexConfirm:
    """직전 N거래일 지수 확정. **예외를 올리지 않는다**(daily 를 죽이지 않음).

    fetch    : (name, bars) -> 지수 DataFrame. 기본 `data.load_index`.
               daily 는 record_daily 와 같은 조회를 넘긴다.
    http_get : KRX 호출 주입구 (테스트).
    """
    base = _base(fetch)
    td = _ds(trade_date) if trade_date else None
    summ: dict = {"status": None, "targets": [], "confirmed": [],
                  "pending": [], "skipped": {}, "context_updated": 0,
                  "regime_updated": 0, "regime_changed": [],
                  "provisional": None, "calls": 0}
    ic = IndexConfirm(summ, {}, base)
    if not td:
        summ["status"] = "NO_DATE"
        return ic
    t0 = time.perf_counter()
    try:
        if mark_provisional(store, td):
            summ["provisional"] = td
        n = int(C.INDEX_CONFIRM_DAYS if days is None else days)
        from .events import trading_calendar
        cal = trading_calendar(store)
        targets = [x for x in cal if x < td][-n:] if n > 0 else []
        summ["targets"] = targets
        if not targets:
            summ["status"] = "NO_TARGETS"
            return ic
        vals, rep = fetch_official(targets, key=key, http_get=http_get)
        summ["calls"] = rep["calls"]
        summ["status"] = rep["status"]
        if rep["status"] == "NOKEY":
            log.warning("지수 확정 생략 — KRX_API_KEY 미설정 (직전 %d거래일은 "
                        "잠정 유지)", len(targets))
            return ic
        if rep["status"] == "ERROR":
            log.warning("지수 확정 생략 — KRX 지수 조회 실패 (%s)",
                        rep.get("error"))
            summ["error"] = rep.get("error")
            return ic
        summ["pending"] = [d for d, st in rep["by_date"].items()
                           if st != "DATA"]
        for d in summ["pending"]:
            summ["skipped"][d] = rep["by_date"][d]
        if summ["pending"]:
            log.warning("지수 확정 보류 %s — KRX 응답 %s (잠정 유지)",
                        ",".join(summ["pending"]),
                        ",".join(rep["by_date"][d] for d in summ["pending"]))
        if not vals:
            return ic
        _apply(store, cal, td, vals, base, ic, min_n)
    except Exception as exc:  # noqa: BLE001 - daily 를 죽이지 않는다
        log.warning("지수 확정 실패 — 건너뜁니다 (%s: %s)",
                    type(exc).__name__, exc)
        summ["status"] = "EXCEPTION"
        summ["error"] = f"{type(exc).__name__}: {exc}"[:200]
        ic.official = {}
    finally:
        summ["elapsed_sec"] = round(time.perf_counter() - t0, 2)
        if not ic.official and summ["status"] != "EXCEPTION":
            # 오늘 확정이 없어도 예전에 확정한 공식 값은 국면 기록에 쓴다.
            try:
                ic.official = {k: v for k, v in stored_official(store).items()
                               if v}
            except Exception:  # noqa: BLE001
                ic.official = {}
    return ic


def _apply(store, cal, td, vals, base, ic: IndexConfirm, min_n) -> None:
    from .daily import market_context
    from . import regime as R
    summ = ic.summary
    official = stored_official(store)
    for d, v in vals.items():
        for name in ("KOSPI", "KOSDAQ"):
            official[name][d] = float(v[name])
    bars = int(C.REGIME_MA_LONG) + int(C.REGIME_GAP_FILL_DAYS) + 120
    ks_df = overlay_frame(base("KOSPI", bars), official["KOSPI"])
    kq_df = overlay_frame(base("KOSDAQ", bars), official["KOSDAQ"])
    ks = R.index_close_series(ks_df)
    kq = R.index_close_series(kq_df)
    need_ks = max(int(C.REGIME_MA_LONG), int(C.REGIME_RET_BARS) + 1)
    ok_dates: list[str] = []
    for d in sorted(vals):
        miss = _gap(ks, cal, d, need_ks) + _gap(kq, cal, d, 6)
        if miss:
            summ["skipped"][d] = f"지수 히스토리 공백 {len(miss)}일"
            log.warning("지수 확정 생략 %s — 공식 값 시계열에 빈 거래일 %d일 "
                        "(예: %s) · MA200·수익률을 믿을 수 없음",
                        d, len(miss), miss[0])
            continue
        ok_dates.append(d)
    now = _now()
    for d in ok_dates:
        row = market_context(ks_df, kq_df, d)
        if row is None:
            summ["skipped"][d] = "KOSPI 종가 없음"
            continue
        old = _mkt_row(store, d)
        if old is not None and old.get("is_final") == 1 and \
                old.get("index_source") == SRC_KRX and \
                all(_same(old.get(c), row.get(c)) for c in _MKT):
            summ["confirmed"].append(d)       # 이미 같은 값으로 확정
            continue
        tol = float(C.INDEX_CONFIRM_TOL)
        if old is not None:
            diff = [f"{lab} {old.get(c) or 0:,.2f}→{row[c]:,.2f}"
                    for c, lab in (("kospi_close", "KOSPI"),
                                   ("kosdaq_close", "KOSDAQ"))
                    if row.get(c) is not None and
                    not _same(old.get(c), row.get(c), tol)]
            if diff:
                log.info("지수 확정 %s · 잠정값과 다름: %s", d, " · ".join(diff))
        _upsert_final(store, row, now)
        summ["context_updated"] += 1
        summ["confirmed"].append(d)
    # 국면: 공식 값 시계열로 그 날짜들 재판정. 이후 record_daily 도 같은
    # 공식 값 시계열(ic.fetch)을 쓴다.
    confirmed = list(summ["confirmed"])
    keep = stored_official(store)
    for d in confirmed:
        for name in ("KOSPI", "KOSDAQ"):
            keep[name][d] = official[name][d]
    ic.official = keep
    if confirmed:
        rep = R.apply_confirmed(store, ks, confirmed, upto=td,
                                confirmed_at=now, min_n=min_n)
        summ["regime_updated"] = rep["updated"]
        summ["regime_changed"] = [
            {"d": c["d"], "raw": c["raw"], "label": c["label"],
             "kospi_close": c["kospi_close"], "confirmed": c.get("confirmed")}
            for c in rep["changed"]]
    log.info("지수 확정 %d일 (%s) · market_context 갱신 %d · 국면 갱신 %d · "
             "국면 변경 %d · 보류 %d", len(confirmed),
             ",".join(confirmed) or "-", summ["context_updated"],
             summ["regime_updated"], len(summ["regime_changed"]),
             len(summ["skipped"]))
