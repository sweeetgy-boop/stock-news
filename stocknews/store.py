# -*- coding: utf-8 -*-
"""SQLite 저장소.

왜 로컬 캐시가 필수인가
----------------------
코스피+코스닥 전종목은 약 2,800개다. 종목마다 pykrx 로 시세를 받으면
매일 저녁 2,800회 요청이 나가고, KRX 스크래핑은 그 정도 부하에서
차단되거나 몇 시간이 걸린다.

해법은 요청 축을 바꾸는 것이다.
  최초 1회  : 종목별 400일 히스토리 적재 (느림, 재개 가능하게 만든다)
  이후 매일 : '특정 일자 전종목' 1회 요청으로 하루치 봉만 추가

일단 적재되면 저녁 스캔은 네트워크를 전혀 쓰지 않고 SQLite 읽기 +
pandas 연산만 한다. 2,800종목 채점이 수 분 안에 끝난다.

그리고 매일의 점수 스냅샷을 남겨야 금요일 주간 분석이 가능하다.
주간 분석의 가치는 예측이 아니라 '지난주 추천이 맞았는지'의 자기 검증에 있다.
"""
from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

__all__ = ["Store"]

# 서버 타임존이 UTC 여도 국내장 기준으로 동작해야 한다.
# datetime.now() 를 그대로 쓰면 UTC 서버에서 날짜와 시간창이 어긋난다.
KST = timezone(timedelta(hours=9))


def _now_kst() -> datetime:
    """타임존 정보를 뗀 KST 현재 시각.

    DB 에는 naive 문자열로 저장한다. published / collected 를 같은
    형식으로 통일해야 문자열 비교가 정상 동작한다.
    """
    return datetime.now(KST).replace(tzinfo=None)


def _now_str() -> str:
    return _now_kst().isoformat(timespec="seconds")

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS prices (
  ticker TEXT NOT NULL,
  d      TEXT NOT NULL,
  o REAL, h REAL, l REAL, c REAL, v REAL, amt REAL,
  PRIMARY KEY (ticker, d)
);
CREATE INDEX IF NOT EXISTS ix_prices_d ON prices(d);

