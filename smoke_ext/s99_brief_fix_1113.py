# -*- coding: utf-8 -*-
"""2026-11-13 발송 전 수정: 아침 수집 줄 · ③ 상위·하위 · 정기보고서 한 줄 ·
이름 매칭 경계 · 같은 종목 사건 병합."""
from __future__ import annotations

from datetime import datetime

S = "11-13 브리핑 수정"


def run(check, near, tmp):
    def morning_line():
        from stocknews import news_health as nh
        calls = []
        orig = nh.load_rounds
        try:
            nh.load_rounds = lambda store, days=None, mode=None: (
                calls.append(mode) or [])
            assert nh.last_news_round_line(None) == (None, {"rounds": 0})
            nh.load_rounds = lambda store, days=None, mode=None: [
                (datetime(2026, 11, 12, 23, 3), {"naver": 0, "google": 9}),
                (datetime(2026, 11, 13, 6, 0),
                 {"naver": 5, "google": 9, "yahoo": 0, "dart": 3})]
            line, info = nh.last_news_round_line(None)
            assert line == "뉴스 소스 4개 중 3개 수집 (11-13 06:00 회차)", line
            assert info["missing"] == ["yahoo"]
            nh.load_rounds = lambda store, days=None, mode=None: [
                (datetime(2026, 11, 13, 6, 0), {})]      # 켠 소스 0개
            assert nh.last_news_round_line(None)[0] is None

            def boom(*a, **k):
                raise RuntimeError("x")
            nh.load_rounds = boom
            assert nh.last_news_round_line(None)[0] is None
        finally:
            nh.load_rounds = orig
        out = nh.append_line("☀️ 아침\n※ 끝", "뉴스 소스 4개 중 3개 수집 (11-13 06:00 회차)")
        assert nh.remove_line(out) == "☀️ 아침\n※ 끝"
    check(S, "아침 줄 = 직전 news 회차 · 기록 없음/조회 실패면 None", morning_line)

    def morning_mode_filter():
        from stocknews import news_health as nh
        from stocknews.store import Store
        st = Store(tmp / "rounds.db")
        got = {}
        orig = st.run_history
        st.run_history = lambda days=14, mode=None, limit=200: (
            got.update(mode=mode) or orig(days=days, mode=mode, limit=limit))
        assert nh.load_rounds(st, mode="news") == []
        assert got["mode"] == "news", got
    check(S, "load_rounds(mode='news') 가 runs 를 news 로만 조회", morning_mode_filter)

    def periodic_dart():
        from stocknews import news_v2 as nv
        f = nv.Focus(reco={"111110", "222220"}, tracked={"333330"},
                     d_ref="2026-11-12", names={"111110": "가나", "222220": "다라",
                                                "333330": "마바"})
        rows = [
            {"stock_code": "111110", "corp_name": "가나", "rcept_no": "9",
             "report_nm": "연결재무제표기준영업(잠정)실적(공정공시)"},
            {"stock_code": "111110", "corp_name": "가나", "rcept_no": "8",
             "report_nm": "[기재정정]분기보고서 (2026.09)"},
            {"stock_code": "111110", "corp_name": "가나", "rcept_no": "7",
             "report_nm": "분기보고서 (2026.09)"},
            {"stock_code": "222220", "corp_name": "다라", "rcept_no": "6",
             "report_nm": "반기보고서 (2026.06)"},
            {"stock_code": "333330", "corp_name": "마바", "rcept_no": "5",
             "report_nm": "사업보고서 (2025.12)"},
            {"stock_code": "333330", "corp_name": "마바", "rcept_no": "4",
             "report_nm": "주요사항보고서(유상증자결정)"},
        ]
        out, info = nv.dart_block(None, f, "2026-11-12",
                                  fetch=lambda b, e: {"rows": rows})
        body = [x for x in out if x.startswith("•")]
        assert body[-1] == "• 정기보고서 4건(가나·다라·마바)", body
        assert not any("분기보고서" in x or "반기보고서" in x for x in body[:-1]), body
        assert any("주요사항보고서" in x for x in body), "정기 아닌 보고서가 빠졌다"
        assert any("영업(잠정)실적" in x for x in body)
        assert info["periodic"] == 4 and info["rows"] == 6
        only = [r for r in rows if "분기" in r["report_nm"]]
        out2, _ = nv.dart_block(None, f, "2026-11-12", fetch=lambda b, e: {"rows": only})
        assert [x for x in out2 if x.startswith("•")] == ["• 정기보고서 2건(가나)"], out2
    check(S, "공시 블록: 정기보고서(기재정정 포함)는 맨 아래 한 줄", periodic_dart)

    def name_boundary():
        from stocknews.news import build_alias_index, map_tickers
        idx = build_alias_index({"237880": "클리오", "100590": "머큐리",
                                 "452400": "이닉스", "005930": "삼성전자",
                                 "000660": "SK하이닉스", "035420": "NAVER"})

        def codes(t):
            return sorted(c for c, _ in map_tickers(t, idx))
        assert codes("[위클리오늘] 외국인 순매도") == []
        assert codes("클리오, 3분기 흑자") == ["237880"]
        assert codes("[특징주]클리오 강세") == ["237880"]
        assert codes("JP모건, 머큐리 시스템즈 '비중확대' 상향") == []
        assert codes("머큐리시스템즈 실적") == []
        assert codes("머큐리, 단일판매 공급계약") == ["100590"]
        assert codes("머큐리 수주") == ["100590"]
        assert codes("美, 하이닉스 중국 공장 장비 제동") == []
        assert codes("SK하이닉스 급락") == ["000660"]
        assert codes("이닉스 상장") == ["452400"]
        assert codes("삼성전자가 상승") == ["005930"]
        assert codes("삼성전자·SK하이닉스 동반 하락") == ["000660", "005930"]
        assert codes("XNAVER 협업") == [] and codes("NAVER 웹툰") == ["035420"]
        # 같은 이름이 두 번: 하나만 유효해도 붙는다
        assert codes("위클리오늘 보도 — 클리오 신제품") == ["237880"]
        # DART 종목코드 경로는 경계와 무관
        assert [c for c, _ in map_tickers("[공시] x", idx, stock_code="237880")] == ["237880"]
    check(S, "이름 매칭: 다른 단어 일부·해외 회사명 일부는 붙이지 않음", name_boundary)

    def same_ticker_merge():
        from stocknews import config as C
        from stocknews import news_v2 as nv

        def r(i, t, origin="google"):
            return {"id": i, "title": t, "title_norm": None, "origin": origin,
                    "source": "s" + i}
        g1 = [r("a", "일동제약, 바이오 유럽서 파도프라잔·비만 신약 기술수출 모색")]
        g2 = [r("b", "일동제약, '파도프라잔' 해외 기술수출 논의 구체화")]
        g3 = [r("c", "일동제약, 3분기 영업이익 흑자전환")]
        g4 = [r("d", "제일약품, 파도프라잔 경쟁약 기술수출 논의 구체화")]
        codes = {"a": {"249420"}, "b": {"249420"}, "c": {"249420"}, "d": {"271980"}}
        out, st = nv.refine_groups([g1, g2, g3, g4], codes, sim=0.99)
        ids = [sorted(x["id"] for x in g) for g in out]
        assert ["a", "b"] in ids and ["c"] in ids and ["d"] in ids, ids
        assert st.get("merged_ticker") == 1, st
        # 두 종목 태깅 묶음은 이 단계에서 합치지 않는다
        two = {"a": {"005930", "000660"}, "b": {"005930", "000660"},
               "c": set(), "d": set()}
        out, st = nv.refine_groups([g1, g2], two, sim=0.99)
        assert len(out) == 2 and not st.get("merged_ticker"), st
        old = C.NEWS_V2_SAME_TICKER_MERGE
        try:
            C.NEWS_V2_SAME_TICKER_MERGE = 0
            out, _ = nv.refine_groups([g1, g2], codes, sim=0.99)
            assert len(out) == 2
        finally:
            C.NEWS_V2_SAME_TICKER_MERGE = old
    check(S, "같은 한 종목 묶음끼리만 사건 토큰으로 병합 · 2종목·끄면 안 함", same_ticker_merge)

    def event_norm_keeps_body():
        from stocknews.news import event_title_norm, normalize_title
        t = "한화에어로스페이스, 육군본부 과장단 초청…민·관·군 방산수출 협력 강조"
        assert "방산수출" not in normalize_title(t)            # 저장용(기존) 그대로
        assert "방산수출" in event_title_norm(t)
        assert event_title_norm("삼성전자 신제품 출시 - 연합뉴스") == "삼성전자 신제품 출시"
        assert event_title_norm(None) == ""
    check(S, "사건 비교용 정규화: 가운뎃점 뒤 본문 유지 · ' - 매체' 꼬리만 제거",
          event_norm_keeps_body)
