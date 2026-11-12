# -*- coding: utf-8 -*-
"""[0] 이벤트 판정·채점 — 가상 데이터 검사.

경계: 20일째(같은 이벤트) / 21일째(새 이벤트), 기간 중 슬롯 변경, 기간 중
목록 이탈 후 재등장, 휴장일 낀 구간, 거래정지·시가 없음·상한가 시가,
보유 중 거래정지, 수정주가 불연속, 미도래, 산술 앵커.
"""
from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd

S = "v2[0] 이벤트·채점"


def _cal(n: int, start: str = "2026-09-01", skip: tuple = ()) -> list[str]:
    out, cur = [], date.fromisoformat(start)
    while len(out) < n:
        s = cur.isoformat()
        if cur.weekday() < 5 and s not in skip:
            out.append(s)
        cur += timedelta(days=1)
    return out


def _recos(rows):
    return pd.DataFrame(rows, columns=["d", "ticker", "slot", "grade", "rank"])


def run(check, near, tmp):
    from stocknews import events as ev
    from stocknews import config as C

    cal = _cal(60)

    def boundary():
        r = _recos([(cal[0], "A", "VALUE", "A", 1),
                    (cal[19], "A", "VALUE", "A", 1),     # 20일째
                    (cal[20], "A", "VALUE", "A", 1)])    # 21일째
        a = ev.assign_events(r, cal, window=20)
        got = a.set_index("d")
        assert bool(got.at[cal[0], "is_event"]) is True
        assert bool(got.at[cal[19], "is_event"]) is False, "20일째는 같은 이벤트"
        assert int(got.at[cal[19], "day_in_window"]) == 20
        assert got.at[cal[19], "event_date"] == cal[0]
        assert bool(got.at[cal[20], "is_event"]) is True, "21일째는 새 이벤트"
        assert got.at[cal[0], "window_end"] == cal[19]
        assert len(ev.events_only(a)) == 2
    check(S, "20일째 같은 이벤트 · 21일째 새 이벤트", boundary)

    def no_extend():
        # 매일 추천돼도 기간을 연장하지 않는다 → 0, 20, 40 일째가 이벤트
        r = _recos([(cal[i], "A", "TREND", "A", 1) for i in range(45)])
        e = ev.events_only(ev.assign_events(r, cal, window=20))
        assert list(e["d"]) == [cal[0], cal[20], cal[40]], list(e["d"])
    check(S, "기간 중 연속 추천은 기간 연장 안 함", no_extend)

    def slot_change():
        r = _recos([(cal[0], "B", "VALUE", "B", 3),
                    (cal[5], "B", "TREND", "A", 2)])
        a = ev.assign_events(r, cal, window=20).set_index("d")
        assert not a.at[cal[5], "is_event"]
        assert a.at[cal[5], "event_slot"] == "VALUE"
        assert bool(a.at[cal[5], "slot_changed"]) and bool(a.at[cal[5], "grade_changed"])
        assert not a.at[cal[0], "slot_changed"]
    check(S, "기간 중 슬롯·등급 변경 = 같은 이벤트 + 변경 표시", slot_change)

    def drop_reappear():
        r = _recos([(cal[0], "C", "SEQ", "A", 1),
                    (cal[1], "C", "SEQ", "A", 1),
                    (cal[12], "C", "SEQ", "A", 1)])     # 이탈 후 재등장
        a = ev.assign_events(r, cal, window=20).set_index("d")
        assert not a.at[cal[12], "is_event"]
        assert int(a.at[cal[12], "day_in_window"]) == 13
    check(S, "기간 중 목록 이탈 후 재등장 = 같은 이벤트", drop_reappear)

    def holiday():
        hol = ("2026-09-24", "2026-09-25")              # 평일 휴장 2일
        c2 = _cal(40, skip=hol)
        r = _recos([(c2[0], "D", "VALUE", "A", 1), (c2[20], "D", "VALUE", "A", 1)])
        a = ev.assign_events(r, c2, window=20).set_index("d")
        # 달력일로는 28일 이상 지났지만 거래일로 21일째 → 새 이벤트
        assert bool(a.at[c2[20], "is_event"])
        gap = (date.fromisoformat(c2[19]) - date.fromisoformat(c2[0])).days
        assert gap > 25, gap
        assert a.at[c2[0], "window_end"] == c2[19]
        # 달력 끝을 넘는 종료일은 주말·공휴일을 빼고 투영
        p = ev.project_trading_days("2026-10-02", 3, {"2026-10-05"})
        assert p == ["2026-10-06", "2026-10-07", "2026-10-08"], p
    check(S, "휴장일 낀 구간은 거래일로 센다 · 달력 밖 종료일 투영", holiday)

    def dedupe_and_zero():
        r = _recos([(cal[0], "E", "FILL", "A", 7), (cal[0], "E", "VALUE", "B", 2)])
        a = ev.assign_events(r, cal, window=20)
        assert len(a) == 1 and a.iloc[0]["slot"] == "VALUE"   # rank 작은 행
        r2 = _recos([(cal[i], "F", "VALUE", "A", 1) for i in (0, 1, 2)])
        assert ev.events_only(ev.assign_events(r2, cal, window=0)).shape[0] == 3
        assert ev.assign_events(_recos([]), cal).empty
    check(S, "같은 날 중복 행 1개 · window=0 은 제한 없음 · 빈 입력", dedupe_and_zero)

    # ── 채점 ──
    c = _cal(30)
    rows = []

    def put(t, i, o, cl):
        rows.append((c[i], t, o, cl))

    for i in range(30):
        put("A", i, 100.0, 100.0)                   # 기본 평탄
        put("M1", i, 100.0, 100.0 + i)              # 대조 종목들
        put("M2", i, 100.0, 100.0 - i * 0.5)
    rows = [x for x in rows if not (x[1] == "A" and x[0] in (c[5], c[9]))]
    put("A", 5, 100.0, 104.0)        # 진입일(이벤트 c[4] 다음날) 시가 100
    put("A", 9, 108.0, 110.0)        # 5일째(c[9]) 종가 110
    # 거래정지: S 는 진입일 행 없음
    for i in range(30):
        if i != 5:
            put("S", i, 50.0, 50.0)
    # 시가 없음: N 은 진입일 시가 NaN
    for i in range(30):
        put("N", i, np.nan if i == 5 else 20.0, 20.0)
    # 상한가 시가: L 전일 종가 100 → 시가 130
    for i in range(30):
        put("L", i, 130.0 if i == 5 else 100.0, 100.0 if i < 5 else 130.0)
    # 보유 중 거래정지: H 는 c[7] 이후 행 없음
    for i in range(8):
        put("H", i, 10.0, 10.0 + (1.0 if i == 7 else 0.0))
    # 수정주가 불연속: X 는 c[7] 에 종가 1/2 (분할 원시 가격)
    for i in range(30):
        put("X", i, 200.0 if i < 7 else 100.0, 200.0 if i < 7 else 100.0)
    df = pd.DataFrame(rows, columns=["d", "ticker", "o", "c"])
    panel = ev.PricePanel.from_frame(df, c)
    idx = ev.index_frames_from_df({"KOSPI": pd.DataFrame(
        {"Open": [1000.0] * 30, "Close": [1000.0] * 9 + [1050.0] + [1000.0] * 20},
        index=c)})
    mk = {t: "KOSPI" for t in ("A", "S", "N", "L", "H", "X", "M1", "M2")}
    evs = pd.DataFrame({"ticker": ["A", "S", "N", "L", "H", "X"],
                        "event_date": [c[4]] * 6})
    sc = ev.score_events(evs, panel, idx, mk, horizons=(5, 20, 60)).set_index("ticker")

    def anchor():
        a = sc.loc["A"]
        assert a["entry_date"] == c[5] and a["entry_status"] == "OK"
        near(a["entry_price"], 100.0, label="진입가")
        assert a["exit_date_5"] == c[9]
        near(a["ret_5"], 10.0, label="총수익")
        near(a["net_5"], 10.0 - C.ROUND_TRIP_COST_PCT, label="순수익")
        near(a["idx_5"], 5.0, label="지수")
        near(a["ex_idx_5"], 9.5 - 5.0, label="지수 대비")
        assert np.isfinite(a["med_5"]) and np.isclose(a["ex_med_5"], a["net_5"] - a["med_5"])
    check(S, "채점 앵커: 익일 시가 100 → 5일째 종가 110 · 비용 0.5 · 지수 +5", anchor)

    def entry_block():
        assert sc.loc["S", "entry_status"] == "SUSPENDED"
        assert sc.loc["N", "entry_status"] == "NO_OPEN"
        assert sc.loc["L", "entry_status"] == "LIMIT_UP"
        for t in ("S", "N", "L"):
            assert sc.loc[t, "status_5"] == "NO_ENTRY"
            assert np.isnan(sc.loc[t, "ret_5"])
    check(S, "진입 불가: 거래정지 · 시가 없음 · 상한가 시가", entry_block)

    def halted():
        h = sc.loc["H"]
        assert h["status_5"] == "HALTED", h["status_5"]
        assert h["exit_date_5"] == c[7]
        near(h["ret_5"], 10.0, label="마지막 거래 가격 11/10")
    check(S, "보유 중 거래정지 = 마지막 거래 가격으로 종료", halted)

    def adj_break():
        assert sc.loc["X", "status_5"] == "ADJ_BREAK"
        assert np.isnan(sc.loc["X", "ret_5"])
        # 불연속 종목은 전종목 중위에서도 빠진다
        e, x = panel.pos[c[5]], panel.pos[c[9]]
        o, cl = panel.open.iloc[e], panel.close.iloc[x]
        ok = ["A", "M1", "M2", "L"]          # L 은 정확히 +30% = 상한가, 불연속 아님
        exp = float(np.median([(cl[t] / o[t] - 1) * 100 for t in ok]))
        near(panel.universe_median(e, x), exp, label="중위")
    check(S, "수정주가 불연속 구간 제외 (채점·중위 모두)", adj_break)

    def pending():
        assert sc.loc["A", "status_20"] == "OK"
        assert sc.loc["A", "status_60"] == "PENDING"
        late = ev.score_events(pd.DataFrame({"ticker": ["A"], "event_date": [c[-1]]}),
                               panel, idx, mk, horizons=(5,))
        assert late.iloc[0]["entry_status"] == "PENDING"
        assert late.iloc[0]["status_5"] == "PENDING"
    check(S, "미도래는 PENDING (0 으로 채우지 않음)", pending)

    def cells():
        x = ev.stat_cell([1.0, -1.0, 2.0], min_n=20)
        assert x["n"] == 3 and not x["enough"]
        assert ev.fmt_cell(x).startswith("표본 부족")
        y = ev.stat_cell(list(range(-5, 20)), min_n=20)
        assert y["enough"] and abs(y["win"] - 19 / 25 * 100) < 1e-9
        assert C.SCORE_HORIZONS == (5, 10, 20, 40, 60) and C.EVENT_WINDOW_DAYS == 20
    check(S, "표 칸: 20건 미만 '표본 부족' · 설정값", cells)

    def schema_hook():
        from stocknews.store import Store
        from stocknews import v2_hooks as hk
        st = Store(tmp / "v2_0.db")
        assert hk.regime_on(st, "2026-09-01") is None
        assert hk.send_targets(st, "2026-09-01") is None
        assert hk.tracked_tickers(st, "2026-09-01") == set()
        Store(tmp / "v2_0.db")          # 재초기화 멱등
    check(S, "schema_v2 적용 멱등 · 훅 기본값 = 지금과 같은 동작", schema_hook)
