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

★ 연말 휴장 규칙 — 실측으로 대조했다
--------------------------------
KRX 는 12월 31일을 휴장한다. 31일이 영업일이 아니면(주말) 그 직전
영업일을 휴장한다.

2026-09-02 실측: `prices` 에 적재된 실제 거래일의 12월 마지막 날과
이 규칙의 추정이 두 해 모두 일치했다.

  연도  실제 폐장일   규칙 추정    비고
  2024  12-30        12-30       12/31(화) 휴장
  2025  12-30        12-30       12/31(수) 휴장
  2023  (시세 없음)   12-28       12/31(일) -> 12/29(금) 휴장

같은 규칙으로 계산한 최종 매수일도 달력 모양에 따라 갈린다. 둘 다
실측 거래일로 검산한 값이다.

  2024  기준일 12-31 -> 최종매수 12-27 (12/28·29 주말) · 배당락 12-30
  2025  기준일 12-31 -> 최종매수 12-29                 · 배당락 12-30

**남은 가정 하나:** 12월 31일이 결제 영업일이라는 것. 증시는 닫혀도
예탁결제는 도는 날이라 12/29 매수가 12/31 에 결제된다고 본다. 이 가정이
2025년의 최종매수일 12-29 를 만든다. 규정 원문으로 대조하지는 못했다.

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
    "last_trading_day_of_year", "next_record_date", "past_ex_dates",
    "recovery_days", "recovery_stats", "record_date_plan",
    "report_send_allowed", "total_return_ratio",
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
    try:
        meta = store.ticker_meta()
        caps = {c: r.get("market_cap") for c, r in meta.iterrows()}
    except Exception as exc:                 # noqa: BLE001
        log.debug("시가총액 조회 실패: %s", exc)
        caps = {}

    # 회복일수는 상위 종목만 본다. 전종목이면 2,500번 시계열을 읽는다.
    lookback = int(dc.recovery_years) * 300

    rows = []
    for r in top.to_dict("records"):
        code = str(r["code"])
        hist = store.dividends_of(code)
        dps_by_year, settle_dt, latest = {}, None, {}
        if hist is not None and not hist.empty:
            for h in hist.to_dict("records"):
                dps_by_year[int(h["fiscal_year"])] = h.get("dps")
                if int(h["fiscal_year"]) == base:
                    latest = h
                    if h.get("settle_dt"):
                        settle_dt = h["settle_dt"]
        plan = record_date_plan(settle_dt, asof=asof, holidays=holidays,
                                observed=observed,
                                settle_days=int(dc.settle_days))
        if plan["confirmed"]:
            out["confirmed_calendar"] = True

        # 과거 배당락 회복 통계. 시세 범위를 벗어난 해는 no_data 로 남는다.
        try:
            ohlcv = store.load_ohlcv(code, days=lookback)
            closes = None if ohlcv is None or ohlcv.empty else ohlcv["종가"]
        except Exception as exc:             # noqa: BLE001
            log.debug("%s 시세 조회 실패: %s", code, exc)
            closes = None
        past = past_ex_dates(settle_dt, years=int(dc.recovery_years),
                             asof=asof, holidays=holidays, observed=observed,
                             settle_days=int(dc.settle_days))
        rec_stats = recovery_stats(closes, past)

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
            "buyback": latest.get("buyback"),
            "market_cap": caps.get(code),
            "total_return": total_return_ratio(
                latest.get("total_dividend"), latest.get("buyback"),
                caps.get(code)),
            "recovery": rec_stats,
        })
    out["rows"] = rows
    out["recovery_samples"] = sum(r["recovery"]["samples"] for r in rows)
    out["buyback_known"] = sum(1 for r in rows if r["buyback"] is not None)
    return out


# ══════════════════ 배당락 회복일수 (과거 통계 · 예측 아님) ══════════════════
# 컬럼명에 '(참고)' 를 붙이는 이유: 과거 몇 번의 회복일수는 표본이 3건도
# 안 되고, 그 사이 시장 국면이 달랐다. 평균 12거래일이라는 숫자가 '이번에도
# 12일이면 회복한다'로 읽히면 그건 통계가 아니라 점이다.
def past_ex_dates(settle_dt, *, years: int = 3, asof=None, holidays=(),
                  observed=None, settle_days: int = 2) -> list[dict]:
    """과거 `years` 개 사업연도의 배당락일. 최근 연도부터.

    지나간 해는 `observed`(실제 적재 거래일)로 확정 계산이 된다.
    """
    today = as_date(asof) or now_kst().date()
    out = []
    for i in range(1, years + 1):
        y = today.year - i
        plan = record_date_plan(settle_dt, asof=date(y, 1, 1),
                               holidays=holidays, observed=observed,
                               settle_days=settle_days)
        rec = plan.get("record_date")
        if rec is None or rec.year != y:
            continue
        out.append({"year": y, "record_date": rec,
                    "ex_date": plan.get("ex_date"),
                    "confirmed": plan.get("confirmed")})
    return out


