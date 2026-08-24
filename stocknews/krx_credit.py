# -*- coding: utf-8 -*-
"""종목별 신용거래융자 잔고 — 자동 수집 가능성 진단.

결론부터: 자동 수집할 수 없다. 데이터가 공개되지 않는다.
======================================================

2026-08-24 에 후보 소스를 전부 직접 두드려 확인한 결과다. 추측이 아니다.

  KRX 정보데이터시스템 / Data Marketplace
      통계 메뉴 464개를 전량 덤프해 검색했다.
        '융자' → "인프라투융자회사 시세" 1건. 신용융자와 무관하다.
        '신용' → 6건 전부 채권 발행사 **신용등급**(13210, 13211,
                 14023, 14024). 신용거래융자가 아니다.
        '잔고' → 5건 전부 **공매도** 순보유잔고(33001~33004).
      즉 종목별 신용거래융자 잔고 화면이 존재하지 않는다.

  KRX getJsonData.cmd
      알려진 bld(MDCSTAT01501 전종목시세, MDCSTAT01701 등락률)에도
      `HTTP 400`, 본문 `LOGOUT` 을 돌려준다. 로더 페이지로 JSESSIONID
      를 받아도 같다. 익명 조회 경로가 닫혔다.

  네이버 금융
      종목 메인(main.naver)과 외국인·기관(frgn.naver) 페이지에서
      '신용' 0건, '융자' 0건.

  FinanceDataReader
      StockListing 키는 KRX / KRX-DELISTING / KRX-ADMINISTRATIVE 뿐이고
      신용 관련 컬럼이 없다.

그래서 이 모듈은 bld 를 추측하지 않는다
--------------------------------------
예전 구현은 후보 bld 4개를 넣고 돌려봤다. 찾을 대상이 아예 없으므로
그 목록은 영원히 실패한다. 더 나쁜 건 **빈 응답을 '신용잔고 0'으로
오해할 위험**이다. 그래서 `CANDIDATE_BLDS` 를 비웠고, 스모크 테스트가
비어 있는지 확인한다(추측이 다시 들어오는 것을 막는 회귀 방지).

`KRX_CREDIT_BLD` 환경변수는 남겼다. KRX 가 나중에 이 통계를 공개하면
코드를 고치지 않고 값만 넣어 살릴 수 있다. 명시적으로 주지 않으면
네트워크 요청조차 하지 않는다.

실제로 쓸 수 있는 경로
--------------------
  1) 수동 CSV (`data/credit_manual.csv`) — 현재 유일하게 동작한다.
     주입한 종목만 실측으로 채점되고, 나머지는 매물대 POC 프록시다.
  2) 증권사 API — 종목별 신용잔고를 실제로 주는 유일한 자동 경로다.
     키움 OpenAPI+ `opt10013`(신용매매동향) 등. 계좌와 로그인이 필요해
     이 저장소만으로는 구성할 수 없다.

`--mode credit-probe` 는 위 진단을 **실행 시점에 다시 검증**한다.
KRX 가 정책을 바꿨는지 사람이 확인하지 않아도 알 수 있게 하려는 것이다.
"""
from __future__ import annotations

import logging
import os
import random
import re
import time
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests

log = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))
BASE = "https://data.krx.co.kr"
ENDPOINT = f"{BASE}/comm/bldAttendant/getJsonData.cmd"
MENU_URL = f"{BASE}/contents/MDC/MAIN/main/index.cmd"
REFERER = f"{BASE}/contents/MDC/MDI/mdiLoader/index.cmd"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# 비워 둔다. 이유는 모듈 독스트링 참조. 스모크 테스트가 비었는지 확인한다.
CANDIDATE_BLDS: tuple[str, ...] = ()

# 신용거래융자로 볼 수 있는 메뉴명. '신용등급'은 채권이라 제외한다.
CREDIT_MENU_HINTS = ("신용거래융자", "신용융자", "신용잔고", "신용공여",
                     "신용거래")
MENU_NAME_RE = re.compile(r'data-menu-name="([^"]*)"')

