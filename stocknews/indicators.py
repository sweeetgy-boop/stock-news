# -*- coding: utf-8 -*-
"""20/40/60일 이동평균 골든크로스 판정.

설계 요지
---------
"골든크로스가 났다"만 보면 신호가 하루에 수백 개 뜬다. 실제로 돈이 되는
골든크로스는 아래 5개 조건이 겹칠 때다. 그래서 크로스 발생 자체는
10점 중 3점만 배정하고, 나머지 7점을 품질 조건에 배분한다.

  1) 어느 쌍이 교차했나      : 20x60 > 40x60 > 20x40
  2) 며칠 전에 났나(신선도)  : D+0~D+5 가 알파, D+20 이면 무의미
  3) 교차 시점 이평선 밀집도 : 3선이 3% 안에 모여 있다가 터진 크로스가 강력
  4) 장기선(60) 기울기       : 60선이 우하향이면 하락 추세 중 일시 반등
  5) 거래량 확증             : 크로스일 거래량이 20일 평균의 1.5배 이상

그리고 크로스 후 종가가 다시 60선 아래로 떨어진 횟수(휩쏘)를 세어
가짜 크로스를 감점한다.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .config import MAConfig
from .contracts import CrossEvent, TrendSignal

__all__ = [
    "moving_averages",
    "cross_series",
    "last_cross",
    "slope_pct",
    "convergence_pct",
    "alignment_of",
    "evaluate_trend",
]


def moving_averages(close: pd.Series, cfg: MAConfig) -> pd.DataFrame:
    """단·중·장기 단순이동평균."""
    return pd.DataFrame(
        {
            f"ma{cfg.short}": close.rolling(cfg.short).mean(),
            f"ma{cfg.mid}": close.rolling(cfg.mid).mean(),
            f"ma{cfg.long}": close.rolling(cfg.long).mean(),
        }
    )


def cross_series(fast: pd.Series, slow: pd.Series) -> pd.Series:
    """교차 시계열. +1=골든크로스, -1=데드크로스, 0=없음.

    직전봉 차이가 0 이하였다가 당봉에 양수로 바뀌는 순간만 +1 로 잡는다.
    이렇게 해야 두 선이 붙어서 미세하게 진동할 때 신호가 연속으로
    발생하는 것을 막을 수 있다.
    """
    diff = (fast - slow).astype("float64")
    prev = diff.shift(1)
    out = pd.Series(0, index=diff.index, dtype="int8")
    out[(prev <= 0) & (diff > 0)] = 1
    out[(prev >= 0) & (diff < 0)] = -1
    # 이평선이 아직 안 채워진 구간(NaN)은 신호 없음으로 강제
    out[diff.isna() | prev.isna()] = 0
    return out


def last_cross(fast: pd.Series, slow: pd.Series, pair: str,
               n_fast: int, n_slow: int) -> CrossEvent | None:
    """가장 최근 교차 1건."""
    cs = cross_series(fast, slow)
    nz = cs[cs != 0]
    if nz.empty:
        return None
    date = nz.index[-1]
    bars_ago = int(len(cs.index) - 1 - cs.index.get_loc(date))
    return CrossEvent(
        pair=pair,
        kind="GOLDEN" if int(nz.iloc[-1]) == 1 else "DEAD",
        date=date,
        bars_ago=bars_ago,
        fast=n_fast,
        slow=n_slow,
    )


def slope_pct(s: pd.Series, window: int) -> float:
    """window 거래일 전 대비 변화율(%). 이평선 방향성 판정용."""
    s = s.dropna()
    if len(s) < window + 1:
        return float("nan")
    base = float(s.iloc[-window - 1])
    if base == 0:
        return float("nan")
    return (float(s.iloc[-1]) / base - 1.0) * 100.0


def convergence_pct(ma_row: pd.Series, price: float) -> float:
    """3개 이평선의 최대-최소 폭을 주가로 나눈 밀집도(%)."""
    vals = [float(v) for v in ma_row.tolist() if pd.notna(v)]
    if len(vals) < 2 or not price or price <= 0:
        return float("nan")
    return (max(vals) - min(vals)) / price * 100.0


def alignment_of(ma_row: pd.Series, cfg: MAConfig) -> str:
    """정배열 / 역배열 / 혼재."""
    s = ma_row.get(f"ma{cfg.short}")
    m = ma_row.get(f"ma{cfg.mid}")
    l = ma_row.get(f"ma{cfg.long}")
    if any(pd.isna(x) for x in (s, m, l)):
        return "UNKNOWN"
    if s > m > l:
        return "GOLDEN"
    if s < m < l:
        return "DEAD"
    return "MIXED"


def _freshness_points(bars_ago: int, cfg: MAConfig, full: float = 2.0) -> float:
    """신선도 점수. fresh_days 이내 만점, stale_days 에서 0점으로 선형 감쇠."""
    if bars_ago <= cfg.fresh_days:
        return full
    if bars_ago >= cfg.stale_days:
        return 0.0
    span = cfg.stale_days - cfg.fresh_days
    return round(full * (1.0 - (bars_ago - cfg.fresh_days) / span), 2)


def _pick_best_cross(mas: pd.DataFrame, cfg: MAConfig) -> tuple[CrossEvent | None, float]:
    """세 쌍의 최근 골든크로스 중 (가중치 x 신선도)가 가장 높은 것을 고른다.

    가중치는 추세 전환 신뢰도 순서다. 20x60은 단기선이 장기선을 뚫는
    가장 결정적인 신호이고, 40x60은 신뢰도는 높지만 늦게 나오며,
    20x40은 가장 자주 나오지만 가장 약하다.
    """
    weights = {
        (cfg.short, cfg.long): 3.0,
        (cfg.mid, cfg.long): 2.5,
        (cfg.short, cfg.mid): 2.0,
    }
    best: CrossEvent | None = None
    best_pts = 0.0
    best_rank = -1.0
    for (nf, ns), w in weights.items():
        ev = last_cross(mas[f"ma{nf}"], mas[f"ma{ns}"], f"{nf}x{ns}", nf, ns)
        if ev is None or ev.kind != "GOLDEN":
            continue
        rank = w * max(_freshness_points(ev.bars_ago, cfg, full=1.0), 0.01)
        if rank > best_rank:
            best, best_pts, best_rank = ev, w, rank
    return best, best_pts


def _whipsaw_count(close: pd.Series, ma_long: pd.Series, since_bars: int) -> int:
    """크로스 이후 종가가 장기선 아래로 이탈한 거래일 수."""
    if since_bars <= 0:
        return 0
    tail_close = close.iloc[-since_bars - 1:]
    tail_ma = ma_long.iloc[-since_bars - 1:]
    return int((tail_close < tail_ma).sum())


def evaluate_trend(ohlcv: pd.DataFrame, cfg: MAConfig) -> TrendSignal | None:
    """골든크로스 종합 점수(0~10) 산출.

    ohlcv : index=거래일(오름차순), columns 최소 ['종가','거래량']
    """
    need = cfg.long + cfg.slope_window + 2
    if ohlcv is None or len(ohlcv) < need:
        return None
    if "종가" not in ohlcv or "거래량" not in ohlcv:
        raise KeyError("ohlcv 에 '종가','거래량' 컬럼이 필요합니다")

    close = ohlcv["종가"].astype("float64")
    volume = ohlcv["거래량"].astype("float64")
    mas = moving_averages(close, cfg)
    last = mas.iloc[-1]
    price = float(close.iloc[-1])

    align = alignment_of(last, cfg)
    best, cross_pts = _pick_best_cross(mas, cfg)

    # 밀집도는 크로스 시점 기준. 크로스가 없으면 오늘 기준(임박 판정용).
    if best is not None:
        conv = convergence_pct(mas.loc[best.date], float(close.loc[best.date]))
    else:
        conv = convergence_pct(last, price)

    slope_long = slope_pct(mas[f"ma{cfg.long}"], cfg.slope_window)

    vol_ma20 = volume.rolling(20).mean()
    if best is not None:
        denom = float(vol_ma20.loc[best.date]) if pd.notna(vol_ma20.loc[best.date]) else np.nan
        vol_ratio = float(volume.loc[best.date]) / denom if denom and denom > 0 else float("nan")
    else:
        denom = float(vol_ma20.iloc[-1])
        vol_ratio = float(volume.iloc[-1]) / denom if denom > 0 else float("nan")

    whip = _whipsaw_count(close, mas[f"ma{cfg.long}"], best.bars_ago if best else 0)

    bd: dict = {}

    # ① 교차 쌍 가중치 (최대 3.0)
    bd["cross_pair"] = round(cross_pts, 2)

    # ② 신선도 (최대 2.0)
    bd["freshness"] = _freshness_points(best.bars_ago, cfg) if best else 0.0

    # ③ 밀집도 (최대 2.0)
    if pd.isna(conv):
        bd["convergence"] = 0.0
    elif conv <= cfg.convergence_tight_pct:
        bd["convergence"] = 2.0
    elif conv <= cfg.convergence_loose_pct:
        bd["convergence"] = 1.0
    else:
        bd["convergence"] = 0.0

    # ④ 장기선 기울기 (최대 1.5)
    if pd.isna(slope_long):
        bd["slope_long"] = 0.0
    elif slope_long >= 0.0:
        bd["slope_long"] = 1.5
    elif slope_long >= -0.5:
        bd["slope_long"] = 0.75
    else:
        bd["slope_long"] = 0.0

    # ⑤ 거래량 확증 (최대 1.5)
    if pd.isna(vol_ratio):
        bd["volume"] = 0.0
    elif vol_ratio >= cfg.vol_surge_strong:
        bd["volume"] = 1.5
    elif vol_ratio >= cfg.vol_surge_weak:
        bd["volume"] = 0.75
    else:
        bd["volume"] = 0.0

    score = sum(bd.values())

    # 휩쏘 감점: 허용치를 넘긴 이탈 1회당 -0.5
    if whip > cfg.whipsaw_tolerance:
        penalty = 0.5 * (whip - cfg.whipsaw_tolerance)
        bd["whipsaw_penalty"] = -round(penalty, 2)
        score -= penalty

    # 종가가 장기선 아래면 추세 전환이 아직 확증되지 않은 것으로 본다
    ma_long_now = float(last[f"ma{cfg.long}"])
    if price < ma_long_now:
        bd["below_ma_long"] = -1.0
        score -= 1.0

    score = float(max(0.0, min(10.0, round(score, 2))))

    return TrendSignal(
        score=score,
        alignment=align,
        best_cross=best,
        ma_short=float(last[f"ma{cfg.short}"]),
        ma_mid=float(last[f"ma{cfg.mid}"]),
        ma_long=ma_long_now,
        convergence_pct=float(conv) if pd.notna(conv) else float("nan"),
        slope_long_pct=float(slope_long) if pd.notna(slope_long) else float("nan"),
        vol_ratio_at_cross=float(vol_ratio) if pd.notna(vol_ratio) else float("nan"),
        whipsaw_count=whip,
        breakdown=bd,
    )
