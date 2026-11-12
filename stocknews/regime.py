# -*- coding: utf-8 -*-
"""[C] 국면 라벨 — 거래일마다 시장 국면을 3단계로 판정해 기록한다.

**기록은 플래그와 무관하게 항상 한다.** 점수·등급·추천 선정은 이 값을
읽지 않는다. 표시(`REGIME_BANNER`)와 TREND 절반(`REGIME_TREND_CUT`, 실제
자르기는 B)만 플래그로 켠다.

판정 (그날 조건 = raw_label)
---------------------------
  상승 = KOSPI 종가 > KOSPI MA200   그리고  MA60 위 비율 ≥ 50%
  하락 = KOSPI 종가 < KOSPI MA200   그리고  MA60 위 비율 < 40%
  중립 = 그 외 (종가 = MA200 이면 중립)

  · KOSPI MA200 = 기준일 포함 지수 직전 200봉 종가 평균. 200봉 미만이면
    판정 불가(NULL, null_reason="지수 MA200 봉 부족").
  · 지수 신선도: 지수 히스토리에 **기준일 행이 있어야** 한다(= 기준일까지
    자른 지수의 마지막 날짜가 기준일). 없으면 fresh=0 이고 지수 칸
    (종가·MA200·괴리·20일 수익률)과 국면을 전부 NULL 로 둔다. 묵은 값을
    그날 값처럼 적지 않는다.
  · MA60 위 비율 = DB prices 에서, 기준일에 시세 행이 있고 그 종목 **자기
    봉** 기준 직전 60봉(기준일 포함)이 있는 종목 중 `종가 > 60봉 평균` 인
    종목의 비율(%). 60봉 미만 종목은 분모에서 뺀다. 비교는 `종가×60 >
    60봉 합` 으로 한다(원 단위 정수 가격이면 부동소수 오차 없음).
    판정 가능 종목이 `REGIME_MIN_BREADTH_N`(500) 미만이면 판정 불가
    (null_reason="MA60 판정 종목 부족") — 부분 적재일 방어.
    거래정지 종목은 그날 행이 없으므로 분모에서 빠진다. 수정주가 불연속
    (분할 등)은 보정하지 않는다 — 전체 2,000여 종목 중 소수라 비율에 주는
    영향이 작다.

전환 (확정 국면 = label)
-----------------------
  · 확정 국면과 다른 raw_label 이 **같은 값으로 5거래일 연속**이면 그
    5번째 날에 전환한다. 중간에 다른 값이 끼면 연속 카운트는 그 값으로
    새로 1부터 센다. 확정 국면과 같은 raw 가 나오면 대기 카운트는 0.
  · 초기 상태 = 확정 국면 없음(NULL). 첫 확정도 같은 규칙(같은 raw 5일
    연속)을 따른다. 그 전의 날은 label=NULL, null_reason="확정 전".
  · raw_label 이 NULL 인 날(지수 신선하지 않음·봉 부족): 그날 label 은
    NULL. 연속 카운트는 **0 으로 끊긴다**(그날은 조건을 '충족'하지
    않았으므로). 확정 국면 자체는 유지되어 다음 판정 가능일에 이어진다.
  · 상태 기계는 인과적이다 — d 의 label 은 d 까지의 raw 만 본다.
  · 달력(거래일) 위에서 센다. 기록이 빠진 거래일은 raw NULL 로 본다.

기록 경로
--------
  · 실시간: nightly 의 daily 단계(`run_daily`)가 스캔 기준일로
    `record_daily()` 를 부른다. 최근 `REGIME_GAP_FILL_DAYS` 거래일 중 빠진
    날도 같이 채운다. 실패해도 daily 를 죽이지 않는다(경고만).
    상태 기계는 테이블에 쌓인 raw 열 전체 위에서 돈다.
  · 소급: `--mode regime-backfill` (1회성, nightly 밖). 달력 전 기간을
    계산한다. 소급 행은 kind='소급'. 이미 있는 '실시간' 행은 그날 밤
    관측값(fresh·지수 칸·비율·raw)을 유지하고 label·pending 만 전 기간
    상태 기계 결과로 다시 쓴다(실시간 기록은 기록 시작일부터 상태 기계를
    돌려 '확정 전' 이 남을 수 있으므로). reco_regime 스냅샷은 바꾸지 않는다.
  · 추천 시점 국면: `reco_regime(d, rank, ticker, regime)`. recos 테이블은
    건드리지 않는다.
"""
from __future__ import annotations