# 응답 컬럼 탐색 패턴 (KRX 는 컬럼명을 자주 바꾼다)
TICKER_HINTS = ("ISU_SRT_CD", "ISU_CD", "SRT_CD", "종목코드")
NAME_HINTS = ("ISU_ABBRV", "ISU_NM", "종목명")
BALANCE_QTY_HINTS = ("LOAN_BAL_QTY", "BAL_QTY", "LOAN_QTY", "신용잔고수량",
                     "잔고수량", "LOAN_BALANCE_QTY")
BALANCE_AMT_HINTS = ("LOAN_BAL_AMT", "BAL_AMT", "신용잔고금액")
RATIO_HINTS = ("LOAN_BAL_RTO", "BAL_RTO", "신용잔고비율", "잔고비율")
LISTED_HINTS = ("LIST_SHRS", "ISU_SHRS", "상장주식수")

__all__ = ["fetch_credit_balance", "diagnose", "probe", "refresh_credit_auto",
           "configured_bld", "menu_credit_hits", "CANDIDATE_BLDS",
           "CREDIT_MENU_HINTS"]


def configured_bld(bld: str | None = None) -> str | None:
    """명시적으로 지정된 bld. 없으면 None (추측하지 않는다)."""
    return (bld or os.getenv("KRX_CREDIT_BLD") or "").strip() or None


def _post(bld: str, trdDd: str, extra: dict | None = None,
          timeout: float = 15.0) -> dict | None:
    payload = {
        "bld": bld,
        "locale": "ko_KR",
        "trdDd": trdDd,
        "mktId": "ALL",
        "share": "1",
        "money": "1",
        "csvxls_isNo": "false",
    }
    if extra:
        payload.update(extra)
    headers = {
        "User-Agent": UA,
        "Referer": REFERER,
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Accept": "application/json, text/javascript, */*; q=0.01",
    }
    for attempt in range(3):
        try:
            res = requests.post(ENDPOINT, data=payload, headers=headers,
                                timeout=timeout)
            if res.status_code != 200:
                log.debug("KRX %s → HTTP %s %s", bld, res.status_code,
                          res.text[:40])
                time.sleep(1.5 * (attempt + 1))
                continue
            return res.json()
        except (requests.RequestException, ValueError) as exc:
            if attempt == 2:
                log.warning("KRX 요청 실패 (%s): %s", bld, exc)
            time.sleep(1.0 + attempt + random.random())
    return None


def _probe_endpoint(trdDd: str, timeout: float = 15.0) -> dict:
    """엔드포인트가 익명 조회를 받아주는지 1회 확인. 알려진 bld 로 시험."""
    bld = "dbms/MDC/STAT/standard/MDCSTAT01501"   # 전종목 시세
    headers = {
        "User-Agent": UA, "Referer": REFERER,
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "application/json, text/javascript, */*; q=0.01",
    }
    try:
        res = requests.post(ENDPOINT, headers=headers, timeout=timeout, data={
            "bld": bld, "locale": "ko_KR", "trdDd": trdDd, "mktId": "ALL",
            "share": "1", "money": "1", "csvxls_isNo": "false"})
    except requests.RequestException as exc:
        return {"reachable": False, "note": f"{type(exc).__name__}: {exc}"}
    body = (res.text or "")[:80].strip()
    login_required = res.status_code == 400 and "LOGOUT" in body.upper()
    return {"reachable": res.status_code == 200, "status": res.status_code,
            "body": body, "login_required": login_required,
            "probe_bld": bld}


def menu_credit_hits(html: str) -> list[str]:
    """메뉴 HTML 에서 신용거래융자로 보이는 항목만 뽑는다.

    '신용등급'(채권 발행사)은 제외한다. 네트워크 없이 검증 가능하도록
    파싱만 분리해 뒀다.
    """
    names = MENU_NAME_RE.findall(html or "")
    out = []
    for n in names:
        if "신용등급" in n:
            continue
        if any(k in n for k in CREDIT_MENU_HINTS):
            out.append(n)
    return sorted(set(out))


def _probe_menu(timeout: float = 25.0) -> dict:
    """KRX 통계 메뉴에 신용거래융자 화면이 생겼는지 확인."""
    try:
        res = requests.get(MENU_URL, headers={"User-Agent": UA},
                           timeout=timeout)
    except requests.RequestException as exc:
        return {"ok": False, "note": f"{type(exc).__name__}: {exc}"}
    if res.status_code != 200:
        return {"ok": False, "status": res.status_code}
    names = MENU_NAME_RE.findall(res.text)
    return {"ok": True, "menus": len(names),
            "credit_hits": menu_credit_hits(res.text)}


