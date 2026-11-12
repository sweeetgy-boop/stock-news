# -*- coding: utf-8 -*-
"""백테스트 — 워크포워드 이벤트 스터디.

설계 원칙
--------
**운영 코드를 그대로 쓴다.** 백테스트용 별도 스코어링을 만들면 두 구현이
갈라지고, 그때부터 백테스트 결과는 아무 의미가 없다. `screen_one` /
`evaluate_position` 을 시계열 절단(slice)해서 호출한다. 우리 지표는 전부
과거만 참조하므로 절단만으로 룩어헤드가 차단된다.

정직한 측정을 위한 네 가지
------------------------
  1) 익일 시가 체결   종가로 판정하므로 실제 체결은 다음 날이다.
                      종가 체결로 계산하면 성과가 부풀려진다.
  2) 왕복 비용 차감   수수료 + 거래세 0.5%.
  3) 최대 역행폭(MAE) 보유 중 최저가까지의 낙폭. "얼마나 더 빠졌나"에
                      답한다. 평균 수익률만 보면 이걸 놓친다.
  4) 대조군 3종       시장 중위 / 무작위 진입 / 단순 RSI<30.
                      (3)이 가장 중요하다. 시장 대비 초과수익이 없으면
                      그건 전략이 아니라 그냥 하락장 베타다.

v2-A (2026-11-12) — 측정 정의를 [0] 공통 기반으로 통일
---------------------------------------------------
  · 채점은 `events.score_events` 하나로 한다. 전략·무작위·RSI 대조군이
    **같은 지표**(전종목 중위 대비 초과수익 = alpha_h = ex_med_h)를 갖는다.
    예전 `summarize()` 는 전략의 alpha 평균을 무작위 대조군의 **순수익**
    평균과 비교했다 — 단위가 다른 두 숫자의 비교라 판정이 무의미했다.
  · 보유 h 의 정의도 [0] 을 따른다: 익일 시가 진입(1일째) → h일째 종가.
    (예전 `_fwd` 는 h거래일 뒤 시가 청산. `_fwd` 는 참고용으로만 남긴다.)
  · 같은 종목이 N(=EVENT_WINDOW_DAYS)거래일 안에 다시 잡히면 같은 이벤트다
    (`events.assign_events`). 간격(step) 5 로 표본을 뽑으면 한 흐름이
    4~5번 세어져 표본 수가 부풀고 서로 독립이 아니게 된다.
  · 시장 중위는 풀(유동성 통과 종목)이 아니라 [0] 의 전종목 중위다.

알려진 한계 (반드시 인지할 것)
---------------------------
  · 배제 플래그(관리종목·자본잠식)의 과거 시점 값이 없다. 현재 기준으로도
    적용하지 않는다. 따라서 결과는 **낙관적**이다. 당시 관리종목이던
    종목이 표본에 섞여 있다.
  · 신용잔고 실측 히스토리가 없으므로 LPS 의 credit_heat 는 프록시 캡
    상태다. 실측이 쌓이면 재실행해야 한다.
  · 상장폐지 종목이 DB 에 없으므로 생존 편향이 있다.
"""
from __future__ import annotations

import logging
import random
from dataclasses import dataclass, field
from datetime import datetime

import numpy as np
import pandas as pd

from .config import Config, DEFAULT, EVENT_WINDOW_DAYS
from .contracts import Position
from .events import (assign_events, events_only, PricePanel, score_events,
                     stat_cell, ticker_markets, trading_calendar)
from .exits import evaluate_position, stop_price_for
from .indicators import moving_averages
from .liquidation import liquidation_band
from .screener import screen_one

log = logging.getLogger(__name__)

__all__ = ["BacktestConfig", "run_backtest", "summarize", "sweep_thresholds",
           "simulate_exit_rules", "score_context", "dedupe_events",
           "score_rows", "attach_regime", "exits_vs_hold", "NO_REGIME",
           "backtest_candidates", "finish_events"]

# 국면 훅(C)이 값을 주지 않을 때 표에 쓰는 라벨
NO_REGIME = "국면 없음"


