# -*- coding: utf-8 -*-
"""거래일 판정.

왜 필요한가
----------
시각만 보고 배치를 돌리면 토·일·공휴일에 오작동한다. 실제로 확인된
문제가 여섯 개였다.

  1) refresh_master 가 휴일에 빈 종목 목록을 받으면 mark_inactive(빈집합)
     으로 **전 종목을 비활성화**한다. 시스템 전체가 멈춘다.
  2) 알림 시간창이 요일을 몰라 토요일 10시에 열린다. 금요일 데이터로
     알림이 나간다.
  3) daily/exits 가 휴장일에 돌면 같은 거래일 스냅샷을 재생성하고
     같은 알림을 토·일에 반복 발송한다.
  4) 월요일 아침 브리핑이 '최근 16시간'만 보므로 주말 뉴스를 놓친다.
  5) mode_update 가 공휴일을 catchup 기간 내내 매번 재요청한다.
  6) 쿨다운이 달력일이라 거래일 의도와 어긋난다.

판정 원칙
--------
공휴일 목록을 하드코딩하지 않는다. 매년 바뀌고 임시 휴장도 있다.
대신 세 단계로 판정한다.

  주말            -> 요일로 확정 (CLOSED_WEEKEND)
  알려진 휴장일    -> DB 캐시 (CLOSED_HOLIDAY). 조회가 0건이면 기록된다
  시세가 있는 날   -> 거래일 확정 (TRADING)
  그 외 평일       -> UNKNOWN. 낙관적으로 거래일로 취급하되,
                     '확실히 휴장'을 요구하는 게이트는 통과시킨다

이 방식은 자기 학습형이다. 새 공휴일을 만나면 한 번 헛조회하고 기록한 뒤
다음부터 건너뛴다.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, time as dtime, timedelta, timezone

log = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))

# 한국거래소 정규장
KRX_OPEN = dtime(9, 0)
KRX_CLOSE = dtime(15, 30)

TRADING = "TRADING"
CLOSED_WEEKEND = "CLOSED_WEEKEND"
CLOSED_HOLIDAY = "CLOSED_HOLIDAY"
UNKNOWN = "UNKNOWN"

__all__ = ["KRX_OPEN", "KRX_CLOSE", "TRADING", "CLOSED_WEEKEND",
           "CLOSED_HOLIDAY", "UNKNOWN", "now_kst", "as_date", "is_weekend",
           "market_status", "is_definitely_closed", "last_trading_day",
           "calendar_gap_days", "trading_days_between", "news_window_hours",
           "should_scan_intraday"]


def now_kst() -> datetime:
    return datetime.now(KST).replace(tzinfo=None)


def as_date(d) -> date:
    if isinstance(d, datetime):
        return d.date()
    if isinstance(d, date):
        return d
    return datetime.strptime(str(d)[:10], "%Y-%m-%d").date()


def is_weekend(d) -> bool:
    return as_date(d).weekday() >= 5


def market_status(store, d=None) -> str:
    """그 날짜의 장 상태."""
    day = as_date(d if d is not None else now_kst())
    if day.weekday() >= 5:
        return CLOSED_WEEKEND
    ds = day.isoformat()
    if store.is_known_non_trading(ds):
        return CLOSED_HOLIDAY
    if store.has_price_date(ds):
        return TRADING
    return UNKNOWN


def is_definitely_closed(store, d=None) -> bool:
    """확실히 휴장인가.

    UNKNOWN(평일이지만 미확인)은 False 를 돌린다. 장중에는 아직 시세가
    적재되지 않았을 수 있으므로, 확실한 경우만 차단해야 한다.
    """
    return market_status(store, d) in (CLOSED_WEEKEND, CLOSED_HOLIDAY)


def last_trading_day(store) -> date | None:
    ds = store.last_price_date()
    return as_date(ds) if ds else None


def calendar_gap_days(store, now: datetime | None = None) -> int:
    """마지막 거래일로부터 경과한 달력일 수.

    월요일 아침이면 보통 3(금->월). 연휴 뒤면 더 크다.
    """
    last = last_trading_day(store)
    if last is None:
        return 0
    return max(0, (as_date(now or now_kst()) - last).days)


def trading_days_between(store, start, end) -> int:
    """두 날짜 사이 거래일 수. 쿨다운을 거래일 기준으로 세는 데 쓴다.

    시세가 적재된 날짜를 세므로 공휴일이 자동 제외된다. 데이터가 없으면
    주말만 제외한 근사값을 돌린다.
    """
    a, b = as_date(start), as_date(end)
    if b <= a:
        return 0
    try:
        have = store.existing_dates(since=a.isoformat())
        n = sum(1 for x in have if a.isoformat() < x <= b.isoformat())
        if n:
            return n
    except Exception:  # noqa: BLE001 - 저장소 미준비 상태 허용
        pass
    # 폴백: 주말만 제외
    n, cur = 0, a + timedelta(days=1)
    while cur <= b:
        if cur.weekday() < 5:
            n += 1
        cur += timedelta(days=1)
    return n


def news_window_hours(store, now: datetime | None = None,
                      base: int = 16, cap: int = 120) -> int:
    """뉴스 브리핑이 볼 시간 범위.

    월요일 아침에 '최근 16시간'만 보면 금요일 장 마감 이후부터 일요일
    오후까지의 뉴스가 통째로 빠진다. 마지막 거래일로부터의 공백만큼
    자동으로 넓힌다.
    """
    now = now or now_kst()
    gap = calendar_gap_days(store, now)
    if gap <= 1:
        return base
    # 마지막 거래일 장 마감(15:30) 이후 전부를 덮는다
    last = last_trading_day(store)
    if last is None:
        return base
    close_dt = datetime.combine(last, KRX_CLOSE)
    hours = int((now - close_dt).total_seconds() // 3600) + 1
    return max(base, min(hours, cap))


def should_scan_intraday(store, now: datetime | None = None
                         ) -> tuple[bool, str]:
    """장중 스캔(flash)을 돌려야 하는가. (실행여부, 사유) 반환."""
    now = now or now_kst()
    status = market_status(store, now)
    if status == CLOSED_WEEKEND:
        return False, "주말 휴장"
    if status == CLOSED_HOLIDAY:
        return False, "공휴일 휴장"
    t = now.time()
    # 정규장 전후 여유를 둔다. 09:00 동시호가 직전과 마감 직후를 포함.
    if t < dtime(8, 50) or t > dtime(15, 40):
        return False, f"장시간 밖 ({t.strftime('%H:%M')})"
    return True, status
