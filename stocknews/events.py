# -*- coding: utf-8 -*-
"""[0] 공통 기반 — 이벤트 판정과 채점.

**이 모듈이 '추천 1건'과 '그 추천의 성적'의 유일한 정의다.** 백테스트,
실전 재채점, 재추천 제한, 추천 추적, 주간 표가 전부 이 두 함수를 쓴다.
같은 개념을 모듈마다 따로 구현하면 숫자가 서로 어긋나고, 어긋난 순간
어느 표도 믿을 수 없게 된다.

이벤트 판정 (`assign_events`)
---------------------------
  · 종목 단위다. 슬롯·등급과 무관하게 같은 종목이면 같은 흐름으로 본다.
  · 그 종목이 처음 추천된 날이 이벤트 날짜다.
  · 그날을 1일째로 세어 N거래일(달력일 아님) 동안은 다시 추천돼도 새
    이벤트가 아니다. 기간 중 계속 추천돼도 **기간을 연장하지 않는다.**
  · 기간이 끝난 뒤(N+1일째 이후) 처음 추천되는 날이 다음 이벤트다.
  · 기간 중 목록에서 빠졌다가 다시 나와도 같은 이벤트다.

  예) N=20, 이벤트가 달력 위치 i 이면 i ~ i+19 가 기간이고, i+19(20일째)
      재추천은 같은 이벤트, i+20(21일째) 재추천은 새 이벤트다.

채점 (`score_events`)
--------------------
  · 진입 = 이벤트 **다음 거래일 시가**. 이벤트 날짜는 장 마감 후 스캔
    기준일이라 그날 종가로는 살 수 없다.
  · 보유 h거래일 = 진입일을 1일째로 세어 h일째 **종가**에 청산.
    (MIN_HOLD_DAYS 와 같은 규약: 진입일 봉을 1일로 센다.)
  · 비용 = 왕복 `ROUND_TRIP_COST_PCT`(0.5%p)를 수익률에서 뺀다.
  · 대조 1 = 소속 시장 지수(KOSPI/KOSDAQ)의 같은 구간 수익률
            (진입일 시가 → h일째 종가). 비용을 빼지 않는다.
  · 대조 2 = 같은 구간 전 종목 중위 수익률(진입일 시가 → h일째 종가).
            비용을 빼지 않는다. 그래서 초과수익은 비용만큼 보수적이다.
  · 초과수익 = 순수익(비용 차감) − 대조 수익률.

수정주가 기준
------------
DB 시세는 백필분(pykrx = 수정주가)과 일일 적재분(원시 가격)이 섞여 있고
조정계수 테이블이 없다. 그래서 '수정주가로 다시 계산'하는 대신 **원시
가격의 불연속이 낀 구간을 채점에서 뺀다.** KRX 일일 가격제한폭이 ±30% 라
하루 30%를 넘는 움직임은 실제 거래로 생길 수 없다 — 분할·병합·감자
같은 권리 변동이 원시 가격에 그대로 남은 흔적이다. 그런 구간은
`ADJ_BREAK` 로 표시하고 수익률을 비운다. 0 으로 넣지 않는다.

상태 코드
--------
  진입(entry_status)  OK / PENDING(다음 거래일 미도래) / SUSPENDED(그날 행
                     없음 = 거래정지) / NO_OPEN(시가 없음·0) / LIMIT_UP(시가가
                     전일 종가 대비 상한가 근처 = 실제로 살 수 없음)
  청산(status_h)     OK / PENDING(h일째 미도래) / HALTED(h일째 행 없음 →
                     마지막 거래 가격으로 종료) / ADJ_BREAK(수정주가 불연속)
                     / NO_ENTRY(진입 불가 — 사유는 entry_status)

미래 데이터 금지: 진입·청산 가격은 모두 이벤트 날짜 이후 값이고, 판정
(어느 날이 이벤트인가)은 그 날짜까지의 추천 기록만 본다.
"""
from __future__ import annotations

import bisect
import logging
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from .config import (EVENT_WINDOW_DAYS, SCORE_HORIZONS, ROUND_TRIP_COST_PCT,
                     ENTRY_LIMIT_UP_PCT, PRICE_JUMP_BREAK_PCT,
                     MIN_SCORED_FOR_JUDGMENT)

log = logging.getLogger(__name__)

