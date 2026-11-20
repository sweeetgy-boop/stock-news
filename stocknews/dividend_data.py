# -*- coding: utf-8 -*-
"""배당 데이터 수집 (OpenDART).

이 모듈은 **수집과 저장만** 한다. 점수·추천·스크리닝·알림에 값을 넣지
않는다. 필터 엔진은 별도 모듈의 몫이다.

★ 규격 — 추측이 아니라 2026-09-01 실측이다
------------------------------------------
`GET https://opendart.fss.or.kr/api/alotMatter.json`
    ?corp_code=00126380&bsns_year=2025&reprt_code=11011

응답 `list` 의 한 행은 이렇게 생겼다.

    {"se": "주당 현금배당금(원)", "stock_knd": "보통주",
     "thstrm": "1,668", "frmtrm": "1,446", "lwfr": "1,444",
     "stlm_dt": "2025-12-31"}

여기서 알아야 할 것이 넷이다.

  1) **한 번의 호출이 3개 사업연도를 준다.** thstrm=당기, frmtrm=전기,
     lwfr=전전기. 그래서 5년을 받으려면 종목당 2회면 된다(bsns_year 를
     Y 와 Y-3 으로). 연도마다 부르면 5회다 — 2.5배 낭비다.
  2) **`se` 에 단위가 붙어 있다.** `현금배당금총액(백만원)` 은 백만원
     단위다. 원으로 통일하려면 라벨에서 단위를 읽어 곱해야 한다.
     하드코딩하면 `(원)` 으로 보고하는 필러에서 100만배 틀린다.
  3) **`stock_knd` 로 우선주 행이 섞여 온다.** 삼성전자는 보통주 1,668원
     / 우선주 1,669원이 같은 응답에 있다. 보통주만 골라야 한다.
  4) **`stlm_dt` 가 결산일을 준다.** 사업연도만으로는 12월 결산인지
     3월 결산인지 알 수 없어 배당기준일을 계산할 수 없다. 같은 응답에
     공짜로 오므로 같이 저장한다 — 나중에 받으려면 전종목 재수집이다.

`GET .../fnlttSinglAcntAll.json?...&fs_div=CFS` 는 전체 재무제표를 준다.
`sj_div == "CF"` 인 행만 보면 현금흐름표다. 여기서도 3개 연도가 온다
(thstrm_amount / frmtrm_amount / bfefrmtrm_amount).

    {"sj_div": "CF",
     "account_id": "ifrs-full_PurchaseOfPropertyPlantAndEquipment...",
     "account_nm": "유형자산의 취득", "thstrm_amount": "47522179000000"}

**부호를 믿을 수 없다.** 실측: 삼성전자 2025 `유형자산의 취득` 이
`+47,522,179백만`, `자기주식의 취득` 이 `+8,189,263백만` 으로 유출인데
양수다. 반면 `투자활동현금흐름` 은 음수다. 필러마다 규약이 갈린다.
그래서 CAPEX 는 절대값으로 더한다.

계정 식별은 `account_id`(IFRS 표준코드)를 1순위로 본다. 한글 계정명은
회사마다 갈리지만 표준코드는 같다. 이름 폴백은 **정확히 일치**하는
집합으로만 둔다 — 느슨한 정규식은 `영업에서 창출된 현금흐름`(이자·법인세
차감 전 소계)을 영업활동현금흐름으로 오인해 값을 부풀린다.

★ 무배당과 누락을 섞지 않는다
---------------------------
`status` 컬럼이 셋을 구분한다.

    paid       주당 현금배당금 > 0
    none       그 연도를 보고했고 주당 현금배당금이 '-' — **무배당 확정**
    no_report  그 연도 칸이 비어 있다 (상장 전 · 공시 없음)

조회 자체가 실패한 종목은 **행을 쓰지 않는다.** 실패를 0 으로 적으면
다음 실행이 그 값을 믿고 무배당으로 채점한다. 실패는 수집 결과의
`failures` 로만 보고한다.

`fiscal_year` 는 사업연도이고 `settle_dt` 가 그 사업연도의 결산일이다.
12월 결산이 아닌 회사도 있으므로 사업연도만으로 날짜를 유추하면 안 된다.
"""
from __future__ import annotations

