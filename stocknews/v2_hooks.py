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
    """
    return None


def regime_row(store, d) -> dict | None:
    """그 거래일 국면 판정 근거 1행. 배너용.

    키: d, label, kospi_close, kospi_ma200, ma200_gap_pct, kospi_ret_20d,
        above_ma60_pct, fresh(bool). 없으면 None.
    """
    return None


# ════════════════ [B] 추천 필터 — 주인: B ════════════════
def send_targets(store, d) -> list[dict] | None:
    """그 거래일 recos 중 필터를 통과해 **발송·추적 대상**이 된 행.

    각 dict 는 recos 행(d, rank, ticker, name, slot, grade, value_score,
    trend_score) + is_new_event(bool) + day_in_window(int).
    None = 필터 미구현/미기록 → 호출부는 그날 recos 전체를 대상으로 본다.
    """
    return None


def exclusion_line(store, d) -> str | None:
    """"오늘 제외 — SEQ n, 보충 n, 재추천 제한 n" 한 줄. 없으면 None."""
    return None


# ════════════════ [E] 위험 정보 — 주인: E ════════════════
def sigma_on(store, d, ticker: str) -> float | None:
    """그 거래일 종목의 EWMA 일간 변동성 σ (소수, 예: 0.023). 없으면 None.

    · config.RISK_INFO 가 꺼져 있으면 항상 None (= 지금과 같은 동작).
      호출부가 플래그를 따로 볼 필요가 없다.
    · risk_metrics 기록(현재 calc_version)이 있으면 그 값, 없으면 같은
      정의로 그날까지의 시세에서 즉석 계산한다(쓰지 않는다).
    · d 가 거래일이 아니면 그 이전 마지막 거래일. 그날 종목 행이 없거나
      (정지) 표본 부족·수정주가 불연속 직후면 None.
    · 정의: σ²_t = 0.94·σ²_{t-1} + 0.06·r_t², r = 종가 로그수익률(평균 0),
      최근 120거래일 창, 60개 미만·불연속 뒤 60거래일은 None
      (stocknews/risk.py 모듈 docstring).

    "평소 출렁임의 n배" (D·F ③ 요약)
      n = ln(1 + R) / (σ · √h)    R = 보유 수익률(소수), h = 보유 거래일
      σ 는 **기준일(추천 이벤트일)** 의 값으로 묻는다 — 움직인 뒤의 σ 는
      그 움직임으로 부풀어 n 이 작게 나온다. 계산은
      `stocknews.risk.move_multiple(R, σ, h)` (부호 유지, 표시는 |n|배).
    """
    from . import risk
    if not risk.enabled():
        return None
    try:
        return risk.sigma_on(store, d, ticker)
    except Exception:  # noqa: BLE001 - 표시 전용. 실패 = 데이터 없음
        return None


def stop_hit_prob(store, d, ticker: str, price: float, stop: float,
                  days: int = 20) -> float | None:
    """price 에서 days 거래일 안에 stop 에 닿을 확률(0~1). 없으면 None.

    모형: 로그 드리프트 0 기하 브라운 운동 + 반사원리,
          P = 2·Φ( ln(B'/P) / (σ√T) ),  B' = stop·exp(-0.5826·σ)
          (종가 판정에 맞춘 Broadie–Glasserman–Kou 이산 관측 보정).
    σ = sigma_on(store, d, ticker). 플래그 OFF·σ 없음이면 None.
    경계: price ≤ stop → 1.0 · days ≤ 0 → 0.0 · stop ≤ 0 → 0.0.
    """
    sigma = sigma_on(store, d, ticker)
    if sigma is None:
        return None
    from . import risk
    return risk.stop_hit_probability(price, stop, sigma, days)


# ════════════════ [D] 추천 추적 — 주인: D·F ════════════════
def tracked_tickers(store, d) -> set[str]:
    """그 거래일 추적 중(paper_positions 활성)인 종목코드. 없으면 빈 집합."""
    return set()
