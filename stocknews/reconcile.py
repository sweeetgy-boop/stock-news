# -*- coding: utf-8 -*-
"""시세 보정 — pykrx 잠정값을 KRX 오픈API 확정값으로 덮어쓴다.

왜 두 소스인가
--------------
KRX 오픈API 는 요청 2회·수 초로 전종목을 주지만 D일분이 **익영업일
08:00** 에야 열린다. 21:30 nightly 는 그때까지 기다릴 수 없으므로 여전히
pykrx 로 잠정 적재하고, 다음 날 아침 이 모듈이 KRX 값을 정본으로 확정한다.

2026-09-16 실측이 그 필요를 보였다. 종목별 폴백(pykrx → 네이버 차트)으로
적재된 9/14 · 9/15 는 종가가 KRX 와 각각 2,065/2,422 · 2,030/2,417종목
달랐다. 전종목 스냅샷으로 적재된 9/11 은 2,570종목 전부 일치했다.

하는 일 (시세만)
----------------
  - 같은 날짜의 잠정 행과 KRX 행을 필드별로 비교해 차이를 price_diffs 에
    남기고, 행 전체를 KRX 값으로 덮어쓴다 (source='krx_api', is_final=1).
  - 잠정 행이 없던 종목은 활성 마스터에 있을 때만 추가한다. 마스터 밖
    종목(우선주·스팩 등)까지 넣으면 날짜별 적재량이 부풀어 부분 적재
    판정(`load_floor`)의 기준이 흔들린다.
  - KRX 에 없는 종목의 잠정 행은 그대로 두고 로그만 남긴다.

scans · recos 는 건드리지 않는다. 채점 재계산은 이 모듈의 몫이 아니다.
"""
from __future__ import annotations

import logging
from collections import Counter

import pandas as pd

log = logging.getLogger(__name__)

__all__ = ["reconcile_day", "FIELDS", "FIELD_LABELS", "WARN_PCT"]

# (prices 컬럼, KRX 어댑터 컬럼)
FIELDS: tuple[tuple[str, str], ...] = (
    ("o", "시가"), ("h", "고가"), ("l", "저가"), ("c", "종가"),
    ("v", "거래량"), ("amt", "거래대금"),
)
FIELD_LABELS = {"o": "시가", "h": "고가", "l": "저가", "c": "종가",
                "v": "거래량", "amt": "거래대금"}

# 이 비율(%)을 넘는 차이는 알림에 경고로 올린다. 덮어쓰기는 똑같이 한다.
WARN_PCT = 5.0
# 값이 전부 정수(원·주)라 0.5 미만 차이는 부동소수 표현 차이다.
_TOL = 0.5


def _num(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f          # NaN -> None


def _valid_krx_row(r: pd.Series) -> bool:
    """적재 가능한 KRX 행인가. `Store.upsert_cross_section` 과 같은 규칙.

    거래량 0 은 거래정지다. 시·고·저가가 0 으로 온다(2026-09-15 115종목).
    """
    v = _num(r.get("거래량"))
    if v is None or v <= 0:
        return False
    return all((_num(r.get(c)) or 0) > 0 for c in ("시가", "고가", "저가", "종가"))


def reconcile_day(store, trade_date, krx: pd.DataFrame,
                  universe: set[str] | None = None) -> dict:
    """하루치 보정. 결과 요약 dict 를 돌려준다.

    trade_date : 'YYYY-MM-DD' 또는 'YYYYMMDD'
    krx        : `data.krx_daily_prices` 의 반환값 (None 아님)
    universe   : 추가를 허용할 종목. None 이면 KRX 유효 행 전부.

    반환 키
      compared        비교한 잠정 행 수
      diff_tickers    한 필드라도 다른 종목 수
      diff_fields     {필드: 건수}
      diff_records    price_diffs 에 남긴 행 수
      warnings        5% 초과 차이 목록 [(ticker, field, pykrx, krx, pct)]
      warn_fields     {필드: 건수} (5% 초과)
      added           새로 추가한 종목 수
      missing_in_krx  KRX 에 없어 그대로 둔 종목 목록
      halted_in_krx   KRX 에 있으나 거래량 0 이라 그대로 둔 종목 목록
      already_final   이미 확정이라 비교하지 않은 행 수
      amt_filled      잠정 거래대금이 비어 있어 KRX 로 채운 행 수
      written         덮어쓰기 + 추가 행 수
    """
    ds = str(trade_date)
    iso = ds if "-" in ds else f"{ds[:4]}-{ds[4:6]}-{ds[6:8]}"

    have = store.prices_on(iso)
    valid = krx[krx.apply(_valid_krx_row, axis=1)] if not krx.empty else krx

    rows: list[tuple] = []
    diffs: list[tuple] = []
    warnings: list[tuple] = []
    diff_fields: Counter = Counter()
    warn_fields: Counter = Counter()
    diff_tickers = compared = already_final = amt_filled = 0
    missing: list[str] = []
    halted: list[str] = []

    def _krx_tuple(t: str, k: pd.Series) -> tuple:
        return (t, *(_num(k.get(col)) for _, col in FIELDS))

    for t, p in have.iterrows():
        if int(p.get("is_final") or 0) == 1:
            already_final += 1
            continue
        if t not in valid.index:
            (halted if t in krx.index else missing).append(t)
            continue
        compared += 1
        k = valid.loc[t]
        differs = False
        for f, col in FIELDS:
            pv, kv = _num(p.get(f)), _num(k.get(col))
            if kv is None:
                continue
            if pv is None:
                # 종목별 폴백 경로는 거래대금을 주지 않는다. 채움은 차이가
                # 아니다 — 세면 매일 2,400건짜리 잡음이 된다.
                if f == "amt":
                    amt_filled += 1
                continue
            if abs(pv - kv) < _TOL:
                continue
            differs = True
            diff_fields[f] += 1
            diffs.append((t, f, pv, kv))
            pct = (kv - pv) / pv * 100.0 if pv else float("inf")
            if abs(pct) > WARN_PCT:
                warn_fields[f] += 1
                warnings.append((t, f, pv, kv, pct))
        diff_tickers += int(differs)
        rows.append(_krx_tuple(t, k))

    add = [t for t in valid.index
           if t not in have.index and (universe is None or t in universe)]
    for t in add:
        rows.append(_krx_tuple(t, valid.loc[t]))

    written = store.apply_final_prices(iso, rows, diffs) if rows else 0

    if missing:
        log.warning("%s KRX 에 없는 종목 %d개 — 잠정값 그대로 둠: %s", iso,
                    len(missing), ", ".join(missing[:20]))
    if halted:
        log.info("%s KRX 거래량 0(거래정지) 종목 %d개 — 잠정값 그대로 둠: %s",
                 iso, len(halted), ", ".join(halted[:20]))
    warnings.sort(key=lambda w: -abs(w[4]))
    log.info("%s 시세 보정: 비교 %d · 차이 %d종목 %s · 추가 %d · 확정 기존 %d "
             "· 거래대금 채움 %d · 5%% 초과 %d", iso, compared, diff_tickers,
             dict(diff_fields), len(add), already_final, amt_filled,
             len(warnings))
    return {
        "trade_date": iso,
        "compared": compared,
        "diff_tickers": diff_tickers,
        "diff_fields": dict(diff_fields),
        "diff_records": len(diffs),
        "warnings": warnings,
        "warn_fields": dict(warn_fields),
        "added": len(add),
        "missing_in_krx": missing,
        "halted_in_krx": halted,
        "already_final": already_final,
        "amt_filled": amt_filled,
        "written": written,
    }
