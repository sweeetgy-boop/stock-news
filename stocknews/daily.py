# -*- coding: utf-8 -*-
"""매일 저녁 전종목 스캔 → 추천 10선.

10선을 '점수 상위 10개'로 뽑으면 안 되는 이유
--------------------------------------------
단순 정렬은 세 가지로 망가진다.

  1) 섹터 쏠림   : 2차전지가 동반 급락한 날엔 10개가 전부 2차전지다.
                   그건 10개 추천이 아니라 1개 베팅이다.
  2) 트랙 쏠림   : 하락장엔 매집(V) 신호만, 상승장엔 추세(T) 신호만 나온다.
                   슬롯을 나눠 양쪽을 항상 확보한다.
  3) 유동성 함정 : 점수는 높지만 하루 거래대금 2억인 종목은 진입 자체가 불가.

그래서 슬롯 방식으로 뽑는다.

  슬롯 1~2  : S+ (밴드 진입 후 골든크로스 = 시퀀스 확증) 최우선
  슬롯 3~7  : 트랙 V 상위 (매집, 역추세)
  슬롯 8~10 : 트랙 T 상위 (확증, 순추세)
  빈 슬롯   : 전체 점수 순으로 보충
  제약      : 동일 업종 최대 2개
"""
from __future__ import annotations

import logging
from datetime import datetime

import pandas as pd

from .config import Config, DEFAULT
from .contracts import ScreenResult
from .data import load_index
from .screener import rank_results, screen_one

log = logging.getLogger(__name__)

__all__ = ["scan_all", "select_recommendations", "run_daily",
           "market_context", "collect_market_context"]

SLOT_PLAN = (("SEQ", 2), ("VALUE", 5), ("TREND", 3))


def scan_all(store, tickers: dict, cfg: Config = DEFAULT,
             progress_every: int = 250) -> tuple[list[ScreenResult], list[tuple]]:
    """전종목 순차 채점. 네트워크를 쓰지 않고 로컬 시세만 읽는다.

    종목 하나가 터져도 배치가 죽지 않게 예외를 개별 격리한다.
    이게 없으면 신규상장/액면분할 종목 하나 때문에 매일 배치가 실패한다.
    """
    meta = store.ticker_meta()
    # 배제 플래그를 한 번만 읽어 종목별로 넘긴다. 비어 있으면(공급원 미구동)
    # 배제가 걸리지 않으므로 잡주가 상위에 올라올 수 있다.
    all_flags = store.load_flags()
    if not all_flags:
        log.warning("배제 플래그가 비어 있다 — 'flags' 모드를 먼저 실행하라")

    # 수동 주입 신용잔고. 있는 종목만 LPS credit_heat 가 만점까지 열린다.
    # 없으면 프록시 캡(1.25점)이 걸려 매집 점수 상한이 8.49점이 된다.
    credit_map = store.load_credit_ratios()
    if credit_map:
        log.info("신용잔고 실측 %d종목 적용 (나머지는 프록시)", len(credit_map))

    # '상장 1년 미만' 배제는 DB 에 1년치가 실제로 있을 때만 의미가 있다.
    # 백필이 덜 된 상태에서 봉 수로 판정하면 전 종목이 신규상장으로
    # 배제되어 추천이 0건 나온다. 원인 파악이 어려운 함정이다.
    market_bars = len(store.existing_dates())
    check_listing = market_bars >= 252
    if not check_listing:
        log.warning("DB 거래일 %d일 (<252) — '상장 1년 미만' 배제를 비활성",
                    market_bars)

    results: list[ScreenResult] = []
    errors: list[tuple] = []
    excluded_n = 0
    total = len(tickers)

    for i, (code, name) in enumerate(tickers.items(), 1):
        if progress_every and i % progress_every == 0:
            log.info("  스캔 진행 %d/%d (통과 %d, 배제 %d, 실패 %d)",
                     i, total, len(results) - excluded_n, excluded_n, len(errors))
        try:
            ohlcv = store.load_ohlcv(code, days=420)
            if ohlcv is None or len(ohlcv) < cfg.ma.long + 30:
                continue
            mc = None
            listed = len(ohlcv)
            if code in meta.index:
                v = meta.at[code, "market_cap"]
                mc = float(v) if pd.notna(v) else None
            res = screen_one(
                code, name, ohlcv,
                credit=None, investor=None, shorting=None,
                credit_ratio=credit_map.get(code), flags=all_flags.get(code),
                market_cap=mc,
                listed_days=listed if check_listing else None, cfg=cfg,
            )
            if res.excluded:
                excluded_n += 1
            results.append(res)
        except Exception as exc:  # noqa: BLE001 - 배치 격리
            errors.append((code, f"{type(exc).__name__}: {exc}"))

    log.info("채점 완료: 통과 %d · 배제 %d · 실패 %d",
             len(results) - excluded_n, excluded_n, len(errors))
    return rank_results(results), errors


