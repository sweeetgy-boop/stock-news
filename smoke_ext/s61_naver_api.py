# -*- coding: utf-8 -*-
"""[G] 네이버 증권 JSON API 수집 + 뉴스 소스 건강 — 네트워크 없음.

응답 표본은 smoke_ext/fixtures/naver_*.json (2026-11-12 실호출 저장본):
  naver_mainnews.json   list?category=mainnews   키 officeId/officeHname/articleId · datetime
  naver_focus_401.json  focus?sid=401 (시황·전망)  키 officeID/officeHName/articleID · date · url
  naver_focus_402.json  focus?sid=402 (종목분석)

검사: 키 대소문자 둘 다 · published 파싱(KST) · url 생성/그대로 · 섹션 라벨 ·
빈 결과 · 비200/리다이렉트/JSON 아님/구조 변경의 경고 원인 · 소스별 건수 ·
runs.note 기록 · 평일 연속 0건 알림(상태 변화 때만) · 브리핑 한 줄.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

S = "v2[G] 네이버 API·소스 건강"
FX = Path(__file__).resolve().parent / "fixtures"
KST = timezone(timedelta(hours=9))


def _fx(name: str) -> dict:
    return json.loads((FX / name).read_text(encoding="utf-8"))


class _Res:
    def __init__(self, status=200, body=b"", headers=None):
        self.status_code = status
        self.content = body
        self.headers = headers or {"Content-Type": "application/json; charset=utf-8"}


def _route(overrides=None):
    """URL → 표본 응답. overrides: {'mainnews'|'401'|'402': _Res}."""
    overrides = overrides or {}
    calls: list = []

    def fake_get(url, headers=None, timeout=None, allow_redirects=True, **kw):
        calls.append({"url": url, "headers": dict(headers or {}),
                      "allow_redirects": allow_redirects})
        key = ("mainnews" if "category=mainnews" in url else
               "401" if "sid=401" in url else "402" if "sid=402" in url else None)
        if key in overrides:
            return overrides[key]
        fn = {"mainnews": "naver_mainnews.json", "401": "naver_focus_401.json",
              "402": "naver_focus_402.json"}[key]
        return _Res(200, (FX / fn).read_bytes())
    return fake_get, calls


class _Patch:
    def __init__(self):
        self.saved = []

    def set(self, obj, name, val):
        self.saved.append((obj, name, getattr(obj, name)))
        setattr(obj, name, val)

    def undo(self):
        for obj, name, val in reversed(self.saved):
            setattr(obj, name, val)
        self.saved.clear()


def run(check, near, tmp):
    from stocknews import news_sources as ns
    from stocknews import news_health as nh

    def parse_fixtures():
        main, why = ns.parse_naver_articles(_fx("naver_mainnews.json"), "주요뉴스")
        assert why is None and len(main) == 20, (why, len(main))
        a = _fx("naver_mainnews.json")["articles"][0]
        m0 = main[0]
        assert m0["url"] == (f"https://n.news.naver.com/mnews/article/"
                             f"{a['officeId']}/{a['articleId']}"), m0["url"]
        assert m0["published"] == datetime.strptime(
            a["datetime"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=KST), m0["published"]
        assert m0["published"].utcoffset() == timedelta(hours=9)
        assert (m0["source"], m0["origin"], m0["region"], m0["lang"],
                m0["category_hint"]) == ("네이버금융", "NAVER", "KR", "ko", "주요뉴스")
        for fn, cat in (("naver_focus_401.json", "시황·전망"),
                        ("naver_focus_402.json", "종목분석")):
            data = _fx(fn)
            got, why = ns.parse_naver_articles(data, cat)
            assert why is None and len(got) == 20, (fn, why, len(got))
            b = data["articles"][0]
            assert "officeID" in b and "officeId" not in b, "표본 키가 focus 형식 아님"
            assert got[0]["url"] == b["url"], "focus 는 응답 url 그대로"
            assert got[0]["published"] == datetime.strptime(
                b["date"], "%Y%m%d%H%M%S").replace(tzinfo=KST)
            assert all(g["category_hint"] == cat and g["title"] for g in got)
        # url 이 없는 focus 행은 대문자 키로 만든다
        got, _ = ns.parse_naver_articles(
            {"articles": [{"officeID": "011", "articleID": "0004681506",
                           "title": "t &amp; u", "date": "20261112154213"}]}, "종목분석")
        assert got[0]["url"] == "https://n.news.naver.com/mnews/article/011/0004681506"
        assert got[0]["title"] == "t & u", "HTML 엔티티 미해제"
        assert ns._parse_naver_time("bad") is None and ns._parse_naver_time(None) is None
    check(S, "표본 파싱: 키 대소문자 둘 다 · KST 시각 · url · 섹션 라벨 · 라벨 네이버금융", parse_fixtures)

    def empty_and_errors():
        got, why = ns.parse_naver_articles({"articles": []}, "주요뉴스")
        assert got == [] and why == "기사 0건"
        got, why = ns.parse_naver_articles({"page": "1"}, "주요뉴스")
        assert got == [] and why.startswith("응답 구조 변경"), why
        d, why = ns.naver_decode(302, {"Location": "https://stock.naver.com/news"}, b"")
        assert d is None and why == "리다이렉트 302 → https://stock.naver.com/news", why
        d, why = ns.naver_decode(404, {}, b"")
        assert d is None and why == "HTTP 404"
        d, why = ns.naver_decode(200, {"Content-Type": "text/html"}, b"<html></html>")
        assert d is None and why.startswith("JSON 아님 (content-type=text/html"), why
        d, why = ns.naver_decode(200, {}, (FX / "naver_mainnews.json").read_bytes())
        assert why is None and len(d["articles"]) == 20
    check(S, "빈 결과 · 리다이렉트/비200/JSON 아님/구조 변경 → 원인 문자열", empty_and_errors)

    def collect_offline():
        p = _Patch()
        fake, calls = _route()
        p.set(ns.requests, "get", fake)
        p.set(ns.time, "sleep", lambda *_a, **_k: None)
        try:
            ns.LAST_WARN.clear()
            out = ns.collect_naver()
        finally:
            p.undo()
        assert len(out) == 60, len(out)
        cats = [o["category_hint"] for o in out]
        assert cats == ["주요뉴스"] * 20 + ["시황·전망"] * 20 + ["종목분석"] * 20
        assert all(o["published"] is not None for o in out), "published 가 None"
        assert len(calls) == 3 and all(c["allow_redirects"] is False for c in calls)
        assert all(c["headers"].get("Accept") == "application/json" and
                   c["headers"].get("Referer") == "https://stock.naver.com/news/mainnews"
                   for c in calls)
        assert "pageSize=20" in calls[0]["url"] and "date=" not in calls[0]["url"]
        for c in calls[1:]:
            assert "date=" in c["url"] and "enableFallback=true" in c["url"] \
                and "maxDays=7" in c["url"], c["url"]
        assert not ns.LAST_WARN, ns.LAST_WARN
        assert not hasattr(ns, "_NAVER_PAGES"), "HTML 경로가 남아 있다"
    check(S, "collect_naver: 표본 3개 → 60건 · 리다이렉트 안 따라감 · 헤더 · focus date", collect_offline)

    def collect_warns():
        p = _Patch()
        fake, _ = _route({
            "mainnews": _Res(302, b"", {"Location": "https://stock.naver.com/news/mainnews"}),
            "401": _Res(200, b"<html>spa</html>", {"Content-Type": "text/html"}),
            "402": _Res(200, b'{"articles": []}')})
        p.set(ns.requests, "get", fake)
        p.set(ns.time, "sleep", lambda *_a, **_k: None)
        msgs: list[str] = []
        # 스모크는 로깅을 꺼 두므로 핸들러 대신 warning 자체를 가로챈다
        p.set(ns.log, "warning", lambda m, *a, **k: msgs.append(m % a))
        try:
            ns.LAST_WARN.clear()
            out = ns.collect_naver()
        finally:
            p.undo()
        assert out == []
        w = ns.LAST_WARN.get("naver", "")
        assert "주요뉴스: 리다이렉트 302 → https://stock.naver.com/news/mainnews" in w, w
        assert "시황·전망: JSON 아님" in w and "종목분석: 기사 0건" in w, w
        assert any("리다이렉트 302" in m for m in msgs), msgs
        assert not any("셀렉터 미매칭" in m for m in msgs)
    check(S, "collect_naver: 302·HTML·빈 결과 → 경고에 원인 · LAST_WARN", collect_warns)

    def collect_all_counts():
        p = _Patch()
        p.set(ns, "collect_naver", lambda: [{"t": 1}] * 0)
        p.set(ns, "collect_google", lambda: [{"t": 1}] * 5)
        p.set(ns, "collect_yahoo", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        p.set(ns, "collect_dart", lambda: [{"t": 1}] * 2)
        old_key = os.environ.pop("DART_API_KEY", None)
        try:
            c: dict = {}
            items = ns.collect_all(counts=c)
            assert len(items) == 5 + 2
            assert c == {"naver": 0, "google": 5, "yahoo": 0}, c   # DART 키 없음 = 끔
            assert "RuntimeError" in ns.LAST_WARN.get("yahoo", "")
            os.environ["DART_API_KEY"] = "x"
            c = {}
            ns.collect_all(use_google=False, counts=c)
            assert c == {"naver": 0, "yahoo": 0, "dart": 2}, c      # 끈 소스는 키 없음
            assert ns.enabled_sources(use_naver=False) == ("google", "yahoo", "dart")
            # counts 를 안 주면 예전과 같다(반환 목록만)
            assert len(ns.collect_all()) == 7
        finally:
            os.environ.pop("DART_API_KEY", None)
            if old_key is not None:
                os.environ["DART_API_KEY"] = old_key
            p.undo()
    check(S, "collect_all(counts=): 켜진 소스만 · 예외=0 · DART 키 없으면 제외", collect_all_counts)

    def store_roundtrip():
        from stocknews.news import process_and_store
        from stocknews.store import Store
        st = Store(tmp / "naver.db")
        raw, _ = ns.parse_naver_articles(_fx("naver_focus_402.json"), "종목분석")
        res = process_and_store(st, raw)
        assert res["stored"] > 0, res
        df = st.news_since(hours=24 * 3650)
        nav = df[df["origin"] == "NAVER"]
        assert len(nav) == res["stored"] and set(nav["source"]) == {"네이버금융"}
        assert nav["published"].notna().all(), "published 가 비었다"
        assert nav["published"].str.match(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d$").all(), \
            nav["published"].head().tolist()
    check(S, "process_and_store: NAVER 행 published 채워짐(naive KST) · 라벨 유지", store_roundtrip)

    def note_roundtrip():
        n = nh.format_counts({"naver": 60, "google": 212, "yahoo": 0})
        assert n == "naver:60,google:212,yahoo:0"
        assert nh.parse_counts(f"rc=0 news_sources={n}") == {"naver": 60, "google": 212, "yahoo": 0}
        assert nh.parse_counts("rc=0 news_sources=-") == {}
        assert nh.parse_counts("rc=0 trade_date=2026-11-12") is None
        assert nh.parse_counts(None) is None
    check(S, "runs.note news_sources= 직렬화 왕복", note_roundtrip)

    weekday = lambda t: (t.date() if isinstance(t, datetime) else t).weekday() < 5  # noqa: E731

    def streak_rules():
        # 2026-11-09(월) ~ : 하루 3회차(06·21·23시)
        def at(day, hh):
            return datetime(2026, 11, day, hh, 0)
        r = [(at(9, 6), {"naver": 5, "google": 9}),
             (at(9, 21), {"naver": 0, "google": 9}),
             (at(9, 23), {"naver": 0, "google": 9})]
        assert nh.zero_streak(r, "naver", weekday) == 2
        ev = nh.evaluate(r, {"naver": 0, "google": 9}, at(10, 6), weekday, 3)
        assert ev == [{"source": "naver", "kind": "down", "streak": 3}], ev
        r.append((at(10, 6), {"naver": 0, "google": 9}))
        assert nh.evaluate(r, {"naver": 0, "google": 9}, at(10, 21), weekday, 3) == [], \
            "4회째에 다시 알림"
        r.append((at(10, 21), {"naver": 0, "google": 9}))
        ev = nh.evaluate(r, {"naver": 7, "google": 9}, at(10, 23), weekday, 3)
        assert ev == [{"source": "naver", "kind": "up", "after": 4, "count": 7}], ev
        # 주말 회차는 세지도 끊지도 않는다 · 끈 회차(키 없음)도 마찬가지
        r2 = [(at(13, 21), {"naver": 0}),            # 금
              (at(14, 21), {"naver": 5}),            # 토 — 무시
              (at(15, 21), {"naver": 0}),            # 일 — 무시
              (at(16, 6), {"google": 3}),            # 월 · naver 끔 — 무시
              (at(16, 21), {"naver": 0})]            # 월
        assert nh.zero_streak(r2, "naver", weekday) == 2
        assert nh.evaluate(r2, {"naver": 0}, at(14, 23), weekday, 3) == [], "토요일 회차가 셌다"
        assert nh.evaluate(r2, {"naver": 0}, at(16, 23), weekday, 3)[0]["streak"] == 3
        txt = nh.alert_text([{"source": "naver", "kind": "down", "streak": 3}],
                            {"naver": "주요뉴스: 리다이렉트 302 → x"})
        assert txt == ("⚠️ 뉴스 소스 네이버 0건 3회 연속 (평일 수집 회차)"
                       " · 원인: 주요뉴스: 리다이렉트 302 → x"), txt
        for bad in ("매수", "매도"):
            assert bad not in txt
    check(S, "연속 0건: 3회째 1번만 · 4회째 침묵 · 복구 1번 · 주말·끈 회차 무시", streak_rules)

    def alert_admin_only():
        from stocknews import notify as nt
        from stocknews.store import Store
        st = Store(tmp / "health.db")
        now = datetime.now(KST).replace(tzinfo=None, microsecond=0)
        # 최근 평일 3개 시각(오늘 포함 이전) — 실제 날짜에 묶이지 않게 요일만 본다
        days = []
        d = now
        while len(days) < 3:
            if d.weekday() < 5:
                days.append(d)
            d -= timedelta(days=1)
        days.reverse()
        p = _Patch()
        sent: list = []
        p.set(nh, "_counted_fn", lambda store: weekday)
        p.set(nt, "send_admin", lambda text, dry_run=False, **k: sent.append((text, dry_run)))
        p.set(nt, "send_telegram", lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("전 수신자 발송 경로 호출")))
        try:
            for t in days[:2]:
                st.log_run("news", t - timedelta(minutes=5), 1, 0,
                           note="rc=0 news_sources=naver:0,google:10")
            info = nh.check_and_alert(st, {"naver": 0, "google": 10}, dry_run=True,
                                      now=days[2])
            assert sent == [("⚠️ 뉴스 소스 네이버 0건 3회 연속 (평일 수집 회차)", True)], sent
            assert info["events"][0]["kind"] == "down" and info["sent"] is None
            st.log_run("news", days[2], 1, 0, note="rc=0 news_sources=naver:0,google:10")
            info = nh.check_and_alert(st, {"naver": 0, "google": 10}, dry_run=True,
                                      now=days[2] + timedelta(minutes=30))
            assert info == {} and len(sent) == 1, "같은 사태로 반복 알림"
        finally:
            p.undo()
    check(S, "알림: send_admin 한 번(dry-run) · 전 수신자 경로 안 탐 · 반복 없음", alert_admin_only)

    def brief_line():
        from stocknews.store import Store
        st = Store(tmp / "line.db")
        now = datetime(2026, 11, 12, 6, 20)
        p = _Patch()
        p.set(nh, "load_rounds", lambda store, days=None: [
            (datetime(2026, 11, 11, 13, 0), {"naver": 9, "google": 9, "yahoo": 9}),  # 창 밖
            (datetime(2026, 11, 11, 21, 30), {"naver": 0, "google": 9, "yahoo": 0}),
            (datetime(2026, 11, 11, 23, 0), {"naver": 0, "google": 9, "yahoo": 0})])
        try:
            line, info = nh.source_line(st, ("naver", "google", "yahoo"), 16, now=now)
            assert line == "뉴스 소스 3개 중 1개 수집", line
            assert info["missing"] == ["naver", "yahoo"] and info["rounds"] == 2
            line, _ = nh.source_line(st, ("naver", "google", "yahoo", "dart"), 16,
                                     current={"naver": 4, "google": 1, "yahoo": 0,
                                              "dart": 3}, now=now)
            assert line == "뉴스 소스 4개 중 3개 수집", line
        finally:
            p.undo()
        line, info = nh.source_line(st, ("naver",), 16, now=now)   # 이력 없음
        assert line == "뉴스 소스 1개 중 0개 수집" and info["rounds"] == 0
        ev = "🌙 저녁\n\n본문\n\n⏰ 2026-11-12 23:00:00"
        out = nh.append_line(ev, "뉴스 소스 4개 중 4개 수집")
        assert out == "🌙 저녁\n\n본문\n\n뉴스 소스 4개 중 4개 수집\n⏰ 2026-11-12 23:00:00"
        assert nh.remove_line(out) == ev
        mo = "☀️ 아침\n※ 끝"
        out = nh.append_line(mo, "뉴스 소스 4개 중 4개 수집")
        assert out == "☀️ 아침\n※ 끝\n\n뉴스 소스 4개 중 4개 수집" and nh.remove_line(out) == mo
    check(S, "브리핑 한 줄: n=켜진 소스 · m=창 안 회차에서 ≥1건 · ⏰ 위/맨 끝", brief_line)

    def run_screen_glue():
        import argparse
        import run_screen as RS
        from stocknews.store import Store
        st = Store(tmp / "glue.db")
        p = _Patch()
        sent_admin: list = []

        def fake_collect(use_naver=True, use_google=True, use_yahoo=True,
                         use_dart=True, counts=None):
            if counts is not None:
                counts.update({"naver": 0, "google": 3})
            ns.LAST_WARN.clear()
            ns.LAST_WARN["naver"] = "주요뉴스: HTTP 404"
            return []
        from stocknews import notify as nt
        p.set(RS, "collect_all", fake_collect)
        p.set(nt, "send_admin", lambda text, dry_run=False, **k: sent_admin.append(dry_run))
        saved = dict(RS.SUMMARY)
        try:
            RS.SUMMARY.clear()
            args = argparse.Namespace(no_naver=False, no_google=False, no_yahoo=False,
                                      no_dart=False, dry_run=True)
            RS._collect_news(st, args)
            assert RS.SUMMARY["news_sources"] == {"naver": 0, "google": 3}
            assert RS.SUMMARY["news_source_warn"] == {"naver": "주요뉴스: HTTP 404"}
            note = RS._run_note(0)
            assert note.endswith("news_sources=naver:0,google:3"), note
            assert nh.parse_counts(note) == {"naver": 0, "google": 3}
            RS.SUMMARY.clear()
            assert "news_sources" not in RS._run_note(0), "수집 안 한 모드에 붙음"
            import inspect
            for fn in (RS.mode_news, RS.mode_brief_morning, RS.mode_brief_evening,
                       RS._collect_news):
                assert "log_run(" not in inspect.getsource(fn), "모드 안에서 runs 직접 기록"
            for fn in (RS.mode_news, RS.mode_brief_morning, RS.mode_brief_evening):
                src = inspect.getsource(fn)
                assert "collect_all(" not in src, f"{fn.__name__} 가 건수 경로를 우회"
        finally:
            RS.SUMMARY.clear()
            RS.SUMMARY.update(saved)
            p.undo()
    check(S, "run_screen: 건수 → SUMMARY → runs.note(main 한 곳) · 세 수집 경로 공용", run_screen_glue)

    def fixtures_clean():
        import re
        for f in sorted(FX.glob("naver_*.json")):
            t = f.read_text(encoding="utf-8")
            assert not re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", t), f"{f.name}: 이메일 형태"
            assert not re.search(r"\b01[016789]-\d{3,4}-\d{4}\b", t), f"{f.name}: 전화번호 형태"
    check(S, "표본 파일: 이메일·전화번호 형태 없음", fixtures_clean)