import logging
import os
import re
import time
from datetime import datetime

from .flags import _dart_get, _to_num, dart_corp_codes
from .kiwoom import RateLimiter
from .trading_day import now_kst
from .universe import _is_common_share

log = logging.getLogger(__name__)

__all__ = [
    "REPRT_ANNUAL", "collect_dividends", "fetch_one", "free_cash_flow",
    "latest_fiscal_year", "parse_alot_matter", "parse_cashflow",
    "payout_ratio", "resolve_status", "year_batches",
]

# 사업보고서. 배당에 관한 사항은 연간 확정치를 여기서 본다.
REPRT_ANNUAL = "11011"

# ── DART 유량 제한 ──
# 공개된 제한은 분당 1,000회다. 초당 제한은 문서에 없으므로 분당값을
# 60 으로 나눈 보수값을 쓴다. 일일 한도는 별도이고(키 등급에 따라 다름)
# 창으로 막을 수 없으므로, 총 호출 수를 결과에 찍어 사람이 보게 한다.
DART_PER_SECOND = 12
DART_PER_MINUTE = 900
DART_PER_HOUR = 18_000

# `se` 라벨 끝의 단위. 원으로 통일한다.
_UNIT_SCALE = {"원": 1.0, "천원": 1e3, "백만원": 1e6,
               "억원": 1e8, "십억원": 1e9, "조원": 1e12}

# 항목명 (공백 제거 후 비교)
_SE_DPS = "주당현금배당금"
_SE_TOTAL = "현금배당금총액"
_SE_PAR = "주당액면가액"
_SE_NI = {"(연결)당기순이익": "cfs", "(별도)당기순이익": "ofs"}
_SE_PAYOUT = {"(연결)현금배당성향": "cfs", "(별도)현금배당성향": "ofs"}

# 응답의 3개 연도 칸 → 기준 사업연도로부터 몇 년 전인가
_ALOT_TERMS = {"thstrm": 0, "frmtrm": 1, "lwfr": 2}
_CF_TERMS = {"thstrm_amount": 0, "frmtrm_amount": 1, "bfefrmtrm_amount": 2}

# 현금흐름표 계정. account_id 가 1순위, 이름은 정확 일치 폴백.
_OCF_IDS = frozenset({
    "ifrs-full_CashFlowsFromUsedInOperatingActivities",
})
_OCF_NAMES = frozenset({
    "영업활동현금흐름", "영업활동으로인한현금흐름",
    "영업활동으로인한순현금흐름", "영업활동순현금흐름",
})
_CAPEX_IDS = frozenset({
    "ifrs-full_PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities",
    "ifrs-full_PurchaseOfIntangibleAssetsClassifiedAsInvestingActivities",
})
_CAPEX_NAMES = frozenset({
    "유형자산의취득", "무형자산의취득",
})
# 자사주 취득액. 총주주환원율(배당 + 자사주)의 분자다.
#
# **주요사항보고서를 파싱하지 않는다.** 현금흐름표 재무활동에 이미 있다
# (2026-09-01 실측: 삼성전자 2025 `자기주식의 취득` 8,189,263백만 ·
# KT&G 560,112백만). 같은 응답에 오므로 추가 조회가 없고, 공시 본문
# 파싱보다 훨씬 안정적이다.
#
# 소각(retirement)은 더하지 않는다. 소각은 이미 취득한 주식을 없애는
# 것이라 추가 현금 유출이 없다. 취득액에 소각을 더하면 같은 돈을 두 번
# 센다.
_BUYBACK_IDS = frozenset({
    "dart_AcquisitionOfTreasuryShares",
    "ifrs-full_PurchaseOfTreasuryShares",
})
_BUYBACK_NAMES = frozenset({
    "자기주식의취득", "자기주식취득", "자기주식의매입",
})


