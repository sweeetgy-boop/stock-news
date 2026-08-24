# -*- coding: utf-8 -*-
"""스캐너와 알림봇 사이의 유일한 결합점.

김승곤 차장 스캐너(수천 종목 배치)는 그대로 두고, 그 출력물을
adapters 에서 이 계약 객체로 변환한다. 봇은 이 객체만 안다.
frozen=True 로 둔 이유는 게이트/랜더러를 지나는 동안 값이
변조되지 않도록 하기 위함이다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Optional

Confidence = Literal["HIGH", "MID", "LOW"]
CrossKind = Literal["GOLDEN", "DEAD"]
Track = Literal["VALUE", "TREND", "BOTH"]
Grade = Literal["S+", "S", "A", "B", "NONE"]


@dataclass(frozen=True)
class CrossEvent:
    """이평선 교차 1건."""

    pair: str          # "20x60"
    kind: CrossKind
    date: datetime
    bars_ago: int
    fast: int
    slow: int


@dataclass(frozen=True)
class TrendSignal:
    """20/40/60 골든크로스 판정 결과."""

    score: float                    # 0.0 ~ 10.0
    alignment: str                  # GOLDEN(정배열) / DEAD(역배열) / MIXED
    best_cross: Optional[CrossEvent]
    ma_short: float
    ma_mid: float
    ma_long: float
    convergence_pct: float          # 3개 이평선 밀집도(%)
    slope_long_pct: float           # 장기선 기울기(%)
    vol_ratio_at_cross: float
    whipsaw_count: int              # 크로스 후 장기선 하향 이탈 횟수
    breakdown: dict = field(default_factory=dict)


@dataclass(frozen=True)
class FibSignal:
    """1년 고점 기준 피보나치 되돌림 판정 결과."""

    score: float                    # 0.0 ~ 10.0
    high: float
    high_date: datetime
    high_age: int                   # 고점 이후 경과 거래일
    swing_low: float                # 고점 '이전' 파동 저점
    swing: float                    # high - swing_low
    price: float
    ratio: float                    # 되돌림 진행률 (high-price)/swing
    levels: dict                    # {0.618: 45471.0, ...}
    zone: str                       # "0.618 ~ 0.786"
    below_target: bool              # price <= levels[target]
    nearest_level: float            # 가장 가까운 되돌림 비율(0.618 등)
    nearest_gap_pct: float          # 그 레벨과의 이격(%)
    wave_broken: bool               # ratio > 1.0 (전 파동 저점 붕괴)
    confidence: Confidence
    breakdown: dict = field(default_factory=dict)


@dataclass(frozen=True)
class LiquidationSignal:
    """신용 강제청산 압력 판정 결과."""

    score: float                    # 0.0 ~ 10.0 (LPS)
    cost_basis: float               # 신용 추정 평균단가 P0
    basis_method: str               # A(신용잔고) / B(개인순매수) / C(매물대)
    credit_ratio: float             # 신용잔고율(%)
    band_hi: float                  # P0 * 0.840  (-16%)
    band_mid: float                 # P0 * 0.700  (-30%)
    band_lo: float                  # P0 * 0.560  (-44%)
    band_pos: float                 # 0=하단 1=상단
    vol_ratio: float                # 20일 평균 대비 거래량 배수
    short_trend: str                # 감소전환 / 증가 / 중립
    is_margin_due: bool             # D+2 반대매매 예상일 해당
    confidence: Confidence
    breakdown: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ScreenResult:
    """한 종목에 대한 최종 스크리닝 산출물."""

    ticker: str
    name: str
    asof: datetime
    price: float

    trend: Optional[TrendSignal] = None
    fib: Optional[FibSignal] = None
    liq: Optional[LiquidationSignal] = None

    value_score: float = 0.0        # 매집 트랙 종합 점수
    trend_score: float = 0.0        # 추세 트랙 종합 점수
    track: Track = "VALUE"
    grade: Grade = "NONE"
    confluence: bool = False        # 피보 0.618선 == 청산 중심선
    sequence_confirm: bool = False  # 매집신호 후 골든크로스 발생
    mark: str = ""                  # ⭐⭐⭐ 🔵 등
    reasons: tuple = ()
    excluded: Optional[str] = None  # 배제 사유(있으면 발송 금지)


# ══════════════════════════ 포지션 / 청산 ══════════════════════════
# exits_done 비트마스크. 목표 익절은 한 번만 집행해야 하므로 플래그로 남긴다.
# 손절·트레일링·시간 청산은 플래그를 쓰지 않는다. 포지션이 닫힐 때까지
# 매일 다시 신호를 내는 게 맞다(체결 확인 전까지 계속 알려야 한다).
DONE_TAKE1 = 1 << 0
DONE_TAKE2 = 1 << 1

ExitAction = Literal["EXIT_ALL", "TRIM"]


@dataclass(frozen=True)
class Position:
    """보유 포지션.

    진입 시점 스냅샷을 반드시 고정한다. 매일 P0 를 재계산하면 주가가
    내려갈 때 밴드 하단도 따라 내려가 손절이 영원히 발동하지 않는다.
    """

    id: Optional[int]
    ticker: str
    name: str
    track: str                  # VALUE / TREND
    entry_date: str             # YYYY-MM-DD
    entry_price: float
    qty: int
    remaining: int

    # ── 진입 시점 스냅샷 (재계산 금지) ──
    entry_p0: Optional[float] = None
    entry_band_hi: Optional[float] = None
    entry_band_mid: Optional[float] = None
    entry_band_lo: Optional[float] = None
    entry_fib_0382: Optional[float] = None
    entry_fib_0618: Optional[float] = None
    entry_cross_low: Optional[float] = None
    entry_credit_ratio: Optional[float] = None

    # ── 파생 상태 (매 실행 시 시세로부터 재계산) ──
    exits_done: int = 0
    peak_close: Optional[float] = None
    band_break_streak: int = 0
    ma_break_streak: int = 0
    defer_until: Optional[str] = None

    status: str = "OPEN"
    opened_by: Optional[str] = None
    note: Optional[str] = None

    @property
    def in_band(self) -> bool:
        """밴드 내 진입이었는가. 손절 방식이 갈린다."""
        if self.entry_band_lo is None or self.entry_band_hi is None:
            return False
        return self.entry_band_lo <= self.entry_price <= self.entry_band_hi


@dataclass(frozen=True)
class ExitDecision:
    """청산 판정 1건."""

    ticker: str
    name: str
    position_id: Optional[int]
    layer: int                  # 0 무효화 ~ 7 순환매
    rule: str                   # 규칙 식별자
    action: ExitAction
    ratio: float                # 청산 비율 (1.0 = 전량)
    qty: int                    # 청산 수량 (min_lot 반영)
    signal_price: float         # 판정 기준 종가
    ret_pct: float              # 진입가 대비 수익률(%)
    net_ret_pct: float          # 왕복 비용 차감 후
    reason: str
    urgent: bool                # True 면 시간창/알림예산 무시
    fill_note: str = "익일 시가 집행 가정"
    detail: dict = field(default_factory=dict)


LAYER_NAME = {
    0: "무효화",
    1: "손절",
    2: "트레일링",
    3: "목표3차",
    4: "목표2차",
    5: "목표1차",
    6: "시간",
    7: "순환매",
}
