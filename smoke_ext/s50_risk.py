# -*- coding: utf-8 -*-
"""[E] 위험 정보 — σ·원뿔·손절선 도달 확률·원뿔 적중률·리밸런싱.

가상 데이터만 쓴다(네트워크 없음). 경계: 최소 표본 60 / 59, 수정주가
불연속(+40%) 뒤 재시작, 상한가(+30% 정확히)는 불연속 아님, 거래정지 건너뜀,
미래 데이터 금지, 도달 확률 경계값·몬테카를로 대조, 플래그 OFF = 지금과
같음, 리밸런싱 밴드 경계·매수/매도 단어·손익 부재.
"""
from __future__ import annotations

import math
import sqlite3
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

S = "v2[E] 위험 정보"


def _cal(n: int, start: str = "2025-01-02") -> list[str]:
    out, cur = [], date.fromisoformat(start)
    while len(out) < n:
        if cur.weekday() < 5:
            out.append(cur.isoformat())
        cur += timedelta(days=1)
    return out


def _ref_sigma(closes, lam=0.94, seed=20, min_obs=60, lim=0.30 + 1e-6):
    """참조 구현(파이썬 루프). 정의를 글자 그대로 옮긴 것."""
    prev, cnt, ss, var = None, 0, 0.0, None
    for c in closes:
        if c is None or not (c == c) or c <= 0:
            continue
        if prev is not None:
            ratio = c / prev
            if abs(ratio - 1.0) > lim:
                cnt, ss, var = 0, 0.0, None
            else:
                r2 = math.log(ratio) ** 2
                cnt += 1
                if cnt <= seed:
                    ss += r2
                    var = ss / cnt
                else:
                    var = lam * var + (1 - lam) * r2
        prev = c
    last = closes[-1]
    if last is None or not (last == last) or cnt < min_obs or var is None:
        return float("nan")
    return math.sqrt(var)


def _store(tmp: Path, name: str, series: dict[str, list], cal: list[str],
           sectors: dict | None = None):
    from stocknews.store import Store
    st = Store(tmp / name)
    rows = []
    for t, cs in series.items():
        for d, c in zip(cal, cs):
            if c is None or not (c == c):
                continue
            rows.append((t, d, c, c, c, c, 1000.0, c * 1000.0))
    with closing(sqlite3.connect(st.path)) as con:
        con.executemany("INSERT INTO prices(ticker,d,o,h,l,c,v,amt) "
                        "VALUES (?,?,?,?,?,?,?,?)", rows)
        con.executemany(
            "INSERT OR REPLACE INTO tickers(ticker,name,market,sector,active) "
            "VALUES (?,?,?,?,1)",
            [(t, f"종목{t}", "KOSPI", (sectors or {}).get(t, "업종A"))
             for t in series])
        con.commit()
    return st