def _sector_of(meta: pd.DataFrame, ticker: str) -> str | None:
    if ticker in meta.index:
        s = meta.at[ticker, "sector"]
        if isinstance(s, str) and s.strip():
            return s.strip()
    return None


def select_recommendations(results: list[ScreenResult], meta: pd.DataFrame,
                           top_n: int = 10, per_sector: int = 2,
                           cfg: Config = DEFAULT) -> list[tuple[str, ScreenResult]]:
    """슬롯 배정 + 섹터 분산으로 top_n 선정. [(slot, result), ...] 반환."""
    g = cfg.gate
    pool = [r for r in results if not r.excluded and r.grade != "NONE"]

    # 시퀀스 확증 종목은 SEQ 슬롯을 먼저 채우고, 넘치는 만큼은 V/T 슬롯
    # 후보로도 남긴다. used 집합이 중복 선정을 막는다.
    seq_pool = sorted([r for r in pool if r.sequence_confirm],
                      key=lambda r: -max(r.value_score, r.trend_score))
    val_pool = sorted(pool, key=lambda r: -r.value_score)
    trd_pool = sorted(pool, key=lambda r: -r.trend_score)

    picked: list[tuple[str, ScreenResult]] = []
    used: set[str] = set()
    sector_count: dict[str, int] = {}

    def take(slot: str, cand: ScreenResult) -> bool:
        if cand.ticker in used:
            return False
        sec = _sector_of(meta, cand.ticker)
        if sec and sector_count.get(sec, 0) >= per_sector:
            return False
        picked.append((slot, cand))
        used.add(cand.ticker)
        if sec:
            sector_count[sec] = sector_count.get(sec, 0) + 1
        return True

    for slot, n in SLOT_PLAN:
        if slot == "SEQ":
            pool_i, floor = seq_pool, 0.0
        elif slot == "VALUE":
            pool_i, floor = val_pool, g.value_threshold - 1.5
        else:
            pool_i, floor = trd_pool, g.trend_threshold - 1.5
        filled = 0
        for cand in pool_i:
            if filled >= n or len(picked) >= top_n:
                break
            score = (cand.value_score if slot != "TREND" else cand.trend_score)
            if slot != "SEQ" and score < floor:
                break
            if take(slot, cand):
                filled += 1

    # 빈 슬롯 보충 — 섹터 제한은 유지
    if len(picked) < top_n:
        rest = sorted(pool, key=lambda r: -max(r.value_score, r.trend_score))
        for cand in rest:
            if len(picked) >= top_n:
                break
            take("FILL", cand)

    # 그래도 못 채우면 섹터 제한을 풀고 마지막 보충
    if len(picked) < top_n:
        for cand in sorted(pool, key=lambda r: -max(r.value_score, r.trend_score)):
            if len(picked) >= top_n:
                break
            if cand.ticker not in used:
                picked.append(("FILL*", cand))
                used.add(cand.ticker)

    return picked[:top_n]


# ══════════════════ 시장 상태 기록 (기록 전용) ══════════════════
# 이 블록의 산출물은 `market_context` 테이블에만 들어간다. 점수·추천·
# 게이트·리포트 어디에도 쓰지 않는다. 채점 표본이 쌓인 뒤 '어떤 장에서
# 통했나'를 되짚기 위한 것이고, 지금 판단에 쓰면 표본 20건도 안 되는
# 시점에 국면 필터를 넣는 셈이 된다.
def _close_series(df, scan_date) -> pd.Series | None:
    """`scan_date` 이하 구간의 종가 시리즈 (오름차순).

    FDR 은 'Close', 내부 규격은 '종가' 를 쓰므로 둘 다 받는다.
    """
    if df is None or len(df) == 0:
        return None
    col = next((c for c in ("종가", "Close") if c in df.columns), None)
    if col is None:
        return None
    s = df[col].astype("float64").dropna()
    idx = pd.DatetimeIndex(s.index)
    if idx.tz is not None:
        idx = idx.tz_localize(None)
    s.index = idx
    s = s.sort_index()
    s = s[s.index <= pd.Timestamp(scan_date)]
    return s if len(s) else None


