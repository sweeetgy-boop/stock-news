# -*- coding: utf-8 -*-
"""[A] 종가 확정값 보정 — nightly 첫 단계 `reconcile` (R3-4).

밤 적재는 잠정(is_final=0, source=적재 경로). 다음 nightly 첫 단계가 전
거래일분을 KRX 오픈API 확정값으로 덮는다(고가·저가·종가·거래량만). 보정 전
행은 price_provisional 에 통째로, 달랐던 필드는 price_diffs 에 남는다.
백필 구간은 거부, 분할 흔적 종목·날짜는 건너뛴다. 네트워크 없음.
"""
from __future__ import annotations

import contextlib
import importlib
import inspect
import io
import json
import sqlite3
import types
from contextlib import closing
from datetime import datetime

import pandas as pd

S = "A 종가 확정 보정"
DAY, BAS = "2026-11-10", "20261110"           # 화요일
PREV, NEXT = "2026-11-09", "2026-11-11"


def _krx(spec: dict) -> pd.DataFrame:
    """ticker -> (o,h,l,c,v,amt). data.krx_daily_prices 반환 형식."""
    cols = ["시가", "고가", "저가", "종가", "거래량", "거래대금"]
    return pd.DataFrame([list(v) for v in spec.values()], index=list(spec),
                        columns=cols, dtype=float)


def _seed(st, rows, day=DAY, source="naver"):
    """잠정 행 (ticker, o,h,l,c,v,amt)."""
    for t, o, h, l, c, v, amt in rows:
        df = pd.DataFrame({"시가": [o], "고가": [h], "저가": [l], "종가": [c],
                           "거래량": [v], "거래대금": [amt]},
                          index=pd.to_datetime([day]))
        st.upsert_prices(t, df, allow_today=True, source=source)


def _row(st, t, day=DAY):
    with closing(sqlite3.connect(st.path)) as con:
        return con.execute(
            "SELECT o,h,l,c,v,amt,source,is_final FROM prices "
            "WHERE ticker=? AND d=?", (t, day)).fetchone()


