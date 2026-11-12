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
