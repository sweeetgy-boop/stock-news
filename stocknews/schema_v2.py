# -*- coding: utf-8 -*-
"""v2 개편이 추가하는 테이블. `Store._init()` 이 `apply()` 를 부른다.

묶음마다 구역을 나눴다. **자기 구역 안에만** 추가한다.

  DDL_<묶음>        = "CREATE TABLE IF NOT EXISTS ... ;"   (여러 문장 가능)
  MIGRATIONS_<묶음> = (("테이블", "컬럼", "타입"), ...)      기존 테이블 컬럼 추가

`apply()` 는 이름이 DDL_ / MIGRATIONS_ 로 시작하는 모듈 전역을 이름순으로
모아 실행한다. 등록 목록을 따로 두지 않는 이유는 그 목록 한 줄이 묶음마다
같은 자리를 고쳐 병합 충돌이 나기 때문이다.

기존 테이블(recos, scans 등)의 **기존 컬럼은 바꾸지 않는다.** 컬럼 추가만
한다. recos 기록은 지금과 동일해야 한다.
"""
from __future__ import annotations

import sqlite3

__all__ = ["apply"]


# ── [A] 측정 바로잡기 ──
# (A 구역 시작)
# (A 구역 끝)


# ── [B] 추천 필터 ──
# (B 구역 시작)
# reco_events     : 재추천 제한용 이벤트([0] assign_events 결과의 이벤트 행).
#                   (ticker, event_date) 가 키라 재실행·--force 재스캔에도
#                   행이 늘지 않는다. send_filter.record() 가 그 날짜까지의
#                   이벤트를 지우고 다시 쓴다(결정적 재계산 = 멱등).
# reco_filter_log : 그날 발송·추적 대상에서 빠진 recos 행과 사유.
#                   (d, ticker) 키. 그날 행을 지우고 다시 쓴다.
DDL_B = """
CREATE TABLE IF NOT EXISTS reco_events (
  ticker TEXT NOT NULL,
  event_date TEXT NOT NULL,       -- 이벤트 날짜(첫 추천일 = 1일째)
  window_end TEXT,                -- 제한 종료일(N일째). 달력 밖이면 예상 거래일
  window_days INTEGER NOT NULL,   -- N (RECO_COOLDOWN_DAYS)
  name TEXT,
  slot TEXT,                      -- 이벤트 시작 행의 슬롯·등급·점수
  grade TEXT,
  score REAL,                     -- VALUE=매집, TREND=추세, 그 외=max(매집,추세)
  last_d TEXT,                    -- 기간 중 마지막 추천일
  last_slot TEXT,
  last_grade TEXT,
  days_listed INTEGER,            -- 기간 중 추천 목록에 오른 거래일 수
  changes TEXT,                   -- 기간 중 슬롯·등급 변화 기록 (없으면 NULL)
  PRIMARY KEY (ticker, event_date)
);
CREATE INDEX IF NOT EXISTS ix_reco_events_end ON reco_events(window_end);

CREATE TABLE IF NOT EXISTS reco_filter_log (
  d TEXT NOT NULL,
  ticker TEXT NOT NULL,
  name TEXT,
  rank INTEGER,
  slot TEXT,
  grade TEXT,
  reason TEXT NOT NULL,           -- SEQ / 보충 / 국면 / 재추천 제한
  regime TEXT,                    -- 그날 국면 라벨 (없으면 NULL)
  event_date TEXT,                -- 재추천 제한일 때 소속 이벤트 날짜
  day_in_window INTEGER,
  PRIMARY KEY (d, ticker)
);
CREATE INDEX IF NOT EXISTS ix_reco_filter_log_reason ON reco_filter_log(reason);
"""
# (B 구역 끝)


