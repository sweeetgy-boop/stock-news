# -*- coding: utf-8 -*-
"""투 트랙 스크리너.

트랙을 왜 둘로 나누는가
----------------------
신용 청산 바닥 매수(-30% 밴드)는 하락 극단에서 사는 **역추세** 로직이고,
20/40/60 골든크로스는 상승 전환을 확인하고 사는 **순추세** 로직이다.
방향이 반대이므로 한 점수로 합산하면 안 된다. 청산 바닥에서는 이평선이
100% 역배열이라 골든크로스 점수가 0점이고, 골든크로스가 날 때쯤이면
이미 밴드를 벗어나 있다. 합산하면 둘 다 어중간한 종목만 살아남는다.

  트랙 V (매집) : 청산압력 LPS + 피보 되돌림  -> "바닥에 그물을 친다"
  트랙 T (확증) : 20/40/60 골든크로스         -> "반등을 확인하고 올라탄다"

진짜 알파는 두 트랙의 **시간 순서 결합**이다.
  매집 신호 발생 -> 20거래일 내 골든크로스 발생 = 바닥 확인 + 추세 전환 확증
이 시퀀스가 성립한 종목만 S+ 등급으로 올린다.
"""
from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd

from .config import Config, DEFAULT
from .contracts import ScreenResult
from .cost_basis import estimate_cost_basis
from .fibonacci import evaluate_fib
from .indicators import evaluate_trend
from .liquidation import evaluate_liquidation, liquidation_band

__all__ = ["check_exclusion", "confluence_check", "sequence_confirm",
           "screen_one", "screen_universe", "rank_results"]


HARD_EXCLUSION_FLAGS = (
    ("관리종목", "관리종목 지정"),
    ("투자주의환기", "투자주의환기종목"),
    ("감사의견거절", "감사의견 거절/한정"),
    ("자본잠식", "자본잠식률 50% 이상"),
    ("거래정지이력", "최근 90일 거래정지 이력"),
    ("대규모증자", "최근 60일 대규모 유상증자/CB"),
    # 2026년 상장폐지 규정 강화로 1,000원 미만 장기 지속은 관리종목 지정
    # 사유가 된다. 지정을 기다리지 않고 선제 배제한다.
    ("동전주위험", "1,000원 미만 30거래일 지속 (관리종목 지정 위험)"),
)


def check_exclusion(flags: dict | None, market_cap: float | None,
                    listed_days: int | None,
                    min_cap: float = 1_000e8) -> str | None:
    """하드 배제 판정. 배제 사유 문자열 또는 None.

    이 필터가 없으면 LPS 만점 목록이 잡주로 가득 찬다. 청산 반등 로직은
    '펀더멘털이 살아있는 종목이 수급 때문에 과하게 밀린 경우'에만 작동한다.
    """
    flags = flags or {}
    for key, label in HARD_EXCLUSION_FLAGS:
        if flags.get(key):
            return label
    if market_cap is not None and np.isfinite(market_cap) and market_cap < min_cap:
        return f"시가총액 {market_cap / 1e8:,.0f}억 (하한 {min_cap / 1e8:,.0f}억)"
    if listed_days is not None and listed_days < 252:
        return f"상장 {listed_days}일 (1년 미만, 파동 추정 불가)"
    return None


def confluence_check(fib_target_price: float, band_mid: float,
                     price: float, tol_pct: float) -> bool:
    """피보 목표 레벨과 청산 중심선(-30%)이 겹치는가.

    차트상의 되돌림 지지선과 신용 반대매매 투매선이 수학적으로 같은
    가격에서 만나면 서로 독립적인 두 근거가 한 점을 가리키는 것이다.
    """
    if not all(np.isfinite([fib_target_price, band_mid, price])) or price <= 0:
        return False
    return abs(fib_target_price - band_mid) / price * 100.0 <= tol_pct


def sequence_confirm(ohlcv: pd.DataFrame, band: dict, cross_event,
                     window: int) -> bool:
    """골든크로스 직전 window 구간에 청산 밴드 스윗스팟 진입이 있었는가."""
    if cross_event is None or cross_event.kind != "GOLDEN":
        return False
    if cross_event.bars_ago > window:
        return False
    span = band["hi"] - band["lo"]
    if span <= 0:
        return False
    try:
        i = int(ohlcv.index.get_loc(cross_event.date))
    except KeyError:
        return False
    start = max(0, i - window)
    seg = ohlcv["저가"].astype("float64").iloc[start:i + 1]
    upper = band["lo"] + 0.65 * span
    return bool(((seg >= band["lo"]) & (seg <= upper)).any())


def _grade_and_mark(value_score: float, trend_score: float,
                    confluence: bool, seq: bool, cfg: Config):
    g = cfg.gate
    if seq and (value_score >= g.value_threshold or confluence):
        return "S+", "⭐⭐⭐ 🔵"
    if value_score >= g.value_threshold and confluence:
        return "S", "⭐⭐ 🔵"
    if value_score >= g.value_threshold or trend_score >= g.trend_threshold:
        return "A", "⭐ 🔵"
    if value_score >= 6.5 or trend_score >= 6.5:
        return "B", "🔹"
    return "NONE", ""