@dataclass(frozen=True)
class BacktestConfig:
    step: int = 5                       # 평가 간격(거래일). 1이면 전수
    horizons: tuple = (1, 3, 5, 10, 20)
    cost_pct: float = 0.5               # 왕복 비용
    min_bars: int = 160                 # 채점에 필요한 최소 봉 수
    warmup: int = 160                   # 이 봉 수 이후부터 평가 시작
    grades: tuple = ("S+", "S", "A")    # 이벤트로 볼 등급
    max_hold: int = 40                  # 청산 규칙 시뮬 최대 보유 거래일
    regime_window: int = 20             # 국면 판정용 시장 수익률 구간
    seed: int = 7
    # [0] 이벤트 판정 N. 같은 종목이 이 거래일 수 안에 다시 잡히면 같은
    # 이벤트다. 0 이하면 중복 제거를 하지 않는다(예전 동작 — 비교용).
    event_window: int = EVENT_WINDOW_DAYS
    # 청산 엔진과 나란히 놓을 '고정 보유' 거래일 (horizons 에 있어야 한다)
    hold_compare: int = 20


# ══════════════════════════ 보조 ══════════════════════════
def _market_close_matrix(store, tickers: list[str]) -> pd.DataFrame:
    """종가 피벗. 구 국면 판정(`_regime`, regime 컬럼)에 쓴다."""
    pm = store.price_matrix(days=100000)
    if pm is None or pm.empty:
        return pd.DataFrame()
    keep = [t for t in tickers if t in pm.columns]
    return pm[keep] if keep else pm


def _median_forward(pm: pd.DataFrame, i0: int, h: int) -> float:
    """구간 [i0, i0+h] 의 풀 중위 수익률(%). **v2-A 부터 쓰지 않는다** —
    시장 대조는 [0] `PricePanel.universe_median`(전종목, 진입 시가 → h일째
    종가)이다. 구 결과 재현·비교용으로만 남긴다."""
    if pm.empty or i0 + h >= len(pm):
        return float("nan")
    a, b = pm.iloc[i0], pm.iloc[i0 + h]
    m = a.notna() & b.notna() & (a > 0)
    if not m.any():
        return float("nan")
    return float(((b[m] / a[m]) - 1.0).median() * 100.0)


def _regime(pm: pd.DataFrame, i0: int, window: int) -> str:
    """진입 시점의 시장 국면. 하락장 베타를 실력으로 오인하지 않기 위함."""
    if pm.empty or i0 - window < 0:
        return "UNKNOWN"
    a, b = pm.iloc[i0 - window], pm.iloc[i0]
    m = a.notna() & b.notna() & (a > 0)
    if not m.any():
        return "UNKNOWN"
    r = float(((b[m] / a[m]) - 1.0).median() * 100.0)
    if r >= 3.0:
        return "UP"
    if r <= -3.0:
        return "DOWN"
    return "FLAT"


def _rsi_last(close: pd.Series, window: int = 14) -> float:
    d = close.diff()
    gain = d.where(d > 0, 0.0).rolling(window).mean()
    loss = (-d.where(d < 0, 0.0)).rolling(window).mean()
    v = 100 - (100 / (1 + gain / (loss + 1e-9)))
    return float(v.iloc[-1]) if len(v) and pd.notna(v.iloc[-1]) else float("nan")


def _fwd(ohlcv: pd.DataFrame, i0: int, h: int, cost: float) -> dict:
    """익일 시가 진입 → h거래일 뒤 시가 청산. MAE 포함.

    i0 는 '판정한 날'의 인덱스다. 진입은 i0+1 의 시가다.
    """
    o = ohlcv["시가"].to_numpy(dtype="float64")
    lo = ohlcv["저가"].to_numpy(dtype="float64")
    ei, xi = i0 + 1, i0 + 1 + h
    if xi >= len(o):
        return {}
    entry, exit_ = o[ei], o[xi]
    if not (np.isfinite(entry) and np.isfinite(exit_) and entry > 0):
        return {}
    ret = (exit_ / entry - 1.0) * 100.0
    trough = float(np.nanmin(lo[ei:xi + 1])) if xi > ei else entry
    mae = (trough / entry - 1.0) * 100.0        # 항상 <= 0
    return {"entry": float(entry), "exit": float(exit_),
            "ret": ret, "net": ret - cost, "mae": mae}


