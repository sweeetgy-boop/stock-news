# -*- coding: utf-8 -*-
"""[E] 위험 정보 — EWMA 일간 변동성 σ, 20일 예상 범위(원뿔), 손절선 도달
확률, 원뿔 적중률. **기록·표시 전용이다.** 점수·등급·추천 선정에 들어가지
않는다. 플래그 `config.RISK_INFO` 가 꺼져 있으면 아무것도 기록·표시하지
않는다.

σ 정의 (calc_version 1)
----------------------
  r_t   = ln(C_t / C_{t-1})                         종가 로그수익률, 평균 0 가정
  σ²_t  = λ·σ²_{t-1} + (1-λ)·r_t²                   λ = RISK_EWMA_LAMBDA (0.94)
  시드  = (재)시작 뒤 처음 RISK_SIGMA_SEED(20)개 r² 의 평균
  창    = 그날까지 최근 RISK_SIGMA_WINDOW(120)개 거래일. **그날 이후 데이터는
          쓰지 않는다.** 실시간 기록과 소급 기록이 같은 창을 쓰므로 같은 날
          같은 값이 나온다.
  NULL  = (재)시작 뒤 유효 수익률이 RISK_SIGMA_MIN_OBS(60)개 미만이거나,
          그날 종가가 없다(거래정지).

  거래일 달력은 `events.trading_calendar` 와 같다(완전 적재일). 종목에
  행이 없는 날(거래정지)은 갱신하지 않고 건너뛰며, 재개 첫날의 수익률은
  직전 거래 종가 대비다.

수정주가 불연속 ([0] 과 같은 규칙)
-------------------------------
  하루 종가 변동이 PRICE_JUMP_BREAK_PCT(±30%) 를 넘으면 가격제한폭 밖이라
  실제 거래가 아니다 — 분할·병합·감자가 원시 가격에 남은 흔적이다. 그
  수익률은 **쓰지 않고 EWMA 를 그날부터 다시 시작한다.** 그래서 불연속 뒤
  RISK_SIGMA_MIN_OBS 거래일 동안 σ = NULL 이다. 불연속을 그대로 넣으면 r²
  하나(예: ln 0.5² = 0.48)가 σ 를 수십 일 부풀린다. 0 으로 넣지 않는 것도
  같은 이유 — 모르는 값을 0 으로 쓰면 σ 가 과소추정된다.

20일 예상 범위 (원뿔)
--------------------
  로그정규, 로그 드리프트 0 (중앙값 = 기준 종가):
    상·하단(k) = P · exp(±k · σ · √t),  t = 1..RISK_HORIZON_DAYS,  k = 1, 2
  정규 가정이면 20일 뒤 종가가 ±1σ 안에 들 확률은 68.3%, ±2σ 는 95.4%.

손절선 도달 확률
----------------
  같은 모형(로그 드리프트 0 기하 브라운 운동) + 반사원리:
    P(T 안에 최저가 ≤ B) = 2 · Φ( ln(B'/P) / (σ·√T) )
  손절 판정은 **종가**로 한다(`exits` 의 stop_hit = price <= stop). 연속
  관측 공식은 종가 관측보다 확률을 높게 잡으므로 Broadie–Glasserman–Kou
  (1997) 이산 관측 보정으로 장벽을 한 칸 멀리 둔다:
    B' = B · exp(-β · σ · √Δt),  β = −ζ(½)/√(2π) ≈ 0.5826,  Δt = 1거래일
  경계: P ≤ B → 1.0 · T ≤ 0 → 0.0 · B ≤ 0 → 0.0 · σ 없음 → None.
  smoke 가 몬테카를로(일간 종가 관측)와 대조한다.

"평소 출렁임의 n배" (D·F 의 ③ 요약이 쓴다)
------------------------------------------
  n = ln(1 + R) / (σ · √h)
    R = 보유 수익률(소수, 0.123 = +12.3%), h = 보유 거래일(≥1)
    σ = **기준일(추천 이벤트일 또는 진입 기준일)의** 일간 σ — 움직임이
        시작되기 전의 '평소'. 움직인 뒤의 σ 를 쓰면 움직임이 σ 를 키워
        n 이 작게 나온다.
  부호를 유지한다. 표시는 |n| 을 "n배" 로, 방향은 수익률 부호로.
  `move_multiple()` 이 계산한다.

원뿔 적중률
-----------
  기록된 σ(그날) 로 그린 원뿔에 RISK_HORIZON_DAYS 거래일 뒤 종가가 들었는가.
  적중 = |ln(C_{t+20}/C_t)| ≤ k·σ_t·√20. 제외: 20일째 행 없음(정지),
  구간 안 불연속, 미도래. weekly 가 RISK_INFO ON 일 때만 집계한다.
"""
from __future__ import annotations

