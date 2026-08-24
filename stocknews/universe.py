# -*- coding: utf-8 -*-
"""코스피+코스닥 전종목 유니버스 관리.

전종목(약 2,800개)을 그대로 돌리면 안 되는 것들이 섞여 있다.
스팩, 우선주, 리츠, ETN/ETF 는 이 시스템의 로직(신용 청산 + 이평선
추세)이 성립하지 않거나 왜곡된다. 스팩은 2,000원에 붙어 있어 피보나치가
무의미하고, 우선주는 본주와 중복 신호를 만든다.

네트워크 전략
------------
  refresh_master()  : 하루 1회, 전종목 목록/시총 조회 (요청 2~3회)
  fetch_day()       : 하루 1회, '특정 일자 전종목' 시세 (요청 2회)
  backfill_one()    : 최초 1회만 종목별 히스토리 (재개 가능)
"""
from __future__ import annotations

import logging
import random
import time
from datetime import datetime, timedelta

import pandas as pd

from .data import (REF_TICKERS, close_on, market_snapshot,
                   verify_snapshot_date)
from .trading_day import is_definitely_closed
from .trading_day import now_kst as _now_kst

log = logging.getLogger(__name__)

__all__ = ["EXCLUDE_KEYWORDS", "is_tradable_name", "refresh_master",
           "fetch_day", "backfill_one", "liquidity_filter",
           "MARKETS_WANTED"]

# 스냅샷의 Market 값. KONEX 는 유동성이 없어 제외한다.
MARKETS_WANTED = {"KOSPI": "KOSPI", "KOSDAQ": "KOSDAQ",
                  "KOSDAQ GLOBAL": "KOSDAQ"}

# 종목명 기반 배제 키워드
EXCLUDE_KEYWORDS = (
    "스팩", "SPAC", "리츠", "REIT", "ETN", "인프라", "선박투자",
    "고배당", "레버리지", "인버스", "TIGER", "KODEX", "PLUS", "ACE",
    "SOL ", "RISE", "KIWOOM", "HANARO", "ARIRANG", "TIMEFOLIO",
)


def is_tradable_name(name: str) -> bool:
    """종목명으로 1차 배제. 우선주(코드 끝자리 0 아님)는 별도 처리."""
    if not name:
        return False
    upper = name.upper()
    return not any(k.upper() in upper for k in EXCLUDE_KEYWORDS)


def _is_common_share(code: str) -> bool:
    """보통주만. 우선주는 종목코드 끝자리가 0이 아니다(005935 등)."""
    return len(code) == 6 and code.endswith("0")