CREATE TABLE IF NOT EXISTS tickers (
  ticker TEXT PRIMARY KEY,
  name TEXT, market TEXT, sector TEXT,
  market_cap REAL, shares REAL,
  first_seen TEXT, updated TEXT,
  active INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS scans (
  d TEXT NOT NULL, ticker TEXT NOT NULL, name TEXT, price REAL,
  value_score REAL, trend_score REAL, grade TEXT, track TEXT,
  lps REAL, trend_raw REAL, fib_score REAL,
  fib_ratio REAL, fib_below INTEGER, fib_target_price REAL,
  band_mid REAL, band_pos REAL,
  cross_pair TEXT, cross_bars_ago INTEGER,
  confluence INTEGER, sequence_confirm INTEGER,
  PRIMARY KEY (d, ticker)
);
CREATE INDEX IF NOT EXISTS ix_scans_ticker ON scans(ticker);

CREATE TABLE IF NOT EXISTS recos (
  d TEXT NOT NULL, rank INTEGER NOT NULL,
  ticker TEXT, name TEXT, price REAL, slot TEXT, grade TEXT,
  value_score REAL, trend_score REAL, reason TEXT,
  PRIMARY KEY (d, rank)
);
CREATE INDEX IF NOT EXISTS ix_recos_ticker ON recos(ticker);

CREATE TABLE IF NOT EXISTS runs (
  started TEXT, finished TEXT, mode TEXT,
  ok INTEGER, failed INTEGER, elapsed REAL, note TEXT
);

CREATE TABLE IF NOT EXISTS backfill_state (
  ticker TEXT PRIMARY KEY, done_at TEXT, bars INTEGER
);

-- 수동 주입 신용잔고. KRX/증권사 실측을 자동 확보하기 전까지의 경로다.
-- 이 값이 들어오면 LPS 의 credit_heat 가 프록시 캡(1.25점)을 벗고
-- 만점(4.0점)까지 열리며, 청산 계층 6의 '신용잔고율 재급증' 규칙이 살아난다.
-- 확인된 휴장일(공휴일·임시휴장). 주말은 요일로 판정하므로 넣지 않는다.
-- '일자별 전종목' 조회가 0건을 돌려주면 여기에 기록하고, 이후 재요청하지
-- 않는다. 이게 없으면 catchup 기간 내내 같은 공휴일을 매번 다시 요청한다.
CREATE TABLE IF NOT EXISTS non_trading_days (
  d        TEXT PRIMARY KEY,
  reason   TEXT,
  detected TEXT
);

CREATE TABLE IF NOT EXISTS credit_manual (
  ticker  TEXT PRIMARY KEY,
  ratio   REAL,          -- 신용잔고율(%) = 신용잔고주식수 / 상장주식수 x 100
  shares  REAL,          -- 신용잔고주식수 (있으면)
  asof    TEXT,          -- 데이터 기준일 YYYY-MM-DD
  source  TEXT,
  note    TEXT,
  updated TEXT
);

CREATE TABLE IF NOT EXISTS positions (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  ticker         TEXT NOT NULL,
  name           TEXT,
  track          TEXT,              -- VALUE / TREND
  entry_date     TEXT NOT NULL,
  entry_price    REAL NOT NULL,
  qty            INTEGER NOT NULL,
  remaining      INTEGER NOT NULL,
  -- 진입 시점 스냅샷. 절대 재계산하지 않는다.
  entry_p0       REAL,
  entry_band_hi  REAL,
  entry_band_mid REAL,
  entry_band_lo  REAL,
  entry_fib_0382 REAL,
  entry_fib_0618 REAL,
  entry_cross_low REAL,
  entry_credit_ratio REAL,
  -- 파생 상태. 매 실행 시 시세로부터 재계산해 덮어쓴다.
  exits_done     INTEGER DEFAULT 0,
  peak_close     REAL,
  band_break_streak INTEGER DEFAULT 0,
  ma_break_streak   INTEGER DEFAULT 0,
  defer_until    TEXT,
  status         TEXT DEFAULT 'OPEN',
  opened_by      TEXT,
  note           TEXT,
  updated        TEXT
);
CREATE INDEX IF NOT EXISTS ix_pos_status ON positions(status);
CREATE INDEX IF NOT EXISTS ix_pos_ticker ON positions(ticker);

CREATE TABLE IF NOT EXISTS exit_log (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  position_id  INTEGER NOT NULL,
  d            TEXT,
  ticker       TEXT,
  name         TEXT,
  layer        INTEGER,
  rule         TEXT,
  action       TEXT,               -- EXIT_ALL / TRIM
  ratio        REAL,
  qty          INTEGER,
  signal_price REAL,
  ret_pct      REAL,
  net_ret_pct  REAL,
  reason       TEXT,
  executed     INTEGER DEFAULT 0,  -- 0=신호만, 1=체결 확인
  fill_price   REAL,
  fill_qty     INTEGER,
  filled_at    TEXT,
  note         TEXT,
  created      TEXT
);
CREATE INDEX IF NOT EXISTS ix_exit_pos ON exit_log(position_id);
CREATE INDEX IF NOT EXISTS ix_exit_exec ON exit_log(executed);

CREATE TABLE IF NOT EXISTS news (
  id         TEXT PRIMARY KEY,   -- sha1(정규화 제목 + 매체)
  d          TEXT,               -- 수집 기준일 YYYY-MM-DD
  published  TEXT,               -- 발행 시각 (KST, ISO8601)
  title      TEXT,
  title_norm TEXT,
  url        TEXT,
  source     TEXT,               -- 매체명
  origin     TEXT,               -- NAVER / DART / GOOGLE / YAHOO
  region     TEXT,               -- KR / US / GLOBAL
  category   TEXT,
  cluster_id TEXT,               -- 같은 사건 묶음
  cluster_n  INTEGER DEFAULT 1,  -- 그 사건을 다룬 매체 수
  importance REAL,
  lang       TEXT,
  summary    TEXT,
  collected  TEXT
);
CREATE INDEX IF NOT EXISTS ix_news_d ON news(d);
CREATE INDEX IF NOT EXISTS ix_news_cluster ON news(cluster_id);
CREATE INDEX IF NOT EXISTS ix_news_cat ON news(category);

CREATE TABLE IF NOT EXISTS news_tickers (
  news_id TEXT NOT NULL,
  ticker  TEXT NOT NULL,
  name    TEXT,
  PRIMARY KEY (news_id, ticker)
);
CREATE INDEX IF NOT EXISTS ix_news_tickers_t ON news_tickers(ticker);

CREATE TABLE IF NOT EXISTS flags (
  ticker         TEXT PRIMARY KEY,
  admin_issue    INTEGER DEFAULT 0,   -- 관리종목
  alert_issue    INTEGER DEFAULT 0,   -- 투자주의환기종목
  audit_refusal  INTEGER DEFAULT 0,   -- 감사의견 거절/한정
  capital_impair REAL,                -- 자본잠식률(%)  음수=건전
  recent_offering INTEGER DEFAULT 0,  -- 최근 대규모 유상증자/CB
  halt_history   INTEGER DEFAULT 0,   -- 거래정지 이력
  penny_risk     INTEGER DEFAULT 0,   -- 1,000원 미만 장기 지속 (관리종목 지정 위험)
  sources        TEXT,
  note           TEXT,
  updated        TEXT,
  -- 자본잠식만 별도 타임스탬프를 둔다. updated 는 행 전체 기준이라
  -- 로컬 판정이 매일 갱신되면 DART TTL 이 영원히 만료되지 않는다.
  capital_impair_at TEXT
);
"""

_COLS = {"o": "시가", "h": "고가", "l": "저가", "c": "종가",
         "v": "거래량", "amt": "거래대금"}


def _d(x) -> str:
    if isinstance(x, str):
        return x[:10]
    return pd.Timestamp(x).strftime("%Y-%m-%d")


class Store:
    def __init__(self, path: str | Path = "data/quant.db"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _conn(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path, timeout=30.0)
        con.execute("PRAGMA foreign_keys=ON")
        return con

    # 이미 만들어진 DB 에는 CREATE TABLE IF NOT EXISTS 가 컬럼을 추가하지
    # 않는다. 스키마가 늘어날 때마다 여기에 한 줄씩 넣는다.
    _MIGRATIONS = (
        ("flags", "capital_impair_at", "TEXT"),
    )

    def _init(self) -> None:
        with closing(self._conn()) as con:
            con.executescript(_SCHEMA)
            for table, col, coltype in self._MIGRATIONS:
                have = {r[1] for r in con.execute(
                    f"PRAGMA table_info({table})").fetchall()}
                if col not in have:
                    con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}")
            con.commit()

    # ────────────────────────── 시세 ──────────────────────────
    def upsert_prices(self, ticker: str, df: pd.DataFrame) -> int:
        """한 종목의 OHLCV 적재. 컬럼은 한글 규격을 기대한다."""
        if df is None or df.empty:
            return 0
        rows = []
        has_amt = "거래대금" in df.columns
        for idx, r in df.iterrows():
            rows.append((
                ticker, _d(idx),
                float(r["시가"]), float(r["고가"]), float(r["저가"]),
                float(r["종가"]), float(r["거래량"]),
                float(r["거래대금"]) if has_amt and pd.notna(r["거래대금"]) else None,
            ))
        with closing(self._conn()) as con:
            con.executemany(
                "INSERT INTO prices(ticker,d,o,h,l,c,v,amt) VALUES(?,?,?,?,?,?,?,?) "
                "ON CONFLICT(ticker,d) DO UPDATE SET "
                "o=excluded.o,h=excluded.h,l=excluded.l,c=excluded.c,"
                "v=excluded.v,amt=excluded.amt",
                rows,
            )
            con.commit()
        return len(rows)

    def upsert_cross_section(self, trade_date, df: pd.DataFrame) -> int:
        """'특정 일자 전종목' 한 판을 통째로 적재.

        df : index=종목코드, columns=['시가','고가','저가','종가','거래량','거래대금']
        일일 증분 업데이트의 핵심 경로다. 요청 1회로 2,800종목이 들어온다.
        """
        if df is None or df.empty:
            return 0
        ds = _d(trade_date)
        has_amt = "거래대금" in df.columns
        rows = []
        for code, r in df.iterrows():
            try:
                if float(r["거래량"]) <= 0:
                    continue  # 거래정지/휴장 종목은 지표를 왜곡한다
                rows.append((
                    str(code).zfill(6), ds,
                    float(r["시가"]), float(r["고가"]), float(r["저가"]),
                    float(r["종가"]), float(r["거래량"]),
                    float(r["거래대금"]) if has_amt and pd.notna(r["거래대금"]) else None,
                ))
            except (TypeError, ValueError, KeyError):
                continue
        if not rows:
            return 0
        with closing(self._conn()) as con:
            con.executemany(
                "INSERT INTO prices(ticker,d,o,h,l,c,v,amt) VALUES(?,?,?,?,?,?,?,?) "
                "ON CONFLICT(ticker,d) DO UPDATE SET "
                "o=excluded.o,h=excluded.h,l=excluded.l,c=excluded.c,"
                "v=excluded.v,amt=excluded.amt",
                rows,
            )
            con.commit()
        return len(rows)

    def load_ohlcv(self, ticker: str, days: int = 400) -> pd.DataFrame | None:
        """스캔용 OHLCV 로드. 네트워크 없음."""
        q = ("SELECT d,o,h,l,c,v,amt FROM prices WHERE ticker=? "
             "ORDER BY d DESC LIMIT ?")
        with closing(self._conn()) as con:
            df = pd.read_sql_query(q, con, params=(ticker, int(days)))
        if df.empty:
            return None
        df = df.sort_values("d")
        df.index = pd.to_datetime(df["d"])
        out = df[["o", "h", "l", "c", "v", "amt"]].rename(columns=_COLS)
        out = out.astype("float64")
        if out["거래대금"].isna().all():
            out = out.drop(columns=["거래대금"])
        return out[out["거래량"] > 0]

    def last_price_date(self) -> str | None:
        with closing(self._conn()) as con:
            row = con.execute("SELECT MAX(d) FROM prices").fetchone()
        return row[0] if row and row[0] else None

    def existing_dates(self, since: str | None = None) -> set[str]:
        """이미 적재된 거래일 집합. 증분 업데이트에서 중복 요청을 막는다."""
        q = "SELECT DISTINCT d FROM prices"
        params: tuple = ()
        if since:
            q += " WHERE d>=?"
            params = (since,)
        with closing(self._conn()) as con:
            return {d for (d,) in con.execute(q, params).fetchall()}

    def has_price_date(self, d) -> bool:
        """그 날짜의 시세가 적재돼 있는가. 거래일 확정 판정에 쓴다."""
        ds = _d(d)
        with closing(self._conn()) as con:
            row = con.execute(
                "SELECT 1 FROM prices WHERE d=? LIMIT 1", (ds,)).fetchone()
        return row is not None

    def mark_non_trading_day(self, d, reason: str = "no data") -> None:
        ds = _d(d)
        with closing(self._conn()) as con:
            con.execute(
                "INSERT INTO non_trading_days(d,reason,detected) VALUES(?,?,?) "
                "ON CONFLICT(d) DO UPDATE SET reason=excluded.reason",
                (ds, reason, _now_str()))
            con.commit()

    def is_known_non_trading(self, d) -> bool:
        ds = _d(d)
        with closing(self._conn()) as con:
            row = con.execute(
                "SELECT 1 FROM non_trading_days WHERE d=?", (ds,)).fetchone()
        return row is not None

    def known_non_trading_days(self, since: str | None = None) -> set[str]:
        q = "SELECT d FROM non_trading_days"
        params: tuple = ()
        if since:
            q += " WHERE d>=?"
            params = (since,)
        with closing(self._conn()) as con:
            return {d for (d,) in con.execute(q, params).fetchall()}

    def has_scan(self, d) -> bool:
        """그 거래일의 스캔 스냅샷이 이미 있는가. 중복 실행 방지."""
        ds = _d(d)
        with closing(self._conn()) as con:
            row = con.execute(
                "SELECT 1 FROM scans WHERE d=? LIMIT 1", (ds,)).fetchone()
        return row is not None

    def has_exit_signal(self, d) -> bool:
        ds = _d(d)
        with closing(self._conn()) as con:
            row = con.execute(
                "SELECT 1 FROM exit_log WHERE d=? LIMIT 1", (ds,)).fetchone()
        return row is not None

    def bar_count(self, min_bars: int = 120) -> int:
        """min_bars 이상 확보된 종목 수. 백필 진척도 확인용."""
        with closing(self._conn()) as con:
            row = con.execute(
                "SELECT COUNT(*) FROM (SELECT ticker FROM prices "
                "GROUP BY ticker HAVING COUNT(*)>=?)", (int(min_bars),)).fetchone()
        return int(row[0]) if row else 0

    def price_matrix(self, tickers: list[str] | None = None,
                     days: int = 30) -> pd.DataFrame:
        """종가 피벗 (index=날짜, columns=종목코드). 시장 대조군 계산용."""
        with closing(self._conn()) as con:
            dates = pd.read_sql_query(
                "SELECT DISTINCT d FROM prices ORDER BY d DESC LIMIT ?",
                con, params=(int(days),))
            if dates.empty:
                return pd.DataFrame()
            start = dates["d"].min()
            df = pd.read_sql_query(
                "SELECT d,ticker,c FROM prices WHERE d>=?", con, params=(start,))
        if df.empty:
            return pd.DataFrame()
        m = df.pivot(index="d", columns="ticker", values="c")
        m.index = pd.to_datetime(m.index)
        if tickers:
            keep = [t for t in tickers if t in m.columns]
            m = m[keep]
        return m.sort_index()

    # ────────────────────────── 종목 마스터 ──────────────────────────
    def upsert_tickers(self, rows: list[dict]) -> int:
        now = _now_str()
        payload = [(r["ticker"], r.get("name"), r.get("market"),
                    r.get("sector"), r.get("market_cap"), r.get("shares"),
                    now, now, 1) for r in rows]
        with closing(self._conn()) as con:
            con.executemany(
                "INSERT INTO tickers(ticker,name,market,sector,market_cap,shares,"
                "first_seen,updated,active) VALUES(?,?,?,?,?,?,?,?,?) "
                # COALESCE 로 감싼 이유: 업종 보강처럼 일부 필드만 담아 호출하는
                # 경로가 있어서, 그때 기존 시총/상장주식수가 지워지면 안 된다.
                "ON CONFLICT(ticker) DO UPDATE SET "
                "name=COALESCE(excluded.name,name),"
                "market=COALESCE(excluded.market,market),"
                "sector=COALESCE(excluded.sector,sector),"
                "market_cap=COALESCE(excluded.market_cap,market_cap),"
                "shares=COALESCE(excluded.shares,shares),"
                "updated=excluded.updated,active=1",
                payload,
            )
            con.commit()
        return len(payload)

    def mark_inactive(self, alive: set[str]) -> int:
        """오늘 목록에 없는 종목은 상장폐지/거래정지로 보고 비활성화."""
        with closing(self._conn()) as con:
            cur = con.execute("SELECT ticker FROM tickers WHERE active=1")
            dead = [(t,) for (t,) in cur.fetchall() if t not in alive]
            if dead:
                con.executemany("UPDATE tickers SET active=0 WHERE ticker=?", dead)
                con.commit()
        return len(dead)

    def active_tickers(self) -> dict:
        with closing(self._conn()) as con:
            rows = con.execute(
                "SELECT ticker,name FROM tickers WHERE active=1").fetchall()
        return {t: (n or t) for t, n in rows}

    def ticker_meta(self) -> pd.DataFrame:
        with closing(self._conn()) as con:
            return pd.read_sql_query(
                "SELECT ticker,name,market,sector,market_cap,shares "
                "FROM tickers WHERE active=1", con).set_index("ticker")

    # ────────────────────────── 스캔 스냅샷 ──────────────────────────
    def save_scan(self, trade_date, results: list,
                  fib_target: float = 0.618) -> int:
        ds = _d(trade_date)
        rows = []
        for r in results:
            if r.excluded:
                continue
            f, q, t = r.fib, r.liq, r.trend
            cross = t.best_cross if (t and t.best_cross
                                     and t.best_cross.kind == "GOLDEN") else None
            rows.append((
                ds, r.ticker, r.name, r.price,
                r.value_score, r.trend_score, r.grade, r.track,
                q.score if q else None, t.score if t else None,
                f.score if f else None,
                f.ratio if f else None,
                int(f.below_target) if f else None,
                f.levels.get(fib_target) if f else None,
                q.band_mid if q else None, q.band_pos if q else None,
                cross.pair if cross else None,
                cross.bars_ago if cross else None,
                int(r.confluence), int(r.sequence_confirm),
            ))
        if not rows:
            return 0
        with closing(self._conn()) as con:
            con.execute("DELETE FROM scans WHERE d=?", (ds,))
            con.executemany(
                "INSERT INTO scans(d,ticker,name,price,value_score,trend_score,"
                "grade,track,lps,trend_raw,fib_score,fib_ratio,fib_below,"
                "fib_target_price,band_mid,band_pos,cross_pair,cross_bars_ago,"
                "confluence,sequence_confirm) "
                "VALUES(" + ",".join("?" * 20) + ")", rows)
            con.commit()
        return len(rows)

    def save_recos(self, trade_date, picks: list[tuple]) -> int:
        """picks : [(slot, ScreenResult), ...] 순위 순서대로"""
        ds = _d(trade_date)
        rows = [(ds, i, r.ticker, r.name, r.price, slot, r.grade,
                 r.value_score, r.trend_score, " / ".join(r.reasons))
                for i, (slot, r) in enumerate(picks, 1)]
        if not rows:
            return 0
        with closing(self._conn()) as con:
            con.execute("DELETE FROM recos WHERE d=?", (ds,))
            con.executemany(
                "INSERT INTO recos(d,rank,ticker,name,price,slot,grade,"
                "value_score,trend_score,reason) VALUES(?,?,?,?,?,?,?,?,?,?)", rows)
            con.commit()
        return len(rows)

    def scan_history(self, days: int = 10) -> pd.DataFrame:
        with closing(self._conn()) as con:
            ds = pd.read_sql_query(
                "SELECT DISTINCT d FROM scans ORDER BY d DESC LIMIT ?",
                con, params=(int(days),))
            if ds.empty:
                return pd.DataFrame()
            return pd.read_sql_query(
                "SELECT * FROM scans WHERE d>=? ORDER BY d", con,
                params=(ds["d"].min(),))

    def reco_history(self, days: int = 10) -> pd.DataFrame:
        with closing(self._conn()) as con:
            ds = pd.read_sql_query(
                "SELECT DISTINCT d FROM recos ORDER BY d DESC LIMIT ?",
                con, params=(int(days),))
            if ds.empty:
                return pd.DataFrame()
            return pd.read_sql_query(
                "SELECT * FROM recos WHERE d>=? ORDER BY d,rank", con,
                params=(ds["d"].min(),))

    # ────────────────────────── 백필 체크포인트 ──────────────────────────
    def backfill_done(self) -> set[str]:
        with closing(self._conn()) as con:
            return {t for (t,) in con.execute(
                "SELECT ticker FROM backfill_state").fetchall()}

    def mark_backfilled(self, ticker: str, bars: int) -> None:
        with closing(self._conn()) as con:
            con.execute(
                "INSERT INTO backfill_state(ticker,done_at,bars) VALUES(?,?,?) "
                "ON CONFLICT(ticker) DO UPDATE SET done_at=excluded.done_at,"
                "bars=excluded.bars",
                (ticker, _now_str(), bars))
            con.commit()

    # ────────────────────────── 포지션 ──────────────────────────
    _POS_COLS = (
        "id", "ticker", "name", "track", "entry_date", "entry_price", "qty",
        "remaining", "entry_p0", "entry_band_hi", "entry_band_mid",
        "entry_band_lo", "entry_fib_0382", "entry_fib_0618", "entry_cross_low",
        "entry_credit_ratio", "exits_done", "peak_close", "band_break_streak",
        "ma_break_streak", "defer_until", "status", "opened_by", "note",
    )

    def open_position(self, ticker: str, name: str, track: str,
                      entry_date, entry_price: float, qty: int,
                      snapshot: dict | None = None,
                      opened_by: str = "manual", note: str = "") -> int:
        """포지션 개시. snapshot 의 밴드/피보 값이 청산선의 기준이 된다.

        여기서 저장한 값은 이후 절대 갱신하지 않는다. 매일 P0 를 다시
        계산하면 주가 하락에 맞춰 손절선도 내려가 손절이 영원히 발동하지
        않는다. 계좌를 녹이는 전형적인 방식이다.
        """
        s = snapshot or {}
        with closing(self._conn()) as con:
            cur = con.execute(
                "INSERT INTO positions(ticker,name,track,entry_date,entry_price,"
                "qty,remaining,entry_p0,entry_band_hi,entry_band_mid,"
                "entry_band_lo,entry_fib_0382,entry_fib_0618,entry_cross_low,"
                "entry_credit_ratio,exits_done,peak_close,status,opened_by,"
                "note,updated) VALUES(" + ",".join("?" * 21) + ")",
                (ticker, name, track, _d(entry_date), float(entry_price),
                 int(qty), int(qty),
                 s.get("p0"), s.get("band_hi"), s.get("band_mid"),
                 s.get("band_lo"), s.get("fib_0382"), s.get("fib_0618"),
                 s.get("cross_low"), s.get("credit_ratio"),
                 0, float(entry_price), "OPEN", opened_by, note, _now_str()))
            con.commit()
            return int(cur.lastrowid)

    def list_positions(self, status: str | None = "OPEN") -> list:
        from .contracts import Position

        q = f"SELECT {','.join(self._POS_COLS)} FROM positions"
        params: tuple = ()
        if status:
            q += " WHERE status=?"
            params = (status,)
        q += " ORDER BY entry_date, id"
        with closing(self._conn()) as con:
            rows = con.execute(q, params).fetchall()
        out = []
        for r in rows:
            kv = dict(zip(self._POS_COLS, r))
            kv["exits_done"] = int(kv["exits_done"] or 0)
            kv["band_break_streak"] = int(kv["band_break_streak"] or 0)
            kv["ma_break_streak"] = int(kv["ma_break_streak"] or 0)
            kv["qty"] = int(kv["qty"])
            kv["remaining"] = int(kv["remaining"])
            out.append(Position(**kv))
        return out

    def touch_position_state(self, pos_id: int, peak_close: float | None,
                             band_break_streak: int, ma_break_streak: int,
                             defer_until: str | None) -> None:
        """파생 상태 갱신. 진입 스냅샷은 건드리지 않는다."""
        with closing(self._conn()) as con:
            con.execute(
                "UPDATE positions SET peak_close=?,band_break_streak=?,"
                "ma_break_streak=?,defer_until=?,updated=? WHERE id=?",
                (peak_close, int(band_break_streak), int(ma_break_streak),
                 defer_until, _now_str(), int(pos_id)))
            con.commit()

    def record_exit_signal(self, dec, trade_date=None) -> int:
        """청산 신호 기록. 체결은 별도 확인(confirm_exit)으로 반영한다.

        목표 익절(계층 4·5)만 exits_done 비트를 세워 재발송을 막는다.
        손절·트레일링·시간 청산은 비트를 세우지 않는다. 포지션이 닫힐
        때까지 매일 다시 알려야 하기 때문이다.
        """
        from .contracts import DONE_TAKE1, DONE_TAKE2

        ds = _d(trade_date) if trade_date else _now_kst().strftime("%Y-%m-%d")
        with closing(self._conn()) as con:
            cur = con.execute(
                "INSERT INTO exit_log(position_id,d,ticker,name,layer,rule,"
                "action,ratio,qty,signal_price,ret_pct,net_ret_pct,reason,"
                "executed,created) VALUES(" + ",".join("?" * 15) + ")",
                (dec.position_id, ds, dec.ticker, dec.name, dec.layer, dec.rule,
                 dec.action, dec.ratio, dec.qty, dec.signal_price,
                 dec.ret_pct, dec.net_ret_pct, dec.reason, 0, _now_str()))
            bit = 0
            if dec.layer == 5:
                bit = DONE_TAKE1
            elif dec.layer == 4:
                bit = DONE_TAKE2
            if bit and dec.position_id:
                con.execute(
                    "UPDATE positions SET exits_done=exits_done|?,updated=? "
                    "WHERE id=?", (bit, _now_str(), int(dec.position_id)))
            con.commit()
            return int(cur.lastrowid)

    def pending_exits(self, days: int = 10) -> pd.DataFrame:
        since = (_now_kst() - timedelta(days=days)).strftime("%Y-%m-%d")
        with closing(self._conn()) as con:
            return pd.read_sql_query(
                "SELECT * FROM exit_log WHERE executed=0 AND d>=? "
                "ORDER BY layer, d DESC", con, params=(since,))

    def confirm_exit(self, log_id: int, fill_price: float,
                     fill_qty: int | None = None) -> dict:
        """체결 확인. 여기서만 remaining 이 줄어든다.

        신호만으로 잔량을 깎으면 실제 체결이 안 된 물량을 팔았다고
        착각한다. 그래서 두 단계로 나눈다.
        """
        with closing(self._conn()) as con:
            row = con.execute(
                "SELECT position_id,qty,ticker,executed FROM exit_log WHERE id=?",
                (int(log_id),)).fetchone()
            if row is None:
                raise ValueError(f"exit_log {log_id} 없음")
            pos_id, sig_qty, ticker, executed = row
            if executed:
                raise ValueError(f"exit_log {log_id} 는 이미 체결 처리됨")

            qty = int(fill_qty if fill_qty is not None else sig_qty)
            prow = con.execute(
                "SELECT remaining FROM positions WHERE id=?", (pos_id,)).fetchone()
            remaining = int(prow[0]) if prow else 0
            qty = max(0, min(qty, remaining))
            new_remaining = remaining - qty
            status = "CLOSED" if new_remaining <= 0 else "OPEN"

            con.execute(
                "UPDATE exit_log SET executed=1,fill_price=?,fill_qty=?,"
                "filled_at=? WHERE id=?",
                (float(fill_price), qty, _now_str(), int(log_id)))
            con.execute(
                "UPDATE positions SET remaining=?,status=?,updated=? WHERE id=?",
                (new_remaining, status, _now_str(), pos_id))
            con.commit()
        return {"position_id": pos_id, "ticker": ticker, "filled_qty": qty,
                "remaining": new_remaining, "status": status}

    def close_position(self, pos_id: int, note: str = "manual close") -> None:
        with closing(self._conn()) as con:
            con.execute(
                "UPDATE positions SET status='CLOSED',remaining=0,note=?,"
                "updated=? WHERE id=?", (note, _now_str(), int(pos_id)))
            con.commit()

    def exit_log_history(self, days: int = 60) -> pd.DataFrame:
        since = (_now_kst() - timedelta(days=days)).strftime("%Y-%m-%d")
        with closing(self._conn()) as con:
            return pd.read_sql_query(
                "SELECT * FROM exit_log WHERE d>=? ORDER BY d DESC,id DESC",
                con, params=(since,))

    # ────────────────────────── 뉴스 ──────────────────────────
    def upsert_news(self, rows: list[dict]) -> int:
        """뉴스 적재. id 가 정규화 제목 해시라 재실행해도 중복이 쌓이지 않는다."""
        if not rows:
            return 0
        now = _now_str()
        payload = [(
            r["id"], r.get("d"), r.get("published"), r.get("title"),
            r.get("title_norm"), r.get("url"), r.get("source"), r.get("origin"),
            r.get("region"), r.get("category"), r.get("cluster_id"),
            int(r.get("cluster_n", 1) or 1), float(r.get("importance", 0.0) or 0.0),
            r.get("lang"), r.get("summary"), now,
        ) for r in rows]
        with closing(self._conn()) as con:
            con.executemany(
                "INSERT INTO news(id,d,published,title,title_norm,url,source,"
                "origin,region,category,cluster_id,cluster_n,importance,lang,"
                "summary,collected) VALUES(" + ",".join("?" * 16) + ") "
                "ON CONFLICT(id) DO UPDATE SET "
                "cluster_id=excluded.cluster_id,cluster_n=excluded.cluster_n,"
                "importance=excluded.importance,"
                "category=COALESCE(excluded.category,category),"
                "summary=COALESCE(excluded.summary,summary)",
                payload,
            )
            con.commit()
        return len(payload)

    def link_news_tickers(self, pairs: list[tuple]) -> int:
        """(news_id, ticker, name) 매핑 적재."""
        if not pairs:
            return 0
        with closing(self._conn()) as con:
            con.executemany(
                "INSERT OR IGNORE INTO news_tickers(news_id,ticker,name) "
                "VALUES(?,?,?)", pairs)
            con.commit()
        return len(pairs)

    def news_ids(self, days: int = 3) -> set[str]:
        """최근 수집분 id. 재수집 시 중복 처리를 건너뛰는 데 쓴다."""
        since = (_now_kst() - timedelta(days=days)).strftime("%Y-%m-%d")
        with closing(self._conn()) as con:
            return {i for (i,) in con.execute(
                "SELECT id FROM news WHERE d>=?", (since,)).fetchall()}

    def news_since(self, hours: int = 24, region: str | None = None,
                   min_importance: float = 0.0) -> pd.DataFrame:
        cutoff = (_now_kst() - timedelta(hours=hours)).isoformat(timespec="seconds")
        q = ("SELECT * FROM news WHERE COALESCE(published,collected)>=? "
             "AND importance>=?")
        params: list = [cutoff, float(min_importance)]
        if region:
            q += " AND region=?"
            params.append(region)
        q += " ORDER BY importance DESC, cluster_n DESC, published DESC"
        with closing(self._conn()) as con:
            return pd.read_sql_query(q, con, params=params)

    def news_ticker_map(self, news_ids: list[str]) -> pd.DataFrame:
        if not news_ids:
            return pd.DataFrame(columns=["news_id", "ticker", "name"])
        marks = ",".join("?" * len(news_ids))
        with closing(self._conn()) as con:
            return pd.read_sql_query(
                f"SELECT news_id,ticker,name FROM news_tickers "
                f"WHERE news_id IN ({marks})", con, params=news_ids)

    def news_theme_counts(self, days: int = 14) -> pd.DataFrame:
        """일자 x 카테고리 건수. 주간 테마 부침 분석용."""
        since = (_now_kst() - timedelta(days=days)).strftime("%Y-%m-%d")
        with closing(self._conn()) as con:
            return pd.read_sql_query(
                "SELECT d,category,COUNT(DISTINCT cluster_id) AS clusters,"
                "COUNT(*) AS items FROM news WHERE d>=? GROUP BY d,category",
                con, params=(since,))

    # ────────────────────────── 배제 플래그 ──────────────────────────
    _FLAG_FIELDS = ("admin_issue", "alert_issue", "audit_refusal",
                    "capital_impair", "recent_offering", "halt_history",
                    "penny_risk")

    def upsert_flags(self, rows: list[dict]) -> int:
        """부분 갱신. 전달하지 않은 필드는 기존 값을 보존한다.

        관리종목은 KRX, 자본잠식은 DART, 거래정지는 로컬 시세에서 오므로
        공급원마다 채우는 필드가 다르다. COALESCE 로 서로 지우지 않게 한다.
        """
        if not rows:
            return 0
        now = _now_str()
        payload = []
        for r in rows:
            payload.append((
                r["ticker"],
                r.get("admin_issue"), r.get("alert_issue"), r.get("audit_refusal"),
                r.get("capital_impair"), r.get("recent_offering"),
                r.get("halt_history"), r.get("penny_risk"),
                r.get("source"), r.get("note"), now,
            ))
        sets = ",".join(
            f"{f}=COALESCE(excluded.{f},{f})" for f in self._FLAG_FIELDS)
        with closing(self._conn()) as con:
            con.executemany(
                "INSERT INTO flags(ticker,admin_issue,alert_issue,audit_refusal,"
                "capital_impair,recent_offering,halt_history,penny_risk,"
                "sources,note,updated,capital_impair_at) "
                # 마지막 두 개는 CASE 용 추가 바인딩이다(잠식값, 시각).
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,"
                "CASE WHEN ? IS NOT NULL THEN ? ELSE NULL END) "
                "ON CONFLICT(ticker) DO UPDATE SET " + sets + ","
                # sources 를 이어붙이면 매일 문자열이 늘어난다. 최종
                # 기록자만 남긴다. 상세 근거는 note 가 담는다.
                "sources=COALESCE(excluded.sources,flags.sources),"
                "note=COALESCE(excluded.note,flags.note),"
                "updated=excluded.updated,"
                # 자본잠식 값을 실제로 넣은 호출만 타임스탬프를 갱신한다.
                "capital_impair_at=CASE WHEN excluded.capital_impair IS NOT NULL "
                "THEN excluded.updated ELSE flags.capital_impair_at END",
                [p + (p[4], p[10]) for p in payload],
            )
            con.commit()
        return len(payload)

    def clear_flag_field(self, field: str) -> None:
        """공급원 갱신 전 해당 필드를 초기화한다.

        관리종목 해제를 반영하려면 이게 필요하다. 없으면 한 번 켜진
        플래그가 영구히 남아 정상화된 종목이 계속 배제된다.
        """
        if field not in self._FLAG_FIELDS:
            raise ValueError(f"알 수 없는 필드: {field}")
        with closing(self._conn()) as con:
            con.execute(f"UPDATE flags SET {field}=NULL")
            con.commit()

    def load_flags(self) -> dict:
        """{ticker: {한글키: 값}} 형태. screener.check_exclusion 계약에 맞춘다."""
        with closing(self._conn()) as con:
            cur = con.execute(
                "SELECT ticker,admin_issue,alert_issue,audit_refusal,"
                "capital_impair,recent_offering,halt_history,penny_risk FROM flags")
            rows = cur.fetchall()
        out = {}
        for (t, adm, alt, aud, imp, off, halt, penny) in rows:
            out[t] = {
                "관리종목": bool(adm),
                "투자주의환기": bool(alt),
                "감사의견거절": bool(aud),
                "자본잠식": bool(imp is not None and imp >= 50.0),
                "자본잠식률": imp,
                "대규모증자": bool(off),
                "거래정지이력": bool(halt),
                "동전주위험": bool(penny),
            }
        return out

    # 필드별 타임스탬프 컬럼. 없으면 행 전체의 updated 를 쓴다.
    _FLAG_TS_COL = {"capital_impair": "capital_impair_at"}

    def flag_staleness(self, field: str = "capital_impair") -> dict:
        """{ticker: 갱신시각} — TTL 기반 재조회 대상 선별용.

        capital_impair 는 전용 타임스탬프를 본다. updated 를 쓰면 로컬
        판정이 매일 행을 갱신하기 때문에 DART TTL 이 영원히 만료되지 않는다.
        """
        if field not in self._FLAG_FIELDS:
            raise ValueError(f"알 수 없는 필드: {field}")
        ts = self._FLAG_TS_COL.get(field, "updated")
        with closing(self._conn()) as con:
            cur = con.execute(
                f"SELECT ticker,{ts} FROM flags "
                f"WHERE {field} IS NOT NULL AND {ts} IS NOT NULL")
            return dict(cur.fetchall())

    # ────────────────────────── 수동 신용잔고 ──────────────────────────
    def upsert_credit(self, rows: list[dict]) -> int:
        """수동 주입 신용잔고. 전체 교체가 아니라 종목별 갱신이다."""
        if not rows:
            return 0
        now = _now_str()
        payload = [(r["ticker"], r.get("ratio"), r.get("shares"),
                    r.get("asof"), r.get("source", "manual"), r.get("note"), now)
                   for r in rows]
        with closing(self._conn()) as con:
            con.executemany(
                "INSERT INTO credit_manual(ticker,ratio,shares,asof,source,"
                "note,updated) VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(ticker) DO UPDATE SET "
                "ratio=COALESCE(excluded.ratio,ratio),"
                "shares=COALESCE(excluded.shares,shares),"
                "asof=COALESCE(excluded.asof,asof),"
                "source=excluded.source,"
                "note=COALESCE(excluded.note,note),"
                "updated=excluded.updated", payload)
            con.commit()
        return len(payload)

    def load_credit_ratios(self, max_age_days: int | None = 14) -> dict:
        """{ticker: 신용잔고율(%)}.

        신용잔고는 매일 변한다. 오래된 값을 그대로 쓰면 청산 판정이
        과거 상태로 굳으므로 기준일이 오래된 건 버린다.
        """
        with closing(self._conn()) as con:
            rows = con.execute(
                "SELECT ticker,ratio,asof FROM credit_manual "
                "WHERE ratio IS NOT NULL").fetchall()
        out: dict = {}
        cutoff = None
        if max_age_days is not None:
            cutoff = (_now_kst() - timedelta(days=max_age_days)).strftime("%Y-%m-%d")
        for t, ratio, asof in rows:
            if cutoff and asof and str(asof)[:10] < cutoff:
                continue
            out[t] = float(ratio)
        return out

    def credit_coverage(self) -> dict:
        """신용잔고 확보 현황. 몇 종목이 실측으로 채점되는지 확인용."""
        with closing(self._conn()) as con:
            total = con.execute(
                "SELECT COUNT(*) FROM tickers WHERE active=1").fetchone()[0]
            have = con.execute(
                "SELECT COUNT(*) FROM credit_manual WHERE ratio IS NOT NULL"
            ).fetchone()[0]
            newest = con.execute(
                "SELECT MAX(asof) FROM credit_manual").fetchone()[0]
        return {"active": int(total), "with_credit": int(have), "asof": newest}

    # ────────────────────────── CSV 내보내기 ──────────────────────────
    # 김승곤 차장이 신용잔고 증감률과 대조할 수 있도록 전종목 점수표를
    # 엑셀로 떨어뜨린다. 인코딩은 utf-8-sig 여야 한다. BOM 이 없으면
    # 한글 윈도우 엑셀이 UTF-8 CSV 를 깨뜨린다.
    _EXPORTS = {
        "scans": "SELECT * FROM scans WHERE d>=? ORDER BY d,value_score DESC",
        "recos": "SELECT * FROM recos WHERE d>=? ORDER BY d,rank",
        "exit_log": "SELECT * FROM exit_log WHERE d>=? ORDER BY d DESC,id DESC",
    }
    _EXPORTS_FULL = {
        "flags": "SELECT * FROM flags",
        "positions": "SELECT * FROM positions ORDER BY id",
        "tickers": "SELECT ticker,name,market,sector,market_cap,shares "
                   "FROM tickers WHERE active=1 ORDER BY ticker",
        "credit_manual": "SELECT * FROM credit_manual ORDER BY ticker",
    }

    def export_csv(self, out_dir: str | Path = "data/export",
                   days: int = 10, tag: str | None = None) -> dict:
        """스냅샷·추천·플래그를 CSV 로 내보낸다."""
        outp = Path(out_dir)
        outp.mkdir(parents=True, exist_ok=True)
        stamp = tag or _now_kst().strftime("%Y%m%d")
        since = (_now_kst() - timedelta(days=days)).strftime("%Y-%m-%d")

        written: dict = {}
        with closing(self._conn()) as con:
            for name, sql in self._EXPORTS.items():
                df = pd.read_sql_query(sql, con, params=(since,))
                path = outp / f"{name}_{stamp}.csv"
                df.to_csv(path, index=False, encoding="utf-8-sig")
                written[name] = {"rows": int(len(df)), "path": str(path)}
            for name, sql in self._EXPORTS_FULL.items():
                df = pd.read_sql_query(sql, con)
                path = outp / f"{name}_{stamp}.csv"
                df.to_csv(path, index=False, encoding="utf-8-sig")
                written[name] = {"rows": int(len(df)), "path": str(path)}
        return written

    # ────────────────────────── 실행 이력 ──────────────────────────
    def log_run(self, mode: str, started: datetime, ok: int, failed: int,
                note: str = "") -> None:
        fin = datetime.now()
        with closing(self._conn()) as con:
            con.execute(
                "INSERT INTO runs(started,finished,mode,ok,failed,elapsed,note) "
                "VALUES(?,?,?,?,?,?,?)",
                (started.isoformat(timespec="seconds"),
                 fin.isoformat(timespec="seconds"), mode, ok, failed,
                 (fin - started).total_seconds(), note))
            con.commit()