def _mae_cols(ohlcv: pd.DataFrame, i0: int, horizons) -> dict:
    """[0] 구간(진입 i0+1 시가 → h일째 = i0+h)의 최대 역행폭(%). 항상 <= 0.

    채점(`score_events`)은 시가·종가만 보므로 저가가 필요한 MAE 는
    백테스트가 따로 잰다. 구간 정의는 채점과 같다.
    """
    o = ohlcv["시가"].to_numpy(dtype="float64")
    lo = ohlcv["저가"].to_numpy(dtype="float64")
    e = i0 + 1
    out = {}
    if e >= len(o) or not (np.isfinite(o[e]) and o[e] > 0):
        return out
    for h in horizons:
        x = e + int(h) - 1
        if x >= len(o):
            continue
        seg = lo[e:x + 1]
        seg = seg[np.isfinite(seg) & (seg > 0)]
        trough = min(float(seg.min()), o[e]) if len(seg) else o[e]
        out[f"mae_{h}"] = (trough / o[e] - 1.0) * 100.0
    return out


def score_context(store, index: dict | None = None) -> dict:
    """채점 재료(거래일 달력·가격 패널·시장 구분·지수). 한 번만 만든다.

    전략·대조군이 **같은 재료**로 채점돼야 같은 지표가 된다.
    index 가 None 이면 지수 대비(ex_idx)는 NaN 이다 — 0 으로 채우지 않는다.
    """
    cal = trading_calendar(store)
    panel = PricePanel.from_store(store, calendar=cal)
    return {"calendar": cal, "panel": panel,
            "markets": ticker_markets(store), "index": index or {}}


def dedupe_events(rows: pd.DataFrame, calendar: list[str],
                  window: int = EVENT_WINDOW_DAYS) -> tuple[pd.DataFrame, dict]:
    """(판정일, 종목) 후보를 [0] 이벤트 판정으로 줄인다. is_event 행만 남긴다.

    반환 (이벤트 표, {"before": n, "after": m}). window <= 0 이면 그대로.
    """
    n0 = 0 if rows is None else int(len(rows))
    if rows is None or rows.empty or window <= 0:
        return rows, {"before": n0, "after": n0}
    a = assign_events(rows.assign(d=rows["date"]), calendar, window=window)
    ev = events_only(a).drop(columns=["d"], errors="ignore")
    return ev, {"before": n0, "after": int(len(ev))}


def score_rows(rows: pd.DataFrame, ctx: dict,
               bt: "BacktestConfig") -> pd.DataFrame:
    """후보 행을 [0] `score_events` 로 채점하고 백테스트 컬럼으로 옮긴다.

      net_h   = 비용 차감 순수익            (score_events 의 net_h)
      mkt_h   = 같은 구간 전종목 중위 수익률 (med_h)
      alpha_h = 시장 중위 대비 초과수익      (ex_med_h)  ← 비교 기준 지표
      ex_idx_h= 소속 지수 대비 초과수익      (score_events 그대로)
    채점이 불가능한 구간(상한가 시가·수정주가 불연속·미도래)은 NaN 이다.
    """
    if rows is None or rows.empty:
        return pd.DataFrame() if rows is None else rows
    ev = rows.copy()
    ev["event_date"] = ev["date"]
    sc = score_events(ev, ctx["panel"], ctx.get("index"), ctx.get("markets"),
                      horizons=bt.horizons, cost_pct=bt.cost_pct)
    for h in bt.horizons:
        sc[f"mkt_{h}"] = sc[f"med_{h}"]
        sc[f"alpha_{h}"] = sc[f"ex_med_{h}"]
        mc = f"mae_{h}"
        if mc in sc:
            sc[mc] = sc[mc].where(sc[f"net_{h}"].notna())
    return sc


def attach_regime(df: pd.DataFrame, store, col: str = "date",
                  out: str = "regime_v2") -> pd.DataFrame:
    """그 날짜의 국면 라벨(C 의 `v2_hooks.regime_on`)을 붙인다.

    훅이 None 을 주면(기록 없음·지수 신선하지 않음) "국면 없음".
    """
    from . import v2_hooks
    if df is None or df.empty:
        return df
    cache: dict[str, str] = {}

    def _lab(d):
        k = str(d)[:10]
        if k not in cache:
            try:
                v = v2_hooks.regime_on(store, k)
            except Exception as exc:  # noqa: BLE001
                log.warning("국면 조회 실패 %s: %s", k, exc)
                v = None
            cache[k] = v or NO_REGIME
        return cache[k]

    df = df.copy()
    df[out] = df[col].map(_lab)
    return df