import logging
import math
import sqlite3
import time
from contextlib import closing
from datetime import datetime

import numpy as np
import pandas as pd

from . import config as C

log = logging.getLogger(__name__)

__all__ = ["enabled", "BGK_BETA", "ewma_sigma_last", "sigma_panel",
           "close_panel", "record_daily", "backfill", "sigma_on",
           "stop_hit_probability", "cone_bounds", "move_multiple",
           "chart_risk", "caption_line", "cone_hit_rate",
           "render_cone_block", "KIND_LIVE", "KIND_BACKFILL"]

KIND_LIVE = "실시간"
KIND_BACKFILL = "소급"
# Broadie–Glasserman–Kou 이산 관측 보정 상수 −ζ(1/2)/√(2π).
BGK_BETA = 0.5826


def enabled() -> bool:
    """RISK_INFO 플래그. 호출 시점에 읽는다(smoke 가 바꿔 끼울 수 있게)."""
    return bool(getattr(C, "RISK_INFO", False))


def _now() -> str:
    from .trading_day import now_kst
    return now_kst().replace(tzinfo=None).isoformat(timespec="seconds")


def _lim() -> float:
    # 상한가(정확히 +30%)는 실제 거래다. events.PricePanel 과 같은 여유.
    return C.PRICE_JUMP_BREAK_PCT / 100.0 + 1e-6


# ══════════════════════════ σ 계산 ══════════════════════════
def ewma_sigma_last(close: np.ndarray, lam: float | None = None,
                    seed: int | None = None,
                    min_obs: int | None = None) -> np.ndarray:
    """종가 행렬(행=거래일 오름차순, 열=종목)의 **마지막 행** σ.

    행렬 전체가 계산 창이다(호출부가 창을 잘라 넘긴다). NaN = 그날 행 없음.
    반환: 열마다 σ(소수). 조건 미달·마지막 행 종가 없음은 NaN.
    """
    lam = C.RISK_EWMA_LAMBDA if lam is None else float(lam)
    seed = C.RISK_SIGMA_SEED if seed is None else int(seed)
    min_obs = C.RISK_SIGMA_MIN_OBS if min_obs is None else int(min_obs)
    x = np.asarray(close, dtype="float64")
    if x.ndim == 1:
        x = x[:, None]
    n = x.shape[1]
    lim = _lim()
    prev = np.full(n, np.nan)
    cnt = np.zeros(n)
    sumsq = np.zeros(n)
    var = np.full(n, np.nan)
    with np.errstate(invalid="ignore", divide="ignore"):
        for row in x:
            c = np.where(row > 0, row, np.nan)
            ok = np.isfinite(c) & np.isfinite(prev)
            ratio = np.where(ok, c / np.where(ok, prev, 1.0), 1.0)
            brk = ok & (np.abs(ratio - 1.0) > lim)
            if brk.any():
                cnt[brk] = 0.0
                sumsq[brk] = 0.0
                var[brk] = np.nan
            use = ok & ~brk
            if use.any():
                r2 = np.log(ratio) ** 2
                cnt = np.where(use, cnt + 1.0, cnt)
                in_seed = use & (cnt <= seed)
                sumsq = np.where(in_seed, sumsq + r2, sumsq)
                var = np.where(in_seed, sumsq / np.maximum(cnt, 1.0), var)
                rec = use & (cnt > seed)
                var = np.where(rec, lam * var + (1.0 - lam) * r2, var)
            prev = np.where(np.isfinite(c), c, prev)
        last_ok = np.isfinite(np.where(x[-1] > 0, x[-1], np.nan))
        sig = np.sqrt(var)
    sig[(cnt < min_obs) | ~last_ok | ~np.isfinite(sig)] = np.nan
    return sig