import logging
import math
import sqlite3
import time
from contextlib import closing

import pandas as pd

from . import config as C

log = logging.getLogger(__name__)

__all__ = ["UP", "NEUTRAL", "DOWN", "classify", "confirm",
           "index_close_series", "index_metrics", "breadth_by_date",
           "raw_row", "record_daily", "backfill", "get_row", "get_label",
           "labels_map", "regime_rows", "periods", "transitions",
           "banner_line", "with_banner", "reco_regimes"]

UP, NEUTRAL, DOWN = "상승", "중립", "하락"

R_STALE = "지수 신선하지 않음"
R_MA = "지수 MA200 봉 부족"
R_BREADTH = "MA60 판정 종목 부족"
R_UNCONFIRMED = "확정 전"

KIND_BACKFILL = "소급"
KIND_LIVE = "실시간"

_COLS = ("d", "label", "raw_label", "pending_label", "pending_days",
         "kospi_close", "kospi_ma200", "ma200_gap_pct", "kospi_ret_20d",
         "above_ma60_pct", "n_ma60_universe", "fresh", "null_reason", "kind",
         "calc_version", "created")


def _ds(x) -> str:
    if isinstance(x, str):
        return x[:10]
    return pd.Timestamp(x).strftime("%Y-%m-%d")


def _ok(x) -> bool:
    return x is not None and not (isinstance(x, float) and math.isnan(x))


def _now() -> str:
    from .trading_day import now_kst
    return now_kst().strftime("%Y-%m-%d %H:%M:%S")


# ══════════════════════════ 판정 ══════════════════════════
def classify(close, ma200, above_pct,
             up_pct: float | None = None,
             down_pct: float | None = None) -> str | None:
    """그날 조건만으로 본 국면. 입력 하나라도 없으면 None."""
    if not (_ok(close) and _ok(ma200) and _ok(above_pct)):
        return None
    up = C.REGIME_UP_BREADTH_PCT if up_pct is None else up_pct
    dn = C.REGIME_DOWN_BREADTH_PCT if down_pct is None else down_pct
    if close > ma200 and above_pct >= up:
        return UP
    if close < ma200 and above_pct < dn:
        return DOWN
    return NEUTRAL


def confirm(raw: list, days: int | None = None, state: str | None = None
            ) -> list[dict]:
    """raw_label 열 → 날짜별 확정 국면. 규칙은 모듈 머리말 '전환'.

    반환 원소: label(그날 표시값 · raw NULL 이면 NULL), state(유지 중인
    확정 국면), pending_label, pending_days.
    """
    n = int(C.REGIME_CONFIRM_DAYS if days is None else days)
    out: list[dict] = []
    cand, k = None, 0
    for r in raw:
        if r is None:
            cand, k = None, 0
            out.append({"label": None, "state": state,
                        "pending_label": None, "pending_days": 0})
            continue
        if r == state:
            cand, k = None, 0
        else:
            if r == cand:
                k += 1
            else:
                cand, k = r, 1
            if k >= n:
                state, cand, k = r, None, 0
        out.append({"label": state, "state": state,
                    "pending_label": cand, "pending_days": k})
    return out


# ══════════════════════════ 지수 ══════════════════════════
def index_close_series(obj) -> pd.Series | None:
    """지수 일봉(DataFrame: Close/종가, 또는 Series) → 날짜 문자열 인덱스 종가."""
    if obj is None:
        return None
    if isinstance(obj, pd.DataFrame):
        col = next((c for c in ("Close", "종가", "close") if c in obj.columns),
                   None)
        if col is None:
            return None
        s = obj[col]
    else:
        s = obj
    if len(s) == 0:
        return None
    s = pd.Series(pd.to_numeric(s, errors="coerce").to_numpy(dtype="float64"),
                  index=[_ds(i) for i in s.index])
    s = s[~pd.Index(s.index).duplicated(keep="last")]
    s = s[s > 0].dropna().sort_index()
    return s if len(s) else None