def _ret_pct(s: pd.Series | None, bars: int) -> float | None:
    """`bars` 거래일 수익률(%). 봉이 부족하면 None.

    달력일이 아니라 봉 개수로 센다. `s.iloc[-1]` 대비 `s.iloc[-(bars+1)]`
    이므로 두 지점이 정확히 `bars` 봉 떨어져 있다.
    """
    if s is None or len(s) < bars + 1:
        return None
    a = float(s.iloc[-(bars + 1)])
    b = float(s.iloc[-1])
    if a <= 0:
        return None
    return (b / a - 1.0) * 100.0


def market_context(kospi, kosdaq, scan_date: str,
                   ma_window: int = 200) -> dict | None:
    """추천 시점 시장 상태. 순수 계산 — 네트워크·DB 를 만지지 않는다.

    KOSPI 종가를 못 구하면 None (기록할 게 없다). 봉이 부족한 항목만
    None 으로 남기고 나머지는 채운다. 0 으로 채우면 '보합'으로 읽힌다.
    """
    ks = _close_series(kospi, scan_date)
    if ks is None:
        return None
    close = float(ks.iloc[-1])
    ma = float(ks.iloc[-ma_window:].mean()) if len(ks) >= ma_window else None
    kq = _close_series(kosdaq, scan_date)
    return {
        "scan_date": scan_date,
        "kospi_close": close,
        "kospi_ma200": ma,
        "kospi_above_ma200": None if ma is None else int(close > ma),
        "kospi_ret_5d": _ret_pct(ks, 5),
        "kospi_ret_20d": _ret_pct(ks, 20),
        "kosdaq_close": None if kq is None else float(kq.iloc[-1]),
        "kosdaq_ret_5d": _ret_pct(kq, 5),
    }


def collect_market_context(store, trade_date: str, bars: int = 300,
                           fetch=None) -> dict | None:
    """지수를 조회해 시장 상태를 기록한다. 하루 1회.

    **실패해도 예외를 올리지 않는다.** 지수 조회가 안 되는 날에 daily
    전체가 실패하면 정작 중요한 스캔·추천 기록이 날아간다. 경고만 남기고
    None 을 돌린다.

    fetch : 테스트용 주입구. (name, bars) -> DataFrame|None
    """
    if not trade_date:
        return None
    try:
        if store.has_market_context(trade_date):
            return None
        get = fetch or (lambda name, n: load_index(name, bars=n))
        kospi = get("KOSPI", bars)
        kosdaq = get("KOSDAQ", bars)
        row = market_context(kospi, kosdaq, trade_date)
        if row is None:
            log.warning("시장 상태 계산 불가 (KOSPI 종가 없음) — %s 생략",
                        trade_date)
            return None
        store.upsert_market_context(row)
    except Exception as exc:  # noqa: BLE001 - daily 를 죽이지 않는다
        log.warning("시장 상태 기록 실패 — 건너뜁니다 (%s: %s)",
                    type(exc).__name__, exc)
        return None

    ma = row["kospi_ma200"]
    log.info("시장 상태 기록 %s · KOSPI %.2f (MA200 %s · %s) · 5일 %s",
             trade_date, row["kospi_close"],
             f"{ma:.2f}" if ma is not None else "-",
             {1: "위", 0: "아래"}.get(row["kospi_above_ma200"], "판정불가"),
             f"{row['kospi_ret_5d']:+.2f}%"
             if row["kospi_ret_5d"] is not None else "-")
    return row


def run_daily(store, cfg: Config = DEFAULT, top_n: int = 10,
              tickers: dict | None = None) -> dict:
    """저녁 배치 본체. 스캔 → 스냅샷 저장 → 10선 선정 → 저장."""
    started = datetime.now()
    tickers = tickers or store.active_tickers()
    trade_date = store.last_price_date()
    if not trade_date:
        raise RuntimeError("시세 데이터가 없습니다. backfill/update 를 먼저 실행하세요.")

    log.info("전종목 스캔 시작: %d종목 (기준일 %s)", len(tickers), trade_date)
    results, errors = scan_all(store, tickers, cfg)
    log.info("스캔 완료: 성공 %d / 실패 %d", len(results), len(errors))

    saved = store.save_scan(trade_date, results, fib_target=cfg.fib.target)
    meta = store.ticker_meta()
    picks = select_recommendations(results, meta, top_n=top_n, cfg=cfg)
    store.save_recos(trade_date, picks)

    # 추천 저장 **뒤에** 둔다. 지수 조회가 실패해도 스캔·추천 기록은
    # 이미 커밋돼 있어야 한다.
    mkt = collect_market_context(store, trade_date)

    store.log_run("daily", started, len(results), len(errors),
                  note=f"snapshot={saved}, picks={len(picks)}")

    return {"trade_date": trade_date, "results": results, "errors": errors,
            "picks": picks, "snapshot_rows": saved,
            "market_context": mkt}