def _count(st, table):
    with closing(sqlite3.connect(st.path)) as con:
        return con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def run(check, near, tmp):
    from stocknews import config as C
    from stocknews.reconcile import (ReconcileRefused, reconcile_day,
                                     split_trace)
    from stocknews.store import Store

    def t_fields_and_preserve():
        st = Store(tmp / "fields.db")
        _seed(st, [
            # 시가 일치 · 고가/종가/거래량 다름 · 거래대금 없음(종목별 경로)
            ("005930", 100000, 102000, 99000, 101500, 1000, None),
            # 시가까지 다름 -> 시가는 그대로 두고 untouched 로 센다
            ("000660", 50000, 51000, 49000, 50500, 2000, 1.0e8),
        ])
        krx = _krx({
            "005930": (100000, 102500, 99000, 101000, 1100, 1.1e8),
            "000660": (50100, 51000, 48900, 50400, 2000, 1.01e8),
        })
        res = reconcile_day(st, DAY, krx, universe=None)
        assert tuple(res["fields"]) == C.PRICE_CONFIRM_FIELDS == (
            "h", "l", "c", "v"), res["fields"]
        assert res["confirmed"] == 2 and res["written"] == 2, res
        assert res["untouched_diff"] == {"o": 1, "amt": 1}, res
        assert res["amt_missing"] == 1, res
        a = _row(st, "005930")
        # 고가·종가·거래량은 KRX, 시가는 그대로, 거래대금은 채우지 않음
        assert a == (100000, 102500, 99000, 101000, 1100, None,
                     "krx_api", 1), a
        b = _row(st, "000660")
        assert b[0] == 50000 and b[2] == 48900 and b[5] == 1.0e8, b
        assert b[6:] == ("krx_api", 1), b
        # 보정 전 행 전체 보존 (source 포함)
        with closing(sqlite3.connect(st.path)) as con:
            pv = con.execute(
                "SELECT o,h,l,c,v,amt,source FROM price_provisional "
                "WHERE ticker='005930' AND d=?", (DAY,)).fetchone()
            fields = {r[0] for r in con.execute(
                "SELECT field FROM price_diffs WHERE ticker='000660'")}
        assert pv == (100000, 102000, 99000, 101500, 1000, None, "naver"), pv
        assert res["provisional_saved"] == 2, res
        # price_diffs 는 실제로 덮은 필드만 (시가·거래대금 없음)
        assert fields == {"l", "c"}, fields
    check(S, "고가·저가·종가·거래량만 덮음 · 시가·거래대금 그대로 · 보정 전 행 보존",
          t_fields_and_preserve)

    def t_idempotent():
        st = Store(tmp / "idem.db")
        _seed(st, [("005930", 10, 12, 9, 11, 100, None),
                   ("000660", 20, 22, 19, 21, 200, None)])
        krx = _krx({"005930": (10, 12, 9, 10, 110, 1100),
                    "000660": (20, 22, 19, 21, 200, 4200)})
        r1 = reconcile_day(st, DAY, krx)
        snap = (_row(st, "005930"), _row(st, "000660"),
                _count(st, "price_diffs"), _count(st, "price_provisional"))
        r2 = reconcile_day(st, DAY, krx)
        assert r1["confirmed"] == 2 and r2["confirmed"] == 0, (r1, r2)
        assert r2["already_final"] == 2 and r2["written"] == 0, r2
        assert snap == (_row(st, "005930"), _row(st, "000660"),
                        _count(st, "price_diffs"),
                        _count(st, "price_provisional")), "재실행이 바꿨다"
        # 밤 적재가 다시 돌아도 확정 행은 잠정값으로 돌아가지 않는다
        _seed(st, [("005930", 1, 1, 1, 1, 1, 1)], source="fdr_snapshot")
        assert _row(st, "005930")[3] == 10 and _row(st, "005930")[6] == "krx_api"
    check(S, "멱등: 재실행은 비교 0 · 값·보존·차이 기록 불변 · 확정 행 보호",
          t_idempotent)

    def t_backfill_refused():
        st = Store(tmp / "bf.db")
        bf = "2026-08-27"
        assert C.RECONCILE_MIN_DATE == "2026-08-28"
        _seed(st, [("380540", 100, 100, 100, 100, 1000, None)], day=bf)
        try:
            reconcile_day(st, bf, _krx({"380540": (50, 50, 50, 50, 2000, 1e5)}))
        except ReconcileRefused as exc:
            assert exc.code == "before_min_date", exc.code
        else:
            raise AssertionError("백필 구간인데 보정했다")
        assert _row(st, "380540", bf)[6:] == ("naver", 0)
        assert _count(st, "price_provisional") == 0
        assert _count(st, "price_diffs") == 0
    check(S, "백필 구간(RECONCILE_MIN_DATE 이전) 거부 · 아무것도 안 씀",
          t_backfill_refused)

    def t_split_trace_skipped():
        st = Store(tmp / "split.db")
        # 직전 거래일 종가 (확정 여부 무관)
        _seed(st, [("222220", 1000, 1000, 1000, 1000, 50, None),
                   ("333330", 500, 500, 500, 500, 50, None),
                   ("444440", 800, 800, 800, 800, 50, None),
                   ("777770", 1000, 1000, 1000, 1000, 50, None)], day=PREV)
        _seed(st, [
            ("222220", 600, 610, 590, 600, 100, None),    # 직전 1000 -> 600 (-40%)
            ("333330", 505, 510, 500, 506, 100, None),    # 정상 (보정)
            ("444440", 810, 820, 800, 812, 100, None),    # 다음날 +60% 는 안 봄
            ("555550", 2000, 2200, 1800, 2000, 1000, None),   # 수정주가 모양
            ("777770", 1000, 1400, 1000, 1400, 100, None),    # 잠정만 +40% (가짜)
        ])
        _seed(st, [("444440", 1300, 1300, 1300, 1300, 30, None)], day=NEXT)
        others = {f"6{i:04d}0": (500, 500, 500, 500, 1010, 505000)
                  for i in range(10)}
        _seed(st, [(t, 500, 500, 500, 500, 1000, None) for t in others])
        krx = _krx({
            "222220": (600, 610, 590, 601, 100, 6e4),
            "333330": (505, 510, 500, 505, 100, 5e4),
            "444440": (810, 820, 800, 811, 100, 8e4),
            "555550": (1000, 1100, 900, 1000, 2000, 2e6),
            "777770": (1000, 1100, 1000, 1100, 100, 1.1e5),
            **others,
        })
        res = reconcile_day(st, DAY, krx)
        got = dict(res["skipped_split"])
        assert got == {"222220": "jump", "555550": "adjusted"}, got
        assert res["adjusted_suspects"] == 1, res      # 1/13 < 10% -> 진행
        for t in got:
            assert _row(st, t)[7] == 0, f"{t} 분할 흔적인데 확정됐다"
        assert _row(st, "333330")[3:5] == (505, 100), _row(st, "333330")
        assert _row(st, "333330")[7] == 1
        assert _row(st, "444440")[7] == 1, "다음 날 불연속으로 건너뛰었다"
        # 잠정만 튄 행은 오류다 -> 덮어서 가짜 불연속을 없앤다
        assert _row(st, "777770")[3] == 1100 and _row(st, "777770")[7] == 1
        assert res["confirmed"] == 13, res
        with closing(sqlite3.connect(st.path)) as con:
            skipped_diffs = con.execute(
                "SELECT COUNT(*) FROM price_diffs WHERE ticker IN "
                "('222220','555550')").fetchone()[0]
        assert skipped_diffs == 0, "건너뛴 종목의 차이를 기록했다"
        # 판정 함수 단독: 상한가(정확히 +30%)는 흔적이 아니다
        p = {"o": 1300, "h": 1300, "l": 1300, "c": 1300, "v": 10}
        k = {"시가": 1300, "고가": 1300, "저가": 1300, "종가": 1300, "거래량": 10}
        assert split_trace(p, k, prev_c=1000) is None
        assert split_trace(p, k, prev_c=999) == "jump"
        assert split_trace(p, k, prev_c=None) is None
        p2 = dict(p, c=1400)
        k2 = dict(k, 종가=1100)
        assert split_trace(p2, k2, prev_c=1000) is None, "잠정만 튄 건 흔적 아님"
    check(S, "분할 흔적(수정주가 모양 · KRX 직전 대비 ±30%) 종목·날짜만 건너뜀 · 잠정만 튄 건 보정",
          t_split_trace_skipped)

    def t_whole_refusal_kept():
        st = Store(tmp / "whole.db")
        seed, spec = [], {}
        for i in range(3):
            t = f"1{i:04d}0"
            seed.append((t, 2000, 2200, 1800, 2000, 1000, None))
            spec[t] = (1000, 1100, 900, 1000, 2000, 2e6)
        for i in range(7):
            t = f"2{i:04d}0"
            seed.append((t, 500, 500, 500, 500, 1000, None))
            spec[t] = (500, 500, 500, 500, 1010, 505000)
        _seed(st, seed)
        try:
            reconcile_day(st, DAY, _krx(spec))
        except ReconcileRefused as exc:
            assert exc.code == "adjusted_price_suspect", exc.code
        else:
            raise AssertionError("수정주가 30% 인데 보정했다")
        assert _count(st, "price_provisional") == 0
        assert _row(st, "200000")[7] == 0
    check(S, "수정주가 모양 10% 초과 = 전체 거부 유지 (쓰기 전 중단)",
          t_whole_refusal_kept)

    def t_source_labels():
        st = Store(tmp / "src.db")
        df = pd.DataFrame({"시가": [1.0], "고가": [1.0], "저가": [1.0],
                           "종가": [1.0], "거래량": [1.0], "거래대금": [1.0]},
                          index=["005930"])
        st.upsert_cross_section(DAY, df, allow_today=True, source="fdr_snapshot")
        assert _row(st, "005930")[6:] == ("fdr_snapshot", 0)
        _seed(st, [("005930", 2, 2, 2, 2, 2, None)])           # naver 가 덮음
        assert _row(st, "005930")[6:] == ("naver", 0)
        _seed(st, [("000660", 2, 2, 2, 2, 2, None)], source="pykrx")
        st.upsert_prices("035720", pd.DataFrame(
            {"시가": [3.0], "고가": [3.0], "저가": [3.0], "종가": [3.0],
             "거래량": [3.0]}, index=pd.to_datetime([DAY])), allow_today=True)
        assert _row(st, "035720")[6] == "pykrx", "기본값은 기존과 같다"
        from stocknews import universe as uni
        assert 'source="fdr_snapshot"' in inspect.getsource(uni.fetch_day)
        assert 'source="naver"' in inspect.getsource(uni._upsert_one_day)
        assert 'source="naver"' not in inspect.getsource(uni.backfill_one)
    check(S, "잠정 source = 적재 경로 (fdr_snapshot / naver · 백필·기본 pykrx)",
          t_source_labels)

    def t_scoring_reads_confirmed():
        from stocknews.events import PricePanel
        st = Store(tmp / "panel.db")
        _seed(st, [("005930", 100, 100, 100, 100, 10, None)], day=PREV)
        _seed(st, [("005930", 100, 110, 99, 108, 10, None)])
        with closing(sqlite3.connect(st.path)) as con:
            before = tuple(con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                           for t in ("scans", "recos"))
        reconcile_day(st, DAY, _krx({"005930": (100, 110, 99, 105, 12, 1e3)}))
        pp = PricePanel.from_store(st, calendar=[PREV, DAY])
        assert float(pp.close.loc[DAY, "005930"]) == 105.0, pp.close
        assert float(pp.open.loc[DAY, "005930"]) == 100.0
        with closing(sqlite3.connect(st.path)) as con:
            after = tuple(con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                          for t in ("scans", "recos"))
        assert before == after, "보정이 scans/recos 를 건드렸다"
    check(S, "채점 패널은 보정 후 확정 종가를 읽음 · scans/recos 불변",
          t_scoring_reads_confirmed)

    # ── 모드 · nightly ──
    mod = importlib.import_module("run_screen")

    def _run_mode(argv, fake, db, dry=True):
        saved = mod.krx_daily_prices
        mod.krx_daily_prices = fake
        so, se = io.StringIO(), io.StringIO()
        extra = ["--dry-run"] if dry else []
        try:
            with contextlib.redirect_stdout(so), contextlib.redirect_stderr(se):
                rc = mod.main(argv + ["--db", str(db), "--no-lock", "--json",
                                      "--env-file", str(tmp / "absent.env")]
                              + extra)
        finally:
            mod.krx_daily_prices = saved
        return rc, json.loads(so.getvalue().strip().splitlines()[-1])

    def t_target_is_previous_published_day():
        st = Store(tmp / "target.db")
        a = types.SimpleNamespace(date=None)
        tgt = lambda now: mod._reconcile_target(st, a, now=now)
        # 수 21:30 -> 화 · 월 21:30 -> 금 · 수 00:30 catch-up -> 월 (화는 수 08:00 공개)
        assert tgt(datetime(2026, 11, 11, 21, 30)) == "2026-11-10"
        assert tgt(datetime(2026, 11, 9, 21, 30)) == "2026-11-06"
        assert tgt(datetime(2026, 11, 11, 0, 30)) == "2026-11-09"
        assert tgt(datetime(2026, 11, 11, 8, 0)) == "2026-11-10"
        # 토 01:00 catch-up (금 회차) -> 목 (금 공개는 월 08:00)
        assert tgt(datetime(2026, 11, 14, 1, 0)) == "2026-11-12"
        # 기록된 휴장일(월)은 건너뛴다: 화 21:30 -> 금
        st.mark_non_trading_day("2026-11-09", "test")
        assert tgt(datetime(2026, 11, 10, 21, 30)) == "2026-11-06"
        assert C.KRX_PUBLISH_HOUR == 8
    check(S, "기준일 = 공개된 전 거래일 (catch-up 자정 넘김 · 휴장 건너뜀)",
          t_target_is_previous_published_day)

    def t_no_key_is_expected_skip():
        db = tmp / "nokey.db"
        st = Store(db)
        _seed(st, [("005930", 10, 10, 10, 10, 1, None)])

        def nokey(bas_dd, key=None, timeout=30.0, report=None):
            report.update({"status": "NOKEY"})
            return None
        rc, js = _run_mode(["--mode", "reconcile", "--date", DAY], nokey, db)
        assert rc == mod.EXIT_PRECOND and js["reason"] == "no_api_key", js
        assert _row(st, "005930")[7] == 0
        nl = importlib.import_module("nightly")
        step = nl.STEPS[0]
        st_skip = nl._judge(step, 4, "", {"reason": "no_api_key"})[0]
        st_np = nl._judge(step, 4, "", {"reason": "not_published"})[0]
        st_adj = nl._judge(step, 4, "", {"reason": "adjusted_price_suspect"})[0]
        st_fail = nl._judge(step, 1, "", {"reason": "error"})[0]
        assert (st_skip, st_np, st_adj, st_fail) == (
            "skip", "fail", "fail", "fail"), (st_skip, st_np, st_adj, st_fail)
    check(S, "키 없음 = exit 4 기대된 스킵 · 미공개·수정주가 중단·호출 실패 = 실패",
          t_no_key_is_expected_skip)

    def t_steps_first():
        nl = importlib.import_module("nightly")
        first = nl.STEPS[0]
        assert first.name == "reconcile", [s.name for s in nl.STEPS]
        assert first.args == ["--mode", "reconcile"], first.args
        assert [s.name for s in nl.STEPS][1] == "master"
        assert "reconcile" in nl._SENDING
        args = nl._build_args(first, dry=True)
        assert "--dry-run" in args and "--json" in args, args
        assert all(s.name != "reconcile" for s in nl.SEND_DAY_STEPS)
        assert "reconcile" in nl._NIGHTLY_ONLY_MODES
    check(S, "nightly STEPS 첫 단계 = reconcile (dry 면 --dry-run · 일요일 단계 없음)",
          t_steps_first)

    def t_admin_only_send():
        db = tmp / "admin.db"
        st = Store(db)
        _seed(st, [("005930", 10, 12, 9, 11, 100, None)])
        from stocknews import notify
        sent = []

        class _Ok:
            failures: list = []

            def __bool__(self):
                return True

        def boom(*a, **k):
            raise AssertionError("전 수신자 발송 경로를 탔다")

        def fake(bas_dd, key=None, timeout=30.0, report=None):
            report.update({"status": "DATA", "rows": {}, "elapsed_sec": 0.1})
            return _krx({"005930": (10, 12, 9, 10, 110, 1100)})
        saved = (notify.send_admin, mod.send_telegram)
        notify.send_admin = lambda text, dry_run=False, token=None: (
            sent.append(text) or _Ok())
        mod.send_telegram = boom
        try:
            rc, js = _run_mode(["--mode", "reconcile", "--date", DAY], fake, db,
                               dry=False)
        finally:
            notify.send_admin, mod.send_telegram = saved
        assert rc == 0, js
        assert len(sent) == 1 and sent[0].startswith("🔄 시세 보정 11-10"), sent
        assert js["confirmed"] == 1 and js["skipped_split"] == 0, js
    check(S, "보정 결과 알림은 관리자 채팅에만 (send_admin)", t_admin_only_send)