def index_metrics(s: pd.Series | None, d: str,
                  ma: int | None = None, ret_bars: int | None = None) -> dict:
    """기준일 d 의 지수 칸. 지수에 d 행이 없으면 fresh=False, 전부 None."""
    ma = int(C.REGIME_MA_LONG if ma is None else ma)
    rb = int(C.REGIME_RET_BARS if ret_bars is None else ret_bars)
    d = _ds(d)
    out = {"fresh": False, "kospi_close": None, "kospi_ma200": None,
           "ma200_gap_pct": None, "kospi_ret_20d": None}
    if s is None or d not in s.index:
        return out
    sub = s[s.index <= d]
    close = float(sub.iloc[-1])
    out["fresh"] = True
    out["kospi_close"] = close
    if len(sub) >= ma:
        m = float(sub.iloc[-ma:].mean())
        out["kospi_ma200"] = m
        out["ma200_gap_pct"] = (close / m - 1.0) * 100.0
    if len(sub) >= rb + 1:
        a = float(sub.iloc[-(rb + 1)])
        if a > 0:
            out["kospi_ret_20d"] = (close / a - 1.0) * 100.0
    return out


# ══════════════════════════ MA60 위 비율 ══════════════════════════
def breadth_by_date(store, dates, since: str | None = None,
                    window: int | None = None,
                    tail_only: bool = False) -> dict[str, tuple]:
    """{d: (비율%, 위 종목 수, 판정 종목 수)}. 정의는 모듈 머리말.

    since     : 이 날짜 이후 시세만 읽는다. 그 창 안에 60봉이 없는 종목은
                빠지므로 소급값과 어긋날 수 있다(테스트용).
    tail_only : 종목마다 마지막 (60 + K)봉만 읽는다. K = 대상 첫날 이후
                ~ 마지막 날까지 시세 날짜 수. 대상 날짜의 '자기 60봉' 이
                전부 그 안에 들어가므로 전 기간 계산과 **정확히 같다.**
                실시간 경로(대상이 최근 며칠)의 속도용 — 전 기간 읽기
                16초 → 4초대(2026-11 실측, 114만 행).
    """
    w = int(C.REGIME_BREADTH_MA if window is None else window)
    want = sorted({_ds(x) for x in dates})
    if not want:
        return {}
    with closing(sqlite3.connect(store.path, timeout=30.0)) as con:
        if tail_only:
            k = con.execute(
                "SELECT COUNT(DISTINCT d) FROM prices WHERE d>? AND d<=?",
                (want[0], want[-1])).fetchone()[0]
            df = pd.read_sql_query(
                "SELECT ticker, d, c FROM (SELECT ticker, d, c, ROW_NUMBER() "
                "OVER (PARTITION BY ticker ORDER BY d DESC) AS rn "
                "FROM prices WHERE d<=? AND c>0) WHERE rn<=?",
                con, params=(want[-1], w + int(k or 0)))
        else:
            q = "SELECT ticker, d, c FROM prices WHERE d<=?"
            params: list = [want[-1]]
            if since:
                q += " AND d>=?"
                params.append(_ds(since))
            df = pd.read_sql_query(q, con, params=params)
    if df.empty:
        return {d: (None, 0, 0) for d in want}
    df = df[pd.to_numeric(df["c"], errors="coerce") > 0].copy()
    df["c"] = df["c"].astype("float64")
    df = df.sort_values(["ticker", "d"], kind="mergesort").reset_index(drop=True)
    g = df.groupby("ticker", sort=False)
    nth = g.cumcount()
    cs = g["c"].cumsum()
    lag = cs.groupby(df["ticker"], sort=False).shift(w).fillna(0.0)
    wsum = cs - lag
    sel = df["d"].isin(set(want)) & (nth >= w - 1)
    sub = pd.DataFrame({"d": df.loc[sel, "d"],
                        "above": (df.loc[sel, "c"] * w > wsum[sel])})
    agg = sub.groupby("d")["above"].agg(["sum", "count"])
    out = {}
    for d in want:
        if d in agg.index:
            a, n = int(agg.at[d, "sum"]), int(agg.at[d, "count"])
            out[d] = ((100.0 * a / n) if n else None, a, n)
        else:
            out[d] = (None, 0, 0)
    return out


