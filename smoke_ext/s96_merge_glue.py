# -*- coding: utf-8 -*-
"""병합 단계에서 묶음 사이를 이은 곳 — 회귀 검사.

  · [E→D] ③ 추적 요약 "평소 출렁임의 n배" (RISK_INFO ON 일 때만)
  · [G+B+DF] NEWS_V2 저녁 브리핑이 stock_block / foot_lines / watchlist 를 받는다
  · [D] paper --backfill 이 이벤트 창 0 이면 경고를 남긴다
"""
from __future__ import annotations

import math

S = "v2 병합 연결"


def run(check, near, tmp):
    from stocknews import config as C
    from stocknews import paper as P
    from stocknews import paper_render as PR
    from stocknews import v2_hooks

    class _P:
        def __init__(self, k, v):
            self.k, self.v, self.old = k, v, getattr(C, k)

        def __enter__(self):
            setattr(C, self.k, self.v)

        def __exit__(self, *a):
            setattr(C, self.k, self.old)

    row = {"ticker": "000001", "signal_d": "2026-11-06", "ret": 18.05,
           "net": 17.55, "day_n": 2}

    def move_x():
        with _P("RISK_INFO", False):
            assert PR._move_x(None, row) == ""
        orig = v2_hooks.sigma_on
        v2_hooks.sigma_on = lambda store, d, t: 0.064
        try:
            with _P("RISK_INFO", True):
                got = PR._move_x(None, row)
        finally:
            v2_hooks.sigma_on = orig
        n = math.log1p(0.1805) / (0.064 * math.sqrt(2))
        assert got == f" · 평소 출렁임의 {n:.1f}배", got
        v2_hooks.sigma_on = lambda store, d, t: None
        try:
            with _P("RISK_INFO", True):
                assert PR._move_x(None, row) == ""      # σ 없음 → 표기 생략
        finally:
            v2_hooks.sigma_on = orig
    check(S, "[E→D] ③ '평소 출렁임의 n배' — OFF 빈칸 · ON 계산 · σ 없음 생략", move_x)

    def news_v2_params():
        import inspect
        from stocknews import news_v2
        ps = inspect.signature(news_v2.render_evening_v2).parameters
        for k in ("foot_lines", "stock_block", "watchlist"):
            assert k in ps, f"render_evening_v2 에 {k} 없음"
        src = inspect.getsource(__import__("stocknews.renderer",
                                           fromlist=["x"]).render_evening_brief)
        for k in ("foot_lines=foot_lines", "stock_block=stock_block",
                  "watchlist=watchlist"):
            assert k in src, f"render_evening_brief 가 NEWS_V2 경로에 {k} 를 안 넘김"
    check(S, "[G+B+DF] NEWS_V2 저녁 경로에 제외 줄·종목 블록·관심종목 인자 전달",
          news_v2_params)

    def backfill_warn():
        from stocknews.store import Store
        st = Store(tmp / "glue.db")
        with _P("RECO_COOLDOWN_DAYS", 0):
            r = P.backfill(st, sent_dates={}, allow_network=False)
            assert r.get("window") == 0 and "RECO_COOLDOWN_DAYS=0" in (r.get("warning") or "")
        with _P("RECO_COOLDOWN_DAYS", 20):
            r = P.backfill(st, sent_dates={}, allow_network=False)
            assert r.get("window") == 20 and "warning" not in r, r
    check(S, "[D] paper --backfill: 창 0 이면 경고 · 창 20 이면 경고 없음", backfill_warn)
