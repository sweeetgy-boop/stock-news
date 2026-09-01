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

import bisect
import logging
from datetime import date, timedelta

import numpy as np
import pandas as pd

from .config import Config, DEFAULT
from .contracts import STOP_RULE_PREFIX
from .trading_day import as_date, now_kst

log = logging.getLogger(__name__)

__all__ = ["audit_recos", "audit_multi", "score_momentum", "persistence",
           "churn", "band_eta", "weekly_events", "sector_concentration",
           "is_first_friday", "prev_month_bounds", "monthly_review",
           "weekly_report"]


# ────────────────────────── 1. 자기 검증 ──────────────────────────
def _score_horizon(recos: pd.DataFrame, pm: pd.DataFrame,
                   horizon: int) -> tuple[pd.DataFrame, int, int]:
    """한 보유기간의 채점. (채점된 행, 미도래 건수, 가격없음 건수).

    **미도래는 채점에서 뺀다.** 0으로 넣으면 평균이 0쪽으로 끌려가
    성적이 실제보다 나빠 보이고, 실패로 세면 표본이 조용히 줄어든다.
    별도로 세서 리포트에 그대로 노출한다.
    """
    dates = list(pm.index.strftime("%Y-%m-%d"))
    pos = {d: i for i, d in enumerate(dates)}

    rows: list[dict] = []
    pending = 0
    no_price = 0
    for _, r in recos.iterrows():
        d0 = str(r["d"])[:10]
        t = str(r["ticker"])
        if d0 not in pos or t not in pm.columns:
            no_price += 1
            continue
        i0 = pos[d0]
        i1 = i0 + horizon
        if i1 >= len(dates):
            pending += 1          # 보유기간이 아직 경과하지 않음
            continue
        p0, p1 = pm.iloc[i0][t], pm.iloc[i1][t]
        if not (pd.notna(p0) and pd.notna(p1) and p0 > 0):
            no_price += 1
            continue
        ret = float(p1 / p0 - 1.0) * 100.0

        # 같은 구간의 시장 중위 수익률 (대조군)
        col0, col1 = pm.iloc[i0], pm.iloc[i1]
        mask = col0.notna() & col1.notna() & (col0 > 0)
        mkt = (float(((col1[mask] / col0[mask]) - 1.0).median() * 100.0)
               if mask.any() else np.nan)

        rows.append({"date": d0, "ticker": t, "name": r["name"],
                     "slot": r["slot"], "grade": r["grade"],
                     "ret": ret, "mkt": mkt, "alpha": ret - mkt})

    return pd.DataFrame(rows), pending, no_price


def _summarize(df: pd.DataFrame, horizon: int, pending: int,
               no_price: int) -> dict:
    """한 보유기간의 집계. 표본이 0이면 숫자를 만들지 않는다."""
    if df.empty:
        return {"horizon": horizon, "n": 0, "pending": pending,
                "no_price": no_price,
                "note": (f"보유 {horizon}거래일 미도래 {pending}건"
                         if pending else f"보유 {horizon}거래일 표본 없음")}
    return {
        "horizon": horizon,
        "n": int(len(df)),
        "pending": pending,
        "no_price": no_price,
        "win_rate": float((df["ret"] > 0).mean() * 100.0),
        "alpha_win_rate": float((df["alpha"] > 0).mean() * 100.0),
        "mean_ret": float(df["ret"].mean()),
        "median_ret": float(df["ret"].median()),
        "mean_mkt": float(df["mkt"].mean()),
        "mean_alpha": float(df["alpha"].mean()),
        "best": df.nlargest(3, "ret")[["name", "ret"]].to_dict("records"),
        "worst": df.nsmallest(3, "ret")[["name", "ret"]].to_dict("records"),
        "by_slot": (df.groupby("slot")["alpha"]
                    .agg(["count", "mean"]).round(2).to_dict("index")),
        "detail": df,
    }