def raw_row(d: str, kospi: pd.Series | None, breadth: dict,
            min_n: int | None = None) -> dict:
    """그날 조건 1행 (label 은 아직 없음)."""
    mn = int(C.REGIME_MIN_BREADTH_N if min_n is None else min_n)
    d = _ds(d)
    im = index_metrics(kospi, d)
    pct, _a, n = breadth.get(d, (None, 0, 0))
    row = {"d": d, "fresh": int(bool(im["fresh"])),
           "kospi_close": im["kospi_close"], "kospi_ma200": im["kospi_ma200"],
           "ma200_gap_pct": im["ma200_gap_pct"],
           "kospi_ret_20d": im["kospi_ret_20d"],
           "above_ma60_pct": pct, "n_ma60_universe": int(n or 0),
           "raw_label": None, "null_reason": None}
    if not im["fresh"]:
        row["null_reason"] = R_STALE
    elif im["kospi_ma200"] is None:
        row["null_reason"] = R_MA
    elif pct is None or int(n or 0) < mn:
        row["null_reason"] = R_BREADTH
    else:
        row["raw_label"] = classify(im["kospi_close"], im["kospi_ma200"], pct)
    return row


def _apply_states(seq: list[str], raws: dict[str, str | None]) -> dict[str, dict]:
    st = confirm([raws.get(d) for d in seq])
    return dict(zip(seq, st))


def _finish(row: dict, state: dict, kind: str, created: str) -> dict:
    r = dict(row)
    r["label"] = state["label"]
    r["pending_label"] = state["pending_label"]
    r["pending_days"] = int(state["pending_days"] or 0)
    if r["raw_label"] is not None and r["label"] is None:
        r["null_reason"] = R_UNCONFIRMED
    r["kind"] = kind
    r["calc_version"] = int(C.REGIME_CALC_VERSION)
    r["created"] = created
    return r


# ══════════════════════════ 저장·조회 ══════════════════════════
def _upsert(con, rows: list[dict]) -> None:
    if not rows:
        return
    con.executemany(
        f"INSERT OR REPLACE INTO market_regime({','.join(_COLS)}) "
        f"VALUES({','.join('?' * len(_COLS))})",
        [tuple(r.get(c) for c in _COLS) for r in rows])


def regime_rows(store) -> pd.DataFrame:
    """market_regime 전체 (d 오름차순). 테이블이 없으면 빈 프레임."""
    try:
        with closing(sqlite3.connect(store.path, timeout=30.0)) as con:
            return pd.read_sql_query(
                "SELECT * FROM market_regime ORDER BY d", con)
    except Exception:  # noqa: BLE001
        return pd.DataFrame(columns=list(_COLS))


def _stored(store) -> dict[str, dict]:
    df = regime_rows(store)
    if df.empty:
        return {}
    df = df.astype(object).where(pd.notna(df), None)
    return {str(r["d"]): r for r in df.to_dict("records")}


def get_row(store, d) -> dict | None:
    """v2_hooks.regime_row 의 본체. 없으면 None."""
    try:
        with closing(sqlite3.connect(store.path, timeout=30.0)) as con:
            con.row_factory = sqlite3.Row
            r = con.execute("SELECT * FROM market_regime WHERE d=?",
                            (_ds(d),)).fetchone()
    except Exception:  # noqa: BLE001 - 테이블 없음 등
        return None
    if r is None:
        return None
    out = dict(r)
    out["fresh"] = bool(out.get("fresh"))
    return out


