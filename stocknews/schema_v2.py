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
