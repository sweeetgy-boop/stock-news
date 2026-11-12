# -*- coding: utf-8 -*-
"""v2 새 명령·모드 종료 코드 — 합성 DB 에서 run_screen.main() 을 실제로 부른다.

왜 필요한가: 병합 중 `mode_reco_events_backfill` 의 `return EXIT_OK` 가
빠져 None 을 돌리는 상태가 커밋됐는데(C 병합), 함수 단위 검사는 전부
통과했다. 모드를 끝까지 태워 **종료 코드**를 보는 검사가 없었기 때문이다.

네트워크 없음: 지수는 `events.load_index_history` 를 합성 지수로 바꾼다
(모든 v2 모듈이 이 함수 하나로 지수를 받는다).
"""
from __future__ import annotations

import contextlib
import io
import json
import sqlite3
from contextlib import closing
from datetime import date, timedelta

import numpy as np
import pandas as pd

S = "v2 모드 종료 코드"


def _cal(n: int, start: str = "2025-01-02") -> list[str]:
    out, cur = [], date.fromisoformat(start)
    while len(out) < n:
        if cur.weekday() < 5:
            out.append(cur.isoformat())
        cur += timedelta(days=1)
    return out


def run(check, near, tmp):
    import importlib
    from stocknews import events as ev
    from stocknews.store import Store

    rng = np.random.default_rng(20261112)
    days = _cal(320)
    db = tmp / "modes.db"
    st = Store(db)
    tk, rows = [], []
    for j in range(40):
        t = f"{j + 100:06d}"
        tk.append({"ticker": t, "name": f"종목{j}",
                   "market": "KOSPI" if j % 2 else "KOSDAQ",
                   "sector": f"업종{j % 5}", "market_cap": 1e11 * (j + 1),
                   "shares": 1e7})
        px = 10000.0 + 100 * j
        for d in days:
            px *= 1 + rng.normal(scale=0.015)
            rows.append((t, d, px, px * 1.01, px * 0.99, px, 2e5, px * 2e5))
    st.upsert_tickers(tk)
    with closing(sqlite3.connect(db)) as con:
        con.executemany("INSERT INTO prices(ticker,d,o,h,l,c,v,amt) "
                        "VALUES(?,?,?,?,?,?,?,?)", rows)
        slots = ("VALUE", "TREND", "SEQ", "FILL")
        for k, d in enumerate(days[-60::3]):
            for r in range(4):
                t = f"{(k * 3 + r) % 40 + 100:06d}"
                con.execute(
                    "INSERT INTO recos(d,rank,ticker,name,price,slot,grade,"
                    "value_score,trend_score,reason) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (d, r + 1, t, "x", 10000.0, slots[r], "A", 7.0, 8.5, ""))
                con.execute(
                    "INSERT OR IGNORE INTO scans(d,ticker,name,price,value_score,"
                    "trend_score,grade,track) VALUES(?,?,?,?,?,?,?,?)",
                    (d, t, "x", 10000.0, 7.0, 8.5, "A", "TREND"))
        con.commit()

    idx = pd.DataFrame({"Open": np.linspace(2500, 2700, len(days)),
                        "Close": np.linspace(2505, 2705, len(days))}, index=days)
    fake_index = ev.index_frames_from_df({"KOSPI": idx, "KOSDAQ": idx / 3})

    holdings = tmp / "holdings.csv"
    holdings.write_text("code,qty,avg_price\n000100,10,10000\n000101,5,10000\n",
                        encoding="utf-8")
    logs = tmp / "logs"
    logs.mkdir(exist_ok=True)
    empty_env = tmp / "empty.env"
    empty_env.write_text("", encoding="utf-8")

    rs = importlib.import_module("run_screen")
    orig = ev.load_index_history

    def call(*argv) -> tuple[int, dict]:
        ev.load_index_history = lambda *a, **k: dict(fake_index)
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out), \
                    contextlib.redirect_stderr(io.StringIO()):
                rc = rs.main(["--db", str(db), "--dry-run", "--json",
                              "--env-file", str(empty_env), *argv])
        finally:
            ev.load_index_history = orig
        lines = [x for x in out.getvalue().splitlines() if x.startswith("{")]
        js = json.loads(lines[-1]) if lines else {}
        return rc, js

    # (이름, argv, 기대 종료 코드). 순서 = 실제 첫 실행 순서.
    cases = [
        ("regime-backfill", ["--mode", "regime-backfill"], 0),
        ("reco-events-backfill", ["--mode", "reco-events-backfill"], 0),
        ("risk-backfill", ["--mode", "risk-backfill"], 0),
        ("indicators-backfill", ["--mode", "indicators-backfill"], 0),
        ("hurst-backfill", ["--mode", "hurst-backfill"], 0),
        ("paper --backfill", ["--mode", "paper", "--backfill",
                              "--sent-log-dir", str(logs)], 0),
        ("paper", ["--mode", "paper"], 0),
        ("rebalance", ["--mode", "rebalance", "--holdings", str(holdings)], 0),
        ("weekly --weekly-v2", ["--mode", "weekly", "--weekly-v2"], 0),
    ]
    for name, argv, want in cases:
        def one(argv=argv, want=want, name=name):
            rc, js = call(*argv)
            assert rc is not None, f"{name}: 모드가 None 을 돌림 (return 누락)"
            assert isinstance(rc, int), f"{name}: 종료 코드가 int 아님 {rc!r}"
            assert rc == want, f"{name}: rc={rc} (기대 {want}) json={str(js)[:300]}"
            assert js.get("exit_code") == rc, f"{name}: JSON exit_code {js.get('exit_code')} ≠ rc {rc}"
        check(S, f"{name} → rc {want}", one)

    def all_modes_return_int():
        # 등록된 모든 모드 핸들러가 정의상 return 을 갖는가(정적 검사).
        import ast
        import inspect
        for mode, fn in rs.MODES.items():
            src = inspect.getsource(fn)
            tree = ast.parse(src.lstrip() if src.startswith(" ") else src)
            body = tree.body[0].body
            last = body[-1]
            ok = isinstance(last, (ast.Return, ast.Raise)) or (
                isinstance(last, ast.If) and last.orelse)
            assert ok or any(isinstance(n, ast.Return) for n in ast.walk(tree)), \
                f"{mode}: return 문이 없음"
            assert isinstance(last, (ast.Return, ast.Raise, ast.If, ast.Try,
                                     ast.With)), \
                f"{mode}: 마지막 문장이 return 이 아님 ({type(last).__name__})"
    check(S, "등록된 모든 모드 핸들러의 마지막 문장 = return", all_modes_return_int)