def get_label(store, d) -> str | None:
    r = get_row(store, d)
    return None if r is None else r.get("label")


def labels_map(store) -> dict[str, str | None]:
    """{d: label} 전 기간. 여러 날을 한 번에 볼 때(재채점·백테스트) 쓴다."""
    df = regime_rows(store)
    if df.empty:
        return {}
    return {str(d): (None if pd.isna(lb) else str(lb))
            for d, lb in zip(df["d"], df["label"])}


def _fill_reco_regime(con, d: str, label: str | None, kind: str,
                      created: str, replace: bool) -> int:
    """그 날짜 recos 행마다 국면 스냅샷. recos 는 읽기만 한다."""
    if replace:
        con.execute("DELETE FROM reco_regime WHERE d=?", (d,))
    verb = "INSERT OR REPLACE" if replace else "INSERT OR IGNORE"
    cur = con.execute(
        f"{verb} INTO reco_regime(d, rank, ticker, regime, kind, created) "
        "SELECT d, rank, ticker, ?, ?, ? FROM recos WHERE d=?",
        (label, kind, created, d))
    return int(cur.rowcount or 0)


def reco_regimes(store, d=None) -> pd.DataFrame:
    """reco_regime 행 (d 지정 시 그날만)."""
    q = "SELECT * FROM reco_regime"
    params: tuple = ()
    if d is not None:
        q += " WHERE d=?"
        params = (_ds(d),)
    with closing(sqlite3.connect(store.path, timeout=30.0)) as con:
        return pd.read_sql_query(q + " ORDER BY d, rank", con, params=params)


# ══════════════════════════ 실시간 (daily 단계) ══════════════════════════
def _calendar(store) -> list[str]:
    from .events import trading_calendar
    return trading_calendar(store)


def record_daily(store, trade_date, fetch=None, min_n: int | None = None
                 ) -> dict | None:
    """스캔 기준일 국면 기록 + 그날 recos 의 국면 스냅샷.

    **예외를 올리지 않는다.** 실패하면 경고만 남기고 None — daily 의
    스캔·추천 기록은 이미 커밋돼 있다.

    fetch : 테스트용 주입구. (name, bars) -> DataFrame|None.
            기본은 `data.load_index` (FinanceDataReader).
    """
    if not trade_date:
        return None
    td = _ds(trade_date)
    try:
        cal = sorted({x for x in _calendar(store) if x <= td} | {td})
        pos = cal.index(td)
        stored = _stored(store)
        gap = int(C.REGIME_GAP_FILL_DAYS)
        targets = [x for x in cal[max(0, pos - gap):pos] if x not in stored]
        targets.append(td)

        bars = int(C.REGIME_MA_LONG) + gap + 120
        if fetch is None:
            from .data import load_index
            raw_idx = load_index(C.REGIME_INDEX, bars=bars)
        else:
            raw_idx = fetch(C.REGIME_INDEX, bars)
        kospi = index_close_series(raw_idx)
        # 달력 창으로 자르면 장기 거래정지 종목의 '자기 60봉' 이 창 밖으로
        # 밀려 소급값과 어긋난다(실측 2411 vs 2412). 종목별 마지막 N봉만
        # 읽는 tail_only 는 소급과 정확히 같다. 실시간과 소급이 같은 날 같은
        # 값을 내야 재채점이 맞는다.
        br = breadth_by_date(store, targets, tail_only=True)
        new = {x: raw_row(x, kospi, br, min_n=min_n) for x in targets}

        raws = {d: r.get("raw_label") for d, r in stored.items()}
        raws.update({d: r["raw_label"] for d, r in new.items()})
        first = min(raws)
        seq = sorted({x for x in cal if x >= first} | set(raws))
        seq = [x for x in seq if x <= td]
        states = _apply_states(seq, raws)
        created = _now()
        rows = [_finish(new[x], states[x], KIND_LIVE, created) for x in targets]
        with closing(sqlite3.connect(store.path, timeout=30.0)) as con:
            _upsert(con, rows)
            n_reco = _fill_reco_regime(con, td, rows[-1]["label"], KIND_LIVE,
                                       created, replace=True)
            con.commit()
    except Exception as exc:  # noqa: BLE001 - daily 를 죽이지 않는다
        log.warning("국면 기록 실패 — 건너뜁니다 (%s: %s)",
                    type(exc).__name__, exc)
        return None
    row = rows[-1]
    log.info("국면 기록 %s · %s (조건 %s · MA60 위 %s · fresh %d) · 보충 %d일 · "
             "추천 스냅샷 %d행", td, row["label"] or "NULL",
             row["raw_label"] or "NULL",
             "-" if row["above_ma60_pct"] is None
             else f"{row['above_ma60_pct']:.1f}%",
             row["fresh"], len(rows) - 1, n_reco)
    out = dict(row)
    out["filled"] = [r["d"] for r in rows[:-1]]
    out["reco_rows"] = n_reco
    return out


