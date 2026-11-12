# -*- coding: utf-8 -*-
"""묶음 사이의 연결 지점. [0] 이 이름과 반환 규약만 정한다.

병렬로 작업하는 묶음이 서로의 결과를 쓴다(B 는 C 의 국면을, D 는 B 의
발송 대상을, F 는 E 의 σ 를). 각자 상대 구현을 추측해 따로 만들면 병합
때 같은 개념이 두 벌이 된다. 그래서 **함수 이름·인자·반환 규약을 여기서
고정**하고, 본문은 **주인 묶음만** 채운다. 주인이 채우기 전에는 '데이터
없음' 을 돌려서, 호출하는 쪽이 지금과 같은 동작으로 떨어지게 한다.

규칙
  · 주인이 아닌 묶음은 이 파일을 고치지 않는다.
  · 주인은 함수 본문만 바꾼다. 이름·인자·반환 규약은 바꾸지 않는다.
    (바꿔야 하면 보고한다.)
  · 무거운 구현은 주인 모듈에 두고 여기서는 지연 import 로 부른다.
"""
from __future__ import annotations

__all__ = ["regime_on", "regime_row", "send_targets", "exclusion_line",
           "sigma_on", "stop_hit_prob", "tracked_tickers"]


# ════════════════ [C] 국면 — 주인: C ════════════════
def regime_on(store, d) -> str | None:
    """그 거래일의 국면 라벨. "상승" / "중립" / "하락" / None.

    None = 기록 없음 또는 지수 신선하지 않음. 호출부는 None 이면 국면
    분리를 하지 않고(= 지금과 같은 동작) 표에는 "국면 없음" 으로 적는다.
    과거 날짜도 조회된다(`--mode regime-backfill` 이후). 여러 날을 볼 때는
    `stocknews.regime.labels_map(store)` 가 한 번에 준다.
    """
    from .regime import get_label
    return get_label(store, d)


def regime_row(store, d) -> dict | None:
    """그 거래일 국면 판정 근거 1행. 배너용.

    키: d, label, kospi_close, kospi_ma200, ma200_gap_pct, kospi_ret_20d,
        above_ma60_pct, fresh(bool). 없으면 None.
        (추가 키: raw_label, pending_label, pending_days, n_ma60_universe,
         null_reason, kind, calc_version, created)
    """
    from .regime import get_row
    return get_row(store, d)


# ════════════════ [B] 추천 필터 — 주인: B ════════════════
def send_targets(store, d) -> list[dict] | None:
    """그 거래일 recos 중 필터를 통과해 **발송·추적 대상**이 된 행.

    각 dict 는 recos 행(d, rank, ticker, name, slot, grade, value_score,
    trend_score) + is_new_event(bool) + day_in_window(int).
    None = 필터 미구현/미기록 → 호출부는 그날 recos 전체를 대상으로 본다.

    [B 구현] **항상 list 를 돌린다**(그날 recos 가 없으면 빈 list).
      · 플래그 전부 OFF 면 그날 recos 전체, 전부 is_new_event=True.
      · ①SEQ ②보충 ③국면 으로 빠진 행은 들어 있지 않다.
      · RECO_COOLDOWN_DAYS > 0 이면 기간 중 재추천 행도 들어 있다
        (is_new_event=False, status="hold" — '추천 유지 n일째' 표시용).
        추적 신규 편입은 is_new_event=True 행만 한다.
      · 그 밖의 키: status("new"/"hold"), event_date, window_end,
        event_slot, event_grade, slot_changed, grade_changed, regime.
    판정은 recos 를 읽기만 한다(stocknews/send_filter.py).
    """
    from .send_filter import evaluate
    return evaluate(store, d).targets


def exclusion_line(store, d) -> str | None:
    """"오늘 제외 — SEQ n, 보충 n, 재추천 제한 n" 한 줄. 없으면 None.

    [B 구현] 플래그가 전부 OFF 면 None. REGIME_TREND_CUT 이 켜져 있으면
    끝에 ", 국면 n" 이 붙는다.
    """
    from .send_filter import any_on, evaluate, exclusion_line_of
    if not any_on():
        return None
    return exclusion_line_of(evaluate(store, d))


# ════════════════ [E] 위험 정보 — 주인: E ════════════════
def sigma_on(store, d, ticker: str) -> float | None:
    """그 거래일 종목의 EWMA 일간 변동성 σ (소수, 예: 0.023). 없으면 None."""
    return None


def stop_hit_prob(store, d, ticker: str, price: float, stop: float,
                  days: int = 20) -> float | None:
    """price 에서 days 거래일 안에 stop 에 닿을 확률(0~1). 없으면 None."""
    return None


# ════════════════ [D] 추천 추적 — 주인: D·F ════════════════
def tracked_tickers(store, d) -> set[str]:
    """그 거래일 추적 중(paper_positions 활성)인 종목코드. 없으면 빈 집합."""
    return set()