def fetch_credit_balance(trade_date: str, bld: str | None = None
                         ) -> tuple[pd.DataFrame, str]:
    """종목별 신용잔고 조회. (DataFrame, 사용된 bld) 반환.

    **bld 를 명시하지 않으면 네트워크를 쓰지 않고 즉시 빈 결과를 준다.**
    KRX 가 이 통계를 공개하지 않으므로 추측할 후보가 없다. 자세한 근거는
    모듈 독스트링 참조.

    반환 컬럼: ticker, name, balance_qty, balance_amt, ratio, listed_shares
    """
    target = configured_bld(bld)
    if not target:
        log.info("신용잔고 자동 수집 건너뜀: KRX 는 종목별 신용거래융자 "
                 "잔고를 공개하지 않습니다. 수동 CSV 를 쓰거나 증권사 API "
                 "를 연결하십시오. (--mode credit-probe 로 재확인 가능)")
        return pd.DataFrame(), ""

    data = _post(target, trade_date)
    rows = _rows_of(data or {})
    if not rows:
        log.warning("bld %s → 결과 없음. 코드가 맞는지 확인하십시오.", target)
        return pd.DataFrame(), ""

    cols = list(rows[0].keys())
    c_tk = _pick(cols, TICKER_HINTS)
    c_qty = _pick(cols, BALANCE_QTY_HINTS)
    c_rto = _pick(cols, RATIO_HINTS)
    if not c_tk or not (c_qty or c_rto):
        log.warning("bld %s → 신용잔고 컬럼이 없습니다: %s", target, cols[:12])
        return pd.DataFrame(), ""

    c_nm = _pick(cols, NAME_HINTS)
    c_amt = _pick(cols, BALANCE_AMT_HINTS)
    c_ls = _pick(cols, LISTED_HINTS)

    out = []
    for r in rows:
        code = str(r.get(c_tk, "")).strip().zfill(6)
        if len(code) != 6 or not code.isdigit():
            continue
        out.append({
            "ticker": code,
            "name": str(r.get(c_nm, "")).strip() if c_nm else None,
            "balance_qty": _num(r.get(c_qty)) if c_qty else None,
            "balance_amt": _num(r.get(c_amt)) if c_amt else None,
            "ratio": _num(r.get(c_rto)) if c_rto else None,
            "listed_shares": _num(r.get(c_ls)) if c_ls else None,
        })
    if not out:
        return pd.DataFrame(), ""
    log.info("KRX 신용잔고 %d종목 조회 성공 (bld=%s)", len(out), target)
    return pd.DataFrame(out), target


def _rows_of(data: dict) -> list[dict]:
    """KRX 는 결과 배열 키를 여러 이름으로 쓴다. 가장 큰 리스트를 고른다."""
    if not isinstance(data, dict):
        return []
    best: list = []
    for v in data.values():
        if isinstance(v, list) and v and isinstance(v[0], dict):
            if len(v) > len(best):
                best = v
    return best


def _pick(cols: list[str], hints: tuple[str, ...]) -> str | None:
    for h in hints:
        for c in cols:
            if c == h:
                return c
    for h in hints:
        for c in cols:
            if h in c:
                return c
    return None


def _num(v) -> float | None:
    if v is None:
        return None
    t = str(v).replace(",", "").replace("%", "").strip()
    if not t or t in ("-", "--"):
        return None
    try:
        return float(t)
    except ValueError:
        return None


