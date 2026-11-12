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
# (C 구역 끝)


# ── [D·F] 추천 추적 · 평일 종목 발송 개편 ──
# (DF 구역 시작)
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
