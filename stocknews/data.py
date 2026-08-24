# -*- coding: utf-8 -*-
"""데이터 로더.

pykrx OHLCV는 반드시 확보되고, 신용잔고/공매도는 없을 수도 있다.
그래서 로더는 실패해도 None을 돌려주고, 스코어링 쪽에서 점수 상한을
낮추는 방식으로 우아하게 열화(graceful degradation)한다.

컬럼 규격: 시가 / 고가 / 저가 / 종가 / 거래량 / 거래대금
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd

__all__ = ["normalize_ohlcv", "load_ohlcv", "load_investor", "load_shorting",
           "make_loader"]

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
