# -*- coding: utf-8 -*-
"""2026-11-20 저녁: 【최종 추천 · 차트 첨부】 · 분쟁 줄 주가 흐름 · 첫 보도 즉시 알림 ·
【고배당】 · 【재무 순위 상위】 · 새 구역의 안전장치(일부러 넣은 오류). 기록·표시 전용 · 기본 OFF.
"""
from __future__ import annotations

import json
import sqlite3
import types
from contextlib import closing
from datetime import date, datetime, timedelta

S = "11-20 최종 추천·고배당·즉시 알림"
FLAGS = ("PULLBACK_LIST", "DISPUTE_LIST", "HALF_DROP_WARN", "DISPUTE_NEWS_LINE",
         "DISPUTE_NEWS_INSTANT", "CHART_PICKS_LINE", "DIVIDEND_LINE", "FINANCE_RANK_LINE",
         "DAILY_RECO_LINE")
N_DAYS = 330
D0 = 320
NAMES = {"830001": "가나전자", "830002": "다라화학", "830003": "마바건설", "830004": "사아제약",
         "830005": "자차물산", "830006": "카타통신", "830007": "파하은행", "830008": "거너식품"}
SECT = {"830001": "전자", "830002": "화학", "830003": "건설", "830004": "제약",
        "830005": "화학", "830006": "화학", "830007": "화학", "830008": "식품"}


def _cal(n: int, start: str = "2025-08-04") -> list[str]:
    out, cur = [], date.fromisoformat(start)
    while len(out) < n:
        if cur.weekday() < 5:
            out.append(cur.isoformat())
        cur += timedelta(days=1)
    return out


def _close(j: int, i: int) -> float:
    c = 10000.0 * (1 + 0.1 * j) * (1.001 ** i)
    if j == 2 and i >= 200:
        c *= 0.4                          # 마바건설: 고점 대비 60% 낮음
    return c


def _build(tmp):
    from stocknews.store import Store
    cal = _cal(N_DAYS)
    st = Store(tmp / "picks.db")
    rows = []
    for j, t in enumerate(NAMES):
        for i, d in enumerate(cal):
            c = _close(j, i)
            rows.append((t, d, c * 0.999, c * 1.01, c * 0.99, c, 1e5, 1e9))
    uni = [t for t in NAMES if t != "830006"]
    with closing(sqlite3.connect(st.path)) as con:
        con.executemany("INSERT INTO prices(ticker,d,o,h,l,c,v,amt) VALUES(?,?,?,?,?,?,?,?)", rows)
        con.executemany("INSERT INTO tickers(ticker,name,market,sector,active) VALUES(?,?,?,?,1)",
                        [(t, n, "KOSPI", SECT[t]) for t, n in NAMES.items()])
        for i in range(D0 - 3, N_DAYS):
            con.executemany("INSERT INTO scans(d,ticker,name,price) VALUES(?,?,?,?)",
                            [(cal[i], t, NAMES[t], 1.0) for t in uni])
        con.executemany("INSERT INTO recos(d,rank,ticker,name,price,slot,grade,value_score,"
                        "trend_score,reason) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        [(cal[D0], rk, t, NAMES[t], 1.0, "VALUE", "A", 7.0, 5.0, "r")
                         for rk, t in enumerate(("830001", "830003", "830004", "830005"), 1)])
        con.commit()
    return st, cal


def _api(years, op, eps, dps, cons=True):
    """네이버 연간 재무 API 응답 모양."""
    keys = [f"{y}12" for y in years]
    tl = [{"isConsensus": "N", "title": k, "key": k} for k in keys]
    if cons:
        tl.append({"isConsensus": "Y", "title": "9999", "key": "999912"})

    def cols(vals):
        out = {k: {"value": ("-" if v is None else f"{v:,}"), "cx": None} for k, v in zip(keys, vals)}
        out["999912"] = {"value": "9,999", "cx": None}
        return out
    return {"financeInfo": {"trTitleList": tl, "rowList": [
        {"title": "매출액", "columns": cols([1] * len(keys))},
        {"title": "영업이익", "columns": cols(op)},
        {"title": "EPS", "columns": cols(eps)},
        {"title": "주당배당금", "columns": cols(dps)}]}}


