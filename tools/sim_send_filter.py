# -*- coding: utf-8 -*-
"""[B] 추천 필터 '켜기 전 판단 자료' 시뮬레이션 (재실행 가능, 읽기 전용).

과거 recos 전체에 필터를 걸었다고 가정하고 아래를 낸다.

  1) 플래그 조합별 하루 새 추천 수 분포 (평균 · 0개인 날 비율 · 최대 …)
  2) 사유별 제외 건수 (행 · [0] 이벤트 단위)
  3) 필터 적용 전후 20일 초과수익 (전종목 중위 대비 ex_med_20 · 지수 대비
     ex_idx_20), 국면별

판정은 운영 코드와 같은 함수(stocknews.send_filter)를 쓴다. 시뮬레이션용
판정을 따로 만들면 '켜기 전에 본 숫자'와 '켠 뒤 일어나는 일'이 달라진다.

③ 국면 TREND 절반은 C 묶음의 국면 기록(v2_hooks.regime_on)이 있어야 걸린다.
국면이 없으면 '국면 없음 → 미적용' 으로 나온다.

**DB 사본에서 돌린다.** Store 를 열면 v2 테이블 DDL(CREATE IF NOT EXISTS)이
적용된다. recos 와 다른 테이블의 행은 읽기만 한다.

  python tools/sim_send_filter.py --db <사본> [--cooldown 20]
         [--index-cache <폴더>] [--no-network] [--json <파일>]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402

from stocknews import config as C  # noqa: E402
from stocknews import events as ev  # noqa: E402
from stocknews import send_filter as sf  # noqa: E402
from stocknews.store import Store  # noqa: E402


def _flags(cd=0, seq=False, fill=False, regime=False) -> dict:
    return {"cooldown_days": cd, "exclude_seq": seq, "exclude_fill": fill,
            "regime_cut": regime}


def combos(cd: int) -> list[tuple[str, dict]]:
    return [
        ("전부 OFF", _flags()),
        ("SEQ 제외만", _flags(seq=True)),
        ("보충 제외만", _flags(fill=True)),
        (f"재추천 제한만({cd})", _flags(cd=cd)),
        ("국면 컷만(③)", _flags(regime=True)),
        ("B 세 필터 전부", _flags(cd, True, True)),
        ("세 필터 + ③", _flags(cd, True, True, True)),
    ]


def day_dist(res: dict) -> dict:
    n = pd.Series([len(r.new) for r in res.values()], dtype="int64")
    hist = n.value_counts().sort_index()
    return {"days": int(len(n)), "mean": float(n.mean()) if len(n) else None,
            "median": float(n.median()) if len(n) else None,
            "zero_pct": float((n == 0).mean() * 100) if len(n) else None,
            "max": int(n.max()) if len(n) else None,
            "min": int(n.min()) if len(n) else None,
            "hist": {int(k): int(v) for k, v in hist.items()},
            "hold_rows": int(sum(len(r.hold) for r in res.values())),
            "excluded_rows": int(sum(len(r.excluded) for r in res.values()))}


def reason_counts(res: dict, eid: dict) -> dict:
    out = {}
    for why in sf.REASONS:
        rows = [(d, r["ticker"]) for d, x in res.items() for r in x.rows
                if r.get("filter_reason") == why]
        out[why] = {"rows": len(rows),
                    "events": len({eid.get(k) for k in rows})}
    return out


def orphan_holds(res: dict) -> int:
    """이벤트 시작 행이 ①~③ 으로 빠졌는데 기간 중 재등장해 ④ 로 간 행 수.

    이벤트 판정 입력이 recos 전체라서 생기는 경우다. 이 행들은 한 번도
    발송되지 않은 종목인데 '추천 유지' 로 보인다.
    """
    start = {}
    for d in sorted(res):
        for r in res[d].rows:
            if r["is_new_event"]:
                start[(r["ticker"], r["event_date"])] = r["status"]
    n = 0
    for x in res.values():
        for r in x.hold:
            if start.get((r["ticker"], r["event_date"])) == sf.EXCLUDED:
                n += 1
    return n


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--cooldown", type=int, default=int(C.EVENT_WINDOW_DAYS))
    ap.add_argument("--horizon", type=int, default=20)
    ap.add_argument("--index-cache", default=str(ROOT / "data" / "index_cache"))
    ap.add_argument("--no-network", action="store_true")
    ap.add_argument("--json", help="결과 JSON 저장 경로")
    a = ap.parse_args(argv)

    t0 = time.time()
    st = Store(a.db)
    recos = sf._load_recos(st)
    if recos.empty:
        print("recos 가 비어 있습니다.")
        return 1
    cal = sf._calendar(st, recos)
    ev0 = ev.assign_events(recos, cal, window=int(C.EVENT_WINDOW_DAYS))
    eid = {(r.d, r.ticker): r.event_id for r in ev0.itertuples()}
    regimes = {d: sf._regime(st, d) for d in sorted(set(recos["d"]))}
    n_reg = sum(1 for v in regimes.values() if v)

    out = {"db": a.db, "recos_rows": int(len(recos)),
           "recos_days": int(recos["d"].nunique()),
           "first": str(recos["d"].min()), "last": str(recos["d"].max()),
           "events_0": int(ev0["is_event"].sum()),
           "regime_days": n_reg, "combos": [], "tables": {}}

    print(f"recos {out['recos_rows']}행 / {out['recos_days']}일 "
          f"({out['first']} ~ {out['last']}) · [0] 이벤트(N="
          f"{C.EVENT_WINDOW_DAYS}) {out['events_0']}건")
    print(f"국면 기록이 있는 추천일: {n_reg}/{out['recos_days']}"
          + ("  → ③ 국면 컷은 국면 없음으로 미적용" if not n_reg else ""))
    print()
    print("■ 1) 하루 새 추천 수 분포 (발송 대상 'new' 기준)")
    hdr = f"{'조합':<18} {'평균':>5} {'중앙':>5} {'0개 날':>8} {'최대':>4} {'최소':>4}  분포(개수:일수)"
    print(hdr)
    for name, fl in combos(a.cooldown):
        res = sf.evaluate_days(st, None, fl, calendar=cal, recos=recos)
        dd = day_dist(res)
        rc = reason_counts(res, eid)
        orph = orphan_holds(res) if fl["cooldown_days"] else 0
        out["combos"].append({"name": name, "flags": fl, "dist": dd,
                              "reasons": rc, "orphan_holds": orph})
        hist = " ".join(f"{k}:{v}" for k, v in dd["hist"].items())
        print(f"{name:<18} {dd['mean']:>5.2f} {dd['median']:>5.1f} "
              f"{dd['zero_pct']:>7.1f}% {dd['max']:>4} {dd['min']:>4}  {hist}")
    print()
    print("■ 2) 사유별 제외 건수 (행 / [0] 이벤트)")
    for c in out["combos"]:
        if c["name"] == "전부 OFF":
            continue
        rc = c["reasons"]
        bits = " · ".join(f"{k} {v['rows']}행/{v['events']}건"
                          for k, v in rc.items() if v["rows"])
        extra = (f" · 유지(hold) {c['dist']['hold_rows']}행"
                 if c["flags"]["cooldown_days"] else "")
        orph = (f" · 그중 이벤트 시작이 ①~③으로 빠진 유지 {c['orphan_holds']}행"
                if c["orphan_holds"] else "")
        print(f"• {c['name']}: {bits or '제외 없음'}{extra}{orph}")
    print()

    print(f"■ 3) 필터 적용 전후 {a.horizon}일 초과수익 "
          "(채점 단위 = [0] 이벤트 · 익일 시가 진입 · 왕복 0.5%p 차감)")
    index = ev.load_index_history(start=cal[0], cache_dir=a.index_cache,
                                  allow_network=not a.no_network)
    print(f"  지수 히스토리: {', '.join(f'{k} {len(v)}일' for k, v in index.items()) or '없음'}")
    for name in ("B 세 필터 전부", "세 필터 + ③"):
        fl = next(c["flags"] for c in out["combos"] if c["name"] == name)
        tab = sf.score_table(st, horizon=a.horizon, flags=fl, index=index)
        out["tables"][name] = tab
        print(f"\n  [{name}] 채점 단위 {tab['units']}건")
        print(f"  {'그룹':<20} {'국면':<8} {'단위':>4} {'미도래':>5}  "
              f"중위 대비 ex_med_{a.horizon} | 지수 대비 ex_idx_{a.horizon}")
        for r in tab["rows"]:
            print(f"  {r['group']:<20} {r['regime']:<8} {r['units']:>4} "
                  f"{r['pending']:>5}  {ev.fmt_cell(r['ex_med'])} | "
                  f"{ev.fmt_cell(r['ex_idx'])}")
    out["elapsed_sec"] = round(time.time() - t0, 1)
    print(f"\n소요 {out['elapsed_sec']}초")
    if a.json:
        Path(a.json).write_text(json.dumps(out, ensure_ascii=False, indent=1,
                                           default=str), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
