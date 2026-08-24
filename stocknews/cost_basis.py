# -*- coding: utf-8 -*-
"""개미 매집 평균단가 P0 추정.

-30% 곱셈은 쉽다. 어려운 건 "개미들이 신용으로 왕창 들어온 그 단가"를
눈이 아니라 숫자로 뽑는 것이다. 정확도 순으로 3단 폴백을 쓴다.

  방법 A : 신용잔고 증가분 가중 VWAP  ← 문자 그대로 '신용 평균단가'
  방법 B : 개인 순매수대금 가중 VWAP  ← 신용잔고 데이터 없을 때
  방법 C : 매물대 POC(최다 거래 가격대) ← 최후 폴백, 단독 사용 금지

주의: 신용잔고는 익영업일 공시다. 반드시 shift 후 넣어야 한다.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

__all__ = ["vwap_series", "from_credit_balance", "from_retail_netbuy",
           "from_volume_profile", "estimate_cost_basis"]


def vwap_series(ohlcv: pd.DataFrame) -> pd.Series:
    """일별 VWAP. 거래대금이 없으면 (고+저+종)/3 로 근사."""
    if "거래대금" in ohlcv and "거래량" in ohlcv:
        vol = ohlcv["거래량"].astype("float64").replace(0, np.nan)
        v = ohlcv["거래대금"].astype("float64") / vol
        if v.notna().sum() >= max(10, int(len(ohlcv) * 0.5)):
            return v
    return (ohlcv["고가"] + ohlcv["저가"] + ohlcv["종가"]).astype("float64") / 3.0


def from_credit_balance(ohlcv: pd.DataFrame, credit: pd.DataFrame,
                        lookback: int = 90, shift: int = 1) -> float | None:
    """방법 A. 신용잔고가 늘어난 날의 VWAP를 증가분으로 가중평균.

    credit : index=거래일, '신용잔고주식수' 컬럼 필요
    shift  : 공시 지연(영업일). 룩어헤드 편향 방지용으로 기본 1.
    """
    if credit is None or credit.empty or "신용잔고주식수" not in credit:
        return None
    bal = credit["신용잔고주식수"].astype("float64").shift(shift)
    df = pd.DataFrame({"vwap": vwap_series(ohlcv)}).join(bal, how="inner").tail(lookback)
    inflow = df["신용잔고주식수"].diff().clip(lower=0).fillna(0.0)
    total = float(inflow.sum())
    if total <= 0 or df["vwap"].isna().all():
        return None
    w = inflow.where(df["vwap"].notna(), 0.0)
    if float(w.sum()) <= 0:
        return None
    return float((df["vwap"].fillna(0.0) * w).sum() / w.sum())


def from_retail_netbuy(ohlcv: pd.DataFrame, investor: pd.DataFrame,
                       lookback: int = 90, col: str = "개인") -> float | None:
    """방법 B. 개인 순매수대금이 (+)인 날의 VWAP를 순매수대금으로 가중평균."""
    if investor is None or investor.empty or col not in investor:
        return None
    df = pd.DataFrame({"vwap": vwap_series(ohlcv)}).join(
        investor[[col]].astype("float64"), how="inner"
    ).tail(lookback)
    w = df[col].clip(lower=0).fillna(0.0).where(df["vwap"].notna(), 0.0)
    if float(w.sum()) <= 0:
        return None
    return float((df["vwap"].fillna(0.0) * w).sum() / w.sum())


def from_volume_profile(ohlcv: pd.DataFrame, lookback: int = 120,
                        bins: int = 40) -> float | None:
    """방법 C. 가격대별 거래량 분포의 최다 구간(POC) 중심가."""
    df = ohlcv.tail(lookback)
    if len(df) < 30:
        return None
    price = ((df["고가"] + df["저가"] + df["종가"]) / 3.0).astype("float64")
    vol = df["거래량"].astype("float64")
    lo, hi = float(price.min()), float(price.max())
    if not np.isfinite(lo) or not np.isfinite(hi) or hi <= lo:
        return None
    edges = np.linspace(lo, hi, bins + 1)
    idx = np.clip(np.digitize(price.to_numpy(), edges) - 1, 0, bins - 1)
    agg = np.zeros(bins, dtype="float64")
    np.add.at(agg, idx, vol.to_numpy())
    if agg.sum() <= 0:
        return None
    k = int(agg.argmax())
    return float((edges[k] + edges[k + 1]) / 2.0)


def estimate_cost_basis(ohlcv: pd.DataFrame,
                        credit: pd.DataFrame | None = None,
                        investor: pd.DataFrame | None = None,
                        lookback: int = 90,
                        credit_shift: int = 1) -> tuple[float, str, str]:
    """A→B→C 폴백. (평균단가, 사용방법, 신뢰도) 반환.

    세 방법의 산출값이 서로 얼마나 모이는지로 신뢰도를 정한다.
    3% 이내로 수렴하면 HIGH, 10% 이상 벌어지면 LOW.
    """
    cands: dict[str, float] = {}
    for key, val in (
        ("A", from_credit_balance(ohlcv, credit, lookback, credit_shift)),
        ("B", from_retail_netbuy(ohlcv, investor, lookback)),
        ("C", from_volume_profile(ohlcv, max(lookback, 120))),
    ):
        if val is not None and np.isfinite(val) and val > 0:
            cands[key] = float(val)

    if not cands:
        raise ValueError("평균단가 추정 불가: 입력 데이터 부족")

    method = "A" if "A" in cands else ("B" if "B" in cands else "C")
    p0 = cands[method]

    if len(cands) >= 2:
        vals = list(cands.values())
        spread = (max(vals) - min(vals)) / p0 * 100.0
        confidence = "HIGH" if spread <= 3.0 else ("MID" if spread <= 10.0 else "LOW")
    else:
        confidence = "MID" if method == "A" else "LOW"

    # 개인 순매수/매물대 프록시는 신용 평균단가와 등가가 아니므로 한 단계 낮춘다
    if method != "A" and confidence == "HIGH":
        confidence = "MID"

    return float(p0), method, confidence
