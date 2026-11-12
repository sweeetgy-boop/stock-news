# -*- coding: utf-8 -*-
"""[H] 지표 기록 — 기록 전용. 점수·추천·발송에 쓰지 않는다.

`indicator_log(d, ticker, name, value, calc_version, kind, created)` 에 전
종목의 지표를 날짜별로 남긴다. 나중에 '실시간' 구간 성적으로 채택 여부를
판단하기 위한 표본이다. 지표 정의·파라미터·판정 칸은 docs/지표등록.md 가
정본이고, **그 문서에 없는 지표는 점수에 반영하지 않는다.**

지표 (이름은 I 묶음과의 계약 — 바꾸지 말 것)
  rs20 / rs60        종목 N거래일 수익률 − 소속 시장 지수 N거래일 수익률 (%p)
  rs20_pct / rs60_pct  그날 rs 값이 있는 전 종목 중 백분위 (0~100, 높을수록 강함)
  dist_52w_high      52주(250거래일) 최고가 아래로 몇 % (0 = 신고가, 음수 없음)
  mfi14              Money Flow Index(14) (0~100)
  atr14_pct          ATR(14, 단순평균 TR) ÷ 종가 × 100
  bb_width20         볼린저(20, 2σ 모표준편차) (상단−하단) ÷ 중심선 × 100
  kalman_slope       로그 종가 로컬 선형 추세 칼만 필터의 기울기 (%/일)

계산 규칙 (전 지표 공통)
  · 거래일 달력 = `events.trading_calendar()` (온전히 적재된 날짜). 창은 달력
    위치로 센다. 창 안에 그 종목 행이 하나라도 없으면(거래정지) NULL.
    52주 고가만 예외 — 있는 행의 최고가를 쓰고, 대신 그 종목의 DB 첫 행이
    250거래일 전 이전이어야 한다(상장 250거래일 미만 NULL).
  · **그 날짜까지의 데이터만 쓴다.** 모든 창은 t 에서 끝난다. 백분위는 같은
    날짜의 단면만 본다. 칼만 필터도 매 날짜 직전 60거래일로 새로 돌린다 —
    그래서 소급 값과 실시간 값이 같은 데이터에서 정확히 같다.
  · 이상치: 창 안에 종가 일간 변동이 ±30%(`PRICE_JUMP_BREAK_PCT`, +1e-6 여유,
    events.PricePanel 과 같은 기준)를 넘는 날이 있으면 NULL. DB 가격이
    수정주가(백필)와 원시 가격(일일 적재)의 혼재라, 분할·병합 흔적이 낀
    구간을 '수정주가로 다시 계산'할 수 없어서 버린다.
  · RS: 지수가 그 날짜(와 N일 전 날짜)에 없으면 NULL — 실시간 경로에서는
    지수 마지막 날짜 ≠ 기준일이면 그날 RS 전부 NULL 이다(신선도).

쓰기
  · INSERT OR IGNORE. 같은 (d, ticker, name, calc_version) 이 이미 있으면 그대로
    둔다. 실시간 행을 소급이 덮지 않고, 이전 계산버전 행도 남는다.
  · created: 실시간 행은 기록 시각(KST), 소급 행은 NULL(크기 절약 — 시각은
    runs 테이블의 indicators-backfill 이력에 있다).
  · 행은 그 날짜에 시세 행이 있는 종목마다 9개. 값이 없으면 value=NULL 로 남겨
    '계산했는데 규칙상 없음'과 '기록이 없음(파이프라인 공백)'을 가른다.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from contextlib import closing

import numpy as np
import pandas as pd

from . import config as C

log = logging.getLogger(__name__)

__all__ = ["NAMES", "KIND_BACKFILL", "KIND_LIVE", "enabled", "versions",
           "kalman_slope_last", "kalman_weights", "compute_wide",
           "load_wide", "write_rows", "record_live", "maybe_record_daily",
           "backfill", "high52_level", "read_values"]

NAMES = ("rs20", "rs60", "rs20_pct", "rs60_pct", "dist_52w_high",
         "mfi14", "atr14_pct", "bb_width20", "kalman_slope")
KIND_BACKFILL = "소급"
KIND_LIVE = "실시간"


def enabled() -> bool:
    """`config.INDICATOR_LOG`. 호출 시점에 읽는다(테스트가 바꿀 수 있게)."""
    return bool(getattr(C, "INDICATOR_LOG", False))


def versions() -> dict[str, int]:
    v = dict(getattr(C, "INDICATOR_VERSIONS", {}))
    missing = [n for n in NAMES if n not in v]
    if missing:
        raise ValueError(f"INDICATOR_VERSIONS 에 없는 지표: {missing}")
    return {n: int(v[n]) for n in NAMES}


def _now_kst() -> str:
    from .notify import now_kst
    return now_kst().strftime("%Y-%m-%d %H:%M:%S")


# ══════════════════════════ 칼만 필터 ══════════════════════════
def _kalman_params():
    return (float(C.IND_KALMAN_OBS_VAR), float(C.IND_KALMAN_LEVEL_VAR),
            float(C.IND_KALMAN_SLOPE_VAR), float(C.IND_KALMAN_P0_SLOPE))


def kalman_slope_last(y: np.ndarray, params=None) -> np.ndarray:
    """로컬 선형 추세 칼만 필터를 끝까지 돌린 마지막 기울기.

    y : (n,) 또는 (n, m) — 열마다 독립 계열(로그 종가). NaN 금지.
    모형  y_t = μ_t + ε (R) · μ_t = μ_{t-1} + β_{t-1} + η (Qμ) · β_t = β_{t-1} + ζ (Qβ)
    초기  μ_0 = y_0, β_0 = 0, P_0 = diag(R, P0_slope)

    게인은 데이터와 무관하다(결측이 없으면). 그래서 결과는 y 의 고정 선형
    결합이고, `kalman_weights()` 로 같은 값을 빠르게 낸다 — smoke 가 둘의
    일치를 검사한다.
    """
    R, qL, qB, p0B = params or _kalman_params()
    Y = np.asarray(y, dtype="float64")
    one = Y.ndim == 1
    if one:
        Y = Y[:, None]
    n = Y.shape[0]
    lvl = Y[0].copy()
    slp = np.zeros(Y.shape[1])
    P = np.array([[R, 0.0], [0.0, p0B]])
    for t in range(1, n):
        # 예측
        lvl = lvl + slp
        P = np.array([[P[0, 0] + 2 * P[0, 1] + P[1, 1] + qL, P[0, 1] + P[1, 1]],
                      [P[0, 1] + P[1, 1], P[1, 1] + qB]])
        # 갱신
        S = P[0, 0] + R
        k0, k1 = P[0, 0] / S, P[1, 0] / S
        innov = Y[t] - lvl
        lvl = lvl + k0 * innov
        slp = slp + k1 * innov
        P = np.array([[P[0, 0] - k0 * P[0, 0], P[0, 1] - k0 * P[0, 1]],
                      [P[1, 0] - k1 * P[0, 0], P[1, 1] - k1 * P[0, 1]]])
    return slp[0] if one else slp


_W_CACHE: dict = {}


def kalman_weights(n: int, params=None) -> np.ndarray:
    """마지막 기울기 = weights · y (길이 n). 단위 충격으로 필터를 돌려 얻는다."""
    p = tuple(params or _kalman_params())
    key = (int(n), p)
    if key not in _W_CACHE:
        _W_CACHE[key] = kalman_slope_last(np.eye(int(n)), p)
    return _W_CACHE[key]


# ══════════════════════════ 계산 (wide) ══════════════════════════
def _roll_sum(a: np.ndarray, w: int) -> np.ndarray:
    """축 0 으로 길이 w 이동합. 앞 w-1 행과 NaN 이 낀 창은 NaN.

    누적합의 차(cs[t]-cs[t-w])로 구하지 않는다 — 전 기간 누적합이 커지면
    빼기에서 자릿수가 날아가 0 이어야 할 합이 ±1e-6 이 되고, MFI 가
    100.00000000000001 이 된다(사본 실측). 창마다 w 개를 직접 더한다.
    """
    n = a.shape[0]
    out = np.full(a.shape, np.nan)
    if n < w:
        return out
    acc = np.zeros((n - w + 1,) + a.shape[1:])
    for k in range(w):
        acc += a[k:n - w + 1 + k]          # NaN 이 하나라도 있으면 NaN
    out[w - 1:] = acc
    return out


def _lag(a: np.ndarray, k: int) -> np.ndarray:
    out = np.full(a.shape, np.nan)
    if k < a.shape[0]:
        out[k:] = a[:-k] if k else a
    return out


def _break_cs(c: np.ndarray) -> np.ndarray:
    """점프 누적표 (T+1, N). 점프 = 직전 유효 종가 대비 ±30% 초과.

    events.PricePanel 과 같은 기준(`PRICE_JUMP_BREAK_PCT` + 1e-6 여유 —
    정확히 +30% 상한가는 실제 거래라 점프가 아니다). 거래정지로 빈 날은
    직전 종가를 이어 붙여, 재개일의 변동을 점프 판정에 넣는다.
    """
    T, N = c.shape
    cf = pd.DataFrame(c).ffill().to_numpy()
    with np.errstate(invalid="ignore", divide="ignore"):
        ratio = cf / _lag(cf, 1)
    lim = float(C.PRICE_JUMP_BREAK_PCT) / 100.0 + 1e-6
    with np.errstate(invalid="ignore"):
        brk = np.nan_to_num((np.abs(ratio - 1.0) > lim).astype("float64"))
    return np.vstack([np.zeros((1, N)), np.cumsum(brk, axis=0)])


def _clean(brk_cs: np.ndarray, w: int) -> np.ndarray:
    """t-w+1..t 창 안의 전일 대비 변동(w-1개: t-w+2..t)에 점프가 없는가."""
    T = brk_cs.shape[0] - 1
    out = np.zeros((T, brk_cs.shape[1]), dtype=bool)
    if T >= w:
        out[w - 1:] = (brk_cs[w:] - brk_cs[1:T - w + 2]) == 0
    return out


def compute_wide(c: np.ndarray, h: np.ndarray, l: np.ndarray, v: np.ndarray,
                 idx_close: np.ndarray | None = None) -> dict[str, np.ndarray]:
    """날짜 × 종목 행렬 → 지표별 행렬. 모든 값은 그 행(날짜)까지의 데이터만 쓴다.

    c, h, l, v : (T, N) 달력 정렬. 행이 없으면 NaN.
    idx_close  : (T, N) 각 종목 소속 시장 지수 종가(달력 정렬). 없으면 NaN.
    """
    c = np.asarray(c, dtype="float64")
    h = np.asarray(h, dtype="float64")
    l = np.asarray(l, dtype="float64")
    v = np.asarray(v, dtype="float64")
    T, N = c.shape
    present = np.isfinite(c) & (c > 0)
    c = np.where(present, c, np.nan)
    pres_f = present.astype("float64")
    pres_cs = np.vstack([np.zeros((1, N)), np.cumsum(pres_f, axis=0)])

    def full(w: int) -> np.ndarray:
        """t 에서 끝나는 길이 w 창에 행이 전부 있는가."""
        out = np.zeros((T, N), dtype=bool)
        if T >= w:
            out[w - 1:] = (pres_cs[w:] - pres_cs[:-w]) == w
        return out

    brk_cs = _break_cs(c)

    def clean(w: int) -> np.ndarray:
        return _clean(brk_cs, w)

    res: dict[str, np.ndarray] = {}

    # ── RS ──
    ic = (np.full((T, N), np.nan) if idx_close is None
          else np.asarray(idx_close, dtype="float64"))
    ic = np.where(np.isfinite(ic) & (ic > 0), ic, np.nan)
    for n in C.IND_RS_WINDOWS:
        w = n + 1
        with np.errstate(invalid="ignore", divide="ignore"):
            rs = (c / _lag(c, n) - ic / _lag(ic, n)) * 100.0
        ok = full(w) & clean(w) & np.isfinite(rs)
        rs = np.where(ok, rs, np.nan)
        res[f"rs{n}"] = rs
        res[f"rs{n}_pct"] = _pct_rank(rs)

    # ── 52주 고가 거리 ──
    res["dist_52w_high"], _ = _high52(c, h, present, clean)

    # ── MFI(14) ──
    w = int(C.IND_MFI_WINDOW)
    tp = (h + l + c) / 3.0
    mf = tp * v
    dtp = tp - _lag(tp, 1)
    pos = np.where(dtp > 0, mf, 0.0)
    neg = np.where(dtp < 0, mf, 0.0)
    pos = np.where(np.isfinite(dtp), pos, np.nan)
    neg = np.where(np.isfinite(dtp), neg, np.nan)
    ps, ns = _roll_sum(pos, w), _roll_sum(neg, w)
    with np.errstate(invalid="ignore", divide="ignore"):
        mfi = 100.0 * (ps / (ps + ns))     # (100·P)/P 는 100.00000000000001 이 될 수 있다
    ok = full(w + 1) & clean(w + 1) & np.isfinite(mfi) & ((ps + ns) > 0)
    res["mfi14"] = np.where(ok, mfi, np.nan)

    # ── ATR(14) / 종가 ──
    w = int(C.IND_ATR_WINDOW)
    pc = _lag(c, 1)
    tr = np.fmax(h - l, np.fmax(np.abs(h - pc), np.abs(l - pc)))
    tr = np.where(np.isfinite(pc) & np.isfinite(h) & np.isfinite(l), tr, np.nan)
    with np.errstate(invalid="ignore", divide="ignore"):
        atrp = _roll_sum(tr, w) / w / c * 100.0
    ok = full(w + 1) & clean(w + 1) & np.isfinite(atrp)
    res["atr14_pct"] = np.where(ok, atrp, np.nan)

    # ── 볼린저 밴드폭(20, 2σ) / 중심선 ──
    w = int(C.IND_BB_WINDOW)
    mid = _roll_sum(c, w) / w
    dev = np.full((T, N), np.nan)              # 두 번 지나는 모분산 (ddof=0)
    if T >= w:
        acc = np.zeros((T - w + 1, N))
        for k in range(w):
            acc += (c[k:T - w + 1 + k] - mid[w - 1:]) ** 2
        dev[w - 1:] = acc / w
    with np.errstate(invalid="ignore", divide="ignore"):
        bbw = 2.0 * float(C.IND_BB_K) * np.sqrt(dev) / mid * 100.0
    ok = full(w) & clean(w) & np.isfinite(bbw)
    res["bb_width20"] = np.where(ok, bbw, np.nan)

    # ── 칼만 기울기 (로그 종가, 직전 60거래일로 매번 새로) ──
    w = int(C.IND_KALMAN_WINDOW)
    ks = np.full((T, N), np.nan)
    if T >= w:
        wt = kalman_weights(w)
        with np.errstate(invalid="ignore", divide="ignore"):
            y = np.log(c)
        acc = np.zeros((T - w + 1, N))
        for k in range(w):
            acc += wt[k] * y[k:T - w + 1 + k]
        ks[w - 1:] = acc * 100.0
    ok = full(w) & clean(w) & np.isfinite(ks)
    res["kalman_slope"] = np.where(ok, ks, np.nan)

    for k, a in res.items():          # 그 날짜에 행이 없는 종목은 기록 대상이 아니다
        res[k] = np.where(present, a, np.nan)
    return res


def _pct_rank(a: np.ndarray) -> np.ndarray:
    """행(날짜)마다 값이 있는 종목 중 백분위 (rank-1)/(n-1)×100. 동률은 평균 순위."""
    df = pd.DataFrame(a)
    r = df.rank(axis=1, method="average")
    n = df.notna().sum(axis=1).to_numpy(dtype="float64")[:, None]
    with np.errstate(invalid="ignore", divide="ignore"):
        out = (r.to_numpy() - 1.0) / (n - 1.0) * 100.0
    return np.where(n >= 2, out, np.nan)


def _high52(c, h, present, clean_fn):
    """(거리 %, 52주 고가) 행렬. 고가 = 창 안 max(고가, 종가)."""
    T, N = c.shape
    w = int(C.IND_HIGH52_BARS)
    hh = np.fmax(np.where(np.isfinite(h), h, np.nan), c)
    hi = pd.DataFrame(hh).rolling(w, min_periods=1).max().to_numpy()
    first = np.where(present.any(axis=0), present.argmax(axis=0), T + 1)
    age_ok = (np.arange(T)[:, None] - first[None, :]) >= (w - 1)
    ok = present & age_ok & clean_fn(w) & np.isfinite(hi) & (hi > 0)
    with np.errstate(invalid="ignore", divide="ignore"):
        dist = (1.0 - c / hi) * 100.0
    return np.where(ok, dist, np.nan), np.where(ok, hi, np.nan)


# ══════════════════════════ DB 입출력 ══════════════════════════
def _calendar(store, until: str | None = None) -> list[str]:
    from .events import trading_calendar
    cal = trading_calendar(store)
    if until:
        cal = [d for d in cal if d <= until]
        if until not in cal and store.has_price_date(until):
            cal.append(until)     # last_price_date 와 complete_dates 기준이 갈린 날
    return cal


def load_wide(store, until: str | None = None) -> dict:
    """DB 시세 → 달력 정렬 행렬 {dates, tickers, c, h, l, v}. 읽기만 한다."""
    cal = _calendar(store, until)
    if not cal:
        return {"dates": [], "tickers": []}
    with closing(sqlite3.connect(store.path, timeout=30.0)) as con:
        # NOT INDEXED: 범위가 거의 전 테이블이라 ix_prices_d 를 타면 행마다
        # 임의 접근이 된다(사본 실측 16.5초 → 전체 순차 스캔 약 4초).
        df = pd.read_sql_query(
            "SELECT d,ticker,h,l,c,v FROM prices NOT INDEXED WHERE d>=? AND d<=?",
            con, params=(cal[0], cal[-1]))
    di = pd.Index(cal).get_indexer(df["d"])        # 달력 밖(부분 적재일) = -1
    keep = di >= 0
    codes, uniq = pd.factorize(df["ticker"].astype(str).to_numpy()[keep],
                               sort=True)
    tick = [str(t) for t in uniq]
    out = {"dates": cal, "tickers": tick}
    for col in ("c", "h", "l", "v"):
        m = np.full((len(cal), len(tick)), np.nan)
        m[di[keep], codes] = df[col].to_numpy(dtype="float64")[keep]
        out[col] = np.where(m > 0, m, np.nan) if col != "v" else m
    return out


def _index_matrix(dates: list[str], tickers: list[str],
                  index: dict[str, pd.DataFrame] | None,
                  markets: dict[str, str]) -> np.ndarray:
    """종목별 소속 시장 지수 종가를 달력에 맞춘 행렬. 없는 날짜·시장 = NaN."""
    T, N = len(dates), len(tickers)
    out = np.full((T, N), np.nan)
    for mk, f in (index or {}).items():
        if f is None or f.empty or "Close" not in f.columns:
            continue
        s = f["Close"].astype("float64")
        s.index = [str(i)[:10] for i in s.index]
        s = s[~pd.Index(s.index).duplicated(keep="last")]
        col = s.reindex(dates).to_numpy(dtype="float64")
        sel = [j for j, t in enumerate(tickers) if markets.get(t) == mk]
        if sel:
            out[:, sel] = col[:, None]
    return out


def write_rows(store, rows, con=None) -> int:
    """(d, ticker, name, value, calc_version, kind, created) 를 INSERT OR IGNORE.

    반환 = 실제로 추가된 행 수. 이미 있는 키(같은 계산버전)는 그대로 둔다.
    """
    sql = ("INSERT OR IGNORE INTO indicator_log "
           "(d, ticker, name, value, calc_version, kind, created) "
           "VALUES (?,?,?,?,?,?,?)")
    own = con is None
    if own:
        con = sqlite3.connect(store.path, timeout=30.0)
    try:
        before = con.total_changes
        con.executemany(sql, rows)
        added = con.total_changes - before
        if own:
            con.commit()
        return int(added)
    finally:
        if own:
            con.close()


def _rows_for(pos: int, wide: dict, res: dict, kind: str, created: str,
              ver: dict) -> list[tuple]:
    d = wide["dates"][pos]
    pres = np.isfinite(wide["c"][pos])
    rows: list[tuple] = []
    tick = wide["tickers"]
    vals = {n: res[n][pos] for n in NAMES}
    for j in np.nonzero(pres)[0]:
        t = tick[j]
        for n in NAMES:
            x = vals[n][j]
            rows.append((d, t, n, float(x) if np.isfinite(x) else None,
                         ver[n], kind, created))
    return rows


def _null_counts(pos: int, wide: dict, res: dict) -> dict:
    pres = np.isfinite(wide["c"][pos])
    return {n: int((pres & ~np.isfinite(res[n][pos])).sum()) for n in NAMES}


def record_live(store, trade_date: str, index: dict | None = None,
                fetch=None) -> dict:
    """그 거래일 지표를 '실시간'으로 기록한다. daily 스캔 단계가 부른다.

    index : {시장: DataFrame(Close, index=날짜)} — 주면 조회하지 않는다(테스트).
    fetch : (name, bars) -> DataFrame|None. 기본은 data.load_index (FDR).
    지수 마지막 날짜 ≠ trade_date 면 그 시장 RS 는 전부 NULL 이다.
    """
    from .events import ticker_markets, index_frames_from_df
    t0 = time.perf_counter()
    wide = load_wide(store, until=trade_date)
    t_load = time.perf_counter()
    if not wide["dates"] or wide["dates"][-1] != trade_date:
        return {"d": trade_date, "rows": 0, "written": 0,
                "skipped": "no prices for trade_date"}
    fresh: dict[str, bool] = {}
    if index is None:
        from .data import load_index
        get = fetch or (lambda name, n: load_index(name, bars=n))
        raw = {}
        for mk in ("KOSPI", "KOSDAQ"):
            try:
                f = get(mk, max(C.IND_RS_WINDOWS) + 40)
            except Exception as exc:  # noqa: BLE001
                log.warning("지표 기록: 지수 조회 실패 %s (%s)", mk, exc)
                f = None
            if f is not None and len(f):
                raw[mk] = f
        index = index_frames_from_df(raw)
    idx_use = {}
    for mk in ("KOSPI", "KOSDAQ"):
        f = (index or {}).get(mk)
        if f is not None and len(f):
            # 기준일 이후 행은 쓰지 않는다(미래 데이터). 그 다음 마지막 날짜로 신선도.
            f = f[[str(i)[:10] <= trade_date for i in f.index]]
        last = str(f.index[-1])[:10] if f is not None and len(f) else None
        fresh[mk] = last == trade_date
        if fresh[mk]:
            idx_use[mk] = f
        else:
            log.warning("지표 기록: %s 지수 마지막 날짜 %s ≠ 기준일 %s — RS NULL",
                        mk, last, trade_date)
    t_idx = time.perf_counter()
    markets = ticker_markets(store)
    ic = _index_matrix(wide["dates"], wide["tickers"], idx_use, markets)
    res = compute_wide(wide["c"], wide["h"], wide["l"], wide["v"], ic)
    t1 = time.perf_counter()
    pos = len(wide["dates"]) - 1
    rows = _rows_for(pos, wide, res, KIND_LIVE, _now_kst(), versions())
    written = write_rows(store, rows)
    out = {"d": trade_date, "tickers": int(np.isfinite(wide["c"][pos]).sum()),
           "rows": len(rows), "written": written, "index_fresh": fresh,
           "nulls": _null_counts(pos, wide, res),
           "load_sec": round(t_load - t0, 2),
           "index_sec": round(t_idx - t_load, 2),
           "compute_sec": round(t1 - t_idx, 2),
           "write_sec": round(time.perf_counter() - t1, 2),
           "elapsed_sec": round(time.perf_counter() - t0, 2)}
    log.info("지표 기록(실시간) %s · %d종목 · %d행 추가 · %.1f초",
             trade_date, out["tickers"], written, out["elapsed_sec"])
    return out


def maybe_record_daily(store, trade_date: str, **kw) -> dict | None:
    """플래그 ON 일 때만 기록. **예외를 올리지 않는다** — daily 를 죽이지 않는다."""
    if not enabled() or not trade_date:
        return None
    try:
        return record_live(store, trade_date, **kw)
    except Exception as exc:  # noqa: BLE001 - 기록 전용
        log.warning("지표 기록 실패 — 건너뜁니다 (%s: %s)",
                    type(exc).__name__, exc)
        return {"d": trade_date, "error": f"{type(exc).__name__}: {exc}"[:200]}


def backfill(store, index: dict | None = None, date_from: str | None = None,
             date_to: str | None = None, cache_dir=None,
             commit_every: int = 20) -> dict:
    """보유한 가격 전 기간을 '소급'으로 계산해 기록한다. nightly 와 분리된 1회성.

    이미 있는 키(실시간 행 포함)는 INSERT OR IGNORE 로 건드리지 않는다.
    """
    from .events import ticker_markets, load_index_history
    t0 = time.perf_counter()
    wide = load_wide(store)
    dates = wide["dates"]
    if not dates:
        return {"dates": 0, "rows": 0, "written": 0}
    t_load = time.perf_counter()
    if index is None:
        index = load_index_history(start=dates[0], cache_dir=cache_dir)
    markets = ticker_markets(store)
    ic = _index_matrix(dates, wide["tickers"], index, markets)
    res = compute_wide(wide["c"], wide["h"], wide["l"], wide["v"], ic)
    t_comp = time.perf_counter()
    ver = versions()
    # 소급 행의 created 는 NULL. 1천만 행에 같은 시각 문자열을 반복하면
    # 사본 실측 782MB → 560MB 차이다. 계산 시각은 runs 테이블의
    # indicators-backfill 이력(시작·종료)과 반환값 started 에 남는다.
    created = None
    started = _now_kst()
    sel = [i for i, d in enumerate(dates)
           if (not date_from or d >= date_from) and (not date_to or d <= date_to)]
    rows_n = written = 0
    nulls = {n: 0 for n in NAMES}
    with closing(sqlite3.connect(store.path, timeout=30.0)) as con:
        for k, pos in enumerate(sel, 1):
            rows = _rows_for(pos, wide, res, KIND_BACKFILL, created, ver)
            rows_n += len(rows)
            written += write_rows(store, rows, con=con)
            for n, x in _null_counts(pos, wide, res).items():
                nulls[n] += x
            if k % commit_every == 0:
                con.commit()
                log.info("  지표 소급 %d/%d일 (%s) · 누적 %d행", k, len(sel),
                         dates[pos], written)
        con.commit()
    t_end = time.perf_counter()
    return {"dates": len(sel), "started": started,
            "first": dates[sel[0]] if sel else None,
            "last": dates[sel[-1]] if sel else None,
            "tickers": len(wide["tickers"]), "rows": rows_n,
            "written": written, "nulls": nulls,
            "index_markets": sorted((index or {}).keys()),
            "load_sec": round(t_load - t0, 1),
            "compute_sec": round(t_comp - t_load, 1),
            "write_sec": round(t_end - t_comp, 1),
            "elapsed_sec": round(t_end - t0, 1)}


# ══════════════════════════ 조회 · 차트 ══════════════════════════
def read_values(store, d: str, ticker: str) -> dict[str, float | None]:
    """그 날짜·종목의 지표 {이름: 값} (지표별 최신 계산버전). 없으면 빈 dict."""
    with closing(sqlite3.connect(store.path, timeout=30.0)) as con:
        rows = con.execute(
            "SELECT name, value FROM indicator_log i WHERE d=? AND ticker=? "
            "AND calc_version=(SELECT MAX(calc_version) FROM indicator_log "
            "WHERE d=i.d AND ticker=i.ticker AND name=i.name)",
            (d, str(ticker))).fetchall()
    return {n: v for n, v in rows}


def high52_level(store, ticker: str, d: str) -> float | None:
    """차트용 52주 고가(원). dist_52w_high 와 같은 규칙 — NULL 이면 None.

    플래그 OFF 면 None (차트가 지금과 같다).
    """
    if not enabled():
        return None
    cal = _calendar(store, until=d)
    w = int(C.IND_HIGH52_BARS)
    if len(cal) < w or cal[-1] != d:
        return None
    with closing(sqlite3.connect(store.path, timeout=30.0)) as con:
        df = pd.read_sql_query(
            "SELECT d,h,c FROM prices WHERE ticker=? AND d>=? AND d<=?",
            con, params=(str(ticker), cal[0], d))
    if df.empty:
        return None
    df = df.set_index("d").reindex(cal)
    c = df["c"].to_numpy(dtype="float64")[:, None]
    h = df["h"].to_numpy(dtype="float64")[:, None]
    c = np.where(c > 0, c, np.nan)
    present = np.isfinite(c)

    brk_cs = _break_cs(c)
    _dist, hi = _high52(c, h, present, lambda win: _clean(brk_cs, win))
    x = hi[-1, 0]
    return float(x) if np.isfinite(x) else None
