# -*- coding: utf-8 -*-
"""[G] 뉴스 v2 — 가상 데이터 검사 (네트워크 없음).

정렬 우선순위(추천·추적 종목 기사가 맨 위), 묶기(대표 1건 + "외 n건"),
DART 공시 블록 형식, 밤사이 시장 실패 시 생략·기준 시각, OFF 불변.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pandas as pd

S = "v2[G] 뉴스"


def _now():
    from stocknews.trading_day import now_kst
    return now_kst()


def _news(i, title, src, url, *, origin="GOOGLE", region="KR",
          category="기타", importance=1.0, hours_ago=1):
    from stocknews.news import normalize_title
    pub = (_now() - timedelta(hours=hours_ago)).replace(microsecond=0)
    return {"id": f"g{i:03d}", "d": pub.strftime("%Y-%m-%d"),
            "published": pub.isoformat(timespec="seconds"),
            "title": title, "title_norm": normalize_title(title), "url": url,
            "source": src, "origin": origin, "region": region,
            "category": category, "cluster_id": f"g{i:03d}", "cluster_n": 1,
            "importance": importance, "lang": "ko"}


def _reco(t, name):
    return SimpleNamespace(ticker=t, name=name, price=10000.0, grade="A",
                           value_score=5.0, trend_score=5.0, reasons=["x"])


class _Patch:
    """모듈 속성 바꿔 끼우기 + 원복."""

    def __init__(self):
        self.saved = []

    def set(self, mod, name, val):
        self.saved.append((mod, name, getattr(mod, name)))
        setattr(mod, name, val)

    def undo(self):
        for mod, name, val in reversed(self.saved):
            setattr(mod, name, val)
        self.saved.clear()


def _fake_reader_ok(sym, start):
    idx = pd.to_datetime(["2026-11-06", "2026-11-09", "2026-11-10", "2026-11-11"])
    base = {"DJI": 52000.0, "US500": 7500.0, "IXIC": 25000.0, "USD/KRW": 1340.0}[sym]
    return pd.DataFrame({"Close": [base * 0.99, base, base * 1.01, base * 1.02]},
                        index=idx)


def run(check, near, tmp):
    from stocknews import config as C
    from stocknews import events as ev
    from stocknews import news_v2 as nv
    from stocknews import v2_hooks
    from stocknews.renderer import render_evening_brief, render_morning_brief
    from stocknews.store import Store

    today = _now().strftime("%Y-%m-%d")
    st = Store(tmp / "news_v2.db")
    # 추천: 오늘 recos 에 A(000010) — 이벤트 기간 안. 뉴스 태깅 대상.
    st.save_recos(today, [("VALUE", _reco("000010", "가나전자"))])
    rows = [
        # 추천 종목 기사 — 중요도 낮음. ON 이면 맨 위 섹션이어야 한다.
        _news(1, "가나전자 신규 공장 착공 소식 전해져", "매체A", "https://a/1",
              importance=1.0),
        # 같은 사건 3건 (매체만 다름) → 대표 1 + 외 2건
        _news(2, "미국 연준 기준금리 동결 결정 발표", "매체A", "https://a/2",
              region="US", category="매크로", importance=6.0),
        _news(3, "미국 연준 기준금리 동결 결정 발표", "매체B", "https://b/3",
              region="US", category="매크로", importance=6.0),
        _news(4, "미국 연준 기준금리 동결 결정 발표", "매체C", "https://c/4",
              region="US", category="매크로", importance=6.0),
        # 고중요도 비대상 기사
        _news(5, "반도체 수출 사상 최대 기록 경신", "매체D", "https://d/5",
              category="반도체", importance=8.0),
    ]
    st.upsert_news(rows)
    st.link_news_tickers([("g001", "000010", "가나전자")])

    p = _Patch()

    def _on(dart_rows=None, reader=None):
        p.set(C, "NEWS_V2", True)
        p.set(nv, "fetch_dart_disclosures",
              (lambda b, e: {"rows": list(dart_rows or []), "truncated": False})
              if dart_rows is not False else (lambda b, e: None))
        p.set(nv, "_fdr_reader", reader or _fake_reader_ok)

    def priority():
        _on()
        try:
            text = render_morning_brief(st, _now(), hours=24)
        finally:
            p.undo()
        assert "━━ ⭐ 추천·추적 종목 (1건) ━━" in text, text
        star = text.index("⭐ 추천·추적 종목")
        for other in ("🌏 밤사이 해외", "🏛 매크로·정책", "🎯 내 종목·고중요도"):
            if other in text:
                assert star < text.index(other), f"⭐ 가 {other} 보다 뒤"
        sec = text[star:].split("━━ ", 1)[0]
        assert "가나전자 신규 공장" in sec, sec
        assert "반도체 수출" not in sec, "비대상 고중요도 기사가 ⭐ 섹션에 들어감"
    check(S, "추천 종목 기사가 맨 위 섹션 · 비대상은 제외", priority)

    def window():
        # 이벤트 기간(20거래일) 밖의 추천은 대상이 아니다.
        cal = pd.bdate_range(end=today, periods=30).strftime("%Y-%m-%d").tolist()
        if cal[-1] != today:
            cal.append(today)
        st2 = Store(tmp / "news_v2_win.db")
        st2.save_recos(cal[-25], [("VALUE", _reco("000020", "오래된"))])
        st2.save_recos(cal[-3], [("TREND", _reco("000030", "최근"))])
        p.set(ev, "trading_calendar", lambda store, since=None: list(cal))
        # 폴백 경로(send_targets 가 None — B 미구현)를 고정해서 본다.
        # B 병합 후 훅은 항상 list 를 돌리므로 여기서 None 으로 패치한다.
        p.set(v2_hooks, "send_targets", lambda store, d: None)
        try:
            f = nv.focus_tickers(st2, _now())
        finally:
            p.undo()
        assert f.source == "events" and f.reco == {"000030"}, (f.source, f.reco)
        assert f.d_ref == cal[-3]
        # B 경로: 실제 훅(필터 OFF = 그날 recos 전체)이면 그날 종목만.
        p.set(ev, "trading_calendar", lambda store, since=None: list(cal))
        try:
            g = nv.focus_tickers(st2, _now())
        finally:
            p.undo()
        assert g.source == "send_targets" and g.reco == {"000030"}, (g.source, g.reco)
    check(S, "추천 대상 = [0] 이벤트 기간 안(21거래일 전 추천은 제외)", window)

    def hooks():
        p.set(v2_hooks, "send_targets", lambda store, d: [{"ticker": "000099"}])
        p.set(v2_hooks, "tracked_tickers", lambda store, d: {"000077"})
        try:
            f = nv.focus_tickers(st, _now())
        finally:
            p.undo()
        assert f.source == "send_targets" and f.reco == {"000099"}, f.reco
        assert f.tracked == {"000077"} and f.label("000077") == "추적"
        assert f.all == {"000099", "000077"}
    check(S, "send_targets 가 있으면 그 목록 · 추적 종목 합집합", hooks)

    def grouping():
        _on()
        try:
            text = render_morning_brief(st, _now(), hours=24)
        finally:
            p.undo()
        assert text.count("미국 연준 기준금리 동결 결정 발표") == 1, \
            "같은 사건이 두 줄 이상"
        line = next(l for l in text.splitlines() if "연준 기준금리" in l)
        assert "<i>외 2건</i>" in line, line
        assert "(3개 매체)" in line, line
    check(S, "같은 사건 = 대표 1줄 + '외 n건'", grouping)

    def title_merge():
        from stocknews.news import normalize_title
        a = "HD한국조선해양, LNG 운반선 2척 6천629억원에 수주"
        b = "HD한국조선해양, LNG운반선 2척 6629억원 수주…年 목표 93% 달성"
        assert nv.title_sim(normalize_title(a), normalize_title(b)) >= C.NEWS_V2_TITLE_SIM

        def row(i, t, origin="GOOGLE", src="S"):
            return pd.Series({"id": f"m{i}", "title": t, "source": f"{src}{i}",
                              "title_norm": normalize_title(t), "origin": origin,
                              "collected": f"2026-11-1{i}"})
        g = [[row(1, a)], [row(2, b)]]
        out, stt = nv.refine_groups(g, {})
        assert len(out) == 1 and len(out[0]) == 2 and stt["merged"] == 1
        # DART: 같은 서식이라도 회사가 다르면 묶지 않고, 묶여 와도 뗀다.
        d1 = row(3, "[공시] 가나 영업(잠정)실적(공정공시)", "DART")
        d2 = row(4, "[공시] 다라 영업(잠정)실적(공정공시)", "DART")
        out, stt = nv.refine_groups([[d1, d2]], {"m3": {"000010"}, "m4": {"000020"}})
        assert len(out) == 2 and stt["dart_split"] == 1, (len(out), stt)
        out, _ = nv.refine_groups([[d1], [d2]], {})
        assert len(out) == 2, "DART 대표끼리 제목 유사도로 묶임"
    check(S, "제목 유사도 병합 · DART 는 분리·병합 제외", title_merge)

    def dart_format():
        dart = [
            {"stock_code": "000010", "corp_name": "가나전자",
             "report_nm": "주요사항보고서(자기주식취득결정)",
             "rcept_no": "20261111000123", "rcept_dt": "20261111"},
            {"stock_code": "000010", "corp_name": "가나전자",
             "report_nm": "기업설명회(IR)개최", "rcept_no": "20261111000200",
             "rcept_dt": "20261111"},
            {"stock_code": "000010", "corp_name": "가나전자",
             "report_nm": "기업설명회(IR)개최", "rcept_no": "20261111000100",
             "rcept_dt": "20261111"},
            {"stock_code": "999990", "corp_name": "비대상",
             "report_nm": "유상증자결정", "rcept_no": "20261111000999",
             "rcept_dt": "20261111"},
        ]
        _on(dart_rows=dart)
        try:
            text = render_morning_brief(st, _now(), hours=24)
        finally:
            p.undo()
        lines = text.splitlines()
        h = next(i for i, l in enumerate(lines) if l.startswith("━━ 📑 추천·추적 종목 공시"))
        body = []
        for l in lines[h + 1:]:
            if not l.strip():
                break
            body.append(l)
        assert body == [
            '• <a href="https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20261111000200">'
            '가나전자 기업설명회(IR)개최</a> (2건)',
            '• <a href="https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20261111000123">'
            '가나전자 주요사항보고서(자기주식취득결정)</a>',
        ], body
        assert "비대상" not in text
        assert text.index("📑") < text.index("⭐"), "공시 블록이 뉴스 위가 아님"
    check(S, "공시 블록: 대상 종목만 · 제목+링크 · 뉴스 위", dart_format)

    def dart_none():
        # 대상 종목이 없으면 블록 자체가 없다. 조회 실패 + 수집분 없음은 한 줄.
        st3 = Store(tmp / "news_v2_empty.db")
        _on(dart_rows=False)
        try:
            text = render_morning_brief(st3, _now(), hours=24)
            f = nv.Focus(reco={"000010"}, d_ref=today)
            lines, info = nv.dart_block(st, f, _now(), fetch=lambda b, e: None)
        finally:
            p.undo()
        assert "📑" not in text
        assert info["source"] == "db_fallback" and info["rows"] == 0
        assert any("조회 불가" in l for l in lines), lines
    check(S, "공시 블록: 대상 없음 = 생략 · 조회 실패 = 사유 한 줄", dart_none)

    def market_fail():
        def boom(sym, start):
            raise ConnectionError("offline")
        _on(reader=boom)
        try:
            text = render_morning_brief(st, _now(), hours=24)
        finally:
            p.undo()
        assert "밤사이 시장" not in text, "실패했는데 블록이 남음"
        assert "⭐ 추천·추적 종목" in text, "시장 실패가 브리핑을 막음"
        # 일부만 실패하면 그 줄만 빠진다
        def half(sym, start):
            if sym == "IXIC":
                raise TimeoutError("x")
            return _fake_reader_ok(sym, start)
        rows_ = nv.fetch_market(datetime(2026, 11, 11, 6, 20), reader=half)
        assert [r["label"] for r in rows_] == ["다우", "S&P500", "원/달러"], rows_
    check(S, "밤사이 시장: 전부 실패 = 블록 생략 · 일부 실패 = 그 줄만", market_fail)

    def market_basis():
        # 06:20 KST = 16:20 EST → 11/10 봉이 마감. 05:00 KST = 15:00 EST → 진행 중.
        r1 = nv.fetch_market(datetime(2026, 11, 11, 6, 20), reader=_fake_reader_ok)
        dj = r1[0]
        assert dj["base"] == "11/10 뉴욕 마감", dj
        near(dj["value"], 52000.0 * 1.01, 1e-6, "다우 값")
        near(dj["chg_pct"], 1.0, 1e-9, "다우 등락")
        r2 = nv.fetch_market(datetime(2026, 11, 11, 5, 0), reader=_fake_reader_ok)
        assert r2[0]["base"] == "11/09 뉴욕 마감", r2[0]
        fx = [r for r in r1 if r["kind"] == "fx"][0]
        assert fx["base"] == "11/11 06:20 KST 조회 시세", fx
        blk = nv.market_block(r1)
        assert blk[0] == "━━ 🌐 밤사이 시장 ━━"
        assert blk[1] == "• 다우 52,520.00 (+1.00%) · 11/10 뉴욕 마감", blk[1]
        assert nv.market_block([]) == []
    check(S, "밤사이 시장: 마감 판정 · 기준 시각 표시", market_basis)

    def evening_on():
        _on(dart_rows=[])
        try:
            text = render_evening_brief(st, _now(), hours=24)
        finally:
            p.undo()
        assert "━━ 📑 추천·추적 종목 공시" in text and "해당 공시 없음" in text
        assert "⭐ 추천·추적 종목" in text
        assert "밤사이 시장" not in text, "시장 요약은 아침 전용"
        assert "📌 관심종목 현황" in text
    check(S, "저녁 브리핑 ON: 공시 블록 + ⭐ · 시장 요약 없음", evening_on)

    def off_invariant():
        # OFF 면 v2 경로를 부르지 않는다(불리면 즉시 실패하게 바꿔 끼움).
        def boom(*a, **k):
            raise AssertionError("OFF 인데 news_v2 렌더가 불림")
        p.set(C, "NEWS_V2", False)
        p.set(nv, "render_morning_v2", boom)
        p.set(nv, "render_evening_v2", boom)
        p.set(nv, "_fdr_reader", boom)
        p.set(nv, "fetch_dart_disclosures", boom)
        try:
            m = render_morning_brief(st, _now(), hours=24)
            e = render_evening_brief(st, _now(), hours=24)
        finally:
            p.undo()
        for t in (m, e):
            for mark in ("⭐", "📑", "🌐", "외 2건"):
                assert mark not in t, f"OFF 출력에 v2 표식 {mark}"
        assert "(3개 매체)" in m, "OFF 기존 표기(N개 매체) 사라짐"
        assert C.NEWS_V2 is False, "기본값은 OFF"
    check(S, "OFF = 기존 경로 그대로(v2 미호출·표식 없음) · 기본 OFF", off_invariant)