def refresh_master(store, on_date: str | None = None,
                   include_preferred: bool = False,
                   min_expected: int = 500,
                   shrink_tolerance: float = 0.5) -> int:
    """전종목 목록 + 시가총액 + 상장주식수를 마스터 테이블에 반영.

    상장폐지된 종목은 active=0 으로 내려 스캔 대상에서 자동 제외된다.

    ★ 데이터 소스
    -------------
    KRX 전종목 엔드포인트가 로그인 뒤로 들어가면서 pykrx 의
    `get_market_ticker_list` / `get_market_cap_by_ticker` 가 빈 결과를
    준다(2026-08 실측). 그래서 FinanceDataReader 스냅샷을 1순위로 쓴다.
    스냅샷 한 번에 종목명·시장·시가총액·상장주식수가 다 들어온다.

    ★ 휴장일 안전장치
    ----------------
    목록 조회가 비면 mark_inactive(빈집합) 이 **전 종목을 비활성화**해
    이후 모든 배치가 멈춘다. 그래서 목록이 비정상적으로 적으면 마스터를
    갱신하지 않고 그대로 둔다.

    **조회 실패를 휴장일로 기록하지 않는다.** 예전에는 목록이 적으면
    `mark_non_trading_day` 를 불렀는데, 소스 장애로 빈 결과가 오면 실제
    거래일이 영구히 휴장일로 박혀서 그 날짜를 두 번 다시 받지 못했다.
    휴장 판정은 주말/기지정 공휴일 같은 **적극적 근거**가 있을 때만 한다.
    """
    ds = on_date or _now_kst().strftime("%Y%m%d")
    iso = f"{ds[:4]}-{ds[4:6]}-{ds[6:8]}"
    rows: list[dict] = []
    alive: set[str] = set()

    _, meta = market_snapshot()

    if not meta.empty and "종목명" in meta.columns:
        for code, r in meta.iterrows():
            code = str(code).zfill(6)
            if not include_preferred and not _is_common_share(code):
                continue
            mk = MARKETS_WANTED.get(str(r.get("시장", "")).strip().upper())
            if mk is None:
                continue
            name = str(r.get("종목명") or code).strip()
            if not is_tradable_name(name):
                continue
            alive.add(code)
            mc = r.get("시가총액")
            shares = r.get("상장주식수")
            rows.append({
                "ticker": code, "name": name, "market": mk, "sector": None,
                "market_cap": float(mc) if pd.notna(mc) else None,
                "shares": float(shares) if pd.notna(shares) else None,
            })
    else:
        rows, alive = _refresh_master_pykrx(ds, include_preferred)

    # ── 휴장일/조회실패 방어 ──
    prev = len(store.active_tickers())
    floor = max(min_expected, int(prev * shrink_tolerance))
    if len(alive) < floor:
        if is_definitely_closed(store, iso):
            log.info("%s 는 휴장일입니다. 마스터를 갱신하지 않습니다.", iso)
        else:
            log.error(
                "종목 목록이 비정상적으로 적습니다 (%d개, 하한 %d). "
                "소스 장애로 판단해 마스터를 갱신하지 않고 기존 %d종목을 "
                "유지합니다. 휴장일로 기록하지 않으므로 다음 실행에서 "
                "다시 시도합니다.", len(alive), floor, prev)
        return 0

    n = store.upsert_tickers(rows)
    dead = store.mark_inactive(alive)
    log.info("마스터 갱신: 활성 %d종목, 비활성 처리 %d종목", n, dead)
    return n


def _refresh_master_pykrx(ds: str, include_preferred: bool
                          ) -> tuple[list[dict], set[str]]:
    """구 경로 폴백. KRX 가 전종목 조회를 다시 열면 이쪽이 살아난다."""
    from pykrx import stock

    log.warning("스냅샷 실패 → pykrx 전종목 경로로 폴백 "
                "(현재 KRX 가 막아둔 상태일 수 있음)")
    rows: list[dict] = []
    alive: set[str] = set()
    for market in ("KOSPI", "KOSDAQ"):
        try:
            cap = stock.get_market_cap_by_ticker(ds, market=market)
        except Exception as exc:  # noqa: BLE001
            log.warning("시총 조회 실패 %s: %s", market, exc)
            cap = pd.DataFrame()
        try:
            codes = stock.get_market_ticker_list(ds, market=market)
        except Exception as exc:  # noqa: BLE001
            log.error("종목목록 조회 실패 %s: %s", market, exc)
            continue
        for code in codes:
            code = str(code).zfill(6)
            if not include_preferred and not _is_common_share(code):
                continue
            try:
                name = stock.get_market_ticker_name(code)
            except Exception:  # noqa: BLE001
                name = code
            if not is_tradable_name(name):
                continue
            alive.add(code)
            mc = shares = None
            if not cap.empty and code in cap.index:
                mc = float(cap.loc[code].get("시가총액", float("nan")))
                shares = float(cap.loc[code].get("상장주식수", float("nan")))
            rows.append({"ticker": code, "name": name, "market": market,
                         "sector": None, "market_cap": mc, "shares": shares})
    return rows, alive