def run(check, near, tmp):
    import nightly
    import run_screen as RS
    from stocknews import chart as CH
    from stocknews import config as C
    from stocknews import dispute_news as DN
    from stocknews import dividend as DV
    from stocknews import finrank as FR
    from stocknews import paper_render as R
    from stocknews import pricectx as PX
    from stocknews import signals as G
    from stocknews.env import KNOWN_KEYS
    from stocknews.renderer import stock_label

    st, cal = _build(tmp)
    d = cal[D0]
    NOW = datetime.fromisoformat(d + "T10:05:00")
    NEW = [{"ticker": t, "name": NAMES[t], "slot": "VALUE", "grade": "A", "value_score": 7.0,
            "trend_score": 5.0, "is_new_event": True} for t in ("830003", "830001")]

    def _keep():
        return {n: getattr(C, n) for n in FLAGS}

    def _back(snap):
        for n, v in snap.items():
            setattr(C, n, v)

    def _q(sql, args=()):
        with closing(sqlite3.connect(st.path)) as con:
            con.row_factory = sqlite3.Row
            return [dict(r) for r in con.execute(sql, args).fetchall()]

    def _ret(j, n, i=D0):
        return (_close(j, i) / _close(j, i - n) - 1) * 100

    def default_off():
        for f in ("CHART_PICKS_LINE", "DIVIDEND_LINE", "FINANCE_RANK_LINE"):
            assert getattr(C, f) is False and C.ENV_OVERRIDE_FLAGS[f] is bool and f in KNOWN_KEYS, f
        assert not G.any_on()
        lines, info = R.reco_line_block(st, d, chart_rows=NEW)
        assert lines[0] == f"━━ 오늘 기록된 추천 4 (기준일 {d[5:]}) ━━" and len(lines) == 3
        assert "signals" not in info
        assert (C.DISPUTE_SHOW_TDAYS, C.DISPUTE_SHOW_MAX, C.DISPUTE_INSTANT_MAX_PER_DAY,
                C.DISPUTE_INSTANT_FAIL_ALERT) == (20, 5, 5, 3)
        assert nightly.JOBS["dispute-watch"].fail_alert_after == 3
        assert all(j.fail_alert_after == 1 for n, j in nightly.JOBS.items() if n != "dispute-watch")
        assert G.maybe_daily(st, d) is None                       # 전부 OFF → 아무것도 안 함
        assert _q("SELECT COUNT(*) n FROM sig_dividend_data")[0]["n"] == 0
    check(S, "기본 OFF · 전부 OFF 면 메시지가 예전 세 줄 그대로 · 설정값", default_off)

    def price_context():
        ctx = PX.context(st, d, ["830001", "830003", "999999"])
        assert set(ctx) == {"830001", "830003"}
        for n in (1, 3, 5, 10):
            near(ctx["830001"][f"r{n}"], _ret(0, n), 1e-9)
        near(ctx["830001"]["close"], _close(0, D0), 1e-6)
        hi = max(_close(0, i) * 1.01 for i in range(D0 - 250, D0))
        near(ctx["830001"]["high"], hi, 1e-6)
        near(ctx["830001"]["drop"], max(0.0, 1 - _close(0, D0) / hi), 1e-9)
        assert ctx["830003"]["drop"] > 0.5
        assert PX.ret_line(ctx["830001"]) == (
            f" 1일 {_ret(0, 1):+.1f} · 3일 {_ret(0, 3):+.1f} · 5일 {_ret(0, 5):+.1f} · "
            f"10일 {_ret(0, 10):+.1f}")
        assert PX.ret_line(None) == " 1일 - · 3일 - · 5일 - · 10일 -"
        assert PX.pct(-3.44) == "−3.4%" and PX.pct(None) == "-" and PX.won(12345.4) == "12,345원"
        near(PX.since_close(ctx["830001"], cal[D0 - 4]), _ret(0, 4), 1e-9)
        assert PX.since_close(ctx["830001"], "1999-01-01") is None
        short = PX.context(st, cal[5], ["830001"])["830001"]      # 봉 6개 → 10일·고점은 못 구함
        assert "r10" not in short and "high" not in short and "r5" in short
        assert PX.ret_line(short).endswith("10일 -") and PX.high_text(short) == "고점 대비 -"
    check(S, "가격 맥락: 1·3·5·10일 수익률·고점 대비 계산 · 못 구하면 '-'", price_context)

    def chart_section():
        snap = _keep()
        try:
            C.CHART_PICKS_LINE = True
            jobs = CH.select_targets(CH.jobs_from_rows(CH.brief_rows(NEW)))     # 앨범이 쓰는 경로
            assert [j.ticker for j in CH.brief_targets(NEW)] == [j.ticker for j in jobs] \
                == ["830003", "830001"]
            assert CH.brief_rows([]) is None and CH.brief_targets(None) == []
            lines, info = R.reco_line_block(st, d, chart_rows=NEW)
            sig = info["signals"]
            assert sig["chart_picks"] == ["830003", "830001"] and "errors" not in sig, sig
            assert lines[1] == "【최종 추천 · 차트 첨부 2】"
            ctx = PX.context(st, d, ["830003", "830001"])
            assert lines[2] == (f"⚠{stock_label('마바건설', '830003')} {PX.won(_close(2, D0))} · "
                                f"오늘 {PX.pct(_ret(2, 1))} · {PX.high_text(ctx['830003'])}"), lines[2]
            assert ctx["830003"]["drop"] > 0.5 and PX.high_text(ctx["830003"]) == "고점 대비 −55%"
            assert lines[3] == PX.ret_line(ctx["830003"])
            assert lines[4].startswith(stock_label("가나전자", "830001") + " ") and "⚠" not in lines[4]
            assert lines[4].endswith("고점 대비 −1%") and lines[5] == PX.ret_line(ctx["830001"])
            assert lines[6] == "【점수 4】"
            assert lines[7] == " · ".join(("📈" if t in ("830001", "830003") else "")
                                          + stock_label(NAMES[t], t)
                                          for t in ("830001", "830003", "830004", "830005"))
            assert lines[-1] == R.RECO_LINE_NOTE and len(sig["messages"]) == 1
            # 차트 대상 0개 → 구역 생략 · 📈 없음
            l0, i0 = R.reco_line_block(st, d, chart_rows=[])
            assert l0[1] == "【점수 4】" and "📈" not in "\n".join(l0)
            assert i0["signals"]["chart_picks"] == []
            # 앨범 상한(CHART_MAX_COUNT)이 바뀌면 구역도 같이 바뀐다 — 같은 함수를 부른다
            one = type(C.DEFAULT)(chart=type(C.DEFAULT.chart)(max_count=1))
            assert [j.ticker for j in CH.brief_targets(NEW, one)] == ["830003"]
            # all 모드(브리핑 안): weekday_block 이 ① 대상을 그대로 넘긴다
            import inspect
            src = inspect.getsource(R.weekday_block)
            assert "reco_line_block(store, d, chart_rows=new)" in src
            assert 'chart_rows=info.get("new_rows")' in inspect.getsource(RS._daily_reco_line)
            ev = inspect.getsource(RS._brief_evening_v2)
            assert ev.index("_daily_reco_line(") < ev.index("_send_charts(")   # 메시지 먼저 · 앨범 뒤
            assert "brief_rows(info[\"new_rows\"])" in ev
        finally:
            _back(snap)
    check(S, "【최종 추천 · 차트 첨부】: 앨범 대상·순서와 같음 · 두 줄 · ⚠ · 📈 · 0개면 생략", chart_section)

    def dispute_two_lines():
        snap = _keep()
        try:
            C.DISPUTE_LIST = True
            items = [{"rcept_no": f"R{k}", "ticker": t, "name": NAMES[t], "kind": kind,
                      "rcept_dt": cal[D0 - back], "title": "x", "filer": "y"}
                     for k, (t, kind, back) in enumerate((
                         ("830001", "lawsuit", 0), ("830003", "tender", 4), ("830004", "proxy", 19),
                         ("830005", "lawsuit", 20), ("830006", "lawsuit", 1), ("830008", "lawsuit", 7),
                         ("830002", "lawsuit", 9), ("830007", "tender", 12)))]
            G.record_disputes(st, items)
            allw = G.recent_disputes(st, d)
            # 20거래일 안(당일 포함 20개 날) · 유니버스 밖(830006) 제외 · 최근 공시 순
            assert [x["ticker"] for x in allw] == ["830001", "830003", "830008", "830002",
                                                   "830007", "830004"], [x["ticker"] for x in allw]
            lines, info = R.reco_line_block(st, d)
            sig = info["signals"]
            assert sig["dispute"] == ["830001", "830003", "830008", "830002", "830007"]   # 5개 상한
            k = lines.index("【경영권 분쟁 공시 · 주의 5】")
            body = lines[k + 1:-1]
            assert len(body) == 11 and body[-1] == "(과거 8년: 공시 뒤 20일 평균 −3% · 숫자는 수익률 %)"
            ctx = PX.context(st, d, ["830001", "830003"])
            assert body[0] == f"{stock_label('가나전자', '830001')} 경영권 분쟁 소송 {d[5:]} · 공시 당일"
            assert body[1] == PX.ret_line(ctx["830001"])
            assert body[2] == (f"{stock_label('마바건설', '830003')} 공개매수 신고 {cal[D0 - 4][5:]} · "
                               f"공시 후 {PX.pct(_ret(2, 4))}"), body[2]
            assert body[3] == PX.ret_line(ctx["830003"])
            # 【점수】 의 ⚠분쟁 공시 는 기간 안 전부(상한 밖 830004 포함)
            score = lines[lines.index("【점수 4】") + 1]
            assert score.count("⚠분쟁 공시") == 3
            assert G.dispute_base_day("2025-08-09", cal) == "2025-08-11"        # 토요일 접수 → 월요일
        finally:
            _back(snap)
    check(S, "【경영권 분쟁 공시 · 주의】: 두 줄 · 공시 후/공시 당일 · 20거래일·5개 상한 · 맨 아래 한 줄",
          dispute_two_lines)

    def _news(nid, title, at, t, url="https://example.com/a?x=1&y=2", source="가나일보"):
        with closing(sqlite3.connect(st.path)) as con:
            con.execute("INSERT INTO news(id,d,published,title,title_norm,url,source,origin,region,"
                        "category,collected) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (nid, at[:10], at, title, title, url, source, "GOOGLE", "KR", "공시", at))
            con.execute("INSERT INTO news_tickers(news_id,ticker,name) VALUES(?,?,?)",
                        (nid, t, NAMES[t]))
            con.commit()

    sent: list[str] = []

    def instant():
        snap = _keep()
        keep = (RS._emit, DN.fetch_now, DN.collect_watch, RS.process_and_store, RS.now_kst,
                RS.should_scan_intraday, DN._now)
        try:
            C.DISPUTE_NEWS_LINE = True
            C.DISPUTE_NEWS_INSTANT = True
            C.DISPUTE_LIST = False
            DN._now = lambda: NOW
            with closing(sqlite3.connect(st.path)) as con:      # 앞 검사가 넣은 공시 중 일부를 비운다
                con.execute("DELETE FROM sig_dispute WHERE ticker IN ('830004','830005','830008')")
                con.commit()
            # 처음 켠 날의 과거 기사(소급) — 즉시 알림 대상이 아니다
            _news("p0", "거너식품 경영권 분쟁 점화", (NOW - timedelta(days=3)).isoformat(), "830008")
            DN.detect(st, now=NOW - timedelta(hours=2))
            c0, i0 = DN.instant_candidates(st, d, now=NOW - timedelta(hours=1))
            assert c0 == [] and i0.get("since_set") is True               # 켠 뒤 첫 회차: 기준 시각만 적음
            # 켜기 전에 감지된(소급 아님) 사건 — 기준 시각 이전이라 보내지 않는다
            with closing(sqlite3.connect(st.path)) as con:
                con.execute("INSERT INTO sig_dispute_news(ticker,name,kind,first_at,title,detected) "
                            "VALUES('830002','다라화학','first',?,?,?)",
                            ((NOW - timedelta(hours=5)).isoformat(), "다라화학 경영권 분쟁",
                             (NOW - timedelta(hours=4)).isoformat()))
                con.commit()
            _news("p1", "사아제약, 2대주주와 경영권 분쟁 점화 <속보>",
                  (NOW - timedelta(minutes=20)).isoformat(timespec="seconds"), "830004")
            _news("p2", "사아제약 경영권 분쟁 격화", (NOW - timedelta(minutes=10)).isoformat(), "830004")
            _news("p3", "카타통신 경영권 분쟁 점화", (NOW - timedelta(minutes=9)).isoformat(), "830006")
            DN.detect(st, now=NOW)
            cands, info = DN.instant_candidates(st, d, now=NOW)
            assert [r["ticker"] for r in cands] == ["830004"], cands        # 유니버스 밖·과거분 제외
            r = cands[0]
            txt = DN.instant_text(st, d, r, {"price": 1.0, "pct": 4.26}, now=NOW)
            L = txt.split("\n")
            ctx = PX.context(st, d, ["830004"])
            assert L[0] == "🔔 <b>경영권 분쟁 첫 보도</b>" and L[1] == stock_label("사아제약", "830004")
            assert L[2] == ('"<a href="https://example.com/a?x=1&amp;y=2">사아제약, 2대주주와 경영권 '
                            '분쟁 점화 &lt;속보&gt;</a>"'), L[2]
            assert L[3] == f"가나일보 · {NOW - timedelta(minutes=20):%m-%d %H:%M}"
            assert L[4] == "관련 기사 2건 · 분쟁 공시 없음"
            assert L[5] == "지금 +4.3% (10:05 조회)"
            assert L[6] == PX.ret_line(ctx["830004"]).strip() + f" ({d[5:]} 종가까지)"
            assert L[7] == "과거 8년: 분쟁 공시 뒤에 샀을 때 20일 평균 −3%"
            assert L[8] == "첫 보도 기준 성적은 기록 중 · 검증 중" and len(L) == 9
            no_px = DN.instant_text(st, d, r, None, now=NOW).split("\n")
            assert len(no_px) == 8 and not any(x.startswith("지금") for x in no_px)   # '지금' 줄만 빠짐
            assert DN.fetch_now("830004", get=lambda c: {"datas": [{"fluctuationsRatioRaw": "-1.5",
                                                                    "closePriceRaw": "1000"}]}) \
                == {"price": 1000.0, "pct": -1.5}
            assert DN.fetch_now("830004", get=lambda c: {"datas": []}) is None
            assert DN.fetch_now("830004", get=lambda c: 1 / 0) is None

            # 잡 실행: 수집·시세는 가짜로. 발송은 전체 수신자 경로(_emit) 한 번.
            RS._emit = lambda text, dry: (sent.append(text), True)[1]
            DN.fetch_now = lambda t, **k: None                                 # 현재가 실패 → 그래도 보냄
            DN.collect_watch = lambda *a, **k: []
            RS.process_and_store = lambda s, raw: {"collected": 0, "stored": 0}
            RS.now_kst = lambda: NOW
            RS.should_scan_intraday = lambda s, a: (True, "")
            args = types.SimpleNamespace(dry_run=False, ignore_window=False)
            RS.SUMMARY.clear()
            assert RS.mode_dispute_watch(st, args) == RS.EXIT_OK
            assert RS.SUMMARY["sent"] == 1 and RS.SUMMARY["tickers"] == ["830004"], dict(RS.SUMMARY)
            assert len(sent) == 1 and "지금" not in sent[0] and "사아제약" in sent[0]
            row = _q("SELECT * FROM sig_dispute_instant")
            assert len(row) == 1 and row[0]["ticker"] == "830004" and row[0]["via"] == "watch"
            assert row[0]["price_ok"] == 0
            RS.SUMMARY.clear()
            assert RS.mode_dispute_watch(st, args) == RS.EXIT_OK and len(sent) == 1   # 같은 종목은 한 번
            # 보낸 종목도 밤 메시지에는 그대로 표시(표시함으로 적지 않는다)
            assert [x["ticker"] for x in DN.to_show(st, d)] == ["830002", "830004"]
            lines, info2 = R.reco_line_block(st, d)
            k = lines.index("【경영권 분쟁 첫 보도 2】")
            assert lines[k + 3].startswith(stock_label("사아제약", "830004") + ' "<a href=')
            assert lines[k + 3].endswith(f" · 오늘 {PX.pct(_ret(3, 1))}")
            assert lines[k + 4] == PX.ret_line(ctx["830004"])
            # 하루 상한
            with closing(sqlite3.connect(st.path)) as con:
                for i, t in enumerate(("830001", "830003", "830005", "830007")):
                    con.execute("INSERT INTO sig_dispute_instant(event_id,ticker,sent_at,day) "
                                "VALUES(?,?,?,?)", (9000 + i, "9" + t[1:], NOW.isoformat(),
                                                    NOW.strftime("%Y-%m-%d")))
                con.commit()
            _news("p4", "자차물산 경영권 분쟁 점화", (NOW - timedelta(minutes=5)).isoformat(), "830005")
            DN.detect(st, now=NOW)
            c5, i5 = DN.instant_candidates(st, d, now=NOW)
            assert c5 == [] and i5["capped"] == 1 and i5["sent_today"] == 5, i5
            # 뉴스 수집 회차 경로(_instant_alerts) — 오류가 나도 예외를 올리지 않는다
            DN.instant_candidates = lambda *a, **k: 1 / 0
            out = RS._instant_alerts(st, args, "news")
            assert "ZeroDivisionError" in out["error"] and out["sent"] == 0
        finally:
            import importlib
            (RS._emit, DN.fetch_now, DN.collect_watch, RS.process_and_store, RS.now_kst,
             RS.should_scan_intraday, DN._now) = keep
            importlib.reload(DN)
            _back(snap)
        assert RS._instant_alerts(st, types.SimpleNamespace(dry_run=True), "news") is None   # OFF
    check(S, "즉시 알림: 형식 · 현재가 없으면 '지금'만 생략 · 한 번만 · 켜기 전 과거분 제외 · 하루 상한 · 밤 표시 유지",
          instant)

    def fail_streak():
        job = nightly.JOBS["dispute-watch"]
        root = tmp / "streak"
        assert nightly.fail_streak(job, True, root=root) == 1
        assert nightly.fail_streak(job, False, locked=True, root=root) == 1     # 락 겹침은 세지 않음
        assert nightly.fail_streak(job, True, root=root) == 2
        assert nightly.fail_streak(job, True, root=root) == 3                    # 3회째 → 알림
        assert nightly.fail_streak(job, False, root=root) == 0                   # 성공하면 0
        stamp = (root / "data" / "cron_failstreak_dispute-watch.json").stat().st_mtime_ns
        assert nightly.fail_streak(job, False, root=root) == 0                   # 성공이 이어지면 파일 그대로
        assert (root / "data" / "cron_failstreak_dispute-watch.json").stat().st_mtime_ns == stamp
        assert nightly.fail_streak(job, False, root=tmp / "none") == 0 and not (tmp / "none").exists()
        import inspect
        src = inspect.getsource(nightly.main)
        assert "streak % job.fail_alert_after == 0" in src and 'r["rc"] != EXIT_LOCKED' in src
    check(S, "dispute-watch: 실패·락 겹침은 조용히 · 3회 연속 실패 때만 알림", fail_streak)

    FETCH = {  # op(억), eps, dps = [과거 → 최근] 3년
        "830001": _api((2023, 2024, 2025), [10, 10, 10], [1000, 1000, 1000], [300, 300, 400]),
        "830002": _api((2023, 2024, 2025), [10, 10, 10], [1000, 1000, 1000], [500, 400, 450]),   # 감액 이력
        "830003": _api((2023, 2024, 2025), [10, 10, 10], [1000, 1000, 1000], [100, 100, 100]),   # 고점 50% 미만
        "830004": _api((2023, 2024, 2025), [10, 10, -5], [1000, 1000, 1000], [200, 200, 200]),   # 영업 적자
        "830005": _api((2023, 2024, 2025), [10, 10, 10], [1000, 1000, 1000], [600, 700, 800]),
        "830007": _api((2023, 2024, 2025), [10, 10, 10], [1000, 1000, 1000], [None, 500, 500]),  # 무배당 연도
        "830008": _api((2023, 2024, 2025), [10, 10, 10], [1000, 1000, 500], [450, 450, 450]),    # 성향 90%
    }

    def dividend():
        snap = _keep()
        try:
            C.DIVIDEND_LINE = True
            p = DV.parse_annual(FETCH["830001"])
            assert p == {"fy": "202512", "years": 3, "dps": [400.0, 300.0, 300.0], "eps": 1000.0,
                         "op": 10.0}, p                                    # 추정 열(9999)은 버린다
            assert DV.parse_annual({"financeInfo": {"trTitleList": [], "rowList": []}}) is None
            assert DV.parse_annual({"x": 1}) is None and DV.parse_annual(None) is None
            got, finfo = DV.fetch_all(sorted(FETCH) + ["830099"],
                                      get=lambda c: FETCH[c] if c in FETCH else 1 / 0, workers=2)
            assert set(got) == set(FETCH) and finfo["errors"] == {"ZeroDivisionError": 1}
            keep_b = C.DIVIDEND_BUDGET_SEC                      # 전체 시간 상한을 넘으면 남은 종목은 건너뜀
            C.DIVIDEND_BUDGET_SEC = -1.0
            try:
                g0, f0 = DV.fetch_all(sorted(FETCH), get=lambda c: FETCH[c], workers=2)
            finally:
                C.DIVIDEND_BUDGET_SEC = keep_b
            assert g0 == {} and f0["errors"] == {"시간 상한": len(FETCH)}, f0
            # 자료가 모자라면 0건이 아니라 "자료 없음"
            bad = DV.record(st, d, get=lambda c: {"oops": 1})
            assert bad["status"] == "no_data" and "0/7" in bad["reason"], bad
            b0 = DV.block(st, d)
            assert b0["lines"] == ["【고배당 0】", "자료 없음"] and "자료 없음" in b0["error"]
            lines, info = R.reco_line_block(st, d)
            assert "【고배당 0】" in lines and lines[lines.index("【고배당 0】") + 1] == "자료 없음"
            assert any(e.startswith("고배당: 자료 없음") for e in info["signals"]["errors"])
            md = G.maybe_daily(st, d) if False else None                  # (아래에서 따로 확인)
            # 전날 목록: 830002 가 있었다고 해 둔다 → 오늘 빠짐 사유
            with closing(sqlite3.connect(st.path)) as con:
                con.executemany("INSERT INTO sig_dividend_list(d,rank,ticker,name,yield_pct,sector) "
                                "VALUES(?,?,?,?,?,?)",
                                [(cal[D0 - 1], 1, "830002", "다라화학", 3.0, "화학"),
                                 (cal[D0 - 1], 2, "830005", "자차물산", 5.0, "화학"),
                                 (cal[D0 - 30], 1, "830004", "사아제약", 2.0, "제약")])
                con.commit()
            res = DV.record(st, d, get=lambda c: FETCH[c])
            assert res["status"] == "ok" and res["listed"] == 2 and res["coverage"] == 1.0, res
            assert DV.record(st, d, get=lambda c: 1 / 0)["cached"] is True     # 하루 한 번
            data = {r["ticker"]: r for r in _q("SELECT * FROM sig_dividend_data WHERE d=?", (d,))}
            near(data["830005"]["yield_pct"], 800 / _close(4, D0) * 100, 1e-9)
            near(data["830005"]["payout_pct"], 80.0, 1e-9)                  # 정확히 80% 는 통과
            assert data["830005"]["ok"] == 1 and data["830001"]["ok"] == 1
            assert data["830002"]["fail"] == "배당 감액" and data["830002"]["stop"] is None
            assert data["830003"]["fail"] == "고점 대비 50% 미만"
            assert data["830003"]["stop"] == "고점 대비 50% 미만"
            assert data["830004"]["fail"] == "영업이익 적자" and data["830004"]["stop"] == "영업이익 적자"
            assert data["830007"]["fail"] == "최근 3년 중 무배당"
            assert data["830008"]["fail"] == "배당성향 80% 초과" and data["830008"]["stop"] is None
            lst = _q("SELECT * FROM sig_dividend_list WHERE d=? ORDER BY rank", (d,))
            assert [r["ticker"] for r in lst] == ["830005", "830001"]
            blk = DV.block(st, d)
            y5, y1 = 800 / _close(4, D0) * 100, 400 / _close(0, D0) * 100
            assert blk["lines"][0] == "【고배당 2】"
            assert blk["lines"][1] == (f"{stock_label('자차물산', '830005')} {y5:.1f}% · "
                                       f"{stock_label('가나전자', '830001')} {y1:.1f}%")
            assert blk["lines"][2] == f"↳ 새로 들어옴: {stock_label('가나전자', '830001')}"
            assert blk["lines"][3] == f"↳ 빠짐: {stock_label('다라화학', '830002')} 배당 감액"
            assert blk["lines"][4] == (f"↳ ⚠그만 사는 신호: {stock_label('사아제약', '830004')} "
                                       f"영업이익 적자"), blk["lines"]
            assert blk["error"] is None and len(blk["lines"]) == 5
            # 판정 단위
            e = DV.evaluate
            ok = {"dps": [500.0, 500.0, 500.0], "eps": 1000.0, "op": 1.0}
            assert e(ok, 10000.0, 15000.0)["ok"] and e(None, 1, 1)["fail"] == "자료 없음"
            assert e({**ok, "dps": [400.0, 500.0, 500.0]}, 1e4, 1e4)["stop"] == "배당 감액"
            assert e({**ok, "dps": [0.0, 500.0, 500.0]}, 1e4, 1e4)["stop"] == "무배당"
            assert e({**ok, "eps": 400.0}, 1e4, 1e4)["stop"] == "배당성향 100% 초과"
            assert e({**ok, "eps": -5.0}, 1e4, 1e4)["fail"] == "순이익 적자(배당성향 계산 불가)"
            assert e(ok, 1e4, None)["fail"] == "250거래일 미만"
            assert e(ok, 7500.0, 15000.0)["ok"] and not e(ok, 7499.0, 15000.0)["ok"]   # 정확히 50% 는 통과
            # 업종 한도: 같은 업종 4종목이 통과하면 3종목까지만
            with closing(sqlite3.connect(st.path)) as con:
                con.execute("UPDATE tickers SET sector='화학'")
                con.commit()
            same = {t: _api((2023, 2024, 2025), [10, 10, 10], [1000] * 3, [100 + k] * 3)
                    for k, t in enumerate(("830001", "830002", "830004", "830005", "830007", "830008"))}
            r2 = DV.record(st, cal[D0 + 1], get=lambda c: same.get(c) or FETCH[c])
            assert r2["listed"] == 3 and r2["passed"] == 6, r2
            skip = _q("SELECT fail FROM sig_dividend_data WHERE d=? AND ok=1 AND fail IS NOT NULL",
                      (cal[D0 + 1],))
            assert {r["fail"] for r in skip} == {"업종 한도(30%)"} and len(skip) == 3
            # 추천 메시지의 꼬리표
            C.PULLBACK_LIST = False
            lines, info = R.reco_line_block(st, d, chart_rows=NEW)
            score = lines[lines.index("【점수 4】") + 1]
            assert f"{stock_label('가나전자', '830001')} (고배당 {y1:.1f}%)" in score
            assert f"{stock_label('자차물산', '830005')} (고배당 {y5:.1f}%)" in score
            assert info["signals"]["dividend"]["tickers"] == ["830005", "830001"]
            # daily 경로: 자료가 없으면 오류로 올린다(스캔은 그대로)
            keep = DV.record
            DV.record = lambda s, dd: {"status": "no_data", "reason": "자료를 읽은 종목 0/7"}
            try:
                md = G.maybe_daily(st, cal[D0 + 2])
            finally:
                DV.record = keep
            assert md["errors"] == ["dividend: 자료 없음 — 자료를 읽은 종목 0/7"], md
        finally:
            _back(snap)
    check(S, "【고배당】: 조건·탈락 사유·업종 한도·새로 들어옴/빠짐/그만 사는 신호·자료 없음·(고배당 x%)",
          dividend)

    def finance_rank():
        snap = _keep()
        keep = FR.PATH
        try:
            p = tmp / "fr.json"
            p.write_text(json.dumps({"asof": d, "items": [
                {"rank": k, "ticker": t, "name": NAMES[t]} for k, t in enumerate(reversed(NAMES), 1)
            ] + [{"rank": 99, "ticker": "830001", "name": ""}]}, ensure_ascii=False), encoding="utf-8")
            b = FR.block(p)
            assert b["lines"][0] == "【재무 순위 상위 8】" and b["tickers"][0] == "830008"
            assert b["lines"][2] == "(이익을 잘 내면서 값이 싼 순 · 월 1회 갱신 · 검증 중)"
            assert FR.block(tmp / "none.json") is None
            (tmp / "bad.json").write_text("{", encoding="utf-8")
            assert FR.block(tmp / "bad.json") is None
            C.FINANCE_RANK_LINE = True
            FR.PATH = p
            lines, info = R.reco_line_block(st, d)
            assert "【재무 순위 상위 8】" in lines and "errors" not in info["signals"]
            FR.PATH = tmp / "none.json"                                   # 자료 없음 → 구역 없음 + 오류
            lines, info = R.reco_line_block(st, d)
            assert not any("재무 순위" in x for x in lines)
            assert info["signals"]["errors"] == ["재무 순위: 자료 없음"]
        finally:
            FR.PATH = keep
            _back(snap)
    check(S, "【재무 순위 상위】: 목록 파일 · 없으면 구역 없음 + 오류", finance_rank)

    def order_and_safety():
        snap = _keep()
        keep = (FR.PATH, PX.context, DV.block, CH.brief_targets)
        try:
            for f in FLAGS[:-1]:
                setattr(C, f, True)
            C.DISPUTE_NEWS_INSTANT = False
            p = tmp / "fr.json"
            FR.PATH = p
            lines, info = R.reco_line_block(st, d, chart_rows=NEW)
            heads = [x for x in lines if x.startswith("【")]
            order = ["【최종 추천 · 차트 첨부", "【점수", "【눌림목", "【고배당", "【재무 순위 상위",
                     "【경영권 분쟁 첫 보도", "【경영권 분쟁 공시 · 주의"]
            assert len(heads) == len(order) and all(h.startswith(o) for h, o in zip(heads, order)), heads
            assert lines[-1] == R.RECO_LINE_NOTE and "errors" not in info["signals"]
            assert all(len(m) <= G.MSG_LIMIT for m in info["signals"]["messages"])
            text = "\n".join(lines).replace("공개매수", "")
            assert "매수" not in text and "매도" not in text
            # 구역 하나씩 오류 주입 → 그 구역만 빠지고 나머지는 나간다
            def boom(*a, **k):
                raise RuntimeError("일부러 넣은 오류")
            CH.brief_targets = boom
            l1, i1 = R.reco_line_block(st, d, chart_rows=NEW)
            assert not any(x.startswith("【최종") for x in l1) and "📈" not in "\n".join(l1)
            assert [x for x in l1 if x.startswith("【")][0].startswith("【점수 4】")
            assert any(e.startswith("최종 추천: RuntimeError") for e in i1["signals"]["errors"])
            assert sum(x.startswith("【") for x in l1) == 6
            CH.brief_targets = keep[3]
            DV.block = boom
            l2, i2 = R.reco_line_block(st, d, chart_rows=NEW)
            assert l2[l2.index("【고배당 0】") + 1] == "자료 없음" and "(고배당" not in "\n".join(l2)
            assert any(e.startswith("고배당: RuntimeError") for e in i2["signals"]["errors"])
            assert sum(x.startswith("【") for x in l2) == 7
            DV.block = keep[2]
            PX.context = boom                                              # 가격 맥락 실패 → 숫자만 "-"
            l3, i3 = R.reco_line_block(st, d, chart_rows=NEW)
            assert any(e.startswith("가격 맥락: RuntimeError") for e in i3["signals"]["errors"])
            k = l3.index("【최종 추천 · 차트 첨부 2】")
            assert l3[k + 1].endswith("- · 오늘 - · 고점 대비 -") and l3[k + 2] == PX.ret_line(None)
            assert sum(x.startswith("【") for x in l3) == 7 and l3[-1] == R.RECO_LINE_NOTE
            PX.context = keep[1]
            # 길어지면 구역 단위로 나눈다 — 한 유형의 제목과 내용은 같은 메시지
            chunks = [["제목"], ["【최종 추천 · 차트 첨부 2】", "가" * 1500], ["【점수 4】", "나" * 1500],
                      ["【고배당 2】", "다" * 1500], ["※ 끝"]]
            msgs = G.pack_messages(chunks)
            assert len(msgs) == 2 and all(len(m) <= G.MSG_LIMIT for m in msgs)
            assert msgs[1].startswith("【고배당 2】\n") and msgs[1].endswith("※ 끝")
        finally:
            FR.PATH, PX.context, DV.block, CH.brief_targets = keep
            _back(snap)
    check(S, "구역 순서(7개) · 오류를 넣으면 그 구역만 빠짐 · 구역 단위 나눔 · 매수/매도 문구 없음",
          order_and_safety)
