# -*- coding: utf-8 -*-
"""2026-11-16 발송 정리: 금요일 weekly 파일 전용 · 일요일 성적표 ①~⑤ ·
종목 표기 "종목명(종목코드)" · 누적 곡선 = 코호트 연결 지수(시작=100)."""
from __future__ import annotations

from datetime import datetime

import pandas as pd

S = "11-16 주간 정리·종목 표기"


class _Book:
    def __init__(self, pos, sends):
        self._p, self._s = pos, sends

    def positions(self):
        return self._p.copy()

    def sends(self):
        return self._s.copy()


class _PD:
    def __init__(self, pos, sends, assigned, cal):
        self.book = _Book(pos, sends)
        self.assigned = assigned
        self.cal = cal
        self.store = None


def _fixture(n_track: int):
    cal = ["2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05", "2026-11-06",
           "2026-11-09", "2026-11-10", "2026-11-11", "2026-11-12", "2026-11-13"]
    pos = pd.DataFrame([
        {"id": 1, "ticker": "000100", "name": "가나", "signal_d": "2026-11-10",
         "entry_date": "2026-11-11", "enrolled": 1, "slot": "VALUE",
         "skip_reason": None, "entry_status": "OK"},
        {"id": 2, "ticker": "000200", "name": "다라", "signal_d": "2026-11-11",
         "entry_date": None, "enrolled": 0, "slot": "TREND",
         "skip_reason": "상한가 시가", "entry_status": "LIMIT_UP"},
        {"id": 3, "ticker": "000300", "name": "지난주", "signal_d": "2026-11-06",
         "entry_date": "2026-11-09", "enrolled": 1, "slot": "SEQ",
         "skip_reason": None, "entry_status": "OK"},
    ])
    sends = pd.DataFrame([
        {"d": "2026-11-13", "ticker": "000400", "kind": "new", "source": "live"},
        {"d": "2026-11-10", "ticker": "000100", "kind": "new", "source": "live"},
    ])
    assigned = pd.DataFrame([
        {"d": "2026-11-13", "ticker": "000400", "name": "마바", "slot": "FILL*",
         "event_date": "2026-11-13"},
    ])
    rows = [{"id": 1, "ticker": "000100", "name": "가나", "net": 3.21}]
    for i in range(n_track - 1):
        rows.append({"id": 100 + i, "ticker": f"{500 + i:06d}",
                     "name": f"추적종목가나다{i:02d}", "net": round(20 - i * 1.13, 2)})
    marks = pd.DataFrame(rows)
    matured = pd.DataFrame([
        {"ticker": "000900", "name": "만기A", "exit_date": "2026-11-12",
         "net": -4.5, "ex_idx": 1.25, "status": "OK", "slot": "VALUE",
         "signal_d": "2026-10-14", "idx": -5.0},
        {"ticker": "000901", "name": "예전만기", "exit_date": "2026-11-05",
         "net": 2.0, "ex_idx": 3.0, "status": "OK", "slot": "TREND",
         "signal_d": "2026-10-07", "idx": -1.0},
    ])
    return _PD(pos, sends, assigned, cal), marks, matured


def _patch(P, marks, matured):
    saved = (P.active_marks, P.matured_on, P.matured_random, P.pooled_percentile)
    P.active_marks = lambda pd_, d: marks.copy()
    P.matured_on = lambda pd_, d=None, start=None: (
        matured[matured["exit_date"] >= start] if start else matured).copy()
    P.matured_random = lambda pd_, sds, h=None: {}
    P.pooled_percentile = lambda reco, rnd, col="net": (55.0, 3)
    return saved


def _undo(P, saved):
    P.active_marks, P.matured_on, P.matured_random, P.pooled_percentile = saved