# ══════════════════════════ 본체 ══════════════════════════
def run_backtest(store, tickers: dict, cfg: Config = DEFAULT,
                 bt: BacktestConfig = BacktestConfig(),
                 progress_every: int = 50,
                 ctx: dict | None = None) -> pd.DataFrame:
    """이벤트 표를 만든다. 한 행 = (종목, 이벤트 날짜, 등급) 1건.

    판정일 후보(`backtest_candidates`)를 모은 뒤 [0] 이벤트 판정
    (N=bt.event_window)으로 중복을 지우고 [0] 채점으로 성적을 매긴다.
    전후 건수는 `df.attrs` 의 raw_events / events 에 남는다.
    """
    raw = backtest_candidates(store, tickers, cfg, bt, progress_every)
    return finish_events(store, raw, bt, ctx)


def finish_events(store, raw: pd.DataFrame, bt: BacktestConfig,
                  ctx: dict | None = None) -> pd.DataFrame:
    """후보 → 중복 제거 → [0] 채점."""
    if raw is None or raw.empty:
        log.info("백테스트 이벤트 0건")
        raw = pd.DataFrame() if raw is None else raw
        raw.attrs.update({"raw_events": 0, "events": 0})
        return raw
    ctx = ctx or score_context(store)
    ev, st = dedupe_events(raw, ctx["calendar"], bt.event_window)
    df = score_rows(ev, ctx, bt)
    df.attrs.update({"raw_events": st["before"], "events": st["after"]})
    log.info("백테스트 판정 후보 %d건 → 중복 제거(N=%d) 후 이벤트 %d건",
             st["before"], bt.event_window, st["after"])
    return df


def backtest_candidates(store, tickers: dict, cfg: Config = DEFAULT,
                        bt: BacktestConfig = BacktestConfig(),
                        progress_every: int = 50) -> pd.DataFrame:
    """판정일 후보. 한 행 = (종목, 판정일) 에서 등급이 bt.grades 인 것.

    아직 채점·중복 제거 전이다(같은 흐름이 step 간격으로 여러 번 잡힌다).
    """
    rng = random.Random(bt.seed)
    codes = list(tickers)
    pm = _market_close_matrix(store, codes)
    pm_dates = list(pm.index.strftime("%Y-%m-%d")) if not pm.empty else []
    pm_pos = {d: i for i, d in enumerate(pm_dates)}

    rows: list[dict] = []
    for n, (code, name) in enumerate(tickers.items(), 1):
        if progress_every and n % progress_every == 0:
            log.info("  백테스트 진행 %d/%d (이벤트 %d)", n, len(tickers), len(rows))
        ohlcv = store.load_ohlcv(code, days=100000)
        if ohlcv is None or len(ohlcv) < bt.warmup + max(bt.horizons) + 2:
            continue
        dates = list(ohlcv.index.strftime("%Y-%m-%d"))
        close = ohlcv["종가"]

        last_i = len(ohlcv) - max(bt.horizons) - 2
        for i in range(bt.warmup, last_i + 1, bt.step):
            sl = ohlcv.iloc[: i + 1]          # ★ 룩어헤드 차단
            try:
                r = screen_one(code, name, sl, cfg=cfg)
            except Exception:  # noqa: BLE001
                continue
            if r.grade not in bt.grades:
                continue

            base = {
                "ticker": code, "name": name, "date": dates[i],
                "grade": r.grade, "track": r.track,
                "value": r.value_score, "trend": r.trend_score,
                "confluence": int(r.confluence),
                "sequence": int(r.sequence_confirm),
                "fib_ratio": r.fib.ratio if r.fib else np.nan,
                "band_pos": r.liq.band_pos if r.liq else np.nan,
                "lps": r.liq.score if r.liq else np.nan,
                "rsi": _rsi_last(close.iloc[: i + 1]),
            }
            mi = pm_pos.get(dates[i])
            base["regime"] = (_regime(pm, mi, bt.regime_window)
                              if mi is not None else "UNKNOWN")
            base.update(_mae_cols(ohlcv, i, bt.horizons))
            rows.append(base)

    raw = pd.DataFrame(rows)
    log.info("백테스트 판정 후보 %d건 (종목 %d, 간격 %d거래일)",
             len(raw), len(tickers), bt.step)
    return raw


