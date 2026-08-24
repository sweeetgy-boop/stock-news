# -*- coding: utf-8 -*-
"""전 모듈 공용 파라미터.

숫자를 코드에 박지 않고 여기 한 곳에 모아둔 이유는 백테스트로 값을
흔들어보며 튜닝해야 하기 때문이다. -30%, 0.618, 20/40/60 전부
가설이므로 검증 전에는 상수 취급하지 않는다.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MAConfig:
    """이동평균 골든크로스 판정 파라미터."""

    short: int = 20
    mid: int = 40
    long: int = 60

    # 크로스 신선도: fresh_days 이내면 만점, stale_days에서 0점까지 선형 감쇠
    fresh_days: int = 5
    stale_days: int = 20

    # 크로스 시점 3개 이평선 밀집도(%) — 좁을수록 에너지 응축이 큼
    convergence_tight_pct: float = 3.0
    convergence_loose_pct: float = 5.0

    slope_window: int = 5          # 기울기 측정 구간(거래일)
    vol_surge_strong: float = 1.5  # 크로스일 거래량 / 20일 평균
    vol_surge_weak: float = 1.2

    # 크로스 후 되돌림(휩쏘) 판정: 종가가 장기선 아래로 되돌아간 횟수 허용치
    whipsaw_tolerance: int = 1


@dataclass(frozen=True)
class FibConfig:
    """1년 고점 기준 피보나치 되돌림 파라미터."""

    lookback: int = 252            # 1년 ≈ 252거래일
    levels: tuple[float, ...] = (0.236, 0.382, 0.5, 0.618, 0.786, 1.0)
    target: float = 0.618          # "이 값 이하"를 찾는 기준선
    touch_tol_pct: float = 2.5     # 레벨 ±2.5% 이내면 '터치'
    touch_loose_pct: float = 5.0

    high_fresh_days: int = 90      # 고점이 이 이내면 파동이 살아있다고 봄
    high_stale_days: int = 180     # 이보다 오래되면 신뢰도 하향
    min_swing_pct: float = 15.0    # 파동 폭이 고점의 15% 미만이면 레벨 무의미
    min_pre_bars: int = 20         # 고점 앞 구간이 이보다 짧으면 폴백


@dataclass(frozen=True)
class CreditConfig:
    """신용 강제청산 밴드 파라미터.

    담보유지비율 = 주식평가액 / 융자금 >= maint_ratio
      => 청산 트리거가격 Pc = P0 * maint_ratio * 융자비율(L)

    L=0.60 -> 0.840*P0 (-16%)   보증금률 40%, 마진콜 개시
    L=0.50 -> 0.700*P0 (-30%)   대량 청산 집중 구간
    L=0.40 -> 0.560*P0 (-44%)   연쇄청산 언더슈팅
    """

    maint_ratio: float = 1.40
    loan_ratio_hi: float = 0.60
    loan_ratio_mid: float = 0.50
    loan_ratio_lo: float = 0.40

    lookback: int = 90             # 신용 평균단가 추정 구간
    credit_shift: int = 1          # 신용잔고 공시 지연(영업일) — 룩어헤드 방지
    short_shift: int = 2           # 공매도 잔고 공시 지연(영업일)


@dataclass(frozen=True)
class GateConfig:
    """알림 게이트 파라미터."""

    value_threshold: float = 8.0   # 매집 트랙 발송 하한
    trend_threshold: float = 8.0   # 추세 트랙 발송 하한
    daily_budget: int = 3          # 즉시 속보 1일 최대 건수
    cooldown_days: int = 5         # 동일 종목 재발송 금지 기간
    rescore_delta: float = 1.0     # 점수가 이만큼 오르면 쿨다운 예외
    sequence_window: int = 20      # 매집 신호 -> 골든크로스 결합 허용 기간
    confluence_tol_pct: float = 3.0  # 피보 0.618선과 청산 중심선 겹침 허용치
    digest_top_n: int = 5


@dataclass(frozen=True)
class ExitConfig:
    """청산 규칙 파라미터.

    설계 원칙: 청산은 진입 근거를 거울처럼 따라간다.
      트랙 V(역추세) -> 밴드로 들어가서 밴드로 나온다
      트랙 T(순추세) -> 크로스로 들어가서 트레일링으로 나온다
      공통           -> +15% 절반 기계적 익절만 동일

    우선순위(작을수록 우선). 같은 날 여러 개가 걸리면 하나만 집행한다.
      0 무효화 / 1 손절 / 2 트레일링 / 3 목표3차 / 4 목표2차
      5 목표1차 / 6 시간 / 7 순환매
    """

    # ── 계층 2: 목표(익절) ──
    take1_pct: float = 15.0            # 1차 익절 트리거(%)
    take1_ratio: float = 0.50          # 1차 청산 비율
    take2_ratio: float = 0.30          # 2차 청산 비율 (피보 0.382 회복)

    # ── 계층 2: 트레일링 (트랙 T) ──
    trail_pct: float = 8.0             # 진입 후 최고 '종가' 대비 하락폭
    trail_ma: int = 20                 # 이 이평선 종가 이탈 시 청산

    # ── 계층 1: 손절 ──
    band_break_days: int = 3           # 밴드 하단 종가 연속 이탈 일수
    hard_stop_pct: float = 10.0        # 밴드 외 진입 시 고정 손절(%)
    atr_window: int = 14
    atr_mult: float = 2.0              # 밴드 외 진입 시 ATR 손절 배수
    ma_long_break_days: int = 2        # 종가가 MA60 아래 연속 일수
    cross_low_buffer_pct: float = 3.0  # 골든크로스 봉 저가 이탈 허용폭(%)

    # ── 계층 3: 시간 ──
    time_stop_v_days: int = 15         # 트랙 V 시간 손절
    time_stop_v_min_ret: float = 3.0   # 이 수익률 미만이면 청산(%)
    time_stop_t_days: int = 20         # 트랙 T 시간 손절
    credit_resurge_pp: float = 1.5     # 신용잔고율 재급증 폭(%p)

    # ── 계층 4: 순환매 (포트폴리오) ──
    rotation_spread_pct: float = 15.0      # 대칭 자산 편차 임계
    rotation_trim_ratio: float = 0.50      # 급등 자산 청산 비율
    rotation_min_spread_pct: float = 5.0   # 이 미만이면 순환매 보류

    # ── 시장 급락일 손절 유예 (기본 비활성) ──
    # 논쟁적 옵션이다. 켜면 패닉 저가 투매를 피하지만 '손절 미루기'가
    # 습관이 될 위험이 있다. 유예는 1일로 고정하고 계층 0은 예외 없다.
    panic_defer_enabled: bool = False
    panic_index_drop_pct: float = -3.0
    panic_defer_days: int = 1

    # ── 체결/비용 가정 ──
    # 종가로 판단하므로 실제 체결은 익일이다. 백테스트도 동일 가정을 써야
    # 성과가 부풀려지지 않는다.
    fill_mode: str = "next_open"
    roundtrip_cost_pct: float = 0.5    # 수수료 + 거래세 왕복
    min_lot: int = 1                   # 최소 주문 단위


@dataclass(frozen=True)
class Config:
    ma: MAConfig = MAConfig()
    fib: FibConfig = FibConfig()
    credit: CreditConfig = CreditConfig()
    gate: GateConfig = GateConfig()
    exit: ExitConfig = ExitConfig()


DEFAULT = Config()