# ══════════════════════════ 소급 (1회성) ══════════════════════════
def backfill(store, start: str = "2024-01-01", cache_dir=None,
             index=None, min_n: int | None = None,
             allow_network: bool = True) -> dict:
    """달력 전 기간 국면 소급. '실시간' 행의 관측값은 덮어쓰지 않는다.

    index : 테스트용 주입구. KOSPI 일봉 DataFrame/Series. 없으면
            `events.load_index_history(start=, cache_dir=)`.
    """
    t0 = time.perf_counter()
    cal = _calendar(store)
    if not cal:
        return {"ok": False, "reason": "거래일 달력이 비었음", "rows": 0}
    if index is None:
        from .events import load_index_history
        hist = load_index_history(names=(C.REGIME_INDEX,), start=start,
                                  cache_dir=cache_dir,
                                  allow_network=allow_network)
        index = hist.get(str(C.REGIME_INDEX).upper())
    kospi = index_close_series(index)
    t_idx = time.perf_counter()
    br = breadth_by_date(store, cal)
    t_br = time.perf_counter()

    stored = _stored(store)
    live = {d: r for d, r in stored.items() if r.get("kind") == KIND_LIVE}
    new = {d: raw_row(d, kospi, br, min_n=min_n) for d in cal if d not in live}
    raws = {d: r.get("raw_label") for d, r in live.items()}
    raws.update({d: r["raw_label"] for d, r in new.items()})
    seq = sorted(set(cal) | set(raws))
    states = _apply_states(seq, raws)
    created = _now()
    rows = [_finish(new[d], states[d], KIND_BACKFILL, created)
            for d in seq if d in new]
    # '실시간' 행: 그날 밤 관측값(fresh·지수 칸·비율·raw)은 그대로 두고,
    # 확정 국면(label·pending)만 전 기간 상태 기계로 다시 쓴다. 실시간
    # 기록은 기록이 시작된 날부터 상태 기계를 돌리므로 소급 전에는 '확정
    # 전' 으로 남아 있을 수 있다. 추천 시점 스냅샷(reco_regime)은 그대로다.
    for d, r in live.items():
        st = states[d]
        r2 = {c: r.get(c) for c in _COLS}
        r2["label"] = st["label"]
        r2["pending_label"] = st["pending_label"]
        r2["pending_days"] = int(st["pending_days"] or 0)
        if r2["raw_label"] is not None:
            r2["null_reason"] = None if st["label"] is not None else R_UNCONFIRMED
        rows.append(r2)
    labels = {d: states[d]["label"] for d in seq}
    n_reco = 0
    with closing(sqlite3.connect(store.path, timeout=30.0)) as con:
        _upsert(con, rows)
        for (d,) in con.execute("SELECT DISTINCT d FROM recos").fetchall():
            if d in labels:
                n_reco += _fill_reco_regime(con, d, labels[d], KIND_BACKFILL,
                                            created, replace=False)
        con.commit()
    t_end = time.perf_counter()
    allrows = regime_rows(store)
    return {"ok": True, "rows": len(rows) - len(live), "kept_live": len(live),
            "calendar": [cal[0], cal[-1], len(cal)],
            "index_range": (None if kospi is None
                            else [kospi.index[0], kospi.index[-1], len(kospi)]),
            "reco_rows": n_reco,
            "elapsed_sec": round(t_end - t0, 2),
            "elapsed_index_sec": round(t_idx - t0, 2),
            "elapsed_breadth_sec": round(t_br - t_idx, 2),
            "counts": summarize(allrows)}