# ══════════════════════════ 순수 계산 ══════════════════════════
def latest_fiscal_year(now: datetime | None = None) -> int:
    """사업보고서가 나와 있는 가장 최근 사업연도.

    사업보고서 제출 기한이 사업연도 종료 후 90일(3월 말)이다. 그래서
    4월 이후면 작년치가 있고, 1~3월이면 재작년치가 최신이다.
    `datetime.now()` 를 쓰면 UTC 호스트에서 연초에 한 해 어긋난다.
    """
    n = now or now_kst()
    return n.year - 1 if n.month >= 4 else n.year - 2


def year_batches(base_year: int, years: int) -> list[int]:
    """`years` 개 사업연도를 덮는 bsns_year 목록.

    응답 하나가 3개 연도를 주므로 3년 간격으로 부르면 된다.
    5년 -> [base, base-3] (2회로 6년을 덮고 뒤는 잘라 쓴다).
    """
    if years < 1:
        return []
    out: list[int] = []
    y = base_year
    covered = 0
    while covered < years:
        out.append(y)
        covered += len(_ALOT_TERMS)
        y -= len(_ALOT_TERMS)
    return out


def _label_unit(label) -> tuple[str, float]:
    """`'현금배당금총액(백만원)'` -> `('현금배당금총액', 1e6)`.

    **마지막 괄호만** 단위로 본다. `(연결)당기순이익(백만원)` 처럼 앞에도
    괄호가 오기 때문이다. 아는 단위가 아니면 라벨을 건드리지 않는다.
    """
    s = str(label or "").strip()
    m = re.search(r"\(([^()]*)\)\s*$", s)
    if not m:
        return s, 1.0
    unit = m.group(1).strip()
    if unit in _UNIT_SCALE:
        return s[:m.start()].strip(), _UNIT_SCALE[unit]
    if unit in ("%", "주"):          # 단위 없는 비율·주수
        return s[:m.start()].strip(), 1.0
    return s, 1.0


def _norm(text) -> str:
    return re.sub(r"\s+", "", str(text or ""))


def _knd_priority(stock_knd) -> int:
    """보통주 행을 고르는 우선순위. 우선주는 0(배제)."""
    s = _norm(stock_knd)
    if "우선" in s:
        return 0
    if "보통" in s:
        return 2
    return 1 if s in ("", "-") else 0


def payout_ratio(total_dividend, net_income) -> float | None:
    """배당성향(%) = 현금배당금총액 / 당기순이익 x 100.

    순이익이 0 이하면 None 이다. 적자에 배당하면 성향이 음수로 나오고,
    '성향이 낮으니 건전하다'는 해석이 정확히 반대로 뒤집힌다. 없는 값을
    만들지 않고 판정 불가로 남긴다.
    """
    if total_dividend is None or net_income is None:
        return None
    try:
        ni = float(net_income)
        td = float(total_dividend)
    except (TypeError, ValueError):
        return None
    if ni <= 0:
        return None
    return round(td / ni * 100.0, 2)


def free_cash_flow(ocf, capex) -> float | None:
    """FCF = 영업현금흐름 - 자본적지출.

    CAPEX 가 없으면 None 이다. 0 으로 두면 FCF 가 영업현금흐름과 같아져
    커버 판정이 무조건 통과한다 — 없는 데이터로 필터를 통과시키는 셈이다.
    """
    if ocf is None or capex is None:
        return None
    return float(ocf) - float(capex)


def resolve_status(rec: dict) -> str:
    """`paid` / `none` / `no_report`.

    무배당과 누락을 섞지 않는 것이 이 함수의 목적이다. 판정 근거는
    '그 연도 칸이 채워져 있는가'다. 주당액면가액이나 당기순이익이 있으면
    회사가 그 연도를 보고한 것이고, 그때 주당배당금이 `-` 면 무배당이
    확정이다. 아무 칸도 없으면 상장 전이거나 공시가 없다.
    """
    if not rec.get("has_report"):
        return "no_report"
    reported = any(rec.get(k) is not None
                   for k in ("par_value", "net_income", "total_dividend"))
    if not reported or rec.get("dps") is None:
        return "no_report"
    return "paid" if float(rec["dps"]) > 0 else "none"


