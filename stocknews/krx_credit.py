# -*- coding: utf-8 -*-
"""KRX 신용거래융자 잔고 자동 수집.

정직한 전제
----------
KRX 정보데이터시스템의 통계 조회는 아래 엔드포인트로 JSON 을 돌려준다.

    POST https://data.krx.co.kr/comm/bldAttendant/getJsonData.cmd
    form: bld=dbms/MDC/STAT/standard/MDCSTATNNNNN, trdDd=YYYYMMDD, ...

**그런데 '신용거래융자 종목별 잔고'의 정확한 bld 코드는 확인되지 않았다.**
추측한 코드를 박아두면 조용히 빈 결과가 나오고, 그걸 '신용잔고 0'으로
오해하게 된다. 그래서 이 모듈은 이렇게 만들었다.

  1) bld 코드를 **설정으로 뺀다** (`KRX_CREDIT_BLD` 환경변수 또는 --bld)
  2) `probe()` 로 후보 코드를 시험하고 어느 것이 동작하는지 보고한다
  3) 응답 컬럼명을 하드코딩하지 않고 **패턴으로 탐색**한다
     (KRX 는 컬럼명을 자주 바꾼다)
  4) 실패하면 수동 CSV(`data/credit_manual.csv`)가 그대로 유효하다

bld 코드 찾는 방법 (1분)
----------------------
  1. https://data.krx.co.kr 접속
  2. [통계] > [주식] 에서 신용융자 잔고 화면을 연다
  3. F12 개발자도구 > Network 탭 > 조회 버튼 클릭
  4. `getJsonData.cmd` 요청의 Payload 에서 `bld` 값을 복사
  5. `set KRX_CREDIT_BLD=dbms/MDC/STAT/standard/MDCSTATxxxxx`
     또는 `--bld` 로 전달

이 방식이 코드에 추측을 박는 것보다 정직하고, KRX 가 코드를 바꿔도
사용자가 직접 고칠 수 있다.
"""
from __future__ import annotations

import logging
import os
import random
import time
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests

log = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))
BASE = "https://data.krx.co.kr"
ENDPOINT = f"{BASE}/comm/bldAttendant/getJsonData.cmd"
REFERER = f"{BASE}/contents/MDC/MDI/mdiLoader/index.cmd"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
     "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# 확인되지 않은 후보 목록. probe() 로 시험한다.
# 사용자가 DevTools 로 찾은 값을 KRX_CREDIT_BLD 로 주면 이 목록은 무시된다.
CANDIDATE_BLDS: tuple[str, ...] = (
    "dbms/MDC/STAT/standard/MDCSTAT02501",
    "dbms/MDC/STAT/standard/MDCSTAT02601",
    "dbms/MDC/STAT/standard/MDCSTAT02701",
    "dbms/MDC/STAT/standard/MDCSTAT12001",
)

# 응답 컬럼 탐색 패턴 (KRX 는 컬럼명을 자주 바꾼다)
TICKER_HINTS = ("ISU_SRT_CD", "ISU_CD", "SRT_CD", "종목코드")
NAME_HINTS = ("ISU_ABBRV", "ISU_NM", "종목명")
BALANCE_QTY_HINTS = ("LOAN_BAL_QTY", "BAL_QTY", "LOAN_QTY", "신용잔고수량",
                     "잔고수량", "LOAN_BALANCE_QTY")
BALANCE_AMT_HINTS = ("LOAN_BAL_AMT", "BAL_AMT", "신용잔고금액")
RATIO_HINTS = ("LOAN_BAL_RTO", "BAL_RTO", "신용잔고비율", "잔고비율")
LISTED_HINTS = ("LIST_SHRS", "ISU_SHRS", "상장주식수")

__all__ = ["fetch_credit_balance", "probe", "refresh_credit_auto"]


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
                time.sleep(1.5 * (attempt + 1))
                continue
            return res.json()
        except (requests.RequestException, ValueError) as exc:
            if attempt == 2:
                log.warning("KRX 요청 실패 (%s): %s", bld, exc)
            time.sleep(1.0 + attempt + random.random())
    return None


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


def fetch_credit_balance(trade_date: str, bld: str | None = None
                         ) -> tuple[pd.DataFrame, str]:
    """종목별 신용잔고 조회. (DataFrame, 사용된 bld) 반환.

    반환 컬럼: ticker, name, balance_qty, balance_amt, ratio, listed_shares
    실패하면 빈 DataFrame 을 돌린다.
    """
    blds = [bld] if bld else [os.getenv("KRX_CREDIT_BLD")] if os.getenv(
        "KRX_CREDIT_BLD") else list(CANDIDATE_BLDS)
    blds = [b for b in blds if b]

    for b in blds:
        data = _post(b, trade_date)
        rows = _rows_of(data or {})
        if not rows:
            log.debug("bld %s → 결과 없음", b)
            continue
        cols = list(rows[0].keys())
        c_tk = _pick(cols, TICKER_HINTS)
        c_qty = _pick(cols, BALANCE_QTY_HINTS)
        c_rto = _pick(cols, RATIO_HINTS)
        if not c_tk or not (c_qty or c_rto):
            log.debug("bld %s → 컬럼 불일치: %s", b, cols[:12])
            continue

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
        if out:
            log.info("KRX 신용잔고 %d종목 조회 성공 (bld=%s, 컬럼 %s)",
                     len(out), b, {"ticker": c_tk, "qty": c_qty, "ratio": c_rto})
            return pd.DataFrame(out), b
        time.sleep(0.5)

    log.warning("KRX 신용잔고 조회 실패. bld 코드를 확인하십시오 "
                "(--mode credit-probe 또는 KRX_CREDIT_BLD 환경변수)")
    return pd.DataFrame(), ""


def probe(trade_date: str, extra_blds: tuple = ()) -> list[dict]:
    """후보 bld 코드를 시험하고 결과를 보고한다.

    어느 코드가 어떤 컬럼을 돌려주는지 눈으로 확인할 수 있게 만든 진단
    도구다. 추측을 코드에 박는 대신 사용자가 1분 안에 확정하게 한다.
    """
    report = []
    env = os.getenv("KRX_CREDIT_BLD")
    blds = list(dict.fromkeys(
        ([env] if env else []) + list(extra_blds) + list(CANDIDATE_BLDS)))
    for b in blds:
        data = _post(b, trade_date)
        rows = _rows_of(data or {})
        item = {"bld": b, "rows": len(rows)}
        if rows:
            cols = list(rows[0].keys())
            item["columns"] = cols[:16]
            item["ticker_col"] = _pick(cols, TICKER_HINTS)
            item["qty_col"] = _pick(cols, BALANCE_QTY_HINTS)
            item["ratio_col"] = _pick(cols, RATIO_HINTS)
            item["sample"] = {k: rows[0].get(k) for k in cols[:8]}
            item["usable"] = bool(item["ticker_col"]
                                  and (item["qty_col"] or item["ratio_col"]))
        else:
            item["usable"] = False
        report.append(item)
        time.sleep(0.8)
    return report


def refresh_credit_auto(store, trade_date: str | None = None,
                        bld: str | None = None) -> dict:
    """KRX 에서 받아 credit_manual 테이블에 적재한다.

    비율이 없으면 상장주식수로 역산한다. 응답의 상장주식수를 우선 쓰고,
    없으면 마스터의 값을 쓴다.
    """
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
