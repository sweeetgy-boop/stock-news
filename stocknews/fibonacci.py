# -*- coding: utf-8 -*-
"""최근 1년 고점 기준 피보나치 되돌림 판정 및 '레벨 이하' 스크리닝.

흔한 구현 오류
--------------
피보나치 되돌림을 "1년 최고가 - 1년 최저가"로 계산하는 코드가 많은데
이건 틀렸다. 되돌림은 '직전 상승 파동'에 대해 재는 것이므로 스윙
저점은 반드시 **고점 이전 구간**의 저점이어야 한다. 고점 이후에
만들어진 저점을 섞어 쓰면 파동 폭이 부풀려져 0.618 선이 실제보다
훨씬 아래로 내려가고, 결과적으로 신호가 늦게 뜬다.

    H  = 최근 252거래일 최고가            (고점)
    L0 = H 발생일 '이전' 구간의 최저가     (상승 파동 출발점)  ← 핵심
    swing = H - L0
    되돌림 레벨(k) = H - k * swing

되돌림 진행률 ratio = (H - 현재가) / swing 로 정의하면
ratio >= 0.618 이 곧 "0.618 레벨 이하로 내려왔다"와 같다.
ratio > 1.0 이면 상승 파동의 출발점마저 깨진 것이므로 되돌림이
아니라 추세 파괴로 분류한다(wave_broken).
"""
from __future__ import annotations

import pandas as pd

from .config import FibConfig
from .contracts import FibSignal

__all__ = ["fib_levels", "evaluate_fib", "is_below_level"]


def fib_levels(high: float, swing: float, levels: tuple[float, ...]) -> dict:
    """되돌림 비율 -> 가격."""
    return {k: high - k * swing for k in levels}


def is_below_level(price: float, high: float, swing: float, k: float) -> bool:
    """현재가가 k 되돌림 레벨 이하인가."""
    if swing <= 0:
        return False
    return price <= (high - k * swing)


def _zone_label(ratio: float, levels: tuple[float, ...]) -> str:
    """현재 되돌림 진행률이 어느 레벨 구간에 있는지."""
    ks = sorted(levels)
    if ratio < ks[0]:
        return f"고점 ~ {ks[0]:.3f}"
    for lo, hi in zip(ks, ks[1:]):
        if lo <= ratio < hi:
            return f"{lo:.3f} ~ {hi:.3f}"
    return f"{ks[-1]:.3f} 이하 (파동 붕괴)"


def _rebound_points(ohlcv: pd.DataFrame) -> tuple[float, dict]:
    """바닥에서 반등 조짐이 있는지 (최대 2.0점).

    피보 레벨 '이하'라는 것만으로는 계속 흘러내리는 종목을 걸러낼 수
    없다. 5일선 위 회복 + 5일선 상향 전환을 최소한의 확증으로 쓴다.
    """
    close = ohlcv["종가"].astype("float64")
    ma5 = close.rolling(5).mean()
    if len(close) < 8 or pd.isna(ma5.iloc[-1]) or pd.isna(ma5.iloc[-4]):
        return 0.0, {"above_ma5": 0.0, "ma5_turning_up": 0.0}
    pts = {}
    pts["above_ma5"] = 1.0 if float(close.iloc[-1]) > float(ma5.iloc[-1]) else 0.0
    pts["ma5_turning_up"] = 1.0 if float(ma5.iloc[-1]) > float(ma5.iloc[-4]) else 0.0
    return sum(pts.values()), pts