def parse_alot_matter(rows, base_year: int) -> dict[int, dict]:
    """'배당에 관한 사항' 응답 `list` -> `{사업연도: 레코드}`.

    네트워크를 타지 않는 순수 함수다. 픽스처로 앵커 검증이 된다.
    """
    out: dict[int, dict] = {}
    for back in _ALOT_TERMS.values():
        out[base_year - back] = {
            "dps": None, "total_dividend": None, "par_value": None,
            "net_income": None, "payout_disclosed": None,
            "settle_dt": None, "has_report": False,
            "_ni": {}, "_payout": {},
        }
    best: dict[int, int] = {}

    for r in rows or []:
        head, scale = _label_unit(r.get("se"))
        head = _norm(head)
        pri = _knd_priority(r.get("stock_knd"))
        sdt = str(r.get("stlm_dt") or "").strip()[:10] or None
        for term, back in _ALOT_TERMS.items():
            y = base_year - back
            rec = out[y]
            rec["has_report"] = True
            if sdt and not rec["settle_dt"]:
                rec["settle_dt"] = sdt
            val = _to_num(r.get(term))
            if head == _SE_DPS:
                if pri == 0:                    # 우선주 행
                    continue
                if pri >= best.get(y, 0):
                    # `-` 는 값 없음이 아니라 무배당이다. 0 으로 확정한다.
                    rec["dps"] = 0.0 if val is None else val * scale
                    best[y] = pri
            elif head == _SE_TOTAL:
                rec["total_dividend"] = None if val is None else val * scale
            elif head == _SE_PAR:
                rec["par_value"] = None if val is None else val * scale
            elif head in _SE_NI:
                rec["_ni"][_SE_NI[head]] = (None if val is None
                                            else val * scale)
            elif head in _SE_PAYOUT:
                rec["_payout"][_SE_PAYOUT[head]] = val

    # 연결이 1순위, 없으면 별도. 순서를 나중에 정해야 응답 행 순서에
    # 결과가 흔들리지 않는다.
    for rec in out.values():
        for key, src in (("net_income", "_ni"),
                         ("payout_disclosed", "_payout")):
            box = rec.pop(src)
            rec[key] = box.get("cfs") if box.get("cfs") is not None \
                else box.get("ofs")
    return out


def parse_cashflow(rows, base_year: int) -> dict[int, dict]:
    """전체 재무제표 응답 `list` -> `{사업연도: {ocf, capex, fcf}}`.

    `sj_div == "CF"` 행만 본다. CAPEX 는 부호를 믿지 않고 절대값을
    더한다 (모듈 독스트링의 실측 근거 참조).
    """
    out = {base_year - b: {"ocf": None, "capex": None, "fcf": None,
                           "buyback": None}
           for b in _CF_TERMS.values()}
    for r in rows or []:
        if str(r.get("sj_div") or "").strip().upper() != "CF":
            continue
        aid = str(r.get("account_id") or "").strip()
        nm = _norm(r.get("account_nm"))
        if aid in _OCF_IDS or nm in _OCF_NAMES:
            kind = "ocf"
        elif aid in _CAPEX_IDS or nm in _CAPEX_NAMES:
            kind = "capex"
        elif aid in _BUYBACK_IDS or nm in _BUYBACK_NAMES:
            kind = "buyback"
        else:
            continue
        for field, back in _CF_TERMS.items():
            val = _to_num(r.get(field))
            if val is None:
                continue
            rec = out[base_year - back]
            if kind == "ocf":
                rec["ocf"] = float(val)
            else:
                # 유출인데 부호가 양수로 오는 필러가 있다(모듈 독스트링).
                rec[kind] = (rec[kind] or 0.0) + abs(float(val))
    for rec in out.values():
        rec["fcf"] = free_cash_flow(rec["ocf"], rec["capex"])
        # 현금흐름표를 읽었는데 자사주 취득 행이 없으면 그 해에 매입이
        # 없었다는 뜻이다. 0 으로 확정한다. CF 자체를 못 읽었으면 NULL 로
        # 남긴다 — '매입 안 함'과 '모름'을 섞지 않는다.
        if rec["buyback"] is None and rec["ocf"] is not None:
            rec["buyback"] = 0.0
    return out