__all__ = ["trading_calendar", "project_trading_days", "assign_events",
           "events_only", "PricePanel", "load_index_history",
           "index_frames_from_df", "ticker_markets", "score_events",
           "stat_cell", "fmt_cell",
           "ENTRY_OK", "ENTRY_PENDING", "ENTRY_SUSPENDED", "ENTRY_NO_OPEN",
           "ENTRY_LIMIT_UP", "ST_OK", "ST_PENDING", "ST_HALTED",
           "ST_ADJ_BREAK", "ST_NO_ENTRY"]

ENTRY_OK = "OK"
ENTRY_PENDING = "PENDING"
ENTRY_SUSPENDED = "SUSPENDED"
ENTRY_NO_OPEN = "NO_OPEN"
ENTRY_LIMIT_UP = "LIMIT_UP"

ST_OK = "OK"
ST_PENDING = "PENDING"
ST_HALTED = "HALTED"
ST_ADJ_BREAK = "ADJ_BREAK"
ST_NO_ENTRY = "NO_ENTRY"


def _ds(x) -> str:
    if isinstance(x, str):
        return x[:10]
    return pd.Timestamp(x).strftime("%Y-%m-%d")


# ══════════════════════════ 거래일 달력 ══════════════════════════
def trading_calendar(store, since: str | None = None) -> list[str]:
    """채점·이벤트 판정에 쓰는 거래일 달력(오름차순).

    `complete_dates()` 를 쓴다 — 부분 적재일(장중 백필 등)을 거래일로
    세면 'N거래일' 이 하루씩 어긋난다. 휴장일은 시세 행이 없으므로
    자연히 빠진다.
    """
    return sorted(store.complete_dates(since))


def project_trading_days(last: str, n: int,
                         holidays: set[str] | frozenset = frozenset()) -> list[str]:
    """`last` 다음부터 n개의 **예상** 거래일 (주말·알려진 공휴일 제외).

    달력 끝을 넘어가는 기간 종료일(예: 재추천 제한 종료일)을 정할 때만
    쓴다. 미래 임시휴장은 알 수 없으므로 '예상' 이다.
    """
    out: list[str] = []
    cur = date.fromisoformat(_ds(last))
    hol = {_ds(h) for h in holidays}
    while len(out) < n:
        cur += timedelta(days=1)
        s = cur.isoformat()
        if cur.weekday() < 5 and s not in hol:
            out.append(s)
    return out


def _pos(calendar: list[str], d: str) -> int:
    """d 의 달력 위치. 달력에 없으면 d 이전 마지막 거래일 위치(-1 가능)."""
    i = bisect.bisect_right(calendar, d) - 1
    return i


