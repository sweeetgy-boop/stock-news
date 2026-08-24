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

from .config import Config, DEFAULT
from .contracts import Position
from .exits import evaluate_position
from .indicators import moving_averages
from .liquidation import liquidation_band
from .screener import screen_one

log = logging.getLogger(__name__)

__all__ = ["BacktestConfig", "run_backtest", "summarize", "sweep_thresholds",
           "simulate_exit_rules"]


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


# ══════════════════════════ 보조 ══════════════════════════
def _market_close_matrix(store, tickers: list[str]) -> pd.DataFrame:
    """종가 피벗. 시장 중위 수익률(대조군)과 국면 판정에 쓴다."""
    pm = store.price_matrix(days=100000)
    if pm is None or pm.empty:
        return pd.DataFrame()
    keep = [t for t in tickers if t in pm.columns]
    return pm[keep] if keep else pm


def _median_forward(pm: pd.DataFrame, i0: int, h: int) -> float:
    """구간 [i0, i0+h] 의 전종목 중위 수익률(%). 시장 대조군."""
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


# ══════════════════════════ 본체 ══════════════════════════
def run_backtest(store, tickers: dict, cfg: Config = DEFAULT,
                 bt: BacktestConfig = BacktestConfig(),
                 progress_every: int = 50) -> pd.DataFrame:
    """이벤트 표를 만든다. 한 행 = (종목, 판정일, 등급) 1건."""
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

            for h in bt.horizons:
                f = _fwd(ohlcv, i, h, bt.cost_pct)
                if not f:
                    continue
                base[f"ret_{h}"] = f["ret"]
                base[f"net_{h}"] = f["net"]
                base[f"mae_{h}"] = f["mae"]
                base[f"mkt_{h}"] = (_median_forward(pm, mi, h)
                                    if mi is not None else np.nan)
                base[f"alpha_{h}"] = base[f"net_{h}"] - base[f"mkt_{h}"]
            rows.append(base)

    df = pd.DataFrame(rows)
    log.info("백테스트 이벤트 %d건 (종목 %d, 간격 %d거래일)",
             len(df), len(tickers), bt.step)
    return df


def control_random(store, tickers: dict, n_events: int,
                   bt: BacktestConfig = BacktestConfig()) -> pd.DataFrame:
    """무작위 진입 대조군. 같은 종목 풀, 무작위 날짜, 같은 표본 크기."""
    rng = random.Random(bt.seed + 1)
    codes = [c for c in tickers]
    rng.shuffle(codes)
    rows: list[dict] = []
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
        row = {"ticker": code, "date": ohlcv.index[i].strftime("%Y-%m-%d")}
        ok = False
        for h in bt.horizons:
            f = _fwd(ohlcv, i, h, bt.cost_pct)
            if f:
                row[f"net_{h}"] = f["net"]
                row[f"mae_{h}"] = f["mae"]
                ok = True
        if ok:
            rows.append(row)
    return pd.DataFrame(rows)


def control_rsi(store, tickers: dict, threshold: float = 30.0,
                bt: BacktestConfig = BacktestConfig()) -> pd.DataFrame:
    """단순 RSI<30 대조군. 우리 로직이 이보다 나은지 본다."""
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
            ok = False
            for h in bt.horizons:
                f = _fwd(ohlcv, i, h, bt.cost_pct)
                if f:
                    row[f"net_{h}"] = f["net"]
                    row[f"mae_{h}"] = f["mae"]
                    ok = True
            if ok:
                rows.append(row)
    return pd.DataFrame(rows)


# ══════════════════════════ 청산 규칙 시뮬레이션 ══════════════════════════
def simulate_exit_rules(store, events: pd.DataFrame, cfg: Config = DEFAULT,
                        bt: BacktestConfig = BacktestConfig()) -> pd.DataFrame:
    """고정 보유가 아니라 **실제 청산 엔진**으로 청산한다.

    이게 청산 파라미터(익절폭·트레일링폭·시간스톱·밴드 이탈 확인일수)를
    검증하는 유일한 방법이다. 고정 5일 보유 측정은 실제 운용과 대응되지
    않는다.
    """
    if events is None or events.empty:
        return pd.DataFrame()

    out: list[dict] = []
    cache: dict[str, pd.DataFrame] = {}
    for _, ev in events.iterrows():
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
            "entry": entry, "exit": exit_px, "held_days": held,
            "layer": layer, "rule": rule,
            "ret": ret, "net": ret - bt.cost_pct,
            "mae": (seg_low / entry - 1.0) * 100.0,
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

    by_h = {}
    for h in bt.horizons:
        col, ac, mc = f"net_{h}", f"alpha_{h}", f"mae_{h}"
        if col not in events:
            continue
        item = {"net": _stats(events[col])}
        if ac in events:
            item["alpha"] = _stats(events[ac])
        if mc in events:
            item["mae"] = _stats(events[mc])
        by_h[h] = item
    res["by_horizon"] = by_h

    # 등급별 / 트랙별 / 국면별 (기준 구간은 중간 horizon)
    ref = bt.horizons[len(bt.horizons) // 2]
    ac = f"alpha_{ref}"
    res["ref_horizon"] = ref
    for key, col in (("by_grade", "grade"), ("by_track", "track"),
                     ("by_regime", "regime")):
        if col in events and ac in events:
            res[key] = {str(k): _stats(g[ac])
                        for k, g in events.groupby(col)}

    # 대조군
    ctrl = {}
    if rnd is not None and not rnd.empty and f"net_{ref}" in rnd:
        ctrl["random"] = _stats(rnd[f"net_{ref}"])
    if rsi is not None and not rsi.empty and f"net_{ref}" in rsi:
        ctrl["rsi30"] = _stats(rsi[f"net_{ref}"])
    if f"mkt_{ref}" in events:
        ctrl["market"] = _stats(events[f"mkt_{ref}"])
    res["controls"] = ctrl

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