def evaluate_fib(ohlcv: pd.DataFrame, cfg: FibConfig) -> FibSignal | None:
    """1년 고점 기준 피보나치 되돌림 종합 점수(0~10) 산출.

    ohlcv : index=거래일(오름차순), columns 최소 ['고가','저가','종가']
    """
    if ohlcv is None or len(ohlcv) < 60:
        return None
    for col in ("고가", "저가", "종가"):
        if col not in ohlcv:
            raise KeyError(f"ohlcv 에 '{col}' 컬럼이 필요합니다")

    df = ohlcv.tail(cfg.lookback)
    high_date = df["고가"].idxmax()
    high = float(df.loc[high_date, "고가"])
    pos = int(df.index.get_loc(high_date))
    high_age = int(len(df) - 1 - pos)

    # 스윙 저점: 고점 '이전' 구간의 최저가
    fallback = False
    if pos >= cfg.min_pre_bars:
        pre = df.iloc[: pos + 1]
        low_date = pre["저가"].idxmin()
        swing_low = float(pre.loc[low_date, "저가"])
    else:
        # 고점이 창 맨 앞에 있어 직전 파동이 창 밖인 경우 → 전체 저점으로 폴백
        low_date = df["저가"].idxmin()
        swing_low = float(df.loc[low_date, "저가"])
        fallback = True

    swing = high - swing_low
    if swing <= 0:
        return None

    price = float(df["종가"].iloc[-1])
    ratio = (high - price) / swing
    levels = fib_levels(high, swing, cfg.levels)
    target_price = levels[cfg.target]
    below_target = price <= target_price
    wave_broken = ratio > 1.0

    # 가장 가까운 레벨과의 이격
    nearest_level, nearest_gap = min(
        ((k, abs(price - v) / price * 100.0) for k, v in levels.items()),
        key=lambda t: t[1],
    )

    bd: dict = {}

    # ① 되돌림 깊이 (최대 4.0)
    if wave_broken:
        bd["depth"] = 2.0            # 깊긴 하지만 파동이 깨져 신뢰도 하락
    elif ratio >= 0.786:
        bd["depth"] = 4.0
    elif ratio >= 0.618:
        bd["depth"] = 3.5
    elif ratio >= 0.5:
        bd["depth"] = 2.5
    elif ratio >= 0.382:
        bd["depth"] = 1.5
    else:
        bd["depth"] = 0.0

    # ② 레벨 터치 정밀도 (최대 2.0)
    if nearest_gap <= cfg.touch_tol_pct:
        bd["touch"] = 2.0
    elif nearest_gap <= cfg.touch_loose_pct:
        bd["touch"] = 1.0
    else:
        bd["touch"] = 0.0

    # ③ 고점 신선도 (최대 2.0) — 고점이 오래되면 파동 자체가 무의미
    if high_age <= cfg.high_fresh_days:
        bd["high_fresh"] = 2.0
    elif high_age <= cfg.high_stale_days:
        bd["high_fresh"] = 1.0
    else:
        bd["high_fresh"] = 0.0

    # ④ 반등 조짐 (최대 2.0)
    reb, reb_detail = _rebound_points(df)
    bd["rebound"] = reb
    bd.update({f"rebound.{k}": v for k, v in reb_detail.items()})

    score = float(max(0.0, min(10.0, round(bd["depth"] + bd["touch"]
                                          + bd["high_fresh"] + bd["rebound"], 2))))

    # 신뢰도
    swing_pct = swing / high * 100.0
    if fallback or swing_pct < cfg.min_swing_pct or high_age > cfg.high_stale_days:
        confidence = "LOW"
    elif high_age <= cfg.high_fresh_days:
        confidence = "HIGH"
    else:
        confidence = "MID"

    return FibSignal(
        score=score,
        high=high,
        high_date=high_date,
        high_age=high_age,
        swing_low=swing_low,
        swing=swing,
        price=price,
        ratio=float(round(ratio, 4)),
        levels={k: float(v) for k, v in levels.items()},
        zone=_zone_label(ratio, cfg.levels),
        below_target=bool(below_target),
        nearest_level=float(nearest_level),
        nearest_gap_pct=float(round(nearest_gap, 2)),
        wave_broken=bool(wave_broken),
        confidence=confidence,
        breakdown=bd,
    )
