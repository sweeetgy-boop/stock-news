# -*- coding: utf-8 -*-
"""v2-A 보고 — 실전 recos 재채점(5·7번)과 백테스트 결과 표(6·7·8번).

읽기 전용이다. DB 에 쓰지 않는다(읽기 전용 연결 + Store 를 열지 않음.
국면 훅이 Store 를 요구하면 그때만 연다 — 사본에서 돌릴 것).

    python tools/v2_a_report.py --db <DB 사본> --out <md>
        [--index-cache <dir>] [--bt-dir <백테스트 export 폴더>] [--bt-stamp YYYYMMDD_HHMM]

  5번  실전 recos 전체 → [0] assign_events(N=20) → score_events.
       트랙 = 이벤트 시작 행의 slot (VALUE/TREND/SEQ/FILL) + 등급 S+ + 전체.
       지수 대비(ex_idx) · 전종목 중위 대비(ex_med), 5·10·20일 (+40·60 부록).
  6번  백테스트 CSV(bt_events/candidates/random/rsi/exits_<stamp>.csv) 요약
       + 10-07 결과(구 정의)와 나란히.
  7번  5번·6번을 트랙 × 국면 표로. 국면 = `v2_hooks.regime_on(store, 날짜)`
       (C 묶음). None 이면 "국면 없음". **보고 시점에 붙인다** — C 병합·국면
       소급 뒤 이 스크립트만 다시 돌리면 된다(백테스트 재실행 불필요).
  8번  청산 엔진 vs 같은 이벤트 고정 20일 보유, 국면별.

칸은 `events.stat_cell` / `fmt_cell` — 20건 미만은 "표본 부족 (n=…)".
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from stocknews import events as ev  # noqa: E402
from stocknews.config import EVENT_WINDOW_DAYS  # noqa: E402

NO_REGIME = "국면 없음"
REGIME_ORDER = ("상승", "중립", "하락", NO_REGIME)
SLOTS = ("VALUE", "TREND", "SEQ", "FILL")

# 10-07 실행(구 정의: 중복 포함 6,692건, h일 뒤 시가 청산, 대조군은 순수익
# 비교, 시장 중위는 풀 중위). 그 CSV 는 남아 있지 않아 보고 수치만 옮긴다.
OLD_1007 = {
    "events": 6692, "tickers": 975, "period": "2025-05 ~ 2026-09",
    "h5_net": 0.70, "h5_random_net": 0.97,
    "alpha10_splus": 1.7, "alpha10_a": 3.2,
    "engine_net": 1.15, "hold20_net": 3.71, "stop_fixed_net": -12.8,
}


# ══════════════════════════ 공통 ══════════════════════════
class _RO:
    """읽기 전용 store 대용. events.trading_calendar / PricePanel.from_store /
    ticker_markets 가 쓰는 path·complete_dates 만 제공한다."""

    def __init__(self, db: str):
        self.path = db
        self._real = None

    def _con(self):
        uri = Path(self.path).resolve().as_uri() + "?mode=ro"
        return closing(sqlite3.connect(uri, uri=True, timeout=30.0))

    def complete_dates(self, since=None):
        return self.real().complete_dates(since)

    def real(self):
        # complete_dates 의 판정 규칙(부분 적재일 제외)은 Store 에만 있다.
        if self._real is None:
            from stocknews.store import Store
            self._real = Store(self.path)
        return self._real


def _regime_labeler(store_like):
    from stocknews import v2_hooks
    cache: dict[str, str] = {}

    def lab(d) -> str:
        k = str(d)[:10]
        if k not in cache:
            try:
                v = v2_hooks.regime_on(store_like.real(), k)
            except Exception:  # noqa: BLE001
                v = None
            cache[k] = v or NO_REGIME
        return cache[k]
    return lab


def _cell(values) -> str:
    """표 한 칸. 20건 미만은 "표본 부족 (n=…)" ([0] fmt_cell)."""
    return ev.fmt_cell(ev.stat_cell(values))


def _table(rows: list[tuple[str, list[str]]], head: list[str]) -> list[str]:
    out = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    for k, cells in rows:
        out.append("| " + " | ".join([k] + cells) + " |")
    return out


# ══════════════════════════ 5번 · 7번(실전) ══════════════════════════
def live_section(db: str, index_cache: str | None, window: int) -> tuple[list[str], dict]:
    st = _RO(db)
    with st._con() as con:
        recos = pd.read_sql_query(
            "SELECT d, rank, ticker, name, slot, grade FROM recos", con)
    cal = ev.trading_calendar(st)
    assigned = ev.assign_events(recos, cal, window=window)
    evs = ev.events_only(assigned)
    panel = ev.PricePanel.from_store(st, calendar=cal)
    from datetime import date, timedelta
    start = str(min(recos["d"]))[:10] if len(recos) else "2026-01-01"
    start = (date.fromisoformat(start) - timedelta(days=10)).isoformat()
    index = ev.load_index_history(start=start, cache_dir=index_cache)
    sc = ev.score_events(evs, panel, index, ev.ticker_markets(st),
                         horizons=(5, 10, 20, 40, 60))
    lab = _regime_labeler(st)
    sc["regime"] = sc["event_date"].map(lab)

    L = ["## 5. 실전 recos 전체 재채점 ([0] 이벤트 N=%d · score_events)" % window, ""]
    L.append(f"- recos {len(recos)}행 · {recos['d'].nunique()}거래일 "
             f"({recos['d'].min()} ~ {recos['d'].max()}) · 종목 {recos['ticker'].nunique()}개")
    L.append(f"- **독립 표본(이벤트) {len(evs)}건** · 이벤트 종목 {evs['ticker'].nunique()}개 "
             f"(같은 종목 {window}거래일 안 재추천 {len(recos) - len(evs)}행은 같은 이벤트)")
    L.append(f"- 가격 달력 {cal[0]} ~ {cal[-1]} · 지수 "
             + ", ".join(f"{k} ~{v.index[-1]}" for k, v in index.items()))
    L.append("- 진입 = 이벤트 다음 거래일 시가(1일째) → h일째 종가, 왕복 0.5%p 차감. "
             "초과 = 순수익 − 대조(지수 / 전종목 중위). 20건 미만 칸은 '표본 부족'.")
    L.append("")
    st_rows = []
    for h in (5, 10, 20, 40, 60):
        vc = sc[f"status_{h}"].value_counts()
        st_rows.append((f"{h}일", [", ".join(f"{k} {v}" for k, v in vc.items())]))
    L.append("채점 상태 (OK=채점됨 · PENDING=미도래 · NO_ENTRY=진입 불가 · "
             "ADJ_BREAK=수정주가 불연속 · HALTED=보유 중 정지):")
    L.append("")
    L += _table(st_rows, ["보유", "상태별 이벤트 수"])
    L.append("")

    groups = [(s, sc["event_slot"] == s) for s in SLOTS]
    groups.append(("등급 S+", sc["event_grade"] == "S+"))
    groups.append(("전체", pd.Series(True, index=sc.index)))
    ev_n = {k: int(m.sum()) for k, m in groups}

    for metric, title in (("ex_idx", "지수 대비 초과수익 (ex_idx, %p)"),
                          ("ex_med", "전종목 중위 대비 초과수익 (ex_med, %p)")):
        L.append(f"### 5-{'a' if metric == 'ex_idx' else 'b'}. {title}")
        L.append("")
        rows = [(f"{k} (이벤트 {ev_n[k]})",
                 [_cell(sc.loc[m, f"{metric}_{h}"]) for h in (5, 10, 20)])
                for k, m in groups]
        L += _table(rows, ["트랙", "5일", "10일", "20일"])
        L.append("")
    L.append("#### 부록 — 40·60일 (표본 있는 만큼)")
    L.append("")
    rows = [(f"{k}", [_cell(sc.loc[m, f"{mt}_{h}"]) for mt in ("ex_idx", "ex_med")
                      for h in (40, 60)]) for k, m in groups]
    L += _table(rows, ["트랙", "지수 40일", "지수 60일", "중위 40일", "중위 60일"])
    L.append("")
    L.append("참고 — 순수익(비용 차감, 대조 없음):")
    L.append("")
    rows = [(k, [_cell(sc.loc[m, f"net_{h}"]) for h in (5, 10, 20)]) for k, m in groups]
    L += _table(rows, ["트랙", "5일", "10일", "20일"])
    L.append("")

    # 7번 (실전)
    L.append("## 7-a. 실전 이벤트 — 트랙 × 국면 (국면 = v2_hooks.regime_on(이벤트 날짜))")
    L.append("")
    regs = [r for r in REGIME_ORDER if r in set(sc["regime"])] + \
           sorted(set(sc["regime"]) - set(REGIME_ORDER))
    L.append("국면별 이벤트 수: " + ", ".join(
        f"{r} {int((sc['regime'] == r).sum())}" for r in regs))
    L.append("")
    for metric, mname in (("ex_med", "전종목 중위 대비"), ("ex_idx", "지수 대비")):
        for h in (5, 10, 20):
            L.append(f"**{mname} {h}일 (%p)**")
            L.append("")
            rows = [(k, [_cell(sc.loc[m & (sc["regime"] == r), f"{metric}_{h}"])
                         for r in regs]) for k, m in groups]
            L += _table(rows, ["트랙"] + regs)
            L.append("")
    return L, {"recos": len(recos), "events": len(evs), "scored": sc}


# ══════════════════════════ 6·7·8번 (백테스트) ══════════════════════════
def _latest_stamp(bt_dir: Path) -> str | None:
    fs = sorted(bt_dir.glob("bt_events_*.csv"))
    return fs[-1].stem.replace("bt_events_", "") if fs else None


def _read(bt_dir: Path, tag: str, stamp: str) -> pd.DataFrame:
    p = bt_dir / f"bt_{tag}_{stamp}.csv"
    if not p.exists():
        return pd.DataFrame()
    return pd.read_csv(p, dtype={"ticker": str}, encoding="utf-8-sig")


def _m(df: pd.DataFrame, col: str) -> float:
    if df is None or df.empty or col not in df:
        return float("nan")
    s = df[col].dropna()
    return float(s.mean()) if len(s) else float("nan")


def old_columns(old_dir: Path | None, old_json: dict | None) -> dict:
    """구 코드(9b77e79) 백테스트 결과 → 6-e 비교 칸. 없으면 {}."""
    if not old_dir:
        return {}
    stamp = _latest_stamp(old_dir)
    if not stamp:
        return {}
    E = _read(old_dir, "events", stamp)
    X = _read(old_dir, "exits", stamp)
    s = (old_json or {}).get("summary", {})
    rnd = (s.get("controls") or {}).get("random") or {}
    out = {"events": len(E), "tickers": E["ticker"].nunique(),
           "period": f"{E['date'].min()[:7]} ~ {E['date'].max()[:7]}",
           "h5_net": _m(E, "net_5"), "h5_alpha": _m(E, "alpha_5"),
           "h5_random_net": rnd.get("mean", float("nan")),
           "alpha10_splus": _m(E[E["grade"] == "S+"], "alpha_10"),
           "alpha10_a": _m(E[E["grade"] == "A"], "alpha_10"),
           "warnings": s.get("warnings")}
    if not X.empty:
        j = X.merge(E[["ticker", "date", "net_20"]], on=["ticker", "date"], how="left")
        jb = j[j["net"].notna() & j["net_20"].notna()]
        st = X[X["rule"].astype(str).str.startswith("stop:fixed")]
        out.update({"engine_net": _m(jb, "net"), "hold20_net": _m(jb, "net_20"),
                    "stop_fixed_net": _m(st, "net"), "n_stop": len(st)})
    return out


def bt_section(db: str, bt_dir: Path, stamp: str | None,
               old_cols: dict | None = None,
               new_json: dict | None = None) -> list[str]:
    stamp = stamp or _latest_stamp(bt_dir)
    if not stamp:
        return ["## 6. 백테스트 — 결과 CSV 없음", ""]
    E = _read(bt_dir, "events", stamp)
    C = _read(bt_dir, "candidates", stamp)
    R = _read(bt_dir, "random", stamp)
    Q = _read(bt_dir, "rsi", stamp)
    X = _read(bt_dir, "exits", stamp)
    st = _RO(db)
    lab = _regime_labeler(st)
    for df in (E, X):
        if not df.empty:
            df["regime"] = df["date"].map(lab)

    hs = [int(c.split("_")[1]) for c in E.columns
          if c.startswith("alpha_") and c.split("_")[1].isdigit()]
    L = [f"## 6. 수정한 백테스트 재실행 (stamp {stamp})", ""]
    L.append(f"- 판정 후보 {len(C):,}건 → [0] 이벤트(N={EVENT_WINDOW_DAYS}) **{len(E):,}건** · "
             f"종목 {E['ticker'].nunique()}개 · 기간 {E['date'].min()} ~ {E['date'].max()}")
    L.append(f"- 무작위 대조군 {len(R):,}건 · RSI<30 대조군 {len(Q):,}건 "
             f"(둘 다 같은 중복 제거 · 같은 채점)")
    L.append("- 정의: 익일 시가 진입(1일째) → h일째 종가 · 왕복 0.5%p · "
             "초과(alpha) = 순수익 − 같은 구간 전종목 중위 ([0] ex_med)")
    L.append("")

    L.append("### 6-a. 보유기간별 — 중복 제거 전(후보) vs 후(이벤트)")
    L.append("")
    rows = []
    for h in hs:
        rows.append((f"{h}일 순수익", [_cell(C.get(f"net_{h}", [])),
                                     _cell(E[f"net_{h}"])]))
        rows.append((f"{h}일 초과(중위)", [_cell(C.get(f"alpha_{h}", [])),
                                       _cell(E[f"alpha_{h}"])]))
        if f"ex_idx_{h}" in E:
            rows.append((f"{h}일 초과(지수)", [_cell(C.get(f"ex_idx_{h}", [])),
                                           _cell(E[f"ex_idx_{h}"])]))
    L += _table(rows, ["지표", f"후보 {len(C):,}건 (중복 포함)", f"이벤트 {len(E):,}건"])
    L.append("")

    L.append("### 6-b. 대조군 — 같은 지표(시장 중위 대비 초과수익)끼리")
    L.append("")
    rows = []
    for h in hs:
        rows.append((f"{h}일", [_cell(E[f"alpha_{h}"]),
                               _cell(R.get(f"alpha_{h}", [])),
                               _cell(Q.get(f"alpha_{h}", []))]))
    L += _table(rows, ["보유", "전략", "무작위 진입", "RSI<30"])
    L.append("")
    rows = [(f"{h}일", [_cell(E[f"net_{h}"]), _cell(R.get(f"net_{h}", [])),
                       _cell(Q.get(f"net_{h}", []))]) for h in hs]
    L.append("참고 — 순수익끼리:")
    L.append("")
    L += _table(rows, ["보유", "전략", "무작위 진입", "RSI<30"])
    L.append("")

    L.append("### 6-c. 등급·트랙별 초과수익(중위)")
    L.append("")
    rows = []
    for col, keys in (("grade", ("S+", "S", "A")), ("track", ("VALUE", "TREND", "BOTH"))):
        for k in keys:
            m = E[col] == k
            if m.any():
                rows.append((f"{col}={k}", [_cell(E.loc[m, f"alpha_{h}"]) for h in (5, 10, 20) if h in hs]))
    L += _table(rows, ["구분"] + [f"{h}일" for h in (5, 10, 20) if h in hs])
    L.append("")

    if not X.empty:
        L.append("### 6-d. 청산 엔진 vs 같은 이벤트 고정 20일 보유 (순수익)")
        L.append("")
        both = X[X["net"].notna() & X["hold_net"].notna()]
        rows = [("엔진 청산 (같은 표본)", [_cell(both["net"])]),
                ("고정 20일 보유 (같은 표본)", [_cell(both["hold_net"])]),
                ("엔진 청산 (전체 시뮬)", [_cell(X["net"])]),
                ("고정 20일 보유 초과(중위)", [_cell(both["hold_alpha"])])]
        L += _table(rows, ["구분", "값"])
        L.append("")
        L.append("규칙별 (엔진):")
        L.append("")
        rows = []
        for r, g in X.groupby("rule"):
            rows.append((str(r), [f"{len(g)}", f"{g['net'].mean():+.2f}",
                                  f"{g['held_days'].mean():.1f}"]))
        rows.sort(key=lambda x: -int(x[1][0]))
        L += _table(rows, ["규칙", "n", "순수익 평균", "평균 보유일"])
        L.append("")

    # 10-07 대비
    O = OLD_1007
    a10 = lambda g: _m(E[E["grade"] == g], "alpha_10")  # noqa: E731
    stop = X[X["rule"].astype(str).str.startswith("stop:fixed")] if not X.empty else X
    both = X[X["net"].notna() & X["hold_net"].notna()] if not X.empty else X
    new = {
        "h5_net": _m(E, "net_5"), "h5_alpha": _m(E, "alpha_5"),
        "h5_random_net": _m(R, "net_5"), "h5_random_alpha": _m(R, "alpha_5"),
        "alpha10_splus": a10("S+"), "alpha10_a": a10("A"),
        "engine_net": _m(both, "net"), "hold20_net": _m(both, "hold_net"),
        "stop_fixed_net": _m(stop, "net"),
    }

    n_s = int((E["grade"] == "S+").sum())
    n_a = int((E["grade"] == "A").sum())
    new.update({"events": len(E), "cands": len(C), "tickers": E["ticker"].nunique(),
                "period": f"{E['date'].min()[:7]} ~ {E['date'].max()[:7]}",
                "n_stop": len(stop), "n_both": len(both), "n_s": n_s, "n_a": n_a,
                "warnings": (new_json or {}).get("summary", {}).get("warnings")})
    oc = old_cols or {}

    def yn(b):
        return "예" if b else "아니오"

    def keep(a: bool, b: bool) -> str:
        return "유지" if a == b else "**달라짐**"

    L.append("### 6-e. 10-07 결과(구 정의)와 비교")
    L.append("")
    L.append("'구 코드·같은 사본' = 9b77e79 의 backtest 를 이 사본에 같은 인자로 돌린 값"
             "(구 정의: 중복 포함, h일 뒤 시가 청산, 풀 중위, 대조군 순수익 비교).")
    L.append("")

    def oc_get(k, fmt="{:+.2f}"):
        v = oc.get(k)
        return "-" if v is None or (isinstance(v, float) and np.isnan(v)) else fmt.format(v)

    old_beats = (O['h5_net'] > O['h5_random_net'])
    new_beats = (new['h5_alpha'] > new['h5_random_alpha'])
    rows = [
        ("표본", [f"{O['events']:,}건 (중복 포함) · {O['tickers']}종목 · {O['period']}",
                (f"{oc['events']:,}건 (중복 포함) · {oc['tickers']}종목 · {oc['period']}"
                 if oc else "-"),
                f"후보 {len(C):,} → 이벤트 {len(E):,}건 · {new['tickers']}종목 · "
                f"{new['period']}", "표본이 독립 이벤트로 줄어듦"]),
        ("h=5 전략", [f"순수익 {O['h5_net']:+.2f}%",
                     f"순수익 {oc_get('h5_net', '{:+.2f}%')} · "
                     f"초과 {oc_get('h5_alpha', '{:+.2f}%p')}",
                     f"순수익 {new['h5_net']:+.2f}% · 초과 {new['h5_alpha']:+.2f}%p", ""]),
        ("h=5 무작위", [f"순수익 {O['h5_random_net']:+.2f}%",
                      f"순수익 {oc_get('h5_random_net', '{:+.2f}%')} (초과 미계산)",
                      f"순수익 {new['h5_random_net']:+.2f}% · 초과 {new['h5_random_alpha']:+.2f}%p", ""]),
        ("전략 > 무작위? (h=5)", [yn(old_beats) + " (순수익끼리)",
                              (yn(oc['h5_alpha'] > oc['h5_random_net'])
                               + " (구 summarize: 전략 초과 vs 무작위 순수익)") if oc else "-",
                              yn(new_beats) + " (초과끼리)", keep(old_beats, new_beats)]),
        ("alpha_10 S+ vs A", [f"S+ {O['alpha10_splus']:+.1f} vs A {O['alpha10_a']:+.1f}",
                             f"S+ {oc_get('alpha10_splus')} vs A {oc_get('alpha10_a')}",
                             f"S+ {new['alpha10_splus']:+.2f} (n={n_s}) vs A "
                             f"{new['alpha10_a']:+.2f} (n={n_a})",
                             keep(O['alpha10_splus'] < O['alpha10_a'],
                                  new['alpha10_splus'] < new['alpha10_a'])]),
        ("엔진 vs 20일 보유 (순수익)", [f"{O['engine_net']:+.2f} vs {O['hold20_net']:+.2f}",
                                   f"{oc_get('engine_net')} vs {oc_get('hold20_net')}",
                                   f"{new['engine_net']:+.2f} vs {new['hold20_net']:+.2f} "
                                   f"(n={len(both)})",
                                   keep(O['engine_net'] < O['hold20_net'],
                                        new['engine_net'] < new['hold20_net'])]),
        ("고정손절(stop:fixed) 평균", [f"{O['stop_fixed_net']:+.1f}%",
                                   f"{oc_get('stop_fixed_net', '{:+.2f}%')} "
                                   f"(n={oc.get('n_stop', '-')})",
                                   f"{new['stop_fixed_net']:+.2f}% (n={len(stop)})", ""]),
        ("summarize() 경고", ["-",
                             "; ".join(oc.get("warnings") or []) or "(없음)" if oc else "-",
                             "; ".join(new.get("warnings") or []) or "(없음)", ""]),
    ]
    L += _table(rows, ["항목", "10-07 (구 정의)", "구 코드·같은 사본", "지금 (v2-A)", "결론"])
    L.append("")

    # 7-b
    L.append("## 7-b. 백테스트 이벤트 — 트랙 × 국면 (초과수익 = 중위 대비, %p)")
    L.append("")
    regs = [r for r in REGIME_ORDER if r in set(E["regime"])] + \
           sorted(set(E["regime"]) - set(REGIME_ORDER))
    L.append("국면별 이벤트 수: " + ", ".join(
        f"{r} {int((E['regime'] == r).sum())}" for r in regs))
    L.append("")
    groups = [(f"track={t}", E["track"] == t) for t in ("VALUE", "TREND", "BOTH")
              if (E["track"] == t).any()]
    groups.append(("등급 S+", E["grade"] == "S+"))
    groups.append(("전체", pd.Series(True, index=E.index)))
    for h in (5, 10, 20):
        if h not in hs:
            continue
        L.append(f"**{h}일**")
        L.append("")
        rows = [(k, [_cell(E.loc[m & (E["regime"] == r), f"alpha_{h}"]) for r in regs])
                for k, m in groups]
        L += _table(rows, ["트랙"] + regs)
        L.append("")

    # 8
    if not X.empty:
        L.append("## 8. 청산 엔진 vs 고정 20일 보유 — 국면별 (같은 이벤트, 순수익 %)")
        L.append("")
        both = X[X["net"].notna() & X["hold_net"].notna()].copy()
        regs8 = [r for r in REGIME_ORDER if r in set(X["regime"])] + \
                sorted(set(X["regime"]) - set(REGIME_ORDER))
        rows = []
        for r in ["전체"] + regs8:
            g = both if r == "전체" else both[both["regime"] == r]
            rows.append((r, [_cell(g["net"]), _cell(g["hold_net"]),
                             (f"{g['net'].mean() - g['hold_net'].mean():+.2f}"
                              if len(g) >= 20 else "표본 부족")]))
        L += _table(rows, ["국면", "엔진 청산", "고정 20일 보유", "엔진 − 보유 (평균)"])
        L.append("")
    return L


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", required=True, help="DB 사본 경로 (운영 DB 금지)")
    ap.add_argument("--out", required=True, help="markdown 출력 경로")
    ap.add_argument("--index-cache", default=None)
    ap.add_argument("--bt-dir", default=None, help="백테스트 --export-dir")
    ap.add_argument("--bt-stamp", default=None)
    ap.add_argument("--bt-json", default=None,
                    help="백테스트 --json stdout 파일 (경고 목록 표시용)")
    ap.add_argument("--bt-old-dir", default=None,
                    help="구 코드(9b77e79) 백테스트 export 폴더 (6-e 비교용, 선택)")
    ap.add_argument("--bt-old-json", default=None)
    ap.add_argument("--window", type=int, default=EVENT_WINDOW_DAYS)
    a = ap.parse_args(argv)

    L = ["# v2-A 측정 바로잡기 — 보고 표", ""]
    live, _ = live_section(a.db, a.index_cache, a.window)
    L += live
    if a.bt_dir:
        def _js(p):
            import json
            if not p or not Path(p).exists():
                return None
            txt = Path(p).read_text(encoding="utf-8").strip().splitlines()
            return json.loads(txt[-1]) if txt else None
        old = old_columns(Path(a.bt_old_dir) if a.bt_old_dir else None,
                          _js(a.bt_old_json))
        L += bt_section(a.db, Path(a.bt_dir), a.bt_stamp, old, _js(a.bt_json))
    Path(a.out).write_text("\n".join(L) + "\n", encoding="utf-8")
    print(f"written {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