def close_panel(store, end: str | None = None, rows: int | None = None,
                since: str | None = None,
                tickers: list[str] | None = None) -> pd.DataFrame:
    """거래일 × 종목 종가 (오름차순). 읽기만 한다.

    달력 = 완전 적재일(`events.trading_calendar`) ≤ end. end 가 부분
    적재일이어도(장중 등) 그날을 마지막 행으로 넣는다 — 그날 σ 를 묻는
    것이므로. rows 가 있으면 마지막 rows 거래일만.
    """
    from .events import trading_calendar
    cal = trading_calendar(store)
    if end:
        cal = [d for d in cal if d <= end]
        if not cal or cal[-1] != end:
            with closing(sqlite3.connect(store.path, timeout=30.0)) as con:
                have = con.execute("SELECT 1 FROM prices WHERE d=? LIMIT 1",
                                   (end,)).fetchone()
            if have:
                cal.append(end)
    if since:
        cal = [d for d in cal if d >= since]
    if rows:
        cal = cal[-int(rows):]
    if not cal:
        return pd.DataFrame()
    q = "SELECT d,ticker,c FROM prices WHERE d>=? AND d<=?"
    params: list = [cal[0], cal[-1]]
    if tickers is not None:
        tickers = [str(t) for t in tickers]
        if not tickers:
            return pd.DataFrame(index=cal)
        q += f" AND ticker IN ({','.join('?' * len(tickers))})"
        params += tickers
    with closing(sqlite3.connect(store.path, timeout=30.0)) as con:
        df = pd.read_sql_query(q, con, params=params)
    calset = set(cal)
    df = df[df["d"].isin(calset)]
    out = df.pivot(index="d", columns="ticker", values="c").reindex(cal)
    return out.where(out > 0)


def sigma_panel(close: pd.DataFrame, targets: list[str] | None = None,
                window: int | None = None) -> pd.DataFrame:
    """종가 패널 → 날짜별 σ 패널 (targets 행만, 기본 전체 날짜).

    날짜 t 의 σ 는 [t-window, t] 행(수익률 window 개)만 본다.
    """
    window = C.RISK_SIGMA_WINDOW if window is None else int(window)
    if close is None or close.empty:
        return pd.DataFrame()
    dates = list(close.index)
    pos = {d: i for i, d in enumerate(dates)}
    targets = dates if targets is None else [d for d in targets if d in pos]
    x = close.to_numpy(dtype="float64")
    out = np.full((len(targets), x.shape[1]), np.nan)
    for k, d in enumerate(targets):
        i = pos[d]
        out[k] = ewma_sigma_last(x[max(0, i - window): i + 1])
    return pd.DataFrame(out, index=targets, columns=close.columns)


def _write(store, d_rows: list[tuple], kind: str) -> int:
    """(d, ticker, σ) 목록 기록. 실시간은 덮어쓰고 소급은 비어 있을 때만."""
    if not d_rows:
        return 0
    verb = "INSERT OR REPLACE" if kind == KIND_LIVE else "INSERT OR IGNORE"
    now = _now()
    lam = float(C.RISK_EWMA_LAMBDA)
    ver = int(C.RISK_CALC_VERSION)
    with closing(sqlite3.connect(store.path, timeout=60.0)) as con:
        before = con.total_changes
        con.executemany(
            f"{verb} INTO risk_metrics(d,ticker,sigma_ewma,lam,calc_version,"
            "kind,created) VALUES (?,?,?,?,?,?,?)",
            [(d, t, float(s), lam, ver, kind, now) for d, t, s in d_rows])
        con.commit()
        return con.total_changes - before