def run(check, near, tmp):
    from stocknews import paper as P
    from stocknews import paper_render as R

    asof = datetime(2026, 11, 15, 21, 30)

    def label():
        from stocknews.renderer import stock_label
        assert stock_label("삼성E&A", "028050") == "삼성E&amp;A(028050)"
        assert stock_label("JYP Ent.", "035900", bold=True) == "<b>JYP Ent.(035900)</b>"
        assert stock_label(None, "005930") == "005930"
        assert stock_label("005930", "005930") == "005930"
        assert stock_label(float("nan"), "005930") == "005930"
    check(S, "stock_label = 종목명(종목코드) · 이름 없으면 코드", label)

    def sunday_sections():
        pd_, marks, matured = _fixture(8)
        saved = _patch(P, marks, matured)
        try:
            text, info = R.sunday_scorecard(None, asof, "2026-11-13", pdata=pd_)
        finally:
            _undo(P, saved)
        assert not info["split"] and len(info["messages"]) == 1
        for h in ("① 이번 주 새 추천 (3)", "② 이번 주 만기 (1)",
                  "③ 추적 중 전체 (8종목", "④ 누적 성적", "※ 검증 중 · 투자 판단 근거 아님"):
            assert h in text, (h, text)
        assert "• 가나(000100) · 매집 · 편입 11-11 · +3.21%" in text, text
        assert "• 다라(000200) · 추세 · 미편입(상한가 시가) · -" in text, text
        assert "• 마바(000400) · 보충 · 편입 대기(다음 거래일 시가) · -" in text, text
        assert "지난주(000300)" not in text.split("③")[0], "지난주 편입이 ①에 들어갔다"
        assert "• 만기A(000900) · 최종 -4.50% · 지수 대비 +1.25%p" in text
        assert "예전만기" not in text.split("③")[0].split("②")[1]
        s3 = text.split("━━ ③")[1].split("━━ ④")[0]
        assert "추적종목가나다00(000500) +20.00% · 추적종목가나다01(000501) +18.87% · " \
               "추적종목가나다02(000502) +17.74%" in s3, s3
        assert "…" not in text
        cum = text.split("━━ ④")[1]
        assert "만기 2건 · 지수 대비 평균 +2.12%p · 승률 50% · 무작위 대비 백분위 55" in cum, cum
        assert "• 유형별 지수 대비: 매집 표본 부족(n=1) · 추세 표본 부족(n=1)" in cum, cum
    check(S, "일요일 ①~⑤ 구성 · ①은 이번 주 신호(편입 대기·미편입 포함)", sunday_sections)

    def sunday_empty():
        pd_, marks, matured = _fixture(1)
        pd_.book._p = pd_.book._p.iloc[0:0]
        pd_.book._s = pd_.book._s.iloc[0:0]
        saved = _patch(P, marks.iloc[0:0], matured.iloc[0:0])
        try:
            text, _ = R.sunday_scorecard(None, asof, "2026-11-13", pdata=pd_)
        finally:
            _undo(P, saved)
        for h in ("① 이번 주 새 추천 (0) ━━\n없음", "② 이번 주 만기 (0) · 20거래일 확정 ━━\n없음",
                  "종목 · 수익률 높은 순) ━━\n없음", "④ 누적 성적 (만기 20거래일) ━━\n없음"):
            assert h in text, (h, text)
    check(S, "0건인 칸은 '없음' 한 줄", sunday_empty)

    def sunday_40_and_split():
        pd_, marks, matured = _fixture(40)
        saved = _patch(P, marks, matured)
        try:
            text, info = R.sunday_scorecard(None, asof, "2026-11-13", pdata=pd_)
            assert not info["split"], info["lengths"]
            assert len(text) <= R.SEND_LIMIT
            tickers = [str(t) for t in marks["ticker"]]
            assert all(f"({t})" in text for t in tickers)
            # 상한을 억지로 낮춰 ③ 분리를 본다 — 종목은 빠지지 않는다.
            t2, i2 = R.sunday_scorecard(None, asof, "2026-11-13", pdata=pd_,
                                        limit=700)
        finally:
            _undo(P, saved)
        msgs = i2["messages"]
        assert i2["split"] and len(msgs) >= 3, i2["lengths"]
        assert "③ 추적 중 전체" in msgs[0] and "• 다음 메시지로 보냄" in msgs[0]
        assert "━━ ④ 누적 성적" in msgs[0] and "투자 판단 근거 아님" in msgs[0]
        rest = "\n".join(msgs[1:])
        assert all(f"({t})" in rest for t in tickers), "③ 종목이 빠졌다"
        assert all(f"({t})" not in msgs[0].split("━━ ③")[1].split("━━ ④")[0]
                   for t in tickers)
        assert all(len(m) <= 700 for m in msgs[1:]), i2["lengths"]
        assert msgs[1].startswith("🗓 <b>[주간 성적표 ③]</b>") and "(1/" in msgs[1]
    check(S, "추적 40종목 한 메시지 · 상한 넘으면 ③만 다음 메시지(생략 없음)",
          sunday_40_and_split)

    def curve_chain():
        m = pd.DataFrame([
            {"signal_d": "2026-10-01", "net": 10.0, "idx": 5.0},
            {"signal_d": "2026-10-01", "net": 10.0, "idx": 5.0},
            {"signal_d": "2026-10-08", "net": -10.0, "idx": 0.0},
        ])
        saved = (P.matured_on, P.matured_random)
        P.matured_on = lambda pd_, d=None, start=None: m.copy()
        P.matured_random = lambda pd_, sds, h=None: {
            "2026-10-01": [(2.0, 5)] * 3, "2026-10-08": [(-2.0, 5)] * 3}
        try:
            df = R.curve_points(None, "2026-11-13")
        finally:
            P.matured_on, P.matured_random = saved
        near(df["lvl_reco"].tolist()[-1], 99.0, 1e-9, "곡선")
        near(df["lvl_index"].tolist()[-1], 105.0, 1e-9, "곡선")
        near(df["lvl_random"].tolist()[-1], 102.0 * 0.98, 1e-9, "곡선")
        cap = R.curve_caption(df, "2026-11-13")
        assert "시작=100" in cap and "추천 99.0 · 지수 105.0" in cap, cap
        assert "누적합" not in cap
    check(S, "누적 곡선 = 코호트 연결 지수(시작=100) · 캡션", curve_chain)

    def weekly_file():
        import importlib
        rs = importlib.import_module("run_screen")
        body = rs.weekly_file_text("📅 <b>[주간 누적 분석]</b> x\n• <b>삼성E&amp;A</b> 1회",
                                   "■ v2 <i>a</i>", "2026-11-13")
        assert "<b>" not in body and "삼성E&A 1회" in body and "주간 분석 v2" in body
        p = rs.save_weekly_report("t", None, "2026-11-13",
                                  pattern=str(tmp / "rp" / "weekly_{d}.txt"))
        assert p is not None and p.name == "weekly_20261113.txt" and p.is_file()
    check(S, "주간 리포트 파일: 태그 제거 · weekly_YYYYMMDD.txt", weekly_file)

    def weekly_not_sent():
        # mode_weekly 는 _emit(전 수신자)을 부르지 않고 _emit_admin 한 줄만.
        import importlib
        import inspect
        rs = importlib.import_module("run_screen")
        src = inspect.getsource(rs.mode_weekly)
        assert "_emit(" not in src.replace("_emit_admin(", ""), "weekly 가 발송한다"
        assert '_emit_admin(f"주간 상세 저장됨 · {path}"' in src
    check(S, "금요일 weekly 발송 없음 · 관리자 한 줄", weekly_not_sent)
