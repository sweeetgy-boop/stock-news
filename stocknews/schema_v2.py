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
# (B 구역 끝)


# ── [C] 국면 라벨 ──
# (C 구역 시작)
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
# (E 구역 끝)


# ── [G] 뉴스 ──
# (G 구역 시작)
# (G 구역 끝)


# ── [H] 지표 기록 ──
# (H 구역 시작)
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
