# -*- coding: utf-8 -*-
"""[I] 주간 분석 — 합성 데이터로 알려진 파라미터를 복원하는지 검사.

DFA(백색잡음·fGn) · GARCH(1,1) MLE · EWMA · 2상태 국면전환(EM) · 3요인 분해
· 5분위 · 상관 · 허스트 기록(H 스키마 임시 생성) · 플래그 OFF = 지금과 같음
· 끝에서 끝(합성 Store) 리포트.
"""
from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import date, timedelta

import numpy as np
import pandas as pd

S = "v2[I] 주간 분석"

# H 묶음이 만드는 테이블. 여기서는 임시 DB 에만 직접 만든다(병합 시 H 것).
_H_DDL = """CREATE TABLE IF NOT EXISTS indicator_log(
  d TEXT NOT NULL, ticker TEXT NOT NULL, name TEXT NOT NULL, value REAL,
  calc_version INTEGER NOT NULL, kind TEXT NOT NULL, created TEXT,
  PRIMARY KEY(d, ticker, name, calc_version));"""


def _cal(n: int, start: str = "2025-01-02") -> list[str]:
    out, cur = [], date.fromisoformat(start)
    while len(out) < n:
        if cur.weekday() < 5:
            out.append(cur.isoformat())
        cur += timedelta(days=1)
    return out


def _fgn(n: int, H: float, rng) -> np.ndarray:
    """Davies–Harte 로 분수 가우시안 잡음(허스트 H) 생성."""
    k = np.arange(n + 1, dtype="float64")
    g = 0.5 * (np.abs(k - 1) ** (2 * H) - 2 * k ** (2 * H) + (k + 1) ** (2 * H))
    c = np.concatenate([g, g[-2:0:-1]])
    lam = np.clip(np.fft.fft(c).real, 0, None)
    m = len(c)
    w = rng.normal(size=m) + 1j * rng.normal(size=m)
    x = np.fft.fft(np.sqrt(lam / m) * w)
    return x.real[:n]


def _sim_garch(T, om, a, b, rng, N=1):
    r = np.zeros((T, N))
    h = np.full(N, om / (1 - a - b))
    for t in range(T):
        z = rng.normal(size=N)
        r[t] = np.sqrt(h) * z
        h = om + a * r[t] ** 2 + b * h
    return r