# ── [C] 국면 라벨 ──
# (C 구역 시작)
# market_regime : 거래일별 국면. 정의·전환 규칙은 stocknews/regime.py.
#   label      확정 국면(상승/중립/하락). NULL = 판정 불가(null_reason) 또는
#              확정 전(첫 5거래일 연속 충족 전).
#   raw_label  그날 조건만으로 본 국면(5일 확인 전). NULL = 판정 불가.
#   pending_*  전환 대기 중인 새 조건과 연속 일수.
#   fresh      지수 마지막 날짜 = 기준일. 0 이면 지수 칸·label 은 NULL.
#   kind       '소급'(regime-backfill) / '실시간'(daily 단계).
# reco_regime : 추천 시점 국면 스냅샷. recos 테이블은 컬럼 하나도 건드리지
#   않는다(기존 smoke 'recos 스키마 불변' 이 정확한 컬럼 목록을 강제한다).
#   (d, rank) 로 recos 와 1:1 조인.
DDL_C = """
CREATE TABLE IF NOT EXISTS market_regime (
  d               TEXT PRIMARY KEY,
  label           TEXT,
  raw_label       TEXT,
  pending_label   TEXT,
  pending_days    INTEGER,
  kospi_close     REAL,
  kospi_ma200     REAL,
  ma200_gap_pct   REAL,
  kospi_ret_20d   REAL,
  above_ma60_pct  REAL,
  n_ma60_universe INTEGER,
  fresh           INTEGER NOT NULL DEFAULT 0,
  null_reason     TEXT,
  kind            TEXT,
  calc_version    INTEGER,
  created         TEXT
);
CREATE TABLE IF NOT EXISTS reco_regime (
  d       TEXT NOT NULL,
  rank    INTEGER NOT NULL,
  ticker  TEXT,
  regime  TEXT,
  kind    TEXT,
  created TEXT,
  PRIMARY KEY (d, rank)
);
"""
MIGRATIONS_C = ()
# (C 구역 끝)


