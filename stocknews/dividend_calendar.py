# -*- coding: utf-8 -*-
"""배당락 캘린더 + DPS 성장률 + 월간 배당 리포트 데이터.

★ 이 모듈이 내는 미래 날짜는 전부 **추정**이다. 공시가 아니다.
--------------------------------------------------------
두 가지 이유로 확정할 수 없다.

  1) **미래 공휴일을 모른다.** 이 저장소는 공휴일 목록을 하드코딩하지
     않는다(AGENTS 8-2장). 지나간 휴장일만 `non_trading_days` 에 쌓인다.
     그래서 2026년 12월 달력은 주말과 이미 알려진 휴장일만으로 세운
     추정이고, 임시공휴일이 끼면 틀린다.

  2) **배당기준일은 회사가 정한다.** 상법상 기본은 결산기말일이지만,
     2024년 배당절차 개선 이후 배당액을 확정한 뒤(주주총회 후) 기준일을
     잡는 회사가 늘었다. 그런 회사는 12월이 아니라 이듬해 2~4월이
     기준일이다. DART 의 '배당에 관한 사항'은 결산일(`stlm_dt`)만 주고
     배당기준일은 주지 않으므로, 여기서 계산한 12월 기준일은 **전통적
     관행을 따르는 회사에만 맞는다.**

그래서 컬럼명과 리포트 문구에 '추정'을 박아 넣는다. 추정치를 확정
날짜처럼 보여주면 사람이 그 날짜에 주문을 낸다.

★ 달력이 두 개인 이유
------------------
결제는 **영업일** 기준 T+2 이고, 매수는 **거래일**에만 할 수 있다.
평소에는 두 달력이 같아서 구분이 필요 없지만, 연말에 갈라진다.

  12월 31일  증시 휴장(거래일 아님) · 결제는 되는 영업일

그래서 '기준일 12/31 에 명부에 오르는 최종 매수일'을 거래일만으로 세면
하루 앞당겨진다. 두 달력을 따로 받는다.

★ 연말 휴장 규칙 (관행 기반 · 검증 필요)
-----------------------------------
KRX 는 12월 31일을 휴장한다. 31일이 영업일이 아니면(주말) 그 직전
영업일을 휴장한다. 이 규칙으로 두 해를 재현할 수 있다.

  2023  12/31 일요일 -> 직전 영업일 12/29(금) 휴장 -> 폐장일 12/28(목)
  2024  12/31 화요일 -> 12/31 휴장            -> 폐장일 12/30(월)

**과거 연도는 추정하지 않는다.** `prices` 에 적재된 실제 거래일이 있으면
그걸 쓴다(`observed`). 규칙은 미래 연도에만 적용된다.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from .config import DEFAULT, Config
from .notify import now_kst

log = logging.getLogger(__name__)

__all__ = [
    "build_dividend_report", "dps_cagr", "ex_dividend_date",
    "is_first_saturday", "krx_year_end_break", "last_buy_date",
    "last_trading_day_of_year", "next_record_date", "record_date_plan",
    "report_send_allowed",
]


def as_date(x) -> date | None:
    """문자열 / datetime / date 를 date 로. 못 읽으면 None."""
    if x is None:
        return None
    if isinstance(x, datetime):
        return x.date()
    if isinstance(x, date):
        return x
    try:
        return date.fromisoformat(str(x)[:10])
    except (TypeError, ValueError):
        return None


# ══════════════════════════ 달력 ══════════════════════════
def business_days(start: date, end: date, holidays=()) -> list[date]:
    """[start, end] 의 영업일. 주말과 주어진 공휴일을 뺀다.

    미래 구간이면 공휴일 집합이 비어 있을 수밖에 없다 — 그 사실은
    호출부가 `confirmed=False` 로 전달한다.
    """
    hol = {as_date(h) for h in holidays}
    out, d = [], start
    while d <= end:
        if d.weekday() < 5 and d not in hol:
            out.append(d)
        d += timedelta(days=1)
    return out


def krx_year_end_break(year: int, holidays=()) -> date | None:
    """그 해 연말 휴장일 (모듈 독스트링의 관행 규칙)."""
    bdays = business_days(date(year, 12, 1), date(year, 12, 31), holidays)
    if not bdays:
        return None
    last = date(year, 12, 31)
    if last in bdays:
        return last
    return bdays[-1]          # 31일이 주말이면 직전 영업일을 휴장한다


def last_trading_day_of_year(year: int, holidays=(),
                             observed=None) -> tuple[date | None, bool]:
    """그 해 마지막 거래일. `(날짜, 확정여부)`.

    `observed` 는 실제 적재된 거래일 문자열 집합이다. 있으면 거기서
    찾는다 — 지나간 해를 추정할 이유가 없다.
    """
    if observed:
        got = sorted(d for d in (str(x)[:10] for x in observed)
                     if d.startswith(f"{year:04d}-12"))
        if got:
            return as_date(got[-1]), True
    bdays = business_days(date(year, 12, 1), date(year, 12, 31), holidays)
    brk = krx_year_end_break(year, holidays)
    tdays = [d for d in bdays if d != brk]
    return (tdays[-1] if tdays else None), False


def _settle_date(buy: date, bdays: list[date], settle_days: int) -> date | None:
    """매수일의 결제일 = 그 뒤 `settle_days` 번째 영업일."""
    later = [d for d in bdays if d > buy]
    if len(later) < settle_days:
        return None
    return later[settle_days - 1]


def last_buy_date(record_date, *, trading_days, business_days_,
                  settle_days: int = 2) -> date | None:
    """기준일에 주주명부에 오르기 위한 최종 매수일.

    기준일 이하의 거래일을 뒤에서부터 훑어, 결제일이 기준일 이하가 되는
    첫 날을 고른다. 결제는 영업일, 매수는 거래일이라 달력이 둘이다.
    """
    rec = as_date(record_date)
    if rec is None:
        return None
    tds = sorted(d for d in (as_date(t) for t in trading_days)
                 if d is not None and d <= rec)
    bds = sorted(d for d in (as_date(b) for b in business_days_)
                 if d is not None)
    for t in reversed(tds):
        st = _settle_date(t, bds, settle_days)
        if st is not None and st <= rec:
            return t
    return None


def ex_dividend_date(record_date, *, trading_days, business_days_,
                     settle_days: int = 2) -> date | None:
    """배당락일 = 최종 매수일의 다음 거래일. 그날 사면 배당을 못 받는다."""
    lb = last_buy_date(record_date, trading_days=trading_days,
                       business_days_=business_days_, settle_days=settle_days)
    if lb is None:
        return None
    later = sorted(d for d in (as_date(t) for t in trading_days)
                   if d is not None and d > lb)
    return later[0] if later else None


def next_record_date(settle_dt, asof=None) -> date | None:
    """다음 배당기준일(추정). 결산기말일 기준이고 이미 지났으면 다음 해.

    `settle_dt` 는 DART 가 준 결산일(`2025-12-31`)이다. 월/일만 쓴다.
    2월 29일 결산은 없지만, 있어도 그 해에 29일이 없으면 28일로 내린다.
    """
    base = as_date(settle_dt)
    if base is None:
        return None
    today = as_date(asof) or now_kst().date()
    for year in (today.year, today.year + 1):
        try:
            cand = date(year, base.month, base.day)
        except ValueError:
            cand = date(year, base.month, 28)
        if cand >= today:
            return cand
    return None


def record_date_plan(settle_dt, *, asof=None, holidays=(), observed=None,
                     settle_days: int = 2) -> dict:
    """`{record_date, last_buy, ex_date, confirmed}` — 전부 추정치다.

    `confirmed` 는 달력이 실측인지 여부다. 기준일 자체는 회사가 정하므로
    `confirmed=True` 여도 '기준일이 확정됐다'는 뜻이 아니다.
    """
    rec = next_record_date(settle_dt, asof=asof)
    out = {"record_date": rec, "last_buy": None, "ex_date": None,
           "confirmed": False}
    if rec is None:
        return out
    # 기준일 앞으로 두 달치 달력이면 연휴를 건너뛰기에 충분하다.
    start = rec - timedelta(days=60)
    end = rec + timedelta(days=30)
    bds = business_days(start, end, holidays)
    if observed:
        obs = sorted(d for d in (as_date(x) for x in observed)
                     if d is not None and start <= d <= end)
        # 실측 거래일이 기준일 근처까지 있으면 그걸 쓴다.
        if obs and obs[-1] >= rec - timedelta(days=7):
            tds, confirmed = obs, True
        else:
            tds, confirmed = None, False
    else:
        tds, confirmed = None, False
    if tds is None:
        brk = krx_year_end_break(rec.year, holidays)
        tds = [d for d in bds if d != brk]
    out["confirmed"] = confirmed
    out["last_buy"] = last_buy_date(rec, trading_days=tds,
                                    business_days_=bds,
                                    settle_days=settle_days)
    out["ex_date"] = ex_dividend_date(rec, trading_days=tds,
                                      business_days_=bds,
                                      settle_days=settle_days)
    return out


# ══════════════════════════ 성장률 ══════════════════════════
def dps_cagr(dps_by_year: dict, base_year: int, years: int = 5) -> float | None:
    """DPS 연평균 성장률(%). 구간이 `years` 년이면 성장 기간은 years-1 번이다.

    시작 연도 DPS 가 0 이거나 결측이면 None 이다. 0 에서 시작한 성장률은
    무한대이고, '무배당에서 배당 시작'은 성장률이 아니라 사건이다.

    **걸리는 종목이 적어도 기준을 완화하지 않는다.** 5년 내내 배당한
    종목만 값이 나오는 것이 맞다.
    """
    if years < 2:
        return None
    first_y, last_y = base_year - (years - 1), base_year
    first = dps_by_year.get(first_y)
    last = dps_by_year.get(last_y)
    try:
        f, l = float(first), float(last)
    except (TypeError, ValueError):
        return None
    if f != f or l != l or f <= 0 or l < 0:
        return None
    periods = years - 1
    return round(((l / f) ** (1.0 / periods) - 1.0) * 100.0, 2)


# ══════════════════════════ 발송 게이트 ══════════════════════════
def is_first_saturday(d=None) -> bool:
    """그 달의 첫 토요일인가.

    `weekly.is_first_friday` 와 같은 규격이다. '첫째 주 토요일'이 아니라
    '그 달의 첫 토요일'이므로 `day <= 7` 로 판정한다.
    """
    dt = as_date(d if d is not None else now_kst())
    if dt is None:
        return False
    return dt.weekday() == 5 and dt.day <= 7


def report_send_allowed(now: datetime | None = None, cfg: Config = DEFAULT,
                        force: bool = False) -> tuple[bool, str]:
    """월간 배당 리포트를 오늘 내보내도 되는가. `(허용여부, 사유코드)`.

    `notify.reco_send_allowed` 와 같은 규격이다(요일만 보고 시각은 보지
    않는다). 두 게이트는 서로 참조하지 않는다 — 추천 10선은 일요일 주
    1회이고 이쪽은 매월 첫 토요일 월 1회다. 한 판정에 섞으면 한쪽을
    고칠 때 다른 쪽이 조용히 사라진다.

    사유코드
      force      --force 로 요일 무시
      send_day   매월 첫 토요일
      off_day    발송일이 아님
    """
    n = now or now_kst()
    if force:
        return True, "force"
    dow = int(cfg.dividend.report_dow) % 7
    if n.weekday() == dow and n.day <= 7:
        return True, "send_day"
    return False, "off_day"


# ══════════════════════════ 리포트 데이터 ══════════════════════════
def build_dividend_report(store, cfg: Config = DEFAULT, *, asof=None,
                          trade_date=None, base_year: int | None = None,
                          force_screen: bool = False) -> dict:
    """상위 N 종목 표 + 캘린더 + 성장률. 렌더링은 하지 않는다.

    필터는 `dividend_screen` 이 이미 판정한 것을 읽는다. 없으면 그 자리에서
    돌린다 — 리포트만 보려는 사람이 명령을 두 번 치지 않게 한다.
    """
    from .dividend_data import latest_fiscal_year
    from .dividend_screen import collect_dividend_screen

    dc = cfg.dividend
    base = base_year if base_year is not None else latest_fiscal_year()
    td = trade_date or store.last_price_date()
    out: dict = {"trade_date": td, "base_year": base, "rows": [],
                 "evaluated": 0, "passed": 0, "funnel": {},
                 "confirmed_calendar": False}
    if not td:
        out["reason"] = "no_trade_date"
        return out

    scr = collect_dividend_screen(store, cfg=cfg, base_year=base,
                                  trade_date=td, force=force_screen)
    out["screen"] = {k: scr.get(k) for k in
                     ("evaluated", "passed", "stored", "skipped", "reason")}
    fun = store.dividend_screen_funnel(td)
    out.update({"evaluated": fun["evaluated"], "passed": fun["passed"],
                "funnel": fun["by_filter"]})

    top = store.dividend_screen_on(td, passed_only=True)
    if top is None or top.empty:
        out["reason"] = scr.get("reason") or "no_pass"
        return out
    top = top.head(int(dc.top_n))

    holidays = store.known_non_trading_days()
    observed = store.existing_dates()

    rows = []
    for r in top.to_dict("records"):
        code = str(r["code"])
        hist = store.dividends_of(code)
        dps_by_year, settle_dt = {}, None
        if hist is not None and not hist.empty:
            for h in hist.to_dict("records"):
                dps_by_year[int(h["fiscal_year"])] = h.get("dps")
                if h.get("settle_dt") and int(h["fiscal_year"]) == base:
                    settle_dt = h["settle_dt"]
        plan = record_date_plan(settle_dt, asof=asof, holidays=holidays,
                                observed=observed,
                                settle_days=int(dc.settle_days))
        if plan["confirmed"]:
            out["confirmed_calendar"] = True
        rows.append({
            "code": code, "name": r.get("name"), "price": r.get("price"),
            "dps": r.get("dps"), "div_yield": r.get("div_yield"),
            "years_paid": r.get("years_paid"), "payout": r.get("payout"),
            "cagr": dps_cagr(dps_by_year, base, int(dc.cagr_years)),
            "record_date": plan["record_date"],
            "last_buy": plan["last_buy"],
            "ex_date": plan["ex_date"],
            "calendar_confirmed": plan["confirmed"],
            "settle_dt": settle_dt,
        })
    out["rows"] = rows
    return out