def control_random(store, tickers: dict, n_events: int,
                   bt: BacktestConfig = BacktestConfig(),
                   ctx: dict | None = None) -> pd.DataFrame:
    """무작위 진입 대조군. 같은 종목 풀, 무작위 날짜, 같은 표본 크기.

    전략과 같은 [0] 채점·같은 중복 제거를 거친다 → alpha_h 끼리 비교한다.
    """
    rng = random.Random(bt.seed + 1)
    codes = [c for c in tickers]
    rng.shuffle(codes)
    rows: list[dict] = []
    picked: dict[str, list[int]] = {}
    guard = 0
    while len(rows) < n_events and guard < n_events * 20 and codes:
        guard += 1
        code = codes[rng.randrange(len(codes))]
        ohlcv = store.load_ohlcv(code, days=100000)
        if ohlcv is None or len(ohlcv) < bt.warmup + max(bt.horizons) + 2:
            continue
        last_i = len(ohlcv) - max(bt.horizons) - 2
        if last_i <= bt.warmup:
            continue
        i = rng.randrange(bt.warmup, last_i + 1)
        # 같은 종목의 N거래일 안 재추첨은 중복 제거에서 사라진다. 표본
        # 크기를 전략과 맞추려고 뽑을 때 미리 피한다.
        if any(abs(i - j) < max(bt.event_window, 1)
               for j in picked.get(code, ())):
            continue
        picked.setdefault(code, []).append(i)
        row = {"ticker": code, "date": ohlcv.index[i].strftime("%Y-%m-%d")}
        row.update(_mae_cols(ohlcv, i, bt.horizons))
        rows.append(row)
    return _score_control(store, rows, bt, ctx)


def _score_control(store, rows: list[dict], bt: BacktestConfig,
                   ctx: dict | None) -> pd.DataFrame:
    """대조군 후보 → 중복 제거 → [0] 채점. 전략과 같은 경로."""
    raw = pd.DataFrame(rows)
    if raw.empty:
        return raw
    ctx = ctx or score_context(store)
    ev, st = dedupe_events(raw, ctx["calendar"], bt.event_window)
    df = score_rows(ev, ctx, bt)
    df.attrs.update({"raw_events": st["before"], "events": st["after"]})
    return df


def control_rsi(store, tickers: dict, threshold: float = 30.0,
                bt: BacktestConfig = BacktestConfig(),
                ctx: dict | None = None) -> pd.DataFrame:
    """단순 RSI<30 대조군. 우리 로직이 이보다 나은지 본다.

    전략과 같은 [0] 채점·같은 중복 제거를 거친다 → alpha_h 끼리 비교한다.
    """
    rows: list[dict] = []
    for code in tickers:
        ohlcv = store.load_ohlcv(code, days=100000)
        if ohlcv is None or len(ohlcv) < bt.warmup + max(bt.horizons) + 2:
            continue
        close = ohlcv["종가"]
        last_i = len(ohlcv) - max(bt.horizons) - 2
        for i in range(bt.warmup, last_i + 1, bt.step):
            if _rsi_last(close.iloc[: i + 1]) > threshold:
                continue
            row = {"ticker": code, "date": ohlcv.index[i].strftime("%Y-%m-%d")}
            row.update(_mae_cols(ohlcv, i, bt.horizons))
            rows.append(row)
    return _score_control(store, rows, bt, ctx)


