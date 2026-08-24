# -*- coding: utf-8 -*-
"""stock-news 퀀트 스크리너.

트랙 V (매집)  : 신용 강제청산 밴드 + 1년 고점 피보나치 되돌림
트랙 T (확증)  : 20/40/60일 이동평균 골든크로스
결합           : 밴드 진입 후 골든크로스 = S+ 등급

일일  : 전종목 스캔 → 스냅샷 저장 → 추천 10선
주간  : 누적 스냅샷 분석 (자기검증 · 점수 모멘텀 · 연속등재 · ETA)
뉴스  : 국내외 수집 → 중복제거·클러스터·종목태깅 → 아침/저녁/주간 브리핑
        (뉴스에는 점수를 매기지 않는다. 중요도는 매체 수 + 내 종목 여부로만)
청산  : 진입 근거를 거울처럼 따라가는 8계층 규칙
        트랙 V는 밴드로 들어가서 밴드로 나오고,
        트랙 T는 크로스로 들어가서 트레일링으로 나온다.
"""
from .config import (Config, CreditConfig, DEFAULT, ExitConfig, FibConfig,
                     GateConfig, MAConfig)
from .contracts import (DONE_TAKE1, DONE_TAKE2, CrossEvent, ExitDecision,
                        FibSignal, LiquidationSignal, Position, ScreenResult,
                        TrendSignal)
from .daily import run_daily, scan_all, select_recommendations
from .exits import evaluate_position, evaluate_rotation, run_exits
from .fibonacci import evaluate_fib, fib_levels, is_below_level
from .flags import flag_summary, refresh_flags
from .indicators import evaluate_trend, last_cross, moving_averages
from .liquidation import evaluate_liquidation, liquidation_band, margin_call_due_dates
from .news import classify, normalize_title, process_and_store, theme_shift
from .news_sources import collect_all
from .screener import rank_results, screen_one, screen_universe
from .store import Store
from .weekly import audit_recos, band_eta, score_momentum, weekly_report

__all__ = [
    "Config", "MAConfig", "FibConfig", "CreditConfig", "GateConfig", "DEFAULT",
    "CrossEvent", "TrendSignal", "FibSignal", "LiquidationSignal", "ScreenResult",
    "moving_averages", "last_cross", "evaluate_trend",
    "fib_levels", "is_below_level", "evaluate_fib",
    "liquidation_band", "margin_call_due_dates", "evaluate_liquidation",
    "screen_one", "screen_universe", "rank_results",
    "Store", "scan_all", "select_recommendations", "run_daily",
    "weekly_report", "audit_recos", "score_momentum", "band_eta",
    "collect_all", "process_and_store", "classify", "normalize_title",
    "theme_shift",
    "ExitConfig", "Position", "ExitDecision", "DONE_TAKE1", "DONE_TAKE2",
    "evaluate_position", "evaluate_rotation", "run_exits",
    "refresh_flags", "flag_summary",
]

__version__ = "0.5.0"
