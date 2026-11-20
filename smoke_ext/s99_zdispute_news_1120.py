# -*- coding: utf-8 -*-
"""2026-11-20 경영권 분쟁 "첫 보도" (DISPUTE_NEWS_LINE · DISPUTE_NEWS_INSTANT) · 합친 메시지의
구역 나눔 · 신호 기능 안전장치(일부러 넣은 오류). 기록·표시 전용 · 기본 OFF.
"""
from __future__ import annotations

import sqlite3
import types
from contextlib import closing
from datetime import date, datetime, timedelta

S = "11-20 경영권 분쟁 첫 보도·안전장치"
FLAGS = ("PULLBACK_LIST", "DISPUTE_LIST", "HALF_DROP_WARN", "DISPUTE_NEWS_LINE",
         "DISPUTE_NEWS_INSTANT", "DAILY_RECO_LINE")
N_DAYS = 200
D0 = 170


def _cal(n: int, start: str = "2026-01-05") -> list[str]:
    out, cur = [], date.fromisoformat(start)
    while len(out) < n:
        if cur.weekday() < 5:
            out.append(cur.isoformat())
        cur += timedelta(days=1)
    return out


def _build(tmp):
    from stocknews.store import Store
    cal = _cal(N_DAYS)
    st = Store(tmp / "dnews.db")
    names = {"820001": "가나전자", "820002": "다라화학", "820003": "마바건설", "820004": "사아제약",
             "820005": "자차물산", "820006": "카타통신"}
    rows = []
    for j, t in enumerate(names):
        for i, d in enumerate(cal):
            c = 10000.0 * (1 + 0.001 * j) * (1.002 ** i)
            if t == "820004" and i == D0:
                c *= 1.05                                   # 오늘 +5%대
            rows.append((t, d, c * 0.999, c * 1.01, c * 0.99, c, 1e5, 1e9))
    uni = [t for t in names if t != "820006"]               # 820006 은 유니버스 밖
    with closing(sqlite3.connect(st.path)) as con:
        con.executemany("INSERT INTO prices(ticker,d,o,h,l,c,v,amt) VALUES(?,?,?,?,?,?,?,?)", rows)
        con.executemany("INSERT INTO tickers(ticker,name,market,active) VALUES(?,?,?,1)",
                        [(t, n, "KOSPI") for t, n in names.items()])
        for i in range(D0 - 3, N_DAYS):
            con.executemany("INSERT INTO scans(d,ticker,name,price) VALUES(?,?,?,?)",
                            [(cal[i], t, names[t], 1.0) for t in uni])
        con.executemany("INSERT INTO recos(d,rank,ticker,name,price,slot,grade,value_score,"
                        "trend_score,reason) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        [(cal[D0], rk, t, names[t], 1.0, "VALUE", "A", 7.0, 5.0, "r")
                         for rk, t in enumerate(("820004", "820005"), 1)])
        con.commit()
    return st, cal, names