def audit_recos(store, horizon: int = 5, lookback: int = 12) -> dict:
    """과거 추천의 실제 성적 (단일 보유기간).

    horizon : 보유 가정 거래일수 (기본 5 = 1주)
    대조군을 안 쓰면 하락장 반등을 전략의 실력으로 착각한다.

    여러 기간을 한 번에 보려면 `audit_multi()` 를 쓴다.
    """
    recos = store.reco_history(days=lookback)
    pm = store.price_matrix(days=lookback + horizon + 5)
    if recos.empty or pm.empty:
        return {"n": 0, "note": "누적 데이터 부족"}
    df, pending, no_price = _score_horizon(recos, pm, horizon)
    return _summarize(df, horizon, pending, no_price)


def audit_multi(store, cfg: Config = DEFAULT,
                horizons: tuple[int, ...] | None = None,
                lookback: int | None = None) -> dict:
    """보유기간 5/10/20 을 **병렬로** 채점한다.

    한 기간만 보면 '이 전략이 5일물인지 20일물인지' 를 알 수 없다.
    5일에 알파가 없어도 20일에 나올 수 있고 그 반대도 가능하다. 어느
    보유기간에서 알파가 나는지 비교할 수 있게 나란히 낸다.

    가격 행렬은 **가장 긴 기간** 기준으로 한 번만 뽑아 세 기간이 같은
    구간을 본다. 기간마다 따로 뽑으면 대조군 모집단이 달라져 비교가
    성립하지 않는다.

    반환에 `total_recos`(전체 누적 추천 건수)와 `min_sample` 을 담는다.
    표본이 최소치에 닿기 전에는 성적을 신뢰하지 않는다는 표시다.
    """
    ac = cfg.audit
    hs = tuple(horizons) if horizons else ac.horizons
    lb = int(lookback) if lookback else ac.lookback_dates
    longest = max(hs) if hs else 0

    out: dict = {
        "horizons": list(hs),
        "primary": hs[0] if hs else None,
        "min_sample": ac.min_sample,
        "total_recos": store.reco_count(),
        "by_horizon": {},
    }

    recos = store.reco_history(days=lb)
    pm = store.price_matrix(days=lb + longest + 5)
    if recos.empty or pm.empty:
        out["note"] = "누적 데이터 부족"
        out["by_horizon"] = {
            h: {"horizon": h, "n": 0, "pending": 0, "no_price": 0,
                "note": "누적 데이터 부족"} for h in hs}
        return out

    out["sampled_recos"] = int(len(recos))
    for h in hs:
        df, pending, no_price = _score_horizon(recos, pm, h)
        out["by_horizon"][h] = _summarize(df, h, pending, no_price)
    return out


# ────────────────────── 1-b. 월간 회고 (전월) ──────────────────────
def is_first_friday(d=None) -> bool:
    """그 달의 첫 금요일인가. 월간 회고를 붙일 날을 정한다.

    '첫째 주 금요일'이 아니라 '그 달의 첫 금요일'이다. 1일이 토요일이면
    첫 금요일은 7일이므로 `day <= 7` 로 판정된다.
    """
    dt = as_date(d if d is not None else now_kst())
    return dt.weekday() == 4 and dt.day <= 7


def prev_month_bounds(d=None) -> tuple[str, str, str]:
    """`d` 기준 전월의 (첫날, 마지막날, "YYYY-MM"). 달력일 문자열."""
    dt = as_date(d if d is not None else now_kst())
    first_this = date(dt.year, dt.month, 1)
    last_prev = first_this - timedelta(days=1)
    first_prev = date(last_prev.year, last_prev.month, 1)
    return (first_prev.isoformat(), last_prev.isoformat(),
            f"{last_prev.year:04d}-{last_prev.month:02d}")


def _span_bars(dates: list[str], start: str, end: str) -> int | None:
    """[start, end] 사이 거래일 수. start 봉을 1일로 센다.

    달력일로 세면 주말·연휴에 앞서간다. 적재된 거래일만 센다. 구간이
    시세 범위를 벗어나면 None (셀 수 없음).
    """
    if not dates or not start or not end:
        return None
    s, e = str(start)[:10], str(end)[:10]
    if s > e or e > dates[-1] or s < dates[0]:
        return None
    lo = bisect.bisect_left(dates, s)
    hi = bisect.bisect_right(dates, e)
    return max(0, hi - lo)


