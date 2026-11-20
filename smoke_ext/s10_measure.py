# -*- coding: utf-8 -*-
"""v2-A 측정 바로잡기 — 회귀 검사.

1) 백테스트 대조군 비교가 같은 지표(시장 중위 대비 초과수익)끼리인가
2) 백테스트 이벤트 중복 제거가 [0] 이벤트 판정(N=20)을 쓰는가 · 채점 통일
3) 지수 신선도 가드: 기준일 값이 없는 지수로 market_context 를 기록하지 않음
"""
from __future__ import annotations

import sys
import types
from datetime import date, timedelta

import numpy as np
import pandas as pd

S = "v2-A 측정"


def _cal(n: int, start: str = "2026-03-02") -> list[str]:
    out, cur = [], date.fromisoformat(start)
    while len(out) < n:
        if cur.weekday() < 5:
            out.append(cur.isoformat())
        cur += timedelta(days=1)
    return out


def _idx_frame(end: str, n: int = 260, last: float = 300.0) -> pd.DataFrame:
    """FDR 형태 지수 프레임. 영업일 기준으로 `end` 에서 끝난다."""
    ix = pd.bdate_range(end=end, periods=n)
    c = np.full(n, 100.0)
    c[-1] = last
    return pd.DataFrame({"Open": c, "Close": c}, index=ix)