def build_rows(code: str, alot: dict, cash: dict, *,
               years: int, base_year: int) -> list[dict]:
    """파싱 결과를 `dividends` 행으로 합친다.

    `no_report` 인 연도도 행으로 남긴다. '조회했고 그 연도는 없었다'와
    '아직 조회하지 않았다'를 구분해야 다음 실행이 헛조회를 반복하지
    않는다.
    """
    wanted = [base_year - i for i in range(years)]
    rows: list[dict] = []
    for y in wanted:
        rec = alot.get(y)
        if rec is None:
            continue
        cf = cash.get(y) or {}
        rows.append({
            "code": code,
            "fiscal_year": int(y),
            "dps": rec.get("dps"),
            "total_dividend": rec.get("total_dividend"),
            "net_income": rec.get("net_income"),
            "payout_ratio": payout_ratio(rec.get("total_dividend"),
                                         rec.get("net_income")),
            "ocf": cf.get("ocf"),
            "capex": cf.get("capex"),
            "fcf": cf.get("fcf"),
            "buyback": cf.get("buyback"),
            "status": resolve_status(rec),
            "settle_dt": rec.get("settle_dt"),
        })
    return rows


# ══════════════════════════ 수집 ══════════════════════════
def _default_get(path: str, params: dict) -> dict | None:
    """DART 호출. 기존 클라이언트를 그대로 쓴다.

    `flags._dart_get` 이 이미 재시도·백오프·status 분기(000/013/020/021)를
    갖고 있다. 여기서 또 한 벌 만들면 DART 클라이언트가 세 벌이 된다.
    """
    return _dart_get(path, params)


def fetch_one(corp_code: str, *, years: int = 5, base_year: int | None = None,
              get=_default_get, limiter: RateLimiter | None = None,
              sleep=time.sleep, with_cashflow: bool = True) -> dict:
    """한 회사의 배당 + 현금흐름. `{"alot","cash","calls","error"}`.

    예외를 올리지 않는다. 실패는 `error` 로 돌려주고 호출부가 격리한다.
    """
    base = base_year if base_year is not None else latest_fiscal_year()
    alot: dict[int, dict] = {}
    cash: dict[int, dict] = {}
    calls = 0

    def _call(path: str, params: dict):
        nonlocal calls
        if limiter is not None:
            limiter.acquire(sleep=sleep)
        calls += 1
        return get(path, params)

    for by in year_batches(base, years):
        data = _call("alotMatter.json",
                     {"corp_code": corp_code, "bsns_year": str(by),
                      "reprt_code": REPRT_ANNUAL})
        if data is None:
            return {"alot": alot, "cash": cash, "calls": calls,
                    "error": "dart_unavailable"}
        alot.update(parse_alot_matter(data.get("list"), by))

    # 무배당 회사는 현금흐름을 조회하지 않는다. FCF 커버 판정에 도달할
    # 수 없으므로 호출만 낭비다 (전종목이면 1,000회 이상 차이가 난다).
    pays = any((r.get("dps") or 0) > 0 for r in alot.values())
    if with_cashflow and pays:
        for fs in ("CFS", "OFS"):
            data = _call("fnlttSinglAcntAll.json",
                         {"corp_code": corp_code, "bsns_year": str(base),
                          "reprt_code": REPRT_ANNUAL, "fs_div": fs})
            if data is None:
                return {"alot": alot, "cash": cash, "calls": calls,
                        "error": "dart_unavailable"}
            got = parse_cashflow(data.get("list"), base)
            if any(v.get("ocf") is not None for v in got.values()):
                cash = got
                break
            # 연결을 제출하지 않는 회사가 있다. 그때만 별도로 한 번 더.
    return {"alot": alot, "cash": cash, "calls": calls, "error": None}