def recovery_days(closes, ex_date, until=None) -> dict:
    """배당락 이후 '락 전 종가' 회복까지 걸린 거래일수.

    락 전 종가 = 배당락일 **직전 거래일**의 종가. 배당락일 종가로 잡으면
    이미 떨어진 값이 기준이 되어 회복이 실제보다 쉬워 보인다.

    `until` 은 탐색 상한(그 날짜 미만까지만 본다)이다. **다음 배당락일을
    넘겨서 세면 안 된다.** 넘기면 다음 해의 하락·회복이 섞여 들어가
    '이번 배당락에서 회복하는 데 걸린 기간'이 아니게 된다. 시세에 구멍이
    있을 때 1년 뒤 봉을 회복으로 잡는 오류도 이 상한이 막는다.

    반환 `{"days": int|None, "status": "ok"|"unrecovered"|"no_data"}`
      ok           회복까지 걸린 거래일수 (락일을 1일로 센다)
      unrecovered  상한까지 회복하지 못했다
      no_data      락일이 시세 범위 밖이거나 앞·뒤 봉이 없다
    """
    ex = as_date(ex_date)
    if ex is None or closes is None or len(closes) == 0:
        return {"days": None, "status": "no_data"}
    end = as_date(until)
    idx = [as_date(i) for i in closes.index]
    before = [(d, i) for i, d in enumerate(idx) if d is not None and d < ex]
    after = [(d, i) for i, d in enumerate(idx)
             if d is not None and d >= ex and (end is None or d < end)]
    if not before or not after:
        return {"days": None, "status": "no_data"}
    base = float(closes.iloc[before[-1][1]])
    if base != base or base <= 0:
        return {"days": None, "status": "no_data"}
    for n, (_d, i) in enumerate(after, 1):
        try:
            c = float(closes.iloc[i])
        except (TypeError, ValueError):
            continue
        if c == c and c >= base:
            return {"days": n, "status": "ok"}
    return {"days": None, "status": "unrecovered"}


def recovery_stats(closes, ex_dates) -> dict:
    """여러 해의 회복일수를 묶는다. `{samples, avg, max, unrecovered, items}`.

    미회복은 평균에 넣지 않는다. 임의의 큰 수로 채우면 평균이 그 수에
    좌우되고, 0 으로 채우면 즉시 회복한 것이 된다. 건수로만 보고한다.
    """
    items, days = [], []
    unrec = 0
    # 오래된 락일부터 훑고, 각 구간의 상한을 '다음 락일'로 준다.
    ordered = sorted((e for e in (ex_dates or []) if e.get("ex_date")),
                     key=lambda e: as_date(e["ex_date"]))
    for k, e in enumerate(ordered):
        nxt = (as_date(ordered[k + 1]["ex_date"])
               if k + 1 < len(ordered) else None)
        got = recovery_days(closes, e.get("ex_date"), until=nxt)
        items.append({"year": e.get("year"),
                      "ex_date": e.get("ex_date"), **got})
        if got["status"] == "ok":
            days.append(int(got["days"]))
        elif got["status"] == "unrecovered":
            unrec += 1
    return {"samples": len(days),
            "avg": (round(sum(days) / len(days), 1) if days else None),
            "max": (max(days) if days else None),
            "unrecovered": unrec, "items": items}


# ══════════════════════════ 총주주환원율 ══════════════════════════
def total_return_ratio(total_dividend, buyback, market_cap) -> float | None:
    """총주주환원율(%) = (현금배당총액 + 자사주 취득액) / 시가총액 x 100.

    자사주 **취득액**만 더한다. 소각은 이미 취득한 주식을 없애는 것이라
    추가 현금 유출이 없다 — 더하면 같은 돈을 두 번 센다.

    자사주가 NULL 이면 None 이다. 0 으로 가정하면 '자사주를 안 샀다'와
    '현금흐름표를 못 읽었다'가 같은 값이 된다. 전자는 `dividend_data` 가
    이미 0 으로 확정해 둔다.
    """
    try:
        td = float(total_dividend)
        bb = float(buyback)
        mc = float(market_cap)
    except (TypeError, ValueError):
        return None
    if td != td or bb != bb or mc != mc or mc <= 0:
        return None
    return round((td + bb) / mc * 100.0, 2)
