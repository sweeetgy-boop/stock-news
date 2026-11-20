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

nightly 의 **첫 단계**(`reconcile`)가 전 거래일분을 확정한다(2026-11 R3-4).
21:30 이면 전 거래일분은 그날 08:00 에 이미 열려 있다.

하는 일 (시세만)
----------------
  - 같은 날짜의 잠정 행과 KRX 행을 필드별로 비교한다.
  - 잠정 행은 `PRICE_CONFIRM_FIELDS`(고가·저가·종가·거래량)만 KRX 값으로
    덮고 확정으로 표시한다 (source='krx_api', is_final=1). 시가는 실측상
    전부 일치해서, 거래대금은 종목별 경로가 주지 않아서 건드리지 않는다.
  - 덮기 직전의 잠정 행 전체를 `price_provisional` 에, 달랐던 필드를
    `price_diffs` 에 남긴다(같은 트랜잭션).
  - 잠정 행이 없던 종목은 활성 마스터에 있을 때만 추가한다. 마스터 밖
    종목(우선주·스팩 등)까지 넣으면 날짜별 적재량이 부풀어 부분 적재
    판정(`load_floor`)의 기준이 흔들린다.
  - KRX 에 없는 종목의 잠정 행은 그대로 두고 로그만 남긴다.

scans · recos 는 건드리지 않는다. 채점 재계산은 이 모듈의 몫이 아니다.
채점([0] events.PricePanel.from_store)은 prices 를 읽으므로 보정된 날부터
확정값을 쓴다.

수정주가 방어 (세 겹)
--------------------
pykrx 종목별 조회는 수정주가(분할·증자 소급 반영)를 주고 KRX 오픈API 는
원시 가격을 준다. 수정주가 행을 원시 가격으로 덮으면 권리락 지점에 가짜
급락·급등이 생긴다. 그래서

  1) `RECONCILE_MIN_DATE` 이전(백필 구간)은 실행을 거부한다.
  2) 날짜와 무관하게, 차이 종목 중 수정주가 모양(가격 비율 x 거래량 비율
     = 1)이 `RECONCILE_ADJ_SUSPECT_RATIO` 를 넘으면 **쓰기 전에** 중단한다.
  3) 그 이하면 분할 흔적이 있는 **종목·날짜만** 건너뛰고 나머지는 덮는다
     (`split_trace`: 수정주가 모양 또는 ±PRICE_JUMP_BREAK_PCT 불연속).