def screen_one(ticker: str,
               name: str,
               ohlcv: pd.DataFrame,
               credit: pd.DataFrame | None = None,
               investor: pd.DataFrame | None = None,
               shorting: pd.DataFrame | None = None,
               credit_ratio: float | None = None,
               flags: dict | None = None,
               market_cap: float | None = None,
               listed_days: int | None = None,
               cfg: Config = DEFAULT) -> ScreenResult:
    """한 종목 스크리닝.

    ohlcv : index=거래일(오름차순), columns ['시가','고가','저가','종가','거래량']
            '거래대금'이 있으면 VWAP 정확도가 올라간다.
    """
    asof = ohlcv.index[-1] if len(ohlcv) else datetime.now()
    price = float(ohlcv["종가"].iloc[-1]) if len(ohlcv) else float("nan")

    excluded = check_exclusion(flags, market_cap, listed_days)
    if excluded:
        return ScreenResult(ticker=ticker, name=name, asof=asof,
                            price=price, excluded=excluded)

    trend = evaluate_trend(ohlcv, cfg.ma)
    fib = evaluate_fib(ohlcv, cfg.fib)

    liq = None
    band = None
    try:
        p0, method, basis_conf = estimate_cost_basis(
            ohlcv, credit, investor,
            lookback=cfg.credit.lookback,
            credit_shift=cfg.credit.credit_shift,
        )
        liq = evaluate_liquidation(ohlcv, p0, method, basis_conf, cfg.credit,
                                   credit_ratio=credit_ratio, shorting=shorting)
        band = liquidation_band(p0, cfg.credit)
    except ValueError:
        pass

    # ── 트랙 V: 매집 점수 ──
    lps = liq.score if liq else 0.0
    fibs = fib.score if fib else 0.0
    value_score = 0.55 * lps + 0.45 * fibs

    # ── 트랙 T: 추세 점수 ──
    trend_score = trend.score if trend else 0.0

    # ── 결합 판정 ──
    conf = False
    if fib and band:
        conf = confluence_check(fib.levels.get(cfg.fib.target, float("nan")),
                                band["mid"], price, cfg.gate.confluence_tol_pct)
        if conf:
            value_score = min(10.0, value_score + 0.5)

    seq = False
    if trend and band:
        seq = sequence_confirm(ohlcv, band, trend.best_cross,
                               cfg.gate.sequence_window)

    value_score = round(float(value_score), 2)
    trend_score = round(float(trend_score), 2)

    grade, mark = _grade_and_mark(value_score, trend_score, conf, seq, cfg)

    if seq:
        track = "BOTH"
    elif trend_score >= value_score:
        track = "TREND"
    else:
        track = "VALUE"

    reasons = []
    if fib and fib.below_target:
        reasons.append(f"1년 고점 대비 {cfg.fib.target:.3f} 되돌림 이하 "
                       f"(진행률 {fib.ratio:.3f})")
    if fib and fib.wave_broken:
        reasons.append("전 파동 저점 붕괴 — 되돌림이 아니라 추세 파괴")
    if trend and trend.best_cross and trend.best_cross.kind == "GOLDEN":
        reasons.append(f"{trend.best_cross.pair} 골든크로스 "
                       f"D+{trend.best_cross.bars_ago}")
    if trend and trend.alignment == "GOLDEN":
        reasons.append("20>40>60 정배열 완성")
    if liq and liq.is_margin_due:
        reasons.append("D+2 반대매매 집행 예상일")
    if conf:
        reasons.append("피보 0.618선 = 신용청산 중심선 겹침")
    if seq:
        reasons.append("청산 밴드 진입 후 골든크로스 (시퀀스 확증)")

    return ScreenResult(
        ticker=ticker, name=name, asof=asof, price=price,
        trend=trend, fib=fib, liq=liq,
        value_score=value_score, trend_score=trend_score,
        track=track, grade=grade, confluence=conf, sequence_confirm=seq,
        mark=mark, reasons=tuple(reasons),
    )


def rank_results(results: list[ScreenResult]) -> list[ScreenResult]:
    """등급 -> 시퀀스 -> 최고점수 순 정렬."""
    order = {"S+": 0, "S": 1, "A": 2, "B": 3, "NONE": 4}
    return sorted(
        results,
        key=lambda r: (order.get(r.grade, 9),
                       0 if r.sequence_confirm else 1,
                       -max(r.value_score, r.trend_score)),
    )


def screen_universe(loader, tickers: dict, cfg: Config = DEFAULT,
                    on_error=None) -> list[ScreenResult]:
    """유니버스 일괄 스크리닝.

    loader(ticker) -> dict(ohlcv=..., credit=..., investor=..., shorting=...,
                           credit_ratio=..., flags=..., market_cap=...,
                           listed_days=...)
    tickers : {종목코드: 종목명}

    종목 하나가 터져도 배치 전체가 죽지 않도록 예외를 개별 격리한다.
    수천 종목 배치에서는 이게 없으면 매일 아침 배치가 실패한다.
    """
    out: list[ScreenResult] = []
    for code, name in tickers.items():
        try:
            payload = loader(code)
            if payload is None or payload.get("ohlcv") is None:
                continue
            out.append(screen_one(code, name, cfg=cfg, **payload))
        except Exception as exc:  # noqa: BLE001 - 배치 격리 목적
            if on_error:
                on_error(code, name, exc)
    return rank_results(out)
