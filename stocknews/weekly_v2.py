# -*- coding: utf-8 -*-
"""[I] 주간 분석 — weekly 모드에서만 돈다. nightly 에 넣지 않는다.

플래그 `WEEKLY_ANALYSIS_V2`(기본 OFF) 또는 `--weekly-v2` 로만 실행된다.
**기록 전용 분석**이다. 점수·등급·추천 선정에 어떤 값도 돌려주지 않는다.

  1. 팩터 분해      시장·소형주·모멘텀 3요인을 DB 시세로 구성하고, [0] 이벤트
                    (실전 recos → assign_events) 의 20일 순수익을 '요인 몫' 과
                    '남는 초과수익' 으로 나눈다. 트랙(이벤트 슬롯)별·국면별.
  2. 허스트 지수    DFA-1, 최근 250 수익률. 250거래일 미만은 NULL.
                    indicator_log 에 name='hurst_dfa' 로 주 1회 기록.
  3. 지표 상관 행렬 indicator_log 의 지표 전체 + risk_metrics.sigma_ewma +
                    scans.obv_slope20. 날짜별 순위상관(스피어만) 평균. |ρ|≥0.8 표시.
  4. 5분위 표       지표값으로 전 종목 5등분 → 20일 시장(전종목 중위) 대비 초과
                    수익(ex_med_20, [0] score_events 정의). 국면별 · 소급/실시간 분리.
  5. NULL 비율      지표별.
  6. GARCH vs EWMA  20일 실현 변동성 적중 비교.
  7. 국면전환 모형  2상태 가우시안(해밀턴 필터 + EM). 현재 고변동 국면 확률을
                    C 의 규칙 라벨과 나란히 표시만 한다.

표본이 `MIN_SCORED_FOR_JUDGMENT`(20) 미만인 칸은 숫자 대신 "표본 부족".

외부 의존성은 numpy·pandas 뿐이다. GARCH MLE, 해밀턴 필터·EM, DFA 는 여기서
직접 구현하고 smoke(`smoke_ext/s80_weekly_v2.py`)가 합성 데이터로 알려진
파라미터를 복원하는지 검사한다.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from . import config as C
from . import events as ev

log = logging.getLogger(__name__)

__all__ = ["dfa_hurst", "hurst_at", "garch11_fit", "garch11_forecast",
           "ewma_sigma", "markov2_fit", "FactorModel", "build_factors",
           "factor_decompose", "weekly_sample_dates", "load_indicator_long",
           "quintile_table", "correlation_table", "null_ratio_table",
           "vol_forecast_compare", "regime_switch_now", "record_hurst",
           "hurst_backfill", "build_report", "render_report",
           "run_weekly_v2", "HURST_NAME", "NO_REGIME"]

HURST_NAME = "hurst_dfa"
NO_REGIME = "국면 없음"
KIND_BACK = "소급"
KIND_LIVE = "실시간"
ALL = "전체"


def _cfg(name: str, default):
    return getattr(C, name, default)


def _min_n() -> int:
    return int(C.MIN_SCORED_FOR_JUDGMENT)


def _table_exists(con: sqlite3.Connection, name: str) -> bool:
    return con.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                       "AND name=?", (name,)).fetchone() is not None


def _cols(con: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in con.execute(f"PRAGMA table_info({table})")}


# ══════════════════════════ DFA 허스트 ══════════════════════════
def _dfa_scales(T: int, n: int = 12, smin: int = 10) -> np.ndarray:
    smax = max(smin + 1, T // 4)
    s = np.unique(np.round(np.logspace(np.log10(smin), np.log10(smax), n))
                  .astype(int))
    return s[(s >= 4) & (s <= T // 2)]


def dfa_hurst(x, scales=None) -> np.ndarray | float:
    """DFA-1 허스트 지수. x 는 (T,) 또는 (T, N) **수익률**(증분) 행렬.

    프로필 Y = cumsum(x − 평균). 각 척도 s 마다 길이 s 구간(앞에서·뒤에서
    양방향)으로 자르고 구간마다 1차 추세를 빼 잔차 RMS F(s) 를 구한다.
    H = log F(s) 의 log s 에 대한 기울기. 백색잡음 ≈ 0.5, 지속성 > 0.5,
    반지속성 < 0.5. 열마다 독립 계산(벡터화). NaN 이 있는 열은 NaN.
    """
    X = np.asarray(x, dtype="float64")
    one = X.ndim == 1
    if one:
        X = X[:, None]
    T, N = X.shape
    out = np.full(N, np.nan)
    good = np.isfinite(X).all(axis=0)
    if T < 20 or not good.any():
        return float(out[0]) if one else out
    Xg = X[:, good]
    Y = np.cumsum(Xg - Xg.mean(axis=0), axis=0)
    sc = np.asarray(_dfa_scales(T) if scales is None else scales, dtype=int)
    logs, logF = [], []
    for s in sc:
        nseg = T // s
        if nseg < 2:
            continue
        t = np.arange(s, dtype="float64")
        A = np.column_stack([np.ones(s), t])
        R = np.eye(s) - A @ np.linalg.pinv(A)          # 추세 제거 투영
        fw = Y[:nseg * s].reshape(nseg, s, -1)
        bw = Y[T - nseg * s:].reshape(nseg, s, -1)
        Z = np.concatenate([fw, bw], axis=0)
        res = np.einsum("ij,kjn->kin", R, Z)
        F2 = (res ** 2).mean(axis=(0, 1))
        with np.errstate(divide="ignore"):
            logF.append(0.5 * np.log(F2))
        logs.append(np.log(s))
    if len(logs) < 3:
        return float(out[0]) if one else out
    L = np.asarray(logs)
    F = np.vstack(logF)                                  # (S, Ng)
    Lc = L - L.mean()
    slope = (Lc[:, None] * (F - F.mean(axis=0))).sum(axis=0) / (Lc ** 2).sum()
    slope[~np.isfinite(slope)] = np.nan
    out[good] = slope
    return float(out[0]) if one else out


def _log_returns_last(closes: np.ndarray, window: int) -> np.ndarray | None:
    """한 종목 종가 열에서 마지막 window 개의 로그수익률. 부족하면 None.

    정지일(NaN)은 건너뛰어 그 종목의 거래일끼리 잇는다. 가격제한폭(±30%)
    밖의 하루 변동은 권리 변동(분할·병합)의 흔적이라 0 으로 둔다 — 실제
    거래로 생길 수 없는 점프가 DFA 를 지배하지 않게.
    """
    v = closes[np.isfinite(closes) & (closes > 0)]
    if len(v) < window + 1:
        return None
    seg = v[-(window + 1):]
    r = np.diff(np.log(seg))
    lim = np.log(1.0 + C.PRICE_JUMP_BREAK_PCT / 100.0) + 1e-6
    lo = np.log(1.0 - C.PRICE_JUMP_BREAK_PCT / 100.0) - 1e-6
    r[(r > lim) | (r < lo)] = 0.0
    return r


def hurst_at(panel: ev.PricePanel, pos: int,
             window: int | None = None) -> pd.Series:
    """패널 위치 pos(그날 포함)까지의 종가로 종목별 허스트.

    그날 종가가 있는 종목만 대상이다. 250 수익률 미만이면 NaN(=NULL 기록).
    """
    w = int(window or _cfg("WEEKLY_V2_HURST_WINDOW", 250))
    Cm = panel._c[:pos + 1]
    cols = list(panel.close.columns)
    tick, mats, short = [], [], []
    for j, t in enumerate(cols):
        if not np.isfinite(Cm[pos, j]):
            continue
        r = _log_returns_last(Cm[:, j], w)
        if r is None:
            short.append(t)
        else:
            tick.append(t)
            mats.append(r)
    vals = {}
    if mats:
        H = dfa_hurst(np.column_stack(mats))
        vals.update(dict(zip(tick, np.atleast_1d(H))))
    vals.update({t: np.nan for t in short})
    return pd.Series(vals, dtype="float64").sort_index()


# ══════════════════════════ GARCH(1,1) ══════════════════════════
def _garch_ll(r2: np.ndarray, v0: np.ndarray, a: np.ndarray,
              b: np.ndarray) -> np.ndarray:
    """분산 목표(variance targeting) 가우시안 로그우도. r2 (T,N), a·b (G,N)."""
    T = r2.shape[0]
    om = v0 * (1.0 - a - b)
    h = np.broadcast_to(v0, a.shape).copy()
    quad = np.zeros(a.shape)
    logs = np.zeros(a.shape)
    prod = np.ones(a.shape)
    # Σ log h 는 h/v0 의 곱을 16개씩 모아 한 번에 log 한다(log 호출이 병목).
    for t in range(T):
        if t > 0:
            h = om + a * r2[t - 1] + b * h
        quad += r2[t] / h
        prod *= h / v0
        if t % 16 == 15:
            logs += np.log(prod)
            prod.fill(1.0)
    logs += np.log(prod)
    return -0.5 * (logs + T * np.log(v0) + quad)


def garch11_fit(r, iters: int = 40) -> dict:
    """GARCH(1,1) 준최우추정(VT-QMLE). r 은 (T,) 또는 (T,N) 수익률(소수).

    ω = σ̄²(1−α−β) 로 무조건부 분산을 표본 분산에 고정하고 (α, β) 를
    로그우도 최대화로 찾는다. 최적화는 전 열 동시: 거친 격자 → 3×3 패턴
    탐색(보폭 반감). scipy 를 쓰지 않는다.

    반환 {omega, alpha, beta, v0(표본분산), h_last(마지막 조건부분산),
          r_last, loglik} — 각 (N,) 배열.
    """
    R = np.asarray(r, dtype="float64")
    if R.ndim == 1:
        R = R[:, None]
    R = R - R.mean(axis=0)
    r2 = R ** 2
    v0 = r2.mean(axis=0)
    v0 = np.where(v0 > 0, v0, 1e-12)
    N = R.shape[1]
    # 거친 격자 (α, 지속성 p=α+β)
    ga = np.array([0.03, 0.07, 0.12, 0.20])
    gp = np.array([0.70, 0.88, 0.95, 0.98, 0.995])
    A, P = np.meshgrid(ga, gp, indexing="ij")
    A, P = A.ravel(), P.ravel()
    ok = P > A
    A, P = A[ok], P[ok]
    aa = np.repeat(A[:, None], N, axis=1)
    bb = np.repeat((P - A)[:, None], N, axis=1)
    ll = _garch_ll(r2, v0, aa, bb)
    k = np.argmax(ll, axis=0)
    ca, cp = A[k].copy(), P[k].copy()
    best = ll[k, np.arange(N)]
    sa = np.full(N, 0.03)
    sp = np.full(N, 0.03)
    da = np.array([-1, -1, -1, 0, 0, 1, 1, 1])
    dp = np.array([-1, 0, 1, -1, 1, -1, 0, 1])
    for _ in range(int(iters)):
        act = np.where(np.maximum(sa, sp) >= 1e-4)[0]     # 아직 수렴 안 한 열만
        if act.size == 0:
            break
        na = ca[None, act] + da[:, None] * sa[None, act]
        npp = cp[None, act] + dp[:, None] * sp[None, act]
        na = np.clip(na, 1e-4, 0.6)
        npp = np.clip(npp, na + 1e-4, 0.9995)
        lln = _garch_ll(r2[:, act], v0[act], na, npp - na)
        lln[~np.isfinite(lln)] = -np.inf
        kk = np.argmax(lln, axis=0)
        ar = np.arange(act.size)
        cand = lln[kk, ar]
        better = cand > best[act] + 1e-10
        ca[act] = np.where(better, na[kk, ar], ca[act])
        cp[act] = np.where(better, npp[kk, ar], cp[act])
        best[act] = np.where(better, cand, best[act])
        sa[act] = np.where(better, sa[act], sa[act] * 0.5)
        sp[act] = np.where(better, sp[act], sp[act] * 0.5)
    alpha, beta = ca, cp - ca
    om = v0 * (1.0 - alpha - beta)
    h = v0.copy()
    for t in range(1, R.shape[0]):
        h = om + alpha * r2[t - 1] + beta * h
    return {"omega": om, "alpha": alpha, "beta": beta, "v0": v0,
            "h_last": h, "r_last": R[-1], "loglik": best}


def garch11_forecast(fit: dict, horizon: int = 20) -> np.ndarray:
    """향후 horizon 일 평균 분산의 제곱근 = 일간 σ 예측 (N,)."""
    a, b, v0 = fit["alpha"], fit["beta"], fit["v0"]
    h1 = fit["omega"] + a * fit["r_last"] ** 2 + b * fit["h_last"]
    p = a + b
    k = np.arange(horizon)[:, None]
    hk = v0 + (p[None, :] ** k) * (h1 - v0)[None, :]
    return np.sqrt(hk.mean(axis=0))


def ewma_sigma(r, lam: float = 0.94, init: int = 20) -> np.ndarray:
    """EWMA σ (RiskMetrics). r (T,) 또는 (T,N). 마지막 관측 반영 후 다음 날 σ."""
    R = np.asarray(r, dtype="float64")
    if R.ndim == 1:
        R = R[:, None]
    k = max(1, min(init, R.shape[0]))
    v = (R[:k] ** 2).mean(axis=0)
    for t in range(R.shape[0]):
        v = lam * v + (1.0 - lam) * R[t] ** 2
    return np.sqrt(v)


# ══════════════════════════ 2상태 국면전환 ══════════════════════════
def _norm_pdf(x, mu, sd):
    return np.exp(-0.5 * ((x - mu) / sd) ** 2) / (sd * np.sqrt(2 * np.pi))


def markov2_fit(r, max_iter: int = 500, tol: float = 1e-9) -> dict:
    """2상태 가우시안 마르코프 국면전환 (해밀턴 필터 + 킴 평활 + EM).

    상태별 평균·분산이 다르다. 전이행렬 P[i,j] = P(s_t=j | s_{t-1}=i).
    상태 1 = 분산이 큰 쪽(고변동)으로 정렬해 돌려준다.

    반환 {mu(2,), sigma(2,), P(2,2), filtered(T,2), smoothed(T,2),
          loglik, iters, duration(2,) 기대 지속일}
    """
    x = np.asarray(r, dtype="float64")
    x = x[np.isfinite(x)]
    T = len(x)
    if T < 50:
        raise ValueError("표본 부족")
    sd0 = x.std() or 1e-6
    mu = np.array([x.mean(), x.mean()])
    sg = np.array([0.6 * sd0, 1.6 * sd0])
    P = np.array([[0.95, 0.05], [0.05, 0.95]])
    pi0 = np.array([0.5, 0.5])
    prev = -np.inf
    floor = 1e-4 * sd0
    for it in range(1, max_iter + 1):
        eta = np.column_stack([_norm_pdf(x, mu[k], sg[k]) for k in range(2)])
        eta = np.maximum(eta, 1e-300)
        filt = np.zeros((T, 2))
        pred = np.zeros((T, 2))
        ll = 0.0
        p = pi0
        for t in range(T):
            pr = p if t == 0 else filt[t - 1] @ P
            pred[t] = pr
            num = pr * eta[t]
            s = num.sum()
            ll += np.log(s)
            filt[t] = num / s
        sm = np.zeros((T, 2))
        sm[-1] = filt[-1]
        joint = np.zeros((2, 2))
        for t in range(T - 2, -1, -1):
            ratio = sm[t + 1] / np.maximum(pred[t + 1], 1e-300)
            sm[t] = filt[t] * (P @ ratio)
            sm[t] /= sm[t].sum()
            joint += filt[t][:, None] * P * ratio[None, :]
        # M 단계
        denom = sm[:-1].sum(axis=0)
        P = joint / np.maximum(denom[:, None], 1e-300)
        P = P / P.sum(axis=1, keepdims=True)
        w = sm.sum(axis=0)
        mu = (sm * x[:, None]).sum(axis=0) / w
        sg = np.sqrt((sm * (x[:, None] - mu) ** 2).sum(axis=0) / w)
        sg = np.maximum(sg, floor)
        pi0 = sm[0]
        if abs(ll - prev) < tol * max(1.0, abs(ll)):
            break
        prev = ll
    order = np.argsort(sg)                     # 0 = 저변동, 1 = 고변동
    mu, sg = mu[order], sg[order]
    P = P[np.ix_(order, order)]
    filt, sm = filt[:, order], sm[:, order]
    dur = 1.0 / np.maximum(1.0 - np.diag(P), 1e-12)
    return {"mu": mu, "sigma": sg, "P": P, "filtered": filt, "smoothed": sm,
            "loglik": float(ll), "iters": it, "duration": dur}


# ══════════════════════════ 3요인 ══════════════════════════
@dataclass
class FactorModel:
    """DB 시세로 구성한 일간 요인 수익률(소수).

    cc : 종가→종가.  oc : 당일 시가→종가 (진입일 하루치에만 쓴다).
    r_cc : 종목 일간 종가 수익률 행렬 (D, N). 불연속·결측은 NaN.
    """
    dates: list[str]
    cc: pd.DataFrame          # columns MKT, SMB, MOM, KOSPI
    oc: pd.DataFrame          # columns MKT, SMB, MOM
    r_cc: np.ndarray
    n_used: pd.Series


def build_factors(panel: ev.PricePanel, shares: dict[str, float],
                  markets: dict[str, str] | None = None,
                  q: float | None = None, mom_lb: int | None = None,
                  mkt_weight: str | None = None) -> FactorModel:
    """시장·소형주·모멘텀 요인.

    MKT  = 전 종목(KOSPI+KOSDAQ) 동일가중 평균 수익률 (기본). 'cap' 이면
           전일 시총가중. 기본을 동일가중으로 둔 이유: 시총가중은 초대형
           2~3종목이 지배해 일반 종목의 β_시장 이 0.1 안팎으로 나오고(2026-11
           실측), 공통 움직임이 SMB 로 새어 부호가 뒤집힌다. 대조군([0] 의
           ex_med = 전종목 중위)도 동일가중 성격이다.
    SMB  = 전일 시총 하위 q 평균 − 상위 q 평균 (동일가중)
    MOM  = 직전 60거래일 수익률 상위 q − 하위 q (동일가중). 신호는 전일까지.
    KOSPI= KOSPI 종목만의 시총가중 (국면전환 모형 입력용)

    시가총액 = 현재 상장주식수 × 그날 종가. 과거 주식수 이력이 DB 에 없어
    증자·감자 전 구간은 근사다. 가격제한폭 밖 하루 변동(수정주가 불연속)
    은 그날 수익률을 비운다.
    """
    q = float(q if q is not None else _cfg("WEEKLY_V2_FACTOR_QUANTILE", 0.3))
    lb = int(mom_lb if mom_lb is not None else _cfg("WEEKLY_V2_MOM_LOOKBACK", 60))
    cap_w = str(mkt_weight or _cfg("WEEKLY_V2_MKT_WEIGHT", "equal")) == "cap"
    cols = list(panel.close.columns)
    D, N = panel._c.shape
    Cm, Om = panel._c, panel._o
    sh = np.array([float(shares.get(t) or np.nan) for t in cols])
    mk = markets or {}
    kospi = np.array([mk.get(t, "") == "KOSPI" for t in cols])
    lim = C.PRICE_JUMP_BREAK_PCT / 100.0 + 1e-6
    with np.errstate(invalid="ignore", divide="ignore"):
        rcc = np.full((D, N), np.nan)
        rcc[1:] = Cm[1:] / Cm[:-1] - 1.0
        rcc[np.abs(rcc) > lim] = np.nan
        roc = Cm / Om - 1.0
        roc[np.abs(roc) > lim] = np.nan
        cap_prev = np.full((D, N), np.nan)
        cap_prev[1:] = Cm[:-1] * sh[None, :]
        mom = np.full((D, N), np.nan)
        if D > lb + 1:
            mom[lb + 1:] = Cm[lb:-1] / Cm[:-lb - 1] - 1.0
            # 신호 구간에 불연속이 있으면 비운다
            bcs = panel._break_cs
            nb = np.zeros((D, N))
            nb[lb + 1:] = bcs[lb:-1] - bcs[:-lb - 1]
            mom[nb > 0] = np.nan

    def _row(r, t):
        v = np.isfinite(r) & np.isfinite(cap_prev[t]) & (cap_prev[t] > 0)
        if v.sum() < 20:
            return np.nan, np.nan, np.nan, np.nan, int(v.sum())
        if cap_w:
            w = cap_prev[t][v]
            mkt = float((w * r[v]).sum() / w.sum())
        else:
            mkt = float(r[v].mean())
        kv = v & kospi
        kos = (float((cap_prev[t][kv] * r[kv]).sum() / cap_prev[t][kv].sum())
               if kv.sum() >= 20 else np.nan)
        cap = cap_prev[t][v]
        lo, hi = np.quantile(cap, [q, 1 - q])
        rv = r[v]
        smb = float(rv[cap <= lo].mean() - rv[cap >= hi].mean())
        mv = v & np.isfinite(mom[t])
        if mv.sum() >= 20:
            m = mom[t][mv]
            mlo, mhi = np.quantile(m, [q, 1 - q])
            rm = r[mv]
            wml = float(rm[m >= mhi].mean() - rm[m <= mlo].mean())
        else:
            wml = np.nan
        return mkt, smb, wml, kos, int(v.sum())

    cc_rows, oc_rows, nn = [], [], []
    for t in range(D):
        a = _row(rcc[t], t)
        b = _row(roc[t], t)
        cc_rows.append(a[:4])
        oc_rows.append(b[:3])
        nn.append(a[4])
    idx = list(panel.dates)
    cc = pd.DataFrame(cc_rows, index=idx, columns=["MKT", "SMB", "MOM", "KOSPI"])
    oc = pd.DataFrame(oc_rows, index=idx, columns=["MKT", "SMB", "MOM"])
    return FactorModel(idx, cc, oc, rcc, pd.Series(nn, index=idx))


FACTORS = ("MKT", "SMB", "MOM")
FACTOR_KO = {"MKT": "시장", "SMB": "소형주", "MOM": "모멘텀"}


def factor_decompose(scored: pd.DataFrame, panel: ev.PricePanel,
                     fm: FactorModel, horizon: int = 20,
                     beta_window: int | None = None,
                     min_obs: int | None = None) -> pd.DataFrame:
    """채점된 이벤트마다 순수익 = Σ β_k·F_k(보유구간) + 남는 초과수익.

    β 는 이벤트 날짜(그날 포함)까지 직전 beta_window 거래일 일간 수익률을
    [1, MKT, SMB, MOM] 에 회귀해 구한다(미래 데이터 없음). 보유구간 요인
    수익률 = 진입일 시가→종가 요인 + 이후 h−1 일 종가→종가 요인의 합
    (score_events 의 '익일 시가 진입 → h일째 종가' 와 같은 구간).

    남는 초과수익 = net_h − 요인 몫. net_h 는 왕복 비용 차감이므로 남는
    초과수익도 비용 차감 기준이다.
    """
    bw = int(beta_window or _cfg("WEEKLY_V2_BETA_WINDOW", 120))
    mo = int(min_obs or _cfg("WEEKLY_V2_BETA_MIN_OBS", 60))
    h = int(horizon)
    net_col = f"net_{h}"
    if scored is None or scored.empty or net_col not in scored.columns:
        return pd.DataFrame()
    Fcc = fm.cc[list(FACTORS)].to_numpy()
    Foc = fm.oc[list(FACTORS)].to_numpy()
    rows = []
    for r in scored.to_dict("records"):
        net = r.get(net_col)
        rec = {k: r.get(k) for k in ("ticker", "event_date", "event_slot",
                                     "event_grade", "regime", net_col,
                                     f"ex_med_{h}")}
        rec.update({"beta_ok": False, "factor_part": np.nan,
                    "residual": np.nan})
        for f in FACTORS:
            rec[f"b_{f}"] = np.nan
            rec[f"part_{f}"] = np.nan
        if net is None or not np.isfinite(net):
            rows.append(rec)
            continue
        ed = ev._ds(r.get("event_date"))
        p = panel.pos.get(ed)
        j = panel.col(str(r.get("ticker")))
        if p is None or j is None:
            rows.append(rec)
            continue
        e, x = p + 1, p + h
        a = max(1, p - bw + 1)
        y = fm.r_cc[a:p + 1, j]
        X = Fcc[a:p + 1]
        m = np.isfinite(y) & np.isfinite(X).all(axis=1)
        Fw = Foc[e] + np.nansum(Fcc[e + 1:x + 1], axis=0) if x < len(Fcc) else None
        if m.sum() < mo or Fw is None or not np.isfinite(Fw).all():
            rows.append(rec)
            continue
        A = np.column_stack([np.ones(m.sum()), X[m]])
        b, *_ = np.linalg.lstsq(A, y[m], rcond=None)
        parts = b[1:] * Fw * 100.0              # % 단위 (net 과 같게)
        rec["beta_ok"] = True
        for k, f in enumerate(FACTORS):
            rec[f"b_{f}"] = float(b[1 + k])
            rec[f"part_{f}"] = float(parts[k])
        rec["factor_part"] = float(parts.sum())
        rec["residual"] = float(net - parts.sum())
        rows.append(rec)
    return pd.DataFrame(rows)


# ══════════════════════════ 데이터 적재 ══════════════════════════
def weekly_sample_dates(calendar: list[str]) -> list[str]:
    """달력의 주별 마지막 거래일(보통 금요일)."""
    if not calendar:
        return []
    s = pd.Series(pd.to_datetime(calendar), index=calendar)
    iso = s.dt.isocalendar()
    key = iso["year"].astype(str) + "-" + iso["week"].astype(str)
    return [str(x) for x in s.groupby(key.values).apply(lambda g: g.index[-1])
            .sort_values().tolist()]


def load_indicator_long(store, dates: list[str] | None = None) -> tuple[pd.DataFrame, dict]:
    """지표 값을 long 형식(d, ticker, name, value, kind)으로.

    출처 셋: indicator_log(H, 이름별 최신 calc_version) · risk_metrics(E,
    sigma_ewma) · scans.obv_slope20(항상 '실시간'). 테이블이 없으면 빠지고
    meta 에 '없음' 으로 남는다. dates 가 None 이면 전 기간.
    """
    meta = {"indicator_log": False, "risk_metrics": False, "scans": False}
    frames = []
    with closing(sqlite3.connect(store.path, timeout=30.0)) as con:
        def _where(col="d"):
            if dates is None:
                return "", ()
            ph = ",".join("?" * len(dates))
            return f" WHERE {col} IN ({ph})", tuple(dates)

        if _table_exists(con, "indicator_log"):
            meta["indicator_log"] = True
            w, p = _where()
            df = pd.read_sql_query(
                "SELECT d, ticker, name, value, calc_version, kind "
                f"FROM indicator_log{w}", con, params=p)
            if not df.empty:
                mx = df.groupby("name")["calc_version"].transform("max")
                df = df[df["calc_version"] == mx]
                frames.append(df[["d", "ticker", "name", "value", "kind"]])
        if _table_exists(con, "risk_metrics"):
            meta["risk_metrics"] = True
            have = _cols(con, "risk_metrics")
            w, p = _where()
            kind = "kind" if "kind" in have else f"'{KIND_LIVE}'"
            cv = "calc_version" if "calc_version" in have else "1"
            df = pd.read_sql_query(
                f"SELECT d, ticker, sigma_ewma AS value, {kind} AS kind, "
                f"{cv} AS calc_version FROM risk_metrics{w}", con, params=p)
            if not df.empty:
                df = df[df["calc_version"] == df["calc_version"].max()]
                df["name"] = "sigma_ewma"
                frames.append(df[["d", "ticker", "name", "value", "kind"]])
        if _table_exists(con, "scans") and "obv_slope20" in _cols(con, "scans"):
            meta["scans"] = True
            w, p = _where()
            df = pd.read_sql_query(
                f"SELECT d, ticker, obv_slope20 AS value FROM scans{w}",
                con, params=p)
            if not df.empty:
                df["name"] = "obv_slope20"
                df["kind"] = KIND_LIVE
                frames.append(df[["d", "ticker", "name", "value", "kind"]])
    if not frames:
        return pd.DataFrame(columns=["d", "ticker", "name", "value", "kind"]), meta
    out = pd.concat(frames, ignore_index=True)
    out["d"] = out["d"].astype(str).str[:10]
    out["ticker"] = out["ticker"].astype(str)
    out["value"] = pd.to_numeric(out["value"], errors="coerce")
    return out, meta


def _regimes(store, dates) -> dict[str, str]:
    from . import v2_hooks
    out = {}
    for d in sorted(set(dates)):
        try:
            lab = v2_hooks.regime_on(store, d)
        except Exception as exc:  # noqa: BLE001
            log.warning("regime_on(%s) 실패: %s", d, exc)
            lab = None
        out[d] = lab or NO_REGIME
    return out


# ══════════════════════════ 5분위 ══════════════════════════
def quintile_table(values: pd.DataFrame, fwd: pd.DataFrame,
                   regimes: dict[str, str],
                   min_tickers: int | None = None) -> pd.DataFrame:
    """지표별 5분위 → ex_med_20 평균·중앙·표본 수. 국면 × 소급/실시간.

    values : long (d, ticker, name, value, kind)
    fwd    : (d, ticker, fwd) — 가상 이벤트 채점 결과(ex_med_20)
    분위는 **날짜마다** 그날 값이 있는 종목끼리 나눈다(시점 간 수준 차이가
    분위를 오염시키지 않게). 그날 종목이 min_tickers 미만이면 그날은 뺀다.

    반환 컬럼: name, kind, regime, q(1~5), n, mean, median
    """
    mt = int(min_tickers or _cfg("WEEKLY_V2_QUINTILE_MIN_TICKERS", 50))
    if values is None or values.empty or fwd is None or fwd.empty:
        return pd.DataFrame(columns=["name", "kind", "regime", "q", "n",
                                     "mean", "median"])
    v = values.dropna(subset=["value"]).merge(fwd, on=["d", "ticker"],
                                              how="inner")
    v = v[np.isfinite(v["fwd"])]
    if v.empty:
        return pd.DataFrame(columns=["name", "kind", "regime", "q", "n",
                                     "mean", "median"])
    g = v.groupby(["name", "kind", "d"])
    cnt = g["value"].transform("size")
    v = v[cnt >= mt].copy()
    if v.empty:
        return pd.DataFrame(columns=["name", "kind", "regime", "q", "n",
                                     "mean", "median"])
    pct = v.groupby(["name", "kind", "d"])["value"].rank(method="first", pct=True)
    v["q"] = np.minimum(np.ceil(pct * 5).astype(int), 5)
    v["regime"] = v["d"].map(regimes).fillna(NO_REGIME)
    both = pd.concat([v, v.assign(regime=ALL)], ignore_index=True)
    agg = (both.groupby(["name", "kind", "regime", "q"])["fwd"]
           .agg(n="size", mean="mean", median="median").reset_index())
    agg = agg.drop_duplicates(["name", "kind", "regime", "q"])
    return agg


def _virtual_forward(store, panel, markets, pairs: pd.DataFrame,
                     horizon: int) -> pd.DataFrame:
    """(d, ticker) 를 가상 이벤트로 보고 [0] score_events 로 채점 → ex_med_h."""
    if pairs.empty:
        return pd.DataFrame(columns=["d", "ticker", "fwd"])
    evs = pairs.rename(columns={"d": "event_date"})[["ticker", "event_date"]]
    sc = ev.score_events(evs, panel, None, markets, horizons=(horizon,))
    if sc.empty:
        return pd.DataFrame(columns=["d", "ticker", "fwd"])
    out = pd.DataFrame({"d": sc["event_date"].astype(str).str[:10],
                        "ticker": sc["ticker"].astype(str),
                        "fwd": pd.to_numeric(sc[f"ex_med_{horizon}"],
                                             errors="coerce")})
    return out


# ══════════════════════════ 상관 ══════════════════════════
def correlation_table(values: pd.DataFrame, min_pairs: int = 30
                      ) -> tuple[pd.DataFrame, pd.DataFrame]:
    """날짜별 스피어만 순위상관(쌍별 결측 제외)의 평균. (행렬, 날짜 수 행렬)."""
    if values is None or values.empty:
        return pd.DataFrame(), pd.DataFrame()
    v = values.dropna(subset=["value"])
    names = sorted(v["name"].unique())
    acc = pd.DataFrame(0.0, index=names, columns=names)
    cnt = pd.DataFrame(0, index=names, columns=names)
    for _, g in v.groupby("d"):
        w = g.pivot_table(index="ticker", columns="name", values="value",
                          aggfunc="first")
        if w.shape[1] < 2:
            continue
        c = w.rank().corr(min_periods=min_pairs)
        c = c.reindex(index=names, columns=names)
        m = c.notna()
        acc = acc + c.fillna(0.0)
        cnt = cnt + m.astype(int)
    mean = acc / cnt.where(cnt > 0)
    return mean, cnt


# ══════════════════════════ NULL 비율 ══════════════════════════
def null_ratio_table(store, values_latest: pd.DataFrame,
                     priced: dict[str, int]) -> pd.DataFrame:
    """지표별 NULL 비율. values_latest 는 이름별 '마지막 기록일' 의 행.

    rows_null_pct  = 기록된 행 중 값이 NULL 인 비율
    vs_priced_pct  = 그날 시세가 있는 종목 대비 값이 없는(행 없음 + NULL) 비율
    """
    out = []
    if values_latest is None or values_latest.empty:
        return pd.DataFrame(columns=["name", "d", "rows", "rows_null_pct",
                                     "priced", "vs_priced_pct"])
    for (name, d), g in values_latest.groupby(["name", "d"]):
        rows = len(g)
        nnull = int(g["value"].isna().sum())
        pr = int(priced.get(d, 0))
        have = int(g["value"].notna().sum())
        out.append({"name": name, "d": d, "rows": rows,
                    "rows_null_pct": nnull / rows * 100.0 if rows else np.nan,
                    "priced": pr,
                    "vs_priced_pct": (1 - have / pr) * 100.0 if pr else np.nan})
    return pd.DataFrame(out).sort_values("name").reset_index(drop=True)


# ══════════════════════════ GARCH vs EWMA ══════════════════════════
def _ewma_lambda() -> tuple[float, str]:
    for nm in ("RISK_EWMA_LAMBDA", "EWMA_LAMBDA", "SIGMA_EWMA_LAMBDA",
               "E_EWMA_LAMBDA", "RISK_LAMBDA"):
        val = getattr(C, nm, None)
        if isinstance(val, (int, float)) and 0 < float(val) < 1:
            return float(val), f"config.{nm}"
    return float(_cfg("WEEKLY_V2_EWMA_LAMBDA", 0.94)), "I 기본값(E 구역 값 없음)"


def _loss_row(pred: np.ndarray, real: np.ndarray, other: np.ndarray | None
              ) -> dict:
    m = np.isfinite(pred) & np.isfinite(real) & (pred > 0) & (real > 0)
    if other is not None:
        m &= np.isfinite(other)
    n = int(m.sum())
    if n == 0:
        return {"n": 0}
    p, rv = pred[m] * 100.0, real[m] * 100.0
    err = p - rv
    ql = (rv ** 2) / (p ** 2) - np.log((rv ** 2) / (p ** 2)) - 1.0
    hit_tol = float(_cfg("WEEKLY_V2_VOL_HIT_TOL", 0.25))
    hit = np.abs(err) <= hit_tol * rv
    row = {"n": n, "mae": float(np.abs(err).mean()),
           "rmse": float(np.sqrt((err ** 2).mean())),
           "bias": float(err.mean()), "qlike": float(ql.mean()),
           "hit": float(hit.mean() * 100.0)}
    if other is not None:
        o = other[m] * 100.0
        row["closer"] = float((np.abs(err) < np.abs(o - rv)).mean() * 100.0)
    return row


def vol_forecast_compare(store, panel: ev.PricePanel, tickers: list[str],
                         horizon: int = 20, step: int | None = None,
                         window: int | None = None,
                         max_origins: int | None = None) -> dict:
    """원점마다 GARCH(1,1) 와 EWMA 의 '향후 20일 일간 σ' 예측을 실현값과 비교.

    원점 = step 거래일 간격(겹치지 않는 20일 실현 구간). GARCH 는 원점까지
    window 수익률로 매번 다시 적합한다. EWMA 는 E 의 risk_metrics 기록값이
    있으면 그것, 없으면 같은 λ 로 여기서 계산(→ 'E 기록값 아님' 표기).
    실현 σ = 원점 다음 날부터 20일 일간 수익률의 RMS (평균 0 가정).
    """
    h = int(horizon)
    step = int(step or _cfg("WEEKLY_V2_GARCH_STEP", 20))
    win = int(window or _cfg("WEEKLY_V2_GARCH_WINDOW", 500))
    mino = int(_cfg("WEEKLY_V2_GARCH_MIN_OBS", 250))
    lam, lam_src = _ewma_lambda()
    cols = [t for t in tickers if panel.col(t) is not None]
    out = {"lambda": lam, "lambda_src": lam_src, "origins": [], "n_tickers": 0,
           "garch": {"n": 0}, "ewma_e": {"n": 0}, "ewma_self": {"n": 0},
           "ewma_e_n": 0, "ewma_self_n": 0, "params": None}
    if not cols:
        return out
    J = np.array([panel.col(t) for t in cols])
    D = len(panel.dates)
    lim = C.PRICE_JUMP_BREAK_PCT / 100.0 + 1e-6
    with np.errstate(invalid="ignore", divide="ignore"):
        R = np.full((D, len(J)), np.nan)
        R[1:] = panel._c[1:, J] / panel._c[:-1, J] - 1.0
    R[np.abs(R) > lim] = np.nan
    last = D - 1 - h                     # 실현 구간이 끝까지 있는 마지막 원점
    origins = list(range(last, mino, -step))[::-1]
    if max_origins:
        origins = origins[-int(max_origins):]
    # E 기록값
    e_sig = {}
    with closing(sqlite3.connect(store.path, timeout=30.0)) as con:
        if _table_exists(con, "risk_metrics") and origins:
            ds = [panel.dates[o] for o in origins]
            ph = ",".join("?" * len(ds))
            for d, t, s in con.execute(
                    f"SELECT d, ticker, sigma_ewma FROM risk_metrics "
                    f"WHERE d IN ({ph})", ds):
                if s is not None:
                    e_sig[(str(d)[:10], str(t))] = float(s)
    # 원점별 표본을 모은 뒤 창 길이가 같은 것끼리 열로 쌓아 한 번에 적합한다
    # (원점마다 따로 적합하면 파이썬 루프가 원점 수만큼 늘어난다).
    groups: dict[int, list] = {}
    for o in origins:
        a = max(1, o - win + 1)
        if (o + 1 - a) < mino:
            continue
        Rw = R[a:o + 1]
        fut = R[o + 1:o + 1 + h]
        miss = np.isnan(Rw).mean(axis=0)
        ok = (miss <= 0.05) & (np.isfinite(fut).sum(axis=0) >= h - 1)
        if ok.sum() == 0:
            continue
        d = panel.dates[o]
        tk = [cols[i] for i in np.where(ok)[0]]
        groups.setdefault(Rw.shape[0], []).append({
            "d": d, "Rk": np.nan_to_num(Rw[:, ok], nan=0.0),
            # 자체 EWMA: 원점까지 그 종목 전체 이력
            "ps": ewma_sigma(np.nan_to_num(R[1:o + 1][:, ok], nan=0.0), lam).ravel(),
            "real": np.sqrt(np.nanmean(fut[:, ok] ** 2, axis=0)),
            "pe": np.array([e_sig.get((d, t), np.nan) for t in tk])})
        out["origins"].append(d)
    out["origins"].sort()
    P_g, P_e, P_s, REAL = [], [], [], []
    pars = []
    for _, items in sorted(groups.items()):
        fit = garch11_fit(np.hstack([it["Rk"] for it in items]))
        P_g.append(garch11_forecast(fit, h))
        P_e.extend(it["pe"] for it in items)
        P_s.extend(it["ps"] for it in items)
        REAL.extend(it["real"] for it in items)
        pars.append(np.column_stack([fit["alpha"], fit["beta"]]))
    if not P_g:
        return out
    g, e, s, rv = (np.concatenate(x) for x in (P_g, P_e, P_s, REAL))
    use_e = np.isfinite(e)
    # EWMA 는 E 기록값 우선, 없으면 자체 계산값. 둘을 섞지 않고 따로 집계.
    out["n_tickers"] = len(cols)
    out["ewma_e_n"] = int(use_e.sum())
    out["ewma_self_n"] = int((~use_e).sum())
    out["garch"] = _loss_row(g, rv, None)
    out["garch_vs_self"] = _loss_row(g, rv, s)
    out["ewma_self"] = _loss_row(s, rv, g)
    if use_e.any():
        out["ewma_e"] = _loss_row(e[use_e], rv[use_e], g[use_e])
        out["garch_on_e"] = _loss_row(g[use_e], rv[use_e], e[use_e])
    pp = np.vstack(pars)
    out["params"] = {"alpha_med": float(np.median(pp[:, 0])),
                     "beta_med": float(np.median(pp[:, 1])),
                     "persist_med": float(np.median(pp.sum(axis=1)))}
    return out


# ══════════════════════════ 국면전환 ══════════════════════════
def regime_switch_now(store, fm: FactorModel, show_dates: list[str]) -> dict:
    """KOSPI(DB 구성, 시총가중) 일간 수익률에 2상태 모형 → 현재 고변동 확률."""
    x = fm.cc["KOSPI"]
    x = x[np.isfinite(x)]
    if len(x) < 120:
        return {"ok": False, "reason": f"표본 부족 (n={len(x)})"}
    try:
        res = markov2_fit(x.to_numpy())
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}
    filt = pd.Series(res["filtered"][:, 1], index=x.index)
    smooth = pd.Series(res["smoothed"][:, 1], index=x.index)
    reg = _regimes(store, show_dates)
    rows = [{"d": d, "p_high": float(filt.get(d, np.nan)),
             "p_high_smooth": float(smooth.get(d, np.nan)),
             "rule": reg.get(d, NO_REGIME)} for d in show_dates]
    return {"ok": True, "n": int(len(x)), "first": x.index[0],
            "last": x.index[-1], "mu": res["mu"].tolist(),
            "sigma": res["sigma"].tolist(), "P": res["P"].tolist(),
            "duration": res["duration"].tolist(), "iters": res["iters"],
            "p_high_now": float(filt.iloc[-1]), "rows": rows}


# ══════════════════════════ 허스트 기록 ══════════════════════════
def record_hurst(store, d: str, values: pd.Series, kind: str,
                 replace: bool) -> dict:
    """indicator_log 에 hurst_dfa 를 기록. 테이블이 없으면 기록하지 않는다.

    DDL 은 H 묶음이 만든다. 여기서 만들지 않는다.
    """
    cv = int(_cfg("WEEKLY_V2_HURST_CALC_VERSION", 1))
    from .trading_day import now_kst
    created = now_kst().isoformat(timespec="seconds")
    with closing(sqlite3.connect(store.path, timeout=30.0)) as con:
        if not _table_exists(con, "indicator_log"):
            return {"written": 0, "reason": "indicator_log 없음"}
        verb = "INSERT OR REPLACE" if replace else "INSERT OR IGNORE"
        rows = [(d, str(t), HURST_NAME,
                 None if not np.isfinite(v) else float(v), cv, kind, created)
                for t, v in values.items()]
        before = con.total_changes
        con.executemany(
            f"{verb} INTO indicator_log (d, ticker, name, value, calc_version,"
            " kind, created) VALUES (?,?,?,?,?,?,?)", rows)
        con.commit()
        return {"written": con.total_changes - before, "rows": len(rows)}


def hurst_backfill(store, panel: ev.PricePanel | None = None,
                   dates: list[str] | None = None) -> dict:
    """주별 표본일마다 허스트를 소급 계산해 kind='소급' 으로 기록(덮어쓰지 않음).

    1회성 명령(`--mode hurst-backfill`). nightly·weekly 와 분리한다.
    """
    t0 = time.time()
    panel = panel or ev.PricePanel.from_store(store)
    cal = panel.dates
    w = int(_cfg("WEEKLY_V2_HURST_WINDOW", 250))
    ds = dates or [d for d in weekly_sample_dates(cal)
                   if panel.pos[d] >= w]
    if cal:
        ds = [d for d in ds if d != cal[-1]]   # 최신일은 weekly 가 '실시간' 으로
    tot = {"dates": 0, "written": 0, "rows": 0, "non_null": 0}
    for d in ds:
        vals = hurst_at(panel, panel.pos[d], w)
        r = record_hurst(store, d, vals, KIND_BACK, replace=False)
        if r.get("reason"):
            tot["reason"] = r["reason"]
            break
        tot["dates"] += 1
        tot["written"] += r["written"]
        tot["rows"] += r["rows"]
        tot["non_null"] += int(vals.notna().sum())
    tot["first"] = ds[0] if ds else None
    tot["last"] = ds[-1] if ds else None
    tot["elapsed_sec"] = round(time.time() - t0, 1)
    return tot


# ══════════════════════════ 리포트 ══════════════════════════
def _read_recos(store) -> pd.DataFrame:
    with closing(sqlite3.connect(store.path, timeout=30.0)) as con:
        return pd.read_sql_query(
            "SELECT d, rank, ticker, name, slot, grade FROM recos", con)


def _shares(store) -> dict[str, float]:
    with closing(sqlite3.connect(store.path, timeout=30.0)) as con:
        rows = con.execute("SELECT ticker, shares, market_cap FROM tickers"
                           ).fetchall()
    return {str(t): (s if s else np.nan) for t, s, _ in rows}


def _priced_counts(panel: ev.PricePanel, dates) -> dict[str, int]:
    out = {}
    for d in set(dates):
        p = panel.pos.get(d)
        if p is not None:
            out[d] = int(np.isfinite(panel._c[p]).sum())
    return out


def build_report(store, asof: str | None = None, record: bool = True,
                 lock_dir: str | Path = "data/locks",
                 no_lock: bool = False) -> dict:
    """주간 분석 재료 일괄 생성. 단계별 소요(초)를 timings 에 남긴다."""
    tm: dict[str, float] = {}
    t0 = time.time()
    h = int(_cfg("WEEKLY_V2_HORIZON", 20))
    cal = ev.trading_calendar(store)
    if asof:
        cal = [d for d in cal if d <= asof]
    rep: dict = {"asof": cal[-1] if cal else None, "horizon": h,
                 "timings": tm, "min_n": _min_n()}
    if len(cal) < 30:
        rep["error"] = "시세 달력이 짧음"
        return rep
    panel = ev.PricePanel.from_store(store, calendar=cal)
    markets = ev.ticker_markets(store)
    tm["load_prices"] = time.time() - t0

    # 1. 팩터
    t = time.time()
    fm = build_factors(panel, _shares(store), markets)
    recos = _read_recos(store)
    recos = recos[recos["d"].astype(str).str[:10] <= cal[-1]]
    evs = ev.events_only(ev.assign_events(recos, cal))
    if evs is not None and not evs.empty:
        sc = ev.score_events(evs, panel, None, markets, horizons=(h,))
        reg = _regimes(store, sc["event_date"].astype(str).str[:10])
        sc["regime"] = sc["event_date"].astype(str).str[:10].map(reg)
        dec = factor_decompose(sc, panel, fm, h)
    else:
        sc, dec = pd.DataFrame(), pd.DataFrame()
    n_scored = (int(pd.to_numeric(dec[f"net_{h}"], errors="coerce").notna().sum())
                if not dec.empty else 0)
    rep["factor"] = {"events": int(len(evs)) if evs is not None else 0,
                     "scored": n_scored,
                     "decomposed": int(dec["beta_ok"].sum()) if not dec.empty else 0,
                     "detail": dec,
                     "factor_days": int(fm.cc["MKT"].notna().sum()),
                     "factor_mean": {f: float(fm.cc[f].mean() * 100) for f in FACTORS}}
    tm["factor"] = time.time() - t

    # 2. 허스트 (최신일)
    t = time.time()
    w = int(_cfg("WEEKLY_V2_HURST_WINDOW", 250))
    hv = hurst_at(panel, len(cal) - 1, w)
    rep["hurst"] = {"d": cal[-1], "n": int(len(hv)),
                    "non_null": int(hv.notna().sum()),
                    "median": float(hv.median()) if hv.notna().any() else None,
                    "p10": float(hv.quantile(0.1)) if hv.notna().any() else None,
                    "p90": float(hv.quantile(0.9)) if hv.notna().any() else None,
                    "record": {"written": 0, "reason": "기록 안 함"}}
    if record:
        rep["hurst"]["record"] = _record_live(store, cal[-1], hv, lock_dir,
                                              no_lock)
    tm["hurst"] = time.time() - t

    # 3·4·5 지표 데이터
    t = time.time()
    samples = weekly_sample_dates(cal)
    done_samples = [d for d in samples if panel.pos[d] + h < len(cal)]
    corr_weeks = int(_cfg("WEEKLY_V2_CORR_WEEKS", 26))
    corr_dates = samples[-corr_weeks:]
    want = sorted(set(done_samples) | set(corr_dates))
    vals, meta = load_indicator_long(store, want)
    rep["sources"] = meta
    tm["load_indicators"] = time.time() - t

    t = time.time()
    cvals = vals[vals["d"].isin(set(corr_dates))]
    corr, ccount = correlation_table(cvals)
    rep["corr"] = {"matrix": corr, "count": ccount,
                   "dates": len(set(cvals["d"])) if not cvals.empty else 0,
                   "flag": float(_cfg("WEEKLY_V2_CORR_FLAG", 0.8))}
    tm["corr"] = time.time() - t

    t = time.time()
    qv = vals[vals["d"].isin(set(done_samples))]
    pairs = qv.dropna(subset=["value"])[["d", "ticker"]].drop_duplicates()
    fwd = _virtual_forward(store, panel, markets, pairs, h)
    regs = _regimes(store, qv["d"].unique()) if not qv.empty else {}
    qt = quintile_table(qv, fwd, regs)
    rep["quintile"] = {"table": qt, "virtual_events": int(len(pairs)),
                       "scored": int(np.isfinite(fwd["fwd"]).sum())
                       if not fwd.empty else 0,
                       "dates": sorted(set(qv["d"])) if not qv.empty else []}
    tm["quintile"] = time.time() - t

    # 5. NULL 비율 — 이름별 마지막 기록일 (전 기간에서 찾는다)
    t = time.time()
    latest = _latest_rows(store)
    rep["nulls"] = null_ratio_table(store, latest,
                                    _priced_counts(panel, latest["d"].unique()
                                                   if not latest.empty else []))
    tm["nulls"] = time.time() - t

    # 6. GARCH vs EWMA
    t = time.time()
    with closing(sqlite3.connect(store.path, timeout=30.0)) as con:
        last_scan = con.execute("SELECT max(d) FROM scans").fetchone()[0]
        tick = [r[0] for r in con.execute(
            "SELECT ticker FROM scans WHERE d=?", (last_scan,))] if last_scan else []
    if not tick:
        tick = list(panel.close.columns)
    rep["vol"] = vol_forecast_compare(store, panel, tick, h)
    tm["vol"] = time.time() - t

    # 7. 국면전환
    t = time.time()
    rep["switch"] = regime_switch_now(store, fm, samples[-6:])
    tm["switch"] = time.time() - t
    tm["total"] = time.time() - t0
    return rep


def _latest_rows(store) -> pd.DataFrame:
    """지표 이름별 마지막 기록일의 행 (d, ticker, name, value, kind)."""
    frames = []
    with closing(sqlite3.connect(store.path, timeout=30.0)) as con:
        if _table_exists(con, "indicator_log"):
            # 전 테이블 GROUP BY 는 H 가 소급을 채우면 수백만 행 풀스캔이다.
            # PK 가 d 로 시작하므로 최근 기록일 15개만 보고 이름별 마지막 날을 찾는다.
            recent = [r[0] for r in con.execute(
                "SELECT DISTINCT d FROM indicator_log ORDER BY d DESC LIMIT 15")]
            last = []
            if recent:
                ph = ",".join("?" * len(recent))
                last = con.execute(f"SELECT name, max(d) FROM indicator_log "
                                   f"WHERE d IN ({ph}) GROUP BY name",
                                   recent).fetchall()
            for name, d in last:
                frames.append(pd.read_sql_query(
                    "SELECT d, ticker, name, value, kind FROM indicator_log "
                    "WHERE name=? AND d=? AND calc_version=(SELECT max("
                    "calc_version) FROM indicator_log WHERE name=?)",
                    con, params=(name, d, name)))
        if _table_exists(con, "risk_metrics"):
            d = con.execute("SELECT max(d) FROM risk_metrics").fetchone()[0]
            if d:
                f = pd.read_sql_query(
                    "SELECT d, ticker, sigma_ewma AS value FROM risk_metrics "
                    "WHERE d=?", con, params=(d,))
                f["name"] = "sigma_ewma"
                frames.append(f)
        if _table_exists(con, "scans") and "obv_slope20" in _cols(con, "scans"):
            d = con.execute("SELECT max(d) FROM scans").fetchone()[0]
            if d:
                f = pd.read_sql_query(
                    "SELECT d, ticker, obv_slope20 AS value FROM scans "
                    "WHERE d=?", con, params=(d,))
                f["name"] = "obv_slope20"
                frames.append(f)
    if not frames:
        return pd.DataFrame(columns=["d", "ticker", "name", "value"])
    out = pd.concat(frames, ignore_index=True)
    out["d"] = out["d"].astype(str).str[:10]
    out["value"] = pd.to_numeric(out["value"], errors="coerce")
    return out


def _record_live(store, d, hv, lock_dir, no_lock) -> dict:
    """weekly 의 허스트 기록. weekly 는 읽기 모드라 평소 락을 잡지 않는다.

    INSERT 순간에만 DB 락을 **기다리지 않고** 시도한다. 다른 쓰기 잡이
    돌고 있으면 기록을 건너뛰고 사유를 남긴다 — 주간 분석 때문에
    nightly 나 저녁 브리핑이 rc=3 으로 비키는 일이 없게.
    """
    if no_lock:
        return record_hurst(store, d, hv, KIND_LIVE, replace=True)
    from .joblock import JobLock
    lk = JobLock(mode="weekly", lock_dir=lock_dir, wait_seconds=0, timeout=300)
    if not lk.acquire():
        return {"written": 0, "reason": f"DB 락 점유 중({lk.holder.get('mode')})"
                                        " — 기록 건너뜀"}
    try:
        return record_hurst(store, d, hv, KIND_LIVE, replace=True)
    finally:
        lk.release()


# ── 렌더 ──
_ABBR = {"rs20": "rs20", "rs60": "rs60", "rs20_pct": "rs20p", "rs60_pct": "rs60p",
         "dist_52w_high": "d52h", "mfi14": "mfi", "atr14_pct": "atr%",
         "bb_width20": "bbw", "kalman_slope": "kal", HURST_NAME: "hurst",
         "sigma_ewma": "sigma", "obv_slope20": "obv"}


def _ab(n: str) -> str:
    return _ABBR.get(n, n[:6])


def _f(x, nd=2, sign=True) -> str:
    if x is None or not np.isfinite(x):
        return "—"
    return f"{x:+.{nd}f}" if sign else f"{x:.{nd}f}"


def _factor_lines(rep: dict) -> list[str]:
    f = rep.get("factor") or {}
    dec = f.get("detail")
    h = rep["horizon"]
    out = [f"■ 1. 팩터 분해 — 추천 성과 = 요인 몫 + 남는 초과수익 ({h}거래일)",
           f"  [0] 이벤트 {f.get('events', 0)}건 · 채점 완료 "
           f"{f.get('scored', 0)} · β 추정 가능 {f.get('decomposed', 0)}",
           f"  요인: 시장({'시총가중' if _cfg('WEEKLY_V2_MKT_WEIGHT', 'equal') == 'cap' else '동일가중'}) · 소형주(시총 하위30−상위30) · "
           "모멘텀(60일 상위30−하위30), DB 구성",
           "  단위 %p · 순수익은 왕복 0.5 차감 · 남는 = 순수익 − 요인 몫"]
    if dec is None or dec.empty or not dec["beta_ok"].any():
        out.append("  데이터 없음 (채점 완료 이벤트 없음)")
        return out
    d = dec[dec["beta_ok"]].copy()
    d["event_slot"] = d["event_slot"].fillna("?")
    d["regime"] = d["regime"].fillna(NO_REGIME)
    slots = sorted(d["event_slot"].unique())
    regs = sorted(d["regime"].unique())
    groups = [(ALL, ALL, d)] + [(s, ALL, d[d["event_slot"] == s]) for s in slots]
    if len(regs) > 1:
        for rg in regs:
            groups.append((ALL, rg, d[d["regime"] == rg]))
            for s in slots:
                g = d[(d["regime"] == rg) & (d["event_slot"] == s)]
                if len(g):
                    groups.append((s, rg, g))
    else:
        out.append(f"  국면: 전 이벤트가 '{regs[0]}' — 국면 분리 생략")
    out.append("  트랙/국면: n · 순수익 = 시장 + 소형주 + 모멘텀 + 남는 (남는 중앙값)")
    for s, rg, g in groups:
        n = len(g)
        lab = f"  {s}/{rg}:"
        if n < _min_n():
            out.append(f"{lab} 표본 부족 (n={n})")
            continue
        out.append(
            f"{lab} n={n} · {_f(g[f'net_{h}'].mean())} = "
            f"{_f(g['part_MKT'].mean())} + {_f(g['part_SMB'].mean())} + "
            f"{_f(g['part_MOM'].mean())} + {_f(g['residual'].mean())}"
            f" (중앙 {_f(g['residual'].median())})")
    b = d[[f"b_{x}" for x in FACTORS]].median()
    out.append("  β 중앙값: " + " · ".join(
        f"{FACTOR_KO[x]} {b[f'b_{x}']:.2f}" for x in FACTORS))
    return out


def _hurst_lines(rep: dict) -> list[str]:
    hh = rep.get("hurst") or {}
    rec = hh.get("record") or {}
    w = int(_cfg("WEEKLY_V2_HURST_WINDOW", 250))
    s = (f"■ 2. 허스트(DFA) {hh.get('d')} — 계산 {hh.get('non_null', 0)} · "
         f"NULL({w}거래일 미만) {hh.get('n', 0) - hh.get('non_null', 0)}")
    out = [s]
    if hh.get("median") is not None:
        out.append(f"  분포: 하위10% {hh['p10']:.2f} · 중앙 {hh['median']:.2f} · "
                   f"상위10% {hh['p90']:.2f}")
    if rec.get("reason"):
        out.append(f"  indicator_log 기록: 0행 ({rec['reason']})")
    else:
        out.append(f"  indicator_log 기록: {rec.get('written', 0)}행 "
                   f"(name={HURST_NAME}, kind={KIND_LIVE})")
    return out


def _corr_lines(rep: dict) -> list[str]:
    c = rep.get("corr") or {}
    m = c.get("matrix")
    flag = c.get("flag", 0.8)
    out = [f"■ 3. 지표 간 상관 (날짜별 순위상관 평균 · 최근 표본일 "
           f"{c.get('dates', 0)}개)"]
    if m is None or m.empty or m.shape[0] < 2:
        out.append("  데이터 없음 (지표 2개 이상 필요)")
        return out
    names = list(m.index)
    out.append("        " + " ".join(f"{_ab(n):>6}" for n in names))
    for a in names:
        cells = []
        for b in names:
            v = m.at[a, b]
            cells.append("     ·" if a == b else (f"{v:+6.2f}" if np.isfinite(v)
                                                    else "     —"))
        out.append(f"  {_ab(a):>6}" + " ".join(cells))
    hi = []
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            v = m.at[a, b]
            if np.isfinite(v) and abs(v) >= flag:
                hi.append(f"{a}~{b} {v:+.2f}")
    out.append(f"  |ρ|≥{flag:.1f} 쌍: " + (", ".join(hi) if hi else "없음"))
    return out


def _quint_lines(rep: dict) -> list[str]:
    q = rep.get("quintile") or {}
    t = q.get("table")
    h = rep["horizon"]
    ds = q.get("dates") or []
    out = [f"■ 4. 지표별 5분위 — {h}일 시장(전종목 중위) 대비 초과수익 %p",
           f"  표본일 {len(ds)}개(주별 마지막 거래일, {h}일 경과분) · 가상 이벤트 "
           f"{q.get('virtual_events', 0):,} · 채점 {q.get('scored', 0):,}",
           "  Q1=지표 낮음 … Q5=높음 · 칸 = 평균 / 중앙 (n)",
           "  ※ 표본이 서로 독립이 아님 — 주별 표본일의 20일 구간이 겹치고 같은 날"
           " 종목끼리 상관된다. n 은 독립 표본 수가 아니다."]
    if t is None or t.empty:
        out.append("  데이터 없음")
        return out
    for (name, kind), g in t.groupby(["name", "kind"], sort=True):
        for rg in [ALL] + sorted(x for x in g["regime"].unique() if x != ALL):
            gg = g[g["regime"] == rg].set_index("q")
            if gg.empty:
                continue
            if rg != ALL and set(g["regime"].unique()) == {ALL, rg}:
                continue           # 국면이 하나뿐이면 '전체' 와 같다
            out.append(f"  {name} [{kind}] {rg}")
            cells = []
            for qq in range(1, 6):
                if qq not in gg.index:
                    cells.append(f"Q{qq} 표본 부족 (n=0)")
                    continue
                r = gg.loc[qq]
                if r["n"] < _min_n():
                    cells.append(f"Q{qq} 표본 부족 (n={int(r['n'])})")
                else:
                    cells.append(f"Q{qq} {_f(r['mean'])}/{_f(r['median'])} "
                                 f"({int(r['n']):,})")
            out.append("    " + " | ".join(cells))
    return out


def _null_lines(rep: dict) -> list[str]:
    t = rep.get("nulls")
    out = ["■ 5. 지표별 NULL 비율 (이름별 마지막 기록일)"]
    if t is None or t.empty:
        out.append("  데이터 없음")
        return out
    out.append("  지표           기록일      행   행중NULL  시세종목대비 결측")
    for r in t.itertuples(index=False):
        out.append(f"  {r.name:<14} {r.d} {r.rows:>5} {_f(r.rows_null_pct, 1, False):>7}% "
                   f"{_f(r.vs_priced_pct, 1, False):>7}% (시세 {r.priced})")
    return out


def _vol_lines(rep: dict) -> list[str]:
    v = rep.get("vol") or {}
    h = rep["horizon"]
    tol = float(_cfg("WEEKLY_V2_VOL_HIT_TOL", 0.25)) * 100
    org = v.get("origins") or []
    out = [f"■ 6. GARCH(1,1) vs EWMA σ — {h}일 실현 변동성 적중",
           f"  원점 {len(org)}개({org[0] if org else '-'}~{org[-1] if org else '-'}, "
           f"{_cfg('WEEKLY_V2_GARCH_STEP', 20)}일 간격) · 대상 {v.get('n_tickers', 0)}종목 "
           f"· λ={v.get('lambda')} ({v.get('lambda_src')})",
           f"  일간 σ %단위 · 적중 = |예측−실현| ≤ 실현의 {tol:.0f}% · "
           "우세 = 상대보다 실현에 가까운 비율"]

    def line(lab, r):
        if not r or r.get("n", 0) == 0:
            return f"  {lab}: 데이터 없음"
        if r["n"] < _min_n():
            return f"  {lab}: 표본 부족 (n={r['n']})"
        s = (f"  {lab}: n={r['n']:,} MAE {r['mae']:.3f} RMSE {r['rmse']:.3f} "
             f"편향 {r['bias']:+.3f} QLIKE {r['qlike']:.3f} 적중 {r['hit']:.0f}%")
        if "closer" in r:
            s += f" 우세 {r['closer']:.0f}%"
        return s
    out.append(line("GARCH(1,1)", v.get("garch_vs_self") or v.get("garch")))
    out.append(line("EWMA (E 기록값 아님)", v.get("ewma_self")))
    if v.get("ewma_e_n"):
        out.append(line("EWMA (E 기록값)", v.get("ewma_e")))
        out.append(line("GARCH (같은 표본)", v.get("garch_on_e")))
    else:
        out.append("  EWMA (E 기록값): 데이터 없음 (risk_metrics 없음/해당일 없음)")
    p = v.get("params")
    if p:
        out.append(f"  GARCH 중앙값 α {p['alpha_med']:.3f} · β {p['beta_med']:.3f}"
                   f" · 지속성 {p['persist_med']:.3f}")
    return out


def _switch_lines(rep: dict) -> list[str]:
    s = rep.get("switch") or {}
    out = ["■ 7. 2상태 국면전환 모형 (KOSPI 시총가중 · DB 구성) — 표시만"]
    if not s.get("ok"):
        out.append(f"  데이터 없음 ({s.get('reason', '-')})")
        return out
    mu, sg, P, du = s["mu"], s["sigma"], s["P"], s["duration"]
    out.append(f"  표본 {s['n']}일 ({s['first']}~{s['last']}) · EM {s['iters']}회")
    out.append(f"  저변동: 평균 {mu[0]*100:+.3f}% σ {sg[0]*100:.2f}% 지속 "
               f"{du[0]:.0f}일 | 고변동: 평균 {mu[1]*100:+.3f}% σ {sg[1]*100:.2f}% "
               f"지속 {du[1]:.0f}일")
    out.append(f"  현재({s['last']}) 고변동 국면 확률 {s['p_high_now']*100:.0f}%")
    out.append("  날짜        고변동확률(필터)  규칙 라벨(C)")
    for r in s["rows"]:
        ph = r["p_high"]
        out.append(f"  {r['d']}  {(f'{ph*100:5.0f}%') if np.isfinite(ph) else '    —':>14}"
                   f"      {r['rule']}")
    return out


def render_report(rep: dict) -> str:
    if rep.get("error"):
        return f"[주간 분석 v2] {rep['error']}"
    tm = rep.get("timings") or {}
    lines = [f"[주간 분석 v2] — 기준 {rep['asof']}",
             "검증 중 · 투자 판단 근거 아님 · 기록 전용(점수 반영 없음)",
             f"표본 {rep['min_n']}건 미만 칸은 '표본 부족'", ""]
    for fn in (_factor_lines, _hurst_lines, _corr_lines, _quint_lines,
               _null_lines, _vol_lines, _switch_lines):
        lines.extend(fn(rep))
        lines.append("")
    src = rep.get("sources") or {}
    lines.append("출처: " + " · ".join(
        f"{k} {'있음' if v else '없음'}" for k, v in src.items()))
    lines.append(f"소요 {tm.get('total', 0):.1f}초")
    return "\n".join(lines)


def run_weekly_v2(store, record: bool = True, out_dir: str | Path | None = None,
                  lock_dir: str | Path = "data/locks", no_lock: bool = False,
                  asof: str | None = None) -> tuple[str, dict]:
    """weekly 모드가 부른다. (리포트 텍스트, --json 요약)."""
    rep = build_report(store, asof=asof, record=record, lock_dir=lock_dir,
                       no_lock=no_lock)
    text = render_report(rep)
    od = Path(out_dir or _cfg("WEEKLY_V2_REPORT_DIR", "data/reports"))
    path = None
    try:
        od.mkdir(parents=True, exist_ok=True)
        path = od / f"weekly_v2_{(rep.get('asof') or 'na').replace('-', '')}.txt"
        path.write_text(text, encoding="utf-8")
    except OSError as exc:
        log.warning("주간 분석 리포트 파일 기록 실패: %s", exc)
    tm = rep.get("timings") or {}
    summ = {"asof": rep.get("asof"),
            "elapsed_sec": round(tm.get("total", 0.0), 1),
            "timings": {k: round(v, 2) for k, v in tm.items()},
            "report_file": str(path) if path else None,
            "hurst_written": (rep.get("hurst") or {}).get("record", {}).get("written", 0),
            "hurst_record_note": (rep.get("hurst") or {}).get("record", {}).get("reason"),
            "events": (rep.get("factor") or {}).get("events", 0),
            "decomposed": (rep.get("factor") or {}).get("decomposed", 0),
            "sources": rep.get("sources")}
    return text, summ