def run(check, near, tmp):
    from stocknews import backtest as B
    from stocknews import events as ev

    bt = B.BacktestConfig(horizons=(5,), event_window=20)

    # ── 1) 같은 지표끼리 비교 ──
    def same_metric():
        # 전략: 순수익 +0.5, 초과(alpha) +1.0
        # 무작위: 순수익 0.0, 초과 +2.0  → 같은 지표로 보면 전략이 못하다.
        # 예전 버그(전략 alpha vs 무작위 net)는 1.0 > 0.0 이라 '낫다'고 봤다.
        n = 30
        strat = pd.DataFrame({"ticker": [f"T{i}" for i in range(n)],
                              "date": ["2026-03-02"] * n,
                              "net_5": [0.5] * n, "alpha_5": [1.0] * n,
                              "mkt_5": [-0.5] * n})
        rnd = pd.DataFrame({"net_5": [0.0] * n, "alpha_5": [2.0] * n})
        rsi = pd.DataFrame({"net_5": [-3.0] * n, "alpha_5": [0.5] * n})
        s = B.summarize(strat, bt, rnd, rsi)
        near(s["controls"]["random"]["mean"], 2.0, label="무작위 = alpha 평균")
        near(s["controls"]["rsi30"]["mean"], 0.5, label="RSI = alpha 평균")
        near(s["controls_net"]["random"]["mean"], 0.0, label="순수익은 참고로만")
        assert any("무작위 진입보다 낫지 않다" in w for w in s["warnings"]), \
            s["warnings"]
        assert not any("RSI" in w for w in s["warnings"]), s["warnings"]
        # 시장 중위는 대조군 비교에서 빠지고 참고값으로만 남는다
        assert "market" not in s["controls"]
        near(s["market_ret"]["mean"], -0.5, label="시장 중위 수익률(참고)")
    check(S, "대조군 비교 = 같은 지표(초과수익)끼리", same_metric)

    # ── 2) 중복 제거 + [0] 채점 ──
    cal = _cal(80)

    def dedupe():
        # step 5 로 연속 포착: 0,5,10,15 는 한 이벤트, 20 은 새 이벤트(21일째)
        rows = pd.DataFrame({
            "ticker": ["A"] * 6 + ["B"],
            "date": [cal[i] for i in (0, 5, 10, 15, 20, 25)] + [cal[5]],
            "grade": ["A"] * 7})
        evs, st = B.dedupe_events(rows, cal, window=20)
        assert st == {"before": 7, "after": 3}, st
        got = sorted(zip(evs["ticker"], evs["date"]))
        assert got == [("A", cal[0]), ("A", cal[20]), ("B", cal[5])], got
        assert "d" not in evs.columns
        # window=0 이면 그대로 (예전 동작)
        same, st0 = B.dedupe_events(rows, cal, window=0)
        assert st0 == {"before": 7, "after": 7} and len(same) == 7
        # 기본 N 은 [0] 의 EVENT_WINDOW_DAYS
        from stocknews.config import EVENT_WINDOW_DAYS
        assert B.BacktestConfig().event_window == EVENT_WINDOW_DAYS == 20
    check(S, "백테스트 이벤트 중복 제거 = [0] assign_events(N=20)", dedupe)

    def unified_scoring():
        # 패널: A 는 진입 시가 100 → 5일째 종가 110, M 은 평탄(중위 계산용)
        rows = []
        for i, d in enumerate(cal[:30]):
            rows.append((d, "A", 100.0, 110.0 if i >= 9 else 100.0))
            rows.append((d, "M", 100.0, 100.0))
            rows.append((d, "N", 100.0, 100.0 - (2.0 if i >= 9 else 0.0)))
        panel = ev.PricePanel.from_frame(
            pd.DataFrame(rows, columns=["d", "ticker", "o", "c"]), cal[:30])
        ctx = {"calendar": cal[:30], "panel": panel,
               "markets": {"A": "KOSPI", "M": "KOSPI", "N": "KOSPI"},
               "index": {}}
        cand = pd.DataFrame({"ticker": ["A"], "date": [cal[4]],
                             "mae_5": [-1.0]})
        sc = B.score_rows(cand, ctx, B.BacktestConfig(horizons=(5,)))
        r = sc.iloc[0]
        # [0] 정의: 익일(cal[5]) 시가 100 → 5일째(cal[9]) 종가 110, 비용 0.5
        near(r["net_5"], 9.5, label="순수익")
        near(r["mkt_5"], r["med_5"], label="mkt = [0] 전종목 중위")
        near(r["alpha_5"], r["ex_med_5"], label="alpha = [0] ex_med")
        near(r["alpha_5"], 9.5 - 0.0, label="중위 0 (A +10, M 0, N -2)")
        assert np.isnan(r["ex_idx_5"]), "지수 없으면 NaN (0 채우기 금지)"
        # 대조군도 같은 경로: 후보 → 중복 제거 → 채점
        rnd = B._score_control(None, [{"ticker": "A", "date": cal[4]},
                                      {"ticker": "A", "date": cal[6]}],
                               B.BacktestConfig(horizons=(5,)), ctx)
        assert len(rnd) == 1 and rnd.attrs["raw_events"] == 2
        near(rnd.iloc[0]["alpha_5"], r["alpha_5"], label="대조군 같은 지표")
    check(S, "백테스트 채점 = [0] score_events (전략·대조군 같은 경로)",
          unified_scoring)

    def exits_vs_hold():
        sim = pd.DataFrame({"net": [1.0] * 25 + [5.0],
                            "hold_net": [2.0] * 25 + [np.nan],
                            "regime_v2": ["상승"] * 22 + [None] * 4})
        out = B.exits_vs_hold(sim)
        assert out["전체"]["engine"]["n"] == 25, "고정 보유 없는 행은 비교 제외"
        assert out["전체"]["engine_all"]["n"] == 26
        assert out["상승"]["engine"]["enough"] is True
        assert out[B.NO_REGIME]["hold"]["n"] == 3
        assert out[B.NO_REGIME]["hold"]["enough"] is False  # 20건 미만
        assert ev.fmt_cell(out[B.NO_REGIME]["hold"]).startswith("표본 부족")
    check(S, "청산 엔진 vs 고정 보유 국면별 칸 (20건 미만 표본 부족)",
          exits_vs_hold)

    # ── 3) 지수 신선도 가드 ──
    def fresh_helpers():
        from stocknews.data import index_is_fresh, index_last_date
        f = _idx_frame("2026-09-17")
        assert index_last_date(f, "2026-10-13") == "2026-09-17"
        assert not index_is_fresh(f, "2026-10-13")
        assert index_is_fresh(f, "2026-09-17")
        # 기준일 뒤 날짜가 더 있어도 기준일 값이 있으면 신선
        g = _idx_frame("2026-09-18")
        assert index_is_fresh(g, "2026-09-17")
        assert not index_is_fresh(None, "2026-09-17")
        assert not index_is_fresh(pd.DataFrame(), "2026-09-17")
    check(S, "신선도 판정: 기준일 이하 마지막 날짜 == 기준일", fresh_helpers)

    def guard_collect():
        from stocknews.daily import collect_market_context
        from stocknews.store import Store
        st = Store(tmp / "v2a_mkt.db")
        stale = _idx_frame("2026-09-17")
        fresh = _idx_frame("2026-10-13", last=250.0)
        # KOSPI 가 지난 값이면 기록하지 않는다 (예전: 09-17 값이 그대로 기록)
        got = collect_market_context(
            st, "2026-10-13", fetch=lambda name, n: stale)
        assert got is None and not st.has_market_context("2026-10-13")
        # 신선하면 기록
        row = collect_market_context(
            st, "2026-10-13", fetch=lambda name, n: fresh)
        assert row is not None and st.has_market_context("2026-10-13")
        near(row["kospi_close"], 250.0, label="기준일 종가")
        # KOSDAQ 만 지난 값이면 코스닥 칸만 비운다 (0 으로 채우지 않는다)
        st2 = Store(tmp / "v2a_mkt2.db")
        row2 = collect_market_context(
            st2, "2026-10-13",
            fetch=lambda name, n: fresh if name == "KOSPI" else stale)
        assert row2 is not None and row2["kosdaq_close"] is None
        assert row2["kosdaq_ret_5d"] is None
        near(row2["kospi_close"], 250.0, label="코스피는 기록")
    check(S, "market_context: 지수 마지막 날짜 ≠ 기준일이면 미기록", guard_collect)

    def guard_load_index():
        # FDR 을 가짜 모듈로 바꿔 끼운다 (네트워크 없음)
        from stocknews import data as D
        from stocknews import sector_metrics as SM
        from stocknews.store import Store
        stale = _idx_frame("2026-09-17")
        fake = types.ModuleType("FinanceDataReader")
        fake.DataReader = lambda sym, a=None, b=None: stale.copy()
        prev = sys.modules.get("FinanceDataReader")
        sys.modules["FinanceDataReader"] = fake
        try:
            assert D.load_index("KOSPI", bars=60) is not None, "asof 없으면 예전대로"
            assert D.load_index("KOSPI", bars=60, asof="2026-10-13") is None
            assert D.load_index("KOSPI", bars=60, asof="2026-09-17") is not None
            # 섹터 지표의 코스피 수익률 폴백도 지난 값을 쓰지 않는다
            st = Store(tmp / "v2a_sm.db")
            assert SM._kospi_returns(st, "2026-10-13") == (None, None)
            r5, _ = SM._kospi_returns(st, "2026-09-17")
            assert r5 is not None
        finally:
            if prev is None:
                sys.modules.pop("FinanceDataReader", None)
            else:
                sys.modules["FinanceDataReader"] = prev
    check(S, "load_index(asof) · 섹터 지표 폴백도 신선도 검사", guard_load_index)