def _rows_from(sig: pd.DataFrame) -> list[tuple]:
    st = sig.stack()
    st = st[np.isfinite(st.to_numpy(dtype="float64"))]
    return [(str(d), str(t), float(v)) for (d, t), v in st.items()]


def record_daily(store, trade_date: str) -> dict:
    """daily 스캔 단계: 그 거래일 전 종목 σ 를 '실시간' 으로 기록한다.

    플래그는 호출부가 본다. 예외는 호출부가 삼킨다(기록 전용).
    """
    t0 = time.time()
    close = close_panel(store, end=trade_date,
                        rows=C.RISK_SIGMA_WINDOW + 1)
    if close.empty or close.index[-1] != trade_date:
        return {"d": trade_date, "written": 0, "reason": "no prices"}
    sig = sigma_panel(close, targets=[trade_date])
    rows = _rows_from(sig)
    n = _write(store, rows, KIND_LIVE)
    out = {"d": trade_date, "written": n,
           "tickers": int(close.iloc[-1].notna().sum()),
           "null": int(close.iloc[-1].notna().sum()) - len(rows),
           "calc_version": int(C.RISK_CALC_VERSION),
           "elapsed_sec": round(time.time() - t0, 2)}
    log.info("risk_metrics %s σ 기록 %d종목 (NULL %d) %.1fs", trade_date,
             n, out["null"], out["elapsed_sec"])
    return out


def backfill(store, start: str | None = None, end: str | None = None,
             progress_every: int = 50) -> dict:
    """과거 거래일 σ 를 '소급' 으로 채운다. nightly 와 분리된 1회성 명령.

    이미 있는 행(실시간 포함)은 건드리지 않는다(INSERT OR IGNORE). 재실행
    하면 빈 날짜만 채우므로 멱등이다. 날짜마다 그날까지의 창만 쓴다.
    """
    t0 = time.time()
    close = close_panel(store, end=end)
    if close.empty:
        return {"dates": 0, "written": 0, "elapsed_sec": 0.0}
    dates = list(close.index)
    targets = [d for d in dates if (not start or d >= start)]
    total = 0
    win = C.RISK_SIGMA_WINDOW
    x = close.to_numpy(dtype="float64")
    pos = {d: i for i, d in enumerate(dates)}
    for k, d in enumerate(targets, 1):
        i = pos[d]
        s = ewma_sigma_last(x[max(0, i - win): i + 1])
        ok = np.isfinite(s)
        rows = [(d, str(t), float(v)) for t, v in
                zip(close.columns[ok], s[ok])]
        total += _write(store, rows, KIND_BACKFILL)
        if progress_every and k % progress_every == 0:
            log.info("  σ 소급 %d/%d (%s) 누적 %d행 %.0fs", k, len(targets),
                     d, total, time.time() - t0)
    out = {"dates": len(targets), "first": targets[0] if targets else None,
           "last": targets[-1] if targets else None, "written": total,
           "tickers": int(close.shape[1]),
           "calc_version": int(C.RISK_CALC_VERSION),
           "elapsed_sec": round(time.time() - t0, 1)}
    log.info("σ 소급 완료: %d거래일 · %d행 기록 · %.1f초", out["dates"],
             total, out["elapsed_sec"])
    return out


def _snap_date(store, d: str) -> str | None:
    """d 이하 마지막 거래일(완전 적재일, d 자신이 적재일이면 d)."""
    d = str(d)[:10]
    with closing(sqlite3.connect(store.path, timeout=30.0)) as con:
        if con.execute("SELECT 1 FROM prices WHERE d=? LIMIT 1",
                       (d,)).fetchone():
            return d
    from .events import trading_calendar
    cal = [x for x in trading_calendar(store) if x <= d]
    return cal[-1] if cal else None


