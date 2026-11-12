# -*- coding: utf-8 -*-
"""[C] 국면 라벨 — 가상 데이터 검사.

3단계 판정 경계(50% / 40% / 종가=MA200), 5거래일 연속 전환(초기 상태 ·
끼어든 다른 값 · NULL 날), MA60 위 비율 정의(60봉 미만 제외 · 종가=평균은
'위' 아님), 지수 신선하지 않으면 NULL·"지수 데이터 없음", 배너 OFF 시
텍스트 불변, recos 기존 컬럼·값 불변, 훅(regime_on/regime_row), 소급이
지수 히스토리에 그날이 없는 '실시간' 행을 덮어쓰지 않음(그날이 있으면
공식 값으로 다시 계산 — R3-3, smoke_ext/s31_index_confirm.py).
"""
from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import date, datetime, timedelta

import pandas as pd

S = "v2[C] 국면"


def _cal(n: int, start: str = "2026-01-05") -> list[str]:
    out, cur = [], date.fromisoformat(start)
    while len(out) < n:
        if cur.weekday() < 5:
            out.append(cur.isoformat())
        cur += timedelta(days=1)
    return out


def _store(path, series: dict[str, list[tuple[str, float]]]):
    from stocknews.store import Store
    s = Store(path)
    rows = [(t, d, c, c, c, c, 1000.0, c * 1000.0)
            for t, pts in series.items() for d, c in pts]
    with closing(sqlite3.connect(s.path)) as con:
        con.executemany("INSERT INTO prices(ticker,d,o,h,l,c,v,amt) "
                        "VALUES(?,?,?,?,?,?,?,?)", rows)
        con.commit()
    return s


def _index(dates: list[str], start: float = 1000.0, step: float = 1.0):
    return pd.DataFrame({"Close": [start + i * step for i in range(len(dates))]},
                        index=pd.to_datetime(dates))


