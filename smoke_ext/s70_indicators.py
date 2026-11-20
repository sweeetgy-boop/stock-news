# -*- coding: utf-8 -*-
"""[H] 지표 기록 — 가상 데이터 검사.

값 범위(MFI·백분위 0~100, 비율 지표 음수 없음), 수동 계산 앵커, 미래 데이터
미사용(뒤를 잘라도·바꿔도 앞 값이 같다), ±30% 점프 NULL(정확히 +30% 는
아님), 거래정지 창 NULL, 지수 신선도 → RS NULL, 계산버전 덮어쓰기 금지,
실시간 행을 소급이 덮지 않음, 플래그 OFF 불변.
"""
from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import date, timedelta

import numpy as np
import pandas as pd

S = "v2[H] 지표 기록"


def _cal(n: int, start: str = "2025-01-01") -> list[str]:
    out, cur = [], date.fromisoformat(start)
    while len(out) < n:
        if cur.weekday() < 5:
            out.append(cur.isoformat())
        cur += timedelta(days=1)
    return out


def _walk(rng, n, p0=10000.0, vol=0.02):
    r = rng.normal(0.0005, vol, n)
    r = np.clip(r, -0.25, 0.25)
    c = p0 * np.exp(np.cumsum(r))
    o = c * np.exp(rng.normal(0, 0.005, n))
    h = np.maximum(o, c) * (1 + np.abs(rng.normal(0, 0.01, n)))
    l = np.minimum(o, c) * (1 - np.abs(rng.normal(0, 0.01, n)))
    v = rng.integers(1_000, 1_000_000, n).astype(float)
    return o, h, l, c, v


def _synthetic(n: int = 300, seed: int = 7):
    """종목 9개 · n 거래일. J=+50% 점프(280), L=정확히 +30%(270),
    S=거래정지(285·286 행 없음), N=신규상장(100 부터)."""
    rng = np.random.default_rng(seed)
    cal = _cal(n)
    tick = ["A1", "A2", "A3", "Q1", "Q2", "Q3", "J", "L", "S", "N"]
    mk = {t: ("KOSDAQ" if t.startswith("Q") or t in ("S", "N") else "KOSPI")
          for t in tick}
    rows = []
    for t in tick:
        o, h, l, c, v = _walk(rng, n, p0=float(rng.integers(2000, 90000)))
        if t == "J":
            for a in (o, h, l, c):
                a[280:] *= 1.5
        if t == "L":
            k = 1.3 * c[269] / c[270]
            for a in (o, h, l, c):
                a[270:] *= k             # 270일 종가 = 269일 종가 × 1.3 정확히
            h[270] = max(h[270], c[270])
        for i in range(n):
            if t == "S" and i in (285, 286):
                continue
            if t == "N" and i < 100:
                continue
            rows.append((t, cal[i], o[i], h[i], l[i], c[i], v[i]))
    px = pd.DataFrame(rows, columns=["ticker", "d", "o", "h", "l", "c", "v"])
    idx = {}
    for m, p0 in (("KOSPI", 2500.0), ("KOSDAQ", 800.0)):
        cl = p0 * np.exp(np.cumsum(rng.normal(0.0003, 0.01, n)))
        idx[m] = pd.DataFrame({"Open": cl, "Close": cl}, index=cal)
    return cal, tick, mk, px, idx


def _wide(cal, tick, px):
    out = {"dates": cal, "tickers": tick}
    for col in ("c", "h", "l", "v"):
        m = px.pivot(index="d", columns="ticker", values=col)
        out[col] = m.reindex(index=cal, columns=tick).to_numpy(dtype="float64")
    return out


def _store(tmp, name, cal, tick, mk, px):
    from stocknews.store import Store
    st = Store(tmp / name)
    st.upsert_tickers([{"ticker": t, "name": t, "market": mk[t]} for t in tick])
    with closing(sqlite3.connect(st.path)) as con:
        con.executemany(
            "INSERT INTO prices(ticker,d,o,h,l,c,v,amt) VALUES(?,?,?,?,?,?,?,NULL)",
            [tuple(r) for r in px[["ticker", "d", "o", "h", "l", "c", "v"]]
             .itertuples(index=False)])
        con.commit()
    return st