# ══════════════════════════ 이벤트 판정 ══════════════════════════
def assign_events(recos: pd.DataFrame, calendar: list[str],
                  window: int = EVENT_WINDOW_DAYS,
                  holidays: set[str] | frozenset = frozenset()) -> pd.DataFrame:
    """추천 행마다 소속 이벤트를 붙인다.

    recos    : 최소 컬럼 d, ticker. 그 밖의 컬럼(slot, grade, rank, 점수
               등)은 그대로 통과한다. 같은 (d, ticker) 가 여러 행이면
               rank 가 가장 작은 1행만 남긴다(rank 없으면 첫 행).
    calendar : `trading_calendar()` 결과.
    window   : N. 0 이하면 모든 추천일이 각자 이벤트다(제한 없음).

    반환 컬럼 (입력 컬럼 + 아래)
      event_id       "<ticker>:<event_date>"
      event_date     이벤트 날짜
      is_event       이 행이 이벤트 시작 행인가
      day_in_window  이벤트 날짜를 1일째로 센 거래일 순번
      window_end     기간 마지막 거래일(N일째). 달력 밖이면 주말·공휴일을
                     뺀 예상 거래일.
      event_slot / event_grade   이벤트 시작 행의 슬롯·등급
      slot_changed / grade_changed  이 행이 이벤트 시작 행과 다른가
    """
    cols_extra = ["event_id", "event_date", "is_event", "day_in_window",
                  "window_end", "event_slot", "event_grade",
                  "slot_changed", "grade_changed"]
    if recos is None or recos.empty:
        return pd.DataFrame(columns=list(getattr(recos, "columns", []))
                            + cols_extra)
    df = recos.copy()
    df["d"] = df["d"].map(_ds)
    df["ticker"] = df["ticker"].astype(str)
    sort_cols = ["ticker", "d"] + (["rank"] if "rank" in df.columns else [])
    df = df.sort_values(sort_cols, kind="mergesort")
    df = df.drop_duplicates(["ticker", "d"], keep="first").reset_index(drop=True)

    cal = list(calendar)
    hol = frozenset(_ds(h) for h in holidays)
    has_slot = "slot" in df.columns
    has_grade = "grade" in df.columns

    def _end_date(p: int) -> str | None:
        e = p + max(int(window), 1) - 1
        if e < len(cal):
            return cal[e]
        if not cal:
            return None
        extra = e - (len(cal) - 1)
        return project_trading_days(cal[-1], extra, hol)[-1]

    out_rows = []
    cur_t = None
    ev_pos = ev_date = ev_slot = ev_grade = None
    for r in df.itertuples(index=False):
        rd = r._asdict()
        p = _pos(cal, rd["d"])
        if p < 0:
            p = 0          # 달력 시작 이전 — 첫 거래일로 붙인다 (방어용)
        if rd["ticker"] != cur_t:
            cur_t = rd["ticker"]
            ev_pos = None
        new = (ev_pos is None or window <= 0 or p >= ev_pos + int(window))
        if new:
            ev_pos, ev_date = p, rd["d"]
            ev_slot = rd.get("slot") if has_slot else None
            ev_grade = rd.get("grade") if has_grade else None
        rd.update({
            "event_id": f"{cur_t}:{ev_date}",
            "event_date": ev_date,
            "is_event": bool(new),
            "day_in_window": int(p - ev_pos + 1),
            "window_end": _end_date(ev_pos) if window > 0 else ev_date,
            "event_slot": ev_slot,
            "event_grade": ev_grade,
            "slot_changed": bool(has_slot and rd.get("slot") != ev_slot),
            "grade_changed": bool(has_grade and rd.get("grade") != ev_grade),
        })
        out_rows.append(rd)
    out = pd.DataFrame(out_rows)
    return out.sort_values(["d", "ticker"], kind="mergesort").reset_index(drop=True)


def events_only(assigned: pd.DataFrame) -> pd.DataFrame:
    """`assign_events()` 결과에서 이벤트 시작 행만."""
    if assigned is None or assigned.empty:
        return assigned
    return assigned[assigned["is_event"]].reset_index(drop=True)