def run(check, near, tmp):
    from stocknews import config as C
    from stocknews import regime as R
    from stocknews import v2_hooks as H

    # ── 3단계 판정 경계 ──
    def boundaries():
        f = R.classify
        assert f(101, 100, 50.0) == R.UP, "MA200 위 + 50% = 상승 (≥)"
        assert f(101, 100, 49.99) == R.NEUTRAL, "MA200 위 + 49.99% = 중립"
        assert f(99, 100, 39.99) == R.DOWN, "MA200 아래 + 39.99% = 하락"
        assert f(99, 100, 40.0) == R.NEUTRAL, "MA200 아래 + 40% = 중립 (<)"
        assert f(100, 100, 90.0) == R.NEUTRAL, "종가 = MA200 은 상승 아님"
        assert f(100, 100, 10.0) == R.NEUTRAL, "종가 = MA200 은 하락 아님"
        assert f(101, 100, 30.0) == R.NEUTRAL, "지수 위·비율 낮음 = 중립"
        assert f(99, 100, 70.0) == R.NEUTRAL, "지수 아래·비율 높음 = 중립"
        assert f(None, 100, 50.0) is None and f(101, None, 50) is None
        assert f(101, 100, None) is None and f(101, 100, float("nan")) is None
        # 비율은 100*a/n 로 계산 → 정확히 50.0 / 40.0 이 경계에 닿는다
        assert 100.0 * 1000 / 2000 == 50.0 and 100.0 * 800 / 2000 == 40.0
    check(S, "3단계 판정 경계 50% / 40% / 종가=MA200", boundaries)

    # ── 5거래일 연속 전환 ──
    U, N, D = R.UP, R.NEUTRAL, R.DOWN

    def lab(raw, state=None):
        return [x["label"] for x in R.confirm(raw, days=5, state=state)]

    def initial():
        assert lab([U] * 4) == [None] * 4, "첫 확정도 5일 필요"
        assert lab([U] * 5) == [None] * 4 + [U], "5번째 날 확정"
        assert lab([U, U, N, U, U, U, U]) == [None] * 7, "끼어든 값이 연속을 끊음"
        assert lab([U, U, N, U, U, U, U, U])[-1] == U
    check(S, "초기 상태 NULL · 첫 확정도 5일 연속", initial)

    def switch():
        got = lab([D] * 4 + [N] + [D] * 4, state=U)
        assert got == [U] * 9, got
        got = lab([D] * 5, state=U)
        assert got == [U] * 4 + [D], "5번째 연속일에 전환"
        st = R.confirm([D, D, N], days=5, state=U)
        assert st[-1]["pending_label"] == N and st[-1]["pending_days"] == 1
        assert lab([D, D, U, D, D, D, D], state=U) == [U] * 7, \
            "확정 국면과 같은 raw 가 끼면 대기 0"
        # 하락 확정 후 상승 4일 + 중립 → 그대로 하락
        assert lab([U] * 4 + [N], state=D) == [D] * 5
    check(S, "전환은 새 조건 5거래일 연속일 때만", switch)

    def null_days():
        got = R.confirm([D, D, D, None, D, D, D, D, D], days=5, state=U)
        labels = [x["label"] for x in got]
        assert labels[3] is None, "판정 불가 날은 NULL"
        assert got[3]["state"] == U, "확정 국면은 NULL 날을 건너 유지"
        assert labels[:3] == [U] * 3 and labels[4:8] == [U] * 4, labels
        assert labels[8] == D, "NULL 날이 연속을 끊음 → 그 뒤 5일째 전환"
        assert lab([U, None, U, U, U, U]) == [None] * 6, "초기 확정도 끊김"
    check(S, "NULL 날 = 연속 카운트 리셋 · 확정 국면 유지", null_days)

    # ── MA60 위 비율 정의 ──
    def breadth():
        cal = _cal(61)
        ser = {
            "AAAAAA": [(d, 100.0) for d in cal[:59]] + [(cal[59], 110.0)],
            "BBBBBB": [(d, 100.0) for d in cal[:60]],          # 종가 = 평균
            "CCCCCC": [(d, 100.0) for d in cal[1:60]],         # 59봉 → 제외
            "DDDDDD": [(d, 200.0 - i) for i, d in enumerate(cal[:60])],
        }
        s = _store(tmp / "breadth.db", ser)
        br = R.breadth_by_date(s, [cal[59], cal[58]])
        pct, a, n = br[cal[59]]
        assert (a, n) == (1, 3), (a, n)
        near(pct, 100.0 / 3, 1e-9, "MA60 위 비율")
        assert br[cal[58]] == (None, 0, 0), "아무도 60봉 미만 → 판정 불가"
        # since 창이 60봉을 덮으면 결과 동일
        assert R.breadth_by_date(s, [cal[59]], since=cal[0])[cal[59]] == (pct, a, n)
    check(S, "MA60 위 비율: 60봉 미만 제외 · 종가=평균은 위 아님", breadth)

    def tail_equals_full():
        import numpy as np
        rng = np.random.default_rng(7)
        cal = _cal(160)
        ser = {}
        for i in range(12):
            keep = rng.random(len(cal)) > (0.6 if i < 3 else 0.05)  # 0~2: 장기 정지 잦음
            px = np.cumsum(rng.integers(-30, 31, len(cal))) + 5000
            ser[f"E{i:05d}"] = [(d, float(p)) for d, p, k in zip(cal, px, keep) if k]
        # 부분 적재일(한 종목만 있는 날)도 대상 구간 안에 섞는다
        ser["E00000"].append(("2026-08-08", 4999.0))       # 토요일 = 달력 밖
        s = _store(tmp / "tail.db", ser)
        tgt = cal[-11:]
        a = R.breadth_by_date(s, tgt)
        b = R.breadth_by_date(s, tgt, tail_only=True)
        assert a == b, (a, b)
        assert any(v[2] for v in a.values())
    check(S, "MA60 비율: 종목별 꼬리 읽기 = 전 기간 계산", tail_equals_full)

    # 실시간 기록용 가상 시장: 4종목 · 80거래일, 전부 우상향
    cal = _cal(80)
    ser = {f"T{i:05d}": [(d, 1000.0 + j * (i + 1)) for j, d in enumerate(cal)]
           for i in range(4)}
    idx_dates = _cal(300, start="2025-01-01")
    idx_dates = sorted(set(x for x in idx_dates if x < cal[0]) | set(cal))
    idx_full = _index(idx_dates)

    def fetch_full(_name, _bars):
        return idx_full

    def fetch_stale(_name, _bars):
        return idx_full.iloc[:-1]                 # 기준일 행 없음

    def stale():
        s = _store(tmp / "stale.db", ser)
        row = R.record_daily(s, cal[-1], fetch=fetch_stale, min_n=1)
        assert row is not None
        assert row["fresh"] == 0 and row["label"] is None
        assert row["kospi_close"] is None and row["kospi_ma200"] is None, \
            "묵은 지수 값을 그날 값으로 적지 않음"
        assert row["null_reason"] == R.R_STALE
        assert row["above_ma60_pct"] is not None, "비율은 DB 로 계산됨"
        assert H.regime_on(s, cal[-1]) is None
        r = H.regime_row(s, cal[-1])
        assert r is not None and r["fresh"] is False
        assert "지수 데이터 없음" in R.banner_line(r)
        # 보충된 앞 날짜들은 지수에 행이 있으므로 fresh
        assert H.regime_row(s, cal[-2])["fresh"] is True
    check(S, "지수 신선하지 않음 → 국면 NULL · '지수 데이터 없음'", stale)

    def live_and_hooks():
        s = _store(tmp / "live.db", ser)
        with closing(sqlite3.connect(s.path)) as con:
            con.executemany(
                "INSERT INTO recos(d,rank,ticker,name,price,slot,grade,"
                "value_score,trend_score,reason) VALUES(?,?,?,?,?,?,?,?,?,?)",
                [(cal[-1], 1, "T00000", "가", 1079.0, "VALUE", "A", 7.1, 3.2, "r1"),
                 (cal[-1], 2, "T00001", "나", 1158.0, "TREND", "B", 2.0, 6.5, "r2"),
                 (cal[-2], 1, "T00002", "다", 1234.0, "SEQ", "S+", 8.0, 8.0, "r3")])
            con.commit()
            before = con.execute("SELECT * FROM recos ORDER BY d,rank").fetchall()
            cols = [r[1] for r in con.execute("PRAGMA table_info(recos)")]
        assert cols == ["d", "rank", "ticker", "name", "price", "slot",
                        "grade", "value_score", "trend_score", "reason"], cols
        row = R.record_daily(s, cal[-1], fetch=fetch_full, min_n=1)
        assert row["fresh"] == 1 and row["raw_label"] == R.UP
        assert len(row["filled"]) == C.REGIME_GAP_FILL_DAYS, row["filled"]
        assert row["label"] == R.UP, "보충 10일 + 당일 → 5일째에 확정"
        with closing(sqlite3.connect(s.path)) as con:
            after = con.execute("SELECT * FROM recos ORDER BY d,rank").fetchall()
            cols2 = [r[1] for r in con.execute("PRAGMA table_info(recos)")]
            rr = con.execute("SELECT d,rank,ticker,regime FROM reco_regime "
                             "ORDER BY d,rank").fetchall()
        assert before == after, "recos 값이 바뀜"
        assert cols2 == cols, "recos 컬럼이 바뀜"
        assert rr == [(cal[-1], 1, "T00000", R.UP), (cal[-1], 2, "T00001", R.UP)], rr
        # 훅: 과거 날짜도 조회
        assert H.regime_on(s, cal[-1]) == R.UP
        first = row["filled"][0]
        assert H.regime_on(s, first) is None, "보충 첫날은 확정 전"
        assert H.regime_row(s, first)["null_reason"] == R.R_UNCONFIRMED
        assert H.regime_on(s, row["filled"][4]) == R.UP
        r = H.regime_row(s, cal[-1])
        for k in ("d", "label", "kospi_close", "kospi_ma200", "ma200_gap_pct",
                  "kospi_ret_20d", "above_ma60_pct", "fresh"):
            assert k in r, k
        assert r["fresh"] is True and r["above_ma60_pct"] == 100.0
        near(r["ma200_gap_pct"], (r["kospi_close"] / r["kospi_ma200"] - 1) * 100,
             1e-9, "MA200 괴리")
        assert H.regime_on(s, "1999-01-04") is None
        # 다시 돌려도 같은 값 (같은 날 재실행)
        again = R.record_daily(s, cal[-1], fetch=fetch_full, min_n=1)
        assert again["label"] == R.UP and again["filled"] == []
        # 실패는 삼킨다
        def boom(*_a):
            raise RuntimeError("network down")
        assert R.record_daily(s, cal[-1], fetch=boom, min_n=1) is None
    check(S, "실시간 기록 · reco_regime · recos 기존 컬럼·값 불변 · 훅", live_and_hooks)

    def backfill_keeps_live():
        s = _store(tmp / "bf.db", ser)
        # 그날 밤 지수가 묵어 있었던 '실시간' 기록
        R.record_daily(s, cal[70], fetch=lambda *_: idx_full[idx_full.index
                       < pd.Timestamp(cal[70])], min_n=1)
        # R3-3: 소급은 지수에 그날이 있으면 실시간 행도 공식 값으로 다시
        # 계산한다. 여기서는 '그날이 없는' 경로(관측값 유지)를 본다.
        idx_no70 = idx_full[idx_full.index != pd.Timestamp(cal[70])]
        res = R.backfill(s, index=idx_no70, min_n=1)
        assert res["ok"] and res["kept_live"] >= 1
        r70 = H.regime_row(s, cal[70])
        assert r70["kind"] == R.KIND_LIVE and r70["fresh"] is False
        assert r70["null_reason"] == R.R_STALE
        # 실시간 기록은 cal[60] 부터 상태 기계 → cal[64] 확정이었다.
        # 소급 뒤에는 전 기간 기준(cal[59] 부터)으로 cal[63] 확정.
        assert H.regime_row(s, cal[63])["kind"] == R.KIND_LIVE
        df = R.regime_rows(s)
        assert len(df) == len(cal)
        lab_ = dict(zip(df["d"], df["label"]))
        # 60봉 전까지는 비율 판정 불가 → NULL
        assert all(pd.isna(lab_[d]) for d in cal[:59])
        # cal[59] 부터 5일 연속 상승 → cal[63] 확정
        assert pd.isna(lab_[cal[62]]) and lab_[cal[63]] == R.UP
        # cal[70] NULL 은 연속을 끊지만 확정 국면은 유지 → cal[71] 상승
        assert pd.isna(lab_[cal[70]]) and lab_[cal[71]] == R.UP
        tr = R.transitions(df)
        assert tr == [{"d": cal[63], "from": None, "to": R.UP}], tr
        res2 = R.backfill(s, index=idx_no70, min_n=1)
        df2 = R.regime_rows(s)
        assert list(df2["label"].fillna("-")) == list(df["label"].fillna("-")), \
            "소급 재실행 멱등"
        assert res2["rows"] == res["rows"]
    check(S, "소급: 전 기간 · 60봉 전 NULL · 실시간 관측값 보존 · 멱등", backfill_keeps_live)

    # ── 배너 ──
    def banner_text():
        row = {"d": "2026-11-11", "label": R.DOWN, "fresh": True,
               "kospi_close": 2450.123, "ma200_gap_pct": -3.44,
               "kospi_ret_20d": -5.21}
        b = R.banner_line(row)
        # 하락은 수치만 (해석 문구 금지 · 2026-11-12)
        assert "\n" not in b and "하락" in b and "추천 성과 저조" not in b and " — " not in b
        assert "2,450.12" in b and "-3.4%" in b and "-5.2%" in b
        b = R.banner_line(dict(row, label=R.UP))
        assert "상승 구간 — 절대수익보다 시장 대비 초과수익으로 볼 것" in b
        b = R.banner_line(dict(row, label=R.NEUTRAL))
        assert "중립" in b and "—" not in b, "중립은 수치만"
        for lb in (R.UP, R.NEUTRAL, R.DOWN, None):
            t = R.banner_line(dict(row, label=lb))
            assert "매수" not in t and "매도" not in t
        assert "지수 데이터 없음" in R.banner_line(dict(row, fresh=False))
    check(S, "배너 문구 (하락·상승·중립·지수 없음)", banner_text)

    def banner_flag():
        s = _store(tmp / "banner.db", ser)
        R.record_daily(s, cal[-1], fetch=fetch_full, min_n=1)
        txt = "🌙 <b>[저녁 뉴스 정리]</b> 2026-11-11 23:00\n• 본문"
        old = C.REGIME_BANNER
        try:
            C.REGIME_BANNER = False
            assert R.with_banner(s, txt) is txt, "OFF 면 같은 객체 그대로"
            C.REGIME_BANNER = True
            on = R.with_banner(s, txt)
            first, rest = on.split("\n", 1)
            assert rest == txt and first.startswith("📊 국면 상승"), first
        finally:
            C.REGIME_BANNER = old
    check(S, "배너 OFF = 텍스트 불변 · ON = 맨 위 한 줄", banner_flag)

    def brief_modes_off():
        """run_screen 의 브리핑 두 모드가 OFF 일 때 렌더러 출력 그대로 보낸다."""
        import run_screen as RS
        from stocknews.renderer import render_evening_brief, render_morning_brief
        s = _store(tmp / "brief.db", ser)
        R.record_daily(s, cal[-1], fetch=fetch_full, min_n=1)
        fixed = datetime(2026, 11, 11, 23, 0)
        sent: list[str] = []
        saved = (RS._emit, RS.now_kst, RS._send_watch_charts, C.REGIME_BANNER)

        class A:
            no_collect = True
            no_auto_window = True
            news_hours = 16
            dry_run = True
        try:
            RS._emit = lambda text, dry: sent.append(text)
            RS.now_kst = lambda: fixed
            RS._send_watch_charts = lambda *_a, **_k: None
            C.REGIME_BANNER = False
            RS.mode_brief_morning(s, A())
            RS.mode_brief_evening(s, A())
            assert sent[0] == render_morning_brief(s, fixed, hours=16)
            assert sent[1] == render_evening_brief(s, fixed, picks=None, hours=16)
            C.REGIME_BANNER = True
            RS.mode_brief_morning(s, A())
            RS.mode_brief_evening(s, A())
            assert sent[2].split("\n", 1)[1] == sent[0]
            assert sent[3].split("\n", 1)[1] == sent[1]
            assert sent[2].startswith("📊 국면")
        finally:
            RS._emit, RS.now_kst, RS._send_watch_charts, C.REGIME_BANNER = saved
    check(S, "brief-morning·evening: OFF 동일 · ON 맨 위 한 줄", brief_modes_off)

    def flags_default_off():
        assert C.REGIME_BANNER is False and C.REGIME_TREND_CUT is False
        assert C.REGIME_CONFIRM_DAYS == 5
        assert C.REGIME_UP_BREADTH_PCT == 50.0 and C.REGIME_DOWN_BREADTH_PCT == 40.0
    check(S, "플래그 기본 OFF · 상수", flags_default_off)