def diagnose(trade_date: str, extra_blds: tuple = ()) -> dict:
    """신용잔고 자동 수집이 지금도 불가능한지 실행 시점에 재검증한다.

    결론을 문서에만 적어두면 KRX 가 정책을 바꿨을 때 아무도 모른다.
    그래서 세 가지를 실제로 확인한다.

      endpoint : getJsonData.cmd 가 익명 조회를 받아주는가
      menu     : 통계 메뉴에 신용거래융자 화면이 생겼는가
      bld      : 사용자가 지정한 bld 가 실제로 신용잔고를 주는가

    반환에 `verdict` 와 `next_steps` 가 들어간다.
    """
    report: dict = {"trade_date": trade_date}

    report["endpoint"] = _probe_endpoint(trade_date)
    time.sleep(0.5)
    report["menu"] = _probe_menu()

    tested = []
    for b in [x for x in (configured_bld(), *extra_blds) if x]:
        df, used = fetch_credit_balance(trade_date, bld=b)
        tested.append({"bld": b, "rows": int(len(df)), "usable": bool(used)})
        time.sleep(0.8)
    report["bld_tested"] = tested

    usable = next((t["bld"] for t in tested if t["usable"]), None)
    menu_hits = report["menu"].get("credit_hits") or []

    if usable:
        report["verdict"] = "가능"
        report["next_steps"] = [f".env 에 KRX_CREDIT_BLD={usable} 고정"]
    elif menu_hits:
        report["verdict"] = "메뉴에 신용 화면이 보입니다 — 재조사 필요"
        report["next_steps"] = [
            f"KRX 메뉴에서 발견: {', '.join(menu_hits)}",
            "그 화면을 열고 F12 > Network > 조회 버튼 클릭",
            "getJsonData.cmd 요청 Payload 의 bld 값 복사",
            "--mode credit-probe --bld <값> 으로 확인",
        ]
    else:
        report["verdict"] = "불가 — KRX 가 이 통계를 공개하지 않습니다"
        steps = ["data/credit_manual.csv 수동 주입을 쓰십시오 "
                 "(--mode credit)"]
        if report["endpoint"].get("login_required"):
            steps.append("참고: getJsonData.cmd 가 로그인을 요구합니다 "
                         "(HTTP 400 LOGOUT). 익명 조회 경로가 닫혔습니다.")
        steps.append("실측 자동화를 원하면 증권사 API 가 유일한 경로입니다 "
                     "(키움 OpenAPI+ opt10013 신용매매동향 등).")
        report["next_steps"] = steps
    return report


# 이전 이름. run_screen 과 외부 호출 호환을 위해 남긴다.
probe = diagnose


def refresh_credit_auto(store, trade_date: str | None = None,
                        bld: str | None = None) -> dict:
    """신용잔고 자동 적재 시도. bld 가 지정되지 않으면 즉시 건너뛴다.

    비율이 없으면 상장주식수로 역산한다. 응답의 상장주식수를 우선 쓰고,
    없으면 마스터의 값을 쓴다.
    """
    if not configured_bld(bld):
        return {"ok": False, "skipped": True,
                "note": "KRX 는 종목별 신용거래융자 잔고를 공개하지 않습니다. "
                        "수동 CSV 또는 증권사 API 를 쓰십시오. "
                        "(--mode credit-probe 로 재확인)"}

    td = trade_date or store.last_price_date()
    if not td:
        return {"ok": False, "note": "시세가 없어 기준일을 정할 수 없음"}
    ymd = str(td).replace("-", "")[:8]

    df, used = fetch_credit_balance(ymd, bld=bld)
    if df.empty:
        return {"ok": False, "trade_date": td, "bld": used or None,
                "note": "조회 실패 — 수동 CSV 사용 또는 bld 확인 필요"}

    meta = store.ticker_meta()
    rows, derived = [], 0
    for _, r in df.iterrows():
        ratio = r.get("ratio")
        qty = r.get("balance_qty")
        if (ratio is None or pd.isna(ratio)) and qty and pd.notna(qty):
            listed = r.get("listed_shares")
            if (listed is None or pd.isna(listed)) and r["ticker"] in meta.index:
                v = meta.at[r["ticker"], "shares"]
                listed = float(v) if pd.notna(v) else None
            if listed and listed > 0:
                ratio = round(float(qty) / float(listed) * 100.0, 3)
                derived += 1
        if ratio is None or pd.isna(ratio):
            continue
        rows.append({"ticker": r["ticker"], "ratio": float(ratio),
                     "shares": float(qty) if qty and pd.notna(qty) else None,
                     "asof": str(td)[:10], "source": f"krx:{used}",
                     "note": None})

    stored = store.upsert_credit(rows)
    cov = store.credit_coverage()
    log.info("KRX 신용잔고 적재 %d종목 (비율 역산 %d) · 커버리지 %d/%d",
             stored, derived, cov["with_credit"], cov["active"])
    return {"ok": True, "trade_date": str(td)[:10], "bld": used,
            "fetched": int(len(df)), "stored": stored, "derived": derived,
            "coverage": cov}
