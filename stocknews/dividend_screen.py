# -*- coding: utf-8 -*-
"""배당주 필터 엔진 (F1~F6).

`dividends` 테이블(1단계 수집분)과 로컬 시세·마스터만 읽는다. 네트워크를
타지 않는다. 점수·추천·스크리닝·청산 어디에도 값을 넣지 않는다.

★ 설계 원칙 셋
-------------
1) **탈락도 기록한다.** 통과 종목만 남기면 '필터가 빡빡한가'를 판단할
   근거가 없다. 종목마다 처음 걸린 필터와 그 사유를 남긴다. 그러면
   `F1 에서 2,100종목이 떨어졌다` 같은 깔때기가 그대로 보인다.

2) **판정 불가와 탈락을 구분한다.** 데이터가 없어서 판정할 수 없는 것은
   조건을 못 맞춘 것과 다르다. 둘을 섞으면 '수집이 덜 됐다'와 '조건이
   빡빡하다'를 구분할 수 없다. `failed_at` 은 같아도 `reason` 이 다르고,
   깔때기 집계에서 `no_data` 로 따로 센다.

   그렇다고 판정 불가를 통과시키지도 않는다. 5년 순이익 중 한 해가
   비었으면 '적자 없음'을 확인한 게 아니다.

3) **필터는 순차 단락(short-circuit)이다.** F1 에서 떨어지면 F2 를 보지
   않는다. 깔때기의 각 칸이 '이 필터에서 처음 떨어진 수'를 뜻해야
   합계가 맞는다. 모든 필터를 다 평가하면 한 종목이 여러 칸에 잡혀
   깔때기가 아니라 교집합 표가 된다.

★ F6 은 반쪽이다 — 그 사실을 기록한다
----------------------------------
PER 은 구할 수 있다. `tickers.market_cap` 과 `dividends.net_income` 이
둘 다 원 단위이므로 `PER = 시가총액 / 당기순이익` 이다. 추가 조회가 없다.

**PBR 은 구할 수 없다.** 자본총계가 이 저장소에 없다. FDR 전종목
스냅샷에는 BPS·PBR 컬럼이 없고(2026-09 실측 컬럼: 종목명·시장·시가총액·
상장주식수), DART 에서 받으려면 전종목 재조회가 한 번 더 붙는다.
그래서 **PBR 조건은 건너뛰고 건너뛴 사실을 `skipped` 컬럼에 남긴다.**
없는 값을 추정해서 채우면 필터가 통과시킨 근거가 거짓이 된다.

업종 중앙값도 표본이 적으면 의미가 없다. 업종에 3종목만 있으면 중앙값이
사실상 자기 자신이다. `sector_min_members` 미만이면 F6 전체를 스킵한다.
'미분류' 업종도 스킵한다 — 서로 무관한 종목들의 중앙값이다.
"""
from __future__ import annotations

import logging

import pandas as pd

from .config import DEFAULT, Config
from .trading_day import now_kst

log = logging.getLogger(__name__)

__all__ = [
    "FILTERS", "collect_dividend_screen", "screen_dividends", "screen_one",
    "sector_per_medians",
]

# 필터 이름과 한 줄 설명. 리포트와 깔때기가 같은 어휘를 쓰게 한다.
FILTERS: tuple[tuple[str, str], ...] = (
    ("F1", "배당수익률"),
    ("F2", "연속성"),
    ("F3", "배당성향"),
    ("F4", "FCF 커버"),
    ("F5", "이익 안정"),
    ("F6", "밸류에이션"),
)

_EPS = 1e-9