# ══════════════════════════ 가격 패널 ══════════════════════════
@dataclass
class PricePanel:
    """거래일 × 종목 시가·종가 행렬. 채점 전용.

    dates  : 거래일 달력(오름차순, 'YYYY-MM-DD')
    open/close : index=dates, columns=ticker. 행이 없으면 NaN.
    """
    dates: list[str]
    open: pd.DataFrame
    close: pd.DataFrame

    def __post_init__(self):
        self.pos = {d: i for i, d in enumerate(self.dates)}
        # 수정주가 불연속 누적표: 종가(직전 유효값 기준) 일간 변동이
        # 가격제한폭을 넘은 날 = 1. 구간 (a, b] 의 불연속 수 = cs[b]-cs[a].
        cf = self.close.ffill()
        ratio = (cf / cf.shift(1)).to_numpy(dtype="float64")
        # 상한가(정확히 +30%)는 실제 거래다. 부동소수 오차(1.3-1.0 =
        # 0.30000000000000004)로 걸리지 않게 여유를 둔다.
        lim = PRICE_JUMP_BREAK_PCT / 100.0 + 1e-6
        with np.errstate(invalid="ignore"):
            brk = np.abs(ratio - 1.0) > lim
        brk = np.nan_to_num(brk.astype("float64"), nan=0.0)
        self._break_cs = np.cumsum(brk, axis=0)
        self._o = self.open.to_numpy(dtype="float64")
        self._c = self.close.to_numpy(dtype="float64")
        self._cf = cf.to_numpy(dtype="float64")
        self._col = {t: j for j, t in enumerate(self.close.columns)}
        self._median_cache: dict[tuple[int, int], float] = {}

    # ── 생성 ──
    @classmethod
    def from_frame(cls, df: pd.DataFrame, calendar: list[str] | None = None
                   ) -> "PricePanel":
        """long 형식(d, ticker, o, c) → 패널. smoke·외부 데이터용."""
        d = df.copy()
        d["d"] = d["d"].map(_ds)
        d["ticker"] = d["ticker"].astype(str)
        cal = sorted(set(d["d"])) if calendar is None else list(calendar)
        o = d.pivot(index="d", columns="ticker", values="o").reindex(cal)
        c = d.pivot(index="d", columns="ticker", values="c").reindex(cal)
        cols = sorted(set(o.columns) | set(c.columns))
        o, c = o.reindex(columns=cols), c.reindex(columns=cols)
        # 0/음수는 거래된 가격이 아니다 (Store._price_ok 와 같은 규칙)
        o = o.where(o > 0)
        c = c.where(c > 0)
        return cls(cal, o, c)

    @classmethod
    def from_store(cls, store, since: str | None = None,
                   calendar: list[str] | None = None) -> "PricePanel":
        """DB 시세 전체(또는 since 이후)로 패널을 만든다. 읽기만 한다."""
        import sqlite3
        from contextlib import closing
        cal = calendar if calendar is not None else trading_calendar(store, since)
        if since:
            cal = [x for x in cal if x >= _ds(since)]
        if not cal:
            return cls([], pd.DataFrame(), pd.DataFrame())
        with closing(sqlite3.connect(store.path, timeout=30.0)) as con:
            df = pd.read_sql_query(
                "SELECT d,ticker,o,c FROM prices WHERE d>=? AND d<=?",
                con, params=(cal[0], cal[-1]))
        df = df[df["d"].isin(set(cal))]
        return cls.from_frame(df, cal)

    # ── 조회 ──
    def col(self, ticker: str) -> int | None:
        return self._col.get(str(ticker))

    def breaks_between(self, j: int, a: int, b: int) -> int:
        """종목 j 의 위치 a 다음 날 ~ b 사이 불연속 수 (a < b)."""
        if b <= a:
            return 0
        return int(self._break_cs[b, j] - self._break_cs[a, j])

    def universe_median(self, e: int, x: int) -> float:
        """진입 위치 e 시가 → 청산 위치 x 종가의 전 종목 중위 수익률(%).

        시가·종가가 모두 있고, 구간에 수정주가 불연속이 없는 종목만 센다.
        """
        key = (e, x)
        if key in self._median_cache:
            return self._median_cache[key]
        o = self._o[e]
        c = self._c[x]
        ok = np.isfinite(o) & np.isfinite(c) & (o > 0) & (c > 0)
        if e >= 1:
            nb = self._break_cs[x] - self._break_cs[e - 1]
        else:
            nb = self._break_cs[x]
        ok &= nb == 0
        val = float(np.median(c[ok] / o[ok] - 1.0) * 100.0) if ok.any() else float("nan")
        self._median_cache[key] = val
        return val


def ticker_markets(store) -> dict[str, str]:
    """종목코드 → 소속 시장('KOSPI'/'KOSDAQ'). 비활성 종목 포함."""
    import sqlite3
    from contextlib import closing
    with closing(sqlite3.connect(store.path, timeout=30.0)) as con:
        rows = con.execute("SELECT ticker, market FROM tickers").fetchall()
    return {str(t): (m or "").upper() for t, m in rows}


