# -*- coding: utf-8 -*-
"""금요일 주간 분석. 매일 쌓인 스냅샷을 재료로 쓴다.

매일 점수를 저장해야만 얻을 수 있는 것
------------------------------------
하루치 스냅샷으로는 "지금 몇 점인가"만 안다. 5일을 쌓으면 "점수가
올라오는 중인가 내려가는 중인가"를 알 수 있고, 이게 훨씬 강한 정보다.
바닥은 한 점이 아니라 과정이기 때문이다.

주간 리포트 7개 항목
  1. 자기 검증(audit)   : 지난주 추천의 실제 성적 vs 시장 중위수  ← 가장 중요
  2. 점수 모멘텀        : 5일간 매집점수 상승폭 상위 = 바닥 다지는 중
  3. 연속 등재          : 5일 내내 리스트에 든 종목 = 노이즈가 아닌 구조적 신호
  4. 신규 진입 / 이탈   : 이번 주 새로 들어온 종목, 빠진 종목
  5. 청산 중심선 ETA    : 밴드 접근 속도로 도달 예상 거래일 추정
  6. 주중 이벤트        : 피보 0.618 하향 이탈, 골든크로스 발생
  7. 섹터 편중 경고     : 추천이 한 업종에 몰렸는지

1번을 리포트 맨 앞에 두는 이유는, 검증 없는 추천은 시간이 지나면
반드시 신뢰를 잃기 때문이다. 시스템이 스스로 성적표를 낸다.
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from .config import Config, DEFAULT

log = logging.getLogger(__name__)

__all__ = ["audit_recos", "score_momentum", "persistence", "churn",
           "band_eta", "weekly_events", "sector_concentration", "weekly_report"]


# ────────────────────────── 1. 자기 검증 ──────────────────────────
def audit_recos(store, horizon: int = 5, lookback: int = 12) -> dict:
    """과거 추천의 실제 성적. 시장 중위 수익률을 대조군으로 쓴다.

    horizon : 보유 가정 거래일수 (기본 5 = 1주)
    대조군을 안 쓰면 하락장 반등을 전략의 실력으로 착각한다.
    """
    recos = store.reco_history(days=lookback)
    pm = store.price_matrix(days=lookback + horizon + 5)
    if recos.empty or pm.empty:
        return {"n": 0, "note": "누적 데이터 부족"}

    dates = list(pm.index.strftime("%Y-%m-%d"))
    pos = {d: i for i, d in enumerate(dates)}

    rows = []
    for _, r in recos.iterrows():
        d0 = str(r["d"])[:10]
        t = str(r["ticker"])
        if d0 not in pos or t not in pm.columns:
            continue
        i0 = pos[d0]
        i1 = i0 + horizon
        if i1 >= len(dates):
            continue
        p0, p1 = pm.iloc[i0][t], pm.iloc[i1][t]
        if not (pd.notna(p0) and pd.notna(p1) and p0 > 0):
            continue
        ret = float(p1 / p0 - 1.0) * 100.0

        # 같은 구간의 시장 중위 수익률
        col0, col1 = pm.iloc[i0], pm.iloc[i1]
        mask = col0.notna() & col1.notna() & (col0 > 0)
        mkt = float(((col1[mask] / col0[mask]) - 1.0).median() * 100.0) if mask.any() else np.nan

        rows.append({"date": d0, "ticker": t, "name": r["name"],
                     "slot": r["slot"], "grade": r["grade"],
                     "ret": ret, "mkt": mkt, "alpha": ret - mkt})

    if not rows:
        return {"n": 0, "note": f"보유기간 {horizon}일 경과 추천 없음"}

    df = pd.DataFrame(rows)
    by_slot = (df.groupby("slot")["alpha"]
               .agg(["count", "mean"]).round(2).to_dict("index"))
    return {
        "n": int(len(df)),
        "horizon": horizon,
        "win_rate": float((df["ret"] > 0).mean() * 100.0),
        "alpha_win_rate": float((df["alpha"] > 0).mean() * 100.0),
        "mean_ret": float(df["ret"].mean()),
        "median_ret": float(df["ret"].median()),
        "mean_mkt": float(df["mkt"].mean()),
        "mean_alpha": float(df["alpha"].mean()),
        "best": df.nlargest(3, "ret")[["name", "ret"]].to_dict("records"),
        "worst": df.nsmallest(3, "ret")[["name", "ret"]].to_dict("records"),
        "by_slot": by_slot,
        "detail": df,
    }


# ────────────────────────── 2. 점수 모멘텀 ──────────────────────────
def score_momentum(scans: pd.DataFrame, top_n: int = 8,
                   min_days: int = 4) -> pd.DataFrame:
    """주간 매집점수 상승폭 상위. 바닥이 다져지는 중인 종목."""
    if scans.empty:
        return pd.DataFrame()
    s = scans.sort_values("d")
    g = s.groupby("ticker")
    agg = g.agg(
        name=("name", "last"),
        days=("d", "count"),
        first_v=("value_score", "first"),
        last_v=("value_score", "last"),
        last_t=("trend_score", "last"),
        price=("price", "last"),
        band_pos=("band_pos", "last"),
        fib_ratio=("fib_ratio", "last"),
    )
    agg = agg[agg["days"] >= min_days].copy()
    if agg.empty:
        return pd.DataFrame()
    agg["delta_v"] = (agg["last_v"] - agg["first_v"]).round(2)
    return agg.nlargest(top_n, "delta_v").reset_index()


# ────────────────────────── 3. 연속 등재 ──────────────────────────
def persistence(recos: pd.DataFrame, scans: pd.DataFrame,
                min_hits: int = 3) -> pd.DataFrame:
    """추천 리스트에 반복 등재된 종목. 하루짜리 노이즈를 걸러낸다."""
    if recos.empty:
        return pd.DataFrame()
    days = recos["d"].nunique()
    cnt = (recos.groupby(["ticker", "name"])
           .agg(hits=("rank", "count"), best_rank=("rank", "min"),
                avg_v=("value_score", "mean"), avg_t=("trend_score", "mean"))
           .reset_index())
    cnt = cnt[cnt["hits"] >= min_hits].copy()
    if cnt.empty:
        return cnt
    cnt["hit_rate"] = (cnt["hits"] / max(days, 1) * 100).round(0)
    cnt[["avg_v", "avg_t"]] = cnt[["avg_v", "avg_t"]].round(2)
    if not scans.empty:
        last = scans.sort_values("d").groupby("ticker").last()
        cnt["price"] = cnt["ticker"].map(last["price"])
        cnt["grade"] = cnt["ticker"].map(last["grade"])
    return cnt.sort_values(["hits", "avg_v"], ascending=[False, False])


# ────────────────────────── 4. 신규 진입 / 이탈 ──────────────────────────
def churn(recos: pd.DataFrame, week_days: int = 5) -> dict:
    """이번 주 새로 진입한 종목과 빠진 종목."""
    if recos.empty:
        return {"entered": [], "dropped": []}
    days = sorted(recos["d"].unique())
    if len(days) < 2:
        return {"entered": [], "dropped": []}
    cur_days = days[-week_days:]
    prev_days = days[:-week_days] or days[:1]
    cur = recos[recos["d"].isin(cur_days)]
    prev = recos[recos["d"].isin(prev_days)]
    cur_map = dict(zip(cur["ticker"], cur["name"]))
    prev_map = dict(zip(prev["ticker"], prev["name"]))
    entered = [(t, n) for t, n in cur_map.items() if t not in prev_map]
    dropped = [(t, n) for t, n in prev_map.items() if t not in cur_map]
    return {"entered": entered, "dropped": dropped}


# ────────────────────────── 5. 청산 중심선 ETA ──────────────────────────
def band_eta(scans: pd.DataFrame, target_pos: float = 0.5,
             top_n: int = 8, min_days: int = 4) -> pd.DataFrame:
    """밴드 위치 하락 속도로 청산 중심선(-30%) 도달 예상 거래일 추정.

    선형 외삽이므로 정밀 예측이 아니라 '매복 우선순위'용이다.
    아직 밴드 위에 있고 내려오는 중인 종목만 대상으로 한다.
    """
    if scans.empty or "band_pos" not in scans.columns:
        return pd.DataFrame()
    s = scans.dropna(subset=["band_pos"]).sort_values("d")
    rows = []
    for t, grp in s.groupby("ticker"):
        if len(grp) < min_days:
            continue
        y = grp["band_pos"].to_numpy(dtype="float64")
        x = np.arange(len(y), dtype="float64")
        slope = float(np.polyfit(x, y, 1)[0])
        cur = float(y[-1])
        if slope >= -1e-4 or cur <= target_pos:
            continue  # 하락 중이 아니거나 이미 도달
        eta = (cur - target_pos) / (-slope)
        if not np.isfinite(eta) or eta > 60:
            continue
        rows.append({
            "ticker": t, "name": grp["name"].iloc[-1],
            "price": float(grp["price"].iloc[-1]),
            "band_mid": float(grp["band_mid"].iloc[-1])
            if pd.notna(grp["band_mid"].iloc[-1]) else np.nan,
            "band_pos": round(cur, 3),
            "speed_per_day": round(slope, 4),
            "eta_days": int(round(eta)),
        })
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).nsmallest(top_n, "eta_days")


# ────────────────────────── 6. 주중 이벤트 ──────────────────────────
def weekly_events(scans: pd.DataFrame, week_days: int = 5) -> dict:
    """주중 발생한 상태 전이 이벤트."""
    out: dict = {"fib_breaks": [], "cross": []}
    if scans.empty:
        return out
    s = scans.sort_values("d")
    days = sorted(s["d"].unique())[-week_days:]
    if not days:
        return out
    first_day, last_day = days[0], days[-1]

    # 피보 0.618 하향 이탈: 주 시작엔 미도달 -> 주말엔 이하
    if "fib_below" in s.columns:
        a = s[s["d"] == first_day].set_index("ticker")
        b = s[s["d"] == last_day].set_index("ticker")
        common = a.index.intersection(b.index)
        for t in common:
            if (a.at[t, "fib_below"] in (0, 0.0)) and (b.at[t, "fib_below"] in (1, 1.0)):
                out["fib_breaks"].append({
                    "ticker": t, "name": b.at[t, "name"],
                    "price": float(b.at[t, "price"]),
                    "ratio": float(b.at[t, "fib_ratio"])
                    if pd.notna(b.at[t, "fib_ratio"]) else np.nan,
                })

    # 골든크로스: 주중에 새로 발생 (마감일 기준 bars_ago < 주간 일수)
    b = s[s["d"] == last_day]
    if "cross_bars_ago" in b.columns:
        fresh = b[b["cross_bars_ago"].notna() & (b["cross_bars_ago"] < week_days)]
        for _, r in fresh.sort_values("trend_score", ascending=False).head(12).iterrows():
            out["cross"].append({
                "ticker": r["ticker"], "name": r["name"],
                "price": float(r["price"]), "pair": r["cross_pair"],
                "bars_ago": int(r["cross_bars_ago"]),
                "trend_score": float(r["trend_score"]),
            })
    return out


# ────────────────────────── 7. 섹터 편중 ──────────────────────────
def sector_concentration(recos: pd.DataFrame, meta: pd.DataFrame,
                         week_days: int = 5, warn_ratio: float = 0.4) -> dict:
    """주간 추천의 업종 분포. 한 업종이 40% 넘으면 경고."""
    if recos.empty or meta.empty or "sector" not in meta.columns:
        return {"dist": {}, "warn": None}
    days = sorted(recos["d"].unique())[-week_days:]
    cur = recos[recos["d"].isin(days)].drop_duplicates("ticker")
    sec = cur["ticker"].map(meta["sector"]).fillna("미분류")
    dist = sec.value_counts().to_dict()
    total = sum(dist.values())
    warn = None
    if total:
        top_sec, top_cnt = max(dist.items(), key=lambda kv: kv[1])
        if top_cnt / total >= warn_ratio:
            warn = f"{top_sec} {top_cnt}/{total}종목 ({top_cnt / total * 100:.0f}%)"
    return {"dist": dist, "warn": warn}


# ────────────────────────── 종합 ──────────────────────────
def weekly_report(store, cfg: Config = DEFAULT, week_days: int = 5,
                  horizon: int = 5) -> dict:
    """금요일 리포트 재료 일괄 생성."""
    scans = store.scan_history(days=week_days * 2)
    recos = store.reco_history(days=week_days * 3)
    meta = store.ticker_meta()

    week_scan_days = sorted(scans["d"].unique())[-week_days:] if not scans.empty else []
    week_scans = scans[scans["d"].isin(week_scan_days)] if week_scan_days else scans

    return {
        "trade_date": store.last_price_date(),
        "days_covered": len(week_scan_days),
        "universe_size": int(week_scans["ticker"].nunique()) if not week_scans.empty else 0,
        "audit": audit_recos(store, horizon=horizon),
        "momentum": score_momentum(week_scans),
        "persistence": persistence(recos, week_scans),
        "churn": churn(recos, week_days),
        "eta": band_eta(week_scans),
        "events": weekly_events(week_scans, week_days),
        "sector": sector_concentration(recos, meta, week_days),
    }