def _compliance(store, cfg: Config, entries: pd.DataFrame,
                exits: pd.DataFrame, start: str, end: str) -> dict:
    """규칙 준수율. 숫자와 목록만 만든다.

    세 가지를 센다. 정의를 코드로 고정해 두지 않으면 매달 다른 것을
    세게 되고, 그러면 추세를 볼 수 없다.

      최소보유일 위반  체결된 청산 중 보유 거래일 < MIN_HOLD_DAYS 이고
                       규칙이 손절·무효화·만료가 아닌 것. 엔진은 이런
                       신호를 내지 않으므로 사람이 개입한 흔적이다.
      손절 미이행      규칙이 stop:* 인데 executed=0. 나가라는 신호를
                       받고 나가지 않은 건수다.
      재진입 위반      손절 청산 후 COOLDOWN_DAYS 거래일 안에 같은 종목을
                       다시 진입한 건수.

    `close_position` 은 exit_log 를 남기지 않으므로 수동 종료는 보유일을
    셀 수 없다. 그 건수를 `unverifiable` 로 따로 낸다 — 검증 못 한 것을
    준수로 세면 준수율이 부풀려진다.
    """
    ec = cfg.exit
    dates = store.price_dates()
    min_hold, cooldown = int(ec.min_hold_days), int(ec.cooldown_days)
    exempt = (STOP_RULE_PREFIX, "invalidation:", "hold:expired")

    min_hold_list: list[dict] = []
    stop_pending: list[dict] = []
    checked = 0
    if exits is not None and not exits.empty:
        for _, r in exits.iterrows():
            rule = str(r.get("rule") or "")
            executed = int(r.get("executed") or 0)
            if rule.startswith(STOP_RULE_PREFIX) and not executed:
                stop_pending.append({
                    "ticker": str(r["ticker"]), "name": str(r.get("name") or ""),
                    "d": str(r["d"]), "rule": rule,
                    "signal_price": r.get("signal_price"),
                    "ret_pct": r.get("ret_pct")})
            if not executed:
                continue
            checked += 1
            if rule.startswith(exempt):
                continue
            held = _span_bars(dates, str(r.get("entry_date") or ""), str(r["d"]))
            if held is not None and held < min_hold:
                min_hold_list.append({
                    "ticker": str(r["ticker"]), "name": str(r.get("name") or ""),
                    "entry_date": str(r.get("entry_date") or ""),
                    "d": str(r["d"]), "rule": rule, "held": held,
                    "min_hold_days": min_hold})

    # 재진입 위반: 구간 앞쪽 손절도 봐야 하므로 여유를 두고 조회한다.
    look_from = (as_date(start) - timedelta(days=cooldown * 3 + 30)).isoformat()
    prior = store.exit_log_between(look_from, end)
    stops: dict[str, list[str]] = {}
    if prior is not None and not prior.empty:
        for _, r in prior.iterrows():
            if str(r.get("rule") or "").startswith(STOP_RULE_PREFIX):
                stops.setdefault(str(r["ticker"]), []).append(str(r["d"]))

    reentry: list[dict] = []
    if entries is not None and not entries.empty:
        for _, p in entries.iterrows():
            t = str(p["ticker"])
            ed = str(p["entry_date"])[:10]
            for sd in stops.get(t, ()):
                if sd >= ed:
                    continue          # 진입 이후의 손절은 위반이 아니다
                gap = _span_bars(dates, sd, ed)
                if gap is not None and gap <= cooldown:
                    reentry.append({
                        "ticker": t, "name": str(p.get("name") or ""),
                        "stop_date": sd, "entry_date": ed,
                        "gap_bars": gap, "cooldown_days": cooldown})
                    break

    # 수동 종료(exit_log 없음)는 보유일 검증이 불가능하다.
    unverifiable = 0
    if entries is not None and not entries.empty:
        with_log = set()
        if exits is not None and not exits.empty:
            with_log = {int(x) for x in exits["position_id"].dropna().tolist()}
        unverifiable = int(sum(1 for _, p in entries.iterrows()
                               if str(p.get("status")) == "CLOSED"
                               and int(p["id"]) not in with_log))

    violations = len(min_hold_list) + len(stop_pending) + len(reentry)
    denom = checked + len(stop_pending) + int(len(entries) if entries is not None
                                              and not entries.empty else 0)
    return {
        "min_hold_days": min_hold,
        "cooldown_days": cooldown,
        "checked_exits": checked,
        "checked_entries": int(len(entries)) if entries is not None
        and not entries.empty else 0,
        "min_hold_violations": len(min_hold_list),
        "min_hold_list": min_hold_list,
        "stop_not_executed": len(stop_pending),
        "stop_not_executed_list": stop_pending,
        "reentry_violations": len(reentry),
        "reentry_list": reentry,
        "unverifiable_closes": unverifiable,
        "violations": violations,
        "checks": denom,
        "compliance_pct": (round((denom - violations) / denom * 100.0, 1)
                           if denom else None),
    }


