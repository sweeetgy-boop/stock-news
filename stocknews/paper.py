# -*- coding: utf-8 -*-
"""[D] 추천 추적 — 종이 편입(paper tracking). 기록 전용.

실제로 **발송된** 새 추천을 '그 다음 거래일 시가'에 샀다고 가정하고
20거래일까지 표시, 40·60거래일까지 기록한다. 실제 보유(positions)와
섞지 않는다. 점수·추천 선정·발송 여부에 영향을 주지 않는다.

정의는 전부 [0] 공통 기반(`events.py`)을 쓴다. 여기서 수익률을 따로
계산하지 않는다.
  · 이벤트 판정  = events.assign_events (창 = B 의 RECO_COOLDOWN_DAYS,
                   없으면 EVENT_WINDOW_DAYS)
  · 편입가·성적  = events.score_events (이벤트 다음 거래일 시가 진입,
                   진입일 = 1일째, h일째 종가, 왕복 0.5%p, 지수·전종목 중위 대비)
  · 편입 불가    = score_events 의 entry_status (거래정지·시가 없음·상한가 시가)
  · 추적 중 정지 = score_events 의 HALTED (마지막 거래 가격으로 종료)

흐름
----
  발송 기록(paper_sends)  ← 평일 ① 발송 성공 시 / 소급은 운영 로그에서
        ↓ update()
  편입(paper_positions)   ← 다음 거래일 시가가 생기면 1행 (불가면 사유)
  확정 성적(paper_results) ← h일째 종가가 생기면 h 마다 1행
  무작위 대조군           ← 신호일마다 같은 수 × 100묶음, 고정 시드
  그림자 청산             ← 21일째 시가가 생기면 현행 / 대안 / 고정 20일

**추가 전용.** 모든 테이블에 UPDATE·DELETE 를 막는 트리거가 있고, 이
모듈에도 수정 경로가 없다. 잘못 들어간 편입은 `PaperBook.add_correction`
으로 정정 행을 추가한다(유효 행 = 같은 event_id 의 마지막 행).
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from contextlib import closing
from dataclasses import replace as dc_replace
from pathlib import Path

import numpy as np
import pandas as pd

from . import config as C
from . import events as E

log = logging.getLogger(__name__)

__all__ = ["PaperBook", "paper_window", "cooldown_warning", "targets_for",
           "sent_dates_from_logs", "record_sends", "update", "backfill",
           "tracked_tickers", "PaperData", "active_marks", "matured_on",
           "ENTRY_SKIP_TEXT", "RULE_MAP"]

KIND_NEW = "new"      # 평일 ① 새 추천 (DAILY_LIST_V2 / PAPER_TRACKING)
KIND_LIST = "list"    # 일요일 추천 10선 (기존 발송 경로)

ENTRY_SKIP_TEXT = {
    E.ENTRY_SUSPENDED: "거래정지 (다음 거래일 시세 없음)",
    E.ENTRY_NO_OPEN: "시가 없음",
    E.ENTRY_LIMIT_UP: "시가 상한가 (실제로 살 수 없음)",
}

# 사용자가 적은 규칙 이름 → 현행 엔진(exits.evaluate_position) rule.
# 이름이 다르면 코드 기준. 엔진에 없는 규칙은 None.
RULE_MAP = {
    "고정 손절": "stop:fixed",
    "MA20 이탈": "trail:ma",
    "고점 트레일링": "trail:peak",
    "+15% 익절": "target:take1",
    "만기": "hold:expired",
}

_SHADOW_QTY = 1000      # 비율 계산용 가상 수량 (실제 수량 아님)


def _now() -> str:
    from .trading_day import now_kst
    return now_kst().replace(tzinfo=None).isoformat(timespec="seconds")


def _ds(x) -> str:
    return E._ds(x)


def _nan(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if np.isfinite(f) else None


# ══════════════════════════ 추가 전용 접근자 ══════════════════════════
class PaperBook:
    """paper_* 테이블의 유일한 쓰기 경로. **추가만 한다.**

    공개 메서드 이름에 update/set/delete/remove/edit/modify/replace 가
    들어가지 않는다(smoke 검사). 정정은 `add_correction` 이 새 행을 붙인다.
    """

    def __init__(self, store):
        self.path = str(store.path)
        # 테이블은 Store._init → schema_v2.apply 가 만든다. 없으면 여기서도.
        with closing(self._conn()) as con:
            from . import schema_v2
            schema_v2.apply(con)
            con.commit()

    def _conn(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path, timeout=30.0)
        con.row_factory = sqlite3.Row
        return con

    # ── 쓰기 (INSERT 만) ──
    def add_sends(self, rows: list[tuple]) -> int:
        """[(d, ticker, kind, sent_at, source)] — 이미 있으면 그대로 둔다."""
        if not rows:
            return 0
        with closing(self._conn()) as con:
            before = con.total_changes
            con.executemany(
                "INSERT OR IGNORE INTO paper_sends(d,ticker,kind,sent_at,source)"
                " VALUES(?,?,?,?,?)", rows)
            con.commit()
            return con.total_changes - before

    _POS_COLS = ("event_id", "ticker", "name", "signal_d", "event_date",
                 "day_in_window", "enrolled", "entry_date", "entry_price",
                 "entry_status", "skip_reason", "market", "slot", "grade",
                 "rank", "value_score", "trend_score", "regime", "track",
                 "stop_price", "alt_stop_price", "atr14", "band_hi",
                 "band_mid", "band_lo", "fib_0382", "cross_low", "backfill",
                 "corrects_id", "note", "created")

    def add_position(self, row: dict) -> int | None:
        """편입 1행. 같은 event_id 가 이미 있으면 넣지 않는다(None)."""
        with closing(self._conn()) as con:
            have = con.execute("SELECT 1 FROM paper_positions WHERE event_id=?",
                               (row["event_id"],)).fetchone()
            if have:
                return None
            return self._insert_pos(con, row)

    def add_correction(self, original_id: int, note: str, **fields) -> int:
        """정정 행 추가. 원래 행은 그대로 남는다. 유효 행 = 마지막 행."""
        with closing(self._conn()) as con:
            r = con.execute("SELECT * FROM paper_positions WHERE id=?",
                            (int(original_id),)).fetchone()
            if r is None:
                raise KeyError(f"paper_positions id={original_id} 없음")
            row = {k: r[k] for k in self._POS_COLS}
            row.update(fields)
            row["corrects_id"] = int(original_id)
            row["note"] = note
            return self._insert_pos(con, row)

    def _insert_pos(self, con, row: dict) -> int:
        row = dict(row)
        row.setdefault("created", _now())
        vals = [row.get(k) for k in self._POS_COLS]
        cur = con.execute(
            f"INSERT INTO paper_positions({','.join(self._POS_COLS)}) VALUES("
            + ",".join("?" * len(self._POS_COLS)) + ")", vals)
        con.commit()
        return int(cur.lastrowid)

    _RES_COLS = ("position_id", "horizon", "exit_date", "exit_price", "status",
                 "ret", "net", "idx", "ex_idx", "med", "ex_med", "mdd",
                 "corrects_id", "created")

    def add_results(self, rows: list[dict]) -> int:
        """확정 성적. (position_id, horizon) 이 이미 있으면 넣지 않는다."""
        n = 0
        with closing(self._conn()) as con:
            for row in rows:
                have = con.execute(
                    "SELECT 1 FROM paper_results WHERE position_id=? AND horizon=?",
                    (int(row["position_id"]), int(row["horizon"]))).fetchone()
                if have:
                    continue
                row = dict(row)
                row.setdefault("created", _now())
                con.execute(
                    f"INSERT INTO paper_results({','.join(self._RES_COLS)}) "
                    "VALUES(" + ",".join("?" * len(self._RES_COLS)) + ")",
                    [row.get(k) for k in self._RES_COLS])
                n += 1
            con.commit()
        return n

    def add_bundles(self, rows: list[tuple]) -> int:
        """[(signal_d, bundle, k, seed, universe_n, tickers)]"""
        if not rows:
            return 0
        now = _now()
        with closing(self._conn()) as con:
            before = con.total_changes
            con.executemany(
                "INSERT OR IGNORE INTO paper_random_bundles(signal_d,bundle,k,"
                "seed,universe_n,tickers,created) VALUES(?,?,?,?,?,?,?)",
                [(*r, now) for r in rows])
            con.commit()
            return con.total_changes - before

    def add_random_results(self, rows: list[tuple]) -> int:
        """[(signal_d, horizon, bundle, mean_net, n_ok, pct, rank)]"""
        if not rows:
            return 0
        now = _now()
        with closing(self._conn()) as con:
            before = con.total_changes
            con.executemany(
                "INSERT OR IGNORE INTO paper_random_results(signal_d,horizon,"
                "bundle,mean_net,n_ok,pct,rank,created) VALUES(?,?,?,?,?,?,?,?)",
                [(*r, now) for r in rows])
            con.commit()
            return con.total_changes - before

    def add_shadow(self, rows: list[dict]) -> int:
        cols = ("position_id", "rule_set", "seq", "rule", "layer", "ratio",
                "signal_date", "exec_date", "exec_price", "plan_price",
                "gap_pct", "net", "hold_net", "regime", "created")
        if not rows:
            return 0
        now = _now()
        with closing(self._conn()) as con:
            before = con.total_changes
            con.executemany(
                f"INSERT OR IGNORE INTO paper_shadow({','.join(cols)}) VALUES("
                + ",".join("?" * len(cols)) + ")",
                [[r.get(k) if k != "created" else now for k in cols]
                 for r in rows])
            con.commit()
            return con.total_changes - before

    # ── 읽기 ──
    def _df(self, sql: str, params=()) -> pd.DataFrame:
        with closing(sqlite3.connect(self.path, timeout=30.0)) as con:
            return pd.read_sql_query(sql, con, params=params)

    def sends(self) -> pd.DataFrame:
        return self._df("SELECT * FROM paper_sends ORDER BY d, ticker")

    def positions(self) -> pd.DataFrame:
        """유효 편입 행 — 같은 event_id 중 마지막(정정 반영) 행."""
        return self._df(
            "SELECT p.* FROM paper_positions p JOIN (SELECT event_id, "
            "MAX(id) AS mid FROM paper_positions GROUP BY event_id) m "
            "ON p.id = m.mid ORDER BY p.signal_d, p.rank, p.id")

    def all_position_rows(self) -> pd.DataFrame:
        return self._df("SELECT * FROM paper_positions ORDER BY id")

    def results(self) -> pd.DataFrame:
        return self._df(
            "SELECT r.* FROM paper_results r JOIN (SELECT position_id, horizon,"
            " MAX(id) AS mid FROM paper_results GROUP BY position_id, horizon) m"
            " ON r.id = m.mid")

    def bundles(self) -> pd.DataFrame:
        return self._df("SELECT * FROM paper_random_bundles")

    def random_results(self) -> pd.DataFrame:
        return self._df("SELECT * FROM paper_random_results")

    def shadow(self) -> pd.DataFrame:
        return self._df("SELECT * FROM paper_shadow")


# ══════════════════════════ 설정 ══════════════════════════
def paper_window() -> int:
    """이벤트 창. B 의 RECO_COOLDOWN_DAYS 가 있으면 그것, 없으면 [0] 기본."""
    w = getattr(C, "RECO_COOLDOWN_DAYS", None)
    return int(C.EVENT_WINDOW_DAYS if w is None else w)


def cooldown_warning() -> str | None:
    """PAPER_TRACKING 을 켰는데 재추천 제한이 0 이면 경고 문구."""
    if not getattr(C, "PAPER_TRACKING", False):
        return None
    w = getattr(C, "RECO_COOLDOWN_DAYS", None)
    if w is not None and int(w) == 0:
        return ("PAPER_TRACKING 이 켜져 있는데 RECO_COOLDOWN_DAYS=0 — 같은 종목이 "
                "매일 새 이벤트로 편입돼 추적이 중복됩니다")
    return None


# ══════════════════════════ 대상 (발송 후보) ══════════════════════════
def _recos_all(store, upto: str | None = None) -> pd.DataFrame:
    with closing(sqlite3.connect(store.path, timeout=30.0)) as con:
        df = pd.read_sql_query("SELECT * FROM recos ORDER BY d, rank", con)
    if upto:
        df = df[df["d"] <= _ds(upto)]
    return df


def _assigned(store, upto: str | None, cal: list[str]) -> pd.DataFrame:
    return E.assign_events(_recos_all(store, upto), cal, window=paper_window())


def targets_for(store, d: str, cal: list[str] | None = None,
                assigned: pd.DataFrame | None = None) -> list[dict]:
    """그 거래일 발송·추적 대상 recos 행 (B 훅이 있으면 그것).

    각 dict: recos 열 + is_new_event, day_in_window, slot_changed,
    grade_changed, event_date, event_id.
    B 미병합(훅 None) → 그날 recos 전체, [0] assign_events 로 새 이벤트 판정.
    """
    from . import v2_hooks
    d = _ds(d)
    cal = cal if cal is not None else E.trading_calendar(store)
    a = assigned if assigned is not None else _assigned(store, d, cal)
    day = a[a["d"] == d] if a is not None and len(a) else pd.DataFrame()
    amap = {str(r["ticker"]): r for r in day.to_dict("records")}
    try:
        hook = v2_hooks.send_targets(store, d)
    except Exception as exc:  # noqa: BLE001 - B 실패가 브리핑을 막지 않는다
        log.warning("send_targets 실패 — recos 전체로 대체: %s", exc)
        hook = None
    out = []
    if hook is None:
        for r in day.sort_values("rank").to_dict("records"):
            r["is_new_event"] = bool(r["is_event"])
            out.append(r)
        return out
    for h in hook:
        r = dict(h)
        t = str(r.get("ticker"))
        base = amap.get(t, {})
        for k in ("event_id", "event_date", "event_slot", "event_grade"):
            r.setdefault(k, base.get(k))
        if r.get("event_date") is None and r.get("day_in_window"):
            i = E._pos(cal, d) - int(r["day_in_window"]) + 1
            if 0 <= i < len(cal):
                r["event_date"] = cal[i]
                r["event_id"] = f"{t}:{cal[i]}"
        r.setdefault("is_new_event", bool(base.get("is_event", False)))
        r.setdefault("day_in_window", base.get("day_in_window"))
        out.append(r)
    return out


# ══════════════════════════ 발송 기록 ══════════════════════════
_JSON_LINE = re.compile(r'^\{.*"mode":\s*"daily".*\}\s*$')


def sent_dates_from_logs(log_dir: str | Path) -> dict[str, str]:
    """운영 로그에서 **실제 발송된** 추천 기준일 {기준일: 로그파일}.

    판정 근거: daily 단계의 JSON 요약에 `"reco_sent": true` 가 있고, 그
    JSON 앞(같은 단계의 로그 줄)에 `텔레그램 발송 완료` 가 있어야 한다.
    dry-run 이면 '발송 완료' 줄이 없다(_emit 가 화면에만 찍는다).
    기준일 = reco_date(일요일 --send-only) 또는 trade_date.
    """
    out: dict[str, str] = {}
    p = Path(log_dir)
    if not p.is_dir():
        return out
    for f in sorted(p.glob("*.log")):
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if '"reco_sent": true' not in text:
            continue
        buf: list[str] = []
        for line in text.splitlines():
            s = line.strip()
            if s.startswith("{") and '"mode"' in s:
                if _JSON_LINE.match(s) and '"reco_sent": true' in s:
                    try:
                        j = json.loads(s)
                    except ValueError:
                        j = {}
                    d = j.get("reco_date") or j.get("trade_date")
                    sent = any("텔레그램 발송 완료" in b for b in buf)
                    if d and sent and not j.get("dry_run"):
                        out.setdefault(_ds(d), f.name)
                buf = []
                continue
            buf.append(s)
    return out


def record_sends(store, d: str, tickers: list[str], kind: str,
                 source: str = "live", sent_at: str | None = None) -> int:
    """발송 성공 직후 부른다. 편입은 여기서 하지 않는다(update 가 한다)."""
    rows = [(_ds(d), str(t), kind, sent_at or _now(), source) for t in tickers]
    return PaperBook(store).add_sends(rows)


# ══════════════════════════ 데이터 묶음 ══════════════════════════
class PaperData:
    """update·표시가 공유하는 읽기 묶음. upto 이후 데이터는 보지 않는다."""

    def __init__(self, store, upto: str | None = None, index=None,
                 markets=None, allow_network: bool = True):
        self.store = store
        full = E.trading_calendar(store)
        self.upto = _ds(upto) if upto else (full[-1] if full else None)
        self.cal = [x for x in full if self.upto is None or x <= self.upto]
        self.book = PaperBook(store)
        rec = _recos_all(store, self.upto)
        start = None
        if len(rec):
            first = str(rec["d"].min())
            i = max(0, E._pos(self.cal, first) - 5)
            start = self.cal[i] if self.cal else first
        self.panel = (E.PricePanel.from_store(store, calendar=[
            x for x in self.cal if start is None or x >= start])
            if self.cal else None)
        self.markets = markets if markets is not None else E.ticker_markets(store)
        if index is None:
            try:
                index = E.load_index_history(
                    start=start or "2024-01-01",
                    cache_dir=getattr(C, "PAPER_INDEX_CACHE", None),
                    allow_network=allow_network)
            except Exception as exc:  # noqa: BLE001
                log.warning("지수 히스토리 실패 — 지수 대비 생략: %s", exc)
                index = {}
        self.index = index or {}
        self.assigned = E.assign_events(rec, self.cal, window=paper_window())

    def pos_of(self, d: str) -> int:
        return E._pos(self.cal, _ds(d))

    def day_n(self, entry_date: str, d: str) -> int:
        """entry_date 를 1일째로 센 d 의 거래일 순번."""
        return self.pos_of(d) - self.pos_of(entry_date) + 1

    def date_at(self, entry_date: str, h: int) -> str | None:
        i = self.pos_of(entry_date) + int(h) - 1
        return self.cal[i] if 0 <= i < len(self.cal) else None

    def score(self, ev: pd.DataFrame, horizons) -> pd.DataFrame:
        return E.score_events(ev, self.panel, self.index, self.markets,
                              horizons=tuple(horizons))


# ══════════════════════════ 편입 ══════════════════════════
def _snapshot(store, ticker: str, signal_d: str) -> dict:
    """그림자 청산용 진입 스냅샷. 신호일까지의 시세만 쓴다(pos-open 과 같은 방식)."""
    from .exits import atr
    from .screener import screen_one
    out: dict = {}
    ohlcv = store.load_ohlcv(ticker, days=420)
    if ohlcv is None or len(ohlcv) == 0:
        return out
    ohlcv = ohlcv[ohlcv.index <= pd.Timestamp(signal_d)]
    if len(ohlcv) < C.DEFAULT.ma.long + 30:
        return out
    out["atr14"] = _nan(atr(ohlcv, 14))
    try:
        res = screen_one(ticker, ticker, ohlcv, cfg=C.DEFAULT)
    except Exception as exc:  # noqa: BLE001 - 스냅샷은 부가 정보
        log.debug("스냅샷 실패 %s: %s", ticker, exc)
        return out
    if res.liq is not None:
        out.update({"band_hi": _nan(res.liq.band_hi),
                    "band_mid": _nan(res.liq.band_mid),
                    "band_lo": _nan(res.liq.band_lo)})
    if res.fib is not None:
        out["fib_0382"] = _nan(res.fib.levels.get(0.382))
    if (res.trend is not None and res.trend.best_cross
            and res.trend.best_cross.kind == "GOLDEN"):
        try:
            out["cross_low"] = _nan(ohlcv.loc[res.trend.best_cross.date, "저가"])
        except KeyError:
            pass
    return out


def alt_stop_price(entry: float, atr14: float | None) -> float:
    """대안 세트 손절: 진입가 − 2×ATR14, 손절폭 10% 이상 15% 이하."""
    lo = C.PAPER_ALT_STOP_MIN_PCT / 100.0
    hi = C.PAPER_ALT_STOP_MAX_PCT / 100.0
    if atr14 is None or not np.isfinite(atr14) or entry <= 0:
        dist = lo
    else:
        dist = min(max(C.PAPER_ALT_ATR_MULT * float(atr14) / entry, lo), hi)
    return float(entry) * (1.0 - dist)


def _enroll(pd_: PaperData, stats: dict, backfill_srcs: set[str]) -> None:
    """발송 기록 → 편입 행. 이벤트마다 처음 발송된 날짜 기준 1행."""
    from . import v2_hooks
    from .exits import stop_price_for
    book = pd_.book
    sends = book.sends()
    if sends.empty:
        return
    sends = sends[sends["d"] <= pd_.upto]
    have = set(book.all_position_rows()["event_id"].astype(str))
    a = pd_.assigned
    amap = {(str(r["d"]), str(r["ticker"])): r for r in a.to_dict("records")}
    # 같은 날 여러 행이면 순위 순. 이벤트당 처음 발송된 날짜 하나만.
    order = []
    for s in sends.to_dict("records"):
        r = amap.get((str(s["d"]), str(s["ticker"])))
        if r is None:
            stats["send_without_reco"] = stats.get("send_without_reco", 0) + 1
            continue
        order.append((str(s["d"]), int(r.get("rank") or 99), s, r))
    order.sort(key=lambda x: (x[0], x[1]))
    n = len(pd_.cal)
    for d, _rk, s, r in order:
        eid = str(r["event_id"])
        if eid in have:
            continue
        p = pd_.pos_of(d)
        if p + 1 >= n:
            stats["pending_entry"] = stats.get("pending_entry", 0) + 1
            continue          # 다음 거래일 시가 미도래
        ev = pd.DataFrame([{"ticker": r["ticker"], "event_date": d}])
        sc = pd_.score(ev, (1,)).iloc[0]
        status = str(sc["entry_status"])
        ok = status == E.ENTRY_OK
        entry = _nan(sc["entry_price"]) if ok else None
        try:
            regime = v2_hooks.regime_on(pd_.store, d)
        except Exception:  # noqa: BLE001
            regime = None
        vs, ts = float(r.get("value_score") or 0), float(r.get("trend_score") or 0)
        row = {
            "event_id": eid, "ticker": str(r["ticker"]), "name": r.get("name"),
            "signal_d": d, "event_date": r.get("event_date"),
            "day_in_window": int(r.get("day_in_window") or 1),
            "enrolled": 1 if ok else 0,
            "entry_date": sc.get("entry_date"), "entry_price": entry,
            "entry_status": status,
            "skip_reason": None if ok else ENTRY_SKIP_TEXT.get(status, status),
            "market": sc.get("market") or None, "slot": r.get("slot"),
            "grade": r.get("grade"), "rank": int(r.get("rank") or 0),
            "value_score": vs, "trend_score": ts, "regime": regime,
            "track": "TREND" if ts >= vs else "VALUE",
            "backfill": 1 if str(s.get("source") or "") in backfill_srcs
            or str(s.get("source") or "").startswith("log:") else 0,
        }
        if ok:
            snap = _snapshot(pd_.store, row["ticker"], d)
            row.update(snap)
            row["stop_price"] = stop_price_for(entry, C.DEFAULT)
            row["alt_stop_price"] = alt_stop_price(entry, snap.get("atr14"))
        if book.add_position(row) is not None:
            have.add(eid)
            stats["enrolled" if ok else "not_enrolled"] = \
                stats.get("enrolled" if ok else "not_enrolled", 0) + 1


# ══════════════════════════ 확정 성적 ══════════════════════════
def _mdd(pd_: PaperData, ticker: str, entry_date: str, exit_date: str,
         entry: float) -> float | None:
    """진입가 대비 최대 낙폭(%) — 진입일~청산일 종가. 진입가를 첫 고점으로."""
    j = pd_.panel.col(ticker)
    if j is None:
        return None
    a, b = pd_.panel.pos.get(entry_date), pd_.panel.pos.get(exit_date)
    if a is None or b is None:
        return None
    c = pd_.panel._c[a:b + 1, j]
    c = c[np.isfinite(c) & (c > 0)]
    if not len(c):
        return None
    path = np.concatenate([[entry], c])
    peak = np.maximum.accumulate(path)
    return float(((path / peak) - 1.0).min() * 100.0)


def _score_results(pd_: PaperData, stats: dict) -> None:
    book = pd_.book
    pos = book.positions()
    if pos.empty:
        return
    pos = pos[pos["enrolled"] == 1]
    res = book.results()
    done = set(zip(res["position_id"], res["horizon"])) if len(res) else set()
    hs = tuple(C.PAPER_RECORD_HORIZONS)
    need = [r for r in pos.to_dict("records")
            if any((r["id"], h) not in done for h in hs)]
    if not need:
        return
    ev = pd.DataFrame([{"ticker": r["ticker"], "event_date": r["signal_d"],
                        "_pid": r["id"], "_entry": r["entry_price"]}
                       for r in need])
    sc = pd_.score(ev, hs)
    rows = []
    for r in sc.to_dict("records"):
        pid = int(r["_pid"])
        for h in hs:
            st = r.get(f"status_{h}")
            if (pid, h) in done or st in (E.ST_PENDING, None):
                continue
            xd = r.get(f"exit_date_{h}")
            mdd = (_mdd(pd_, r["ticker"], r["entry_date"], xd,
                        float(r["entry_price"]))
                   if st in (E.ST_OK, E.ST_HALTED) and xd else None)
            rows.append({
                "position_id": pid, "horizon": h, "exit_date": xd,
                "exit_price": _nan(r.get(f"exit_price_{h}")), "status": st,
                "ret": _nan(r.get(f"ret_{h}")), "net": _nan(r.get(f"net_{h}")),
                "idx": _nan(r.get(f"idx_{h}")),
                "ex_idx": _nan(r.get(f"ex_idx_{h}")),
                "med": _nan(r.get(f"med_{h}")),
                "ex_med": _nan(r.get(f"ex_med_{h}")), "mdd": mdd})
    stats["results"] = stats.get("results", 0) + book.add_results(rows)


# ══════════════════════════ 무작위 대조군 ══════════════════════════
def _universe(store, d: str) -> list[str]:
    """그날 추천 대상 유니버스 = scans 스냅샷(배제·유동성 필터 통과 종목)."""
    with closing(sqlite3.connect(store.path, timeout=30.0)) as con:
        rows = con.execute("SELECT ticker FROM scans WHERE d=?", (_ds(d),)).fetchall()
    return sorted({str(t) for (t,) in rows})


def random_seed(d: str) -> list[int]:
    return [int(C.PAPER_RANDOM_SEED), int(_ds(d).replace("-", ""))]


def draw_bundles(universe: list[str], k: int, d: str,
                 n: int | None = None) -> list[list[str]]:
    """고정 시드 무작위 묶음. 같은 입력이면 항상 같은 결과."""
    n = int(C.PAPER_RANDOM_BUNDLES if n is None else n)
    uni = sorted(universe)
    if k <= 0 or len(uni) < k:
        return []
    rng = np.random.default_rng(random_seed(d))
    return [sorted(rng.choice(uni, size=k, replace=False).tolist())
            for _ in range(n)]


def _random(pd_: PaperData, stats: dict) -> None:
    book = pd_.book
    pos = book.positions()
    if pos.empty:
        return
    ok = pos[pos["enrolled"] == 1]
    have_b = book.bundles()
    have_sd = set(have_b["signal_d"]) if len(have_b) else set()
    new_rows = []
    for sd, g in ok.groupby("signal_d"):
        if sd in have_sd:
            continue
        uni = _universe(pd_.store, sd)
        bs = draw_bundles(uni, len(g), sd)
        if not bs:
            continue
        seed = json.dumps(random_seed(sd))
        new_rows += [(sd, b, len(g), seed, len(uni), ",".join(t))
                     for b, t in enumerate(bs)]
    stats["bundles"] = stats.get("bundles", 0) + book.add_bundles(new_rows)

    # 성적: 추천 묶음이 그 h 에서 전부 확정된 신호일만.
    bund = book.bundles()
    if bund.empty:
        return
    res = book.results()
    rr = book.random_results()
    done = set(zip(rr["signal_d"], rr["horizon"])) if len(rr) else set()
    out = []
    for sd, gb in bund.groupby("signal_d"):
        cohort = ok[ok["signal_d"] == sd]
        hs = [h for h in C.PAPER_RECORD_HORIZONS if (sd, h) not in done]
        if not hs or cohort.empty:
            continue
        # upto 이후에 확정된 행은 보지 않는다 (과거 기준일로 다시 돌릴 때
        # 미래 성적이 섞이거나, 미도래 구간이 NaN 으로 영구 기록되지 않게).
        cr = res[res["position_id"].isin(cohort["id"])
                 & (res["exit_date"] <= pd_.upto)]
        ready = [h for h in hs
                 if len(cr[cr["horizon"] == h]) == len(cohort)]
        if not ready:
            continue
        members = sorted({t for s in gb["tickers"] for t in str(s).split(",") if t})
        sc = pd_.score(pd.DataFrame({"ticker": members, "event_date": sd}),
                       tuple(ready)).set_index("ticker")
        for h in ready:
            reco = cr[cr["horizon"] == h]["net"].astype(float).dropna()
            reco_mean = float(reco.mean()) if len(reco) else np.nan
            means = []
            for b, tick in zip(gb["bundle"], gb["tickers"]):
                v = sc.loc[[t for t in str(tick).split(",") if t],
                           f"net_{h}"].astype(float).dropna()
                m = float(v.mean()) if len(v) else np.nan
                means.append(m)
                out.append((sd, h, int(b), _nan(m), int(len(v)), None, None))
            pct, rank = percentile_of(reco_mean, means)
            out.append((sd, h, -1, _nan(reco_mean), int(len(reco)), pct, rank))
    stats["random_results"] = stats.get("random_results", 0) + \
        book.add_random_results(out)


def percentile_of(value: float, dist) -> tuple[float | None, int | None]:
    """value 가 dist(무작위 묶음 평균) 중 몇 번째인가.

    pct  = (아래인 묶음 + 같은 묶음/2) / 유효 묶음 × 100. 높을수록 추천이 낫다.
    rank = 1 + value 보다 큰 묶음 수 (1 = 101개 중 최고).
    """
    arr = np.array([x for x in dist if x is not None and np.isfinite(x)],
                   dtype="float64")
    if value is None or not np.isfinite(value) or not len(arr):
        return None, None
    below = float((arr < value).sum())
    eq = float((arr == value).sum())
    pct = (below + eq / 2.0) / len(arr) * 100.0
    return round(pct, 1), int(1 + (arr > value).sum())


# ══════════════════════════ 그림자 청산 ══════════════════════════
def _position_obj(r: dict, stop: float, qty: int = _SHADOW_QTY):
    from .contracts import Position
    return Position(
        id=int(r["id"]), ticker=str(r["ticker"]), name=str(r.get("name") or ""),
        track=str(r.get("track") or "VALUE"), entry_date=str(r["entry_date"]),
        entry_price=float(r["entry_price"]), qty=qty, remaining=qty,
        stop_price=float(stop),
        entry_band_hi=_nan(r.get("band_hi")), entry_band_mid=_nan(r.get("band_mid")),
        entry_band_lo=_nan(r.get("band_lo")), entry_fib_0382=_nan(r.get("fib_0382")),
        entry_cross_low=_nan(r.get("cross_low")))


def _alt_cfg(disable: set[str]):
    cfg = C.DEFAULT
    ex = dc_replace(cfg.exit, trail_pct=float(C.PAPER_ALT_TRAIL_PCT))
    if "peak" in disable:
        ex = dc_replace(ex, trail_pct=1e9)          # 고점 트레일링 비활성
    if "ma" in disable:
        ex = dc_replace(ex, trail_ma=10 ** 6)       # MA 계산 불가 → 비활성
    return dc_replace(cfg, exit=ex)


def _alt_decide(pos, seg: pd.DataFrame):
    """대안 세트 판정. 규칙 본체는 evaluate_position 그대로 쓰고, 대안 세트의
    추가 조건(+10% 이후 활성, MA20 2일 확인)이 안 맞으면 그 규칙을 끄고 다시
    판정한다."""
    from .exits import evaluate_position
    from .indicators import moving_averages
    disable: set[str] = set()
    for _ in range(3):
        dec, st = evaluate_position(pos, seg, None, _alt_cfg(disable))
        if dec is None:
            return None, st
        if dec.rule == "trail:peak" and "peak" not in disable:
            armed = float(st.get("peak_close") or 0) >= \
                pos.entry_price * (1 + C.PAPER_ALT_TRAIL_ARM_PCT / 100.0)
            if not armed:
                disable.add("peak")
                continue
        if dec.rule == "trail:ma" and "ma" not in disable:
            k = int(C.PAPER_ALT_MA_CONFIRM_DAYS)
            close = seg["종가"].astype("float64")
            ma = moving_averages(close, C.DEFAULT.ma)
            col = f"ma{C.DEFAULT.exit.trail_ma}"
            mser = ma[col] if col in ma.columns else close.rolling(
                C.DEFAULT.exit.trail_ma).mean()
            tail = (close < mser).tail(k)
            if not (len(tail) == k and bool(tail.all())):
                disable.add("ma")
                continue
        return dec, st
    return dec, st


def _simulate(r: dict, ohlcv: pd.DataFrame, stop: float, alt: bool,
              hold_net: float | None, regime) -> list[dict] | None:
    """진입일~20일째 매일 종가로 판정, 신호 다음 거래일 시가 체결 가정.

    반환 None = 아직 확정 불가(체결일 시세 미도래).
    """
    from .contracts import DONE_TAKE1, DONE_TAKE2
    from .exits import evaluate_position
    H = int(C.PAPER_DISPLAY_DAYS)
    entry = float(r["entry_price"])
    idx = ohlcv.index
    e = int(idx.searchsorted(pd.Timestamp(r["entry_date"])))
    if e >= len(idx) or idx[e] != pd.Timestamp(r["entry_date"]):
        return None
    pos = _position_obj(r, stop)
    rows = []
    seq = 0
    cost = float(C.ROUND_TRIP_COST_PCT)
    for t in range(e, min(e + H, len(idx))):
        if pos.remaining <= 0:
            break
        seg = ohlcv.iloc[:t + 1]
        if alt:
            dec, _st = _alt_decide(pos, seg)
        else:
            dec, _st = evaluate_position(pos, seg, None, C.DEFAULT)
        if dec is None:
            continue
        if t + 1 >= len(idx):
            return None                      # 체결일 시가 미도래
        xo = float(ohlcv["시가"].iloc[t + 1])
        if not (np.isfinite(xo) and xo > 0):
            xo = float(ohlcv["종가"].iloc[t])
        frac = dec.qty / _SHADOW_QTY
        plan = float(stop) if dec.rule == "stop:fixed" else float(dec.signal_price)
        seq += 1
        rows.append({
            "position_id": int(r["id"]), "rule_set": "alt" if alt else "current",
            "seq": seq, "rule": dec.rule, "layer": int(dec.layer),
            "ratio": round(frac, 4), "signal_date": idx[t].strftime("%Y-%m-%d"),
            "exec_date": idx[t + 1].strftime("%Y-%m-%d"), "exec_price": xo,
            "plan_price": plan, "gap_pct": (xo / plan - 1.0) * 100.0,
            "net": (xo / entry - 1.0) * 100.0 - cost, "hold_net": hold_net,
            "regime": regime})
        done = pos.exits_done
        if dec.rule == "target:take1":
            done |= DONE_TAKE1
        if dec.rule == "target:fib_0382":
            done |= DONE_TAKE2
        pos = dc_replace(pos, remaining=pos.remaining - dec.qty, exits_done=done)
    if pos.remaining > 0 and e + H > len(idx) - 1:
        return None                          # 만기·체결 미도래
    return rows


def _shadow(pd_: PaperData, stats: dict) -> None:
    book = pd_.book
    pos = book.positions()
    if pos.empty:
        return
    pos = pos[pos["enrolled"] == 1]
    sh = book.shadow()
    have = set(sh["position_id"]) if len(sh) else set()
    res = book.results()
    r20 = res[res["horizon"] == C.PAPER_DISPLAY_DAYS].set_index("position_id") \
        if len(res) else pd.DataFrame()
    out = []
    for r in pos.to_dict("records"):
        pid = int(r["id"])
        if pid in have or pid not in getattr(r20, "index", []):
            continue
        hr = r20.loc[pid]
        if hr["status"] not in (E.ST_OK, E.ST_HALTED):
            continue
        ohlcv = pd_.store.load_ohlcv(str(r["ticker"]), days=420)
        if ohlcv is None or ohlcv.empty:
            continue
        ohlcv = ohlcv[ohlcv.index <= pd.Timestamp(pd_.upto)]
        hold = _nan(hr["net"])
        cur = _simulate(r, ohlcv, float(r["stop_price"]), False, hold, r.get("regime"))
        alt = _simulate(r, ohlcv, float(r["alt_stop_price"] or r["stop_price"]),
                        True, hold, r.get("regime"))
        if cur is None or alt is None:
            continue
        out += cur + alt + [{
            "position_id": pid, "rule_set": "hold20", "seq": 1,
            "rule": "hold:20d", "layer": None, "ratio": 1.0,
            "signal_date": hr["exit_date"], "exec_date": hr["exit_date"],
            "exec_price": _nan(hr["exit_price"]), "plan_price": None,
            "gap_pct": None, "net": hold, "hold_net": hold,
            "regime": r.get("regime")}]
    stats["shadow"] = stats.get("shadow", 0) + book.add_shadow(out)


# ══════════════════════════ 실행 ══════════════════════════
def update(store, upto: str | None = None, index=None, markets=None,
           allow_network: bool = True) -> dict:
    """발송 기록 → 편입 → 확정 성적 → 무작위 대조군 → 그림자 청산. 멱등.

    upto 이후 시세는 보지 않는다. 같은 입력으로 다시 돌려도 결과가 같다
    (이미 있는 행은 다시 넣지 않는다).
    """
    t0 = time.time()
    stats: dict = {}
    w = cooldown_warning()
    if w:
        log.warning("%s", w)
        stats["warning"] = w
    pd_ = PaperData(store, upto, index=index, markets=markets,
                    allow_network=allow_network)
    if not pd_.cal or pd_.panel is None:
        stats["skipped"] = "no prices"
        return stats
    for step in (_enroll, _score_results, _random, _shadow):
        try:
            if step is _enroll:
                step(pd_, stats, set())
            else:
                step(pd_, stats)
        except Exception as exc:  # noqa: BLE001 - 추적은 발송을 막지 않는다
            log.exception("paper %s 실패: %s", step.__name__, exc)
            stats.setdefault("errors", []).append(
                f"{step.__name__}: {type(exc).__name__}: {exc}"[:200])
    stats["upto"] = pd_.upto
    stats["elapsed_sec"] = round(time.time() - t0, 1)
    stats["_data"] = pd_
    return stats


def backfill(store, log_dir: str | Path = "logs",
             sent_dates: dict[str, str] | None = None, upto: str | None = None,
             index=None, markets=None, allow_network: bool = True) -> dict:
    """소급 편입. 운영 로그로 '실제 발송된' 추천 기준일을 찾아 발송 기록을
    만들고(source = log:<파일>), update 로 편입·성적·대조군·그림자를 같은
    날짜로 채운다. nightly 와 무관한 1회성 명령."""
    t0 = time.time()
    found = dict(sent_dates) if sent_dates is not None else \
        sent_dates_from_logs(log_dir)
    rec = _recos_all(store, upto)
    rows = []
    for d, src in sorted(found.items()):
        day = rec[rec["d"] == d]
        rows += [(d, str(t), KIND_LIST, None, f"log:{src}")
                 for t in day.sort_values("rank")["ticker"]]
    added = PaperBook(store).add_sends(rows)
    st = update(store, upto=upto, index=index, markets=markets,
                allow_network=allow_network)
    st.update({"sent_dates": sorted(found), "sends_added": added,
               "sent_rows": len(rows),
               "elapsed_total_sec": round(time.time() - t0, 1)})
    return st


def event_counts(store, sent_dates) -> dict:
    """검증용: 필터 OFF 이벤트 수 vs 실제 발송분 이벤트 수."""
    cal = E.trading_calendar(store)
    a = E.assign_events(_recos_all(store), cal, window=C.EVENT_WINDOW_DAYS)
    s = a[a["d"].isin(set(sent_dates))]
    return {"recos_rows": int(len(a)), "events_all": int(a["is_event"].sum()),
            "sent_rows": int(len(s)),
            "sent_events_any_row": int(s["event_id"].nunique()),
            "sent_events_starting_on_sent_day": int(s["is_event"].sum())}


# ══════════════════════════ 조회 (표시·훅) ══════════════════════════
def tracked_tickers(store, d) -> set[str]:
    """그 거래일 추적 중(진입일 ≤ d, 20일째 미만)인 종목. PAPER_TRACKING 꺼짐 = 빈 집합."""
    if not getattr(C, "PAPER_TRACKING", False):
        return set()
    try:
        pos = PaperBook(store).positions()
    except Exception:  # noqa: BLE001
        return set()
    if pos.empty:
        return set()
    pos = pos[(pos["enrolled"] == 1) & (pos["entry_date"] <= _ds(d))]
    if pos.empty:
        return set()
    cal = E.trading_calendar(store)
    p = E._pos(cal, _ds(d))
    out = set()
    for r in pos.to_dict("records"):
        n = p - E._pos(cal, str(r["entry_date"])) + 1
        if 1 <= n < int(C.PAPER_DISPLAY_DAYS):
            out.add(str(r["ticker"]))
    return out


def active_marks(pd_: PaperData, d: str) -> pd.DataFrame:
    """d 종가 기준 추적 중(1~19일째) 포지션의 현재 성적 + 신호일별 무작위 표.

    반환 열: 포지션 열 + day_n, net, ex_idx, status. 무작위 묶음 현재 평균은
    attrs['random'] = {signal_d: [100개 평균]} 에 둔다.
    """
    pos = pd_.book.positions()
    if pos.empty:
        return pos
    d = _ds(d)
    pos = pos[(pos["enrolled"] == 1) & (pos["entry_date"] <= d)].copy()
    if pos.empty:
        return pos
    pos["day_n"] = [pd_.day_n(e, d) for e in pos["entry_date"]]
    pos = pos[(pos["day_n"] >= 1) & (pos["day_n"] < int(C.PAPER_DISPLAY_DAYS))]
    rows = []
    rnd: dict = {}
    bund = pd_.book.bundles()
    for (sd, n), g in pos.groupby(["signal_d", "day_n"]):
        ev = pd.DataFrame({"ticker": g["ticker"], "event_date": sd,
                           "_pid": g["id"]})
        sc = pd_.score(ev, (int(n),))
        for r, s in zip(g.to_dict("records"), sc.to_dict("records")):
            r.update({"net": _nan(s.get(f"net_{n}")),
                      "ex_idx": _nan(s.get(f"ex_idx_{n}")),
                      "status": s.get(f"status_{n}")})
            rows.append(r)
        gb = bund[bund["signal_d"] == sd] if len(bund) else bund
        if len(gb):
            members = sorted({t for x in gb["tickers"] for t in str(x).split(",") if t})
            ms = pd_.score(pd.DataFrame({"ticker": members, "event_date": sd}),
                           (int(n),)).set_index("ticker")[f"net_{n}"]
            rnd[sd] = [(_nan(ms.loc[str(x).split(",")].astype(float).mean()),
                        int(ms.loc[str(x).split(",")].notna().sum()))
                       for x in gb.sort_values("bundle")["tickers"]]
    out = pd.DataFrame(rows)
    out.attrs["random"] = rnd
    return out


def pooled_percentile(reco: pd.DataFrame, random_by_sd: dict,
                      col: str = "net") -> tuple[float | None, int | None]:
    """신호일별 묶음 b 를 이어 붙인 '무작위 포트폴리오 b' 100개와 추천 전체 비교.

    무작위 포트폴리오 b 평균 = Σ_sd (묶음 b 평균 × 유효 수) / Σ 유효 수.
    """
    if reco is None or reco.empty or not random_by_sd:
        return None, None
    v = reco[col].astype(float).dropna()
    if not len(v):
        return None, None
    nb = max(len(x) for x in random_by_sd.values())
    pooled = []
    for b in range(nb):
        s = w = 0.0
        for sd, lst in random_by_sd.items():
            if b < len(lst) and lst[b][0] is not None and lst[b][1]:
                s += lst[b][0] * lst[b][1]
                w += lst[b][1]
        pooled.append(s / w if w else None)
    return percentile_of(float(v.mean()), pooled)


def matured_on(pd_: PaperData, d: str | None = None,
               start: str | None = None) -> pd.DataFrame:
    """h=20 확정 성적 + 포지션 열. d 이하(그리고 start 이상) 만기분."""
    res = pd_.book.results()
    pos = pd_.book.positions()
    if res.empty or pos.empty:
        return pd.DataFrame()
    r = res[res["horizon"] == int(C.PAPER_DISPLAY_DAYS)]
    m = r.merge(pos, left_on="position_id", right_on="id", suffixes=("", "_p"))
    if d is not None:
        m = m[m["exit_date"] <= _ds(d)]
    if start is not None:
        m = m[m["exit_date"] >= _ds(start)]
    return m.reset_index(drop=True)


def matured_random(pd_: PaperData, signal_ds, h: int | None = None) -> dict:
    """{signal_d: [(묶음 평균, 유효 수)] × 100} — 확정 저장된 무작위 성적."""
    h = int(C.PAPER_DISPLAY_DAYS if h is None else h)
    rr = pd_.book.random_results()
    if rr.empty:
        return {}
    rr = rr[(rr["horizon"] == h) & (rr["bundle"] >= 0)
            & rr["signal_d"].isin(set(signal_ds))]
    out = {}
    for sd, g in rr.groupby("signal_d"):
        g = g.sort_values("bundle")
        out[sd] = [(_nan(m), int(n or 0)) for m, n in zip(g["mean_net"], g["n_ok"])]
    return out


def brief_date(store, asof=None) -> str | None:
    """브리핑 기준 거래일 = asof 이하 마지막 완전 거래일."""
    cal = E.trading_calendar(store)
    if not cal:
        return None
    if asof is None:
        return cal[-1]
    p = E._pos(cal, _ds(asof))
    return cal[p] if p >= 0 else None


def shadow_table(pd_: PaperData, d: str | None = None) -> dict:
    """weekly 비교표 재료. 국면별로 나눈다(국면 없음 = 한 묶음)."""
    sh = pd_.book.shadow()
    if sh.empty:
        return {}
    pos = pd_.book.positions().set_index("id")
    if d is not None:
        last = sh.groupby("position_id")["exec_date"].max()
        keep = set(last[last <= _ds(d)].index)
        sh = sh[sh["position_id"].isin(keep)]
    sh = sh.copy()
    sh["regime"] = sh["regime"].fillna("국면 없음")
    out = {}
    for reg, g in [("전체", sh)] + list(sh.groupby("regime")):
        per = {}
        for rs, gg in g.groupby("rule_set"):
            tot = gg.assign(w=gg["net"] * gg["ratio"]).groupby("position_id")["w"].sum()
            per[rs] = E.stat_cell(tot)
        rules = {}
        for (rs, rule), gg in g[g["rule_set"] != "hold20"].groupby(["rule_set", "rule"]):
            diff = (gg["net"] - gg["hold_net"]).astype(float)
            rules[(rs, rule)] = {"n": int(len(gg)),
                                 "diff_mean": _nan(diff.mean()),
                                 "gap_mean": _nan(gg["gap_pct"].astype(float).mean())}
        out[reg] = {"sets": per, "rules": rules,
                    "positions": int(g["position_id"].nunique())}
    return out
