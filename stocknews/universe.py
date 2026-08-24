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

from .trading_day import now_kst as _now_kst

log = logging.getLogger(__name__)

__all__ = ["EXCLUDE_KEYWORDS", "is_tradable_name", "refresh_master",
           "fetch_day", "backfill_one", "liquidity_filter"]

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

    ★ 휴장일 안전장치
    ----------------
    공휴일에는 종목 목록 조회가 빈 결과를 돌려준다. 그 상태로
    mark_inactive(빈집합) 을 호출하면 **전 종목이 비활성화되어** 이후
    모든 배치가 멈춘다. 스케줄에 매일 master 가 있으므로 공휴일마다
    터진다.

    그래서 목록이 비정상적으로 적으면 마스터를 갱신하지 않고 그대로
    둔다. 조회 실패를 '상장폐지'로 오해하는 것보다 갱신을 미루는 쪽이
    언제나 안전하다.
    """
    from pykrx import stock

    # 조회 기준일. 휴장일이면 pykrx 가 빈 결과를 주는 게 정상이다.
    ds = on_date or _now_kst().strftime("%Y%m%d")
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

    # ── 휴장일/조회실패 방어 ──
    prev = len(store.active_tickers())
    floor = max(min_expected, int(prev * shrink_tolerance))
    if len(alive) < floor:
        store.mark_non_trading_day(
            f"{ds[:4]}-{ds[4:6]}-{ds[6:8]}",
            f"ticker list {len(alive)} < {floor}")
        log.error("종목 목록이 비정상적으로 적습니다 (%d개, 하한 %d). "
                  "휴장일 또는 조회 실패로 판단해 마스터를 갱신하지 않습니다. "
                  "기존 %d종목을 그대로 유지합니다.",
                  len(alive), floor, prev)
        return 0

    n = store.upsert_tickers(rows)
    dead = store.mark_inactive(alive)
    log.info("마스터 갱신: 활성 %d종목, 비활성 처리 %d종목", n, dead)
    return n


def fetch_day(store, trade_date: str) -> int:
    """'특정 일자 전종목' 시세 1판을 받아 적재. 일일 증분 경로.

    trade_date : 'YYYYMMDD'
    """
    from pykrx import stock

    total = 0
    for market in ("KOSPI", "KOSDAQ"):
        df = None
        for attempt in range(3):
            try:
                # pykrx 버전에 따라 '일자별 전종목' 진입점이 다르다.
                if hasattr(stock, "get_market_ohlcv_by_ticker"):
                    df = stock.get_market_ohlcv_by_ticker(trade_date, market=market)
                else:
                    df = stock.get_market_ohlcv(trade_date, market=market)
                break
            except Exception as exc:  # noqa: BLE001
                log.warning("일자 시세 조회 실패 %s %s (%d/3): %s",
                            trade_date, market, attempt + 1, exc)
                time.sleep(2 ** attempt + random.random())
        if df is None or df.empty:
            continue
        ds = f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:8]}"
        total += store.upsert_cross_section(ds, df)
        time.sleep(0.5)

    iso = f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:8]}"
    if total == 0:
        # 휴장일로 기록한다. 이게 없으면 catchup 기간 내내 같은 공휴일을
        # 매번 다시 요청한다(성공하지만 0건이므로 have 에 안 들어간다).
        store.mark_non_trading_day(iso, "cross-section returned 0 rows")
        log.info("%s 적재 0건 → 휴장일로 기록 (이후 재요청하지 않음)", iso)
    else:
        log.info("%s 시세 적재 %d건", iso, total)
    return total


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
