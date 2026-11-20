# -*- coding: utf-8 -*-
"""2026-11-12 정리: daily 시각·이중 기록, pykrx 안내문, 지표 소급 기본 범위."""
from __future__ import annotations

import contextlib
import io
import sys

S = "v2 정리(시간대·안내문·소급 범위)"


def run(check, near, tmp):
    def daily_clock():
        import inspect
        from stocknews import daily as D
        src = inspect.getsource(D.run_daily)
        assert "datetime.now()" not in src.replace("datetime.now() 라", "")
        assert "store.log_run(" not in src, "run_daily 가 runs 를 직접 기록(이중 기록)"
    check(S, "run_daily: datetime.now() 없음 · log_run 이중 기록 없음", daily_clock)

    def quiet():
        from stocknews.quiet_import import quiet_import
        d = tmp / "qpkg"
        d.mkdir(exist_ok=True)
        (d / "noisy_mod_v2.py").write_text(
            'print("KRX 로그인 실패: KRX_ID 또는 KRX_PW 환경 변수가 설정되지 않았습니다.")\n'
            'print("다른 경고 줄")\nVALUE = 7\n', encoding="utf-8")
        sys.path.insert(0, str(d))
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                m = quiet_import("noisy_mod_v2")
            assert m.VALUE == 7
            assert buf.getvalue() == "", buf.getvalue()     # stdout 오염 없음
        finally:
            sys.path.remove(str(d))
            sys.modules.pop("noisy_mod_v2", None)
        from pathlib import Path
        root = Path(__file__).resolve().parents[1] / "stocknews"
        for f in ("data.py", "universe.py"):
            assert "from pykrx import stock" not in (root / f).read_text(encoding="utf-8"), f
    check(S, "pykrx 안내문 걸러냄 · 다른 출력은 stdout 에 안 섞임 · 직접 import 없음", quiet)

    def ind_default():
        import importlib
        from types import SimpleNamespace
        from stocknews import events as ev
        from stocknews import indicator_log as ind
        rs = importlib.import_module("run_screen")
        cal = [f"2025-{m:02d}-{d:02d}" for m in range(1, 13) for d in range(1, 29)][:300]
        got = {}
        o_cal, o_bf = ev.trading_calendar, ind.backfill
        ev.trading_calendar = lambda store, since=None: list(cal)
        ind.backfill = lambda store, date_from=None, date_to=None, cache_dir=None: (
            got.update(f=date_from) or {"dates": 1})
        try:
            a = SimpleNamespace(db=str(tmp / "x.db"), ind_from=None, ind_to=None,
                                ind_all=False, index_cache=None)
            assert rs.mode_indicators_backfill(None, a) == 0
            assert got["f"] == cal[-250], got
            a.ind_all = True
            rs.mode_indicators_backfill(None, a)
            assert got["f"] is None, got                     # 전 기간
            a.ind_all, a.ind_from = False, "2025-06-01"
            rs.mode_indicators_backfill(None, a)
            assert got["f"] == "2025-06-01"
        finally:
            ev.trading_calendar, ind.backfill = o_cal, o_bf
    check(S, "지표 소급 기본 = 최근 250거래일 · --ind-all 전 기간 · --ind-from 우선", ind_default)

    def admin_only():
        import os
        from stocknews import notify as N
        assert N.admin_chat_id("111, 222;333") == "111"
        assert N.admin_chat_id("") is None
        calls = []
        orig, old = N.send_telegram, os.environ.get("TELEGRAM_CHAT_ID")
        N.send_telegram = lambda text, chat_id=None, token=None, retries=3: calls.append(chat_id)
        os.environ["TELEGRAM_CHAT_ID"] = "111,222"
        try:
            assert N.send_admin("x", dry_run=True) is None and calls == []
            N.send_admin("x")
            assert calls == ["111"], calls                  # 첫 수신자에게만
        finally:
            N.send_telegram = orig
            if old is None:
                os.environ.pop("TELEGRAM_CHAT_ID", None)
            else:
                os.environ["TELEGRAM_CHAT_ID"] = old
    check(S, "관리자 채팅 = 첫 수신자에게만 · dry-run 은 미발송", admin_only)
