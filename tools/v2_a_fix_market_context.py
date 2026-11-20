# -*- coding: utf-8 -*-
"""v2-A 4번 — market_context 고정값 행을 실제 지수로 재기록할 수 있는지 대조.

2026-09-17~10-13 의 15행은 KOSPI 6,724.34 가 그대로 반복 기록됐다
(`load_index` 에 기준일 신선도 검사가 없던 시절, FDR 이 갱신되지 않은
지수를 돌려줌). 이 도구는 FDR 의 **지금** 히스토리로 같은 함수
(`daily.market_context`)를 다시 계산해 DB 값과 나란히 보여준다.

**기본은 대조만 한다(쓰기 없음).** `--apply` 를 줘야 DB 에 쓴다 — 사용자
승인 후에만 쓴다. 쓸 때도 `--from`~`--to` 구간의 행만 덮어쓴다.

    python tools/v2_a_fix_market_context.py --db <사본> [--out <md>]
    python tools/v2_a_fix_market_context.py --db <사본> --all        # 전 행 대조
    python tools/v2_a_fix_market_context.py --db <DB> --apply        # 승인 후
"""
from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

FIELDS = ("kospi_close", "kospi_ma200", "kospi_above_ma200", "kospi_ret_5d",
          "kospi_ret_20d", "kosdaq_close", "kosdaq_ret_5d")


def _f(v, nd=2):
    if v is None or (isinstance(v, float) and not math.isfinite(v)):
        return "-"
    if isinstance(v, int):
        return str(v)
    return f"{v:,.{nd}f}"


def _same(a, b, tol=0.005):
    if a is None or b is None:
        return a is None and b is None
    return abs(float(a) - float(b)) <= tol


def build(db: str, d_from: str, d_to: str, all_rows: bool,
          cache_dir: str | None) -> tuple[list[dict], list[str]]:
    from stocknews.daily import market_context
    from stocknews.data import index_is_fresh
    from stocknews.events import load_index_history
    import sqlite3
    from contextlib import closing

    # 대조는 읽기 전용 연결로 한다 (Store 를 열면 스키마 적용이 일어난다)
    uri = Path(db).resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as con:
        con.row_factory = sqlite3.Row
        rows = [dict(r) for r in con.execute(
            "SELECT * FROM market_context ORDER BY scan_date")]
    if not all_rows:
        rows = [r for r in rows if d_from <= r["scan_date"] <= d_to]
    idx = load_index_history(start="2024-09-01", cache_dir=cache_dir)
    ks, kq = idx.get("KOSPI"), idx.get("KOSDAQ")
    out, notes = [], []
    if ks is None:
        notes.append("KOSPI 히스토리를 받지 못했다 — 대조 불가")
        return out, notes
    for r in rows:
        d = r["scan_date"]
        fresh_k = index_is_fresh(ks, d)
        fresh_q = kq is not None and index_is_fresh(kq, d)
        new = market_context(ks if fresh_k else None,
                             kq if fresh_q else None, d) if fresh_k else None
        diff = [f for f in FIELDS
                if new is None or not _same(r.get(f), new.get(f))]
        out.append({"d": d, "db": r, "new": new, "fresh_kospi": fresh_k,
                    "fresh_kosdaq": fresh_q, "diff": diff})
    return out, notes


def render(rows: list[dict], notes: list[str], d_from: str, d_to: str) -> str:
    L = []
    L.append(f"### market_context 대조 — DB 값 vs FDR 재계산 ({d_from}~{d_to})")
    L.append("")
    L.append("재계산 = `daily.market_context(FDR 히스토리, 기준일)` — daily 가 "
             "기록할 때와 같은 함수. 쓰기 없음(대조만).")
    L.append("")
    L.append("| 기준일 | DB KOSPI | 실제 KOSPI | DB MA200 | 실제 MA200 | DB 위/아래 | 실제 | "
             "DB 5일% | 실제 5일% | DB 20일% | 실제 20일% | DB KOSDAQ | 실제 KOSDAQ | "
             "DB KQ 5일% | 실제 KQ 5일% | 다른 칸 |")
    L.append("|" + "---|" * 16)
    n_diff = 0
    for x in rows:
        a, b = x["db"], x["new"] or {}
        if x["diff"]:
            n_diff += 1
        L.append("| " + " | ".join([
            x["d"],
            _f(a.get("kospi_close")), _f(b.get("kospi_close")),
            _f(a.get("kospi_ma200")), _f(b.get("kospi_ma200")),
            _f(a.get("kospi_above_ma200")), _f(b.get("kospi_above_ma200")),
            _f(a.get("kospi_ret_5d")), _f(b.get("kospi_ret_5d")),
            _f(a.get("kospi_ret_20d")), _f(b.get("kospi_ret_20d")),
            _f(a.get("kosdaq_close")), _f(b.get("kosdaq_close")),
            _f(a.get("kosdaq_ret_5d")), _f(b.get("kosdaq_ret_5d")),
            (", ".join(x["diff"]) if x["diff"] else "일치")
            + ("" if x["fresh_kospi"] else " (FDR 에도 그날 값 없음)"),
        ]) + " |")
    L.append("")
    L.append(f"대조 {len(rows)}행 · 값이 다른 행 {n_diff}행 (허용오차 0.005).")
    for n in notes:
        L.append(f"- {n}")
    return "\n".join(L)


def apply_rows(db: str, rows: list[dict]) -> int:
    from stocknews.store import Store
    st = Store(db)
    n = 0
    for x in rows:
        if x["new"] is None or not x["diff"]:
            continue
        st.upsert_market_context(x["new"])
        n += 1
    return n


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", required=True)
    ap.add_argument("--from", dest="d_from", default="2026-09-17")
    ap.add_argument("--to", dest="d_to", default="2026-10-13")
    ap.add_argument("--all", action="store_true", help="구간 무시, 전 행 대조")
    ap.add_argument("--cache-dir", default=None, help="지수 히스토리 CSV 캐시")
    ap.add_argument("--out", default=None, help="대조표 markdown 저장 경로")
    ap.add_argument("--apply", action="store_true",
                    help="값이 다른 행을 재계산 값으로 덮어쓴다 (사용자 승인 후에만)")
    a = ap.parse_args(argv)

    rows, notes = build(a.db, a.d_from, a.d_to, a.all, a.cache_dir)
    rng = ("전체" if a.all else a.d_from), ("" if a.all else a.d_to)
    md = render(rows, notes, *rng)
    if a.out:
        Path(a.out).write_text(md + "\n", encoding="utf-8")
    print(md)
    if a.apply:
        if a.all:
            print("--apply 는 --all 과 같이 쓸 수 없다 (구간을 명시하라)")
            return 64
        n = apply_rows(a.db, rows)
        print(f"\n덮어쓴 행: {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
