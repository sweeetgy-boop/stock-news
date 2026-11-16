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
        {"id": 4, "ticker": "000350", "name": "월요편입", "signal_d": "2026-11-13",
         "entry_date": "2026-11-16", "enrolled": 1, "slot": "VALUE",
         "skip_reason": None, "entry_status": "OK"},
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
        for h in ("① 이번 주 새 추천 (4)", "② 이번 주 만기 (1)",
                  "③ 추적 중 전체 (8종목", "④ 누적 성적", "※ 검증 중 · 투자 판단 근거 아님"):
            assert h in text, (h, text)
        assert "• 가나(000100) · 매집 · 편입 11-11 · +3.21%" in text, text
        assert "• 다라(000200) · 추세 · 미편입(상한가 시가) · -" in text, text
        assert "• 마바(000400) · 보충 · 편입 대기(다음 거래일 시가) · -" in text, text
        assert "• 월요편입(000350) · 매집 · 편입 대기(다음 거래일 시가) · -" in text, text
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

    def cohort_bars():
        m = pd.DataFrame([
            {"signal_d": "2026-10-01", "net": 10.0, "idx": 5.0},
            {"signal_d": "2026-10-01", "net": 6.0, "idx": 5.0},
            {"signal_d": "2026-10-08", "net": -10.0, "idx": 0.0},
        ])
        saved = (P.matured_on, P.matured_random)
        P.matured_on = lambda pd_, d=None, start=None: m.copy()
        P.matured_random = lambda pd_, sds, h=None: {
            "2026-10-01": [(2.0, 5), (4.0, 5), (9.0, 5)],
            "2026-10-08": [(-2.0, 5)] * 3}
        try:
            df = R.curve_points(None, "2026-11-13")
            png = R.curve_chart(None, "2026-11-13", tmp / "c" / "P00_curve.png")
        finally:
            P.matured_on, P.matured_random = saved
        assert df["signal_d"].tolist() == ["2026-10-01", "2026-10-08"]
        assert df["n"].tolist() == [2, 1]
        near(df["reco"][0], 8.0, 1e-9, "코호트")         # 같은 날 묶음 동일 비중 평균
        near(df["index"][0], 5.0, 1e-9, "코호트")
        near(df["random"][0], 4.0, 1e-9, "코호트")       # 무작위 묶음 평균의 중앙값
        near(df["reco"][1], -10.0, 1e-9, "코호트")
        assert not any(c.startswith("lvl_") for c in df.columns), "이어 붙인 지수가 남았다"
        assert png is not None and png.is_file() and png.stat().st_size > 1000
        cap = R.curve_caption(df, "2026-11-13")
        assert "10-01 (2종목): 추천 +8.00% · 지수 +5.00% · 무작위 +4.00%" in cap, cap
        assert "10-08 (1종목): 추천 -10.00% · 지수 +0.00% · 무작위 -2.00%" in cap, cap
        assert "코호트 = 같은 날 편입된 묶음" in cap
        assert "시작=100" not in cap and "이어 붙" not in cap and "누적" not in cap
    check(S, "일요일 차트 = 코호트별 추천·지수·무작위 막대 · 캡션 숫자", cohort_bars)

    def sunday_detail_file():
        import importlib
        rs = importlib.import_module("run_screen")
        m = pd.DataFrame([
            {"signal_d": "2026-10-01", "net": -4.0, "idx": -5.0, "ex_idx": 1.0,
             "mdd": -9.5, "slot": "VALUE", "regime": "상승"},
            {"signal_d": "2026-10-08", "net": 2.0, "idx": 0.0, "ex_idx": 2.0,
             "mdd": -3.0, "slot": "FILL*", "regime": None},
        ])
        saved = (P.matured_on, P.matured_random, P.pooled_percentile)
        P.matured_on = lambda pd_, d=None, start=None: m.copy()
        P.matured_random = lambda pd_, sds, h=None: {}
        P.pooled_percentile = lambda reco, rnd, col="net": (40.0, 2)
        try:
            txt = R.sunday_detail(None, "2026-11-13", asof)
        finally:
            P.matured_on, P.matured_random, P.pooled_percentile = saved
        for s in ("━━ 일요일 성적표 상세 (기준 거래일 2026-11-13) ━━",
                  "• 최대 낙폭 중앙 -6.25% · 최악 -9.50%", "[유형별 · 수익률(비용 차감)]",
                  "• 매집:", "• 보충:", "[국면별 · 수익률(비용 차감)]", "• 상승:",
                  "• 국면 없음:", "무작위 대비 백분위 40"):
            assert s in txt, (s, txt)
        pat = str(tmp / "rp2" / "weekly_{d}.txt")
        f = rs.save_weekly_report("금요일 본문", None, "2026-11-13", pattern=pat)
        p1 = rs.append_weekly_section("2026-11-13", txt, R.SUNDAY_DETAIL_HEAD, pattern=pat)
        p2 = rs.append_weekly_section("2026-11-13", txt, R.SUNDAY_DETAIL_HEAD, pattern=pat)
        assert f == p1 == p2
        body = p2.read_text(encoding="utf-8")
        assert "금요일 본문" in body and body.count(R.SUNDAY_DETAIL_HEAD) == 1, body
        assert body.index("금요일 본문") < body.index(R.SUNDAY_DETAIL_HEAD)
        p3 = rs.append_weekly_section("2026-11-20", txt, R.SUNDAY_DETAIL_HEAD, pattern=pat)
        assert p3.name == "weekly_20261120.txt" and p3.read_text(encoding="utf-8").startswith("━━ 일요일")
    check(S, "일요일 빠진 줄(낙폭·유형별·국면별) → 주간 파일에 덧붙임(재실행 1회분)",
          sunday_detail_file)

    def ops_notices_admin_only():
        import importlib
        import inspect
        nl = importlib.import_module("nightly")
        src = inspect.getsource(nl._notify)
        assert "send_admin(text)" in src and "send_telegram(" not in src
        rs = importlib.import_module("run_screen")
        hs = inspect.getsource(rs._holdings_msg)
        assert "_emit_admin(text" in hs and "if positions:" in hs
        es = inspect.getsource(rs.mode_exits)
        assert "if decs:" in es and "_emit_admin(render_exit_digest" in es
        ds = inspect.getsource(rs._daily_send_only)
        assert "_emit_admin(render_reco_missing" in ds
        # 실제 호출: _notify 는 첫 수신자에게만
        from stocknews import notify as N
        import os
        calls = []
        orig, old = N.send_telegram, os.environ.get("TELEGRAM_CHAT_ID")
        N.send_telegram = lambda text, chat_id=None, token=None, retries=3: (
            calls.append(chat_id) or N.SendReport(sent=1, failures=()))
        os.environ["TELEGRAM_CHAT_ID"] = "1111,2222"
        try:
            assert nl._notify("완료", lambda *_a: None, True) is True
        finally:
            N.send_telegram = orig
            if old is None:
                os.environ.pop("TELEGRAM_CHAT_ID", None)
            else:
                os.environ["TELEGRAM_CHAT_ID"] = old
        assert calls == ["1111"], calls
    check(S, "운영 상태 알림(잡 완료·스킵·실패 · 보유 0 · 청산 0 · 기록 없음) = 관리자만",
          ops_notices_admin_only)

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
