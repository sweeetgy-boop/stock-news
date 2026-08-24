# -*- coding: utf-8 -*-
"""데이터 로더.

pykrx OHLCV는 반드시 확보되고, 신용잔고/공매도는 없을 수도 있다.
그래서 로더는 실패해도 None을 돌려주고, 스코어링 쪽에서 점수 상한을
낮추는 방식으로 우아하게 열화(graceful degradation)한다.

컬럼 규격: 시가 / 고가 / 저가 / 종가 / 거래량 / 거래대금

★ 2026-08 실측: KRX '전종목' 계열 엔드포인트 중단
--------------------------------------------------
`data.krx.co.kr/comm/bldAttendant/getJsonData.cmd` 가 로그인을 요구하게
바뀌어, 알려진 bld 에도 `HTTP 400 / body="LOGOUT"` 을 돌려준다.
그 결과 pykrx 의 **전종목(bulk) 함수들이 빈 결과**를 준다.

    동작함    stock.get_market_ohlcv(start, end, ticker)     종목 1개 시계열
    동작함    fdr.StockListing('KRX')                        전종목 스냅샷
    동작함    fdr.StockListing('KRX-ADMINISTRATIVE')         관리종목
    중단      stock.get_market_ohlcv_by_ticker(date, ...)    일자별 전종목
    중단      stock.get_market_ticker_list(date, ...)        종목 목록
    중단      stock.get_market_cap_by_ticker(date, ...)      시총
    중단      stock.get_market_trading_value_by_investor(..) 투자자별
    중단      stock.get_shorting_balance_by_ticker(...)      공매도 잔고

그래서 전종목 경로는 FinanceDataReader 스냅샷을 1순위로 쓴다.

**주의: FDR 스냅샷에는 날짜 파라미터가 없다.** 항상 '최신'을 주고,
장중에 부르면 종가가 아닌 현재가가 섞인다. 그 값을 그날의 종가로
적재하면 DB 가 조용히 오염된다. 그래서 `verify_snapshot_date()` 로
기준 종목의 종가를 대조해 **확인된 경우에만** 적재한다.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

import pandas as pd

log = logging.getLogger(__name__)

__all__ = ["normalize_ohlcv", "load_ohlcv", "load_investor", "load_shorting",
           "make_loader", "market_snapshot", "verify_snapshot_date",
           "close_on", "REF_TICKERS"]

# 날짜 대조용 기준 종목. 거래정지 가능성이 낮은 초대형주만 쓴다.
REF_TICKERS = ("005930", "000660", "005380")

_COLMAP = {
    "Open": "시가", "High": "고가", "Low": "저가", "Close": "종가",
    "Volume": "거래량", "Amount": "거래대금", "Value": "거래대금",
    "open": "시가", "high": "고가", "low": "저가", "close": "종가",
    "volume": "거래량",
}
REQUIRED = ("시가", "고가", "저가", "종가", "거래량")


def normalize_ohlcv(df: pd.DataFrame) -> pd.DataFrame:
    """컬럼명 한글 규격으로 통일 + 정렬 + 결측 제거."""
    if df is None or df.empty:
        raise ValueError("빈 OHLCV")
    out = df.rename(columns=_COLMAP).copy()
    missing = [c for c in REQUIRED if c not in out.columns]
    if missing:
        raise KeyError(f"OHLCV 컬럼 누락: {missing}")
    keep = list(REQUIRED) + (["거래대금"] if "거래대금" in out.columns else [])
    out = out[keep].astype("float64")
    out.index = pd.to_datetime(out.index)
    out = out[~out.index.duplicated(keep="last")].sort_index()
    # 거래정지일(거래량 0, 종가 유지) 은 지표를 왜곡하므로 제거
    return out[out["거래량"] > 0]


def load_ohlcv(ticker: str, days: int = 400) -> pd.DataFrame:
    """pykrx 우선, 실패 시 FinanceDataReader 폴백."""
    end = datetime.now()
    start = end - timedelta(days=int(days * 1.6))
    try:
        from pykrx import stock
        df = stock.get_market_ohlcv(start.strftime("%Y%m%d"),
                                    end.strftime("%Y%m%d"), ticker)
        return normalize_ohlcv(df).tail(days)
    except Exception:  # noqa: BLE001
        import FinanceDataReader as fdr
        df = fdr.DataReader(ticker, start, end)
        return normalize_ohlcv(df).tail(days)


def load_investor(ticker: str, days: int = 180) -> pd.DataFrame | None:
    """투자자별 순매수대금. 신용잔고 없을 때 평균단가 프록시(방법 B)로 쓴다."""
    end = datetime.now()
    start = end - timedelta(days=int(days * 1.6))
    try:
        from pykrx import stock
        df = stock.get_market_trading_value_by_date(
            start.strftime("%Y%m%d"), end.strftime("%Y%m%d"), ticker, detail=True
        )
        if df is None or df.empty or "개인" not in df.columns:
            return None
        df.index = pd.to_datetime(df.index)
        return df[["개인"]].astype("float64").sort_index()
    except Exception:  # noqa: BLE001
        return None


def load_shorting(ticker: str, days: int = 120) -> pd.DataFrame | None:
    """공매도 잔고. 공시가 T+2 지연이므로 스코어링에서 shift 처리한다."""
    end = datetime.now()
    start = end - timedelta(days=int(days * 1.6))
    try:
        from pykrx import stock
        df = stock.get_shorting_balance_by_date(
            start.strftime("%Y%m%d"), end.strftime("%Y%m%d"), ticker
        )
        if df is None or df.empty:
            return None
        df.index = pd.to_datetime(df.index)
        return df.sort_index()
    except Exception:  # noqa: BLE001
        return None


_SNAP_PRICE = {"Open": "시가", "High": "고가", "Low": "저가",
               "Close": "종가", "Volume": "거래량", "Amount": "거래대금"}
_SNAP_META = {"Name": "종목명", "Market": "시장",
              "Marcap": "시가총액", "Stocks": "상장주식수"}


def market_snapshot() -> tuple[pd.DataFrame, pd.DataFrame]:
    """전종목 스냅샷. (시세, 메타) 를 종목코드 인덱스로 돌려준다.

    KRX 전종목 엔드포인트가 막힌 뒤의 1순위 경로다. 요청 1회로 약 2,900
    종목이 들어온다.

    시세 : 시가 고가 저가 종가 거래량 거래대금
    메타 : 종목명 시장 시가총액 상장주식수

    **날짜가 없다.** 이 함수는 '최신'만 준다. 그날의 종가인지는
    `verify_snapshot_date()` 로 따로 확인해야 한다.
    실패하면 빈 DataFrame 두 개를 돌려준다.
    """
    try:
        import FinanceDataReader as fdr
        lst = fdr.StockListing("KRX")
    except Exception as exc:  # noqa: BLE001
        log.warning("전종목 스냅샷 조회 실패: %s", exc)
        return pd.DataFrame(), pd.DataFrame()

    if lst is None or lst.empty:
        log.warning("전종목 스냅샷이 비었습니다")
        return pd.DataFrame(), pd.DataFrame()

    code_col = next((c for c in ("Code", "Symbol", "종목코드")
                     if c in lst.columns), None)
    if not code_col:
        log.warning("스냅샷에 종목코드 컬럼이 없습니다: %s",
                    list(lst.columns)[:12])
        return pd.DataFrame(), pd.DataFrame()

    lst = lst.copy()
    lst[code_col] = lst[code_col].astype(str).str.zfill(6)
    lst = lst[lst[code_col].str.fullmatch(r"\d{6}")]
    lst = lst.drop_duplicates(subset=[code_col]).set_index(code_col)

    def _slice(mapping):
        cols = {src: dst for src, dst in mapping.items() if src in lst.columns}
        if not cols:
            return pd.DataFrame()
        return lst[list(cols)].rename(columns=cols)

    prices = _slice(_SNAP_PRICE)
    meta = _slice(_SNAP_META)

    missing = [c for c in REQUIRED if c not in prices.columns]
    if missing:
        log.warning("스냅샷 시세 컬럼 누락 %s → 시세 경로 사용 불가", missing)
        prices = pd.DataFrame()
    else:
        for c in prices.columns:
            prices[c] = pd.to_numeric(prices[c], errors="coerce")
        prices = prices.dropna(subset=list(REQUIRED))

    for c in meta.columns:
        if c != "종목명" and c != "시장":
            meta[c] = pd.to_numeric(meta[c], errors="coerce")

    log.info("전종목 스냅샷: 시세 %d종목, 메타 %d종목", len(prices), len(meta))
    return prices, meta


def close_on(ticker: str, trade_date: str) -> float | None:
    """특정 거래일의 종가 1개. 종목별 시계열 엔드포인트를 쓴다(동작함).

    trade_date : 'YYYYMMDD'
    거래일이 아니거나 조회 실패면 None.
    """
    try:
        from pykrx import stock
        df = stock.get_market_ohlcv(trade_date, trade_date, ticker)
    except Exception as exc:  # noqa: BLE001
        log.debug("종가 조회 실패 %s %s: %s", ticker, trade_date, exc)
        return None
    if df is None or df.empty or "종가" not in df.columns:
        return None
    try:
        v = float(df["종가"].iloc[-1])
    except (IndexError, ValueError, TypeError):
        return None
    return v if v > 0 else None


def verify_snapshot_date(prices: pd.DataFrame, trade_date: str,
                         refs: tuple[str, ...] = REF_TICKERS,
                         need: int = 2) -> tuple[bool, str]:
    """스냅샷이 정말 `trade_date` 의 종가인지 대조한다.

    FDR 스냅샷은 날짜가 없어서 장중에 부르면 현재가가 섞인다. 기준
    종목의 종가를 종목별 엔드포인트(동작함)로 받아 비교한다.

    반환 (일치여부, 사유). `need` 개 이상 일치해야 통과.
    기준 종목을 하나도 조회할 수 없으면 통과시키지 않는다. 확인되지
    않은 값을 종가로 적재하는 것보다 적재를 미루는 쪽이 안전하다.
    """
    if prices is None or prices.empty:
        return False, "스냅샷이 비었음"

    checked = matched = 0
    detail = []
    for t in refs:
        if t not in prices.index:
            continue
        ref = close_on(t, trade_date)
        if ref is None:
            continue
        checked += 1
        got = float(prices.at[t, "종가"])
        ok = abs(got - ref) < max(1.0, ref * 1e-6)
        detail.append(f"{t}:{got:.0f}vs{ref:.0f}{'=' if ok else '!'}")
        if ok:
            matched += 1

    if checked == 0:
        return False, "기준 종목 종가를 조회할 수 없어 날짜 확인 불가"
    if matched >= min(need, checked):
        return True, f"기준 {matched}/{checked} 일치 ({' '.join(detail)})"
    return False, (f"기준 {matched}/{checked} 일치 — 스냅샷이 {trade_date} "
                   f"종가가 아님 (장중 현재가 가능) ({' '.join(detail)})")


def make_loader(credit_provider=None, flag_provider=None, days: int = 400):
    """screen_universe 에 넘길 loader 팩토리.

    credit_provider(ticker) -> DataFrame(index=날짜, '신용잔고주식수')
        KRX/증권사에서 종목별 신용잔고를 확보했을 때 주입한다. 없으면
        평균단가는 방법 B/C 로 폴백하고 신용 과열도 점수가 캡된다.
    flag_provider(ticker) -> dict  (관리종목/자본잠식/시총/상장일수 등)
    """

    def loader(ticker: str) -> dict | None:
        try:
            ohlcv = load_ohlcv(ticker, days)
        except Exception:  # noqa: BLE001
            return None
        if len(ohlcv) < 80:
            return None

        credit = credit_provider(ticker) if credit_provider else None
        credit_ratio = None
        if credit is not None and "신용잔고율" in getattr(credit, "columns", []):
            try:
                credit_ratio = float(credit["신용잔고율"].dropna().iloc[-1])
            except (IndexError, ValueError):
                credit_ratio = None

        extra = flag_provider(ticker) if flag_provider else {}
        return {
            "ohlcv": ohlcv,
            "credit": credit,
            "investor": load_investor(ticker),
            "shorting": load_shorting(ticker),
            "credit_ratio": credit_ratio,
            "flags": extra.get("flags"),
            "market_cap": extra.get("market_cap"),
            "listed_days": extra.get("listed_days"),
        }

    return loader