def monthly_review(store, cfg: Config = DEFAULT, asof=None) -> dict:
    """전월 회고. 숫자와 목록만 낸다.

    해석 문구를 넣지 않는다. '개선됐다', '양호하다' 같은 말은 표본이
    20건도 안 되는 시점에 확신을 만들어낸다. 사람이 숫자를 보고 판단한다.
    """
    start, end, month = prev_month_bounds(asof)
    entries = store.positions_between(start, end)
    exits = store.exit_log_between(start, end)
    recos = store.recos_between(start, end)

    hs = tuple(cfg.audit.horizons)
    by_h: dict = {}
    if recos is not None and not recos.empty:
        # 전월 추천을 채점하려면 최장 보유기간만큼 이후 시세가 필요하다.
        span_days = (as_date(now_kst()) - as_date(start)).days + 10
        pm = store.price_matrix(days=span_days)
        for h in hs:
            if pm is None or pm.empty:
                by_h[h] = {"horizon": h, "n": 0, "pending": 0, "no_price": 0,
                           "note": "시세 부족"}
                continue
            df, pending, no_price = _score_horizon(recos, pm, h)
            by_h[h] = _summarize(df, h, pending, no_price)
    else:
        by_h = {h: {"horizon": h, "n": 0, "pending": 0, "no_price": 0,
                    "note": "전월 추천 없음"} for h in hs}

    return {
        "month": month,
        "start": start,
        "end": end,
        "horizons": list(hs),
        "recos": int(len(recos)) if recos is not None and not recos.empty else 0,
        "entries": (entries.to_dict("records")
                    if entries is not None and not entries.empty else []),
        "exits": (exits.to_dict("records")
                  if exits is not None and not exits.empty else []),
        "by_horizon": by_h,
        "compliance": _compliance(store, cfg, entries, exits, start, end),
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
                  horizon: int = 5, monthly: bool | None = None) -> dict:
    """금요일 리포트 재료 일괄 생성.

    monthly : None 이면 '그 달의 첫 금요일인가'로 자동 판정한다.
              True/False 로 강제할 수 있다(검증·수동 재발행용).
    """
    want_monthly = is_first_friday() if monthly is None else bool(monthly)
    scans = store.scan_history(days=week_days * 2)
    recos = store.reco_history(days=week_days * 3)
    meta = store.ticker_meta()

    week_scan_days = sorted(scans["d"].unique())[-week_days:] if not scans.empty else []
    week_scans = scans[scans["d"].isin(week_scan_days)] if week_scan_days else scans

    return {
        "trade_date": store.last_price_date(),
        "days_covered": len(week_scan_days),
        "universe_size": int(week_scans["ticker"].nunique()) if not week_scans.empty else 0,
        # 보유기간 5/10/20 병렬 채점. 단일 기간만 필요하면 audit_recos().
        "audit": audit_multi(store, cfg=cfg),
        "momentum": score_momentum(week_scans),
        "persistence": persistence(recos, week_scans),
        "churn": churn(recos, week_days),
        "eta": band_eta(week_scans),
        "events": weekly_events(week_scans, week_days),
        "sector": sector_concentration(recos, meta, week_days),
        # 전월 회고는 매월 첫 금요일에만 붙인다. 매주 붙이면 같은 숫자를
        # 네 번 보게 되고, 그러면 읽지 않는다.
        "monthly": monthly_review(store, cfg=cfg) if want_monthly else None,
    }