# ── [D·F] 추천 추적 · 평일 종목 발송 개편 ──
# (DF 구역 시작)
# 전부 **추가 전용**이다. UPDATE·DELETE 는 트리거가 막는다(정정은 정정 행
# 추가 — paper_positions.corrects_id / paper_results.corrects_id). 코드에도
# 수정 경로가 없다(smoke s40 이 PaperBook 공개 메서드 이름을 검사한다).
#
#   paper_sends          실제 발송 기록 (d, ticker, kind). 편입의 근거
#   paper_positions      편입(또는 미편입 + 사유) 1행 = 발송된 이벤트 1건
#   paper_results        보유기간 h 확정 성적 (5/10/20/40/60, 20 = 최종 표시)
#   paper_random_bundles 신호일별 무작위 100묶음 구성 (고정 시드)
#   paper_random_results 묶음별 h 성적 + 추천 묶음의 백분위(bundle = -1)
#   paper_shadow         그림자 청산 (현행 엔진 / 대안 세트 / 고정 20일)
DDL_DF = """
CREATE TABLE IF NOT EXISTS paper_sends (
  d TEXT NOT NULL, ticker TEXT NOT NULL, kind TEXT NOT NULL,
  sent_at TEXT, source TEXT,
  PRIMARY KEY (d, ticker, kind)
);
CREATE TABLE IF NOT EXISTS paper_positions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id TEXT NOT NULL, ticker TEXT NOT NULL, name TEXT,
  signal_d TEXT NOT NULL, event_date TEXT, day_in_window INTEGER,
  enrolled INTEGER NOT NULL, entry_date TEXT, entry_price REAL,
  entry_status TEXT, skip_reason TEXT,
  market TEXT, slot TEXT, grade TEXT, rank INTEGER,
  value_score REAL, trend_score REAL, regime TEXT, track TEXT,
  stop_price REAL, alt_stop_price REAL, atr14 REAL,
  band_hi REAL, band_mid REAL, band_lo REAL, fib_0382 REAL, cross_low REAL,
  backfill INTEGER DEFAULT 0, corrects_id INTEGER, note TEXT, created TEXT
);
CREATE INDEX IF NOT EXISTS ix_paper_pos_event ON paper_positions(event_id);
CREATE TABLE IF NOT EXISTS paper_results (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  position_id INTEGER NOT NULL, horizon INTEGER NOT NULL,
  exit_date TEXT, exit_price REAL, status TEXT,
  ret REAL, net REAL, idx REAL, ex_idx REAL, med REAL, ex_med REAL,
  mdd REAL, corrects_id INTEGER, created TEXT
);
CREATE INDEX IF NOT EXISTS ix_paper_res_pos ON paper_results(position_id, horizon);
CREATE TABLE IF NOT EXISTS paper_random_bundles (
  signal_d TEXT NOT NULL, bundle INTEGER NOT NULL, k INTEGER, seed TEXT,
  universe_n INTEGER, tickers TEXT, created TEXT,
  PRIMARY KEY (signal_d, bundle)
);
CREATE TABLE IF NOT EXISTS paper_random_results (
  signal_d TEXT NOT NULL, horizon INTEGER NOT NULL, bundle INTEGER NOT NULL,
  mean_net REAL, n_ok INTEGER, pct REAL, rank INTEGER, created TEXT,
  PRIMARY KEY (signal_d, horizon, bundle)
);
CREATE TABLE IF NOT EXISTS paper_shadow (
  position_id INTEGER NOT NULL, rule_set TEXT NOT NULL, seq INTEGER NOT NULL,
  rule TEXT, layer INTEGER, ratio REAL,
  signal_date TEXT, exec_date TEXT, exec_price REAL,
  plan_price REAL, gap_pct REAL, net REAL, hold_net REAL,
  regime TEXT, created TEXT,
  PRIMARY KEY (position_id, rule_set, seq)
);
CREATE TRIGGER IF NOT EXISTS paper_sends_no_upd BEFORE UPDATE ON paper_sends
  BEGIN SELECT RAISE(ABORT, 'paper_sends is append-only'); END;
CREATE TRIGGER IF NOT EXISTS paper_sends_no_del BEFORE DELETE ON paper_sends
  BEGIN SELECT RAISE(ABORT, 'paper_sends is append-only'); END;
CREATE TRIGGER IF NOT EXISTS paper_positions_no_upd BEFORE UPDATE ON paper_positions
  BEGIN SELECT RAISE(ABORT, 'paper_positions is append-only'); END;
CREATE TRIGGER IF NOT EXISTS paper_positions_no_del BEFORE DELETE ON paper_positions
  BEGIN SELECT RAISE(ABORT, 'paper_positions is append-only'); END;
CREATE TRIGGER IF NOT EXISTS paper_results_no_upd BEFORE UPDATE ON paper_results
  BEGIN SELECT RAISE(ABORT, 'paper_results is append-only'); END;
CREATE TRIGGER IF NOT EXISTS paper_results_no_del BEFORE DELETE ON paper_results
  BEGIN SELECT RAISE(ABORT, 'paper_results is append-only'); END;
CREATE TRIGGER IF NOT EXISTS paper_rb_no_upd BEFORE UPDATE ON paper_random_bundles
  BEGIN SELECT RAISE(ABORT, 'paper_random_bundles is append-only'); END;
CREATE TRIGGER IF NOT EXISTS paper_rb_no_del BEFORE DELETE ON paper_random_bundles
  BEGIN SELECT RAISE(ABORT, 'paper_random_bundles is append-only'); END;
CREATE TRIGGER IF NOT EXISTS paper_rr_no_upd BEFORE UPDATE ON paper_random_results
  BEGIN SELECT RAISE(ABORT, 'paper_random_results is append-only'); END;
CREATE TRIGGER IF NOT EXISTS paper_rr_no_del BEFORE DELETE ON paper_random_results
  BEGIN SELECT RAISE(ABORT, 'paper_random_results is append-only'); END;
CREATE TRIGGER IF NOT EXISTS paper_shadow_no_upd BEFORE UPDATE ON paper_shadow
  BEGIN SELECT RAISE(ABORT, 'paper_shadow is append-only'); END;
CREATE TRIGGER IF NOT EXISTS paper_shadow_no_del BEFORE DELETE ON paper_shadow
  BEGIN SELECT RAISE(ABORT, 'paper_shadow is append-only'); END;
"""
# (DF 구역 끝)


