# -*- coding: utf-8 -*-
"""신용 강제청산(반대매매) 밴드 및 청산압력점수(LPS).

담보유지비율 = 주식평가액 / 융자금 >= 1.40
  => Pc = P0 * 1.40 * L      (L = 융자비율)

  L=0.60 -> 0.840*P0  (-16.0%)  마진콜 개시
  L=0.50 -> 0.700*P0  (-30.0%)  대량 청산 집중 (밴드 정중앙, r=0.50)
  L=0.40 -> 0.560*P0  (-44.0%)  연쇄청산 언더슈팅

'-30%' 는 융자비율 50% 한 케이스일 뿐이므로 상수로 박지 않고
밴드로 계산한 뒤 정규화 위치 r 로 채점한다.

반대매매 집행 타임라인(제도 고정):
  D일 종가 담보부족 -> D+1 통보 -> D+2 09:00 동시호가 하한가 처분
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .config import CreditConfig
from .contracts import LiquidationSignal

__all__ = ["liquidation_band", "band_position", "margin_call_due_dates",
           "evaluate_liquidation"]


def liquidation_band(p0: float, cfg: CreditConfig) -> dict:
    """청산 밴드 상단/중심/하단 가격."""
    m = cfg.maint_ratio
    return {
        "hi": p0 * m * cfg.loan_ratio_hi,
        "mid": p0 * m * cfg.loan_ratio_mid,
        "lo": p0 * m * cfg.loan_ratio_lo,
    }


def band_position(price: float, band: dict) -> float:
    """밴드 내 정규화 위치. 0=하단(-44%), 1=상단(-16%), 0.5=중심(-30%)."""
    span = band["hi"] - band["lo"]
    if span <= 0:
        return float("nan")
    return (price - band["lo"]) / span


def margin_call_due_dates(ohlcv: pd.DataFrame, drop_pct: float = -0.05,
                          offset_bd: int = 2) -> list:
    """종가 급락일을 찾아 반대매매 집행 예상일(D+offset 거래일)을 반환.

    BDay 대신 실제 거래일 인덱스를 쓴다. 한국 공휴일이 자동 반영된다.
    """
    close = ohlcv["종가"].astype("float64")
    ret = close.pct_change()
    idx = ohlcv.index
    out = []
    for d in ret[ret <= drop_pct].index:
        i = int(idx.get_loc(d))
        if i + offset_bd < len(idx):
            out.append(idx[i + offset_bd])
    return out


def _short_trend(shorting: pd.DataFrame | None, shift: int) -> tuple[str, float]:
    """공매도 잔고 추세. (라벨, 5일 변화율%) 반환."""
    if shorting is None or shorting.empty:
        return "중립", float("nan")
    # pykrx get_shorting_balance_by_date 는 '공매도잔고' 컬럼을 준다.
    # 다른 공급원 대비 대체 컬럼명도 함께 탐색한다.
    candidates = ("공매도잔고", "잔고수량", "공매도잔고수량", "balance")
    col = next((c for c in candidates if c in shorting), None)
    if col is None:
        return "중립", float("nan")
    s = shorting[col].astype("float64").shift(shift).dropna()
    if len(s) < 6:
        return "중립", float("nan")
    chg = (float(s.iloc[-1]) / float(s.iloc[-6]) - 1.0) * 100.0
    consec_down = bool((s.diff().tail(5) < 0).all())
    if consec_down or chg <= -10.0:
        return "감소전환", chg
    if chg >= 10.0:
        return "증가", chg
    return "중립", chg


def evaluate_liquidation(ohlcv: pd.DataFrame,
                         p0: float,
                         basis_method: str,
                         basis_confidence: str,
                         cfg: CreditConfig,
                         credit_ratio: float | None = None,
                         shorting: pd.DataFrame | None = None) -> LiquidationSignal:
    """청산압력점수 LPS(0~10) 산출.

    credit_ratio : 신용잔고율(%) = 신용잔고주식수 / 상장주식수 * 100
                   None 이면 ① 항목을 프록시로 간주해 상한 2.5점 적용
    """
    close = ohlcv["종가"].astype("float64")
    volume = ohlcv["거래량"].astype("float64")
    price = float(close.iloc[-1])

    band = liquidation_band(p0, cfg)
    r = band_position(price, band)

    vol_ma20 = float(volume.rolling(20).mean().iloc[-1])
    vol_ratio = float(volume.iloc[-1]) / vol_ma20 if vol_ma20 > 0 else float("nan")

    strend, _ = _short_trend(shorting, cfg.short_shift)
    due = set(margin_call_due_dates(ohlcv))
    is_due = ohlcv.index[-1] in due

    bd: dict = {}

    # ① 신용 과열도 (최대 4.0 / 프록시일 때 2.5 캡)
    if credit_ratio is None or not np.isfinite(credit_ratio):
        bd["credit_heat"] = 1.25
        bd["credit_heat_note"] = "프록시(실측 신용잔고 없음)"
    else:
        if credit_ratio >= 5.0:
            bd["credit_heat"] = 4.0
        elif credit_ratio >= 4.0:
            bd["credit_heat"] = 3.0
        elif credit_ratio >= 3.0:
            bd["credit_heat"] = 2.0
        elif credit_ratio >= 2.0:
            bd["credit_heat"] = 1.0
        else:
            bd["credit_heat"] = 0.0

    # ② 청산밴드 진입도 (최대 3.0) — r=0.25~0.65 가 -30% 스윗스팟
    if not np.isfinite(r):
        bd["band"] = 0.0
    elif r > 1.0:
        bd["band"] = 0.0
    elif r >= 0.65:
        bd["band"] = 1.5
    elif r >= 0.25:
        bd["band"] = 3.0
    elif r >= 0.0:
        bd["band"] = 2.0
    else:
        bd["band"] = 1.0

    # ③ 투매 확증 (최대 2.0)
    surge = 1.0 if (np.isfinite(vol_ratio) and vol_ratio >= 3.0) else 0.0
    gap = 0.0
    if len(ohlcv) >= 2 and "시가" in ohlcv and "저가" in ohlcv:
        o = float(ohlcv["시가"].iloc[-1])
        lo = float(ohlcv["저가"].iloc[-1])
        prev = float(close.iloc[-2])
        if prev > 0 and (o / prev - 1.0) <= -0.05 and band["lo"] <= lo <= band["hi"]:
            gap = 0.5
    bd["panic_volume"] = surge
    bd["panic_gap"] = gap
    bd["margin_due"] = 0.5 if is_due else 0.0

    # ④ 숏커버 전환 (최대 1.0)
    bd["short_cover"] = 1.0 if strend == "감소전환" else 0.0

    score = float(max(0.0, min(10.0, round(
        bd["credit_heat"] + bd["band"] + bd["panic_volume"]
        + bd["panic_gap"] + bd["margin_due"] + bd["short_cover"], 2))))

    return LiquidationSignal(
        score=score,
        cost_basis=float(p0),
        basis_method=basis_method,
        credit_ratio=float(credit_ratio) if credit_ratio is not None
        and np.isfinite(credit_ratio) else float("nan"),
        band_hi=float(band["hi"]),
        band_mid=float(band["mid"]),
        band_lo=float(band["lo"]),
        band_pos=float(r) if np.isfinite(r) else float("nan"),
        vol_ratio=float(vol_ratio) if np.isfinite(vol_ratio) else float("nan"),
        short_trend=strend,
        is_margin_due=bool(is_due),
        confidence=basis_confidence,
        breakdown=bd,
    )