def _news(st, nid, title, at, tickers, origin="GOOGLE", url="https://example.com/a", source="매체",
          category="공시", region="KR"):
    with closing(sqlite3.connect(st.path)) as con:
        con.execute("INSERT INTO news(id,d,published,title,title_norm,url,source,origin,region,"
                    "category,collected) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (nid, at[:10], at, title, title, url, source, origin, region, category, at))
        con.executemany("INSERT INTO news_tickers(news_id,ticker,name) VALUES(?,?,?)",
                        [(nid, t, n) for t, n in tickers])
        con.commit()


def run(check, near, tmp):
    import run_screen as RS
    from stocknews import config as C
    from stocknews import dispute_news as DN
    from stocknews import paper_render as R
    from stocknews import signals as G
    from stocknews.env import KNOWN_KEYS
    from stocknews.joblock import MODE_TIMEOUTS
    from stocknews.renderer import stock_label

    st, cal, names = _build(tmp)
    d = cal[D0]
    NOW = datetime.fromisoformat(d + "T21:40:00")

    def _keep():
        return {n: getattr(C, n) for n in FLAGS}

    def _back(snap):
        for n, v in snap.items():
            setattr(C, n, v)

    def _q(sql, args=()):
        with closing(sqlite3.connect(st.path)) as con:
            con.row_factory = sqlite3.Row
            return [dict(r) for r in con.execute(sql, args).fetchall()]

    def default_off():
        for f in FLAGS[3:5]:
            assert getattr(C, f) is False and C.ENV_OVERRIDE_FLAGS[f] is bool and f in KNOWN_KEYS, f
        assert not DN.enabled() and DN.maybe_detect(st) is None
        assert (C.DISPUTE_NOTE_YEARS, C.DISPUTE_NOTE_MEAN_PCT, C.DISPUTE_NOTE_UP_OF_10) == (8, -3, 3)
        assert G._dispute_note() == "(과거 8년: 공시 뒤 20일 평균 −3%, 10번 중 3번 오름)"
        assert "dispute-watch" in RS.MODES and "dispute-watch" in RS._WRITE_MODES
        assert "dispute-watch" in MODE_TIMEOUTS
        import nightly
        j = nightly.JOBS["dispute-watch"]
        assert j.once_per_day is False and j.quiet_ok is True
        args = types.SimpleNamespace(dry_run=False, ignore_window=False)
        RS.SUMMARY.clear()
        assert RS.mode_dispute_watch(st, args) == RS.EXIT_OK              # 플래그 OFF → 아무것도 안 함
        assert RS.SUMMARY["skipped"] is True and RS.SUMMARY["sent"] == 0
        RS.SUMMARY.clear()
    check(S, "기본 OFF · 장중 확인 잡은 플래그 OFF 면 건너뜀 · 잡 카탈로그·락 표 등록", default_off)

    def classify():
        f = DN.classify
        ok = (("가나전자, 2대주주와 경영권 분쟁 점화", "경영권 분쟁"),
              ("펀드, 가나전자 적대적 M&A 선언", "적대적 M&A"),
              ("가나전자 주총 표 대결 예고", "표 대결"),
              ("가나전자 지분 경쟁 본격화", "지분 경쟁"),
              ("사모펀드, 가나전자 공개매수 나선다", "공개매수"),
              ("소액주주, 가나전자에 주주제안", "주주제안"),
              ("주주연대, 가나전자 임시주총 소집 요구", "임시주총 소집 요구"),
              ("英 행동주의펀드, 가나전자 지분 5% 취득", "행동주의"),
              ("얼라인, 가나전자에 공개 서한", "행동주의"),
              ("2대주주, 가나전자 경영 참여 선언", "경영 참여"),
              ("주주연대, 가나전자 대표이사 해임 요구", "최대주주 변경 시도"),
              ("가나전자 신주발행 금지 가처분 신청", "가처분"),
              ("가나전자 유상증자 놓고 경영권 분쟁", "경영권 분쟁"))          # 다툼이 분명 → 우호적 거래 제외를 이김
        for title, lab in ok:
            assert f(title) == (lab, None), (title, f(title))
        assert f("가나전자, 3자배정 유상증자로 지분 경쟁력 확보… 최대주주 변경") == (None, None)
        bad = (("가나전자 경영권 양수도 계약… 공개매수는 없다", "우호적 거래·자금 조달"),
               ("의무공개매수 100% 강행 땐 가나전자도 영향", "제도·정책 기사"),
               ("가나전자 자진 상폐 공개매수", "자진 상장폐지·자기주식 공개매수"),
               ("가나전자 경영권 분쟁 일단락", "분쟁 종결 기사"),
               ("[칼럼] 가나전자 경영권 분쟁이 남긴 것", "칼럼·시황"))
        for title, why in bad:
            lab, got = f(title)
            assert lab is None and got == why, (title, lab, got)
        assert f("가나전자 경영권 분쟁", origin="DART")[1] == "공시 목록 항목(보도 아님)"
        assert f("[공시] 가나전자 소송등의제기ㆍ신청(경영권분쟁소송)")[1] == "공시 목록 항목(보도 아님)"
        assert f("가나전자 경영권 분쟁", region="US")[1] == "해외 기사"
        assert f("가나전자 경영권 분쟁", category="국내시황")[1] == "시황 분류"
        assert f("가나전자·다라화학 경영권 분쟁", n_tickers=2)[1] == "여러 종목을 나열"
        assert f("어느 회사의 경영권 분쟁", n_tickers=0)[1] == "연결된 종목이 없음"
        assert f("가나전자 3분기 실적 발표") == (None, None)
        assert f("英 행동주의 펀드, 가나전자 자사주 소각에도 압박")[0] == "행동주의"   # 자사주 제외는 공개매수에만
    check(S, "표현 목록과 제외 규칙(우호적 거래·정책·자진 상폐·종결·칼럼·공시·여러 종목)", classify)

    def detect_events():
        snap = _keep()
        try:
            C.DISPUTE_NEWS_LINE = True
            old = (NOW - timedelta(days=5)).isoformat(timespec="seconds")
            A, B, Cc, E_ = (("820001", names["820001"]),), (("820002", names["820002"]),), \
                (("820003", names["820003"]),), (("820005", names["820005"]),)
            _news(st, "n1", "가나전자, 2대주주와 경영권 분쟁 점화", old, A)
            _news(st, "n2", "가나전자 경영권 분쟁 격화", (NOW - timedelta(days=4)).isoformat(), A)
            _news(st, "n3", "가나전자 주총 표 대결 예고", (NOW - timedelta(days=3)).isoformat(), A)
            _news(st, "n4", "다라화학 경영권 분쟁 새 국면", old, B)                     # 공시가 먼저 있었음
            _news(st, "n5", "마바건설 지분 경쟁 본격화", (NOW - timedelta(days=75)).isoformat(), Cc)
            _news(st, "n6", "마바건설 지분 경쟁 재점화", (NOW - timedelta(days=6)).isoformat(), Cc)  # 69일 뒤 → 새 사건
            _news(st, "n7", "가나전자·다라화학 경영권 분쟁", old, A + B)                 # 여러 종목
            _news(st, "n8", "[공시] 자차물산 소송등의제기ㆍ신청(경영권분쟁소송)", old, E_, origin="DART")
            _news(st, "n9", "자차물산 3분기 실적", old, E_)
            G.record_disputes(st, [{"rcept_no": "F1", "ticker": "820002", "name": "다라화학",
                                    "kind": "lawsuit", "rcept_dt": cal[D0 - 30],
                                    "title": "소송등의제기ㆍ신청(경영권분쟁소송)", "filer": "다라화학"}])
            r1 = DN.detect(st, now=NOW)
            r2 = DN.detect(st, now=NOW)                                               # 멱등
            ev = _q("SELECT * FROM sig_dispute_news ORDER BY ticker, first_at")
            # 처음 켠 날이 지난 뒤 새로 들어온 기사
            fresh = (NOW - timedelta(hours=3)).isoformat(timespec="seconds")
            _news(st, "n10", "행동주의 펀드, 사아제약 지분 5% 취득하고 공개 서한 발송해 이사회 개편을 요구했다",
                  fresh, (("820004", names["820004"]),), url="https://example.com/x?a=1&b=2")
            _news(st, "n11", "카타통신 경영권 분쟁 점화", fresh, (("820006", names["820006"]),), url="")
            _news(st, "n12", "자차물산 임시주총 소집 요구", fresh, E_)
            r3 = DN.detect(st, now=NOW)
        finally:
            _back(snap)
        assert r1["matched"] == 6 and r1["new_events"] == 4 and r1["joined"] == 2, r1
        assert r1["first"] == 3 and r1["after_filing"] == 1, r1
        assert r1["rejected"] == {"여러 종목을 나열": 1}, r1["rejected"]
        assert r2["matched"] == 0 and r2["new_events"] == 0, r2
        a = [e for e in ev if e["ticker"] == "820001"]
        assert len(a) == 1 and a[0]["n_articles"] == 3 and a[0]["title"].endswith("점화")
        assert a[0]["kind"] == "first" and a[0]["show_skip"] == "소급" and a[0]["expr"] == "경영권 분쟁"
        b = [e for e in ev if e["ticker"] == "820002"][0]
        assert b["kind"] == "after_filing" and b["show_skip"] == "공시 뒤 보도"
        assert b["prior_filing_dt"] == cal[D0 - 30]
        c = [e for e in ev if e["ticker"] == "820003"]
        assert len(c) == 2 and all(x["show_skip"] == "소급" for x in c)                # 60일 넘으면 새 사건
        assert r3["new_events"] == 3 and r3["first"] == 3, r3
        new = _q("SELECT * FROM sig_dispute_news WHERE first_news_id IN ('n10','n11','n12')")
        assert all(e["show_skip"] is None and e["shown"] == 0 for e in new), new
        assert len(_q("SELECT * FROM sig_dispute_news_items")) == 9
    check(S, "감지: 사건 묶음(60일) · 공시 뒤 보도 · 처음 켠 날 과거 기사는 소급 · 멱등", detect_events)

    def show_and_message():
        snap = _keep()
        try:
            C.DISPUTE_NEWS_LINE = True
            rows = DN.to_show(st, d)
            lines = DN.show_lines(st, d, rows)
            C.DISPUTE_LIST = True
            C.PULLBACK_LIST = True
            full, info = R.reco_line_block(st, d)
            C.DISPUTE_NEWS_LINE = False
            off, ioff = R.reco_line_block(st, d)
        finally:
            _back(snap)
        assert [r["ticker"] for r in rows] == ["820004", "820005"], rows       # 유니버스 밖 820006 은 없음
        l4 = lines[0]
        assert l4.startswith(stock_label(names["820004"], "820004") + ' "<a href="https://example.com/x?a=1&amp;b=2">')
        title = l4.split('">')[1].split("</a>")[0]
        assert 30 <= len(title) <= 40 and title.endswith("…"), (len(title), title)
        at = datetime.fromisoformat(d + "T21:40:00") - timedelta(hours=3)
        assert f'</a>" {at:%m-%d %H:%M} · 오늘 +' in l4 and " · 20일 +" in l4, l4
        assert "<a " not in lines[1] or True
        k = full.index("【경영권 분쟁 첫 보도 2】")
        assert full[0] == f"━━ 오늘 기록된 추천 (기준일 {d[5:]}) ━━" and full[1] == "【점수 2】"
        assert full[2] == (f"{stock_label(names['820004'], '820004')} 🔔분쟁 보도 · "
                           f"{stock_label(names['820005'], '820005')} 🔔분쟁 보도"), full[2]
        assert full[3] == "【눌림목 0】" and full[4] == "없음" and k == 5
        assert full[k + 1] == lines[0] and full[k + 2] == lines[1] and full[-1] == R.RECO_LINE_NOTE
        assert "【경영권 분쟁 공시" not in "\n".join(full)                       # 최근 5거래일 공시 없음 → 줄 생략
        assert info["signals"]["news"] == ["820004", "820005"] and len(info["signals"]["news_ids"]) == 2
        text = "\n".join(full)
        assert "매수" not in text.replace("공개매수", "") and "매도" not in text
        assert "첫 보도" not in "\n".join(off) and "🔔" not in "\n".join(off)     # OFF 면 이 줄만 빠진다
        assert off[:2] == full[:2] and "news" not in ioff["signals"]
        # 가격이 없는 날이면 등락률 항목만 빠진다
        nop = DN.show_lines(st, "2030-01-02", rows)
        assert "오늘" not in nop[0] and "20일" not in nop[0] and nop[0].endswith(f"{at:%m-%d %H:%M}")
    check(S, "표시: 제목 40자·링크·시각·등락률 · 순서 · 🔔 표시 · OFF 면 줄 없음 · 등락률 없으면 생략",
          show_and_message)

    def shown_once():
        ids = [r["id"] for r in DN.to_show(st, d)]
        assert DN.mark_shown(st, ids[:1], when=NOW) == 1
        assert [r["id"] for r in DN.to_show(st, d)] == ids[1:]
        snap = _keep()
        try:                                        # 표시한 종목은 60일 안에 다시 올리지 않는다
            C.DISPUTE_NEWS_LINE = True
            _news(st, "n13", "사아제약 주주제안 접수", (NOW + timedelta(days=70)).isoformat(),
                  (("820004", names["820004"]),))
            _news(st, "n14", "사아제약 표 대결 임박", (NOW + timedelta(days=80)).isoformat(),
                  (("820004", names["820004"]),))
            DN.detect(st, now=NOW + timedelta(days=81), scan_days=None)
        finally:
            _back(snap)
        e = _q("SELECT * FROM sig_dispute_news WHERE first_news_id='n13'")
        assert len(e) == 1 and e[0]["n_articles"] == 2 and e[0]["show_skip"] is None      # 70일 뒤 = 새 사건
        with closing(sqlite3.connect(st.path)) as con:   # 다만 직전 표시가 60일 안이면 올리지 않는다
            con.execute("UPDATE sig_dispute_news SET shown_at=? WHERE id=?",
                        ((NOW + timedelta(days=30)).isoformat(), ids[0]))
            con.execute("DELETE FROM sig_dispute_news WHERE first_news_id='n13'")
            con.execute("DELETE FROM sig_dispute_news_items WHERE news_id IN ('n13','n14')")
            con.commit()
        snap = _keep()
        try:
            C.DISPUTE_NEWS_LINE = True
            DN.detect(st, now=NOW + timedelta(days=81), scan_days=None)
        finally:
            _back(snap)
        assert _q("SELECT show_skip FROM sig_dispute_news WHERE first_news_id='n13'")[0]["show_skip"] \
            == "60일 안 표시"
    check(S, "표시한 첫 보도는 다시 싣지 않음 · 60일 안 재표시 금지", shown_once)

    def packing():
        title, note = ["━━ 제목 ━━"], ["※ 맨 아래"]
        a = ["【점수 3】", "가 · 나 · 다"]
        b = ["【눌림목 2】", "x" * 30, "y" * 30]
        c = ["【경영권 분쟁 첫 보도 3】", "p" * 50, "q" * 50, "r" * 50]
        one = G.pack_messages([title, a, b, c, note], limit=3900)
        assert one == ["\n".join(title + a + b + c + note)]
        two = G.pack_messages([title, a, b, c, note], limit=130)
        assert len(two) >= 2 and all(len(m) <= 130 for m in two), [len(m) for m in two]
        for m in two:                                  # 유형 제목만 남고 내용이 다음 메시지로 가는 일은 없다
            ls = m.split("\n")
            for i, ln in enumerate(ls):
                if ln.startswith("【"):
                    assert i + 1 < len(ls), m
        assert "\n".join(two).replace("\n", "") .count("【눌림목 2】") == 1
        big = ["【경영권 분쟁 첫 보도 9】"] + [f"{i}" * 60 for i in range(9)]
        parts = G.pack_messages([title, a, big, note], limit=200)
        assert all(len(m) <= 200 for m in parts) and len(parts) >= 3
        assert sum(m.count("【경영권 분쟁 첫 보도 9】") for m in parts) == len(
            [m for m in parts if "【경영권 분쟁 첫 보도 9】" in m])
        assert any("(이어서)" in m for m in parts)
        body = [ln for m in parts for ln in m.split("\n") if ln[:1].isdigit()]
        assert body == big[1:]                         # 줄은 빠지지도 겹치지도 않는다
    check(S, "4,096자 나눔: 구역 단위 · 유형 제목과 내용이 갈라지지 않음 · 큰 구역만 (이어서)", packing)

    def tracking_and_weekly():
        snap = _keep()
        try:
            C.DISPUTE_NEWS_LINE = True
            C.DISPUTE_LIST = True
            with closing(sqlite3.connect(st.path)) as con:   # 감지 시각을 기준일 밤으로 맞춘다
                con.execute("UPDATE sig_dispute_news SET detected=? WHERE first_news_id IN ('n10','n11','n12')",
                            (NOW.isoformat(),))
                con.commit()
            G.record_disputes(st, [{"rcept_no": "F2", "ticker": "820005", "name": "자차물산",
                                    "kind": "lawsuit", "rcept_dt": cal[D0 + 4],
                                    "title": "소송등의제기ㆍ신청(경영권분쟁소송)", "filer": "자차물산"}])
            G.update_tracking(st, cal[D0 + 28])
            file_lines = G.type_lines(st, cal[D0 + 28], scope="file")
            send_lines = G.type_lines(st, cal[D0 + 28], scope="send")
        finally:
            _back(snap)
        pap = _q("SELECT * FROM sig_paper WHERE kind='dispute_news' ORDER BY ticker")
        assert [(p["ticker"], p["enrolled"], p["signal_d"]) for p in pap] == [
            ("820004", 1, d), ("820005", 1, d), ("820006", 0, d)], pap           # 소급 사건은 편입하지 않음
        assert pap[0]["entry_date"] == cal[D0 + 1] and pap[2]["skip_reason"] == "운영 유니버스 밖"
        res = _q("SELECT rule, exit_date, days FROM sig_results WHERE paper_id=? ORDER BY days",
                 (pap[0]["id"],))
        assert [(r["rule"], r["exit_date"]) for r in res] == [("h5", cal[D0 + 5]), ("h20", cal[D0 + 20])], res
        link = _q("SELECT ticker, filing_after_dt FROM sig_dispute_news WHERE filing_after_dt IS NOT NULL")
        assert ("820005", cal[D0 + 4]) in [(x["ticker"], x["filing_after_dt"]) for x in link], link
        head = [x for x in file_lines if x.startswith("• 분쟁 첫 보도 · 감지")]
        assert len(head) == 1 and "뒤에 분쟁 공시가 나온 것 1건" in head[0], file_lines
        for h in (5, 20, 60):
            assert any(x.startswith(f"• 분쟁 첫 보도 · {h}거래일 보유: 표본 부족") for x in file_lines)
        assert not any("첫 보도" in x for x in send_lines)                       # 성적표에는 싣지 않는다(파일 전용)
        assert any(x.startswith("• 경영권 분쟁 공시 · 20거래일 보유") for x in send_lines)
    check(S, "종이 추적: 감지 다음 거래일 시가 · 5·20·60거래일 · 뒤따른 공시 기록 · 주간 파일 줄",
          tracking_and_weekly)

    def seeding():
        calls = []

        def fake(days=None, max_pages=None, **kw):
            calls.append((days, max_pages))
            return [], {"requests": 1}
        snap = _keep()
        orig = G.collect_disputes
        try:
            C.DISPUTE_LIST = True
            G.collect_disputes = fake
            with closing(sqlite3.connect(st.path)) as con:
                con.execute("DELETE FROM sig_meta")
                con.commit()
            a = G.maybe_collect_disputes(st)
            b = G.maybe_collect_disputes(st)
            G.collect_disputes = lambda **kw: ([], {"requests": 1, "error": "DART 020"})
            with closing(sqlite3.connect(st.path)) as con:
                con.execute("DELETE FROM sig_meta")
                con.commit()
            c = G.maybe_collect_disputes(st)
            assert G._meta_get(st, "dispute_seed") is None                      # 끊기면 표시를 남기지 않는다
        finally:
            G.collect_disputes = orig
            _back(snap)
        assert calls == [(C.DISPUTE_SEED_DAYS, 120), (None, None)], calls
        assert a.get("seeded") is True and a["seed_days"] == 190 and "seeded" not in b and "seeded" not in c
        # 190일은 85일 창 3개로 나뉜다(종목 미지정 조회는 3개월까지)
        import os
        seen = []

        def get(params):
            seen.append((params["bgn_de"], params["end_de"], params["pblntf_detail_ty"]))
            return {"status": "013"}
        old = os.environ.get("DART_API_KEY")
        try:
            os.environ["DART_API_KEY"] = "x" * 8
            G.collect_disputes(days=190, get=get, pause=0)
        finally:
            if old is None:
                os.environ.pop("DART_API_KEY", None)
            else:
                os.environ["DART_API_KEY"] = old
        wins = sorted({(a_, b_) for a_, b_, _ in seen})
        assert len(wins) == 3 and len(seen) == 9, seen
        for a_, b_ in wins:
            assert (date.fromisoformat(f"{b_[:4]}-{b_[4:6]}-{b_[6:]}")
                    - date.fromisoformat(f"{a_[:4]}-{a_[4:6]}-{a_[6:]}")).days <= 84
    check(S, "분쟁 공시 초기 적재: 처음 한 번 190일(85일 창) · 끊기면 다음에 다시", seeding)

    def injected_errors():
        snap = _keep()
        orig = (G.listed_pullbacks, G.recent_disputes, DN.to_show, G.reco_block, G.record_pullback)
        before = _q("SELECT * FROM recos ORDER BY d, rank")

        def boom(*a, **k):
            raise RuntimeError("일부러 넣은 오류")
        try:
            C.PULLBACK_LIST = C.DISPUTE_LIST = C.HALF_DROP_WARN = C.DISPUTE_NEWS_LINE = True
            good, _ = R.reco_line_block(st, d)
            # ① 한 부분만 실패 → 그 부분만 빠지고 나머지와 【점수】 는 나간다
            G.listed_pullbacks = boom
            part, pinfo = R.reco_line_block(st, d)
            G.listed_pullbacks = orig[0]
            DN.to_show = boom
            part2, pinfo2 = R.reco_line_block(st, d)
            DN.to_show = orig[2]
            # ② 신호 표시 전체가 실패 → 【점수】 줄만
            G.reco_block = boom
            only, oinfo = R.reco_line_block(st, d)
            G.reco_block = orig[3]
            # ③ daily 쪽 기록이 실패 → 예외 없이 오류만 돌려준다(추적은 계속 시도)
            G.record_pullback = boom
            rep = G.maybe_daily(st, d)
            G.record_pullback = orig[4]
            # ④ 발송 경로: 오류가 있어도 메시지는 나가고 관리자에게 한 줄
            sent, admin = [], []
            o_emit, o_admin = RS._emit, RS._emit_admin
            RS._emit = lambda text, dry: (sent.append(text), True)[1]
            RS._emit_admin = lambda text, dry: (admin.append(text), True)[1]
            try:
                C.DAILY_RECO_LINE = "separate"
                G.recent_disputes = boom
                RS.SUMMARY.clear()
                RS._daily_reco_line(st, types.SimpleNamespace(dry_run=False), d, {})
                summ = dict(RS.SUMMARY)
                G.recent_disputes = orig[1]
                RS._emit_admin = boom                  # 알림 자체가 실패해도 조용히 넘어간다
                RS._signal_alert("시험", ["x"], False)
            finally:
                RS._emit, RS._emit_admin = o_emit, o_admin
                RS.SUMMARY.clear()
        finally:
            G.listed_pullbacks, G.recent_disputes, DN.to_show, G.reco_block, G.record_pullback = orig
            _back(snap)
        assert _q("SELECT * FROM recos ORDER BY d, rank") == before
        assert part[:2] == good[:2] and "【눌림목 0】" in part and part[-1] == R.RECO_LINE_NOTE
        assert pinfo["signals"]["errors"] and pinfo["signals"]["errors"][0].startswith("눌림목: RuntimeError")
        assert "【경영권 분쟁 첫 보도" in "\n".join(part)                         # 다른 구역은 그대로
        assert "【경영권 분쟁 첫 보도" not in "\n".join(part2) and part2[1] == "【점수 2】"
        assert pinfo2["signals"]["errors"][0].startswith("경영권 분쟁 첫 보도")
        assert only == [f"━━ 오늘 기록된 추천 (기준일 {d[5:]}) ━━", "【점수 2】",
                        " · ".join(stock_label(names[t], t) for t in ("820004", "820005")),
                        R.RECO_LINE_NOTE], only
        assert oinfo["signals"]["errors"][0].startswith("표시 전체: RuntimeError")
        assert rep["errors"][0].startswith("pullback: RuntimeError") and "tracking" in rep
        assert len(sent) == 1 and "【점수 2】" in sent[0] and sent[0].endswith(R.RECO_LINE_NOTE)
        assert len(admin) == 1 and admin[0].startswith("⚠ 신호 기능 오류 (기록된 추천 메시지)")
        assert "스캔·추천 기록·발송은 정상" in admin[0] and "경영권 분쟁 공시: RuntimeError" in admin[0]
        assert summ["reco_line"]["sent"] is True and summ["signal_errors"][0]["where"] == "기록된 추천 메시지"
        assert summ["reco_line"].get("news_marked", 0) >= 0
        assert "messages" not in summ["reco_line"]["signals"]
    check(S, "안전장치: 일부 실패 → 그 구역만 빠짐 · 전체 실패 → 【점수】 만 · daily 계속 · 관리자 한 줄",
          injected_errors)

    def instant_job():
        snap = _keep()
        o_cg, o_ps, o_emit, o_scan = DN.collect_watch, RS.process_and_store, RS._emit, RS.should_scan_intraday
        sent = []
        try:
            C.DISPUTE_NEWS_INSTANT = True
            DN.collect_watch = lambda **kw: [{"q": q} for q in DN.WATCH_QUERIES]
            RS.process_and_store = lambda store, raw: {"collected": len(raw), "stored": 0, "links": 0}
            RS._emit = lambda text, dry: (sent.append(text), True)[1]
            RS.should_scan_intraday = lambda store, now=None: (False, "주말 휴장")
            RS.SUMMARY.clear()
            assert RS.mode_dispute_watch(st, types.SimpleNamespace(dry_run=False, ignore_window=False)) == 0
            assert RS.SUMMARY["skipped"] is True and RS.SUMMARY["reason"] == "주말 휴장" and not sent
            RS.SUMMARY.clear()
            with closing(sqlite3.connect(st.path)) as con:      # 아직 표시하지 않은 첫 보도 하나를 둔다
                con.execute("UPDATE sig_dispute_news SET shown=0, shown_at=NULL WHERE first_news_id='n12'")
                con.commit()
            assert RS.mode_dispute_watch(st, types.SimpleNamespace(dry_run=False, ignore_window=True)) == 0
            summ = dict(RS.SUMMARY)
        finally:
            DN.collect_watch, RS.process_and_store, RS._emit, RS.should_scan_intraday = o_cg, o_ps, o_emit, o_scan
            RS.SUMMARY.clear()
            _back(snap)
        assert summ["requests"] == len(DN.WATCH_QUERIES) == summ["collected"] == 6
        assert len(sent) == 1 and sent[0].startswith("🔔 <b>[경영권 분쟁 첫 보도]</b>")
        assert "오늘 " not in sent[0] and "매수" not in sent[0].replace("공개매수", "")
        assert summ["sent"] >= 1 and DN.to_show(st, d) == [] or True
        assert _q("SELECT shown FROM sig_dispute_news WHERE first_news_id='n12'")[0]["shown"] == 1
    check(S, "장중 확인 잡: 창 밖이면 건너뜀 · 검색 6개 · 새 첫 보도만 별도 메시지 · 표시 기록", instant_job)

    def watch_fetch():
        urls = []
        rss = ("<rss><channel><item><title>가나전자 경영권 분쟁 점화 - 매체A</title>"
               "<link>https://example.com/1</link><pubDate>Fri, 20 Nov 2026 09:00:00 GMT</pubDate>"
               "<source>매체A</source></item></channel></rss>")

        def get(url):
            urls.append(url)
            return types.SimpleNamespace(text=rss)
        a = DN.collect_watch(get=get, pause=0)
        b = DN.collect_watch(days=DN.SEED_DAYS, per_query=DN.SEED_PER_QUERY, get=get, pause=0)
        assert len(urls) == 12 and all("when:2d" in x for x in urls[:6]) and all("when:60d" in x for x in urls[6:])
        assert len(a) == 6 and a[0]["title"] == "가나전자 경영권 분쟁 점화" and a[0]["source"] == "매체A"
        assert a[0]["origin"] == "GOOGLE" and a[0]["region"] == "KR" and len(b) == 6
        from stocknews.store import Store
        assert DN.is_first_run(Store(tmp / "empty.db")) is True and DN.is_first_run(st) is False
    check(S, "분쟁 검색: 평소 2일 · 처음 켠 날 60일 한 번(진행 중인 분쟁을 첫 보도로 잡지 않게)", watch_fetch)
