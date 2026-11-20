# -*- coding: utf-8 -*-
"""2026-11-20 신호 후보 — 눌림목(PULLBACK_LIST) · 경영권 분쟁(DISPUTE_LIST) ·
반토막 경고(HALF_DROP_WARN). 기록·표시 전용 · 기본 OFF · recos 는 읽기만.
"""
from __future__ import annotations

import inspect
import sqlite3
from contextlib import closing
from datetime import date, timedelta

import numpy as np

S = "11-20 신호 후보(눌림목·분쟁·반토막)"
N_DAYS = 330
D0 = 290                       # 기준일 위치
FLAGS = ("PULLBACK_LIST", "DISPUTE_LIST", "HALF_DROP_WARN", "DAILY_RECO_LINE")


def _cal(n: int, start: str = "2025-06-02") -> list[str]:
    out, cur = [], date.fromisoformat(start)
    while len(out) < n:
        if cur.weekday() < 5:
            out.append(cur.isoformat())
        cur += timedelta(days=1)
    return out


def _series(touch_ok=(), touch_fail=(), spike=None, amt=1e9, base=10000.0, drop_at=None):
    """꾸준히 오르는 종목. touch_ok 날은 20일선에 닿고 지킴, touch_fail 날은 닿고 종가 이탈.
    spike = (위치, 배수) 면 그날 고가만 튀게 한다(고점 대비 거리 조절).
    drop_at = 위치 면 그날 종가를 평균선 아래로 떨어뜨린다(추적 종료용)."""
    c = base * 1.004 ** np.arange(N_DAYS)
    for k in list(touch_fail) + ([drop_at] if drop_at is not None else []):
        c[k] *= 0.95
    o = c * 0.999
    lo = c * 0.99
    h = c * 1.002
    for k in touch_ok:
        ma = c[k - 19:k + 1].mean()
        lo[k] = ma * 1.005
    for k in list(touch_fail) + ([drop_at] if drop_at is not None else []):
        lo[k] = c[k] * 0.99
    if spike:
        h[spike[0]] = c[D0] * spike[1]              # 기준일 종가의 배수
    return o, h, lo, c, np.full(N_DAYS, amt)


def _build(tmp):
    from stocknews.store import Store
    cal = _cal(N_DAYS)
    st = Store(tmp / "signals.db")
    ok4 = (D0 - 100, D0 - 80, D0 - 60, D0 - 40)
    spec = {
        # 종목: (닿고 지킨 날, 닿고 못 지킨 날, 고가 스파이크, 거래대금)
        "810001": (ok4 + (D0,), (), None, 1e9),                    # 4/4
        "810002": (ok4[:3] + (D0,), (D0 - 40,), None, 1e9),        # 3/4 = 75%
        "810003": (ok4 + (D0 - 20, D0), (), None, 1e9),            # 5/5
        "810004": (ok4 + (D0,), (), None, 5e9),                    # 4/4 · 거래대금 큼
        "810005": (ok4 + (D0,), (), (D0 - 30, 1.30), 1e9),         # 고점 −23% → 제외
        "810006": (ok4[:2] + (D0,), (), None, 1e9),                # 묶음 2회 → 후보 아님
        "810007": (ok4 + (D0,), (), None, 2e9),
        "810008": (ok4 + (D0,), (), None, 3e9),
        "810009": (ok4 + (D0,), (), None, 1e9),                    # 유니버스 밖(scans 없음)
        "810010": (ok4 + (D0, D0 + 3), (), None, 9e9),             # D0 등재 → D0+3 재등재 제한
        "810011": ((), (), (D0 - 30, 2.50), 1e9),                  # 고점 −60% (반토막)
        "810012": ((), (), (D0 - 30, 2.00), 1e9),                  # 정확히 −50% 근처(넘지 않음)
        "810013": ((), (), None, 1e9),
    }
    names = {t: f"신호{t[-2:]}" for t in spec}
    names["810011"] = "반토막E&A"
    rows = []
    paths = {}
    for t, (ok, bad, spike, amt) in spec.items():
        o, h, lo, c, a = _series(ok, bad, spike, amt,
                                 drop_at=D0 + 6 if t == "810003" else None)
        paths[t] = (o, h, lo, c)
        rows += [(t, cal[i], float(o[i]), float(h[i]), float(lo[i]), float(c[i]), 1e5, float(a[i]))
                 for i in range(N_DAYS)]
    uni = [t for t in spec if t != "810009"]
    with closing(sqlite3.connect(st.path)) as con:
        con.executemany("INSERT INTO prices(ticker,d,o,h,l,c,v,amt) VALUES(?,?,?,?,?,?,?,?)", rows)
        con.executemany("INSERT INTO tickers(ticker,name,market,active) VALUES(?,?,?,1)",
                        [(t, names[t], "KOSPI") for t in spec])
        for i in range(D0 - 5, N_DAYS):
            con.executemany("INSERT INTO scans(d,ticker,name,price) VALUES(?,?,?,?)",
                            [(cal[i], t, names[t], 1.0) for t in uni])
        rec = [(cal[D0], rk, t, names[t], 1.0, "VALUE", "A", 7.0, 5.0, "r")
               for rk, t in enumerate(("810011", "810001", "810012", "810013"), 1)]
        con.executemany("INSERT INTO recos(d,rank,ticker,name,price,slot,grade,value_score,"
                        "trend_score,reason) VALUES(?,?,?,?,?,?,?,?,?,?)", rec)
        con.commit()
    return st, cal, names, paths


