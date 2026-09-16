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

수정주가 방어 (두 겹)
--------------------
pykrx 종목별 조회는 수정주가(분할·증자 소급 반영)를 주고 KRX 오픈API 는
원시 가격을 준다. 수정주가 행을 원시 가격으로 덮으면 권리락 지점에 가짜
급락·급등이 생긴다. 그래서

  1) `RECONCILE_MIN_DATE` 이전(백필 구간)은 실행을 거부한다.
  2) 날짜와 무관하게, 차이 종목 중 수정주가 모양(가격 비율 x 거래량 비율
     = 1)이 `RECONCILE_ADJ_SUSPECT_RATIO` 를 넘으면 **쓰기 전에** 중단한다.

둘 다 우회 옵션이 없다. 거부는 `ReconcileRefused` 로 올린다.
"""
from __future__ import annotations

import logging
from collections import Counter

import pandas as pd

from .config import RECONCILE_ADJ_SUSPECT_RATIO, RECONCILE_MIN_DATE

log = logging.getLogger(__name__)

__all__ = ["reconcile_day", "FIELDS", "FIELD_LABELS", "WARN_PCT",
           "ReconcileRefused", "date_refusal", "adjusted_price_like"]

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

# 수정주가 모양 판정 허용오차.
#   가격 비율이 1 에서 이만큼은 벗어나야 후보다. 9/14·9/15 처럼 종가만
#   0.7% 다르고 거래량이 같은 행은 곱이 0.993 이라 역수와 헷갈린다.
ADJ_MIN_PRICE_MOVE = 0.01
#   가격 비율 x 거래량 비율 이 1 에서 이 안이면 역수로 본다. 실측 곱은
#   1.0000 전후다(247540 1.016 x 0.98426). 원·주 반올림 오차가 들어온다.
ADJ_PRODUCT_TOL = 0.005
#   분할·증자 조정은 시·고·저·종가에 같은 비율로 걸린다.
ADJ_FIELD_TOL = 0.01


class ReconcileRefused(RuntimeError):
    """보정 거부. DB 에는 아무것도 쓰지 않은 상태다.

    code   : "before_min_date" | "adjusted_price_suspect"
    detail : SUMMARY 에 실을 근거
    """

    def __init__(self, code: str, reason: str, detail: dict | None = None):
        super().__init__(reason)
        self.code = code
        self.reason = reason
        self.detail = detail or {}


def _iso(trade_date) -> str:
    ds = str(trade_date)
    return ds if "-" in ds else f"{ds[:4]}-{ds[4:6]}-{ds[6:8]}"


def date_refusal(trade_date) -> str | None:
    """보정 금지 날짜면 사유 문장, 아니면 None."""
    iso = _iso(trade_date)
    if iso < RECONCILE_MIN_DATE:
        return (f"{iso} 는 보정 금지 구간입니다 (RECONCILE_MIN_DATE "
                f"{RECONCILE_MIN_DATE} 이전 = 백필 수정주가). KRX 원시 가격으로 "
                "덮으면 권리락 지점에 가짜 급락·급등이 생깁니다.")
    return None


def adjusted_price_like(p, k) -> tuple[float, float] | None:
    """잠정 행 p(o h l c v) 와 KRX 행 k(시가…거래량) 가 수정주가 관계인가.

    맞으면 (가격 비율, 거래량 비율), 아니면 None. 비율은 KRX / 잠정.
    """
    pc, kc = _num(p.get("c")), _num(k.get("종가"))
    pv, kv = _num(p.get("v")), _num(k.get("거래량"))
    if not (pc and kc and pv and kv) or min(pc, kc, pv, kv) <= 0:
        return None
    rp, rv = kc / pc, kv / pv
    if (abs(rp - 1.0) < ADJ_MIN_PRICE_MOVE
            or abs(rp * rv - 1.0) > ADJ_PRODUCT_TOL):
        return None
    for f, col in (("o", "시가"), ("h", "고가"), ("l", "저가")):
        px, kx = _num(p.get(f)), _num(k.get(col))
        if px and kx and px > 0 and abs(kx / px - rp) > ADJ_FIELD_TOL * rp:
            return None
    return rp, rv


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
      adjusted_suspects  수정주가 모양 종목 수 (기준 이하라 진행한 경우)
      written         덮어쓰기 + 추가 행 수

    거부 (`ReconcileRefused`, DB 에 쓰지 않음)
      before_min_date         RECONCILE_MIN_DATE 이전
      adjusted_price_suspect  수정주가 모양 비율 > RECONCILE_ADJ_SUSPECT_RATIO
    """
    iso = _iso(trade_date)
    why = date_refusal(iso)
    if why:
        raise ReconcileRefused("before_min_date", why,
                               {"min_date": RECONCILE_MIN_DATE})

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
    suspects: list[tuple] = []     # (ticker, 가격 비율, 거래량 비율)

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
        if differs:
            adj = adjusted_price_like(p, k)
            if adj:
                suspects.append((t, *adj))
        rows.append(_krx_tuple(t, k))

    add = [t for t in valid.index
           if t not in have.index and (universe is None or t in universe)]
    for t in add:
        rows.append(_krx_tuple(t, valid.loc[t]))

    ratio = len(suspects) / diff_tickers if diff_tickers else 0.0
    if suspects:
        log.warning("%s 수정주가 모양 %d/%d종목 (%.1f%%): %s", iso,
                    len(suspects), diff_tickers, ratio * 100,
                    ", ".join(f"{t} 가격x{rp:.4f} 거래량x{rv:.4f}"
                              for t, rp, rv in suspects[:5]))
    if ratio > RECONCILE_ADJ_SUSPECT_RATIO:
        raise ReconcileRefused(
            "adjusted_price_suspect",
            f"{iso} 차이 종목 {diff_tickers}개 중 {len(suspects)}개"
            f"({ratio * 100:.1f}%)가 수정주가 모양(가격 비율 x 거래량 비율 = 1)"
            f"입니다. 기준 {RECONCILE_ADJ_SUSPECT_RATIO * 100:.0f}% 초과 — "
            "잠정 행이 수정주가로 적재된 것으로 보고 보정하지 않습니다.",
            {"diff_tickers": diff_tickers, "adjusted_suspects": len(suspects),
             "adjusted_ratio": round(ratio, 4),
             "adjusted_examples": [[t, round(rp, 4), round(rv, 4)]
                                   for t, rp, rv in suspects[:10]]})

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
        "adjusted_suspects": len(suspects),
        "written": written,
    }
