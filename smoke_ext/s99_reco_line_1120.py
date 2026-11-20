# -*- coding: utf-8 -*-
"""2026-11-20 저녁 브리핑 "오늘 기록된 추천 10" 한 문단 (DAILY_RECO_LINE).

그날 recos 전부 · 순위순 · 발송 필터와 무관 · ① 바로 위 · 기본 OFF ·
all = 브리핑 안(전체 수신자) / admin = 관리자 채팅에만 별도 메시지.
"""
from __future__ import annotations

import inspect
import sqlite3
from contextlib import closing
from datetime import date, timedelta

S = "11-20 기록된 추천 한 문단"
D_PREV, D_REF, D_NONE = -3, -2, -1      # 달력 끝에서 센 위치


def _cal(n: int, start: str = "2026-09-01") -> list[str]:
    out, cur = [], date.fromisoformat(start)
    while len(out) < n:
        if cur.weekday() < 5:
            out.append(cur.isoformat())
        cur += timedelta(days=1)
    return out


def _build(tmp):
    """12종목 × 40거래일. 기준일 recos 10건(순위 1~10), 그 전날 2건(재추천 제한
    대상), 마지막 날은 recos 없음. 이름 하나에 & 를 넣어 이스케이프를 본다."""
    from stocknews.store import Store
    cal = _cal(40)
    db = tmp / "reco_line.db"
    st = Store(db)
    tick = [f"7{i:05d}" for i in range(12)]
    names = {t: f"가상{i:02d}" for i, t in enumerate(tick)}
    names[tick[3]] = "삼성E&A"
    rows = []
    for j, t in enumerate(tick):
        for i, d in enumerate(cal):
            px = 10000.0 + 50 * j + 3 * i
            rows.append((t, d, px, px * 1.01, px * 0.99, px, 1e5, 1e9))
    slots = ("VALUE", "VALUE", "TREND", "TREND", "SEQ", "SEQ", "FILL",
             "VALUE", "TREND", "FILL*")
    order = [tick[k] for k in (5, 0, 9, 3, 7, 1, 11, 2, 8, 4)]   # 순위 ≠ 코드순
    rec = [(cal[D_REF], rk, t, names[t], 10000.0, slots[rk - 1], "A", 7.0, 5.0, "r")
           for rk, t in enumerate(order, 1)]
    rec += [(cal[D_PREV], rk, t, names[t], 10000.0, "VALUE", "A", 7.0, 5.0, "r")
            for rk, t in enumerate(order[:2], 1)]
    with closing(sqlite3.connect(db)) as con:
        con.executemany("INSERT INTO prices(ticker,d,o,h,l,c,v,amt) "
                        "VALUES(?,?,?,?,?,?,?,?)", rows)
        con.executemany(
            "INSERT INTO tickers(ticker,name,market,active) VALUES(?,?,?,1)",
            [(t, names[t], "KOSPI") for t in tick])
        con.executemany("INSERT INTO recos(d,rank,ticker,name,price,slot,grade,"
                        "value_score,trend_score,reason) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        rec)
        con.commit()
    return st, cal, order, names


def run(check, near, tmp):
    from stocknews import config as C
    from stocknews import paper as P
    from stocknews import paper_render as R
    from stocknews.renderer import stock_label

    st, cal, order, names = _build(tmp)
    d, d_none = cal[D_REF], cal[D_NONE]
    flags = ("DAILY_RECO_LINE", "EXCLUDE_SEQ_FROM_SEND", "RECO_COOLDOWN_DAYS")

    def _keep():
        return {n: getattr(C, n) for n in flags}

    def _back(snap):
        for n, v in snap.items():
            setattr(C, n, v)

    def _recos_dump():
        with closing(sqlite3.connect(st.path)) as con:
            return con.execute("SELECT * FROM recos ORDER BY d, rank").fetchall()

    def default_off():
        assert C.DAILY_RECO_LINE == "off" and R.reco_line_mode() == "off"
        assert C.DAILY_RECO_LINE_MODES[0] == "off"
        pdx = P.PaperData(st, d, index={})
        base, i0 = R.weekday_block(st, d, paper_on=False, pdata=pdx)
        same, i1 = R.weekday_block(st, d, paper_on=False, pdata=pdx,
                                   reco_line=False)
        assert base == same and "reco_line" not in i0 and "reco_line" not in i1
        assert "오늘 기록된 추천" not in base
        assert base.startswith("━━ ① 오늘 새 추천"), base[:40]
        snap = _keep()
        try:
            for bad in ("", None, "ALL ", "yes", 1, True):
                C.DAILY_RECO_LINE = bad
                want = "all" if bad == "ALL " else "off"
                assert R.reco_line_mode() == want, (bad, R.reco_line_mode())
        finally:
            _back(snap)
    check(S, "기본 OFF · OFF 면 종목 부분 텍스트 불변 · 모르는 값은 off", default_off)

    def block_shape():
        before = _recos_dump()
        lines, info = R.reco_line_block(st, d)
        assert len(lines) == 3, lines
        assert lines[0] == f"━━ 오늘 기록된 추천 10 (기준일 {d[5:]}) ━━", lines[0]
        assert lines[2] == "※ 순위순 · 기록용 목록 · 검증 중"
        parts = lines[1].split(" · ")
        assert parts == [stock_label(names[t], t) for t in order], parts
        assert "삼성E&amp;A(" in lines[1], "이스케이프 누락"
        assert "\n" not in lines[1] and "1." not in lines[1]
        assert info == {"d": d, "n": 10, "tickers": order}, info
        assert [r["rank"] for r in R.recorded_recos(st, d)] == list(range(1, 11))
        assert _recos_dump() == before, "recos 가 바뀌었다"
    check(S, "제목 · 순위순 10건 가운뎃점 한 문단 · 안내 한 줄 · recos 읽기 전용",
          block_shape)

    def above_section_one():
        pdx = P.PaperData(st, d, index={})
        base, _ = R.weekday_block(st, d, paper_on=False, pdata=pdx)
        snap = _keep()
        try:
            C.DAILY_RECO_LINE = "all"
            got, info = R.weekday_block(st, d, paper_on=False, pdata=pdx)
            C.DAILY_RECO_LINE = "admin"       # admin 은 블록에 넣지 않는다
            adm, ia = R.weekday_block(st, d, paper_on=False, pdata=pdx)
        finally:
            _back(snap)
        lines, _ = R.reco_line_block(st, d)
        head = "\n".join(lines)
        assert got == head + "\n\n" + base, "① 바로 위가 아니거나 나머지가 달라졌다"
        assert info["reco_line"]["n"] == 10
        assert info["reco_line"]["len"] == len(head)
        assert adm == base and "reco_line" not in ia
        on_p, ip = R.weekday_block(st, d, paper_on=True, pdata=pdx, reco_line=True)
        off_p, _ = R.weekday_block(st, d, paper_on=True, pdata=pdx, reco_line=False)
        assert on_p == head + "\n\n" + off_p
        # 길이 예산(③ 축소 판단)에 문단이 들어간다
        _, tight = R.weekday_block(st, d, paper_on=True, pdata=pdx, reco_line=True,
                                   limit_room=len(off_p))
        assert tight["topn"] == min(C.PAPER_SUMMARY_TOPN, C.PAPER_SUMMARY_MIN_TOPN)
    check(S, "all = ① 바로 위 · 나머지 텍스트 동일 · admin 은 블록에 없음",
          above_section_one)

    def filtered_still_listed():
        snap = _keep()
        try:
            C.EXCLUDE_SEQ_FROM_SEND = True
            C.RECO_COOLDOWN_DAYS = 20
            pdx = P.PaperData(st, d, index={})
            got, info = R.weekday_block(st, d, paper_on=False, pdata=pdx,
                                        reco_line=True)
        finally:
            _back(snap)
        sent = set(info["new_tickers"])
        dropped = [t for t in order if t not in sent]
        assert dropped, "필터로 빠진 종목이 없다 — 검사 전제가 깨졌다"
        assert info["reco_line"]["tickers"] == order
        para = got.split("\n")[1]
        for t in dropped:
            assert f"({t})" in para, f"발송에서 빠진 {t} 가 목록에 없다"
        sec1 = got.split("━━ ① 오늘 새 추천")[1]
        for t in dropped:
            assert f"({t})</b>" not in sec1.split("━━")[1], f"{t} 가 ① 에 남았다"
    check(S, "시퀀스 제외·재추천 제한으로 ① 에서 빠진 종목도 목록에는 그대로",
          filtered_still_listed)

    def empty_day():
        lines, info = R.reco_line_block(st, d_none)
        assert lines == ["오늘 기록된 추천 없음"] and info["n"] == 0, lines
        pdx = P.PaperData(st, d_none, index={})
        got, gi = R.weekday_block(st, d_none, paper_on=False, pdata=pdx,
                                  reco_line=True)
        assert got.startswith("오늘 기록된 추천 없음\n\n━━ ① 오늘 새 추천"), got[:60]
        assert gi["stale"] and gi["reco_line"]["tickers"] == []
    check(S, "그날 recos 없음 → '오늘 기록된 추천 없음' 한 줄", empty_day)

    def env_values():
        from stocknews.env import KNOWN_KEYS
        assert "DAILY_RECO_LINE" in KNOWN_KEYS
        assert C.ENV_OVERRIDE_FLAGS["DAILY_RECO_LINE"] is str
        assert C.ENV_FLAG_CHOICES["DAILY_RECO_LINE"] == ("off", "all", "admin")
        snap = _keep()
        try:
            for raw, want in (("all", "all"), ("ADMIN", "admin"), (" Admin ", "admin"),
                              ("off", "off"), ("0", "off"), ("false", "off")):
                C.DAILY_RECO_LINE = "sentinel"
                out = C.apply_env_flags({"DAILY_RECO_LINE": raw})
                assert C.DAILY_RECO_LINE == want and not out["invalid"], (raw, out)
                assert out["applied"] == {"DAILY_RECO_LINE": want}
            for raw in ("1", "true", "yes", "on", "everyone", "all,admin"):
                C.DAILY_RECO_LINE = "off"
                out = C.apply_env_flags({"DAILY_RECO_LINE": raw})
                assert C.DAILY_RECO_LINE == "off", f"{raw!r} 로 켜졌다"
                assert out["invalid"] == {"DAILY_RECO_LINE": raw}, out
            C.DAILY_RECO_LINE = "admin"
            out = C.apply_env_flags({"DAILY_RECO_LINE": "  "})
            assert C.DAILY_RECO_LINE == "admin" and not out["applied"]
        finally:
            _back(snap)
    check(S, ".env 값: all·admin·off(0) 만 받는다 · 1/true/오타는 무시(OFF 유지)",
          env_values)

    def wiring():
        import run_screen as rs

        class A:
            dry_run = False
        calls: list = []
        o_emit, o_sum = rs._emit_admin, dict(rs.SUMMARY)
        snap = _keep()
        try:
            rs._emit_admin = lambda text, dry: (calls.append((text, dry)) or True)
            rs.SUMMARY.pop("reco_line", None)
            C.DAILY_RECO_LINE = "off"
            rs._daily_reco_line(st, A, d, {})
            assert not calls and "reco_line" not in rs.SUMMARY
            C.DAILY_RECO_LINE = "all"
            _, info = R.weekday_block(st, d, paper_on=False,
                                      pdata=P.PaperData(st, d, index={}))
            rs._daily_reco_line(st, A, d, info)
            assert not calls, "all 인데 관리자에게 따로 보냈다"
            assert rs.SUMMARY["reco_line"]["mode"] == "all"
            assert rs.SUMMARY["reco_line"]["tickers"] == order
            C.DAILY_RECO_LINE = "admin"
            rs._daily_reco_line(st, A, d, {})
            assert len(calls) == 1 and calls[0][1] is False
            assert calls[0][0] == "\n".join(R.reco_line_block(st, d)[0])
            got = rs.SUMMARY["reco_line"]
            assert got["mode"] == "admin" and got["sent"] is True
            assert got["tickers"] == order and got["len"] == len(calls[0][0])
            assert got["len"] <= R.SEND_LIMIT

            def boom(text, dry):
                raise RuntimeError("x")
            rs._emit_admin = boom
            rs._daily_reco_line(st, A, d, {})        # 예외가 새지 않는다
            assert "RuntimeError" in rs.SUMMARY["reco_line"]["error"]
        finally:
            rs._emit_admin = o_emit
            rs.SUMMARY.clear()
            rs.SUMMARY.update(o_sum)
            _back(snap)
        src = inspect.getsource(rs._brief_evening_v2)
        assert src.index("sent = _emit(text") < src.index("_daily_reco_line(")
        assert src.index("record_sends") < src.index("_daily_reco_line(")
        helper = inspect.getsource(rs._daily_reco_line)
        assert "record_sends" not in helper.split('"""')[2], "발송 기록을 건드린다"
        assert "_emit_admin(" in helper and "_emit(" not in helper.replace(
            "_emit_admin(", "")
        legacy = inspect.getsource(rs.mode_brief_evening)
        assert "no_stock_block" in legacy
    check(S, "admin = 관리자 채팅 1건(브리핑 뒤) · all = 추가 발송 없음 · off = 무동작",
          wiring)