def fetch_day(store, trade_date: str, per_ticker_limit: int = 3200,
              throttle: float = 0.3) -> int:
    """'특정 일자 전종목' 시세 1판을 받아 적재. 일일 증분 경로.

    trade_date : 'YYYYMMDD'

    ★ 0건의 두 가지 의미를 구분한다
    ------------------------------
    예전 구현은 0건이면 무조건 휴장일로 기록했다. 그런데 KRX 전종목
    엔드포인트가 막히면서(2026-08) 실제 거래일에도 0건이 나온다. 그
    상태로 휴장일에 박아버리면 **그 날짜를 두 번 다시 받지 못한다.**
    스케줄이 매일 도니까 영업일이 하루씩 영구 소실된다.

    그래서 이렇게 판정한다.

        주말/기지정 공휴일        -> 조회하지 않고 생략 (근거 있음)
        기준 종목도 데이터 없음   -> 휴장일로 기록 (근거 있음)
        기준 종목은 데이터 있음   -> 소스 장애. 기록하지 않고 실패 반환
                                     (다음 실행에서 다시 시도)

    기준 종목(삼성전자 등)은 종목별 엔드포인트로 조회한다. 전종목이
    막혀도 이쪽은 동작하므로 '거래일인지'를 독립적으로 판정할 수 있다.
    """
    iso = f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:8]}"

    # ── 1) 근거 있는 휴장일이면 네트워크를 쓰지 않는다 ──
    if is_definitely_closed(store, iso):
        log.info("%s 휴장일 → 시세 조회 생략", iso)
        store.mark_non_trading_day(iso, "weekend/known holiday")
        return 0

    # ── 2) 전종목 스냅샷 (요청 1회). 날짜를 반드시 대조한다 ──
    prices, _ = market_snapshot()
    ok, why = verify_snapshot_date(prices, trade_date)
    if ok:
        total = store.upsert_cross_section(iso, prices)
        log.info("%s 시세 적재 %d건 (스냅샷, %s)", iso, total, why)
        if total:
            return total
    else:
        log.info("%s 스냅샷 사용 불가: %s", iso, why)

    # ── 3) 그 날이 거래일인지 독립 판정 ──
    ref_close = {t: close_on(t, trade_date) for t in REF_TICKERS}
    if not any(v for v in ref_close.values()):
        store.mark_non_trading_day(iso, "reference tickers have no data")
        log.info("%s 기준 종목 %s 전부 데이터 없음 → 휴장일로 기록",
                 iso, "/".join(REF_TICKERS))
        return 0

    # ── 4) 거래일이다. 종목별로 받는다 (느리지만 정확) ──
    codes = list(store.active_tickers())
    if not codes:
        log.error("%s 활성 종목이 없습니다. 먼저 --mode master 를 "
                  "실행하십시오.", iso)
        return 0

    log.warning("%s 전종목 경로가 막혀 종목별 폴백으로 진행합니다 "
                "(%d종목 x %.2fs ≈ %.0f분)",
                iso, min(len(codes), per_ticker_limit), throttle,
                min(len(codes), per_ticker_limit) * throttle / 60)

    total = failed = 0
    for i, code in enumerate(codes[:per_ticker_limit]):
        c = close_on(code, trade_date)
        if c is None:
            failed += 1
        else:
            total += _upsert_one_day(store, code, trade_date, iso)
        time.sleep(throttle + random.random() * 0.1)
        if (i + 1) % 250 == 0:
            log.info("  ... %d/%d 적재 %d 실패 %d",
                     i + 1, min(len(codes), per_ticker_limit), total, failed)

    if total == 0:
        # 기준 종목은 데이터가 있는데 전 종목이 0건이다. 휴장일이 아니라
        # 소스 장애다. 기록하지 않는다.
        log.error("%s 기준 종목은 데이터가 있으나 적재 0건입니다. "
                  "소스 장애로 판단하고 휴장일로 기록하지 않습니다.", iso)
    else:
        log.info("%s 시세 적재 %d건 (종목별 폴백, 실패 %d)",
                 iso, total, failed)
    return total


