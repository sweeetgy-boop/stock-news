# -*- coding: utf-8 -*-
"""회차 날짜 · 장중 catch-up · 늦은 브리핑 (R3-1).

2026-11-12 실측: 11-11 21:30 nightly 가 누락돼 11-12 13:32 장중에 catch-up
으로 떴다. 마커가 실행일(11-12)로 박혀 그날 밤 정시 회차가 스킵됐고,
update 는 당일분을 2,560종목 헛조회한 뒤 '소스 장애' rc=2 를 냈다.

시각은 전부 주입한다(네트워크 없음 · 실발송 없음).
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import sys
from datetime import date, datetime
from pathlib import Path

import pandas as pd

S = "회차 날짜·장중 catch-up"
REPO = Path(__file__).resolve().parents[1]


def _load_nightly():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_nightly_s91", str(REPO / "nightly.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.modules.pop(spec.name, None)
    return mod


def run(check, near, tmp):
    nl = _load_nightly()
    from stocknews import universe as uni
    from stocknews.store import Store
    import run_screen as rs

    def drive(name, clock, job="nightly", argv_extra=(), runs=None):
        """nightly.main 을 시계 고정으로 태운다. 같은 root 를 이어 쓰면 마커가 남는다."""
        root = tmp / f"s91_{name}"
        root.mkdir(parents=True, exist_ok=True)
        out = {"ran": [], "sent": [], "admin": [], "when": []}

        def fake_step(step, dry, log, timeout):
            out["ran"].append(nl._build_args(step, dry))
            return {"name": step.name, "rc": 0, "state": "ok", "note": "",
                    "detail": "", "elapsed": 0.0,
                    "json": {"rows": 2400} if step.name == "update" else {}}

        def fake_status(store, when):
            out["when"].append(nl.as_date(when).isoformat())
            return nl.CLOSED_WEEKEND if nl.as_date(when).weekday() >= 5 else "OPEN"

        keys = ("REPO", "load_env", "market_status", "_run_step", "_notify",
                "_notify_admin", "_net_probe", "now_kst")
        saved = {k: getattr(nl, k) for k in keys}
        try:
            nl.REPO = root
            nl.load_env = lambda *a, **k: {"exists": True, "path": ""}
            nl.market_status = fake_status
            nl._run_step = fake_step
            nl._notify = lambda text, log, enabled, why="": (
                out["sent"].append(text), True)[1]
            nl._notify_admin = lambda text, log, enabled: (
                out["admin"].append(text), True)[1]
            nl._net_probe = lambda: None
            nl.now_kst = lambda: clock
            argv = ["--job", job, "--db", str(root / "q.db"),
                    "--lock-dir", str(root / "locks"), *argv_extra]
            with contextlib.redirect_stderr(io.StringIO()):
                out["rc"] = nl.main(argv)
        finally:
            for k, v in saved.items():
                setattr(nl, k, v)
        mp = root / nl.JOBS[job].marker_path
        out["marker"] = (json.loads(mp.read_text(encoding="utf-8"))
                         if mp.exists() else {})
        out["log"] = "".join(p.read_text(encoding="utf-8")
                             for p in (root / "logs").glob("*.log"))
        return out

    def expected_args(job="nightly"):
        # 정시 회차의 단계 인자. 수정 전과 같은 상수(STEPS)와 같은 dry 판정.
        dry = os.getenv("STOCKNEWS_SEND") != "1"
        j = nl.apply_dry_jobs(nl.JOBS[job],
                              nl.dry_jobs_from_env(os.getenv(nl.DRY_JOBS_ENV)))
        return [nl._build_args(s, dry) for s in j.steps]

    def expected_plain():
        return expected_args("nightly")

    # ── ① 11-11 누락 → 11-12 13:32 장중 catch-up → 같은 날 21:30 정상 ──
    def missed_then_tonight():
        a = drive("miss", datetime(2026, 11, 12, 13, 32, 9))
        assert a["rc"] == 0, a["rc"]
        assert a["marker"]["date"] == "2026-11-11", a["marker"]
        assert a["when"] == ["2026-11-11"], a["when"]          # 휴장 판정 = 회차
        assert [x[1] for x in a["ran"]] == [s.args[1] for s in nl.STEPS]
        by = {x[1]: x for x in a["ran"]}
        for m in ("daily", "exits"):
            assert by[m][by[m].index("--asof") + 1] == "2026-11-11", by[m]
        for m in ("master", "update", "flags", "news"):
            assert "--asof" not in by[m], by[m]
        assert len(a["admin"]) == 1, a["admin"]
        line = a["admin"][0]
        assert "\n" not in line and "11-11 21:30 회차" in line, line
        assert "16.0시간" in line and "장중" in line, line
        assert not any("보충 실행" in t for t in a["sent"]), "보충 알림이 전 수신자에게"
        # 같은 날 21:30 정시 회차 — 스킵되지 않는다
        b = drive("miss", datetime(2026, 11, 12, 21, 30, 3))
        assert b["rc"] == 0, b["rc"]
        assert b["ran"] == expected_plain(), b["ran"]
        assert b["marker"]["date"] == "2026-11-12", b["marker"]
        assert b["admin"] == [] and "이미 실행" not in b["log"], b["log"][-400:]
        # 같은 회차(11-11)의 catch-up 이 또 떠도 한 번만 돈다
        c = drive("miss_dup", datetime(2026, 11, 12, 13, 32))
        d = drive("miss_dup", datetime(2026, 11, 12, 13, 40))
        assert len(c["ran"]) == len(nl.STEPS) and d["ran"] == [], d["ran"]
        assert "11/11 21:30 회차 이미 실행" in d["sent"][-1], d["sent"]
    check(S, "11-11 누락 → 11-12 13:32 catch-up(마커·판정 11-11) → 21:30 정상 실행",
          missed_then_tonight)

    def on_time_unchanged():
        a = drive("ontime", datetime(2026, 11, 12, 21, 30, 4))
        assert a["ran"] == expected_plain(), a["ran"]
        assert a["when"] == ["2026-11-12"] and a["marker"]["date"] == "2026-11-12"
        assert a["admin"] == [] and "catch-up" not in a["log"]
        assert len(a["sent"]) == 1 and "보충" not in a["sent"][0]
        # 정시 브리핑·주간도 그대로
        for job, clock in (("brief-evening", datetime(2026, 11, 12, 23, 0, 2)),
                           ("brief-morning", datetime(2026, 11, 12, 6, 20, 1)),
                           ("weekly", datetime(2026, 11, 13, 22, 45, 1))):
            o = drive(f"ontime_{job}", clock, job=job)
            assert o["ran"] == expected_args(job), (job, o["ran"])
            assert o["marker"]["date"] == f"{clock:%Y-%m-%d}", (job, o["marker"])
    check(S, "정시 실행(21:30·23:00·06:20·금 22:45): 단계·인자·마커 날짜 불변",
          on_time_unchanged)

    def friday_slot_on_saturday():
        a = drive("fri_sat", datetime(2026, 11, 14, 9, 5))
        assert a["when"] == ["2026-11-13"], a["when"]
        assert len(a["ran"]) == len(nl.STEPS), "금요일 회차가 토요일에 통째로 스킵"
        assert a["marker"]["date"] == "2026-11-13"
        daily = next(x for x in a["ran"] if x[1] == "daily")
        assert "2026-11-13" in daily, daily
        # 토요일 회차를 일요일 낮에 → 휴장 스킵(토), 그날 밤 일요일 발송은 돈다
        b = drive("sat_sun", datetime(2026, 11, 15, 17, 36))
        assert b["ran"] == [] and b["marker"]["date"] == "2026-11-14", b
        c = drive("sat_sun", datetime(2026, 11, 15, 21, 30, 2))
        assert [x[:3] for x in c["ran"]] == [["--mode", "daily", "--send-only"]]
        assert "2026-11-15" in c["ran"][0] and c["marker"]["date"] == "2026-11-15"
        # 일요일 회차를 월요일 새벽에 → 발송 단계(--asof 일요일)
        d = drive("sun_mon", datetime(2026, 11, 16, 1, 0))
        assert len(d["ran"]) == 1 and d["ran"][0][:3] == ["--mode", "daily",
                                                          "--send-only"], d["ran"]
        assert d["ran"][0][d["ran"][0].index("--asof") + 1] == "2026-11-15"
        assert d["ran"][0].count("--asof") == 1, d["ran"][0]
    check(S, "금→토·토→일·일→월 catch-up: 회차 날짜로 휴장 판정·--asof", friday_slot_on_saturday)

    def closed_guard_asof():
        st = Store(tmp / "s91_guard.db")
        ns = argparse.Namespace(force=False, asof=date(2026, 11, 13))   # 금
        assert rs._closed_guard(st, ns, "daily") is None
        ns.asof = date(2026, 11, 14)                                    # 토
        assert "CLOSED_WEEKEND" in (rs._closed_guard(st, ns, "exits") or "")
    check(S, "_closed_guard: --asof 가 있으면 그 날짜로 휴장 판정", closed_guard_asof)

    # ── ② 장 마감 직후(15:45) ──
    def right_after_close():
        a = drive("close", datetime(2026, 11, 12, 15, 45))
        assert a["marker"]["date"] == "2026-11-11" and len(a["ran"]) == len(nl.STEPS)
        assert a["admin"] and "장중" not in a["admin"][0], a["admin"]
        b = drive("close", datetime(2026, 11, 12, 21, 30, 1))
        assert b["ran"] == expected_plain(), "15:45 보충 뒤 21:30 회차가 스킵됨"
    check(S, "장 마감 직후 15:45 catch-up → 그날 21:30 정상", right_after_close)

    # ── ③ 늦은 브리핑 ──
    def late_brief():
        a = drive("late_ev", datetime(2026, 11, 13, 2, 30), job="brief-evening")
        assert a["rc"] == 0 and a["ran"] == [] and a["sent"] == [], a
        assert a["marker"]["date"] == "2026-11-12", a["marker"]
        assert a["marker"]["reason"].startswith("건너뜀"), a["marker"]
        assert "건너뜀" in a["log"] and "발송하지 않음" in a["log"]
        # 같은 날(11-13) 23:00 정시 회차는 막지 않는다
        b = drive("late_ev", datetime(2026, 11, 13, 23, 0, 1), job="brief-evening")
        assert len(b["ran"]) == 1 and b["marker"]["date"] == "2026-11-13", b
        # 2시간 지연은 보낸다
        c = drive("ev_2h", datetime(2026, 11, 13, 1, 0), job="brief-evening")
        assert len(c["ran"]) == 1 and c["sent"], c
        assert c["marker"]["date"] == "2026-11-12" and c["admin"] == [], c
        # 아침 브리핑 06:20 → 장중 10:00 (3.7시간) 은 건너뜀
        d = drive("late_mo", datetime(2026, 11, 12, 10, 0), job="brief-morning")
        assert d["rc"] == 0 and d["ran"] == [] and d["sent"] == []
        assert d["marker"]["date"] == "2026-11-12"
        # 이미 끝낸 회차의 마커는 덮어쓰지 않는다
        root = tmp / "s91_late_done"
        root.mkdir(parents=True, exist_ok=True)
        mp = root / nl.JOBS["brief-evening"].marker_path
        mp.parent.mkdir(parents=True, exist_ok=True)
        mp.write_text(json.dumps({"date": "2026-11-12", "reason": "완주"}),
                      encoding="utf-8")
        e = drive("late_done", datetime(2026, 11, 13, 3, 0), job="brief-evening")
        assert e["rc"] == 0 and e["marker"]["reason"] == "완주", e["marker"]
        # nightly 는 지연 상한이 없다
        assert not nl.too_late(nl.JOBS["nightly"], 16 * 3600)
        assert nl.too_late(nl.JOBS["brief-evening"], 3.5 * 3600)
        assert not nl.too_late(nl.JOBS["brief-evening"], 2.0 * 3600)
    check(S, "브리핑 3.5시간 지연 = 미발송·건너뜀·rc 0 · 2시간 지연은 발송", late_brief)

    # ── 관리자 알림: 관리자 채팅만 · --no-telegram 이면 생략 ──
    def admin_only():
        from stocknews import notify as nt
        calls = []
        saved = nt.send_admin
        logs = []
        try:
            nt.send_admin = lambda text, dry_run=False, token=None: (
                calls.append(text), nt.SendReport(sent=1))[1]
            assert nl._notify_admin("x", logs.append, True) is True
            assert nl._notify_admin("y", logs.append, False) is None
        finally:
            nt.send_admin = saved
        assert calls == ["x"], calls
        line = nl.catch_up_line(nl.JOBS["nightly"], datetime(2026, 11, 11, 21, 30),
                                16 * 3600, datetime(2026, 11, 12, 13, 32), True)
        assert "\n" not in line and "11-11 21:30" in line
    check(S, "보충 실행 알림 = send_admin 한 줄 (--no-telegram 이면 생략)", admin_only)

    # ── ④⑤ fetch_day ──
    st = Store(tmp / "s91_fetch.db")
    st.active_tickers = lambda: {"000001": "가", "000002": "나"}

    def patched_fetch(clock, ymd):
        calls = {"snap": 0, "probe": [], "close": [], "upsert": []}
        keys = ("_now_kst", "market_snapshot", "probe_refs", "close_on",
                "_upsert_one_day", "verify_snapshot_date")
        saved = {k: getattr(uni, k) for k in keys}
        try:
            uni._now_kst = lambda: clock
            uni.market_snapshot = lambda: (calls.__setitem__("snap", calls["snap"] + 1),
                                           (pd.DataFrame(), pd.DataFrame()))[1]
            uni.verify_snapshot_date = lambda p, td: (False, "빈 스냅샷")
            uni.probe_refs = lambda td, retries=0: (calls["probe"].append(td),
                                                    ("TRADING", "ok"))[1]
            uni.close_on = lambda c, td: (calls["close"].append(td), 100.0)[1]
            uni._upsert_one_day = lambda store, c, td, iso: (
                calls["upsert"].append(iso), 1)[1]
            rep = {}
            n = uni.fetch_day(st, ymd, report=rep, throttle=0.0)
        finally:
            for k, v in saved.items():
                setattr(uni, k, v)
        return n, rep, calls

    def today_pending():
        for clock in (datetime(2026, 11, 12, 13, 32), datetime(2026, 11, 12, 0, 30),
                      datetime(2026, 11, 12, 15, 45)):
            n, rep, calls = patched_fetch(clock, "20261112")
            assert n == 0 and rep["verdict"] == "PENDING", (clock, rep)
            assert rep["path"] == "in_progress", rep
            assert calls == {"snap": 0, "probe": [], "close": [], "upsert": []}, calls
        assert not st.is_known_non_trading("2026-11-12")
        # 마감 정리(15:50) 뒤에는 평소대로 묻는다
        n, rep, calls = patched_fetch(datetime(2026, 11, 12, 21, 30), "20261112")
        assert calls["snap"] == 1 and calls["probe"] == ["20261112"], calls
    check(S, "fetch_day 당일·장 마감 전 = 즉시 PENDING (스냅샷·프로브·종목 조회 0회)",
          today_pending)

    def intraday_past_snapshot_skipped():
        n, rep, calls = patched_fetch(datetime(2026, 11, 12, 13, 32), "20261111")
        assert calls["snap"] == 0, "장중에 지난 날짜 스냅샷을 불렀다"
        assert calls["probe"] == ["20261111"] and n == 2, (calls, n)
        assert rep["path"] == "per_ticker", rep
        # 장 마감 뒤(15:45, 사용자 정의 장중 15:40 끝)에는 스냅샷 경로를 쓴다
        _n, _rep, calls2 = patched_fetch(datetime(2026, 11, 12, 15, 45), "20261111")
        assert calls2["snap"] == 1, calls2
        # 주말 낮은 장중이 아니다
        _n, _rep, calls3 = patched_fetch(datetime(2026, 11, 14, 11, 0), "20261113")
        assert calls3["snap"] == 1, calls3
    check(S, "장중(거래일 09:00~15:40) 지난 날짜 스냅샷 생략 · 15:45·주말은 사용",
          intraday_past_snapshot_skipped)

    def update_intraday_catchup():
        """mode_update 전체: 장중 catch-up 은 직전 거래일만 받고 당일은 보류(rc 0)."""
        keys_u = ("_now_kst", "market_snapshot", "probe_refs", "close_on",
                  "_upsert_one_day")
        saved_u = {k: getattr(uni, k) for k in keys_u}
        saved_rs = (rs.now_kst, dict(rs.SUMMARY))
        probes = []
        clock = datetime(2026, 11, 12, 13, 32)
        try:
            uni._now_kst = lambda: clock
            uni.market_snapshot = lambda: (_ for _ in ()).throw(
                AssertionError("장중 스냅샷 호출"))
            uni.probe_refs = lambda td, retries=0: (probes.append(td),
                                                    ("TRADING", "ok"))[1]
            uni.close_on = lambda c, td: 100.0
            uni._upsert_one_day = lambda store, c, td, iso: 1
            rs.now_kst = lambda: clock
            rs.SUMMARY.clear()
            rc = rs.mode_update(st, argparse.Namespace(catchup=1))
            summ = dict(rs.SUMMARY)
        finally:
            for k, v in saved_u.items():
                setattr(uni, k, v)
            rs.now_kst = saved_rs[0]
            rs.SUMMARY.clear()
            rs.SUMMARY.update(saved_rs[1])
        assert rc == rs.EXIT_OK, (rc, summ)
        assert probes == ["20261111"], probes                 # 당일분은 묻지도 않음
        assert summ["judge_pending"] == ["2026-11-12"], summ
        assert summ["source_outage"] == [], summ
        st_, note = nl._health_override(nl.STEPS[1], "ok", "", summ)
        assert st_ == "ok", note                              # '소스 장애' 오보 없음
    check(S, "update 장중 catch-up: 직전 거래일만 · 당일 보류 · 소스 장애 아님 · rc 0",
          update_intraday_catchup)