def run(check, near, tmp):
    from stocknews import config as C
    from stocknews import indicator_log as ind

    cal, tick, mk, px, idx = _synthetic()
    W = _wide(cal, tick, px)
    ic = ind._index_matrix(cal, tick, idx, mk)
    res = ind.compute_wide(W["c"], W["h"], W["l"], W["v"], ic)
    col = {t: j for j, t in enumerate(tick)}
    T = len(cal)

    def ranges():
        # 넓은 무작위 패널에서도 범위를 지킨다
        rng = np.random.default_rng(3)
        cs, hs, ls, vs = [], [], [], []
        for _ in range(60):
            o, h, l, c, v = _walk(rng, 280, vol=0.04)
            cs.append(c); hs.append(h); ls.append(l); vs.append(v)
        big = ind.compute_wide(np.array(cs).T, np.array(hs).T, np.array(ls).T,
                               np.array(vs).T, None)
        for r in (res, big):
            for n in ("mfi14", "rs20_pct", "rs60_pct"):
                a = r[n][np.isfinite(r[n])]
                if r is res or n == "mfi14":
                    assert len(a), n
                if len(a):
                    assert a.min() >= 0.0 and a.max() <= 100.0, (n, a.min(), a.max())
            for n in ("atr14_pct", "bb_width20", "dist_52w_high"):
                a = r[n][np.isfinite(r[n])]
                assert len(a), n
                assert a.min() >= 0.0, (n, a.min())
            d = r["dist_52w_high"]
            assert np.nanmax(d) < 100.0
            p = r["rs20_pct"][-1]
            p = p[np.isfinite(p)]
            if len(p) >= 2:
                assert p.min() == 0.0 and p.max() == 100.0
    check(S, "값 범위: MFI·백분위 0~100 · 비율 지표 음수 없음", ranges)

    def exact_sums():
        # 큰 거래대금 뒤 '하락 없는 14일' → MFI 정확히 100, 평평한 20일 → BB 정확히 0.
        # 누적합 차로 구하면 100.00000000000001 / 1e-9 같은 값이 나온다(사본 실측).
        n = 120
        c = np.r_[np.linspace(1e6, 2e6, 80) * (1 + 0.03 * np.sin(np.arange(80))),
                  np.full(20, 1234567.0), 1234567.0 + np.arange(1, 21) * 1000.0]
        v = np.r_[np.full(80, 9.7e8), np.full(40, 3.3e5)]
        out = ind.compute_wide(c[:, None], c[:, None] * 1.001, c[:, None] * 0.999,
                               v[:, None], None)
        assert out["mfi14"][n - 1, 0] == 100.0, repr(out["mfi14"][n - 1, 0])
        assert out["bb_width20"][99, 0] == 0.0, repr(out["bb_width20"][99, 0])
        assert np.nanmax(out["mfi14"]) <= 100.0 and np.nanmin(out["mfi14"]) >= 0.0
        # 하락일 없는 창 × 무작위 거래량 500종목: 전부 정확히 100 (사본에서
        # 100*P/(P+0) 순서로 계산하면 100.00000000000001 이 나왔다)
        rng = np.random.default_rng(11)
        m = 500
        cc = np.cumprod(1 + rng.uniform(0.001, 0.02, (16, m)), axis=0) * rng.uniform(1e3, 1e6, m)
        vv = rng.uniform(1e3, 1e9, (16, m))
        o2 = ind.compute_wide(cc, cc * 1.001, cc * 0.999, vv, None)
        assert (o2["mfi14"][-1] == 100.0).all(), np.unique(o2["mfi14"][-1])
    check(S, "창 합계는 직접 합산 (MFI 100 초과·BB 음수 잡음 없음)", exact_sums)

    def manual():
        from stocknews.exits import atr
        j, t = col["A1"], T - 1
        c = W["c"][:, j]; h = W["h"][:, j]; l = W["l"][:, j]; v = W["v"][:, j]
        I = idx["KOSPI"]["Close"].to_numpy()
        near(res["rs20"][t, j], (c[t] / c[t - 20] - I[t] / I[t - 20]) * 100, 1e-9, "rs20")
        near(res["rs60"][t, j], (c[t] / c[t - 60] - I[t] / I[t - 60]) * 100, 1e-9, "rs60")
        H = max(h[t - 249:t + 1].max(), c[t - 249:t + 1].max())
        near(res["dist_52w_high"][t, j], (1 - c[t] / H) * 100, 1e-9, "52주")
        tp = (h + l + c) / 3
        pos = neg = 0.0
        for k in range(t - 13, t + 1):
            if tp[k] > tp[k - 1]:
                pos += tp[k] * v[k]
            elif tp[k] < tp[k - 1]:
                neg += tp[k] * v[k]
        near(res["mfi14"][t, j], 100 * pos / (pos + neg), 1e-9, "mfi")
        df = pd.DataFrame({"고가": h[:t + 1], "저가": l[:t + 1], "종가": c[:t + 1]})
        near(res["atr14_pct"][t, j], atr(df, 14) / c[t] * 100, 1e-9, "atr = exits.atr")
        seg = c[t - 19:t + 1]
        near(res["bb_width20"][t, j], 4 * seg.std() / seg.mean() * 100, 1e-9, "bb")
        y = np.log(c[t - 59:t + 1])
        near(res["kalman_slope"][t, j], ind.kalman_slope_last(y) * 100, 1e-9, "kalman")
        # 백분위: 그날 rs20 이 있는 종목 중 순위
        row = res["rs20"][t]
        ok = np.isfinite(row)
        rk = pd.Series(row[ok]).rank().to_numpy()
        exp = (rk[list(np.nonzero(ok)[0]).index(j)] - 1) / (ok.sum() - 1) * 100
        near(res["rs20_pct"][t, j], exp, 1e-9, "rs20_pct")
    check(S, "수동 계산 앵커 (RS·52주·MFI·ATR·BB·칼만·백분위)", manual)

    def kalman():
        w = ind.kalman_weights(60)
        assert abs(w.sum()) < 1e-9, "수준 이동에 기울기가 변하면 안 된다"
        rng = np.random.default_rng(1)
        Y = np.cumsum(rng.normal(0, 0.02, (60, 5)), axis=0)
        assert np.allclose(w @ Y, ind.kalman_slope_last(Y), atol=1e-12)
        drift = ind.kalman_slope_last(np.arange(60) * 0.01)
        assert abs(drift - 0.01) < 0.0002, drift
        assert C.IND_KALMAN_WINDOW == 60 and C.IND_KALMAN_OBS_VAR == 2.5e-4
    check(S, "칼만: 선형 가중 = 필터 직접 실행 · 일정 추세 복원 · 고정 잡음값", kalman)

    def no_future():
        for k in (150, 251, 299):
            Wk = {key: (W[key][:k] if key not in ("dates", "tickers") else W[key])
                  for key in W}
            rk = ind.compute_wide(Wk["c"], Wk["h"], Wk["l"], Wk["v"], ic[:k])
            for n in ind.NAMES:
                assert np.allclose(rk[n], res[n][:k], equal_nan=True, rtol=0,
                                   atol=1e-9), (n, k)
        # 미래를 바꿔도 과거 값은 그대로
        c2, h2, l2 = W["c"].copy(), W["h"].copy(), W["l"].copy()
        for a in (c2, h2, l2):
            a[200:] *= 1.17
        r2 = ind.compute_wide(c2, h2, l2, W["v"], ic)
        for n in ind.NAMES:
            assert np.allclose(r2[n][:200], res[n][:200], equal_nan=True,
                               atol=1e-9), n
    check(S, "미래 데이터 미사용 (잘라도·바꿔도 앞 값 동일)", no_future)

    def jump():
        j = col["J"]
        for n, last_null in (("rs20", 299), ("atr14_pct", 293), ("mfi14", 293),
                             ("bb_width20", 298), ("kalman_slope", 299),
                             ("dist_52w_high", 299)):
            a = res[n][:, j]
            assert np.isfinite(a[279]), (n, "점프 전날은 값")
            assert np.isnan(a[280:last_null + 1]).all(), (n, "점프 창은 NULL")
            if last_null < 299:
                assert np.isfinite(a[last_null + 1]), (n, "창이 지나면 다시 값")
        jl = col["L"]
        assert np.isfinite(res["atr14_pct"][275, jl]), "정확히 +30% 는 상한가 = 점프 아님"
        assert np.isfinite(res["rs20"][275, jl])
    check(S, "±30% 점프 창 NULL · 정확히 +30% 는 NULL 아님", jump)

    def halt():
        j = col["S"]
        assert np.isnan(res["mfi14"][285:287, j]).all(), "행 없는 날은 기록 대상 아님"
        assert np.isnan(res["atr14_pct"][287, j]), "창 안 거래정지 = NULL"
        assert np.isnan(res["rs20"][299, j])          # 285 가 21일 창 안
        assert np.isfinite(res["dist_52w_high"][287, j]), "52주 고가는 있는 행으로"
        n = col["N"]
        assert np.isnan(res["dist_52w_high"][:, n]).all(), "상장 250거래일 미만 NULL"
        assert np.isfinite(res["dist_52w_high"][299, col["A1"]])
        assert np.isnan(res["dist_52w_high"][248, col["A1"]])
        assert np.isfinite(res["dist_52w_high"][249, col["A1"]])
    check(S, "거래정지 창 NULL · 52주는 상장 250거래일부터", halt)

    st = _store(tmp, "h70.db", cal, tick, mk, px)

    def flag_off():
        old = C.INDICATOR_LOG
        try:
            C.INDICATOR_LOG = False
            assert ind.maybe_record_daily(st, cal[-1], index=idx) is None
            assert ind.high52_level(st, "A1", cal[-1]) is None
            from stocknews.chart import _high52_or_none
            assert _high52_or_none(st, "A1", cal[-1]) is None
            with closing(sqlite3.connect(st.path)) as con:
                assert con.execute("SELECT COUNT(*) FROM indicator_log").fetchone()[0] == 0
        finally:
            C.INDICATOR_LOG = old
        assert C.INDICATOR_LOG is False, "기본값은 OFF"
    check(S, "플래그 OFF(기본) = 기록·52주 고가선 없음", flag_off)

    def live_and_backfill():
        old = C.INDICATOR_LOG
        try:
            C.INDICATOR_LOG = True
            out = ind.maybe_record_daily(st, cal[-1], index=idx)
            assert out and out["index_fresh"] == {"KOSPI": True, "KOSDAQ": True}, out
            npres = int(np.isfinite(W["c"][-1]).sum())
            assert out["written"] == npres * 9, out
            vals = ind.read_values(st, cal[-1], "A1")
            for n in ind.NAMES:
                x, y = vals[n], res[n][-1, col["A1"]]
                assert (x is None and np.isnan(y)) or abs(x - y) < 1e-9, (n, x, y)
            # 차트 52주 고가선 = dist 와 같은 고가
            H = ind.high52_level(st, "A1", cal[-1])
            near(W["c"][-1, col["A1"]] / (1 - vals["dist_52w_high"] / 100), H, 1e-6, "H")
            assert ind.high52_level(st, "N", cal[-1]) is None
        finally:
            C.INDICATOR_LOG = old
        rep = ind.backfill(st, index=idx)
        assert rep["written"] == rep["rows"] - npres * 9, rep
        with closing(sqlite3.connect(st.path)) as con:
            kinds = dict(con.execute(
                "SELECT kind, COUNT(*) FROM indicator_log GROUP BY kind").fetchall())
            assert kinds["실시간"] == npres * 9, kinds
            assert set(kinds) == {"실시간", "소급"}
            last = con.execute("SELECT DISTINCT kind FROM indicator_log WHERE d=?",
                               (cal[-1],)).fetchall()
            assert last == [("실시간",)], "소급이 실시간 행을 덮었다"
            cr = dict(con.execute(
                "SELECT kind, SUM(created IS NULL) FROM indicator_log GROUP BY kind").fetchall())
            assert cr == {"실시간": 0, "소급": kinds["소급"]}, cr
            tot = con.execute("SELECT COUNT(*) FROM indicator_log").fetchone()[0]
            assert tot == int(np.isfinite(W["c"]).sum()) * 9
        rep2 = ind.backfill(st, index=idx)
        assert rep2["written"] == 0, "재실행은 아무것도 바꾸지 않는다"
    check(S, "실시간 기록 = 소급 계산값 · 소급이 실시간을 덮지 않음 · 재실행 멱등",
          live_and_backfill)

    def stale_index():
        st2 = _store(tmp, "h70_stale.db", cal, tick, mk, px)
        old = C.INDICATOR_LOG
        try:
            C.INDICATOR_LOG = True
            stale = {m: f.iloc[:-1] for m, f in idx.items()}   # 하루 늦은 지수
            out = ind.maybe_record_daily(st2, cal[-1], index=stale)
            assert out["index_fresh"] == {"KOSPI": False, "KOSDAQ": False}
            v = ind.read_values(st2, cal[-1], "A1")
            assert v["rs20"] is None and v["rs60_pct"] is None
            assert v["mfi14"] is not None and v["kalman_slope"] is not None
            # 미래 날짜가 섞인 지수는 기준일까지만 쓴다
            fut = {m: pd.concat([f, pd.DataFrame({"Open": [1.0], "Close": [1.0]},
                                                index=["2099-01-01"])])
                   for m, f in idx.items()}
            st3 = _store(tmp, "h70_fut.db", cal, tick, mk, px)
            out3 = ind.maybe_record_daily(st3, cal[-1], index=fut)
            assert out3["index_fresh"]["KOSPI"] is True
        finally:
            C.INDICATOR_LOG = old
    check(S, "지수 신선하지 않으면 RS NULL (다른 지표는 기록)", stale_index)

    def versions():
        rows = [("2026-01-02", "X", "mfi14", 10.0, 1, "실시간", "t")]
        assert ind.write_rows(st, rows) == 1
        assert ind.write_rows(st, [("2026-01-02", "X", "mfi14", 99.0, 1, "소급", "t")]) == 0
        assert ind.write_rows(st, [("2026-01-02", "X", "mfi14", 20.0, 2, "소급", "t")]) == 1
        with closing(sqlite3.connect(st.path)) as con:
            got = con.execute("SELECT calc_version, value, kind FROM indicator_log "
                              "WHERE d='2026-01-02' AND ticker='X' ORDER BY 1").fetchall()
        assert got == [(1, 10.0, "실시간"), (2, 20.0, "소급")], got
        assert ind.read_values(st, "2026-01-02", "X") == {"mfi14": 20.0}
        assert set(ind.versions()) == set(ind.NAMES)
        assert ind.NAMES == ("rs20", "rs60", "rs20_pct", "rs60_pct", "dist_52w_high",
                             "mfi14", "atr14_pct", "bb_width20", "kalman_slope")
    check(S, "계산버전: 같은 버전 덮어쓰기 금지 · 새 버전은 별도 행", versions)

    def docs():
        from pathlib import Path
        p = Path(__file__).resolve().parent.parent / "docs" / "지표등록.md"
        text = p.read_text(encoding="utf-8")
        for n in ind.NAMES:
            assert f"`{n}`" in text, f"문서에 {n} 없음"
        assert "이 문서에 없는 지표는 점수에 반영하지 않는다" in text
        assert "소급" in text and "실시간" in text
    check(S, "docs/지표등록.md 에 지표 9개 + 원칙 문구", docs)