우회 옵션이 없다. 거부는 `ReconcileRefused` 로 올린다.
"""
from __future__ import annotations

import logging
from collections import Counter

import pandas as pd

from .config import (PRICE_CONFIRM_FIELDS, PRICE_CONFIRM_NEIGHBOR_DAYS,
                     PRICE_JUMP_BREAK_PCT, RECONCILE_ADJ_SUSPECT_RATIO,
                     RECONCILE_MIN_DATE)

log = logging.getLogger(__name__)

__all__ = ["reconcile_day", "FIELDS", "FIELD_LABELS", "WARN_PCT",
           "ReconcileRefused", "date_refusal", "adjusted_price_like",
           "split_trace"]

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


def _prev_closes(store, iso: str,
                 days: int = PRICE_CONFIRM_NEIGHBOR_DAYS) -> dict:
    """종목별 직전 종가 {ticker: c} (그날 이전 `days` 달력일 안).

    바로 앞 거래일 단면만 보지 않는다. 분할·병합은 보통 며칠 거래정지 뒤
    재개되므로 그 종목 자기 봉 기준 직전 종가를 쓴다.
    """
    import sqlite3
    from contextlib import closing
    from datetime import date, timedelta

    lo = (date.fromisoformat(iso) - timedelta(days=int(days))).isoformat()
    with closing(sqlite3.connect(store.path, timeout=30.0)) as con:
        df = pd.read_sql_query(
            "SELECT ticker,d,c FROM prices WHERE d>=? AND d<? AND c>0",
            con, params=(lo, iso))
    if df.empty:
        return {}
    return df.sort_values(["ticker", "d"]).groupby("ticker")["c"].last().to_dict()


def _break(a, b) -> bool:
    """a -> b 변동이 가격제한폭 밖인가 ([0] PricePanel 과 같은 기준)."""
    a, b = _num(a), _num(b)
    if not a or not b or a <= 0 or b <= 0:
        return False
    return abs(b / a - 1.0) > PRICE_JUMP_BREAK_PCT / 100.0 + 1e-6


def split_trace(p, k, prev_c=None) -> str | None:
    """종목·날짜 단위 분할 흔적. 있으면 사유 코드, 없으면 None.

      adjusted  잠정 행이 KRX 와 수정주가 관계 (가격 비율 x 거래량 비율 = 1)
      jump      그 종목 직전 종가 -> 그날 **KRX** 종가가 ±PRICE_JUMP_BREAK_PCT
                밖 (가격제한폭 밖 = 원시 가격의 불연속: 분할·병합·감자 등)

    흔적이 있는 행은 덮지 않는다. KRX 원시 가격과 DB 의 수정주가가 섞인
    구간에서 한쪽만 고치면 불연속을 새로 만들거나 옮길 수 있다.
    잠정 종가만 튀고 KRX 는 제한폭 안이면 흔적이 아니라 잠정값 오류다 —
    덮는다(가짜 불연속이 사라진다). 2026-09-15 미투온 +36% 가 그 예다.
    다음 날 종가는 보지 않는다 — nightly 첫 단계 시점엔 그날 적재 전이라
    없고, 그날 행은 그날 밤 받은 값이라 다음 날 분할과 기준이 같다.
    """
    if adjusted_price_like(p, k):
        return "adjusted"
    if _break(prev_c, _num(k.get("종가"))):
        return "jump"
    return None


def _apply(store, iso: str, updates: list[tuple], adds: list[tuple],
           diffs: list[tuple], fields: tuple[str, ...]) -> tuple[int, int]:
    """보정 쓰기 한 트랜잭션: 잠정 행 보존 -> 차이 기록 -> 덮어쓰기 -> 추가.

    updates : (ticker, *fields 값)  잠정 행이 있는 종목. fields 만 덮는다.
    adds    : (ticker, o, h, l, c, v, amt)  잠정 행이 없던 종목. 행 전체.
    diffs   : (ticker, field, 잠정값, KRX값)

    반환은 (확정 + 추가 행 수, 보존한 잠정 행 수).

    나눠 커밋하면 중간에 죽었을 때 값은 확정됐는데 원래 값이 사라진 상태가
    남는다. 확정 행은 다음 보정이 비교하지 않으므로 그 값은 복구되지 않는다.
    """
    import sqlite3
    from contextlib import closing

    from .trading_day import now_kst
    now = now_kst().isoformat(timespec="seconds")
    sets = ",".join(f"{f}=COALESCE(?,{f})" for f in fields)
    with closing(sqlite3.connect(store.path, timeout=30.0)) as con:
        cur = con.executemany(
            "INSERT OR IGNORE INTO price_provisional"
            "(ticker,d,o,h,l,c,v,amt,source,saved) "
            "SELECT ticker,d,o,h,l,c,v,amt,source,? FROM prices "
            "WHERE ticker=? AND d=? AND is_final=0",
            [(now, u[0], iso) for u in updates])
        saved = max(cur.rowcount or 0, 0)
        con.executemany(
            "INSERT INTO price_diffs(ticker,d,field,pykrx_val,krx_val,created) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(ticker,d,field) DO UPDATE SET "
            "pykrx_val=excluded.pykrx_val,krx_val=excluded.krx_val,"
            "created=excluded.created",
            [(t, iso, f, pv, kv, now) for t, f, pv, kv in diffs])
        cur = con.executemany(
            f"UPDATE prices SET {sets},source='krx_api',is_final=1 "
            "WHERE ticker=? AND d=? AND is_final=0",
            [(*u[1:], u[0], iso) for u in updates])
        n = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        cur = con.executemany(
            "INSERT INTO prices(ticker,d,o,h,l,c,v,amt,source,is_final) "
            "VALUES(?,?,?,?,?,?,?,?,'krx_api',1) "
            "ON CONFLICT(ticker,d) DO NOTHING",
            [(t, iso, o, h, l, c, v, amt) for t, o, h, l, c, v, amt in adds])
        n += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        con.commit()
    return n, saved


def reconcile_day(store, trade_date, krx: pd.DataFrame,
                  universe: set[str] | None = None) -> dict:
    """하루치 보정. 결과 요약 dict 를 돌려준다.

    trade_date : 'YYYY-MM-DD' 또는 'YYYYMMDD'
    krx        : `data.krx_daily_prices` 의 반환값 (None 아님)
    universe   : 추가를 허용할 종목. None 이면 KRX 유효 행 전부.

    잠정 행은 `PRICE_CONFIRM_FIELDS`(고가·저가·종가·거래량)만 KRX 값으로
    덮는다. 시가·거래대금은 그대로 둔다(차이가 있으면 untouched_diff 로 센다).

    반환 키
      compared        비교한 잠정 행 수
      diff_tickers    보정 필드가 하나라도 다른 종목 수 (건너뛴 종목 포함)
      diff_fields     {필드: 건수} (보정 필드만, 건너뛴 종목 포함)
      diff_records    price_diffs 에 남긴 행 수 (실제로 덮은 것만)
      warnings        5% 초과 차이 목록 [(ticker, field, 잠정, krx, pct)]
      warn_fields     {필드: 건수} (5% 초과)
      added           새로 추가한 종목 수
      missing_in_krx  KRX 에 없어 그대로 둔 종목 목록
      halted_in_krx   KRX 에 있으나 거래량 0 이라 그대로 둔 종목 목록
      already_final   이미 확정이라 비교하지 않은 행 수
      amt_missing     잠정 거래대금이 비어 있는 행 수 (채우지 않음)
      untouched_diff  {필드: 건수} 보정 대상이 아닌 필드(시가·거래대금)의 차이
      adjusted_suspects  수정주가 모양 종목 수 (기준 이하라 진행한 경우)
      skipped_split   분할 흔적이라 덮지 않은 [(ticker, 사유)] (adjusted/jump)
      confirmed       확정으로 바꾼 잠정 행 수
      provisional_saved  price_provisional 에 보존한 잠정 행 수
      fields          덮은 필드 목록
      written         확정 + 추가 행 수

    거부 (`ReconcileRefused`, DB 에 쓰지 않음)
      before_min_date         RECONCILE_MIN_DATE 이전
      adjusted_price_suspect  수정주가 모양 비율 > RECONCILE_ADJ_SUSPECT_RATIO
    """
    iso = _iso(trade_date)
    why = date_refusal(iso)
    if why:
        raise ReconcileRefused("before_min_date", why,
                               {"min_date": RECONCILE_MIN_DATE})

    fields = tuple(PRICE_CONFIRM_FIELDS)
    col_of = dict(FIELDS)
    have = store.prices_on(iso)
    valid = krx[krx.apply(_valid_krx_row, axis=1)] if not krx.empty else krx
    prev_c = _prev_closes(store, iso) if not have.empty else {}

    updates: list[tuple] = []
    diffs: list[tuple] = []
    warnings: list[tuple] = []
    diff_fields: Counter = Counter()
    warn_fields: Counter = Counter()
    untouched: Counter = Counter()
    diff_tickers = differ_any = compared = already_final = amt_missing = 0
    missing: list[str] = []
    halted: list[str] = []
    suspects: list[tuple] = []     # (ticker, 가격 비율, 거래량 비율)
    skipped: list[tuple] = []      # (ticker, 사유)

    for t, p in have.iterrows():
        if int(p.get("is_final") or 0) == 1:
            already_final += 1
            continue
        if t not in valid.index:
            (halted if t in krx.index else missing).append(t)
            continue
        compared += 1
        k = valid.loc[t]
        row_diffs: list[tuple] = []
        row_warns: list[tuple] = []
        other = False
        for f, col in FIELDS:
            pv, kv = _num(p.get(f)), _num(k.get(col))
            if kv is None:
                continue
            if pv is None:
                # 종목별 폴백 경로는 거래대금을 주지 않는다. 채우지 않는다
                # (PRICE_CONFIRM_FIELDS 주석) — 차이로도 세지 않는다.
                if f == "amt":
                    amt_missing += 1
                continue
            if abs(pv - kv) < _TOL:
                continue
            if f not in fields:
                untouched[f] += 1
                other = True
                continue
            row_diffs.append((t, f, pv, kv))
            pct = (kv - pv) / pv * 100.0 if pv else float("inf")
            if abs(pct) > WARN_PCT:
                row_warns.append((t, f, pv, kv, pct))
        if row_diffs:
            diff_tickers += 1
            for _, f, _, _ in row_diffs:
                diff_fields[f] += 1
            for w in row_warns:
                warn_fields[w[1]] += 1
            warnings.extend(row_warns)
        if row_diffs or other:
            differ_any += 1
            adj = adjusted_price_like(p, k)
            if adj:
                suspects.append((t, *adj))
        trace = split_trace(p, k, prev_c.get(t))
        if trace:
            skipped.append((t, trace))
            continue
        diffs.extend(row_diffs)
        updates.append((t, *(_num(k.get(col_of[f])) for f in fields)))

    add = [t for t in valid.index
           if t not in have.index and (universe is None or t in universe)]
    adds = [(t, *(_num(valid.loc[t].get(col)) for _, col in FIELDS))
            for t in add]

    ratio = len(suspects) / differ_any if differ_any else 0.0
    if suspects:
        log.warning("%s 수정주가 모양 %d/%d종목 (%.1f%%): %s", iso,
                    len(suspects), differ_any, ratio * 100,
                    ", ".join(f"{t} 가격x{rp:.4f} 거래량x{rv:.4f}"
                              for t, rp, rv in suspects[:5]))
    if ratio > RECONCILE_ADJ_SUSPECT_RATIO:
        raise ReconcileRefused(
            "adjusted_price_suspect",
            f"{iso} 차이 종목 {differ_any}개 중 {len(suspects)}개"
            f"({ratio * 100:.1f}%)가 수정주가 모양(가격 비율 x 거래량 비율 = 1)"
            f"입니다. 기준 {RECONCILE_ADJ_SUSPECT_RATIO * 100:.0f}% 초과 — "
            "잠정 행이 수정주가로 적재된 것으로 보고 보정하지 않습니다.",
            {"diff_tickers": differ_any, "adjusted_suspects": len(suspects),
             "adjusted_ratio": round(ratio, 4),
             "adjusted_examples": [[t, round(rp, 4), round(rv, 4)]
                                   for t, rp, rv in suspects[:10]]})

    written, saved = (_apply(store, iso, updates, adds, diffs, fields)
                      if (updates or adds) else (0, 0))

    if missing:
        log.warning("%s KRX 에 없는 종목 %d개 — 잠정값 그대로 둠: %s", iso,
                    len(missing), ", ".join(missing[:20]))
    if halted:
        log.info("%s KRX 거래량 0(거래정지) 종목 %d개 — 잠정값 그대로 둠: %s",
                 iso, len(halted), ", ".join(halted[:20]))
    if skipped:
        log.warning("%s 분할 흔적 %d종목 — 잠정값 그대로 둠: %s", iso,
                    len(skipped), ", ".join(f"{t}({r})"
                                            for t, r in skipped[:20]))
    warnings.sort(key=lambda w: -abs(w[4]))
    log.info("%s 시세 보정: 비교 %d · 차이 %d종목 %s · 확정 %d · 추가 %d · "
             "건너뜀 %d · 확정 기존 %d · 보정 밖 차이 %s · 5%% 초과 %d", iso,
             compared, diff_tickers, dict(diff_fields), len(updates),
             len(add), len(skipped), already_final, dict(untouched),
             len(warnings))
    return {
        "trade_date": iso,
        "fields": list(fields),
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
        "amt_missing": amt_missing,
        "untouched_diff": dict(untouched),
        "adjusted_suspects": len(suspects),
        "skipped_split": skipped,
        "confirmed": len(updates),
        "provisional_saved": saved,
        "written": written,
    }