def _upsert_one_day(store, ticker: str, trade_date: str, iso: str) -> int:
    """종목 1개의 특정 거래일 1행을 적재."""
    from pykrx import stock
    try:
        df = stock.get_market_ohlcv(trade_date, trade_date, ticker)
    except Exception:  # noqa: BLE001
        return 0
    if df is None or df.empty:
        return 0
    need = ("시가", "고가", "저가", "종가", "거래량")
    if any(c not in df.columns for c in need):
        return 0
    try:
        if float(df["거래량"].iloc[-1]) <= 0:
            return 0
    except (IndexError, ValueError, TypeError):
        return 0
    return store.upsert_prices(ticker, df.tail(1))


def backfill_one(store, ticker: str, days: int = 420,
                 throttle: float = 0.35) -> int:
    """종목 1개 히스토리 적재. 최초 1회용.

    2,800종목 × 0.35초 ≈ 17분. 중간에 끊겨도 backfill_state 체크포인트로
    다음 실행에서 이어서 진행된다.
    """
    from pykrx import stock

    end = datetime.now()
    start = end - timedelta(days=int(days * 1.7))
    for attempt in range(3):
        try:
            df = stock.get_market_ohlcv(start.strftime("%Y%m%d"),
                                        end.strftime("%Y%m%d"), ticker)
            if df is None or df.empty:
                store.mark_backfilled(ticker, 0)
                return 0
            need = ("시가", "고가", "저가", "종가", "거래량")
            if any(c not in df.columns for c in need):
                store.mark_backfilled(ticker, 0)
                return 0
            df = df[df["거래량"] > 0]
            n = store.upsert_prices(ticker, df.tail(days))
            store.mark_backfilled(ticker, n)
            time.sleep(throttle + random.random() * 0.15)
            return n
        except Exception as exc:  # noqa: BLE001
            wait = 2 ** attempt + random.random()
            log.warning("백필 실패 %s (%d/3): %s → %.1fs 대기",
                        ticker, attempt + 1, exc, wait)
            time.sleep(wait)
    return 0


def liquidity_filter(store, tickers: dict, min_amt_20d: float = 5e8,
                     min_bars: int = 120) -> dict:
    """유동성/데이터 하한 필터.

    20일 평균 거래대금 5억 미만은 슬리피지가 커서 실전 진입이 불가능하다.
    거래대금 컬럼이 없으면 종가×거래량으로 근사한다.
    """
    keep: dict = {}
    for code, name in tickers.items():
        df = store.load_ohlcv(code, days=max(min_bars, 40))
        if df is None or len(df) < min_bars:
            continue
        if "거래대금" in df.columns and df["거래대금"].notna().any():
            amt = df["거래대금"].tail(20).mean()
        else:
            amt = (df["종가"] * df["거래량"]).tail(20).mean()
        if pd.notna(amt) and amt >= min_amt_20d:
            keep[code] = name
    return keep


def fill_sectors(store) -> int:
    """업종 정보 보강. 추천 10선의 섹터 편중을 막는 데 쓴다.

    pykrx 에는 업종 API 가 없어서 FinanceDataReader 의 상장목록을 쓴다.
    실패하면 섹터 없이 진행하고, 그 경우 섹터 분산 제한만 비활성된다.
    """
    try:
        import FinanceDataReader as fdr
        lst = fdr.StockListing("KRX")
    except Exception as exc:  # noqa: BLE001
        log.warning("업종 정보 조회 실패, 섹터 분산 제한 비활성: %s", exc)
        return 0

    code_col = next((c for c in ("Code", "Symbol", "종목코드") if c in lst.columns), None)
    sec_col = next((c for c in ("Sector", "Industry", "업종") if c in lst.columns), None)
    if not code_col or not sec_col:
        log.warning("업종 컬럼 없음: %s", list(lst.columns)[:10])
        return 0

    existing = store.active_tickers()
    rows = []
    for _, r in lst[[code_col, sec_col]].dropna().iterrows():
        code = str(r[code_col]).zfill(6)
        if code in existing:
            rows.append({"ticker": code, "name": existing[code],
                         "sector": str(r[sec_col])[:40]})
    if rows:
        store.upsert_tickers(rows)
    log.info("업종 정보 %d종목 반영", len(rows))
    return len(rows)