# ══════════════════════════ 지수 ══════════════════════════
def index_frames_from_df(frames: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    """{시장: DataFrame(Open, Close, index=날짜)} → index 를 'YYYY-MM-DD' 로."""
    out = {}
    for k, f in frames.items():
        if f is None or f.empty:
            continue
        g = f[[c for c in ("Open", "Close") if c in f.columns]].copy()
        g.index = [_ds(i) for i in g.index]
        g = g[~pd.Index(g.index).duplicated(keep="last")]
        out[str(k).upper()] = g.astype("float64")
    return out


def load_index_history(names=("KOSPI", "KOSDAQ"), start: str = "2024-01-01",
                       end: str | None = None,
                       cache_dir: str | Path | None = None,
                       allow_network: bool = True) -> dict[str, pd.DataFrame]:
    """지수 일봉 히스토리 {이름: DataFrame(Open, Close)}.

    FinanceDataReader 로 받고, cache_dir 이 있으면 CSV 로 남긴다. 네트워크가
    실패하면 캐시를 쓴다. 둘 다 없으면 그 지수는 결과에서 빠진다 —
    호출부는 지수 대비 초과수익을 NaN 으로 둔다(0 으로 채우지 않는다).

    **신선도는 여기서 판정하지 않는다.** 채점은 날짜로 정렬해 조회하므로
    없는 날짜는 NaN 이 된다. 기준일 신선도 검사는 `load_index` 쪽 책임이다.
    """
    from .data import INDEX_SYMBOLS
    out: dict[str, pd.DataFrame] = {}
    cdir = Path(cache_dir) if cache_dir else None
    for name in names:
        key = str(name).upper()
        sym = INDEX_SYMBOLS.get(key, key)
        cache = cdir / f"index_{sym}.csv" if cdir else None
        df = None
        if allow_network:
            try:
                import FinanceDataReader as fdr
                df = fdr.DataReader(sym, start, end)
                if df is not None and not df.empty and cache is not None:
                    cache.parent.mkdir(parents=True, exist_ok=True)
                    df[["Open", "Close"]].to_csv(cache, encoding="utf-8")
            except Exception as exc:  # noqa: BLE001
                log.warning("지수 히스토리 조회 실패 %s(%s): %s: %s",
                            key, sym, type(exc).__name__, exc)
                df = None
        if (df is None or df.empty) and cache is not None and cache.exists():
            df = pd.read_csv(cache, index_col=0)
        if df is not None and not df.empty:
            out[key] = df
    return index_frames_from_df(out)


# ══════════════════════════ 채점 ══════════════════════════
def score_events(events: pd.DataFrame, panel: PricePanel,
                 index: dict[str, pd.DataFrame] | None = None,
                 markets: dict[str, str] | None = None,
                 horizons=SCORE_HORIZONS,
                 cost_pct: float = ROUND_TRIP_COST_PCT,
                 block_limit_up: bool = True) -> pd.DataFrame:
    """이벤트별 채점 (wide).

    events : 최소 컬럼 ticker, event_date (또는 d). `events_only()` 결과를
             그대로 넣으면 된다. 다른 컬럼은 통과한다.
    index  : `load_index_history()` 결과. 없으면 지수 대비 = NaN.
    markets: `ticker_markets()` 결과.

    반환 = 입력 컬럼 + entry_date, entry_price, entry_status, market,
           그리고 보유기간 h 마다
           exit_date_h, exit_price_h, status_h,
           ret_h(총수익 %), net_h(비용 차감 %),
           idx_h(지수 %), ex_idx_h(net − idx), med_h(전종목 중위 %),
           ex_med_h(net − med)
    """
    hs = tuple(int(h) for h in horizons)
    rows = []
    if events is None or events.empty:
        return pd.DataFrame()
    idx = index or {}
    mk = markets or {}
    lim_up = 1.0 + ENTRY_LIMIT_UP_PCT / 100.0
    lim_break = PRICE_JUMP_BREAK_PCT / 100.0 + 1e-6
    n = len(panel.dates)

    for r in events.to_dict("records"):
        t = str(r.get("ticker"))
        ed = _ds(r.get("event_date") or r.get("d"))
        rec = dict(r)
        rec["market"] = mk.get(t, "")
        p = panel.pos.get(ed)
        if p is None:
            p = _pos(panel.dates, ed)
        e = p + 1
        j = panel.col(t)
        status = ENTRY_OK
        entry = np.nan
        if e >= n or p < 0:
            status = ENTRY_PENDING
        elif j is None or not np.isfinite(panel._c[e, j]) and not np.isfinite(panel._o[e, j]):
            status = ENTRY_SUSPENDED
        else:
            entry = panel._o[e, j]
            if not (np.isfinite(entry) and entry > 0):
                status, entry = ENTRY_NO_OPEN, np.nan
            elif block_limit_up:
                prev = panel._cf[p, j] if p >= 0 else np.nan
                if np.isfinite(prev) and prev > 0 and entry >= prev * lim_up:
                    status = ENTRY_LIMIT_UP
            # 진입 시가 자체가 불연속(권리락 당일 원시 가격)이면 수정주가
            # 구간이 깨진 것이다. 청산 판정에서 ADJ_BREAK 로 잡힌다.
        rec["entry_date"] = panel.dates[e] if e < n else None
        rec["entry_price"] = float(entry) if np.isfinite(entry) else np.nan
        rec["entry_status"] = status

        mi = idx.get(rec["market"])
        for h in hs:
            x = e + h - 1
            out = {f"exit_date_{h}": None, f"exit_price_{h}": np.nan,
                   f"ret_{h}": np.nan, f"net_{h}": np.nan,
                   f"idx_{h}": np.nan, f"ex_idx_{h}": np.nan,
                   f"med_{h}": np.nan, f"ex_med_{h}": np.nan}
            if status == ENTRY_PENDING or x >= n:
                out[f"status_{h}"] = ST_PENDING
                rec.update(out)
                continue
            if status != ENTRY_OK:
                out[f"status_{h}"] = ST_NO_ENTRY
                rec.update(out)
                continue
            st = ST_OK
            px = panel._c[x, j]
            xd = panel.dates[x]
            if not (np.isfinite(px) and px > 0):
                # h일째 행이 없다 = 거래정지·상장폐지. 마지막 거래 가격으로 종료.
                seg = panel._c[e:x + 1, j]
                valid = np.where(np.isfinite(seg) & (seg > 0))[0]
                if len(valid):
                    k = e + int(valid[-1])
                    px, xd = panel._c[k, j], panel.dates[k]
                else:
                    px, xd = entry, panel.dates[e]  # 진입일 종가도 없음
                st = ST_HALTED
            # 수정주가 불연속: 진입 시가 vs 전일 종가, 그리고 구간 내 종가.
            brk = panel.breaks_between(j, e, x) > 0
            prev = panel._cf[p, j] if p >= 0 else np.nan
            if np.isfinite(prev) and prev > 0 and abs(entry / prev - 1.0) > lim_break:
                brk = True
            if np.isfinite(panel._c[e, j]) and abs(panel._c[e, j] / entry - 1.0) > lim_break:
                brk = True
            if brk:
                out[f"status_{h}"] = ST_ADJ_BREAK
                out[f"exit_date_{h}"] = xd
                rec.update(out)
                continue
            ret = (px / entry - 1.0) * 100.0
            net = ret - float(cost_pct)
            med = panel.universe_median(e, x)
            iv = np.nan
            if mi is not None:
                ed_, xd_ = panel.dates[e], panel.dates[x]
                if ed_ in mi.index and xd_ in mi.index:
                    o_i = mi.at[ed_, "Open"] if "Open" in mi.columns else np.nan
                    c_i = mi.at[xd_, "Close"]
                    if not (np.isfinite(o_i) and o_i > 0):
                        # 지수 시가가 없으면 전일 종가를 진입 기준으로 쓴다.
                        pd_ = panel.dates[p] if p >= 0 else None
                        o_i = mi.at[pd_, "Close"] if pd_ in mi.index else np.nan
                    if np.isfinite(o_i) and o_i > 0 and np.isfinite(c_i):
                        iv = (c_i / o_i - 1.0) * 100.0
            out.update({
                f"exit_date_{h}": xd, f"exit_price_{h}": float(px),
                f"status_{h}": st, f"ret_{h}": ret, f"net_{h}": net,
                f"idx_{h}": iv, f"ex_idx_{h}": net - iv if np.isfinite(iv) else np.nan,
                f"med_{h}": med, f"ex_med_{h}": net - med if np.isfinite(med) else np.nan,
            })
            rec.update(out)
        rows.append(rec)
    return pd.DataFrame(rows)


# ══════════════════════════ 표 칸 ══════════════════════════
def stat_cell(values, min_n: int = MIN_SCORED_FOR_JUDGMENT) -> dict:
    """표 한 칸: 표본 수·평균·중앙값·승률(>0 비율). NaN 은 버린다.

    표본이 min_n 미만이면 `enough=False` — 표시는 "표본 부족" 으로 한다.
    """
    s = pd.Series(values, dtype="float64").dropna()
    n = int(len(s))
    if n == 0:
        return {"n": 0, "mean": None, "median": None, "win": None,
                "enough": False}
    return {"n": n, "mean": float(s.mean()), "median": float(s.median()),
            "win": float((s > 0).mean() * 100.0), "enough": n >= min_n}


def fmt_cell(c: dict) -> str:
    """`stat_cell` 결과 → 표 문자열. 표본 부족이면 숫자 대신 문구."""
    if not c or not c.get("enough"):
        return f"표본 부족 (n={c.get('n', 0) if c else 0})"
    return (f"평균 {c['mean']:+.2f} · 중앙 {c['median']:+.2f} · "
            f"승률 {c['win']:.0f}% (n={c['n']})")
