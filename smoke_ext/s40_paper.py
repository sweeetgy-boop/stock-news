# -*- coding: utf-8 -*-
"""[D·F] 추천 추적(종이 편입) · 평일 종목 발송 개편 — 가상 DB 검사.

추가 전용 강제, 편입 불가 사유, 고정 시드 재현, 만기 확정, 4,096 자 축약
순서, 플래그 OFF 불변, 발송 기록 판정, 재추천 제한 0 경고.
"""
from __future__ import annotations

import inspect
import sqlite3
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

S = "v2[DF] 추천 추적"
N_DAYS = 140
SIG = 100          # 신호일 위치 (이벤트 날짜)


def _cal(n: int, start: str = "2026-03-02") -> list[str]:
    out, cur = [], date.fromisoformat(start)
    while len(out) < n:
        if cur.weekday() < 5:
            out.append(cur.isoformat())
        cur += timedelta(days=1)
    return out


def _build(tmp: Path):
    """가상 DB. 25종목 × 140거래일. 특수 종목:
    LU = 신호 다음날 시가 상한가, SU = 신호 다음날 행 없음(거래정지),
    HA = 진입 후 115일째부터 행 없음(추적 중 거래정지)."""
    from stocknews.store import Store
    cal = _cal(N_DAYS)
    db = tmp / "paper.db"
    st = Store(db)
    rng = np.random.default_rng(7)
    tick = [f"9{i:05d}" for i in range(22)] + ["LU0000", "SU0000", "HA0000"]
    rows = []
    for t in tick:
        px = 10000.0
        for i, d in enumerate(cal):
            prev = px
            px = max(500.0, px * (1 + rng.normal(0.0005, 0.02)))
            o = prev * (1 + rng.normal(0, 0.005))
            if t == "LU0000" and i == SIG + 1:
                o = prev * 1.299
            if t == "SU0000" and i == SIG + 1:
                continue
            if t == "HA0000" and i >= 115:
                continue
            hi, lo = max(o, px) * 1.01, min(o, px) * 0.99
            rows.append((t, d, o, hi, lo, px, 1e5, 1e9))
    with closing(sqlite3.connect(db)) as con:
        con.executemany("INSERT INTO prices(ticker,d,o,h,l,c,v,amt) "
                        "VALUES(?,?,?,?,?,?,?,?)", rows)
        con.executemany(
            "INSERT INTO tickers(ticker,name,market,active) VALUES(?,?,?,1)",
            [(t, f"종목{t[-2:]}", "KOSPI") for t in tick])
        rec = []
        for rk, t in enumerate(["900000", "LU0000", "SU0000", "HA0000",
                                "900001"], 1):
            rec.append((cal[SIG], rk, t, f"종목{t[-2:]}", 10000.0,
                        ("VALUE", "TREND", "SEQ", "VALUE", "FILL")[rk - 1],
                        "A", 7.0, 5.0, "r"))
        rec.append((cal[SIG + 1], 1, "900000", "종목00", 10000.0, "VALUE",
                    "A", 7.0, 5.0, "r"))           # 기간 중 재추천 = 같은 이벤트
        rec.append((cal[SIG + 5], 1, "900002", "종목02", 10000.0, "TREND",
                    "B", 3.0, 7.0, "r"))           # 새 이벤트
        con.executemany("INSERT INTO recos(d,rank,ticker,name,price,slot,grade,"
                        "value_score,trend_score,reason) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        rec)
        for d in (cal[SIG], cal[SIG + 5]):
            con.executemany(
                "INSERT INTO scans(d,ticker,name,price,value_score,trend_score,"
                "grade,track,lps,trend_raw,fib_score,confluence,sequence_confirm)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [(d, t, t, 10000.0, 7.0, 5.0, "A", "VALUE", 6.0, 5.0, 8.0, 1, 0)
                 for t in tick])
        con.commit()
    return st, cal