def _num(v):
    """None / NaN 을 None 으로 통일한다. NaN 은 비교가 전부 False 라 위험하다."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f


def per_of(market_cap, net_income) -> float | None:
    """PER = 시가총액 / 당기순이익. 적자·결측이면 None.

    적자에 PER 을 매기면 음수가 나오고, '중앙값 이하'라는 조건에서
    적자 기업이 가장 싼 종목으로 뽑힌다. 정확히 반대다.
    """
    mc, ni = _num(market_cap), _num(net_income)
    if mc is None or ni is None or ni <= 0 or mc <= 0:
        return None
    return mc / ni


def _median(values: list[float]) -> float | None:
    vals = sorted(v for v in values if v is not None)
    n = len(vals)
    if n == 0:
        return None
    mid = n // 2
    if n % 2:
        return float(vals[mid])
    return (float(vals[mid - 1]) + float(vals[mid])) / 2.0


def sector_per_medians(per_by_code: dict, sector_by_code: dict, *,
                       min_members: int = 5,
                       unknown: str = "미분류") -> dict:
    """`{업종: PER 중앙값}`. 표본이 적은 업종과 미분류는 넣지 않는다.

    반환에 없는 업종은 F6 스킵 대상이다. 0 이나 None 을 넣어두면 호출부가
    그것을 중앙값으로 오해할 수 있으므로 키 자체를 만들지 않는다.
    """
    buckets: dict[str, list[float]] = {}
    for code, per in per_by_code.items():
        per = _num(per)
        if per is None or per <= 0:
            continue
        sec = (sector_by_code.get(code) or "").strip()
        if not sec or sec == unknown:
            continue
        buckets.setdefault(sec, []).append(per)
    out = {}
    for sec, vals in buckets.items():
        if len(vals) < min_members:
            continue
        med = _median(vals)
        if med is not None:
            out[sec] = med
    return out


# ══════════════════════════ 필터 ══════════════════════════
# 각 필터는 (판정, 사유) 를 돌려준다.
#   True   통과
#   False  조건 미달 — 조건이 빡빡한지 판단할 대상
#   None   판정 불가 (데이터 없음) — 수집이 덜 된 것
def _f1_yield(m: dict, cfg: Config):
    y = m.get("div_yield")
    if y is None:
        return None, "현재가 또는 배당금 없음"
    if y + _EPS < cfg.dividend.min_yield:
        return False, f"수익률 {y:.2f}% < {cfg.dividend.min_yield:.1f}%"
    return True, ""


def _f2_streak(m: dict, cfg: Config):
    need = cfg.dividend.min_years
    if m.get("years_checked", 0) < need:
        return None, f"연도 데이터 {m.get('years_checked', 0)}년 < {need}년"
    if m.get("years_paid", 0) < need:
        return False, f"연속 배당 {m.get('years_paid', 0)}년 < {need}년"
    cut = m.get("cut_year")
    if cut:
        return False, f"{cut} 감액"
    return True, ""


def _f3_payout(m: dict, cfg: Config):
    p = m.get("payout")
    if p is None:
        return None, "배당성향 없음 (순이익 결측 또는 적자)"
    if p <= 0:
        return False, f"배당성향 {p:.2f}% <= 0"
    if p > cfg.dividend.max_payout + _EPS:
        return False, f"배당성향 {p:.2f}% > {cfg.dividend.max_payout:.0f}%"
    return True, ""


def _f4_fcf(m: dict, cfg: Config):
    fcf, td = m.get("fcf"), m.get("total_dividend")
    if fcf is None or td is None:
        return None, "FCF 또는 배당총액 없음"
    if fcf + _EPS < td:
        return False, (f"FCF {fcf / 1e8:,.0f}억 < 배당총액 "
                       f"{td / 1e8:,.0f}억")
    return True, ""


def _f5_profit(m: dict, cfg: Config):
    need = cfg.dividend.min_years
    if m.get("ni_years", 0) < need:
        return None, f"순이익 데이터 {m.get('ni_years', 0)}년 < {need}년"
    loss = m.get("loss_years") or []
    if loss:
        return False, "적자 " + ", ".join(str(y) for y in loss)
    return True, ""


def _f6_value(m: dict, cfg: Config):
    """F6 만 '판정 불가 = 스킵'이다.

    F1~F5 는 데이터가 없으면 탈락으로 센다. 우리가 수집하는 항목이라
    비어 있으면 수집이 덜 된 것이고, 그 상태로 통과시키면 근거 없이
    통과한 종목이 생긴다.

    F6 은 다르다. PBR 은 애초에 구할 수 없고 PER 도 업종 표본이 적으면
    조건을 만들 수 없다. 그걸 탈락으로 세면 '밸류에이션이 비싸서'가
    아니라 '데이터가 없어서' 떨어진 종목이 깔때기 F6 칸을 채운다.
    그래서 스킵하고 `skipped` 에 남긴다 — 통과 근거에서 F6 을 뺀다.
    """
    per, med = m.get("per"), m.get("sector_per_median")
    if per is None or med is None:
        return True, ""
    if per > med + _EPS:
        return False, f"PER {per:.1f} > 업종 중앙값 {med:.1f}"
    return True, ""


_FILTER_FN = {"F1": _f1_yield, "F2": _f2_streak, "F3": _f3_payout,
              "F4": _f4_fcf, "F5": _f5_profit, "F6": _f6_value}


# ══════════════════════════ 종목 1건 ══════════════════════════
def screen_one(code: str, name: str, years: dict, *, price=None,
               market_cap=None, sector: str = "",
               sector_per_median=None, base_year: int,
               cfg: Config = DEFAULT) -> dict:
    """한 종목을 F1~F6 으로 판정한다. 순수 함수 — DB·네트워크 없음.

    years : `{사업연도: {dps, total_dividend, net_income, payout_ratio,
                         fcf, status}}`
    """
    dc = cfg.dividend
    wanted = [base_year - i for i in range(dc.min_years)]
    latest = years.get(base_year) or {}

    price = _num(price)
    dps = _num(latest.get("dps"))
    status = str(latest.get("status") or "")
    # 무배당 확정이면 배당금 0 이다. 데이터가 없는 것과 다르다.
    if dps is None and status == "none":
        dps = 0.0
    div_yield = None
    if price is not None and price > 0 and dps is not None:
        div_yield = dps / price * 100.0

    # ── 연속성: 오래된 연도부터 훑어 감액 연도를 찾는다 ──
    years_checked = sum(1 for y in wanted if (years.get(y) or {}).get("status"))
    paid_flags = [str((years.get(y) or {}).get("status") or "") == "paid"
                  for y in wanted]
    years_paid = sum(paid_flags)
    cut_year = None
    ordered = sorted(wanted)
    prev = None
    for y in ordered:
        cur = _num((years.get(y) or {}).get("dps"))
        if cur is None:
            prev = None
            continue
        if prev is not None and cur + _EPS < prev:
            cut_year = y
            break
        prev = cur

    ni_by_year = {y: _num((years.get(y) or {}).get("net_income"))
                  for y in wanted}
    ni_years = sum(1 for v in ni_by_year.values() if v is not None)
    loss_years = sorted(y for y, v in ni_by_year.items()
                        if v is not None and v <= 0)

    payout = _num(latest.get("payout_ratio"))
    per = per_of(market_cap, latest.get("net_income"))

    m = {
        "code": code, "name": name, "fiscal_year": base_year,
        "price": price, "dps": dps,
        "div_yield": None if div_yield is None else round(div_yield, 2),
        "years_checked": years_checked, "years_paid": years_paid,
        "cut_year": cut_year,
        "payout": payout,
        "fcf": _num(latest.get("fcf")),
        "total_dividend": _num(latest.get("total_dividend")),
        "net_income": _num(latest.get("net_income")),
        "ni_years": ni_years, "loss_years": loss_years,
        "per": None if per is None else round(per, 2),
        "sector": sector or "",
        "sector_per_median": (None if sector_per_median is None
                              else round(float(sector_per_median), 2)),
    }

    # PBR 은 이 저장소에서 구할 수 없다. 스킵 사실을 남긴다 (모듈 독스트링).
    skipped = ["F6-PBR"]
    if m["per"] is None or m["sector_per_median"] is None:
        skipped.append("F6-PER")

    failed_at, reason, no_data = None, "", False
    for key, _label in FILTERS:
        ok, why = _FILTER_FN[key](m, cfg)
        if ok is True:
            continue
        failed_at, reason = key, why
        no_data = ok is None
        break

    m.update({"passed": failed_at is None, "failed_at": failed_at,
              "reason": reason, "no_data": no_data,
              "skipped": ",".join(skipped)})
    return m


# ══════════════════════════ 전종목 ══════════════════════════
def screen_dividends(store, *, cfg: Config = DEFAULT, base_year: int | None = None,
                     trade_date: str | None = None) -> dict:
    """전종목 배당 필터. 결과를 돌려주기만 하고 저장하지 않는다.

    저장은 `collect_dividend_screen` 이 한다 — 순수 판정과 부작용을
    나눠 두면 판정만 픽스처로 검증할 수 있다.
    """
    from .dividend_data import latest_fiscal_year

    dc = cfg.dividend
    base = base_year if base_year is not None else latest_fiscal_year()
    td = trade_date or store.last_price_date()

    out: dict = {"scan_date": td, "base_year": base, "evaluated": 0,
                 "passed": 0, "top": [], "rows": [],
                 "funnel": {k: 0 for k, _ in FILTERS},
                 "no_data": {k: 0 for k, _ in FILTERS},
                 "skipped_per": 0}

    div = store.dividends_on(base)
    if div is None or div.empty:
        out["reason"] = "no_dividends"
        return out

    # 최근 min_years 연도를 한 번에 읽어 종목별로 묶는다. 종목마다
    # dividends_of() 를 부르면 2,500번 쿼리한다.
    years_by_code: dict[str, dict] = {}
    for y in range(base - dc.min_years + 1, base + 1):
        frame = store.dividends_on(y)
        if frame is None or frame.empty:
            continue
        for r in frame.to_dict("records"):
            years_by_code.setdefault(str(r["code"]), {})[int(y)] = r

    meta = store.ticker_meta()
    names = {c: str(r.get("name") or c) for c, r in meta.iterrows()}
    sectors = {c: (str(r.get("sector")) if r.get("sector") else "")
               for c, r in meta.iterrows()}
    caps = {c: _num(r.get("market_cap")) for c, r in meta.iterrows()}

    # 현재가는 **기준일 그 행**에서 뽑는다. 행렬의 마지막 행을 쓰면
    # 안 된다 — `last_price_date()` 는 부분 적재된 날짜를 건너뛰므로
    # 마지막 행이 기준일이 아닐 수 있다. 장중에 일부만 들어온 날의
    # 종가로 수익률을 계산하면 조용히 틀린 값이 나온다.
    prices: dict[str, float] = {}
    if td:
        pm = store.price_matrix(days=10)
        if pm is not None and not pm.empty:
            key = pd.Timestamp(str(td)[:10])
            if key in pm.index:
                row = pm.loc[key]
                prices = {str(k): _num(v) for k, v in row.items()
                          if _num(v) is not None}
            else:
                # 기준일 시세가 없으면 다른 날 값으로 대신하지 않는다.
                # F1 이 전부 '판정 불가'로 남아 사람이 알아챈다.
                log.warning("기준일 %s 시세가 가격행렬에 없습니다 — "
                            "수익률을 계산하지 않습니다", td)
    out["priced"] = len(prices)

    per_by_code = {c: per_of(caps.get(c),
                             (years_by_code.get(c, {}).get(base) or {})
                             .get("net_income"))
                   for c in years_by_code}
    medians = sector_per_medians(per_by_code, sectors,
                                 min_members=dc.sector_min_members)

    rows = []
    for code in sorted(years_by_code):
        if code not in names:          # 비활성/상장폐지
            continue
        sec = sectors.get(code, "")
        m = screen_one(code, names[code], years_by_code[code],
                       price=prices.get(code), market_cap=caps.get(code),
                       sector=sec, sector_per_median=medians.get(sec),
                       base_year=base, cfg=cfg)
        m["scan_date"] = td
        rows.append(m)
        out["evaluated"] += 1
        if m["passed"]:
            out["passed"] += 1
        else:
            out["funnel"][m["failed_at"]] += 1
            if m["no_data"]:
                out["no_data"][m["failed_at"]] += 1
        if "F6-PER" in m["skipped"]:
            out["skipped_per"] += 1

    # 통과 종목만 수익률 내림차순으로 순위를 매긴다.
    winners = [r for r in rows if r["passed"]]
    winners.sort(key=lambda r: (-(r["div_yield"] or 0.0), r["code"]))
    for i, r in enumerate(winners, 1):
        r["rank"] = i
    out["rows"] = rows
    out["top"] = winners[:dc.top_n]
    return out


def collect_dividend_screen(store, *, cfg: Config = DEFAULT,
                            base_year: int | None = None,
                            trade_date: str | None = None,
                            force: bool = False) -> dict:
    """필터를 돌려 `dividend_screen` 에 적재한다. 하루 1회.

    **실패해도 예외를 올리지 않는다.** 호출부(리포트)가 이것 때문에
    죽으면 손해가 더 크다.
    """
    out: dict = {"scan_date": trade_date, "evaluated": 0, "passed": 0,
                 "stored": 0, "skipped": False}
    try:
        res = screen_dividends(store, cfg=cfg, base_year=base_year,
                               trade_date=trade_date)
        td = res.get("scan_date")
        if not td:
            out["reason"] = "no_trade_date"
            return out
        out.update({k: res[k] for k in ("scan_date", "base_year", "evaluated",
                                        "passed", "funnel", "no_data",
                                        "skipped_per")
                    if k in res})
        if res.get("reason"):
            out["reason"] = res["reason"]
            return out
        if not force and store.has_dividend_screen(td):
            out["skipped"] = True
            return out
        out["stored"] = store.upsert_dividend_screen(res["rows"])
        out["top"] = res["top"]
    except Exception as exc:                # noqa: BLE001
        log.warning("배당 필터 기록 실패 — 건너뜁니다 (%s: %s)",
                    type(exc).__name__, exc)
        out["error"] = f"{type(exc).__name__}: {exc}"
        return out
    log.info("배당 필터 %s · 평가 %d종목 · 통과 %d · 적재 %d (%s)",
             out["scan_date"], out["evaluated"], out["passed"],
             out["stored"], now_kst().strftime("%H:%M:%S"))
    return out
