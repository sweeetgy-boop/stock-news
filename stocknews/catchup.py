# -*- coding: utf-8 -*-
"""장중 판정 — catch-up 이 장중에 떴을 때 무엇을 건드리지 않을지.

2026-11-12 13:32 실측. 11-11 21:30 nightly 가 누락돼 다음 날 장중에
catch-up 으로 떴다. update 가 당일(11-12)분을 종목별로 2,560종목 헛조회
(21분)했고, 저장 단(`store.in_progress_date`)에서 전부 버려져 0건이 되자
'소스 장애'로 rc=2 를 냈다. 장중에는 당일분을 묻지 않아야 하고, FDR 전종목
스냅샷(날짜 파라미터 없음 = 오늘 현재가)으로 지난 날짜를 적재해서도 안 된다.

'장중' 은 사용자 정의다: 거래일 KST 09:00~15:40 (`config.INTRADAY_*_HM`).
'오늘 봉이 미완성인가' 는 별개 질문이고 `store.in_progress_date()`(평일
00:00~15:50)가 답한다. 둘을 섞지 않는다.
"""
from __future__ import annotations

from datetime import datetime

from . import config as _cfg


def _hm(value) -> tuple[int, int]:
    h, m = value
    return int(h), int(m)


def is_intraday(now: datetime, store=None) -> bool:
    """`now`(naive KST)가 거래일 장중(09:00~15:40)인가.

    주말은 False. store 가 있으면 기록된 공휴일·휴장일도 False 다(DB 만
    본다 — 네트워크를 쓰지 않는다). 미확인 평일은 거래일로 본다: 장중에
    스냅샷을 잘못 쓰는 것보다 한 번 덜 쓰는 편이 안전하다.
    """
    if now.weekday() >= 5:
        return False
    t = (now.hour, now.minute)
    if not (_hm(_cfg.INTRADAY_START_HM) <= t < _hm(_cfg.INTRADAY_END_HM)):
        return False
    if store is not None:
        try:
            from .trading_day import is_definitely_closed
            if is_definitely_closed(store, now):
                return False
        except Exception:                          # noqa: BLE001
            pass
    return True


def brief_max_late_sec() -> float:
    """브리핑 잡의 지연 상한(초). 넘으면 발송하지 않는다."""
    return float(_cfg.BRIEF_MAX_LATE_HOURS) * 3600.0