# ══════════════════════════ 청산 규칙 시뮬레이션 ══════════════════════════
def simulate_exit_rules(store, events: pd.DataFrame, cfg: Config = DEFAULT,
                        bt: BacktestConfig = BacktestConfig()) -> pd.DataFrame:
    """고정 보유가 아니라 **실제 청산 엔진**으로 청산한다.

    이게 청산 파라미터(익절폭·트레일링폭·시간스톱·밴드 이탈 확인일수)를
    검증하는 유일한 방법이다. 고정 5일 보유 측정은 실제 운용과 대응되지
    않는다.

    같은 이벤트의 고정 보유 성적(`hold_*`, [0] 채점의 hold_compare일
    순수익·초과수익)을 행에 같이 남긴다 — 엔진 vs 고정 보유 비교용.
    """
    if events is None or events.empty:
        return pd.DataFrame()

    out: list[dict] = []
    cache: dict[str, pd.DataFrame] = {}
    hh = int(bt.hold_compare)
    for k, (_, ev) in enumerate(events.iterrows(), 1):
        if k % 500 == 0:
            log.info("  청산 시뮬 진행 %d/%d", k, len(events))
        code = str(ev["ticker"])
        ohlcv = cache.get(code)
        if ohlcv is None:
            ohlcv = store.load_ohlcv(code, days=100000)
            if ohlcv is None:
                continue
            cache[code] = ohlcv
        ds = str(ev["date"])[:10]
        try:
            i0 = int(ohlcv.index.get_loc(pd.Timestamp(ds)))
        except KeyError:
            continue
        if i0 + 2 >= len(ohlcv):
            continue

        # 진입: 판정 익일 시가
        entry = float(ohlcv["시가"].iloc[i0 + 1])
        if not np.isfinite(entry) or entry <= 0:
            continue

        # 진입 시점 스냅샷을 그때 데이터로 고정한다 (운영과 동일)
        sl = ohlcv.iloc[: i0 + 1]
        try:
            snap = screen_one(code, str(ev.get("name") or code), sl, cfg=cfg)
        except Exception:  # noqa: BLE001
            continue
        band = (liquidation_band(snap.liq.cost_basis, cfg.credit)
                if snap.liq else {})
        track = "TREND" if snap.trend_score >= snap.value_score else "VALUE"
        cross_low = None
        if (snap.trend and snap.trend.best_cross
                and snap.trend.best_cross.kind == "GOLDEN"):
            try:
                cross_low = float(ohlcv.loc[snap.trend.best_cross.date, "저가"])
            except KeyError:
                cross_low = None

        pos = Position(
            id=0, ticker=code, name=str(ev.get("name") or code), track=track,
            entry_date=ohlcv.index[i0 + 1].strftime("%Y-%m-%d"),
            entry_price=entry, qty=100, remaining=100,
            # 실거래와 같은 규칙으로 손절선을 진입 시 고정한다. 이걸 빼면
            # 백테스트에서만 계층 1 고정 손절이 발동하지 않아 성과가
            # 실제보다 좋게 나온다.
            stop_price=stop_price_for(entry, cfg),
            entry_p0=snap.liq.cost_basis if snap.liq else None,
            entry_band_hi=band.get("hi"), entry_band_mid=band.get("mid"),
            entry_band_lo=band.get("lo"),
            entry_fib_0382=snap.fib.levels.get(0.382) if snap.fib else None,
            entry_fib_0618=snap.fib.levels.get(0.618) if snap.fib else None,
            entry_cross_low=cross_low,
        )

        # 하루씩 전진하며 청산 판정. 첫 결정에서 익일 시가 청산.
        decided = None
        for j in range(i0 + 1, min(i0 + 1 + bt.max_hold, len(ohlcv) - 1)):
            try:
                dec, _ = evaluate_position(pos, ohlcv.iloc[: j + 1],
                                           None, cfg, None, None)
            except Exception:  # noqa: BLE001
                continue
            if dec is not None:
                decided = (j, dec)
                break

        if decided is None:
            j = min(i0 + bt.max_hold, len(ohlcv) - 2)
            exit_px = float(ohlcv["시가"].iloc[j + 1])
            layer, rule = 99, "max_hold"
        else:
            j, dec = decided
            exit_px = float(ohlcv["시가"].iloc[j + 1])
            layer, rule = dec.layer, dec.rule

        held = j - i0
        seg_low = float(np.nanmin(ohlcv["저가"].iloc[i0 + 1: j + 2]))
        ret = (exit_px / entry - 1.0) * 100.0
        out.append({
            "ticker": code, "date": ds, "grade": ev.get("grade"),
            "track": track, "regime": ev.get("regime"),
            "ev_track": ev.get("track"),
            "regime_v2": ev.get("regime_v2"),
            "entry": entry, "exit": exit_px, "held_days": held,
            "layer": layer, "rule": rule,
            "ret": ret, "net": ret - bt.cost_pct,
            "mae": (seg_low / entry - 1.0) * 100.0,
            "hold_h": hh,
            "hold_net": ev.get(f"net_{hh}", np.nan),
            "hold_alpha": ev.get(f"alpha_{hh}", np.nan),
            "hold_status": ev.get(f"status_{hh}"),
        })

    df = pd.DataFrame(out)
    log.info("청산 규칙 시뮬 %d건", len(df))
    return df