def run(check, near, tmp):
    from stocknews import config as C
    from stocknews import risk

    rng = np.random.default_rng(7)

    # ── σ 정의 ──
    def alt_anchor():
        # ±2% 교대 로그수익률 → r² 상수 → σ = 0.02 정확히
        lr = np.array([0.02 if i % 2 else -0.02 for i in range(80)])
        c = 1000 * np.exp(np.concatenate([[0], np.cumsum(lr)]))
        near(risk.ewma_sigma_last(c)[0], 0.02, 1e-12, "교대 ±2%")
    check(S, "σ 앵커: 교대 ±2% → 0.02", alt_anchor)

    def vs_reference():
        lr = rng.normal(0, 0.025, 150)
        c = list(1000 * np.exp(np.concatenate([[0], np.cumsum(lr)])))
        c[40] = float("nan")                 # 정지 1일
        got = risk.ewma_sigma_last(np.array(c, dtype=float))[0]
        near(got, _ref_sigma(c), 1e-12, "참조 구현")
    check(S, "σ 재귀 = 참조 루프 (정지일 건너뜀)", vs_reference)

    def min_obs():
        lr = rng.normal(0, 0.02, 60)
        c = 1000 * np.exp(np.concatenate([[0], np.cumsum(lr)]))   # 수익률 60
        assert np.isfinite(risk.ewma_sigma_last(c)[0]), "60개인데 NULL"
        assert np.isnan(risk.ewma_sigma_last(c[1:])[0]), "59개인데 값"
    check(S, "최소 표본 60 → 값 / 59 → NULL", min_obs)

    def breaks():
        lr = rng.normal(0, 0.02, 200)
        c = 1000 * np.exp(np.concatenate([[0], np.cumsum(lr)]))
        c2 = c.copy()
        c2[100:] *= 1.4                      # +40% 불연속(분할 흔적)
        # 불연속 뒤 수익률 59개 → NULL, 60개 → 불연속 뒤만 쓴 값
        assert np.isnan(risk.ewma_sigma_last(c2[:160])[0]), "불연속 뒤 59 = NULL"
        got = risk.ewma_sigma_last(c2[:161])[0]
        near(got, risk.ewma_sigma_last(c2[100:161])[0], 1e-12, "재시작")
        # 상한가(정확히 +30%)는 실제 거래 → 재시작하지 않는다
        c3 = c.copy()
        c3[150:] = c3[150:] / c3[150] * c3[149] * 1.3
        assert np.isfinite(risk.ewma_sigma_last(c3[:160])[0]), "+30% 를 불연속으로 봄"
    check(S, "수정주가 불연속 → 재시작·60일 NULL / +30% 는 아님", breaks)

    def suspended_last():
        lr = rng.normal(0, 0.02, 90)
        c = 1000 * np.exp(np.concatenate([[0], np.cumsum(lr)]))
        c[-1] = np.nan
        assert np.isnan(risk.ewma_sigma_last(c)[0]), "그날 정지인데 값"
    check(S, "그날 종가 없음(정지) → NULL", suspended_last)

    # ── DB 기록 · 소급 · 미래 데이터 금지 ──
    cal = _cal(200)
    ser = {}
    for k, t in enumerate(("000010", "000020", "000030")):
        lr = rng.normal(0, 0.01 * (k + 1), len(cal))
        ser[t] = list(1000 * np.exp(np.cumsum(lr)))
    st = _store(tmp, "risk.db", ser, cal)

    def schema():
        with closing(sqlite3.connect(st.path)) as con:
            cols = [r[1] for r in con.execute("PRAGMA table_info(risk_metrics)")]
            pk = [r[1] for r in sorted(
                con.execute("PRAGMA table_info(risk_metrics)"),
                key=lambda r: r[5]) if r[5]]
        assert cols == ["d", "ticker", "sigma_ewma", "lam", "calc_version",
                        "kind", "created"], cols
        assert pk == ["d", "ticker", "calc_version"], pk
    check(S, "risk_metrics 스키마 = I 묶음 계약", schema)

    def record_and_backfill():
        d = cal[150]
        rep = risk.record_daily(st, d)
        assert rep["written"] == 3, rep
        with closing(sqlite3.connect(st.path)) as con:
            live = dict(con.execute(
                "SELECT ticker, sigma_ewma FROM risk_metrics WHERE d=? "
                "AND kind='실시간' AND calc_version=?",
                (d, C.RISK_CALC_VERSION)).fetchall())
        # 그날까지 데이터만: 이후 시세를 바꿔도 σ(d) 는 같아야 한다
        for t in ser:
            want = _ref_sigma(ser[t][max(0, 150 - C.RISK_SIGMA_WINDOW):151])
            near(live[t], want, 1e-12, f"{t} 실시간 = 참조")
        b = risk.backfill(st)
        assert b["dates"] == len(cal), b
        with closing(sqlite3.connect(st.path)) as con:
            kinds = dict(con.execute(
                "SELECT kind, COUNT(*) FROM risk_metrics GROUP BY kind"))
            v = dict(con.execute(
                "SELECT ticker, sigma_ewma FROM risk_metrics WHERE d=?",
                (d,)).fetchall())
            k_d = {r[0] for r in con.execute(
                "SELECT kind FROM risk_metrics WHERE d=?", (d,))}
        assert k_d == {"실시간"}, f"소급이 실시간 행을 덮었다 {k_d}"
        assert kinds.get("소급", 0) > 0, kinds
        for t in ser:
            near(v[t], live[t], 1e-15, "실시간 행 보존")
        # 소급 값 = 같은 날 실시간 계산 값 (같은 정의·같은 창)
        d2 = cal[180]
        with closing(sqlite3.connect(st.path)) as con:
            bf = dict(con.execute(
                "SELECT ticker, sigma_ewma FROM risk_metrics WHERE d=?",
                (d2,)).fetchall())
        close = risk.close_panel(st, end=d2, rows=C.RISK_SIGMA_WINDOW + 1)
        sig = risk.sigma_panel(close, targets=[d2])
        for t in ser:
            near(bf[t], float(sig.at[d2, t]), 1e-15, f"{t} 소급 = 실시간")
        # 재실행 = 0행 (멱등)
        assert risk.backfill(st)["written"] == 0, "소급 재실행이 행을 썼다"
    check(S, "실시간 기록 · 소급(INSERT OR IGNORE) · 멱등", record_and_backfill)

    def no_future():
        st2 = _store(tmp, "risk_future.db", ser, cal)
        a = risk.sigma_panel(risk.close_panel(st2), targets=[cal[120]])
        with closing(sqlite3.connect(st2.path)) as con:
            con.execute("UPDATE prices SET c=c*3 WHERE d>?", (cal[120],))
            con.commit()
        b = risk.sigma_panel(risk.close_panel(st2), targets=[cal[120]])
        for t in ser:
            near(a.at[cal[120], t], b.at[cal[120], t], 0, "미래 변경 영향")
    check(S, "미래 데이터 금지 (그날 이후 시세 변경 무영향)", no_future)

    # ── 훅 · 플래그 ──
    def hooks_flag():
        from stocknews import v2_hooks as H
        old = C.RISK_INFO
        try:
            C.RISK_INFO = False
            assert H.sigma_on(st, cal[150], "000010") is None
            assert H.stop_hit_prob(st, cal[150], "000010", 100, 90) is None
            C.RISK_INFO = True
            s = H.sigma_on(st, cal[150], "000010")
            assert s is not None and 0 < s < 0.05, s
            # 기록이 없는 날 → 같은 정의로 즉석 계산(기록과 같아야 한다)
            with closing(sqlite3.connect(st.path)) as con:
                rec = con.execute(
                    "SELECT sigma_ewma FROM risk_metrics WHERE d=? AND "
                    "ticker='000010'", (cal[170],)).fetchone()[0]
                con.execute("DELETE FROM risk_metrics WHERE d=?", (cal[170],))
                con.commit()
            near(H.sigma_on(st, cal[170], "000010"), rec, 1e-15, "즉석 = 기록")
            # 주말 → 직전 거래일
            sat = (date.fromisoformat(cal[150]) + timedelta(
                days=(5 - date.fromisoformat(cal[150]).weekday()) % 7)).isoformat()
            if sat not in cal:
                prev = max(x for x in cal if x <= sat)
                near(H.sigma_on(st, sat, "000010"),
                     H.sigma_on(st, prev, "000010"), 0, "주말 스냅")
            p = H.stop_hit_prob(st, cal[150], "000010", 100.0, 90.0)
            near(p, risk.stop_hit_probability(100.0, 90.0, s, 20), 1e-15, "훅")
            assert H.sigma_on(st, cal[150], "999999") is None
        finally:
            C.RISK_INFO = old
    check(S, "훅: OFF → None · ON → 기록/즉석 계산", hooks_flag)

    # ── 손절선 도달 확률 ──
    def prob_bounds():
        f = risk.stop_hit_probability
        assert f(100, 100, 0.02, 20) == 1.0, "가격 = 손절선 → 1"
        assert f(95, 100, 0.02, 20) == 1.0, "이미 아래 → 1"
        assert f(100, 90, 0.02, 0) == 0.0, "기간 0 → 0"
        assert f(100, 0, 0.02, 20) == 0.0, "손절선 0 → 0"
        assert f(100, 90, 0.0, 20) == 0.0, "σ 0 → 0"
        assert f(100, 90, None, 20) is None and f(None, 90, 0.02) is None
        assert f(100, 90, float("nan"), 20) is None
        # 단조성: σ↑·기간↑·손절선↑ → 확률↑
        assert f(100, 90, 0.01, 20) < f(100, 90, 0.02, 20) < f(100, 90, 0.04, 20)
        assert f(100, 90, 0.02, 5) < f(100, 90, 0.02, 20) < f(100, 90, 0.02, 60)
        assert f(100, 80, 0.02, 20) < f(100, 90, 0.02, 20) < f(100, 95, 0.02, 20)
        # 연속 관측 앵커: 2Φ(ln0.9/(0.03√20))
        want = 2 * 0.5 * math.erfc(-(math.log(0.9) / (0.03 * math.sqrt(20)))
                                   / math.sqrt(2))
        near(f(100, 90, 0.03, 20, discrete=False), want, 1e-12, "연속 앵커")
        assert f(100, 90, 0.03, 20) < want, "이산 보정은 확률을 낮춘다"
    check(S, "도달 확률 경계값·단조성·연속 앵커", prob_bounds)

    def prob_monte_carlo():
        g = np.random.default_rng(11)
        n, days = 200_000, 20
        for sigma, stop in ((0.03, 90.0), (0.02, 95.0), (0.05, 85.0)):
            z = g.standard_normal((n, days)) * sigma
            path = 100.0 * np.exp(np.cumsum(z, axis=1))      # 일간 종가
            mc = float((path.min(axis=1) <= stop).mean())
            got = risk.stop_hit_probability(100.0, stop, sigma, days)
            assert abs(got - mc) < 0.012, \
                f"σ{sigma} 손절{stop}: 공식 {got:.4f} vs MC {mc:.4f}"
    check(S, "도달 확률 ≈ 몬테카를로(종가 관측) ±1.2%p", prob_monte_carlo)

    def cone_and_multiple():
        cb = risk.cone_bounds(10_000, 0.02, 20)
        near(cb[1][1][-1], 10_000 * math.exp(0.02 * math.sqrt(20)), 1e-6, "+1σ")
        near(cb[2][0][-1], 10_000 * math.exp(-0.04 * math.sqrt(20)), 1e-6, "-2σ")
        near(cb[1][0][0], 10_000, 1e-9, "t=0")
        near(risk.move_multiple(0.10, 0.02, 4), math.log(1.1) / 0.04, 1e-12, "n배")
        assert risk.move_multiple(-0.05, 0.02, 1) < 0
        for bad in ((0.1, None, 5), (0.1, 0, 5), (0.1, 0.02, 0), (-1.0, 0.02, 5)):
            assert risk.move_multiple(*bad) is None, bad
    check(S, "원뿔 경계 · '평소 출렁임의 n배' 정의", cone_and_multiple)

    # ── 원뿔 적중률 ──
    def hit_rate():
        cal2 = _cal(130)
        # A: 교대 ±1% → σ=0.01, 20일 뒤 누적 = 0 → 항상 ±1σ 안
        # B: 교대 ±1% 다가 마지막 30일 매일 +3% → 원뿔 위 이탈
        la = np.array([0.01 if i % 2 else -0.01 for i in range(len(cal2))])
        lb = la.copy()
        lb[100:] = 0.03
        sa = list(1000 * np.exp(np.cumsum(la)))
        sb = list(1000 * np.exp(np.cumsum(lb)))
        st3 = _store(tmp, "risk_cone.db", {"000A10": sa, "000B20": sb}, cal2)
        risk.backfill(st3)
        recos = pd.DataFrame({"d": [cal2[70], cal2[100]],
                              "ticker": ["000A10", "000B20"],
                              "rank": [1, 1]})
        rep = risk.cone_hit_rate(st3, days=20, recos=recos)
        assert rep["reco"]["n"] == 2, rep["reco"]
        near(rep["reco"]["hit1"], 0.5, 1e-12, "추천 적중 1/2")
        assert rep["reco"]["above1"] == 1 and rep["reco"]["below1"] == 0
        assert rep["all"]["n"] > 0 and rep["all"]["hit2"] >= rep["all"]["hit1"]
        assert not rep["reco"]["enough"], "2건인데 판정 가능"
        txt = "\n".join(risk.render_cone_block(rep))
        assert "표본 부족" in txt and "±1σ" in txt
        for bad in ("개선", "양호", "매수", "매도"):
            assert bad not in txt, bad
    check(S, "원뿔 적중률 (적중/위 이탈 · 표본 부족)", hit_rate)

    # ── 플래그 OFF = 지금과 같음 ──
    def off_identical():
        from stocknews import daily, weekly
        old = C.RISK_INFO
        try:
            C.RISK_INFO = False
            st4 = _store(tmp, "risk_off.db", ser, cal)
            assert daily._record_risk(st4, cal[-1]) is None
            with closing(sqlite3.connect(st4.path)) as con:
                assert con.execute(
                    "SELECT COUNT(*) FROM risk_metrics").fetchone()[0] == 0
            assert risk.chart_risk(st4, cal[-1], "000010", 1000.0) is None
            rep = weekly.weekly_report(st4)
            assert "cone" not in rep, "OFF 인데 원뿔 키"
            C.RISK_INFO = True
            assert daily._record_risk(st4, cal[-1])["written"] == 3
            assert "cone" in weekly.weekly_report(st4)
        finally:
            C.RISK_INFO = old
    check(S, "OFF: σ 기록·차트 위험정보·weekly 원뿔 없음", off_identical)

    def chart_and_caption():
        from stocknews import chart as ch
        from stocknews.contracts import ScreenResult
        import dataclasses
        fields = {f.name for f in dataclasses.fields(ScreenResult)}
        from datetime import datetime
        base = dict(ticker="000010", name="가상", price=1000.0,
                    asof=datetime(2026, 1, 1))
        res = ScreenResult(**{k: v for k, v in base.items() if k in fields})
        r = {"sigma": 0.021, "days": 20, "price": 1000.0, "stop": 900.0,
             "stop_pct": 10.0, "prob": 0.234, "lam": 0.94}
        off = ch.chart_caption(res, rank=1)
        on = ch.chart_caption(res, rank=1, risk=r)
        assert "도달 확률" not in off
        assert "20일 내 손절선(-10% · 900원) 도달 확률 23% · σ 2.1%/일" in on, on
        assert off.splitlines() == [x for x in on.splitlines()
                                    if "도달 확률" not in x], "다른 줄이 바뀜"
        idx = [i for i, x in enumerate(on.splitlines()) if "도달 확률" in x][0]
        assert "OBV" in on.splitlines()[idx + 1], "OBV 앞에 있어야"
        # PNG
        n = 140
        idxd = pd.bdate_range("2026-01-01", periods=n)
        cl = 1000 * np.exp(np.cumsum(np.random.default_rng(3).normal(0, 0.02, n)))
        df = pd.DataFrame({"시가": cl, "고가": cl * 1.01, "저가": cl * 0.99,
                           "종가": cl, "거래량": 1e5, "거래대금": cl * 1e5},
                          index=idxd)
        res2 = dataclasses.replace(res, price=float(cl[-1]))
        r2 = dict(r, price=float(cl[-1]), stop=float(cl[-1]) * 0.9)
        p_off = ch.render_chart(res2, df, tmp / "c_off.png")
        p_on = ch.render_chart(res2, df, tmp / "c_on.png", risk=r2)
        assert p_off.stat().st_size > 0 and p_on.stat().st_size > 0
        # σ 없음 → 원뿔 없이 그리고, 캡션은 '계산 불가' 한 줄
        rn = dict(r2, sigma=None, prob=None)
        assert ch.render_chart(res2, df, tmp / "c_none.png", risk=rn).exists()
        cn = ch.chart_caption(res, rank=1, risk=rn)
        assert "도달 확률 - (σ 없음" in cn, cn
    check(S, "캡션 도달 확률 줄 · 원뿔 차트 PNG", chart_and_caption)

    # ── 리밸런싱 ──
    def rebalance_all():
        from stocknews import rebalance as rb
        rcal = _cal(5, "2026-11-02")
        st5 = _store(tmp, "rebal.db",
                     {"005930": [70000.0] * 5, "000660": [100000.0] * 5,
                      "035420": [200000.0] * 5}, rcal,
                     sectors={"005930": "반도체", "000660": "반도체",
                              "035420": "인터넷"})
        hp = tmp / "holdings.csv"
        hp.write_text(
            "# 주석\n종목코드,수량,평단\nA005930,100,65000\n000660,20,90000\n"
            "035420,5,180000\n122630,300,20000\n999990,10,12345\n"
            "CASH,1000000,\n005930,0,0\n", encoding="utf-8")
        hs = rb.read_holdings(hp)
        codes = {h.code: h for h in hs}
        assert set(codes) == {"005930", "000660", "035420", "122630",
                              "999990", "CASH"}, codes
        assert codes["005930"].qty == 100

        def fake(kind, bas):
            assert bas == "20261106", bas
            if kind == "ETF":
                return [{"ISU_CD": "122630", "ISU_NM": "KODEX 레버리지",
                         "TDD_CLSPRC": "21,000"}]
            return []
        val = rb.value_holdings(st5, hs, asof=rcal[-1], fetch=fake,
                                calendar=rcal)
        v = {h.code: h for h in val["holdings"]}
        assert v["005930"].value == 7_000_000 and v["005930"].valued_by == "종가"
        assert v["122630"].kind == "ETF" and v["122630"].leverage
        assert v["999990"].valued_by == "평단(대체)"
        total = 7e6 + 2e6 + 1e6 + 6.3e6 + 123_450 + 1e6
        chk = rb.check_limits(val["holdings"])
        got = {(x["kind"], x["label"]): x for x in chk["violations"]}
        sam = got[("종목", "종목005930(005930)")]
        near(sam["weight_pct"], round(7e6 / total * 100, 2), 1e-9, "비중")
        assert sam["reduce_amount"] == math.ceil(7e6 - 0.10 * total)
        assert ("섹터", "반도체") in got
        assert got[("레버리지", "레버리지·인버스 합계")]["reduce_amount"] == \
            math.ceil(6.3e6 - 0.10 * total)
        assert ("종목", "종목000660(000660)") not in got, "11.6% < 15% 인데 위반"
        assert chk["unclassified"] == 2, chk["unclassified"]
        # 밴드 경계: 비중 = 한도+밴드 정확히 → 위반 아님
        hb = [rb.Holding("005930", 15, kind="주식", name="x", value=15.0),
              rb.Holding("CASH", 85, kind="현금", value=85.0)]
        assert not rb.check_limits(hb)["violations"], "15.0% 는 위반 아님"
        hb[0].value, hb[1].value = 15.01, 84.99
        assert rb.check_limits(hb)["violations"], "15.01% 는 위반"
        txt = rb.render_rebalance(val, chk)
        for bad in ("매수", "매도", "수익", "평가손익", "65,000", "90,000",
                    "180,000", "12,345"):
            assert bad not in txt, f"리포트에 '{bad}'"
        assert "줄일 금액" in txt and "투자 판단 근거 아님" in txt
        sm = rb.summary(val, chk)
        assert "avg" not in str(sm) and "65000" not in str(sm)
    check(S, "리밸런싱: 파싱·평가·위반·줄일 금액·밴드 경계·금지어", rebalance_all)

    def rebalance_files():
        root = Path(__file__).resolve().parent.parent
        gi = (root / ".gitignore").read_text(encoding="utf-8")
        assert "\ndata/*\n" in gi and "!data/*.example" in gi
        ex = root / "data" / "holdings.csv.example"
        assert ex.exists(), "템플릿 없음"
        from stocknews import rebalance as rb
        assert len(rb.read_holdings(ex)) >= 2
    check(S, "holdings.csv 커밋 금지 · 템플릿 존재", rebalance_files)

    def modes():
        import importlib
        from stocknews.joblock import MODE_TIMEOUTS
        mod = importlib.import_module("run_screen")
        assert "rebalance" in mod.MODES and "risk-backfill" in mod.MODES
        assert "rebalance" not in mod._WRITE_MODES, "rebalance 는 읽기 전용"
        assert "risk-backfill" in mod._WRITE_MODES
        assert "risk-backfill" in MODE_TIMEOUTS
        agents = (Path(__file__).resolve().parent.parent / "AGENTS.md"
                  ).read_text(encoding="utf-8")
        assert "risk-backfill" in agents and "rebalance" in agents
        assert C.RISK_INFO is False, "기본값은 OFF"
    check(S, "모드 목록 · 락 · AGENTS.md · 기본 OFF", modes)
