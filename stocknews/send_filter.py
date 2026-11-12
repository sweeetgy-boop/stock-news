# -*- coding: utf-8 -*-
"""[B] 추천 필터 — 선정이 끝난 recos 에서 '발송·추적 대상'을 가른다.

**추천 선정과 recos 기록은 건드리지 않는다.** 이 모듈은 recos 를 읽기만
한다. 플래그를 켜든 끄든 recos 테이블 행은 같다. 필터로 빠진 자리를
차순위로 메우지 않는다 — 추천 수가 줄어드는 것이 정상이다.

적용 순서 (고정)
---------------
  ① SEQ 제외      EXCLUDE_SEQ_FROM_SEND   slot=SEQ
  ② 보충 제외     EXCLUDE_FILL_FROM_SEND  slot=FILL / FILL*(섹터 제한 해제 보충)
  ③ 국면 TREND 절반  REGIME_TREND_CUT(C 소유) 이고 그날 국면이 "하락" 일 때만.
                  ①② 뒤에 남은 TREND 슬롯 n개 중 **내림(n // 2)** 개를 추세
                  점수 높은 순(동점이면 순위 순)으로 남긴다. 3→1, 2→1, 1→0.
                  국면이 None(기록 없음)이면 자르지 않는다.
  ④ 재추천 제한   RECO_COOLDOWN_DAYS > 0. [0] `events.assign_events` 로 판정한
                  이벤트의 시작 행이 아니면(= 기간 중 재추천) 새 추천이 아니라
                  '추천 유지 n일째' 한 줄로만 보인다.

이벤트 판정은 두 계열이다 (2026-11-12 분리).

  측정용  recos **전체** → [0] assign_events. A 의 채점, 이 모듈의
          `score_table`(발송 vs 제외 성적)이 쓴다. 필터와 무관한 표본 정의.
  발송용  ①②③ 을 **통과한 행만** → [0] assign_events(window=재추천 제한).
          ④ 와 reco_events 가 쓴다. 그래서 '추천 유지' 는 실제로 새 추천으로
          나간 적 있는 종목에만 붙고, 한 번도 발송되지 않은 종목(이벤트 시작
          행이 ①~③ 으로 빠졌던 종목)이 필터를 통과하면 **새 추천**이다.

행 상태
-------
  new       새 추천. 발송하고 추적 편입 대상(is_new_event=True)
  hold      재추천 제한 기간 중 재추천. "추천 유지 n일째" 한 줄로만 보인다.
            추적은 이미 이벤트 시작일에 편입됐다(is_new_event=False).
  excluded  ①②③ 으로 빠짐. 발송·추적 편입 없음. 조용히 채점만 한다.

`targets` = new + hold (순위 순). `v2_hooks.send_targets` 가 이것을 돌린다.
RECO_COOLDOWN_DAYS = 0 이면 모든 행이 new(is_new_event=True, 1일째)다.

기록
----
  reco_events     : 재추천 제한이 켜져 있을 때만. 그 날짜까지의 이벤트를
                    지우고 결정적으로 다시 쓴다 → 같은 날 재실행에도 동일.
  reco_filter_log : 플래그가 하나라도 켜져 있을 때만. 그날 hold·excluded 행.
                    그날 행을 지우고 다시 쓴다 → 동일.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from . import config as C
from . import events as ev

log = logging.getLogger(__name__)

__all__ = ["REASON_SEQ", "REASON_FILL", "REASON_REGIME", "REASON_COOLDOWN",
           "REASONS", "FILL_SLOTS", "current_flags", "any_on", "regime_keep",
           "slot_score", "floor_relaxed", "DayResult", "evaluate",
           "evaluate_days", "record", "backfill_events", "hold_lines",
           "exclusion_line_of", "render_send", "score_table",
           "render_score_table"]

REASON_SEQ = "SEQ"
REASON_FILL = "보충"
REASON_REGIME = "국면"
REASON_COOLDOWN = "재추천 제한"
REASONS = (REASON_SEQ, REASON_FILL, REASON_REGIME, REASON_COOLDOWN)

FILL_SLOTS = frozenset({"FILL", "FILL*"})
REGIME_CUT_LABEL = "하락"

NEW, HOLD, EXCLUDED = "new", "hold", "excluded"

_RECO_COLS = ("d", "rank", "ticker", "name", "price", "slot", "grade",
              "value_score", "trend_score", "reason")


# ══════════════════════════ 플래그 ══════════════════════════
def current_flags() -> dict:
    """config 플래그를 **호출 시점에** 읽는다(스모크·시뮬레이션이 바꿔 끼운다)."""
    try:
        cd = int(getattr(C, "RECO_COOLDOWN_DAYS", 0) or 0)
    except (TypeError, ValueError):
        cd = 0
    return {
        "cooldown_days": max(cd, 0),
        "exclude_seq": bool(getattr(C, "EXCLUDE_SEQ_FROM_SEND", False)),
        "exclude_fill": bool(getattr(C, "EXCLUDE_FILL_FROM_SEND", False)),
        # ③ 의 플래그는 C 묶음이 config C 구역에 정의한다. 없으면 OFF.
        "regime_cut": bool(getattr(C, "REGIME_TREND_CUT", False)),
    }


def any_on(flags: dict | None = None) -> bool:
    f = current_flags() if flags is None else flags
    return bool(f["cooldown_days"] > 0 or f["exclude_seq"]
                or f["exclude_fill"] or f["regime_cut"])


def regime_keep(n: int) -> int:
    """하락 국면에서 남길 TREND 수. 절반 **내림** (3→1, 2→1, 1→0)."""
    return max(int(n), 0) // 2


def slot_score(slot, value_score, trend_score) -> float | None:
    """슬롯 기준 점수. VALUE=매집, TREND=추세, 그 외(SEQ·FILL)=둘 중 큰 값.

    select_recommendations 의 슬롯별 정렬 키와 같다.
    """
    def f(x):
        try:
            x = float(x)
        except (TypeError, ValueError):
            return None
        return x if x == x else None
    v, t = f(value_score), f(trend_score)
    if slot == "VALUE":
        return v
    if slot == "TREND":
        return t
    vals = [x for x in (v, t) if x is not None]
    return max(vals) if vals else None


def floor_relaxed(row, cfg=None) -> bool:
    """VALUE/TREND 슬롯이 하한 완화(트랙 발송 하한 − 1.5)로 채워졌는가.

    `select_recommendations` 의 슬롯 하한은 `threshold - 1.5` 다. 점수가
    threshold 미만이면 완화된 하한 덕에 들어온 것이다. 표시 전용.
    """
    from .config import DEFAULT
    g = (cfg or DEFAULT).gate
    slot = row.get("slot")
    try:
        if slot == "VALUE":
            return float(row.get("value_score")) < g.value_threshold
        if slot == "TREND":
            return float(row.get("trend_score")) < g.trend_threshold
    except (TypeError, ValueError):
        return False
    return False


# ══════════════════════════ 판정 ══════════════════════════
@dataclass
class DayResult:
    d: str
    flags: dict
    regime: str | None
    rows: list[dict] = field(default_factory=list)   # 그날 recos 전 행 (순위 순)

    def _st(self, s):
        return [r for r in self.rows if r["status"] == s]

    @property
    def new(self) -> list[dict]:
        return self._st(NEW)

    @property
    def hold(self) -> list[dict]:
        return self._st(HOLD)

    @property
    def excluded(self) -> list[dict]:
        return self._st(EXCLUDED)

    @property
    def targets(self) -> list[dict]:
        return [r for r in self.rows if r["status"] in (NEW, HOLD)]

    @property
    def counts(self) -> dict:
        c = {k: 0 for k in REASONS}
        for r in self.rows:
            if r.get("filter_reason"):
                c[r["filter_reason"]] += 1
        return c

    def summary(self) -> dict:
        return {"d": self.d, "flags": dict(self.flags), "regime": self.regime,
                "recos": len(self.rows), "new": len(self.new),
                "hold": len(self.hold), "excluded": len(self.excluded),
                "counts": self.counts}


def _load_recos(store, until: str | None = None) -> pd.DataFrame:
    q = "SELECT " + ",".join(_RECO_COLS) + " FROM recos"
    params: tuple = ()
    if until:
        q += " WHERE d<=?"
        params = (str(until)[:10],)
    q += " ORDER BY d, rank"
    with closing(sqlite3.connect(store.path, timeout=30.0)) as con:
        return pd.read_sql_query(q, con, params=params)


def _holidays(store) -> set[str]:
    out: set[str] = set()
    try:
        out |= set(store.known_non_trading_days())
    except Exception:  # noqa: BLE001
        pass
    try:
        with closing(sqlite3.connect(store.path, timeout=30.0)) as con:
            out |= {d for (d,) in con.execute("SELECT d FROM holidays")}
    except Exception:  # noqa: BLE001
        pass
    return out


def _calendar(store, recos: pd.DataFrame) -> list[str]:
    """[0] 거래일 달력 ∪ recos 날짜.

    recos 날짜는 daily 가 돈 거래일이다. 그날이 부분 적재로 complete_dates
    에서 빠졌어도 '추천일' 로는 세야 day_in_window 가 어긋나지 않는다.
    """
    cal = set(ev.trading_calendar(store))
    if recos is not None and not recos.empty:
        cal |= {str(x)[:10] for x in recos["d"]}
    return sorted(cal)


def _assign(recos: pd.DataFrame, cal: list[str], window: int,
            holidays=frozenset()) -> pd.DataFrame:
    """이벤트 판정. window ≤ 0 이면 제한 없음 = 모든 행이 각자 새 이벤트."""
    if window > 0:
        return ev.assign_events(recos, cal, window=window, holidays=holidays)
    a = recos.copy()
    a["d"] = a["d"].map(lambda x: str(x)[:10])
    a["ticker"] = a["ticker"].astype(str)
    a["event_id"] = a["ticker"] + ":" + a["d"]
    a["event_date"] = a["d"]
    a["is_event"] = True
    a["day_in_window"] = 1
    a["window_end"] = a["d"]
    a["event_slot"] = a.get("slot")
    a["event_grade"] = a.get("grade")
    a["slot_changed"] = False
    a["grade_changed"] = False
    return a


def _regime(store, d) -> str | None:
    try:
        from .v2_hooks import regime_on
        return regime_on(store, d)
    except Exception as exc:  # noqa: BLE001 - 국면이 없으면 자르지 않는다
        log.warning("국면 조회 실패 %s — 국면 없음으로 본다 (%s: %s)",
                    d, type(exc).__name__, exc)
        return None


def _clean(v):
    """pandas/numpy 스칼라 → 파이썬 기본형 (dict 로 넘길 때)."""
    if v is None:
        return None
    if hasattr(v, "item"):
        try:
            v = v.item()
        except (ValueError, AttributeError):
            pass
    if isinstance(v, float) and v != v:
        return None
    return v


def _row_dict(r: dict, cooldown: int) -> dict:
    out = {k: _clean(r.get(k)) for k in _RECO_COLS}
    out["ticker"] = str(out["ticker"])
    out["d"] = str(out["d"])[:10]
    out["rank"] = int(out["rank"])
    on = cooldown > 0
    out["is_new_event"] = bool(r.get("is_event")) if on else True
    out["day_in_window"] = int(r.get("day_in_window") or 1) if on else 1
    out["event_date"] = str(r.get("event_date") or out["d"])[:10]
    out["window_end"] = _clean(r.get("window_end")) if on else out["d"]
    out["event_slot"] = _clean(r.get("event_slot"))
    out["event_grade"] = _clean(r.get("event_grade"))
    out["slot_changed"] = bool(r.get("slot_changed")) if on else False
    out["grade_changed"] = bool(r.get("grade_changed")) if on else False
    out["status"] = None
    out["filter_reason"] = None
    return out


def _filter_123(rows: list[dict], flags: dict, regime: str | None) -> list[dict]:
    """한 거래일 행에 ①②③ 만 건다(④ 는 발송용 이벤트 판정 뒤). rows 는 순위 순."""
    def alive():
        return [r for r in rows if r["status"] is None]

    def excl(r, why):
        r["status"], r["filter_reason"] = EXCLUDED, why

    if flags["exclude_seq"]:                                       # ①
        for r in alive():
            if r["slot"] == "SEQ":
                excl(r, REASON_SEQ)
    if flags["exclude_fill"]:                                      # ②
        for r in alive():
            if r["slot"] in FILL_SLOTS:
                excl(r, REASON_FILL)
    if flags["regime_cut"] and regime == REGIME_CUT_LABEL:         # ③
        tr = [r for r in alive() if r["slot"] == "TREND"]
        tr.sort(key=lambda r: (-(r["trend_score"] if r["trend_score"] is not None
                                 else float("-inf")), r["rank"]))
        for r in tr[regime_keep(len(tr)):]:
            excl(r, REASON_REGIME)
    for r in rows:
        r["regime"] = regime
    return rows


def _apply_cooldown(rows: list[dict], flags: dict) -> list[dict]:
    """④ — ①②③ 을 통과한 행. 발송용 이벤트의 시작 행이 아니면 '추천 유지'."""
    for r in rows:
        if r["status"] is not None:
            continue
        if flags["cooldown_days"] > 0 and not r["is_new_event"]:
            r["status"], r["filter_reason"] = HOLD, REASON_COOLDOWN
        else:
            r["status"] = NEW
    return rows


def _apply_day(rows: list[dict], flags: dict, regime: str | None) -> list[dict]:
    """한 거래일 행에 ①②③④ 를 순서대로 건다(이벤트 필드는 이미 채워진 행)."""
    return _apply_cooldown(_filter_123(rows, flags, regime), flags)


_EVENT_FIELDS = ("is_event", "day_in_window", "event_date", "window_end",
                 "event_slot", "event_grade", "slot_changed", "grade_changed",
                 "event_id")


def _send_track(store, recos: pd.DataFrame, fl: dict, cal: list[str]
                ) -> tuple[dict[str, list[dict]], pd.DataFrame]:
    """발송용 계열. (날짜별 ①②③ 판정 행, 통과 행의 이벤트 판정).

    ①②③ 은 그날 행만 보고 정해지므로 날짜별로 먼저 건다. 통과한 행만
    [0] assign_events 에 넣는다 — 이벤트 = 실제로 새 추천으로 나간 날.
    """
    pre: dict[str, list[dict]] = {}
    for d, g in recos.sort_values(["d", "rank"], kind="mergesort").groupby(
            "d", sort=True):
        rows = [_row_dict(r, 0) for r in g.to_dict("records")]
        pre[str(d)[:10]] = _filter_123(rows, fl, _regime(store, d))
    passed = [r for rows in pre.values() for r in rows if r["status"] is None]
    if not passed:
        return pre, pd.DataFrame()
    pdf = pd.DataFrame([{k: r[k] for k in _RECO_COLS} for r in passed])
    a = _assign(pdf, cal, fl["cooldown_days"], _holidays(store)
                if fl["cooldown_days"] > 0 else frozenset())
    return pre, a


def evaluate_days(store, days=None, flags: dict | None = None,
                  calendar: list[str] | None = None,
                  recos: pd.DataFrame | None = None) -> dict[str, DayResult]:
    """여러 거래일을 한 번에 판정한다 (이벤트 판정은 1회).

    days=None 이면 recos 의 모든 날짜. 판정은 그 날짜까지의 recos 만 본다
    ([0] assign_events 는 앞에서부터 순서대로 판정하므로 뒤 날짜 행이
    앞 날짜의 판정을 바꾸지 않는다).
    """
    fl = current_flags() if flags is None else dict(flags)
    want = None if days is None else sorted({str(x)[:10] for x in days})
    if recos is None:
        recos = _load_recos(store, until=want[-1] if want else None)
    if recos is None or recos.empty:
        return {d: DayResult(d, fl, _regime(store, d), []) for d in (want or [])}
    cal = calendar if calendar is not None else _calendar(store, recos)
    pre, a = _send_track(store, recos, fl, cal)
    amap = ({} if a is None or a.empty else
            {(str(r["d"])[:10], str(r["ticker"])): r for r in a.to_dict("records")})
    out: dict[str, DayResult] = {}
    for d in (want if want is not None else sorted(pre)):
        rows = pre.get(d, [])
        for r in rows:
            if r["status"] is None:                    # ①②③ 통과 → 발송용 이벤트
                ev_r = _row_dict(amap[(d, r["ticker"])], fl["cooldown_days"])
                for k in ("is_new_event", "day_in_window", "event_date",
                          "window_end", "event_slot", "event_grade",
                          "slot_changed", "grade_changed"):
                    r[k] = ev_r[k]
        regime = rows[0]["regime"] if rows else _regime(store, d)
        out[d] = DayResult(d, fl, regime, _apply_cooldown(rows, fl))
    return out


def evaluate(store, d, flags: dict | None = None) -> DayResult:
    ds = str(d)[:10]
    return evaluate_days(store, [ds], flags)[ds]


# ══════════════════════════ 기록 ══════════════════════════
def _slot_label(s) -> str:
    from .renderer import SLOT_LABEL
    return SLOT_LABEL.get(str(s), str(s))


def _event_rows(assigned: pd.DataFrame, window: int) -> list[tuple]:
    rows = []
    for eid, g in assigned.sort_values(["d", "rank"]).groupby("event_id", sort=True):
        g = g.sort_values("d")
        st = g.iloc[0]
        last = g.iloc[-1]
        changes = []
        prev = st
        for _, r in g.iloc[1:].iterrows():
            bits = []
            if r["slot"] != prev["slot"]:
                bits.append(f"슬롯 {_slot_label(prev['slot'])}→{_slot_label(r['slot'])}")
            if r["grade"] != prev["grade"]:
                bits.append(f"등급 {prev['grade']}→{r['grade']}")
            if bits:
                changes.append(f"{r['d'][5:]} " + " · ".join(bits))
            prev = r
        rows.append((
            str(st["ticker"]), str(st["event_date"]), _clean(st["window_end"]),
            int(window), _clean(st["name"]), _clean(st["slot"]),
            _clean(st["grade"]),
            slot_score(st["slot"], st["value_score"], st["trend_score"]),
            str(last["d"]), _clean(last["slot"]), _clean(last["grade"]),
            int(len(g)), "; ".join(changes) or None))
    return rows


def _write_events(store, until: str, window: int,
                  recos: pd.DataFrame | None = None,
                  flags: dict | None = None) -> int:
    """until 까지의 recos 로 **발송용** 이벤트를 다시 계산해 reco_events 를 갈아 끼운다.

    ①②③ 을 통과한 행만 이벤트가 된다(플래그는 flags, 없으면 현재 설정).
    """
    recos = _load_recos(store, until) if recos is None else recos
    if recos.empty:
        return 0
    fl = dict(current_flags() if flags is None else flags)
    fl["cooldown_days"] = int(window)
    _pre, a = _send_track(store, recos, fl, _calendar(store, recos))
    rows = _event_rows(a, window) if a is not None and not a.empty else []
    with closing(sqlite3.connect(store.path, timeout=30.0)) as con:
        con.execute("DELETE FROM reco_events WHERE event_date<=?", (until,))
        con.executemany(
            "INSERT INTO reco_events(ticker,event_date,window_end,window_days,"
            "name,slot,grade,score,last_d,last_slot,last_grade,days_listed,"
            "changes) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        con.commit()
    return len(rows)


def _write_log(store, res: DayResult) -> int:
    rows = [(res.d, r["ticker"], r["name"], r["rank"], r["slot"], r["grade"],
             r["filter_reason"], res.regime,
             r["event_date"] if r["status"] == HOLD else None,
             r["day_in_window"] if r["status"] == HOLD else None)
            for r in res.rows if r["status"] in (HOLD, EXCLUDED)]
    with closing(sqlite3.connect(store.path, timeout=30.0)) as con:
        con.execute("DELETE FROM reco_filter_log WHERE d=?", (res.d,))
        con.executemany(
            "INSERT INTO reco_filter_log(d,ticker,name,rank,slot,grade,reason,"
            "regime,event_date,day_in_window) VALUES(?,?,?,?,?,?,?,?,?,?)", rows)
        con.commit()
    return len(rows)


def record(store, d, flags: dict | None = None) -> dict | None:
    """그 거래일의 판정을 기록한다. 플래그가 전부 OFF 면 아무것도 쓰지 않는다.

    recos 는 읽기만 한다. 같은 날 몇 번을 돌려도 두 테이블은 같다.
    """
    fl = current_flags() if flags is None else dict(flags)
    if not any_on(fl):
        return None
    ds = str(d)[:10]
    recos = _load_recos(store, ds)
    res = evaluate_days(store, [ds], fl, recos=recos)[ds]
    n_ev = 0
    if fl["cooldown_days"] > 0:
        n_ev = _write_events(store, ds, fl["cooldown_days"], recos=recos,
                             flags=fl)
    n_log = _write_log(store, res)
    out = res.summary()
    out.update({"events_rows": n_ev, "log_rows": n_log})
    return out


def backfill_events(store, window: int | None = None) -> dict:
    """과거 recos 전체로 reco_events 를 소급 구성한다 (1회성, nightly 밖).

    window 를 주지 않으면 RECO_COOLDOWN_DAYS, 그것도 0 이면 [0] 의
    EVENT_WINDOW_DAYS(20) — '켜는 시점' 의 값으로 미리 만든다.
    """
    t0 = time.time()
    w = int(window) if window else (current_flags()["cooldown_days"]
                                    or int(C.EVENT_WINDOW_DAYS))
    recos = _load_recos(store)
    if recos.empty:
        return {"window": w, "recos": 0, "days": 0, "events": 0,
                "elapsed_sec": round(time.time() - t0, 2)}
    last = str(recos["d"].max())[:10]
    n = _write_events(store, last, w, recos=recos)
    return {"window": w, "recos": int(len(recos)),
            "days": int(recos["d"].nunique()), "first": str(recos["d"].min()),
            "last": last, "events": n,
            "elapsed_sec": round(time.time() - t0, 2)}


# ══════════════════════════ 표시 ══════════════════════════
def hold_lines(res: DayResult) -> list[str]:
    """재추천 제한 기간 중 오늘도 목록에 있는 종목 — 종목당 한 줄.

    목록에서 빠진 종목은 rows 에 없으므로 표시되지 않는다.
    """
    from .renderer import _e
    out = []
    for r in res.hold:
        s = (f"• <b>{_e(str(r['name']))}</b> ({r['ticker']}) "
             f"추천 유지 {int(r['day_in_window'])}일째")
        if r["slot_changed"]:
            s += f" · 슬롯 {_slot_label(r['event_slot'])}→{_slot_label(r['slot'])}"
        if r["grade_changed"]:
            s += f" · 등급 {r['event_grade']}→{r['grade']}"
        out.append(s)
    return out


def exclusion_line_of(res: DayResult) -> str | None:
    """"오늘 제외 — SEQ n, 보충 n, 재추천 제한 n" (국면 컷이 켜져 있으면 + 국면 n)."""
    if not any_on(res.flags):
        return None
    c = res.counts
    parts = [f"SEQ {c[REASON_SEQ]}", f"보충 {c[REASON_FILL]}",
             f"재추천 제한 {c[REASON_COOLDOWN]}"]
    if res.flags.get("regime_cut"):
        parts.append(f"국면 {c[REASON_REGIME]}")
    return "오늘 제외 — " + ", ".join(parts)


def render_send(res: DayResult, asof, send_dow_label: str = "일요일",
                size: dict | None = None) -> str:
    """필터를 거친 주간 추천 메시지. 플래그 ON 일 때만 쓰인다.

    새 추천이 0개면 "오늘 새 추천 없음" 을 보낸다(조용히 넘어가지 않는다).
    """
    from .renderer import _e, render_reco_stored
    extra = hold_lines(res)
    line = exclusion_line_of(res)
    if line:
        extra = extra + [line]
    new = res.new
    if not new:
        out = [f"🗓 <b>[주간 추천]</b> 기준일 {_e(res.d)}",
               "• 오늘 새 추천 없음"]
        if extra:
            out += [""] + extra
        out += ["", f"⏰ {asof:%Y-%m-%d %H:%M:%S}"]
        return "\n".join(out)
    marks = None
    if res.flags.get("exclude_fill"):
        marks = {r["ticker"]: "하한 완화 적용" for r in new if floor_relaxed(r)}
    df = pd.DataFrame(new)
    return render_reco_stored(df, asof, send_dow_label, size=size,
                              marks=marks, extra_lines=extra)


# ══════════════════════════ 제외 종목 검증 (조용한 채점) ══════════════════════════
GROUP_SENT = "발송"


def _group_of(r: dict) -> str:
    if r["status"] == NEW:
        return GROUP_SENT
    return f"미발송·{r['filter_reason']}"


def score_table(store, horizon: int = 20, flags: dict | None = None,
                index: dict | None = None, cache_dir=None,
                allow_network: bool = True) -> dict:
    """발송된 추천 vs 사유별 제외 종목의 h일 초과수익 (국면별).

    채점 단위 = [0] 이벤트(EVENT_WINDOW_DAYS). 같은 그룹에서 같은 이벤트의
    두 번째 이후 행은 세지 않는다 — 매일 반복 등재를 20번 세지 않게.
    그 그룹에 처음 들어온 날을 추천일로 보고 [0] score_events 로 채점한다
    (익일 시가 진입). 제외 종목은 '미발송' 으로 구분한다 — 발송은 없다.
    """
    fl = current_flags() if flags is None else dict(flags)
    recos = _load_recos(store)
    if recos.empty:
        return {"horizon": horizon, "flags": fl, "rows": [], "units": 0}
    cal = _calendar(store, recos)
    res = evaluate_days(store, None, fl, calendar=cal, recos=recos)
    ev0 = ev.assign_events(recos, cal, window=int(C.EVENT_WINDOW_DAYS))
    eid = {(r.d, r.ticker): r.event_id for r in ev0.itertuples()}

    units, seen = [], set()
    base_seen = set()
    for d in sorted(res):
        for r in res[d].rows:
            e = eid.get((d, r["ticker"]))
            if e not in base_seen:          # 필터 전 전체 = [0] 이벤트
                base_seen.add(e)
                units.append({"group": "필터 전 전체", "ticker": r["ticker"],
                              "event_date": d, "regime": r.get("regime")})
            g = _group_of(r)
            if (g, e) in seen:
                continue
            seen.add((g, e))
            units.append({"group": g, "ticker": r["ticker"], "event_date": d,
                          "regime": r.get("regime")})
    u = pd.DataFrame(units)
    first = u["event_date"].min()
    i0 = max(0, cal.index(first) - 5) if first in cal else 0
    panel = ev.PricePanel.from_store(store, since=cal[i0])
    if index is None:
        index = ev.load_index_history(start=cal[i0], cache_dir=cache_dir,
                                      allow_network=allow_network)
    sc = ev.score_events(u, panel, index, ev.ticker_markets(store),
                         horizons=(int(horizon),))
    h = int(horizon)
    order = (["필터 전 전체", GROUP_SENT]
             + [f"미발송·{x}" for x in REASONS])
    sc["regime_label"] = sc["regime"].map(lambda x: x if x else "국면 없음")
    labels = sorted(set(sc["regime_label"]))
    regimes = labels if len(labels) == 1 else ["전체"] + labels
    rows = []
    for g in order:
        sg = sc[sc["group"] == g]
        if sg.empty:
            continue
        for rg in regimes:
            s = sg if rg == "전체" else sg[sg["regime_label"] == rg]
            rows.append({
                "group": g, "regime": rg, "units": int(len(s)),
                "pending": int((s[f"status_{h}"] == ev.ST_PENDING).sum()),
                "ex_med": ev.stat_cell(s[f"ex_med_{h}"]),
                "ex_idx": ev.stat_cell(s[f"ex_idx_{h}"]),
            })
    return {"horizon": h, "flags": fl, "rows": rows, "units": int(len(u)),
            "first": str(first), "last": str(u["event_date"].max())}


def render_score_table(tab: dict) -> list[str]:
    """weekly 리포트용 블록. 20건 미만 칸은 "표본 부족"."""
    h = tab.get("horizon", 20)
    out = [f"━━ 추천 필터 검증 · {h}일 초과수익 ━━",
           "• 미발송 = 필터로 빠진 종목 (발송 없음 · 조용히 채점만)"]
    rows = tab.get("rows") or []
    if not rows:
        out.append("• 채점할 추천 없음")
        return out
    for r in rows:
        out.append(f"• {r['group']} [{r['regime']}] 이벤트 {r['units']}건"
                   f" (미도래 {r['pending']})")
        out.append(f"  중위 대비: {ev.fmt_cell(r['ex_med'])}")
        out.append(f"  지수 대비: {ev.fmt_cell(r['ex_idx'])}")
    out.append("※ 검증 중 · 투자 판단 근거 아님")
    return out