def sigma_on(store, d, ticker: str) -> float | None:
    """그 거래일 종목의 σ. 기록이 있으면 기록, 없으면 같은 정의로 즉석 계산.

    d 가 거래일이 아니면(일요일 등) 그 이전 마지막 거래일. 그날 종목 행이
    없으면(정지) None. 플래그 판정은 하지 않는다(훅이 한다).
    """
    d0 = _snap_date(store, str(d)[:10])
    if not d0:
        return None
    t = str(ticker)
    with closing(sqlite3.connect(store.path, timeout=30.0)) as con:
        try:
            r = con.execute(
                "SELECT sigma_ewma FROM risk_metrics WHERE d=? AND ticker=? "
                "AND calc_version=?", (d0, t, int(C.RISK_CALC_VERSION))
            ).fetchone()
        except sqlite3.OperationalError:
            r = None
    if r and r[0] is not None and math.isfinite(r[0]):
        return float(r[0])
    close = close_panel(store, end=d0, rows=C.RISK_SIGMA_WINDOW + 1,
                        tickers=[t])
    if close.empty or t not in close.columns or close.index[-1] != d0:
        return None
    s = float(ewma_sigma_last(close[[t]].to_numpy())[0])
    return s if math.isfinite(s) else None


# ══════════════════════════ 확률 · 원뿔 ══════════════════════════
def _phi(x: float) -> float:
    return 0.5 * math.erfc(-x / math.sqrt(2.0))


def stop_hit_probability(price, stop, sigma, days: int | None = None,
                         discrete: bool = True) -> float | None:
    """price 에서 days 거래일 안에 **종가가** stop 이하로 닫힐 확률(0~1).

    모형은 모듈 docstring. discrete=False 면 연속 관측(보정 없음).
    """
    days = C.RISK_HORIZON_DAYS if days is None else days
    try:
        p, b = float(price), float(stop)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(p) or p <= 0 or not math.isfinite(b):
        return None
    if b <= 0:
        return 0.0
    if p <= b:
        return 1.0
    if days is None or days <= 0:
        return 0.0
    try:
        s = float(sigma)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(s) or s < 0:
        return None
    if s == 0:
        return 0.0
    lb = math.log(b / p)
    if discrete:
        lb -= BGK_BETA * s       # Δt = 1거래일
    prob = 2.0 * _phi(lb / (s * math.sqrt(float(days))))
    return min(1.0, max(0.0, prob))


def cone_bounds(price: float, sigma: float, days: int | None = None,
                ks=(1, 2)) -> dict:
    """t = 0..days 의 원뿔. {"t": [...], 1: (하단[], 상단[]), 2: (...)}."""
    days = C.RISK_HORIZON_DAYS if days is None else int(days)
    t = np.arange(days + 1, dtype="float64")
    out: dict = {"t": t}
    for k in ks:
        w = k * float(sigma) * np.sqrt(t)
        out[k] = (float(price) * np.exp(-w), float(price) * np.exp(w))
    return out


def move_multiple(ret, sigma, days) -> float | None:
    """"평소 출렁임의 n배" 의 n = ln(1+R) / (σ·√h). 부호 유지.

    ret   : 수익률 소수(0.123 = +12.3%). ≤ -1 이면 None.
    sigma : 기준일 일간 σ (소수). 없거나 ≤0 이면 None.
    days  : 보유 거래일(≥1). 1 미만이면 None.
    """
    try:
        r, s, h = float(ret), float(sigma), float(days)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(r) and math.isfinite(s) and math.isfinite(h)):
        return None
    if r <= -1.0 or s <= 0 or h < 1:
        return None
    return math.log1p(r) / (s * math.sqrt(h))