def run(check, near, tmp):
    from stocknews import config as C
    from stocknews import paper as P
    from stocknews import paper_render as R
    from stocknews import v2_hooks

    st, cal = _build(tmp)
    sent = {cal[SIG]: "nightly_x.log", cal[SIG + 5]: "nightly_y.log"}

    def tables():
        with closing(sqlite3.connect(st.path)) as con:
            return {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                    for t in ("paper_sends", "paper_positions", "paper_results",
                              "paper_random_bundles", "paper_random_results",
                              "paper_shadow")}

    # ── 만기 미도래 시점까지만 ──
    early = P.backfill(st, sent_dates=sent, upto=cal[SIG + 15], index={},
                       allow_network=False)
    early.pop("_data", None)

    def maturity_gate():
        book = P.PaperBook(st)
        res = book.results()
        assert set(res["horizon"]) <= {5, 10}, sorted(set(res["horizon"]))
        rr = book.random_results()
        assert rr.empty or set(rr["horizon"]) <= {5, 10}
        assert book.shadow().empty, "21일째 시가 전에 그림자 청산이 확정됐다"
    check(S, "만기 전에는 20일 성적·그림자 청산을 확정하지 않는다", maturity_gate)

    full = P.backfill(st, sent_dates=sent, index={}, allow_network=False)
    full.pop("_data", None)
    book = P.PaperBook(st)
    pos = book.positions().set_index("ticker")

    def enroll_skip():
        assert set(pos.index) == {"900000", "LU0000", "SU0000", "HA0000",
                                  "900001", "900002"}, list(pos.index)
        assert int(pos.at["LU0000", "enrolled"]) == 0
        assert "상한가" in pos.at["LU0000", "skip_reason"]
        assert int(pos.at["SU0000", "enrolled"]) == 0
        assert "거래정지" in pos.at["SU0000", "skip_reason"]
        assert int(pos.at["900000", "enrolled"]) == 1
        # 편입가 = 신호 다음 거래일 시가
        with closing(sqlite3.connect(st.path)) as con:
            o = con.execute("SELECT o FROM prices WHERE ticker='900000' AND d=?",
                            (cal[SIG + 1],)).fetchone()[0]
        near(pos.at["900000", "entry_price"], o, 1e-9, "편입가")
        assert pos.at["900000", "entry_date"] == cal[SIG + 1]
        assert int(pos.at["900000", "backfill"]) == 1
        # 손절 계획가: 현행 = 진입가 −10%, 대안 = 10~15% 사이
        e = float(pos.at["900000", "entry_price"])
        near(pos.at["900000", "stop_price"], e * 0.9, 1e-6, "현행 손절")
        alt = float(pos.at["900000", "alt_stop_price"])
        assert e * 0.85 - 1e-6 <= alt <= e * 0.90 + 1e-6, (alt, e)
    check(S, "편입가 = 다음 거래일 시가 · 상한가/거래정지 미편입 + 사유", enroll_skip)

    def maturity_final():
        res = book.results()
        pid = int(pos.at["900000", "id"])
        r20 = res[(res["position_id"] == pid) & (res["horizon"] == 20)]
        assert len(r20) == 1 and r20.iloc[0]["exit_date"] == cal[SIG + 20]
        assert res[(res["position_id"] == pid) & (res["horizon"] == 40)].empty, \
            "40일째 미도래인데 기록됨"
        h = res[(res["position_id"] == int(pos.at["HA0000", "id"]))
                & (res["horizon"] == 20)].iloc[0]
        assert h["status"] == "HALTED" and h["exit_date"] == cal[114], dict(h)
        # 재실행 = 행 추가 없음 (멱등)
        before = tables()
        again = P.backfill(st, sent_dates=sent, index={}, allow_network=False)
        again.pop("_data", None)
        assert tables() == before, (before, tables())
    check(S, "20일 만기 확정 · 추적 중 거래정지 = 마지막 가격 · 재실행 멱등",
          maturity_final)

    def append_only():
        with closing(sqlite3.connect(st.path)) as con:
            for sql in ("UPDATE paper_positions SET entry_price=1",
                        "DELETE FROM paper_positions",
                        "UPDATE paper_results SET net=0",
                        "DELETE FROM paper_sends",
                        "UPDATE paper_random_bundles SET tickers=''",
                        "DELETE FROM paper_shadow"):
                try:
                    con.execute(sql)
                except sqlite3.DatabaseError as exc:
                    assert "append-only" in str(exc), exc
                else:
                    raise AssertionError(f"막히지 않음: {sql}")
        bad = ("update", "set", "delete", "remove", "edit", "modify", "replace",
               "overwrite", "fix")
        for name, _m in inspect.getmembers(P.PaperBook, inspect.isfunction):
            if name.startswith("_"):
                continue
            assert not any(b in name.lower() for b in bad), f"수정 경로: {name}"
        src = inspect.getsource(P).upper()
        for s in ("UPDATE PAPER_", "DELETE FROM PAPER_", "REPLACE INTO PAPER_",
                  "DO UPDATE"):
            assert s not in src, f"paper.py 에 {s}"
        # 정정은 새 행
        n0 = len(book.all_position_rows())
        nid = book.add_correction(int(pos.at["900001", "id"]), "test",
                                  skip_reason="정정")
        assert len(book.all_position_rows()) == n0 + 1
        eff = book.positions().set_index("ticker")
        assert int(eff.at["900001", "id"]) == nid
    check(S, "추가 전용 — UPDATE/DELETE 트리거 · 수정 메서드 없음 · 정정 행",
          append_only)

    def seed_repro():
        a = P.draw_bundles([f"T{i}" for i in range(50)], 5, "2026-10-02")
        b = P.draw_bundles([f"T{i}" for i in reversed(range(50))], 5, "2026-10-02")
        c = P.draw_bundles([f"T{i}" for i in range(50)], 5, "2026-10-08")
        assert a == b and len(a) == C.PAPER_RANDOM_BUNDLES, "같은 입력 다른 결과"
        assert a != c, "날짜가 다르면 묶음도 달라야 한다"
        bd = book.bundles()
        sd = cal[SIG]
        g = bd[bd["signal_d"] == sd].sort_values("bundle")
        k = int(g["k"].iloc[0])
        assert k == int(((book.positions()["signal_d"] == sd)
                         & (book.positions()["enrolled"] == 1)).sum())
        redo = P.draw_bundles(P._universe(st, sd), k, sd)
        assert [",".join(x) for x in redo] == list(g["tickers"])
        rr = book.random_results()
        me = rr[(rr["signal_d"] == sd) & (rr["horizon"] == 20) & (rr["bundle"] == -1)]
        assert len(me) == 1 and 0 <= float(me.iloc[0]["pct"]) <= 100
        assert P.percentile_of(5.0, [1.0, 2.0, 3.0]) == (100.0, 1)
        assert P.percentile_of(0.0, [1.0, 2.0, 3.0]) == (0.0, 4)
    check(S, "무작위 대조군 — 고정 시드 재현 · 같은 수 · 백분위", seed_repro)

    def shadow_sets():
        sh = book.shadow()
        assert set(sh["rule_set"]) == {"current", "alt", "hold20"}, set(sh["rule_set"])
        for pid, g in sh[sh["rule_set"] != "hold20"].groupby(["position_id", "rule_set"]):
            assert abs(g["ratio"].sum() - 1.0) < 1e-6, (pid, g["ratio"].sum())
        assert "stop:fixed" in P.RULE_MAP.values()
        assert P.alt_stop_price(10000, 300) == 10000 * 0.9      # 2×ATR 6% → 하한 10%
        assert P.alt_stop_price(10000, 1000) == 10000 * 0.85    # 20% → 상한 15%
        near(P.alt_stop_price(10000, 600), 10000 * 0.88, 1e-9, "12%")
    check(S, "그림자 청산 — 현행/대안/고정 20일 · 전량 소진 · 대안 손절 범위",
          shadow_sets)

    def limit_shrink():
        d = cal[SIG + 8]
        pdx = P.PaperData(st, d, index={})
        full_t, i1 = R.weekday_block(st, d, paper_on=True, pdata=pdx)
        assert i1["topn"] == C.PAPER_SUMMARY_TOPN and "▲ 상위" in full_t
        assert "③ 추적 중" in full_t and "검증 중 · 투자 판단 근거 아님" in full_t
        short, i2 = R.weekday_block(st, d, paper_on=True, pdata=pdx,
                                    limit_room=len(full_t) - 1)
        assert i2["topn"] < i1["topn"], "③ 이 줄지 않았다"
        sec1 = full_t.split("━━ ②")[0]
        assert short.startswith(sec1), "①이 먼저 줄었다"
        for w in ("매수", "매도"):
            assert w not in full_t, f"발송 문구에 '{w}'"
    check(S, "4,096 자 초과 → ③(상위·하위)부터 줄인다 · ① 유지", limit_shrink)

    def flags_off():
        assert C.PAPER_TRACKING is False and C.DAILY_LIST_V2 is False, "기본 OFF"
        assert v2_hooks.tracked_tickers(st, cal[SIG + 8]) == set()
        old = C.PAPER_TRACKING
        try:
            C.PAPER_TRACKING = True
            tt = v2_hooks.tracked_tickers(st, cal[SIG + 8])
            assert "900000" in tt and "LU0000" not in tt, tt
            # 20일째(만기일)부터는 추적 중이 아니다
            assert "900000" not in v2_hooks.tracked_tickers(st, cal[SIG + 20])
            assert "900000" in v2_hooks.tracked_tickers(st, cal[SIG + 19])
        finally:
            C.PAPER_TRACKING = old
        import run_screen as rs
        from stocknews.renderer import render_daily_holdings, render_evening_brief
        from datetime import datetime
        txt = render_daily_holdings([], {}, datetime(2026, 11, 10, 23), "2026-11-10",
                                    100, 10)
        assert "• 추천 목록은 일요일에 발송됩니다" in txt
        src = inspect.getsource(rs.mode_brief_evening)
        assert "_cfg.PAPER_TRACKING or _cfg.DAILY_LIST_V2" in src
        assert "_paper_sunday" in inspect.getsource(rs._daily_send_only)
        sig = inspect.signature(render_evening_brief)
        assert sig.parameters["stock_block"].default is None
        assert sig.parameters["watchlist"].default is True
        assert "paper" in rs._WRITE_MODES and "paper" in rs.MODES
    check(S, "플래그 OFF = 기존 경로 · 훅 빈 집합 · 기본값 불변", flags_off)

    def cooldown_warn():
        had = hasattr(C, "RECO_COOLDOWN_DAYS")
        old_v = getattr(C, "RECO_COOLDOWN_DAYS", None)
        old_p = C.PAPER_TRACKING
        try:
            C.PAPER_TRACKING = True
            C.RECO_COOLDOWN_DAYS = 0
            assert P.cooldown_warning() and P.paper_window() == 0
            C.RECO_COOLDOWN_DAYS = 20
            assert P.cooldown_warning() is None
            C.PAPER_TRACKING = False
            C.RECO_COOLDOWN_DAYS = 0
            assert P.cooldown_warning() is None
        finally:
            C.PAPER_TRACKING = old_p
            if had:
                C.RECO_COOLDOWN_DAYS = old_v
            else:
                delattr(C, "RECO_COOLDOWN_DAYS")
    check(S, "PAPER_TRACKING + RECO_COOLDOWN_DAYS=0 경고", cooldown_warn)

    def sent_logs():
        ld = tmp / "logs"
        ld.mkdir(exist_ok=True)
        ok = ('2026-10-18 21:30:35 ---- daily 시작 ----\n'
              '2026-10-18 13:30:40,439 [INFO] 텔레그램 발송 완료 (1854자 · 수신자 2명)\n'
              '{"mode": "daily", "reco_sent": true, "send_only": true, '
              '"trade_date": "2026-10-16", "reco_date": "2026-10-16"}\n')
        dry = ('2026-10-25 21:30:35 ---- daily 시작 ----\n'
               '======\n🗓 [주간 추천 10선]\n======\n'
               '{"mode": "daily", "reco_sent": true, "reco_date": "2026-10-23"}\n')
        (ld / "nightly_20261018.log").write_text(ok, encoding="utf-8")
        (ld / "nightly_20261025.log").write_text(dry, encoding="utf-8")
        got = P.sent_dates_from_logs(ld)
        assert got == {"2026-10-16": "nightly_20261018.log"}, got
    check(S, "운영 로그 발송 판정 — '발송 완료' 있는 reco_sent 만", sent_logs)

    def counts():
        c = P.event_counts(st, [cal[SIG]])
        assert c["events_all"] == 6 and c["sent_events_any_row"] == 5, c
    check(S, "필터 OFF 이벤트 수 vs 발송분 이벤트 수", counts)
