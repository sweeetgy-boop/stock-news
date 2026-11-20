# -*- coding: utf-8 -*-
"""부분 적재된 거래일을 지우고 다시 받는다.

언제 쓰나
--------
`Store.partial_dates()` 가 날짜를 뱉을 때. 2026-09 에 update 가 장중에
돌면서 08-28(13종목) / 09-02(3종목) / 09-03(1,546종목) / 09-04(39종목) 이
그렇게 남았다. 가드(complete_dates)가 들어간 뒤로는 update 가 스스로
재요청하지만, 이미 굳어버린 날짜는 한 번 밀어줘야 한다.

왜 --mode update 로 안 하나
--------------------------
전종목 스냅샷 경로가 2026-08 부터 막혀 있다. 실측:

    get_market_ohlcv("20260902", market="ALL")
      -> Error in get_market_ohlcv_by_ticker: Expecting value: line 1 column 1
      -> KeyError: 시가/고가/저가/종가 없음

그래서 fetch_day 는 종목별 폴백으로 내려가는데, 하루당 2,526종목 x 2회
호출이다. 4일이면 20,208회, 스로틀 0.3초로 2.8시간이다.

여기서는 종목당 범위 조회 1회로 4일을 한 번에 받는다. 2,526회로 끝나고
실측 20분이었다.

사용
----
    python tools/repair_partial_days.py 2026-08-28 2026-09-02 2026-09-03 2026-09-04
    python tools/repair_partial_days.py --dry-run 2026-09-02

DB 는 반드시 먼저 백업하십시오. 이 스크립트는 지정한 날짜의 prices 행을
지우고 시작한다.
"""
import argparse
import random
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:                                    # noqa: BLE001
    pass

from stocknews.store import Store                    # noqa: E402
from stocknews.joblock import JobLock                # noqa: E402


def say(msg: str) -> None:
    print(f"{datetime.now():%H:%M:%S} {msg}", flush=True)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("dates", nargs="+", help="복구할 거래일 (YYYY-MM-DD)")
    ap.add_argument("--db", default=str(REPO / "data" / "quant.db"))
    ap.add_argument("--throttle", type=float, default=0.3)
    ap.add_argument("--dry-run", action="store_true",
                    help="삭제/적재 없이 대상만 보고")
    args = ap.parse_args(argv)

    targets = sorted(args.dates)
    # 범위 조회는 대상일 전체를 덮어야 한다. 양 끝을 하루씩 넓혀 잡는다.
    lo = (datetime.strptime(targets[0], "%Y-%m-%d")).strftime("%Y%m%d")
    hi = (datetime.strptime(targets[-1], "%Y-%m-%d")).strftime("%Y%m%d")

    from pykrx import stock

    store = Store(args.db)
    with sqlite3.connect(args.db, timeout=60) as con:
        before = {d: con.execute("SELECT COUNT(*) FROM prices WHERE d=?",
                                 (d,)).fetchone()[0] for d in targets}
    say(f"대상 {targets}")
    say(f"삭제 전 행수 {before}")
    if args.dry_run:
        say("--dry-run: 여기서 멈춘다")
        return 0

    lock = JobLock("quant", mode="update", lock_dir=str(REPO / "data" / "locks"),
                   timeout=7200, wait_seconds=30)
    if not lock.acquire():
        say(f"다른 잡이 실행 중 — 중단: {lock.holder}")
        return 3
    try:
        with sqlite3.connect(args.db, timeout=60) as con:
            con.execute(
                "DELETE FROM prices WHERE d IN (%s)"
                % ",".join("?" * len(targets)), targets)
            con.commit()
        say(f"삭제 완료 ({sum(before.values())}행)")

        tickers = list(store.active_tickers())
        say(f"활성 종목 {len(tickers)}개 · 범위 {lo}~{hi} · 종목당 1회 조회")
        t0 = time.time()
        ok = empty = err = rows = 0
        for i, code in enumerate(tickers, 1):
            try:
                df = stock.get_market_ohlcv(lo, hi, code)
                need = ("시가", "고가", "저가", "종가", "거래량")
                if df is None or df.empty or any(c not in df.columns
                                                 for c in need):
                    empty += 1
                else:
                    df = df[df["거래량"] > 0]
                    df = df[[str(x)[:10] in targets for x in df.index]]
                    if len(df):
                        rows += store.upsert_prices(code, df)
                        ok += 1
                    else:
                        empty += 1
            except Exception as exc:                 # noqa: BLE001
                err += 1
                if err <= 5:
                    say(f"  {code} 실패: {type(exc).__name__}: {str(exc)[:70]}")
            time.sleep(args.throttle + random.random() * 0.1)
            if i % 200 == 0:
                el = time.time() - t0
                eta = (len(tickers) - i) / max(i / el, 1e-9) / 60
                say(f"  {i}/{len(tickers)} · 적재 {rows}행 "
                    f"(성공 {ok} 빈값 {empty} 오류 {err}) · 잔여 약 {eta:.0f}분")

        say(f"재적재 완료: {rows}행 (성공 {ok} / 빈값 {empty} / 오류 {err})")
        with sqlite3.connect(args.db, timeout=60) as con:
            for d in targets:
                n = con.execute("SELECT COUNT(*) FROM prices WHERE d=?",
                                (d,)).fetchone()[0]
                say(f"   {d}  {before[d]:6} -> {n:6}종목")
        left = store.partial_dates(
            since=(datetime.now() - timedelta(days=60)).strftime("%Y-%m-%d"))
        say(f"남은 부분 적재일(최근 60일): {left or '없음'}")
        return 0
    finally:
        lock.release()


if __name__ == "__main__":
    sys.exit(main())
