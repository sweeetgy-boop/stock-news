# -*- coding: utf-8 -*-
"""[C] 지수 확정값 (R3-3) — 네트워크 없이 가짜 KRX 로 검사.

직전 5거래일 KRX 공식 값으로 market_context·market_regime 갱신·확정 표시,
당일 잠정 유지(요청조차 하지 않음), 키 없음·호출 실패·빈 응답 생략,
잠정→확정 국면 변화 WARNING, 지수 히스토리 공백이면 생략, record_daily
보충 행의 확정 표시, 소급이 실시간 관측값을 공식 값으로 덮음, 멱등.
"""
from __future__ import annotations

import logging
import os
import sqlite3
from contextlib import closing
from datetime import date, timedelta

import pandas as pd

S = "v2[C] 지수 확정"


def _cal(n: int, start: str = "2026-01-05") -> list[str]:
    out, cur = [], date.fromisoformat(start)
    while len(out) < n:
        if cur.weekday() < 5:
            out.append(cur.isoformat())
        cur += timedelta(days=1)
    return out


class _Cap(logging.Handler):
    def __init__(self):
        super().__init__(logging.WARNING)
        self.msgs: list[str] = []

    def emit(self, rec):
        self.msgs.append(rec.getMessage())


def run(check, near, tmp):
    from stocknews import config as C
    from stocknews import index_confirm as IC
    from stocknews import regime as R
    from stocknews.daily import collect_market_context
    from stocknews.store import Store

    cal = _cal(80)
    ser = {f"T{i:05d}": [(d, 1000.0 + j * (i + 1)) for j, d in enumerate(cal)]
           for i in range(4)}
    idx_dates = sorted({x for x in _cal(300, start="2025-01-01") if x < cal[0]}
                       | set(cal))
    off = {"KOSPI": {d: 1000.0 + i for i, d in enumerate(idx_dates)},
           "KOSDAQ": {d: 100.0 + i * 0.1 for i, d in enumerate(idx_dates)}}
    WRONG = set(cal[70:75])          # 그날 밤 FDR 잠정값이 틀렸던 5일

    def frame(name, upto, wrong=(), stale_after=None):
        o = off[name]
        ds = [d for d in idx_dates if d <= upto
              and (stale_after is None or d <= stale_after)]
        # 잠정값: MA200 아래로 떨어진 값 → 조건 '중립'(비율 100%)
        vals = [(1.0 if name == "KOSPI" else 0.1) if d in wrong else o[d]
                for d in ds]
        return pd.DataFrame({"Close": vals}, index=pd.to_datetime(ds))

    def night_fetch(d):
        # 그날 밤: 그날 값만 잠정(틀렸으면 틀린 값), 지난날은 정정돼 있음
        return lambda name, bars: frame(name, d, wrong={d} & WRONG)

    def store_with_nights(path, until: str, confirm: bool = False,
                          http=None):
        s = Store(path)
        rows = [(t, d, c, c, c, c, 1000.0, c * 1000.0)
                for t, pts in ser.items() for d, c in pts]
        with closing(sqlite3.connect(s.path)) as con:
            con.executemany("INSERT INTO prices(ticker,d,o,h,l,c,v,amt) "
                            "VALUES(?,?,?,?,?,?,?,?)", rows)
            con.commit()
        for d in cal[59:cal.index(until) + 1]:
            f = night_fetch(d)
            collect_market_context(s, d, fetch=f)
            if confirm:
                ic = IC.confirm_recent(s, d, fetch=f, http_get=http,
                                       key="k", min_n=1)
                R.record_daily(s, d, fetch=ic.fetch, min_n=1)
            else:
                R.record_daily(s, d, fetch=f, min_n=1)
        return s

    calls: list[tuple[str, str]] = []

    def fake_http(service, bas_dd, key):
        d = f"{bas_dd[:4]}-{bas_dd[4:6]}-{bas_dd[6:]}"
        calls.append((service, d))
        name, nm = (("KOSPI", "코스피") if service.startswith("kospi")
                    else ("KOSDAQ", "코스닥"))
        v = off[name].get(d)
        if v is None:
            return []
        return [{"IDX_NM": nm + " 200", "CLSPRC_IDX": "9,999.99"},
                {"IDX_NM": nm, "CLSPRC_IDX": f"{v:,.2f}",
                 "OPNPRC_IDX": f"{v:,.2f}"}]

    def mc(s, d):
        with closing(sqlite3.connect(s.path)) as con:
            con.row_factory = sqlite3.Row
            r = con.execute("SELECT * FROM market_context WHERE scan_date=?",
                            (d,)).fetchone()
        return None if r is None else dict(r)

    def rg(s, d):
        return R.get_row(s, d)

    def snap_rows(s):
        df = R.regime_rows(s)
        return df.astype(object).where(pd.notna(df), None).to_dict("records")

    # 앞선 검사가 logging.disable·로거 레벨을 바꿔 둘 수 있다 → 잠시 풀고 복원
    cap = _Cap()
    saved_disable = logging.root.manager.disable
    logging.disable(logging.NOTSET)
    saved_lv = {}
    for nm in ("stocknews.regime", "stocknews.index_confirm"):
        lg = logging.getLogger(nm)
        saved_lv[nm] = lg.level
        lg.setLevel(logging.INFO)
        lg.addHandler(cap)

    # ── 파싱 ──
    def parse():
        vals, rep = IC.fetch_official([cal[10]], key="k", http_get=fake_http)
        assert rep["status"] == "OK" and rep["calls"] == 2
        assert vals[cal[10]]["KOSPI"] == off["KOSPI"][cal[10]], vals
        assert vals[cal[10]]["KOSDAQ"] == round(off["KOSDAQ"][cal[10]], 2)
        # 빈 응답 = EMPTY, 한 지수만 = PARTIAL
        vals, rep = IC.fetch_official(["2030-01-02"], key="k",
                                      http_get=fake_http)
        assert vals == {} and rep["by_date"] == {"2030-01-02": "EMPTY"}

        def half(svc, b, k):
            return fake_http(svc, b, k) if svc.startswith("kospi") else []
        vals, rep = IC.fetch_official([cal[10]], key="k", http_get=half)
        assert vals == {} and rep["by_date"][cal[10]] == "PARTIAL"
        # 호출 실패 1건 = 전부 생략
        n = {"i": 0}

        def boom(svc, b, k):
            n["i"] += 1
            if n["i"] == 3:
                raise RuntimeError("HTTP 500")
            return fake_http(svc, b, k)
        vals, rep = IC.fetch_official(cal[10:13], key="k", http_get=boom)
        assert vals == {} and rep["status"] == "ERROR" and rep["calls"] == 3
    check(S, "KRX 지수 응답 파싱 · EMPTY · PARTIAL · 실패 1건이면 전부 생략", parse)

    # ── 잠정 기록 (기능 이전의 밤들) → 첫 확정 ──
    def confirm5():
        s = store_with_nights(tmp / "c5.db", cal[74])
        before = {d: rg(s, d) for d in cal[70:75]}
        assert all(before[d]["raw_label"] == R.NEUTRAL for d in WRONG), \
            "잠정값으로 판정한 조건 = 중립"
        assert before[cal[74]]["label"] == R.NEUTRAL, "5일 연속 → 잠정 전환"
        assert mc(s, cal[72])["kospi_close"] == 1.0
        with closing(sqlite3.connect(s.path)) as con:
            rr0 = con.execute("SELECT * FROM reco_regime").fetchall()
            recos0 = con.execute("SELECT * FROM recos").fetchall()
        td = cal[75]
        f = night_fetch(td)
        collect_market_context(s, td, fetch=f)
        calls.clear()
        cap.msgs.clear()
        ic = IC.confirm_recent(s, td, fetch=f, http_get=fake_http, key="k",
                               min_n=1)
        sm = ic.summary
        assert sm["status"] == "OK" and sm["targets"] == cal[70:75], sm
        assert sm["confirmed"] == cal[70:75], sm
        assert sm["context_updated"] == 5 and sm["calls"] == 10, sm
        assert td not in {d for _s, d in calls}, "당일은 요청하지 않음"
        for d in cal[70:75]:
            m = mc(s, d)
            assert m["kospi_close"] == off["KOSPI"][d], (d, m)
            near(m["kosdaq_close"], off["KOSDAQ"][d], 0.005, "코스닥 확정")
            assert m["is_final"] == 1 and m["index_source"] == "KRX"
            assert m["confirmed_at"]
            r = rg(s, d)
            assert r["raw_label"] == R.UP and r["label"] == R.UP, (d, r)
            assert r["is_final"] == 1 and r["index_source"] == "KRX"
            assert r["kospi_close"] == off["KOSPI"][d]
        # MA200 = 공식 값 200봉 평균
        ks = [off["KOSPI"][x] for x in idx_dates if x <= cal[72]][-200:]
        near(mc(s, cal[72])["kospi_ma200"], sum(ks) / 200, 1e-6, "MA200 공식")
        near(rg(s, cal[72])["kospi_ma200"], sum(ks) / 200, 1e-6, "국면 MA200")
        ch = sm["regime_changed"]
        assert [c["d"] for c in ch] == cal[70:75], ch
        assert ch[-1]["label"] == [R.NEUTRAL, R.UP], ch[-1]
        assert all(c["raw"] == [R.NEUTRAL, R.UP] for c in ch)
        warn = [m for m in cap.msgs if "잠정→확정" in m]
        assert len(warn) == 5 and cal[74] in warn[-1] and "중립→상승" in warn[-1], warn
        # 당일: 잠정
        m = mc(s, td)
        assert m["is_final"] == 0 and m["index_source"] == "FDR잠정", m
        assert sm["provisional"] == td
        reg = R.record_daily(s, td, fetch=ic.fetch, min_n=1)
        assert reg["label"] == R.UP and reg["filled"] == []
        r = rg(s, td)
        assert r["is_final"] == 0 and r["index_source"] == "FDR잠정", r
        # recos·reco_regime(추천 시점 스냅샷)은 그대로
        with closing(sqlite3.connect(s.path)) as con:
            assert con.execute("SELECT * FROM reco_regime").fetchall() == rr0
            assert con.execute("SELECT * FROM recos").fetchall() == recos0
        # 멱등: 같은 밤 다시 → 쓰기 없음 · 변화 없음
        snap = snap_rows(s)
        ic2 = IC.confirm_recent(s, td, fetch=f, http_get=fake_http, key="k",
                                min_n=1)
        assert ic2.summary["context_updated"] == 0, ic2.summary
        assert ic2.summary["regime_updated"] == 0, ic2.summary
        assert ic2.summary["regime_changed"] == []
        assert ic2.summary["confirmed"] == cal[70:75]
        assert snap_rows(s) == snap
    check(S, "직전 5거래일 확정 · 당일 잠정 · 잠정→확정 국면 WARNING · 멱등",
          confirm5)

    # ── 매일 확정하는 밤들: 틀린 잠정값이 다음 날 바로 바로잡힘 ──
    def nightly_chain():
        s = store_with_nights(tmp / "chain.db", cal[79], confirm=True,
                              http=fake_http)
        for d in cal[60:79]:
            m = mc(s, d)
            assert m["is_final"] == 1 and m["kospi_close"] == off["KOSPI"][d]
            assert rg(s, d)["raw_label"] == R.UP, d
            assert rg(s, d)["is_final"] == 1 and rg(s, d)["index_source"] == "KRX"
            if d >= cal[63]:                 # 첫 확정 = 60봉 + 5일째
                assert rg(s, d)["label"] == R.UP, d
        assert mc(s, cal[79])["is_final"] == 0
        # 잠정값 5일 연속이 있었어도 매일 밤 확정되므로 전환이 생기지 않는다
        tr = R.transitions(R.regime_rows(s))
        assert [t["to"] for t in tr] == [R.UP], tr
    check(S, "매일 확정: 잠정 오류가 다음 날 바로잡혀 가짜 전환 없음",
          nightly_chain)

    # ── 생략 경로 ──
    def skips():
        s = store_with_nights(tmp / "skip.db", cal[74])
        td = cal[75]
        f = night_fetch(td)
        snap = snap_rows(s)
        old = os.environ.pop("KRX_API_KEY", None)
        try:
            cap.msgs.clear()
            ic = IC.confirm_recent(s, td, fetch=f, http_get=fake_http,
                                   min_n=1)
        finally:
            if old is not None:
                os.environ["KRX_API_KEY"] = old
        assert ic.summary["status"] == "NOKEY" and ic.summary["calls"] == 0
        assert any("KRX_API_KEY" in m for m in cap.msgs)
        assert snap_rows(s) == snap
        base = f("KOSPI", 330)
        got = ic.fetch("KOSPI", 330)
        assert "krx_confirmed" not in got.attrs, "확정이 없으면 FDR 그대로"
        pd.testing.assert_frame_equal(got, base)
        assert mc(s, cal[72])["is_final"] is None

        # 호출 실패
        def boom(*_a):
            raise ConnectionError("dns")
        ic = IC.confirm_recent(s, td, fetch=f, http_get=boom, key="k",
                               min_n=1)
        assert ic.summary["status"] == "ERROR" and ic.summary["confirmed"] == []
        assert snap_rows(s) == snap

        # 한 날짜만 빈 응답 → 그날만 잠정 유지
        def holey(svc, b, k):
            return [] if b == cal[73].replace("-", "") else fake_http(svc, b, k)
        ic = IC.confirm_recent(s, td, fetch=f, http_get=holey, key="k",
                               min_n=1)
        assert ic.summary["pending"] == [cal[73]], ic.summary
        assert cal[73] not in ic.summary["confirmed"]
        assert mc(s, cal[73])["is_final"] is None
        assert rg(s, cal[73])["raw_label"] == R.NEUTRAL, "보류일은 그대로"
        assert mc(s, cal[74])["is_final"] == 1

        # 지수 히스토리가 오래 묵음(FDR 캐시 미갱신) → 공백이라 생략
        s2 = store_with_nights(tmp / "stale.db", cal[74])
        stale = (lambda name, bars: frame(name, td, stale_after=cal[64]))
        ic = IC.confirm_recent(s2, td, fetch=stale, http_get=fake_http,
                               key="k", min_n=1)
        assert ic.summary["confirmed"] == [], ic.summary
        assert all("공백" in v for v in ic.summary["skipped"].values())
        # 예외는 삼킨다
        ic = IC.confirm_recent(s2, td, fetch=lambda *_: 1 / 0,
                               http_get=fake_http, key="k", min_n=1)
        assert ic.summary["status"] == "EXCEPTION"
    check(S, "키 없음·호출 실패·빈 응답·히스토리 공백 = 생략 (경고만)", skips)

    # ── record_daily 보충 행도 공식 값이면 확정 표시 ──
    def gapfill_marked():
        s = store_with_nights(tmp / "gap.db", cal[74])
        with closing(sqlite3.connect(s.path)) as con:
            con.execute("DELETE FROM market_regime WHERE d=?", (cal[72],))
            con.commit()
        td = cal[75]
        f = night_fetch(td)
        ic = IC.confirm_recent(s, td, fetch=f, http_get=fake_http, key="k",
                               min_n=1)
        assert cal[72] in ic.summary["confirmed"]
        reg = R.record_daily(s, td, fetch=ic.fetch, min_n=1)
        assert cal[72] in reg["filled"]
        r = rg(s, cal[72])
        assert r["is_final"] == 1 and r["index_source"] == "KRX", r
        assert r["kospi_close"] == off["KOSPI"][cal[72]]
    check(S, "record_daily 보충 행: 공식 값 날짜는 KRX 확정 표시", gapfill_marked)

    # ── 소급: 실시간 관측값을 공식 값으로 ──
    def backfill_official():
        s = store_with_nights(tmp / "bf.db", cal[74])
        cap.msgs.clear()
        full = frame("KOSPI", cal[-1])
        res = R.backfill(s, index=full, min_n=1)
        assert res["ok"] and res["live_recomputed"] >= 16, res
        assert res["kept_live"] == 0 and res["live_changed"] == 5, res
        for d in cal[70:75]:
            r = rg(s, d)
            assert r["kind"] == R.KIND_LIVE, "kind 는 기록 경로라 유지"
            assert r["kospi_close"] == off["KOSPI"][d]
            assert r["raw_label"] == R.UP and r["label"] == R.UP
            assert r["index_source"] == "FDR" and r["is_final"] == 0
        assert [x["d"] for x in res["live_label_changed"]] == [cal[74]]
        assert sum("국면 소급" in m for m in cap.msgs) == 5, cap.msgs
        # market_context 의 KRX 확정 종가가 FDR 보다 우선
        with closing(sqlite3.connect(s.path)) as con:
            con.execute("UPDATE market_context SET kospi_close=?, is_final=1, "
                        "index_source='KRX' WHERE scan_date=?",
                        (off["KOSPI"][cal[72]], cal[72]))
            con.commit()
        bad = frame("KOSPI", cal[-1], wrong={cal[72]})
        res2 = R.backfill(s, index=bad, min_n=1)
        r = rg(s, cal[72])
        assert r["kospi_close"] == off["KOSPI"][cal[72]], r
        assert r["is_final"] == 1 and r["index_source"] == "KRX"
        assert res2["krx_overlay"] == 1
        # 멱등
        snap = R.regime_rows(s).drop(columns=["created", "confirmed_at"])
        R.backfill(s, index=bad, min_n=1)
        snap2 = R.regime_rows(s).drop(columns=["created", "confirmed_at"])
        pd.testing.assert_frame_equal(snap, snap2)
    check(S, "소급: 실시간 관측값을 공식 값으로 갱신 · KRX 확정 우선 · 멱등",
          backfill_official)

    def schema_cols():
        s = Store(tmp / "schema.db")
        with closing(sqlite3.connect(s.path)) as con:
            for t in ("market_context", "market_regime"):
                cols = {r[1] for r in con.execute(f"PRAGMA table_info({t})")}
                assert {"is_final", "index_source", "confirmed_at"} <= cols, t
            rc = [r[1] for r in con.execute("PRAGMA table_info(recos)")]
        assert rc == ["d", "rank", "ticker", "name", "price", "slot", "grade",
                      "value_score", "trend_score", "reason"], rc
        assert C.INDEX_CONFIRM_DAYS == 5
        assert C.INDEX_CONFIRM_API_BASE.endswith("/svc/apis/idx/")
    check(S, "스키마: 확정 표시 컬럼 · recos 불변 · 상수", schema_cols)

    for nm in ("stocknews.regime", "stocknews.index_confirm"):
        logging.getLogger(nm).removeHandler(cap)
        logging.getLogger(nm).setLevel(saved_lv[nm])
    logging.disable(saved_disable)