# ══════════════════════════ 차트 · 캡션 ══════════════════════════
def chart_risk(store, trade_date: str, ticker: str, price: float,
               cfg=None) -> dict | None:
    """차트·캡션에 넘길 위험 정보. 플래그 OFF·σ 없음·실패면 None.

    손절선은 추천 종목에 아직 포지션이 없으므로 **기준일 종가를 진입가로
    가정**해 `exits.stop_price_for()` 로 구한다(실제 포지션의 기록 손절선과
    같은 규칙). 실제 진입은 다음 날 시가라 손절선도 그만큼 달라진다.
    """
    if not enabled():
        return None
    try:
        from .config import DEFAULT
        from .exits import stop_price_for
        cfg = cfg or DEFAULT
        if not (price and price > 0):
            return None
        s = sigma_on(store, trade_date, ticker)
        days = int(C.RISK_HORIZON_DAYS)
        stop = stop_price_for(float(price), cfg)
        if s is None:
            # 원뿔은 그리지 않고 캡션에 '계산 불가' 한 줄만 남긴다.
            # 줄이 아예 없으면 플래그가 꺼진 것과 구분되지 않는다.
            return {"sigma": None, "days": days, "price": float(price),
                    "stop": float(stop),
                    "stop_pct": float(cfg.exit.stop_loss_pct), "prob": None,
                    "lam": float(C.RISK_EWMA_LAMBDA)}
        return {"sigma": float(s), "days": days, "price": float(price),
                "stop": float(stop), "stop_pct": float(cfg.exit.stop_loss_pct),
                "prob": stop_hit_probability(price, stop, s, days),
                "lam": float(C.RISK_EWMA_LAMBDA)}
    except Exception as exc:  # noqa: BLE001 - 표시 전용
        log.warning("위험 정보 계산 실패 %s: %s", ticker, exc)
        return None


def caption_line(risk: dict | None) -> str | None:
    """캡션 1줄. '20일 내 손절선(-10% · 63,000원) 도달 확률 23% · σ 2.3%/일'.

    σ 가 없으면(표본 부족·수정주가 불연속 직후·정지) '계산 불가' 한 줄.
    """
    if not risk:
        return None
    if risk.get("sigma") is None or risk.get("prob") is None:
        return (f"{risk['days']}일 내 손절선 도달 확률 - (σ 없음: 표본 "
                f"부족·수정주가 불연속 직후)")
    return (f"{risk['days']}일 내 손절선(-{risk['stop_pct']:.0f}% · "
            f"{risk['stop']:,.0f}원) 도달 확률 {risk['prob'] * 100:.0f}% · "
            f"σ {risk['sigma'] * 100:.1f}%/일")


# ══════════════════════════ 원뿔 적중률 ══════════════════════════
def _sigma_matrix(store, dates: list[str], cols) -> pd.DataFrame:
    with closing(sqlite3.connect(store.path, timeout=30.0)) as con:
        try:
            df = pd.read_sql_query(
                "SELECT d,ticker,sigma_ewma FROM risk_metrics "
                "WHERE calc_version=? AND d>=? AND d<=?", con,
                params=(int(C.RISK_CALC_VERSION), dates[0], dates[-1]))
        except Exception:  # noqa: BLE001 - 테이블 없음
            return pd.DataFrame(index=dates, columns=cols, dtype="float64")
    if df.empty:
        return pd.DataFrame(index=dates, columns=cols, dtype="float64")
    m = df.pivot_table(index="d", columns="ticker", values="sigma_ewma",
                       aggfunc="last")
    return m.reindex(index=dates, columns=cols).astype("float64")