# ── [E] 위험 정보 ──
# (E 구역 시작)
# 종목별 EWMA 일간 변동성 σ (stocknews/risk.py). 기록 전용.
#   sigma_ewma    일간 σ, 소수(0.023 = 2.3%). NULL 행은 쓰지 않는다.
#   lam           그 행을 계산한 λ
#   calc_version  config.RISK_CALC_VERSION. 정의가 바뀌면 새 버전으로 쌓인다
#   kind          '실시간' = daily 스캔 단계가 그날 기록 (그날까지 데이터만)
#                 '소급'   = --mode risk-backfill 이 과거 날짜를 채움
#                 같은 정의·같은 입력이라 값은 같다. 실시간 행이 이긴다
#                 (소급은 INSERT OR IGNORE, 실시간은 INSERT OR REPLACE).
# 스키마는 I 묶음(상관 행렬·GARCH 비교)과의 계약이다. 바꾸지 않는다.
DDL_E = """
CREATE TABLE IF NOT EXISTS risk_metrics (
    d            TEXT NOT NULL,
    ticker       TEXT NOT NULL,
    sigma_ewma   REAL,
    lam          REAL,
    calc_version INTEGER NOT NULL,
    kind         TEXT NOT NULL,
    created      TEXT,
    PRIMARY KEY (d, ticker, calc_version)
);
CREATE INDEX IF NOT EXISTS idx_risk_metrics_ticker
    ON risk_metrics(ticker, d);
"""
# (E 구역 끝)


# ── [G] 뉴스 ──
# (G 구역 시작)
# (G 구역 끝)


# ── [H] 지표 기록 ──
# (H 구역 시작)
# 지표 기록. 1행 = (날짜, 종목, 지표, 계산버전). value NULL = 계산했지만 규칙상
# 값 없음(점프·신선도·상장일수). kind = '소급' | '실시간'.
# 이전 calc_version 행은 덮어쓰지 않는다 — 쓰기는 전부 INSERT OR IGNORE.
# WITHOUT ROWID: PK 가 곧 테이블이라 PK 인덱스 사본이 따로 생기지 않는다
# (약 1천만 행 규모. 사본 실측 rowid 표 1,158MB → 782MB). 소급 행은
# created 를 NULL 로 둬 560MB (계산 시각은 runs 테이블에 있다).
DDL_H = """
CREATE TABLE IF NOT EXISTS indicator_log (
  d            TEXT    NOT NULL,
  ticker       TEXT    NOT NULL,
  name         TEXT    NOT NULL,
  value        REAL,
  calc_version INTEGER NOT NULL,
  kind         TEXT    NOT NULL,
  created      TEXT,
  PRIMARY KEY (d, ticker, name, calc_version)
) WITHOUT ROWID;
"""
# (H 구역 끝)


# ── [I] 주간 분석 ──
# (I 구역 시작)
# (I 구역 끝)


# ── [J] 정리 ──
# (J 구역 시작)
# (J 구역 끝)


def apply(con: sqlite3.Connection) -> None:
    """v2 DDL·컬럼 추가를 멱등하게 적용한다."""
    g = globals()
    for name in sorted(k for k in g if k.startswith("DDL_")):
        sql = g[name]
        if sql and str(sql).strip():
            con.executescript(str(sql))
    for name in sorted(k for k in g if k.startswith("MIGRATIONS_")):
        for table, col, coltype in g[name] or ():
            have = {r[1] for r in con.execute(
                f"PRAGMA table_info({table})").fetchall()}
            if have and col not in have:
                con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}")