def collect_dividends(store, *, years: int = 5, base_year: int | None = None,
                      limit: int = 0, ttl_days: int = 30, force: bool = False,
                      with_cashflow: bool = True, tickers: dict | None = None,
                      corp_codes: dict | None = None, get=_default_get,
                      limiter: RateLimiter | None = None, sleep=time.sleep,
                      progress=None) -> dict:
    """활성 종목(보통주)의 배당 데이터를 수집해 적재한다.

    월 1회 갱신용이다. nightly 에 넣지 않는다 — 배당은 사업보고서
    시즌에만 바뀌므로 매일 도는 것은 DART 호출만 태우는 짓이다.

    `ttl_days` 안에 이미 받은 종목은 건너뛴다. 그래서 중간에 끊겨도
    같은 명령을 다시 부르면 남은 종목만 받는다.
    """
    base = base_year if base_year is not None else latest_fiscal_year()
    out: dict = {"base_year": base, "years": int(years), "targets": 0,
                 "stored": 0, "rows": 0, "failed": 0, "skipped_ttl": 0,
                 "no_corp_code": 0, "calls": 0,
                 "by_status": {}, "failures": []}

    names = tickers if tickers is not None else store.active_tickers()
    # 우선주는 배당 공시의 주체가 아니다(보통주 행에 함께 실린다).
    # 마스터가 이미 걸러주지만 --include-preferred 로 들어온 경우를 막는다.
    codes = [c for c in sorted(names) if _is_common_share(c)]

    corp = corp_codes if corp_codes is not None else dart_corp_codes()
    if not corp:
        out["error"] = "no_corp_code_map"
        return out

    fresh: set[str] = set()
    if not force and ttl_days > 0:
        cutoff = (now_kst().timestamp() - ttl_days * 86400)
        for c, at in (store.dividend_asof(base) or {}).items():
            try:
                if datetime.fromisoformat(str(at)).timestamp() >= cutoff:
                    fresh.add(c)
            except (TypeError, ValueError):
                continue

    todo = [c for c in codes if c not in fresh]
    out["skipped_ttl"] = len(codes) - len(todo)
    if limit and limit > 0:
        todo = todo[:limit]
    out["targets"] = len(todo)

    if limiter is None:
        limiter = RateLimiter(DART_PER_SECOND, DART_PER_MINUTE, DART_PER_HOUR)

    status_n: dict[str, int] = {}
    for i, code in enumerate(todo, 1):
        cc = corp.get(code)
        if not cc:
            out["no_corp_code"] += 1
            continue
        try:
            got = fetch_one(cc, years=years, base_year=base, get=get,
                            limiter=limiter, sleep=sleep,
                            with_cashflow=with_cashflow)
        except Exception as exc:            # noqa: BLE001
            # 한 종목이 터져도 나머지는 받는다.
            out["failed"] += 1
            if len(out["failures"]) < 20:
                out["failures"].append(
                    {"code": code, "error": f"{type(exc).__name__}: {exc}"})
            continue
        out["calls"] += got["calls"]
        if got["error"]:
            out["failed"] += 1
            if len(out["failures"]) < 20:
                out["failures"].append({"code": code, "error": got["error"]})
            continue
        rows = build_rows(code, got["alot"], got["cash"],
                          years=years, base_year=base)
        if not rows:
            out["failed"] += 1
            if len(out["failures"]) < 20:
                out["failures"].append({"code": code, "error": "empty"})
            continue
        out["rows"] += store.upsert_dividends(rows)
        out["stored"] += 1
        for r in rows:
            status_n[r["status"]] = status_n.get(r["status"], 0) + 1
        if progress is not None:
            progress(i, len(todo), code)

    out["by_status"] = status_n
    out["coverage"] = store.dividend_coverage(base)
    return out


def dart_key_present() -> bool:
    """DART 인증키가 환경에 있는가. 값은 절대 로그로 내지 않는다."""
    return bool(os.getenv("DART_API_KEY"))