def cone_hit_rate(store, days: int | None = None, recos=None) -> dict:
    """원뿔 적중률. 추천 이벤트([0] events) 와 전종목(기록된 σ 전부).

    반환 {"days", "reco": {...}, "all": {...}, "first_origin", "last_origin"}
      각 {...} = n, hit1, hit2 (비율 0~1 또는 None), above1, below1,
                 excluded (불연속·20일째 정지), expected1/expected2
    """
    from .events import (PricePanel, assign_events, events_only,
                         trading_calendar)
    days = C.RISK_HORIZON_DAYS if days is None else int(days)
    cal = trading_calendar(store)
    if len(cal) <= days:
        return {"days": days, "reco": None, "all": None}
    panel = PricePanel.from_store(store, calendar=cal)
    close = panel.close
    sig = _sigma_matrix(store, cal, close.columns)
    x = close.to_numpy(dtype="float64")
    s = sig.to_numpy(dtype="float64")
    bcs = panel._break_cs           # [0] 과 같은 불연속 규칙(누적 표)
    T = len(cal)
    a = np.arange(T - days)
    c0, c1 = x[a], x[a + days]
    s0 = s[a]
    nb = bcs[a + days] - bcs[a]
    base = np.isfinite(c0) & np.isfinite(s0) & (s0 > 0)
    halted = base & ~np.isfinite(c1)
    brk = base & np.isfinite(c1) & (nb > 0)
    ok = base & np.isfinite(c1) & (nb == 0)
    with np.errstate(invalid="ignore", divide="ignore"):
        z = np.log(c1 / c0) / (s0 * math.sqrt(days))

    def _cell(mask) -> dict:
        n = int(mask.sum())
        zz = z[mask]
        cell = {"n": n, "expected1": 0.6827, "expected2": 0.9545,
                "hit1": None, "hit2": None, "above1": int((zz > 1).sum()),
                "below1": int((zz < -1).sum())}
        if n:
            cell["hit1"] = float((np.abs(zz) <= 1).mean())
            cell["hit2"] = float((np.abs(zz) <= 2).mean())
        cell["enough"] = n >= int(C.RISK_CONE_MIN_N)
        return cell

    out = {"days": days, "first_origin": None, "last_origin": None}
    allc = _cell(ok)
    allc["excluded"] = int(halted.sum() + brk.sum())
    out["all"] = allc
    rows_ok = np.where(ok.any(axis=1))[0]
    if len(rows_ok):
        out["first_origin"] = cal[int(rows_ok[0])]
        out["last_origin"] = cal[int(rows_ok[-1])]

    # 추천 이벤트 — 추천 1건의 정의는 [0] events 가 유일하다.
    if recos is None:
        with closing(sqlite3.connect(store.path, timeout=30.0)) as con:
            recos = pd.read_sql_query(
                "SELECT d,ticker,rank,slot,grade FROM recos", con)
    mask = np.zeros_like(ok)
    excl = 0
    if recos is not None and not recos.empty:
        ev = events_only(assign_events(recos, cal))
        pos = {d: i for i, d in enumerate(cal)}
        for d, t in zip(ev["event_date"], ev["ticker"]):
            i, j = pos.get(str(d)), panel.col(str(t))
            if i is None or j is None or i >= T - days:
                continue
            if ok[i, j]:
                mask[i, j] = True
            elif halted[i, j] or brk[i, j]:
                excl += 1
    rc = _cell(mask)
    rc["excluded"] = excl
    out["reco"] = rc
    return out


def _pct(v) -> str:
    return "-" if v is None else f"{v * 100:.1f}%"


def render_cone_block(rep: dict | None) -> list[str]:
    """weekly 리포트 블록. 해석 문구 없이 숫자만."""
    if not rep or rep.get("all") is None:
        return []
    d = rep["days"]
    out = [f"━━ 변동성 원뿔 적중률 ({d}거래일 뒤 종가) ━━"]

    def _line(label: str, c: dict | None) -> str:
        if not c or not c["n"]:
            return f"• {label}: 만기 표본 없음"
        if not c.get("enough"):
            return f"• {label}: 표본 부족 (n={c['n']:,})"
        return (f"• {label}: ±1σ {_pct(c['hit1'])} · ±2σ {_pct(c['hit2'])} "
                f"(n={c['n']:,} · 위 이탈 {c['above1']:,} · 아래 이탈 "
                f"{c['below1']:,})")

    out.append(_line("추천 이벤트", rep.get("reco")))
    out.append(_line("전종목", rep.get("all")))
    span = ""
    if rep.get("first_origin"):
        span = f" · 기준일 {rep['first_origin']}~{rep.get('last_origin')}"
    out.append(f"• 정규 가정 기대값 ±1σ 68.3% · ±2σ 95.4%{span}")
    out.append("• ※ 기록 전용. σ = EWMA(λ "
               f"{C.RISK_EWMA_LAMBDA:g}) 일간 변동성 · 정지·수정주가 불연속 제외")
    return out
