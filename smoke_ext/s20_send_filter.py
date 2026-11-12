# -*- coding: utf-8 -*-
"""[B] 추천 필터 — 가상 데이터 검사.

적용 순서(①SEQ ②보충 ③국면 ④재추천 제한), recos 불변, reco_events·
reco_filter_log 멱등(같은 날 재실행·재스캔), 새 추천 0개 문구, '추천 유지
n일째' 표시(목록 이탈 시 미표시 · 슬롯 변화 표기), 플래그 OFF = 지금과 같음.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import os
import sqlite3
from datetime import date, datetime, timedelta

S = "v2[B] 추천 필터"

_FLAGS = ("RECO_COOLDOWN_DAYS", "EXCLUDE_SEQ_FROM_SEND",
          "EXCLUDE_FILL_FROM_SEND", "REGIME_TREND_CUT")
_MISSING = object()


@contextlib.contextmanager
def _flags(cd=0, seq=False, fill=False, regime_cut=False, regime=None):
    """config 플래그와 국면 훅을 잠깐 바꿔 끼운다 (끝나면 원상 복구)."""
    from stocknews import config as C
    from stocknews import v2_hooks as hk
    saved = {k: getattr(C, k, _MISSING) for k in _FLAGS}
    saved_reg = hk.regime_on
    try:
        C.RECO_COOLDOWN_DAYS = cd
        C.EXCLUDE_SEQ_FROM_SEND = seq
        C.EXCLUDE_FILL_FROM_SEND = fill
        C.REGIME_TREND_CUT = regime_cut
        hk.regime_on = (lambda store, d: regime)
        yield
    finally:
        for k, v in saved.items():
            if v is _MISSING:
                if hasattr(C, k):
                    delattr(C, k)
            else:
                setattr(C, k, v)
        hk.regime_on = saved_reg


def _cal(n: int, start: str = "2026-09-01") -> list[str]:
    out, cur = [], date.fromisoformat(start)
    while len(out) < n:
        if cur.weekday() < 5:
            out.append(cur.isoformat())
        cur += timedelta(days=1)
    return out


def _dump(db, table: str, order: str) -> list[tuple]:
    with sqlite3.connect(db) as con:
        return con.execute(f"SELECT * FROM {table} ORDER BY {order}").fetchall()


def run(check, near, tmp):
    from stocknews.store import Store
    from stocknews import send_filter as sf
    from stocknews import v2_hooks as hk

    cal = _cal(12)                  # 2026-09-01(화) ~ 09-16(수)
    d0, dm2 = cal[9], cal[7]        # 오늘 = 10번째 거래일, 이틀 전
    db = tmp / "s20.db"
    st = Store(db)
    tk = ["S1", "S2", "V1", "V2", "V3", "T1", "T2", "T3", "F1", "F2", "X"]
    with sqlite3.connect(db) as con:
        con.executemany(
            "INSERT INTO prices(ticker,d,o,h,l,c,v,amt) VALUES(?,?,?,?,?,?,?,?)",
            [(t, d, 100, 101, 99, 100, 1000, 1e8) for d in cal for t in tk])
        con.commit()

    def put(d, rows):
        """rows = [(ticker, slot, grade, value, trend)] 순위 순. save_recos 처럼 갈아 끼운다."""
        with sqlite3.connect(db) as con:
            con.execute("DELETE FROM recos WHERE d=?", (d,))
            con.executemany(
                "INSERT INTO recos(d,rank,ticker,name,price,slot,grade,"
                "value_score,trend_score,reason) VALUES(?,?,?,?,?,?,?,?,?,?)",
                [(d, i, t, f"종목{t}", 100.0, s, g, v, tr, "")
                 for i, (t, s, g, v, tr) in enumerate(rows, 1)])
            con.commit()

    put(dm2, [("S1", "SEQ", "S+", 8.5, 9.0), ("T2", "TREND", "A", 3.0, 9.5),
              ("X", "VALUE", "B", 7.0, 2.0), ("V2", "VALUE", "B", 7.1, 1.0)])
    today = [("S1", "SEQ", "S+", 8.5, 9.0), ("S2", "SEQ", "A", 8.2, 8.0),
             ("V1", "VALUE", "B", 7.2, 1.0), ("V2", "VALUE", "A", 8.1, 1.0),
             ("V3", "VALUE", "B", 6.9, 1.0), ("T1", "TREND", "A", 2.0, 9.0),
             ("T2", "TREND", "A", 2.0, 10.0), ("T3", "TREND", "A", 2.0, 8.5),
             ("F1", "FILL", "A", 2.0, 9.2), ("F2", "FILL*", "A", 2.0, 9.1)]
    put(d0, today)

    def by_t(res):
        return {r["ticker"]: r for r in res.rows}

    def order():
        with _flags(cd=20, seq=True, fill=True, regime_cut=True, regime="하락"):
            res = sf.evaluate(st, d0)
        r = by_t(res)
        # ① 이 ④ 보다 먼저: S1 은 재추천이지만 사유는 SEQ
        assert r["S1"]["filter_reason"] == "SEQ" and r["S2"]["filter_reason"] == "SEQ"
        assert r["F1"]["filter_reason"] == "보충" and r["F2"]["filter_reason"] == "보충"
        # ③ TREND 3개 → 내림 1개, 추세 점수 높은 T2 를 남긴다.
        # [2026-11-12 발송용 계열] T2 는 이틀 전 ③(TREND 1개 → 0개)으로 빠져
        # 한 번도 발송되지 않았다 → 오늘 통과하면 '추천 유지' 가 아니라 새 추천.
        assert r["T1"]["filter_reason"] == "국면" and r["T3"]["filter_reason"] == "국면"
        assert r["T2"]["status"] == "new" and r["T2"]["filter_reason"] is None, r["T2"]
        # V2 는 이틀 전 실제로 발송됐다 → 유지 3일째
        assert r["V2"]["status"] == "hold" and int(r["V2"]["day_in_window"]) == 3
        assert [x["ticker"] for x in res.new] == ["V1", "V3", "T2"], res.new
        assert res.counts == {"SEQ": 2, "보충": 2, "국면": 2, "재추천 제한": 1}
        assert sf.regime_keep(3) == 1 and sf.regime_keep(2) == 1 and sf.regime_keep(1) == 0
        # 빠진 자리를 메우지 않는다 — 행 수는 그대로 10, 새 추천은 2
        assert len(res.rows) == 10 and len(res.targets) == 4
        # 국면 없음이면 ③ 미적용
        with _flags(cd=20, seq=True, fill=True, regime_cut=True, regime=None):
            r2 = by_t(sf.evaluate(st, d0))
        assert r2["T1"]["status"] == "new" and r2["T3"]["status"] == "new"
        assert r2["T2"]["status"] == "hold"
    check(S, "적용 순서 ①SEQ ②보충 ③국면(내림) ④재추천 제한 · 메우지 않음", order)

    def off_same():
        before = _dump(db, "recos", "d,rank")
        with _flags():
            tg = hk.send_targets(st, d0)
            assert hk.exclusion_line(st, d0) is None
            assert sf.record(st, d0) is None, "OFF 인데 기록함"
        assert isinstance(tg, list) and len(tg) == 10
        assert [t["ticker"] for t in tg] == [x[0] for x in today]
        assert all(t["is_new_event"] and t["day_in_window"] == 1 for t in tg)
        assert _dump(db, "reco_events", "ticker") == []
        assert _dump(db, "reco_filter_log", "d") == []
        assert hk.send_targets(st, "2026-01-05") == []
        assert _dump(db, "recos", "d,rank") == before
    check(S, "플래그 OFF = recos 전체 · is_new_event 전부 True · 기록 없음", off_same)

    def recos_unchanged():
        before = _dump(db, "recos", "d,rank")
        for kw in ({"cd": 20}, {"seq": True}, {"fill": True},
                   {"cd": 20, "seq": True, "fill": True, "regime_cut": True,
                    "regime": "하락"}):
            with _flags(**kw):
                sf.record(st, dm2)
                sf.record(st, d0)
                sf.render_send(sf.evaluate(st, d0), datetime(2026, 9, 20, 21, 30))
                hk.send_targets(st, d0)
            assert _dump(db, "recos", "d,rank") == before, kw
    check(S, "플래그 ON/OFF 에서 recos 행 완전히 동일", recos_unchanged)

    def idempotent():
        with _flags(cd=20, seq=True, fill=True):
            sf.record(st, d0)
            e1 = _dump(db, "reco_events", "ticker,event_date")
            l1 = _dump(db, "reco_filter_log", "d,ticker")
            sf.record(st, d0)                          # 같은 날 재실행
            assert _dump(db, "reco_events", "ticker,event_date") == e1
            assert _dump(db, "reco_filter_log", "d,ticker") == l1
            put(d0, today)                             # --force 재스캔 (같은 결과)
            sf.record(st, d0)
            assert _dump(db, "reco_events", "ticker,event_date") == e1
            assert _dump(db, "reco_filter_log", "d,ticker") == l1
            # 소급 명령과 일일 기록이 같은 결과
            sf.backfill_events(st, window=20)
            assert _dump(db, "reco_events", "ticker,event_date") == e1
        keys = [(r[0], r[1]) for r in e1]
        assert len(keys) == len(set(keys)), "reco_events 중복 행"
        ev = {(r[0], r[1]): r for r in e1}
        # [발송용 계열] SEQ 제외로 한 번도 발송되지 않은 S1 은 이벤트가 없다.
        assert not any(k[0] in ("S1", "S2", "F1", "F2") for k in ev), sorted(ev)
        # 제한 종료일 = 20일째. 달력(12일) 밖이라 주말을 뺀 예상 거래일.
        assert ev[("V2", dm2)][2] == "2026-10-07", ev[("V2", dm2)]
        assert ev[("V2", dm2)][3] == 20
        assert ev[("V2", dm2)][11] == 2, ev[("V2", dm2)]            # days_listed
        assert ev[("V2", dm2)][12] and "등급 B→A" in ev[("V2", dm2)][12]
        reasons = {r[1]: r[6] for r in l1}
        assert reasons["S1"] == "SEQ" and reasons["V2"] == "재추천 제한"
        assert "V1" not in reasons, "새 추천이 제외 로그에 있다"
    check(S, "reco_events·reco_filter_log 멱등 (재실행·재스캔·소급)", idempotent)

    def zero_new():
        d1 = cal[10]
        put(d1, [("S1", "SEQ", "S+", 8.5, 9.0), ("F1", "FILL", "A", 2.0, 9.2)])
        try:
            with _flags(seq=True, fill=True):
                res = sf.evaluate(st, d1)
                txt = sf.render_send(res, datetime(2026, 9, 20, 21, 30))
            assert res.new == []
            assert "오늘 새 추천 없음" in txt, txt
            assert "오늘 제외 — SEQ 1, 보충 1, 재추천 제한 0" in txt, txt
        finally:
            with sqlite3.connect(db) as con:
                con.execute("DELETE FROM recos WHERE d=?", (d1,))
                con.commit()
    check(S, "새 추천 0개 → '오늘 새 추천 없음' + 제외 한 줄", zero_new)

    def hold_display():
        with _flags(cd=20):
            res = sf.evaluate(st, d0)
            txt = sf.render_send(res, datetime(2026, 9, 20, 21, 30))
        lines = sf.hold_lines(res)
        assert any("종목T2" in x and "추천 유지 3일째" in x for x in lines), lines
        # S1 은 슬롯·등급 그대로 → 변화 표기 없음, V2 는 등급 B→A
        s1 = next(x for x in lines if "(S1)" in x)
        assert "→" not in s1, s1
        v2 = next(x for x in lines if "(V2)" in x)
        assert "등급 B→A" in v2, v2
        # 오늘 목록에 없는 X 는 기간 중이어도 표시하지 않는다
        assert not any("(X)" in x for x in lines), lines
        assert "추천 유지 3일째" in txt and "(X)" not in txt
        assert "[주간 추천 7선]" in txt, txt.splitlines()[0]
        # 슬롯 변화: 이틀 전 VALUE → 오늘 TREND
        put(dm2, [("T2", "VALUE", "A", 8.0, 9.5)])
        try:
            with _flags(cd=20):
                ln = sf.hold_lines(sf.evaluate(st, d0))
            assert any("(T2)" in x and "슬롯 매집→추세" in x for x in ln), ln
        finally:
            put(dm2, [("S1", "SEQ", "S+", 8.5, 9.0), ("T2", "TREND", "A", 3.0, 9.5),
                      ("X", "VALUE", "B", 7.0, 2.0), ("V2", "VALUE", "B", 7.1, 1.0)])
    check(S, "'추천 유지 n일째' 표시 · 이탈 종목 미표시 · 변화만 표기", hold_display)

    def relaxed_mark():
        with _flags(fill=True):
            txt = sf.render_send(sf.evaluate(st, d0), datetime(2026, 9, 20, 21, 30))
        v1 = next(x for x in txt.splitlines() if "(V1)" in x)
        v2 = next(x for x in txt.splitlines() if "(V2)" in x)
        assert "하한 완화 적용" in v1 and "하한 완화 적용" not in v2, (v1, v2)
        with _flags():
            assert sf.any_on() is False
    check(S, "VALUE 하한 완화 채움은 남기고 '하한 완화 적용' 표기만", relaxed_mark)

    def send_path():
        """일요일 daily --send-only: 필터 ON 이면 필터 발송, OFF 면 예전 그대로."""
        import run_screen as rs
        from stocknews import notify as nt
        fri, sun = "2026-09-18", "2026-09-20"     # d0 뒤의 금·일
        put(fri, today)

        def _run():
            args = argparse.Namespace(force=False, dry_run=False, with_fib=False,
                                      top=10, limit=None, min_amount=0.0,
                                      send_only=True,
                                      asof=date.fromisoformat(sun))
            saved = (dict(rs.SUMMARY), rs.send_telegram, rs._send_charts,
                     os.getcwd())
            texts = []
            try:
                rs.SUMMARY.clear()
                rs.send_telegram = lambda t, *a, **k: (texts.append(t),
                                                       nt.SendReport(sent=1))[1]
                rs._send_charts = lambda *a, **k: None
                os.chdir(tmp)
                with contextlib.redirect_stdout(io.StringIO()), \
                        contextlib.redirect_stderr(io.StringIO()):
                    rc = rs.mode_daily(st, args)
                sm = dict(rs.SUMMARY)
            finally:
                os.chdir(saved[3])
                rs.SUMMARY.clear()
                rs.SUMMARY.update(saved[0])
                rs.send_telegram, rs._send_charts = saved[1], saved[2]
            return rc, sm, texts
        try:
            with _flags():
                rc, sm, texts = _run()
            assert rc == 0 and len(texts) == 1 and "주간 추천 10선" in texts[0], texts
            assert "오늘 제외" not in texts[0] and "send_filter" not in sm
            with _flags(seq=True, fill=True):
                rc, sm, texts = _run()
            assert rc == 0 and "주간 추천 6선" in texts[0], texts[0][:120]
            assert "오늘 제외 — SEQ 2, 보충 2, 재추천 제한 0" in texts[0]
            assert sm["send_filter"]["new"] == 6 and sm["reco_sent"] is True
            assert {r[1] for r in _dump(db, "reco_filter_log", "d,ticker")
                    if r[0] == fri} == {"S1", "S2", "F1", "F2"}
        finally:
            with sqlite3.connect(db) as con:
                con.execute("DELETE FROM recos WHERE d=?", (fri,))
                con.execute("DELETE FROM reco_filter_log WHERE d=?", (fri,))
                con.commit()
    check(S, "일요일 발송 경로: OFF 그대로 · ON 필터 발송 + 기록", send_path)

    def mode_registered():
        import run_screen as rs
        from stocknews.joblock import MODE_TIMEOUTS
        assert "reco-events-backfill" in rs.MODES
        assert "reco-events-backfill" in rs._WRITE_MODES
        assert "reco-events-backfill" in MODE_TIMEOUTS
        with _flags():
            out = sf.backfill_events(st)
        assert out["window"] == 20 and out["events"] > 0, out
    check(S, "소급 모드 등록 · 락 타임아웃 · 기본 창 20", mode_registered)


    def two_tracks():
        # [2026-11-12] 측정용(recos 전체)과 발송용(①②③ 통과 행) 이벤트가 갈린다.
        from stocknews import events as E
        recos = sf._load_recos(st, d0)
        meas = E.events_only(E.assign_events(recos, E.trading_calendar(st), window=20))
        assert ("S1", dm2) in {(r["ticker"], r["event_date"]) for r in meas.to_dict("records")}
        with _flags(cd=20, seq=True):
            res = sf.evaluate(st, d0)
            tg = hk.send_targets(st, d0)
        r = by_t(res)
        # 발송용: S1 은 SEQ 로 계속 빠져 이벤트가 없고 targets 에도 없다.
        assert r["S1"]["status"] == "excluded"
        assert "S1" not in {t["ticker"] for t in tg}
        # 발송된 V2 만 '유지', 처음 통과한 V1·V3 는 새 추천.
        assert r["V2"]["status"] == "hold" and r["V1"]["status"] == "new"
        lines = sf.hold_lines(res)
        assert any("V2" in x or "종목V2" in x for x in lines) and not any("S1" in x for x in lines)
    check(S, "이벤트 두 계열: 측정용(recos 전체) vs 발송용(필터 통과) · 유지는 발송된 종목만",
          two_tracks)