def _naive(lo, c, n, i):
    """정의를 그대로 옮긴 느린 계산 — (묶음 수, 실패 묶음 수, 상승, 닿음, 지킴)."""
    ma = np.array([c[k - n + 1:k + 1].mean() if k >= n - 1 else np.nan for k in range(len(c))])
    tch = [(k >= n - 1) and (lo[k] <= ma[k] * 1.01) for k in range(len(c))]
    a, b = i - 120, i - 1
    cnt = bad = 0
    k = a
    while k <= b:
        if tch[k] and not tch[k - 1]:
            cnt += 1
            e, f = k, False
            while e <= b and tch[e]:
                f = f or c[e] < ma[e]
                e += 1
            bad += f
            k = e
        else:
            k += 1
    return cnt, bad, ma[i] > ma[i - 20], tch[i], tch[i] and c[i] >= ma[i]


def run(check, near, tmp):
    from stocknews import config as C
    from stocknews import events as E
    from stocknews import paper_render as R
    from stocknews import signals as G
    from stocknews.env import KNOWN_KEYS
    from stocknews.renderer import stock_label

    st, cal, names, paths = _build(tmp)
    d = cal[D0]

    def _keep():
        return {n: getattr(C, n) for n in FLAGS}

    def _back(snap):
        for n, v in snap.items():
            setattr(C, n, v)

    def _dump(table):
        with closing(sqlite3.connect(st.path)) as con:
            return con.execute(f"SELECT * FROM {table}").fetchall()

    def default_off():
        for f in FLAGS[:3]:
            assert getattr(C, f) is False, f
            assert C.ENV_OVERRIDE_FLAGS[f] is bool and f in KNOWN_KEYS, f
        assert C.PULLBACK_MAX_DROP == 0.15 and C.PULLBACK_MAX_PER_DAY == 5
        assert C.PULLBACK_COOLDOWN_DAYS == 20 and C.HALF_DROP_LIMIT == 0.50
        assert not G.any_on()
        before = (_dump("recos"), _dump("sig_pullback"), _dump("sig_paper"), _dump("sig_dispute"))
        assert G.maybe_daily(st, d) is None and G.maybe_collect_disputes(st) is None
        assert G.type_lines(st, d) == [] and R._signal_type_lines(st, d) == []
        lines, info = R.reco_line_block(st, d)
        assert lines == [f"━━ 오늘 기록된 추천 4 (기준일 {d[5:]}) ━━",
                         " · ".join(stock_label(names[t], t) for t in
                                    ("810011", "810001", "810012", "810013")),
                         R.RECO_LINE_NOTE], lines
        assert set(info) == {"d", "n", "tickers"}, info
        assert R.reco_line_block(st, cal[D0 - 1])[0] == [R.RECO_LINE_EMPTY]
        assert before == (_dump("recos"), _dump("sig_pullback"), _dump("sig_paper"),
                          _dump("sig_dispute"))
    check(S, "기본 OFF · OFF 면 판정·기록 없음 · 기록된 추천 메시지 그대로", default_off)

    def arrays_match_definition():
        rng = np.random.default_rng(20261120)
        n_cmp = n_c = 0
        for q in range(18):
            m = 420
            dr, vol = ((0.0008, 0.02), (0.003, 0.012), (0.005, 0.01))[q % 3]
            c = 5000 * np.exp(np.cumsum(rng.normal(dr, vol, m)))
            lo = c * (1 - np.abs(rng.normal(0, 0.012, m)))
            for n in G.MAS:
                arr = G.pullback_arrays(lo, c, n)
                pick = np.unique(np.r_[rng.integers(n + 120, m, 25),
                                       np.flatnonzero(arr["cand"])[:20]])
                for k in pick:
                    cnt, bad, rise, tch, hld = _naive(lo, c, n, int(k))
                    assert (arr["runs"][k], arr["held"][k]) == (cnt, cnt - bad), (n, k)
                    well = rise and cnt >= 3 and (cnt - bad) / cnt >= 0.75
                    assert bool(arr["cand"][k]) == bool(well and hld), (n, k)
                    n_cmp += 1
                    n_c += int(arr["cand"][k])
        assert n_cmp > 1000 and n_c > 100, (n_cmp, n_c)
        assert G.pullback_arrays(np.ones(140), np.ones(140), 20) is None      # 봉 부족
    check(S, "눌림목 판정 = 정의 그대로 계산(묶음 수·지킨 수·후보)", arrays_match_definition)

    def drop_boundaries():
        h = np.full(300, 100.0)
        c = np.full(300, 85.0)
        mh, drop = G.high_drop(h, c)
        near(drop[-1], 0.15)
        assert not G._exceeds(drop[-1], 0.15)                                  # 정확히 15% 는 넘지 않음
        c2 = c.copy()
        c2[-1] = 84.9
        assert G._exceeds(G.high_drop(h, c2)[1][-1], 0.15)
        c3 = c.copy()
        c3[-1] = 120.0
        assert G.high_drop(h, c3)[1][-1] == 0.0                               # 신고가 → 0
        assert np.isnan(G.high_drop(h[:250], c[:250])[1][-1])                 # 250봉 이하 → 값 없음
        assert np.isnan(mh[249]) and mh[250] == 100.0
    check(S, "고점 대비 거리: 직전 250봉 고가 · 경계값 · 신고가 0", drop_boundaries)

    def record_and_rank():
        snap = _keep()
        before = _dump("recos")
        try:
            C.PULLBACK_LIST = True
            rep = G.maybe_daily(st, d)
            again = G.record_pullback(st, d)                                   # 다시 돌려도 같다
        finally:
            _back(snap)
        assert _dump("recos") == before, "recos 가 바뀌었다"
        with closing(sqlite3.connect(st.path)) as con:
            con.row_factory = sqlite3.Row
            rows = {r["ticker"]: dict(r) for r in con.execute(
                "SELECT * FROM sig_pullback WHERE d=?", (d,))}
        assert set(rows) == {"810001", "810002", "810003", "810004", "810005", "810007",
                             "810008", "810010"}, sorted(rows)
        assert rows["810001"]["runs"] == 4 and rows["810001"]["held"] == 4
        assert rows["810002"]["runs"] == 4 and rows["810002"]["held"] == 3
        assert rows["810003"]["runs"] == 5 and rows["810003"]["ma"] == 20
        assert rows["810005"]["skip"] == "drop" and rows["810005"]["listed"] == 0
        assert 20 < rows["810005"]["drop_pct"] < 26
        ma = paths["810001"][3][D0 - 19:D0 + 1].mean()
        near(rows["810001"]["ma_value"], ma)
        assert rows["810001"]["low"] <= ma * 1.01
        listed = sorted((r["rank"], t) for t, r in rows.items() if r["listed"])
        # 지킨 비율 → 닿은 횟수 → 거래대금: 5/5 가 먼저, 4/4 는 거래대금 큰 순, 3/4 는 밖
        assert [t for _, t in listed] == ["810003", "810010", "810004", "810008", "810007"], listed
        assert rows["810001"]["skip"] == "max" and rows["810002"]["skip"] == "max"
        assert rep["pullback"]["listed"] == 5 and again["listed"] == 5
        assert rep["pullback"]["skipped"] == {"drop": 1, "max": 2}, rep["pullback"]
    check(S, "기록: 유니버스 안 · 고점 15% 초과 제외 · 순서 · 하루 5개 · recos 불변",
          record_and_rank)

    def cooldown():
        snap = _keep()
        try:
            C.PULLBACK_LIST = True
            for i in (D0 + 1, D0 + 2, D0 + 3):
                G.record_pullback(st, cal[i])
        finally:
            _back(snap)
        with closing(sqlite3.connect(st.path)) as con:
            r = con.execute("SELECT listed, skip FROM sig_pullback WHERE d=? AND ticker=?",
                            (cal[D0 + 3], "810010")).fetchone()
        assert r == (0, "cooldown"), r
        hist = G.scan_dates(st, [cal[D0], cal[D0 + 3]])
        row = [x for x in hist[cal[D0 + 3]] if x["ticker"] == "810010"][0]
        assert row["skip"] == "cooldown"
        loose = G.scan_dates(st, [cal[D0]], max_drop=0.30)
        assert [x for x in loose[cal[D0]] if x["ticker"] == "810005"][0]["skip"] != "drop"
    check(S, "같은 종목 20거래일 재등재 제한 · 기준값을 바꾼 소급 판정", cooldown)

    def classify():
        f = G.classify_dispute
        assert f("소송등의제기ㆍ신청(경영권분쟁소송)") == "lawsuit"
        assert f("소송 등의 제기ㆍ신청 (경영권분쟁소송)") == "lawsuit"
        assert f("[기재정정]소송등의제기ㆍ신청(경영권분쟁소송)") is None
        assert f("소송등의판결ㆍ결정(경영권분쟁소송)") is None
        assert f("소송등의제기ㆍ신청(일정금액이상의청구)") is None
        assert f("공개매수신고서") == "tender" and f("공개매수결과보고서") is None
        assert f("[첨부추가]공개매수신고서") is None and f("공개매수설명서") is None
        assert f("의결권대리행사권유참고서류", "가나다", "가나다") is None            # 회사가 낸 것
        assert f("의결권대리행사권유참고서류", "(주)가나다", "가나다") is None
        assert f("의결권대리행사권유에관한의견표명서", "가나다", "가나다") == "proxy"   # 주주 측 권유에 대한 회사 의견
        assert f("의결권대리행사권유참고서류", "행동주의펀드", "가나다") == "proxy"
        assert f("주식등의대량보유상황보고서(일반)", "누구", "가나다") is None
        nxt = lambda x: cal[E._pos(cal, x) + 1] if E._pos(cal, x) + 1 < len(cal) else None  # noqa: E731
        assert G.dispute_event_day(cal[10], nxt) == cal[11]
        assert G.dispute_event_day(cal[-1], nxt) is None
    check(S, "분쟁 공시 분류(소송 제기·공개매수 신고·주주 측 권유) · 사건일 = 접수일 다음 거래일",
          classify)

    def collect_stub():
        import os
        calls = []

        def get(params):
            calls.append(dict(params))
            if params["pblntf_detail_ty"] != "I001":
                return {"status": "013", "message": "조회된 데이타가 없습니다."}
            page = params["page_no"]
            lst = [{"rcept_no": f"2026{page:02d}00000{i}", "stock_code": "810001" if i else "",
                    "corp_name": "신호01", "report_nm": "소송등의제기ㆍ신청(경영권분쟁소송)" if i < 2
                    else "단일판매ㆍ공급계약체결", "flr_nm": "신호01", "rcept_dt": "20261118"}
                   for i in range(3)]
            return {"status": "000", "total_page": 2, "list": lst}
        old = os.environ.get("DART_API_KEY")
        try:
            os.environ.pop("DART_API_KEY", None)
            assert G.collect_disputes(get=get) == ([], {"requests": 0, "skipped": "no_dart_key"})
            os.environ["DART_API_KEY"] = "x" * 8
            items, info = G.collect_disputes(get=get, pause=0)
        finally:
            if old is None:
                os.environ.pop("DART_API_KEY", None)
            else:
                os.environ["DART_API_KEY"] = old
        assert info["requests"] == 4 and len(items) == 2, (info, items)      # I001 2쪽 + 나머지 1쪽씩
        assert all(i["ticker"] == "810001" and i["kind"] == "lawsuit"
                   and i["rcept_dt"] == "2026-11-18" for i in items)
        assert all("crtfc_key" in c for c in calls)
    check(S, "분쟁 공시 수집: 키 없으면 건너뜀 · 쪽 넘김 · 종목코드 없는 행 제외(네트워크 없음)",
          collect_stub)

    def tracking():
        snap = _keep()
        try:
            C.PULLBACK_LIST = True
            C.DISPUTE_LIST = True
            items = [
                {"rcept_no": "A1", "ticker": "810013", "name": "신호13", "kind": "lawsuit",
                 "rcept_dt": cal[D0], "title": "소송등의제기ㆍ신청(경영권분쟁소송)", "filer": "신호13"},
                {"rcept_no": "A2", "ticker": "810013", "name": "신호13", "kind": "tender",
                 "rcept_dt": cal[D0 + 5], "title": "공개매수신고서", "filer": "누구"},
                {"rcept_no": "A3", "ticker": "810009", "name": "신호09", "kind": "lawsuit",
                 "rcept_dt": cal[D0], "title": "소송등의제기ㆍ신청(경영권분쟁소송)", "filer": "신호09"},
            ]
            assert G.record_disputes(st, items) == 3 and G.record_disputes(st, items) == 0
            G.update_tracking(st, cal[D0])                # 다음 거래일 시가가 아직 없다
            assert _dump("sig_paper") == []
            s1 = G.update_tracking(st, cal[D0 + 8])
            p_mid = _dump("sig_paper")
            G.update_tracking(st, cal[-1])
            assert G.update_tracking(st, cal[-1]).get("results", 0) == 0     # 멱등
        finally:
            _back(snap)
        with closing(sqlite3.connect(st.path)) as con:
            con.row_factory = sqlite3.Row
            pap = [dict(r) for r in con.execute("SELECT * FROM sig_paper ORDER BY id")]
            res = {(r["paper_id"], r["rule"]): dict(r)
                   for r in con.execute("SELECT * FROM sig_results")}
            try:
                con.execute("UPDATE sig_paper SET enrolled=0")
                raise AssertionError("sig_paper 가 수정됐다")
            except sqlite3.DatabaseError as exc:
                assert "append-only" in str(exc)
            try:
                con.execute("DELETE FROM sig_results")
                raise AssertionError("sig_results 가 삭제됐다")
            except sqlite3.DatabaseError as exc:
                assert "append-only" in str(exc)
        assert s1["enrolled"] >= 6 and len(p_mid) <= len(pap)
        pb = {p["ticker"]: p for p in pap if p["kind"] == "pullback" and p["signal_d"] == d}
        assert set(pb) == {"810003", "810010", "810004", "810008", "810007"}, sorted(pb)
        p3 = pb["810003"]
        o, _, _, c = paths["810003"]
        assert p3["entry_date"] == cal[D0 + 1]
        near(p3["entry_price"], o[D0 + 1])
        assert p3["ref"] == "20" and p3["enrolled"] == 1
        h20 = res[(p3["id"], "h20")]
        assert h20["exit_date"] == cal[D0 + 20] and h20["days"] == 20
        near(h20["net"], (c[D0 + 20] / o[D0 + 1] - 1) * 100 - C.ROUND_TRIP_COST_PCT)
        mx = res[(p3["id"], "ma_exit")]                    # D0+6 종가 이탈 → D0+7 시가
        assert mx["exit_date"] == cal[D0 + 7] and mx["days"] == 7, mx
        near(mx["net"], (o[D0 + 7] / o[D0 + 1] - 1) * 100 - C.ROUND_TRIP_COST_PCT)
        assert (pb["810004"]["id"], "ma_exit") not in res  # 평균선 아래로 내려간 적 없음 → 미정
        ds = [p for p in pap if p["kind"] == "dispute"]
        assert [(p["ticker"], p["signal_d"], p["enrolled"]) for p in ds] == [
            ("810009", cal[D0 + 1], 0), ("810013", cal[D0 + 1], 1)], ds     # 두 번째 공시는 같은 분쟁
        assert ds[0]["skip_reason"] == "운영 유니버스 밖" and ds[1]["entry_date"] == cal[D0 + 2]
        assert (ds[1]["id"], "h20") in res and (ds[1]["id"], "ma_exit") not in res
    check(S, "종이 추적: 다음 거래일 시가 편입 · 20일 보유 · 평균선 이탈 종료 · 같은 분쟁 1건 · 추가 전용",
          tracking)

    def message():
        snap = _keep()
        before = _dump("recos")
        try:
            C.HALF_DROP_WARN = True
            only_half, i1 = R.reco_line_block(st, d)
            C.PULLBACK_LIST = True
            C.DISPUTE_LIST = True
            full, i2 = R.reco_line_block(st, d)
            none_day, _ = R.reco_line_block(st, cal[D0 - 1])
            C.HALF_DROP_WARN = False
            C.DISPUTE_LIST = False
            only_pull, _ = R.reco_line_block(st, d)
        finally:
            _back(snap)
        assert _dump("recos") == before
        assert only_half[0] == f"━━ 오늘 기록된 추천 (기준일 {d[5:]}) ━━"
        assert only_half[1] == "【점수 4】" and only_half[-1] == R.RECO_LINE_NOTE and len(only_half) == 4
        parts = only_half[2].split(" · ")
        assert parts[0].startswith("반토막E&amp;A(810011) ⚠고점 −60%"), parts[0]
        assert parts[2] == stock_label(names["810012"], "810012"), parts[2]   # 정확히 50% 는 표시 안 함
        assert i1["signals"]["half_drop"] == ["810011"]
        assert full[1] == "【점수 4】" and "(눌림목" not in full[2]             # 810001 은 목록 밖(max)
        k = full.index("【눌림목 5】")
        assert full[k + 1] == f"{stock_label(names['810003'], '810003')} 20일선 · 고점 −0%"
        j = full.index("【경영권 분쟁 공시 · 주의 1】")
        assert full[j + 1] == "(과거 8년: 공시 뒤 20일 평균 \u22123%, 10번 중 3번 오름)", full[j + 1]
        assert full[j + 2] == f"{stock_label(names['810013'], '810013')} 경영권 분쟁 소송 {d[5:]}"
        assert j == k + 6 and full[-1] == R.RECO_LINE_NOTE
        assert full[2].endswith(f"{stock_label(names['810013'], '810013')} ⚠분쟁 공시"), full[2]
        assert i2["signals"]["messages"] == ["\n".join(full)]           # 상한 안이면 메시지 1개
        assert i2["signals"]["dispute"] == ["810013"]                         # 유니버스 밖 810009 는 없음
        text = "\n".join(full)
        assert "매수" not in text.replace("공개매수", "") and "매도" not in text
        assert none_day[1:4] == ["【점수 0】", "없음", "【눌림목 0】"] and none_day[4] == "없음"
        assert "【경영권 분쟁" not in "\n".join(none_day)                       # 없으면 줄 생략
        assert "【경영권 분쟁" not in "\n".join(only_pull) and "⚠" not in "\n".join(only_pull)
        snap = _keep()
        try:                                              # 점수 종목이 눌림목에도 해당하면 표시
            C.PULLBACK_LIST = True
            with closing(sqlite3.connect(st.path)) as con:
                con.execute("INSERT INTO recos(d,rank,ticker,name,price,slot,grade,value_score,"
                            "trend_score,reason) VALUES(?,?,?,?,?,?,?,?,?,?)",
                            (d, 5, "810003", names["810003"], 1.0, "TREND", "A", 5.0, 7.0, "r"))
                con.commit()
            tagged, _ = R.reco_line_block(st, d)
            assert tagged[2].endswith(f"{stock_label(names['810003'], '810003')} (눌림목 20일선)")
        finally:
            _back(snap)
            with closing(sqlite3.connect(st.path)) as con:
                con.execute("DELETE FROM recos WHERE d=? AND rank=5", (d,))
                con.commit()
    check(S, "합친 메시지: 【점수】⚠고점·(눌림목) · 【눌림목】 · 【경영권 분쟁】 · 없음/생략 · 지시 문구 없음",
          message)

    def weekly_lines():
        snap = _keep()
        try:
            C.PULLBACK_LIST = True
            few = G.type_lines(st, cal[-1])
            C.DISPUTE_LIST = True
            both = G.type_lines(st, cal[-1])
            db2 = tmp / "many.db"
            from stocknews.store import Store
            s2 = Store(db2)
            with closing(sqlite3.connect(s2.path)) as con:
                for i in range(24):
                    con.execute("INSERT INTO sig_paper(kind,ticker,signal_d,ref,enrolled) "
                                "VALUES('pullback',?,?, '20', 1)", (f"9{i:05d}", cal[10]))
                    con.execute("INSERT INTO sig_results(paper_id,rule,exit_date,status,net,days) "
                                "VALUES(?,?,?,?,?,?)", (i + 1, "h20", cal[40], E.ST_OK, i - 10.0, 20))
                    con.execute("INSERT INTO sig_results(paper_id,rule,exit_date,status,net,days) "
                                "VALUES(?,?,?,?,?,?)", (i + 1, "ma_exit", cal[20], E.ST_OK, 1.0, 5))
                con.commit()
            many = G.type_lines(s2, cal[-1])
            early = G.type_lines(s2, cal[30])
        finally:
            _back(snap)
        assert len(few) == 2 and all("표본 부족" in x for x in few), few
        assert few[0].startswith("• 눌림목 · 20거래일 보유: 표본 부족 (n=")
        assert len(both) == 3 and both[2].startswith("• 경영권 분쟁 공시 · 20거래일 보유: 표본 부족")
        assert many[0] == "• 눌림목 · 20거래일 보유: 평균 +1.50% · 중앙 +1.50% · 승률 54% (n=24)", many[0]
        assert many[1].endswith("(n=24) · 평균 보유 5.0거래일"), many[1]
        assert "표본 부족 (n=0)" in early[0] and "(n=24)" in early[1]         # 그날까지 끝난 것만
    check(S, "주간 유형별 줄: 20건 미만 표본 부족 · 켜진 유형만", weekly_lines)

    def read_only_source():
        src = inspect.getsource(G)
        for bad in ("INTO recos", "UPDATE recos", "DELETE FROM recos", "INTO scans",
                    "INTO paper_", "UPDATE prices", "send_telegram"):
            assert bad not in src, bad
        assert "매수" not in src.replace("공개매수", "") and "매도" not in src
    check(S, "signals 는 recos·scans·paper_*·시세를 쓰지 않고 발송하지 않는다", read_only_source)
