# -*- coding: utf-8 -*-
"""섹터 지표 수집. **기록 전용이다.**

왜 기록만 하는가
---------------
이 모듈이 내는 값은 `sector_metrics` 테이블에만 들어간다. 점수·추천·
스크리닝·알림 어디에도 참조를 만들지 않는다. 스모크 테스트가 그 부재를
회귀 가드로 검사한다.

이유는 표본이다. 자기검증 채점이 아직 0건인 시점에 섹터 국면을 판단
축으로 넣으면, 검증되지 않은 가정이 하나 더 얹힌다. 지금 필요한 것은
'나중에 되짚을 수 있게 남겨두는 것' 이다. 지표를 쓰기 시작하는 시점은
채점 표본이 최소치에 닿은 뒤 사람이 정한다.

지표 6종 (섹터 하나당 한 행)
--------------------------
  RS 5d / 20d      섹터 시총가중 평균 수익률 − 코스피 동일 기간 수익률
  RS 순위          20d RS 기준 전체 섹터 중 순위 (1 = 가장 강함)
  모멘텀 지속성    persist_lookback 거래일 전에도 상위 1/3 이었는가
  폭(breadth)      종가가 자기 MA20 위인 종목 비율(%)
  거래대금 집중도  전체 시장 거래대금 대비 비중(%) + 20일 평균 대비 변화율
  신고가           최근 252거래일 최고 종가의 99% 이상인 종목 수와 비율

설계 원칙
--------
계산은 전부 순수 함수다. 네트워크·DB 를 만지지 않으므로 합성 픽스처로
손계산과 대조할 수 있다. 수집기(`collect_sector_metrics`)만 store 와
네트워크를 만지고, 그 안의 실패는 전부 삼킨다 — 섹터 지표가 안 나오는
날에 daily 전체가 실패하면 정작 중요한 스캔·추천 기록이 날아간다.

값이 없으면 None 으로 남긴다. 0 으로 채우면 '보합'이나 '해당 없음'으로
읽혀서, 데이터가 없는 것과 실제로 0인 것을 구분할 수 없게 된다.
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from .config import Config, DEFAULT

log = logging.getLogger(__name__)

__all__ = ["UNCLASSIFIED", "sector_map", "ticker_returns", "above_ma",
           "new_high_flags", "turnover_matrix", "sector_turnover_share",
           "compute_sector_metrics", "collect_sector_metrics"]

# 분류 불가 종목을 담는 이름. 집계에서 제외한다.
# 빈 문자열이나 None 으로 두지 않는 이유는, 그러면 '몇 종목이 분류에서
# 빠졌는지' 를 세지 못하고 조용히 사라지기 때문이다.
UNCLASSIFIED = "미분류"


# ══════════════════════════ 섹터 분류 ══════════════════════════
def sector_map(store, use_fdr: bool = True) -> tuple[dict, dict]:
    """{종목코드: 섹터}. (매핑, 통계) 반환.

    1순위는 master(`tickers.sector`) 다. 매일 `--mode master` 가 채운다.
    비어 있는 종목만 FDR 상장목록으로 보강한다 — 전종목을 FDR 로 다시
    받으면 master 를 채우는 의미가 없고 조회도 느리다.

    분류가 끝까지 안 되면 `UNCLASSIFIED` 로 묶는다. 호출부가 집계에서
    빼되 건수는 셀 수 있어야 한다.
    """
    meta = store.ticker_meta()
    stats = {"active": 0, "from_master": 0, "from_fdr": 0, "unclassified": 0}
    if meta is None or meta.empty:
        return {}, stats

    out: dict[str, str] = {}
    missing: list[str] = []
    for code, row in meta.iterrows():
        stats["active"] += 1
        sec = row.get("sector")
        if isinstance(sec, str) and sec.strip():
            out[str(code)] = sec.strip()
            stats["from_master"] += 1
        else:
            missing.append(str(code))

    if missing and use_fdr:
        for code, sec in _fdr_sectors(missing).items():
            out[code] = sec
            stats["from_fdr"] += 1

    for code in meta.index:
        code = str(code)
        if code not in out:
            out[code] = UNCLASSIFIED
            stats["unclassified"] += 1
    return out, stats


def _fdr_sectors(codes: list[str]) -> dict:
    """FDR 상장목록에서 업종만 뽑아온다. 실패하면 빈 dict.

    `universe.sector_source()` 가 이미 '업종 컬럼이 있는 상장목록'을
    찾아주므로 그걸 그대로 쓴다. 같은 탐색을 두 번 구현하면 한쪽이
    낡는다.
    """
    want = set(codes)
    try:
        from .universe import sector_source
        rep = sector_source()
    except Exception as exc:  # noqa: BLE001
        log.warning("업종 보강 실패 (%s: %s)", type(exc).__name__, exc)
        return {}
    lst = rep.pop("_frame", None)
    if lst is None:
        log.warning("업종을 주는 상장목록이 없습니다 — %d종목 미분류로 둡니다",
                    len(want))
        return {}
    ccol, scol = rep["code_col"], rep["sector_col"]
    got = {}
    for _, r in lst[[ccol, scol]].dropna().iterrows():
        code = str(r[ccol]).zfill(6)
        if code in want:
            got[code] = str(r[scol])[:40]
    log.info("업종 보강: %d/%d종목 (%s)", len(got), len(want), rep["listing"])
    return got


# ══════════════════════════ 종목 단위 계산 ══════════════════════════
def ticker_returns(close: pd.DataFrame, bars: int) -> pd.Series:
    """종목별 `bars` 거래일 수익률(%). index=종목코드.

    달력일이 아니라 봉 개수로 센다. `iloc[-1]` 대비 `iloc[-(bars+1)]` 이라
    두 지점이 정확히 `bars` 봉 떨어져 있다. 봉이 부족하면 전부 NaN.
    """
    if close is None or close.empty or len(close) < bars + 1:
        return pd.Series(dtype="float64")
    a = close.iloc[-(bars + 1)]
    b = close.iloc[-1]
    ok = a.notna() & b.notna() & (a > 0)
    return ((b[ok] / a[ok]) - 1.0) * 100.0


def above_ma(close: pd.DataFrame, window: int = 20) -> pd.Series:
    """종목별 '당일 종가 > 자기 MA{window}' 여부. index=종목코드.

    창이 덜 찬 종목은 제외한다(NaN). 5봉짜리 평균을 MA20 이라 부르면
    폭 지표가 신규 상장 종목 쪽으로 왜곡된다.
    """
    if close is None or close.empty or len(close) < window:
        return pd.Series(dtype="boolean")
    seg = close.tail(window)
    ma = seg.mean(skipna=False)          # 결측이 하나라도 있으면 NaN
    last = close.iloc[-1]
    ok = ma.notna() & last.notna() & (ma > 0)
    return (last[ok] > ma[ok])


def new_high_flags(close: pd.DataFrame, lookback: int = 252,
                   ratio: float = 0.99) -> pd.Series:
    """종목별 52주 신고가 여부. index=종목코드.

    '최근 lookback 거래일 최고 종가 x ratio 이상' 이면 신고가로 본다.
    정확히 최고가일 때만 세면 하루 차이로 전부 0 이 되어 추세를 못 본다.

    봉이 lookback 보다 적으면 있는 만큼으로 판정한다. 상장 6개월 종목의
    '반년 신고가' 는 여전히 정보다. 단 최소 두 봉은 있어야 한다.
    """
    if close is None or close.empty or len(close) < 2:
        return pd.Series(dtype="boolean")
    seg = close.tail(lookback)
    peak = seg.max()
    last = close.iloc[-1]
    ok = peak.notna() & last.notna() & (peak > 0)
    return (last[ok] >= peak[ok] * float(ratio))


def turnover_matrix(store, days: int) -> tuple[pd.DataFrame, str]:
    """거래대금 행렬. (행렬, 출처) 반환.

    `prices.amt` 가 비어 있으면 종가 x 거래량으로 근사한다.
    `universe.liquidity_filter` 가 이미 같은 폴백을 쓴다 — 이 저장소의
    `amt` 는 전 행 NULL 이다(2026-09-01 실측 1,022,436행 전부).
    폴백이 없으면 거래대금 집중도가 영구히 NULL 로 남는다.
    """
    amt = store.field_matrix("amt", days=days)
    if amt is not None and not amt.empty and bool(amt.notna().any().any()):
        return amt, "amt"
    c = store.price_matrix(days=days)
    v = store.field_matrix("v", days=days)
    if c is None or c.empty or v is None or v.empty:
        return pd.DataFrame(), "none"
    cols = [x for x in c.columns if x in v.columns]
    if not cols:
        return pd.DataFrame(), "none"
    return (c[cols] * v[cols]), "close*volume"


def sector_turnover_share(amt: pd.DataFrame, sectors: dict,
                          window: int = 20) -> pd.DataFrame:
    """섹터별 거래대금 비중(%)과 `window`일 평균 대비 변화율(%).

    반환: index=섹터, columns=[share, share_chg]

    분모는 **전체 시장** 거래대금이다. 미분류 종목도 분모에 들어간다.
    그래서 섹터 비중의 합이 100%가 되지 않고, 그 차이가 곧 미분류 비중이다.
    분모에서 빼면 합이 100%로 맞아떨어져 보기엔 좋지만, 분류가 안 된
    거래대금이 얼마인지 알 수 없게 된다.

    비중은 날짜별로 먼저 구한 뒤 평균을 낸다. 순서를 바꾸면(평균 거래대금
    으로 비중을 구하면) 거래대금이 큰 날에 가중이 쏠린다.
    """
    empty = pd.DataFrame(columns=["share", "share_chg"], dtype="float64")
    if amt is None or amt.empty or not sectors:
        return empty

    cols = [c for c in amt.columns if sectors.get(str(c), UNCLASSIFIED)
            != UNCLASSIFIED]
    if not cols:
        return empty
    groups = pd.Series({c: sectors[str(c)] for c in cols})

    # 날짜 x 섹터 거래대금 (분자는 분류된 종목만)
    by_sec = amt[cols].T.groupby(groups).sum(min_count=1).T
    # 분모는 전종목
    total = amt.sum(axis=1, min_count=1)
    total = total.where(total > 0)
    share = by_sec.div(total, axis=0) * 100.0
    if share.empty:
        return empty

    last = share.iloc[-1]
    base = share.tail(window).mean()
    chg = pd.Series(np.nan, index=share.columns, dtype="float64")
    ok = base.notna() & (base > 0) & last.notna()
    chg[ok] = (last[ok] / base[ok] - 1.0) * 100.0
    return pd.DataFrame({"share": last, "share_chg": chg})


# ══════════════════════════ 섹터 집계 ══════════════════════════
def _weighted_mean(vals: pd.Series, caps: pd.Series) -> tuple[float, str]:
    """시총가중 평균. 불가하면 동일가중. (값, 방식) 반환.

    가중 대상 종목 **전부** 에 양수 시총이 있을 때만 시총가중을 쓴다.
    일부만 가중하면 모집단이 조용히 갈라져서, 어떤 섹터는 시총가중이고
    어떤 섹터는 반쯤 섞인 값이 된다. 그러면 섹터 간 비교가 성립하지 않는다.
    """
    v = vals.dropna()
    if v.empty:
        return float("nan"), "none"
    w = caps.reindex(v.index)
    w = w[w.notna() & (w > 0)]
    if len(w) == len(v) and float(w.sum()) > 0:
        return float((v.loc[w.index] * w).sum() / float(w.sum())), "cap"
    return float(v.mean()), "equal"


def compute_sector_metrics(close: pd.DataFrame, amt: pd.DataFrame,
                           sectors: dict, market_caps: pd.Series | None,
                           kospi_ret_5d: float | None,
                           kospi_ret_20d: float | None,
                           prev_ranks: dict | None = None,
                           cfg: Config = DEFAULT,
                           scan_date: str | None = None) -> list[dict]:
    """섹터 지표 전 항목. 순수 계산 — 네트워크·DB 를 만지지 않는다.

    close / amt : index=날짜(오름차순), columns=종목코드
    sectors     : {종목코드: 섹터}. UNCLASSIFIED 는 집계에서 뺀다.
    market_caps : index=종목코드. None 이면 전부 동일가중.
    prev_ranks  : {섹터: rs_rank} — persist_lookback 거래일 전 스냅샷.
                  None 이거나 비어 있으면 momentum_persist 는 None.
    """
    sc = cfg.sector
    if close is None or close.empty or not sectors:
        return []

    # **기준일 이후 봉을 자른다.** 이게 없으면 두 가지로 조용히 망가진다.
    #   1) 부분 적재일이 행렬 끝에 있으면(예: 13종목만 들어온 날) 마지막
    #      행이 대부분 NaN 이라 거의 모든 섹터의 지표가 NULL 이 된다.
    #   2) 장중에 돌리면 아직 확정되지 않은 당일 봉을 보고 계산한다.
    # 실측으로 1번을 맞았다: 157섹터 중 146개가 NULL 이었다.
    if scan_date:
        cut = pd.Timestamp(scan_date)
        close = close[close.index <= cut]
        if amt is not None and not amt.empty:
            amt = amt[amt.index <= cut]
        if close.empty:
            log.warning("섹터 지표: %s 이전 봉이 없습니다", scan_date)
            return []

    caps = (market_caps if market_caps is not None
            else pd.Series(dtype="float64"))

    r_short = ticker_returns(close, sc.rs_short)
    r_long = ticker_returns(close, sc.rs_long)
    ma_ok = above_ma(close, sc.breadth_ma)
    nh = new_high_flags(close, sc.new_high_lookback, sc.new_high_ratio)
    turn = sector_turnover_share(amt, sectors, sc.turnover_ma)

    # 섹터 -> 그 섹터의 종목코드
    buckets: dict[str, list[str]] = {}
    for code in close.columns:
        sec = sectors.get(str(code), UNCLASSIFIED)
        if sec == UNCLASSIFIED:
            continue
        buckets.setdefault(sec, []).append(str(code))

    rows: list[dict] = []
    weight_modes: dict[str, int] = {}
    for sec, codes in buckets.items():
        try:
            idx = pd.Index(codes)
            n = int(len(codes))

            m_short, mode = _weighted_mean(r_short.reindex(idx), caps)
            m_long, mode_l = _weighted_mean(r_long.reindex(idx), caps)
            weight_modes[mode_l] = weight_modes.get(mode_l, 0) + 1

            rs5 = (None if not np.isfinite(m_short) or kospi_ret_5d is None
                   else float(m_short - float(kospi_ret_5d)))
            rs20 = (None if not np.isfinite(m_long) or kospi_ret_20d is None
                    else float(m_long - float(kospi_ret_20d)))

            b = ma_ok.reindex(idx).dropna()
            breadth = (None if b.empty
                       else float(b.astype("float64").mean() * 100.0))

            f = nh.reindex(idx).dropna()
            nh_cnt = None if f.empty else int(f.astype("int64").sum())
            nh_pct = (None if f.empty
                      else float(f.astype("float64").mean() * 100.0))

            share = turn["share"].get(sec, np.nan) if not turn.empty else np.nan
            chg = turn["share_chg"].get(sec, np.nan) if not turn.empty else np.nan

            rows.append({
                "scan_date": scan_date,
                "sector": sec,
                "n_stocks": n,
                "rs_5d": rs5,
                "rs_20d": rs20,
                "rs_rank": None,           # 아래에서 전체 섹터를 보고 채운다
                "momentum_persist": None,  # 순위를 채운 뒤 판정한다
                "breadth_ma20": breadth,
                "turnover_share": (None if not np.isfinite(share)
                                   else float(share)),
                "turnover_share_chg": (None if not np.isfinite(chg)
                                       else float(chg)),
                "new_high_cnt": nh_cnt,
                "new_high_pct": nh_pct,
            })
        except Exception as exc:  # noqa: BLE001 - 섹터 단위 격리
            log.warning("섹터 지표 계산 실패 — %s 건너뜁니다 (%s: %s)",
                        sec, type(exc).__name__, exc)

    _fill_ranks(rows, prev_ranks, sc.persist_top_fraction)
    if weight_modes:
        log.info("섹터 RS 가중 방식: %s",
                 " · ".join(f"{k} {v}개" for k, v in sorted(weight_modes.items())))
    return rows


def _fill_ranks(rows: list[dict], prev_ranks: dict | None,
                top_fraction: float) -> None:
    """rs_20d 로 순위를 매기고 모멘텀 지속성을 판정한다 (제자리 수정).

    rs_20d 가 없는 섹터는 순위를 주지 않는다(None). 순위를 억지로 주면
    데이터가 없는 섹터가 꼴찌로 기록되어 '약세'로 읽힌다.
    """
    scored = [r for r in rows if r["rs_20d"] is not None]
    scored.sort(key=lambda r: -r["rs_20d"])
    for i, r in enumerate(scored, 1):
        r["rs_rank"] = i

    if not prev_ranks:
        return                       # 과거 스냅샷 없음 -> 전부 None 유지
    cutoff_prev = max(1, int(len(prev_ranks) * float(top_fraction)))
    n_now = len(scored)
    cutoff_now = max(1, int(n_now * float(top_fraction)))
    for r in scored:
        was = prev_ranks.get(r["sector"])
        if was is None:
            continue                 # 그 섹터의 과거 순위가 없다 -> None
        r["momentum_persist"] = int(was <= cutoff_prev
                                    and r["rs_rank"] <= cutoff_now)


# ══════════════════════════ 수집 (daily) ══════════════════════════
def _kospi_returns(store, trade_date: str) -> tuple[float | None, float | None]:
    """코스피 5일/20일 수익률.

    `market_context` 에 이미 기록된 값을 1순위로 쓴다. 같은 날 두 번
    조회하면 두 테이블에 다른 숫자가 남을 수 있고, 그러면 나중에 어느
    쪽이 맞는지 알 수 없다.
    """
    try:
        row = store.market_context(trade_date)
    except Exception:  # noqa: BLE001
        row = None
    if row and row.get("kospi_ret_5d") is not None:
        return row.get("kospi_ret_5d"), row.get("kospi_ret_20d")

    from .daily import market_context as _mc
    from .data import load_index
    ks = load_index("KOSPI", bars=60)
    ctx = _mc(ks, None, trade_date)
    if ctx is None:
        return None, None
    return ctx.get("kospi_ret_5d"), ctx.get("kospi_ret_20d")


def collect_sector_metrics(store, trade_date: str, cfg: Config = DEFAULT,
                           use_fdr: bool = True) -> dict:
    """섹터 지표를 계산해 기록한다. 하루 1회.

    **실패해도 예외를 올리지 않는다.** 섹터 지표는 기록 전용 부가 정보라,
    이것 때문에 daily 가 죽으면 손해가 훨씬 크다. 섹터 하나가 터지면
    그 섹터만 건너뛴다(`compute_sector_metrics` 안에서 격리).
    """
    out = {"scan_date": trade_date, "sectors": 0, "skipped": False}
    if not trade_date:
        return out
    try:
        if store.has_sector_metrics(trade_date):
            out["skipped"] = True
            return out

        sc = cfg.sector
        need = max(sc.new_high_lookback, sc.rs_long + 1, sc.breadth_ma,
                   sc.turnover_ma) + 5
        close = store.price_matrix(days=need)
        amt, amt_src = turnover_matrix(store, max(sc.turnover_ma + 10, 40))
        out["turnover_source"] = amt_src
        if close is None or close.empty:
            log.warning("섹터 지표: 시세가 없습니다 — %s 생략", trade_date)
            return out

        sectors, sstats = sector_map(store, use_fdr=use_fdr)
        out.update(sstats)
        if not sectors:
            log.warning("섹터 지표: 종목 마스터가 비어 있습니다 — 생략")
            return out

        meta = store.ticker_meta()
        caps = (meta["market_cap"].astype("float64")
                if meta is not None and "market_cap" in meta.columns else None)

        k5, k20 = _kospi_returns(store, trade_date)
        out["kospi_ret_5d"], out["kospi_ret_20d"] = k5, k20
        if k5 is None and k20 is None:
            # RS 는 코스피 대비값이다. 기준이 없으면 RS 를 만들지 않는다.
            # 나머지 지표(폭·신고가·거래대금)는 그대로 기록한다.
            log.warning("섹터 지표: 코스피 수익률을 못 구해 RS 는 비웁니다")

        prev_ranks = _prev_ranks(store, trade_date, sc.persist_lookback)
        out["prev_snapshot"] = bool(prev_ranks)

        rows = compute_sector_metrics(
            close, amt, sectors, caps, k5, k20,
            prev_ranks=prev_ranks, cfg=cfg, scan_date=trade_date)
        out["sectors"] = store.upsert_sector_metrics(rows)
    except Exception as exc:  # noqa: BLE001 - daily 를 죽이지 않는다
        log.warning("섹터 지표 기록 실패 — 건너뜁니다 (%s: %s)",
                    type(exc).__name__, exc)
        return out

    log.info("섹터 지표 기록 %s · %d섹터 (미분류 %d종목 제외 · 거래대금 %s "
             "· 과거 스냅샷 %s)",
             trade_date, out["sectors"], out.get("unclassified", 0),
             out.get("turnover_source", "-"),
             "있음" if out.get("prev_snapshot") else "없음")
    return out


def _prev_ranks(store, trade_date: str, lookback: int) -> dict:
    """`lookback` 거래일 전 거래일의 RS 순위. 없으면 빈 dict."""
    try:
        dates = store.price_dates()
    except Exception:  # noqa: BLE001
        return {}
    if not dates or trade_date not in dates:
        return {}
    i = dates.index(trade_date) - int(lookback)
    if i < 0:
        return {}
    try:
        return store.sector_rs_ranks(dates[i])
    except Exception:  # noqa: BLE001
        return {}