def run(check, near, tmp):
    from stocknews import weekly_v2 as W
    from stocknews import config as C
    rng = np.random.default_rng(20261112)

    # ── DFA ──
    def dfa_white():
        x = rng.normal(size=(1000, 30))
        h = W.dfa_hurst(x)
        assert h.shape == (30,)
        med = float(np.median(h))
        assert 0.44 <= med <= 0.56, med
        assert np.isnan(W.dfa_hurst(np.array([1.0, np.nan] * 200)))
    check(S, "DFA 백색잡음 → H≈0.5 (30열 중앙값) · NaN 열은 NaN", dfa_white)

    def dfa_fgn():
        for H in (0.3, 0.8):
            hs = [W.dfa_hurst(_fgn(1000, H, rng)) for _ in range(20)]
            med = float(np.median(hs))
            assert abs(med - H) <= 0.08, (H, med)
        # 250 수익률 창(운영 설정)에서도 순서가 맞아야 한다
        lo = np.median([W.dfa_hurst(_fgn(250, 0.3, rng)) for _ in range(20)])
        hi = np.median([W.dfa_hurst(_fgn(250, 0.8, rng)) for _ in range(20)])
        assert lo + 0.25 < hi, (lo, hi)
    check(S, "DFA fGn H=0.3/0.8 복원 (±0.08) · 250창 순서 보존", dfa_fgn)

    # ── GARCH ──
    def garch_recover():
        r = _sim_garch(3000, 2e-6, 0.08, 0.90, rng, N=12)
        fit = W.garch11_fit(r)
        a, b = float(np.median(fit["alpha"])), float(np.median(fit["beta"]))
        assert abs(a - 0.08) <= 0.025, a
        assert abs(b - 0.90) <= 0.04, b
        # 예측식: 1일 앞 = ω + α r² + β h, 장기 → 표본분산
        f1 = W.garch11_forecast(fit, 1)
        exp1 = np.sqrt(fit["omega"] + fit["alpha"] * fit["r_last"] ** 2
                       + fit["beta"] * fit["h_last"])
        assert np.allclose(f1, exp1)
        flong = W.garch11_forecast(fit, 5000)
        assert np.allclose(flong, np.sqrt(fit["v0"]), rtol=0.05)
    check(S, "GARCH(1,1) α=0.08 β=0.90 복원 (12열 중앙값) · 예측식", garch_recover)

    def ewma():
        x = rng.normal(size=50) * 0.02
        v = (x[:20] ** 2).mean()
        for t in range(50):
            v = 0.94 * v + 0.06 * x[t] ** 2
        near(W.ewma_sigma(x, 0.94)[0], np.sqrt(v), tol=1e-12, label="EWMA")
    check(S, "EWMA σ = RiskMetrics 재귀", ewma)

    # ── 국면전환 ──
    def markov():
        T = 3000
        P = np.array([[0.98, 0.02], [0.05, 0.95]])
        mu, sg = np.array([0.001, -0.002]), np.array([0.008, 0.025])
        s = np.zeros(T, dtype=int)
        for t in range(1, T):
            s[t] = rng.random() < P[s[t - 1], 1]
        x = mu[s] + sg[s] * rng.normal(size=T)
        res = W.markov2_fit(x)
        assert abs(res["sigma"][0] - 0.008) < 0.001, res["sigma"]
        assert abs(res["sigma"][1] - 0.025) < 0.003, res["sigma"]
        assert abs(res["P"][0, 0] - 0.98) < 0.015, res["P"]
        assert abs(res["P"][1, 1] - 0.95) < 0.03, res["P"]
        acc = ((res["smoothed"][:, 1] > 0.5).astype(int) == s).mean()
        assert acc > 0.9, acc
        assert np.allclose(res["filtered"].sum(axis=1), 1.0)
    check(S, "2상태 국면전환 EM: σ·전이확률 복원 · 상태 판별 90%↑", markov)

    # ── 3요인 분해 ──
    from stocknews import events as ev
    cal = _cal(200)

    def decompose():
        n = len(cal)
        F = rng.normal(scale=0.01, size=(n, 3))
        Fo = rng.normal(scale=0.005, size=(n, 3))
        beta = np.array([1.2, -0.5, 0.3])
        y = 0.0005 + F @ beta + rng.normal(scale=0.0005, size=n)
        cl = 100 * np.cumprod(1 + y)
        panel = ev.PricePanel.from_frame(pd.DataFrame(
            {"d": cal, "ticker": "T", "o": cl, "c": cl}), cal)
        fm = W.FactorModel(cal, pd.DataFrame(F, index=cal, columns=["MKT", "SMB", "MOM"])
                           .assign(KOSPI=0.0),
                           pd.DataFrame(Fo, index=cal, columns=["MKT", "SMB", "MOM"]),
                           y.reshape(-1, 1).copy(), pd.Series(1, index=cal))
        fm.r_cc[0] = np.nan
        p = 150
        sc = pd.DataFrame({"ticker": ["T", "T"], "event_date": [cal[p], cal[30]],
                           "event_slot": ["VALUE", "VALUE"], "net_20": [3.0, 1.0],
                           "ex_med_20": [1.0, 0.5], "regime": [None, None]})
        dec = W.factor_decompose(sc, panel, fm, 20, beta_window=120, min_obs=60)
        r0 = dec.iloc[0]
        assert bool(r0["beta_ok"])
        for k, f in enumerate(W.FACTORS):
            near(r0[f"b_{f}"], beta[k], tol=0.02, label=f"β {f}")
        Fw = Fo[p + 1] + F[p + 2:p + 21].sum(axis=0)
        exp = np.array([r0[f"b_{f}"] for f in W.FACTORS]) * Fw * 100
        near(r0["factor_part"], exp.sum(), tol=1e-9, label="요인 몫")
        near(r0["factor_part"] + r0["residual"], 3.0, tol=1e-9, label="합")
        # 이벤트 이전 관측이 min_obs 미만이면 분해하지 않는다 (NaN, 0 아님)
        assert not bool(dec.iloc[1]["beta_ok"]) and np.isnan(dec.iloc[1]["residual"])
    check(S, "팩터 분해: β 복원 · 요인 몫=β·F(진입일 시가→종가 + 이후 종가) · 합=순수익",
          decompose)

    def factor_build():
        # 소형 3종목 +2%, 대형 3종목 −1% 인 날 → SMB=+3%p, 동일가중 MKT=+0.5%
        n = 80
        tick = [f"S{i}" for i in range(3)] + [f"B{i}" for i in range(3)] + \
               [f"M{i}" for i in range(24)]
        rows = []
        for j, t in enumerate(tick):
            px = 100.0
            for i, d in enumerate(cal[:n]):
                if i == n - 1:
                    px *= 1.02 if t[0] == "S" else (0.99 if t[0] == "B" else 1.0)
                rows.append((d, t, px, px))
        panel = ev.PricePanel.from_frame(pd.DataFrame(rows, columns=["d", "ticker", "o", "c"]),
                                         cal[:n])
        sh = {t: (1.0 if t[0] == "S" else 1000.0 if t[0] == "B" else 10.0 + k)
              for k, t in enumerate(tick)}
        fm = W.build_factors(panel, sh, {t: "KOSPI" for t in tick}, q=0.1)
        last = fm.cc.iloc[-1]
        near(last["SMB"], 0.03, tol=1e-9, label="SMB")
        near(last["MKT"], (3 * 0.02 - 3 * 0.01) / 30, tol=1e-9, label="MKT 동일가중")
        fm2 = W.build_factors(panel, sh, {t: "KOSPI" for t in tick}, q=0.1,
                              mkt_weight="cap")
        assert fm2.cc.iloc[-1]["MKT"] < 0      # 시총가중이면 대형 −1% 가 지배
        assert C.WEEKLY_V2_MKT_WEIGHT == "equal"
    check(S, "요인 구성: SMB 부호·크기 · 시장 동일가중(기본)/시총가중", factor_build)

    # ── 5분위 · 상관 ──
    def quint():
        rows, fw = [], []
        for d in cal[:4]:
            for i in range(100):
                t = f"T{i:03d}"
                rows.append((d, t, "x", float(i), "소급"))
                rows.append((d, t, "x", float(i), "실시간") if d == cal[3] else
                            (d, t, "y", float(-i), "소급"))
                fw.append((d, t, float(i) / 10))
        vals = pd.DataFrame(rows, columns=["d", "ticker", "name", "value", "kind"])
        fwd = pd.DataFrame(fw, columns=["d", "ticker", "fwd"])
        reg = {cal[0]: "상승", cal[1]: "상승", cal[2]: "하락", cal[3]: "하락"}
        qt = W.quintile_table(vals, fwd, reg, min_tickers=50)
        a = qt[(qt.name == "x") & (qt.kind == "소급") & (qt.regime == W.ALL)].set_index("q")
        assert list(a.index) == [1, 2, 3, 4, 5]
        assert int(a.loc[1, "n"]) == 20 * 4 and a.loc[5, "mean"] > a.loc[1, "mean"]
        near(a.loc[1, "mean"], 0.95, tol=1e-9, label="Q1 평균")
        y = qt[(qt.name == "y") & (qt.regime == W.ALL)].set_index("q")
        assert y.loc[1, "mean"] > y.loc[5, "mean"]
        live = qt[(qt.name == "x") & (qt.kind == "실시간")]
        assert set(live.regime) == {"하락", W.ALL} and int(live[live.q == 1].n.iloc[0]) == 20
        # 종목 수가 min_tickers 미만인 날은 뺀다
        assert W.quintile_table(vals, fwd, reg, min_tickers=101).empty
        # 표 칸: 20건 미만 '표본 부족'
        rep = {"horizon": 20, "quintile": {"table": live.assign(n=5), "dates": [cal[3]]},
               "min_n": C.MIN_SCORED_FOR_JUDGMENT}
        txt = "\n".join(W._quint_lines(rep))
        assert "표본 부족 (n=5)" in txt and "+0." not in txt.split("Q1")[1][:12]
    check(S, "5분위: 날짜별 분위 · 국면/소급·실시간 분리 · 표본 부족 표기", quint)

    def corr():
        rows = []
        for d in cal[:3]:
            z = rng.normal(size=200)
            u = rng.normal(size=200)
            for i in range(200):
                t = f"T{i}"
                rows += [(d, t, "a", z[i], "소급"), (d, t, "b", z[i] ** 3, "소급"),
                         (d, t, "c", u[i], "소급")]
        vals = pd.DataFrame(rows, columns=["d", "ticker", "name", "value", "kind"])
        m, cnt = W.correlation_table(vals)
        near(m.at["a", "b"], 1.0, tol=1e-9, label="단조 변환 순위상관")
        assert abs(m.at["a", "c"]) < 0.2 and int(cnt.at["a", "b"]) == 3
        rep = {"corr": {"matrix": m, "count": cnt, "dates": 3, "flag": 0.8}}
        txt = "\n".join(W._corr_lines(rep))
        assert "a~b +1.00" in txt and "a~c" not in txt
    check(S, "상관: 날짜별 스피어만 평균 · |ρ|≥0.8 쌍 표시", corr)

    # ── 기록 · 플래그 · 끝에서 끝 ──
    from stocknews.store import Store

    def _mk_store(name, n_days=300, n_tick=60, with_h=False):
        st = Store(tmp / name)
        days = _cal(n_days, "2025-01-02")
        rows, tk = [], []
        for j in range(n_tick):
            t = f"{j:06d}"
            mkt = "KOSPI" if j % 2 else "KOSDAQ"
            tk.append({"ticker": t, "name": f"N{j}", "market": mkt,
                       "shares": 1e6 * (j + 1), "market_cap": None})
            px = 1000.0 + 10 * j
            start = 120 if j < 3 else 0          # 3종목은 상장이 늦다 → 허스트 NULL
            for i, d in enumerate(days):
                px *= 1 + rng.normal(scale=0.02)
                if i >= start:
                    rows.append((t, d, px, px * 1.01, px * 0.99, px, 1e5, px * 1e5))
        st.upsert_tickers(tk)
        with closing(sqlite3.connect(st.path)) as con:
            con.executemany("INSERT INTO prices(ticker,d,o,h,l,c,v,amt) "
                            "VALUES(?,?,?,?,?,?,?,?)", rows)
            for k, d in enumerate(days[150:260:5]):
                con.execute("INSERT INTO recos(d,rank,ticker,name,price,slot,grade,"
                            "value_score,trend_score,reason) VALUES(?,?,?,?,?,?,?,?,?,?)",
                            (d, 1, f"{(k * 7) % 60:06d}", "x", 1.0,
                             ("VALUE", "TREND")[k % 2], "A", 8.0, 1.0, ""))
            if with_h:
                con.executescript(_H_DDL)
            con.commit()
        return st, days

    def record():
        st, days = _mk_store("rec_noh.db")
        vals = pd.Series({"000001": 0.5, "000002": np.nan})
        r = W.record_hurst(st, days[-1], vals, "실시간", replace=True)
        assert r["written"] == 0 and "없음" in r["reason"]
        st2, days2 = _mk_store("rec_h.db", with_h=True)
        r = W.record_hurst(st2, days2[-1], vals, "실시간", replace=True)
        assert r["written"] == 2
        with closing(sqlite3.connect(st2.path)) as con:
            got = con.execute("SELECT ticker, value, calc_version, kind FROM "
                              "indicator_log ORDER BY ticker").fetchall()
        assert got == [("000001", 0.5, 1, "실시간"), ("000002", None, 1, "실시간")], got
        # 소급은 덮어쓰지 않는다 (실시간 기록 보존)
        W.record_hurst(st2, days2[-1], pd.Series({"000001": 0.9}), "소급", replace=False)
        with closing(sqlite3.connect(st2.path)) as con:
            v = con.execute("SELECT value, kind FROM indicator_log WHERE "
                            "ticker='000001'").fetchone()
        assert v == (0.5, "실시간"), v
        bf = W.hurst_backfill(st2)
        assert bf["dates"] > 0 and bf["written"] > 0 and "reason" not in bf
        with closing(sqlite3.connect(st2.path)) as con:
            kinds = dict(con.execute("SELECT kind, count(*) FROM indicator_log "
                                     "GROUP BY kind").fetchall())
            nulls = con.execute("SELECT count(*) FROM indicator_log WHERE "
                                "value IS NULL AND ticker IN ('000000','000001',"
                                "'000002')").fetchone()[0]
        assert kinds.get("소급", 0) > 0 and nulls > 0, (kinds, nulls)
        assert W.hurst_backfill(st)["reason"] == "indicator_log 없음"
    check(S, "허스트 기록: 테이블 없으면 0행 · H 스키마에 NULL·kind · 소급은 덮어쓰지 않음",
          record)

    def flag_off():
        import importlib
        rs = importlib.import_module("run_screen")
        assert C.WEEKLY_ANALYSIS_V2 is False
        assert "hurst-backfill" in rs._WRITE_MODES and "weekly" not in rs._WRITE_MODES

        class A:
            weekly_v2 = False
            dry_run = True
            no_lock = True
        before = dict(rs.SUMMARY)
        rs._weekly_v2(None, A())             # store 를 건드리기 전에 끝나야 한다
        assert rs.SUMMARY == before
        # nightly 단계에 들어가지 않았는지
        from pathlib import Path
        root = Path(__file__).resolve().parents[1]
        for f in ("hermes/nightly.py", "nightly.py"):
            p = root / f
            if p.exists():
                assert "hurst" not in p.read_text(encoding="utf-8")
                assert "weekly_v2" not in p.read_text(encoding="utf-8")
    check(S, "플래그 OFF = weekly 그대로 · nightly 에 없음 · hurst-backfill 은 쓰기 모드",
          flag_off)

    def end_to_end():
        st, days = _mk_store("e2e.db", with_h=True)
        # H 지표 2개(하나는 다른 하나의 단조 변환)와 E 의 σ 를 표본일에 심는다.
        # 테이블은 H·E 스키마 그대로 임시 DB 에만 만든다(병합 시 그쪽 DDL).
        samp = W.weekly_sample_dates(days)
        with closing(sqlite3.connect(st.path)) as con:
            con.execute("CREATE TABLE risk_metrics(d TEXT, ticker TEXT, sigma_ewma REAL,"
                        " lam REAL, calc_version INTEGER, kind TEXT, created TEXT,"
                        " PRIMARY KEY(d, ticker, calc_version))")
            rows_h, rows_e = [], []
            for d in samp[-30:]:
                for j in range(60):
                    v = float(rng.normal())
                    rows_h += [(d, f"{j:06d}", "rs20", v, 1, "소급"),
                               (d, f"{j:06d}", "rs20_pct", v ** 3, 1, "소급")]
            for d in days[-100:]:
                for j in range(60):
                    rows_e.append((d, f"{j:06d}", 0.02 + 0.001 * j, 0.94, 1, "소급"))
            con.executemany("INSERT INTO indicator_log(d,ticker,name,value,calc_version,"
                            "kind) VALUES(?,?,?,?,?,?)", rows_h)
            con.executemany("INSERT INTO risk_metrics(d,ticker,sigma_ewma,lam,"
                            "calc_version,kind) VALUES(?,?,?,?,?,?)", rows_e)
            con.commit()
        text, summ = W.run_weekly_v2(st, out_dir=tmp / "rep",
                                     lock_dir=tmp / "locks")
        for sec in ("■ 1.", "■ 2.", "■ 3.", "■ 4.", "■ 5.", "■ 6.", "■ 7."):
            assert sec in text, sec
        assert "검증 중 · 투자 판단 근거 아님" in text
        assert "매수" not in text and "매도" not in text
        assert "국면 없음" in text                 # C 미병합: regime_on → None
        assert "rs20~rs20_pct +1.00" in text       # H 지표 상관 · 0.8 이상 표시
        assert "sigma" in text.split("■ 3.")[1].split("■ 4.")[0]   # E 의 σ 포함
        assert "rs20 [소급]" in text
        e_line = [x for x in text.splitlines() if "EWMA (E 기록값)" in x][0]
        assert "n=" in e_line and "데이터 없음" not in e_line, e_line
        assert "EWMA (E 기록값 아님)" in text
        assert summ["hurst_written"] == 60, summ  # 60종목 (3종목은 NULL 행)
        assert (tmp / "rep").glob("weekly_v2_*.txt")
        with closing(sqlite3.connect(st.path)) as con:
            n = con.execute("SELECT count(*), sum(value IS NULL) FROM indicator_log "
                            "WHERE kind='실시간' AND name='hurst_dfa'").fetchone()
        assert n == (60, 3), n
        # 락 점유 중이면 기록을 건너뛰고 리포트는 낸다
        from stocknews.joblock import JobLock
        held = JobLock(mode="daily", lock_dir=tmp / "locks")
        assert held.acquire()
        try:
            rep = W.build_report(st, lock_dir=tmp / "locks")
        finally:
            held.release()
        assert "락 점유" in rep["hurst"]["record"]["reason"]
    check(S, "끝에서 끝: 합성 Store → 7개 절 · 허스트 실시간 기록 · 락 점유 시 기록만 생략",
          end_to_end)