# ══════════════════════════ 요약 ══════════════════════════
def summarize(df: pd.DataFrame) -> dict:
    """국면별 거래일 수 + NULL 사유별 수."""
    if df is None or df.empty:
        return {}
    lab = df["label"].where(pd.notna(df["label"]), None)
    out = {k: int((lab == k).sum()) for k in (UP, NEUTRAL, DOWN)}
    out["NULL"] = int(lab.isna().sum())
    rs = df.loc[lab.isna(), "null_reason"].fillna("?")
    out["NULL_by_reason"] = {str(k): int(v) for k, v in rs.value_counts().items()}
    return out


def periods(df: pd.DataFrame) -> list[dict]:
    """label 이 같은 연속 구간 [{start, end, label, days}] (NULL 도 구간)."""
    out: list[dict] = []
    if df is None or df.empty:
        return out
    for d, lb in zip(df["d"], df["label"]):
        lb = None if pd.isna(lb) else str(lb)
        if out and out[-1]["label"] == lb:
            out[-1]["end"] = str(d)
            out[-1]["days"] += 1
        else:
            out.append({"start": str(d), "end": str(d), "label": lb, "days": 1})
    return out


def transitions(df: pd.DataFrame) -> list[dict]:
    """확정 국면 전환 날짜. NULL 날은 건너뛰고 직전 확정 국면과 비교한다."""
    out: list[dict] = []
    prev = None
    if df is None or df.empty:
        return out
    for d, lb in zip(df["d"], df["label"]):
        if pd.isna(lb):
            continue
        if lb != prev:
            out.append({"d": str(d), "from": prev, "to": str(lb)})
            prev = lb
    return out


# ══════════════════════════ 배너 ══════════════════════════
def _f(x, fmt: str) -> str:
    return "-" if not _ok(x) else format(float(x), fmt)


def banner_line(row: dict | None, d: str | None = None) -> str:
    """브리핑 맨 위 한 줄. 매수·매도 지시 문구 없음."""
    dd = (row or {}).get("d") or d
    when = f" ({str(dd)[5:10]})" if dd else ""
    if row is None:
        return f"📊 국면{when} — 기록 없음"
    if not row.get("fresh"):
        return f"📊 국면{when} — 지수 데이터 없음"
    nums = (f"KOSPI {_f(row.get('kospi_close'), ',.2f')} · "
            f"MA200 대비 {_f(row.get('ma200_gap_pct'), '+.1f')}% · "
            f"20일 {_f(row.get('kospi_ret_20d'), '+.1f')}%")
    lb = row.get("label")
    if lb == DOWN:
        # 2026-11-12 사용자 지시: 하락 국면은 해석 문구 없이 수치만.
        return f"📊 국면 하락{when} · {nums}"
    if lb == UP:
        return (f"📊 국면 상승{when} · {nums} — 상승 구간 — 절대수익보다 "
                "시장 대비 초과수익으로 볼 것")
    if lb == NEUTRAL:
        return f"📊 국면 중립{when} · {nums}"
    return f"📊 국면 미확정{when} · {nums}"


def with_banner(store, text: str, d=None) -> str:
    """REGIME_BANNER 가 켜져 있으면 맨 위에 국면 한 줄. 꺼져 있으면 그대로."""
    if not getattr(C, "REGIME_BANNER", False):
        return text
    try:
        dd = _ds(d) if d is not None else store.last_price_date()
        line = banner_line(get_row(store, dd) if dd else None, dd)
    except Exception as exc:  # noqa: BLE001 - 배너 때문에 브리핑이 죽지 않게
        log.warning("국면 배너 생략 (%s: %s)", type(exc).__name__, exc)
        return text
    return f"{line}\n{text}"
