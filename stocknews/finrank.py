# -*- coding: utf-8 -*-
"""[S] 재무 순위 줄 (FINANCE_RANK_LINE · 2026-11-20 지시). 표시 전용.

목록은 연구 폴더(research/finrank)에서 만든 "지금 목록"을 stocknews/finance_rank.json 으로
옮겨 둔 것이다. **자동 갱신은 없다** — 월 1회 연구 스크립트를 다시 돌려 파일을 바꾸고
커밋한다. 파일이 없거나 형식이 다르면 구역을 만들지 않는다(호출부가 오류로 적는다).
"""
from __future__ import annotations

import json
from pathlib import Path

from . import config as C

PATH = Path(__file__).with_name("finance_rank.json")
NOTE = "(이익을 잘 내면서 값이 싼 순 · 월 1회 갱신 · 검증 중)"


def load(path: Path | None = None) -> dict | None:
    try:
        j = json.loads((path or PATH).read_text(encoding="utf-8"))
        items = [x for x in j["items"] if x.get("ticker") and x.get("name")]
        items.sort(key=lambda x: int(x["rank"]))
        return {"asof": str(j.get("asof") or ""), "items": items} if items else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


def block(path: Path | None = None) -> dict | None:
    """{"lines": [제목, 종목들, 안내], "tickers"} 또는 None(자료 없음)."""
    from .renderer import stock_label
    data = load(path)
    if not data:
        return None
    top = data["items"][:int(C.FINANCE_RANK_TOP)]
    return {"lines": [f"【재무 순위 상위 {len(top)}】",
                      " · ".join(stock_label(x["name"], str(x["ticker"]).zfill(6)) for x in top),
                      NOTE],
            "tickers": [str(x["ticker"]).zfill(6) for x in top], "asof": data["asof"]}