# ══════════════════════════ 집계 ══════════════════════════
def _stats(s: pd.Series) -> dict:
    s = s.dropna()
    if s.empty:
        return {}
    return {"n": int(len(s)), "win": round(float((s > 0).mean() * 100), 1),
            "mean": round(float(s.mean()), 2),
            "median": round(float(s.median()), 2),
            "p10": round(float(s.quantile(0.10)), 2),
            "p90": round(float(s.quantile(0.90)), 2)}


def summarize(events: pd.DataFrame, bt: BacktestConfig = BacktestConfig(),
              rnd: pd.DataFrame | None = None,
              rsi: pd.DataFrame | None = None) -> dict:
    """구간별·등급별·국면별 집계와 대조군 비교."""
    if events is None or events.empty:
        return {"n": 0, "note": "이벤트 없음"}

    res: dict = {"n": int(len(events)),
                 "period": [str(events["date"].min()),
                            str(events["date"].max())],
                 "tickers": int(events["ticker"].nunique())}

    if "raw_events" in events.attrs:
        res["raw_events"] = int(events.attrs["raw_events"])
    res["event_window"] = int(bt.event_window)
    # 비교 지표: 전종목 중위 대비 초과수익(alpha_h = [0] ex_med_h).
    # 전략·대조군 모두 이 컬럼끼리 비교한다.
    res["metric"] = "alpha = net - 전종목 중위 (ex_med)"

    by_h = {}
    for h in bt.horizons:
        col, ac, mc = f"net_{h}", f"alpha_{h}", f"mae_{h}"
        if col not in events:
            continue
        item = {"net": _stats(events[col])}
        if ac in events:
            item["alpha"] = _stats(events[ac])
        if f"ex_idx_{h}" in events:
            item["ex_idx"] = _stats(events[f"ex_idx_{h}"])
        if mc in events:
            item["mae"] = _stats(events[mc])
        by_h[h] = item
    res["by_horizon"] = by_h

    # 등급별 / 트랙별 / 국면별 (기준 구간은 중간 horizon)
    ref = bt.horizons[len(bt.horizons) // 2]
    ac = f"alpha_{ref}"
    res["ref_horizon"] = ref
    for key, col in (("by_grade", "grade"), ("by_track", "track"),
                     ("by_regime", "regime"), ("by_regime_v2", "regime_v2")):
        if col in events and ac in events:
            res[key] = {str(k): _stats(g[ac])
                        for k, g in events.groupby(col)}

    # 대조군 — **같은 지표**(alpha_ref)끼리 비교한다. 예전에는 전략 alpha 를
    # 대조군 net 과 비교했다(단위가 다른 두 숫자). 순수익은 참고로만 남긴다.
    ctrl = {}
    ctrl_net = {"strategy": _stats(events[f"net_{ref}"])
                if f"net_{ref}" in events else {}}
    for key, frame in (("random", rnd), ("rsi30", rsi)):
        if frame is None or frame.empty:
            continue
        if f"alpha_{ref}" in frame:
            ctrl[key] = _stats(frame[f"alpha_{ref}"])
        if f"net_{ref}" in frame:
            ctrl_net[key] = _stats(frame[f"net_{ref}"])
    res["controls"] = ctrl
    res["controls_net"] = ctrl_net
    # 시장 중위 자체의 초과수익은 정의상 0 이다. 그 구간 시장 중위의
    # **수익률**은 참고값으로 따로 둔다(대조군 비교에 쓰지 않는다).
    if f"mkt_{ref}" in events:
        res["market_ret"] = _stats(events[f"mkt_{ref}"])

    # 판정
    a = res.get("by_horizon", {}).get(ref, {}).get("alpha", {})
    verdict = []
    if a:
        if a.get("mean", 0) <= 0:
            verdict.append("시장 대비 초과수익이 0 이하다. 하락장 베타를 "
                           "실력으로 착각하고 있을 수 있다.")
        if a.get("win", 0) < 50:
            verdict.append("초과수익 승률이 50% 미만이다.")
    if ctrl.get("random") and a:
        if a.get("mean", 0) <= ctrl["random"].get("mean", 0):
            verdict.append("무작위 진입보다 낫지 않다.")
    if ctrl.get("rsi30") and a:
        if a.get("mean", 0) <= ctrl["rsi30"].get("mean", 0):
            verdict.append("단순 RSI<30 규칙보다 낫지 않다. "
                           "복잡도를 정당화할 수 없다.")
    res["warnings"] = verdict
    return res


def sweep_thresholds(events: pd.DataFrame, bt: BacktestConfig = BacktestConfig(),
                     grid: tuple = (6.5, 7.0, 7.5, 8.0, 8.5, 9.0)) -> pd.DataFrame:
    """점수 임계값 스윕.

    이벤트를 한 번 만들어두면 임계값은 사후 필터링으로 공짜로 스윕된다.
    재계산이 필요 없다.
    """
    if events is None or events.empty:
        return pd.DataFrame()
    ref = bt.horizons[len(bt.horizons) // 2]
    ac = f"alpha_{ref}"
    if ac not in events:
        return pd.DataFrame()
    rows = []
    for th in grid:
        for col, label in (("value", "매집"), ("trend", "추세")):
            if col not in events:
                continue
            sub = events[events[col] >= th]
            st = _stats(sub[ac])
            if st:
                rows.append({"track": label, "threshold": th, **st})
    return pd.DataFrame(rows)


def summarize_exits(sim: pd.DataFrame) -> dict:
    """청산 규칙 시뮬 집계. 어느 계층이 실제로 일하는지 본다."""
    if sim is None or sim.empty:
        return {"n": 0}
    out = {"n": int(len(sim)),
           "net": _stats(sim["net"]),
           "mae": _stats(sim["mae"]),
           "held_days": {"mean": round(float(sim["held_days"].mean()), 1),
                         "median": float(sim["held_days"].median())}}
    by_layer = {}
    for k, g in sim.groupby("layer"):
        by_layer[int(k)] = {"n": int(len(g)), **_stats(g["net"]),
                            "held_mean": round(float(g["held_days"].mean()), 1)}
    out["by_layer"] = by_layer
    out["by_rule"] = {str(k): int(len(g)) for k, g in sim.groupby("rule")}
    return out


def exits_vs_hold(sim: pd.DataFrame, by: str | None = "regime_v2") -> dict:
    """청산 엔진 vs 같은 이벤트 고정 보유(hold_compare일), 그룹별.

    두 값이 모두 있는 이벤트만 쓴다(같은 표본). 칸은 `events.stat_cell`
    (20건 미만 enough=False → "표본 부족").
    반환 {그룹: {"engine": cell, "hold": cell, "engine_all": cell}}.
    그룹 "전체" 는 항상 있다.
    """
    if sim is None or sim.empty:
        return {}
    df = sim.copy()
    if "hold_net" not in df:
        df["hold_net"] = np.nan
    both = df[df["net"].notna() & df["hold_net"].notna()]
    out = {"전체": {"engine": stat_cell(both["net"]),
                    "hold": stat_cell(both["hold_net"]),
                    "engine_all": stat_cell(df["net"])}}
    if by and by in df:
        keys = df[by].fillna(NO_REGIME)
        bkeys = both[by].fillna(NO_REGIME)
        for k in sorted(keys.unique(), key=str):
            g, gb = df[keys == k], both[bkeys == k]
            out[str(k)] = {"engine": stat_cell(gb["net"]),
                           "hold": stat_cell(gb["hold_net"]),
                           "engine_all": stat_cell(g["net"])}
    return out
