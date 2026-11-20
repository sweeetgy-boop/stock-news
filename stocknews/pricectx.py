# -*- coding: utf-8 -*-
"""[S] 가격 맥락 — 추천 메시지의 종목 줄에 붙는 숫자(종가 · 오늘 · 1·3·5·10일 · 고점 대비).

표시 전용. 시세(prices)를 읽기만 한다. 점수·추천 선정·차트 선정·recos 기록에 닿지 않는다.

  n일 수익률  기준일 종가 ÷ n봉 전 종가 − 1 (그 종목의 자기 봉 · 거래량 0 인 날은 봉이 아님).
              봉이 모자라거나 기준일 봉이 없으면 값이 없다 → "-".
  오늘        1일 수익률과 같은 수(기준일 종가 ÷ 직전 봉 종가 − 1).
  고점        직전 250봉 고가의 최고값(기준일 제외 · signals.high_drop 과 같은 식).
              고점 대비 = 종가 ÷ 고점 − 1 (신고가면 0). 250봉이 안 되면 값이 없다.
"""
from __future__ import annotations

import numpy as np

HORIZONS = (1, 3, 5, 10)
MINUS = "−"


def context(store, d: str, tickers) -> dict[str, dict]:
    """종목별 {"close", "r1","r3","r5","r10", "high", "drop"(0~1), "closes"(d 까지 날짜→종가)}.
    기준일 봉이 없는 종목은 결과에 없다. 못 구한 항목은 키가 없다."""
    from .signals import HIGH_WIN, high_drop, load_bars
    d = str(d)[:10]
    out: dict[str, dict] = {}
    ts = {str(t) for t in tickers or [] if t}
    if not ts:
        return out
    bars, _ = load_bars(store, d, tickers=ts, bars=HIGH_WIN + 15)
    for t, b in bars.items():
        c = b["c"]
        if not len(c) or b["d"][-1] != d:
            continue
        rec: dict = {"close": float(c[-1]),
                     "closes": {str(x): float(v) for x, v in zip(b["d"][-40:], c[-40:])}}
        for n in HORIZONS:
            if len(c) > n and c[-1 - n] > 0:
                rec[f"r{n}"] = float((c[-1] / c[-1 - n] - 1.0) * 100.0)
        mh, drop = high_drop(b["h"], c)
        if np.isfinite(mh[-1]) and np.isfinite(drop[-1]):
            rec["high"] = float(mh[-1])
            rec["drop"] = float(drop[-1])
        out[t] = rec
    return out


def pct(v, unit: str = "%") -> str:
    """+1.2% / −3.4% (빼기 기호는 U+2212). 값이 없으면 "-"."""
    if v is None or not np.isfinite(v):
        return "-"
    return f"{float(v):+.1f}{unit}".replace("-", MINUS)


def ret_line(rec: dict | None) -> str:
    """둘째 줄: " 1일 ±a · 3일 ±b · 5일 ±c · 10일 ±d" (숫자는 수익률 % · 못 구하면 "-")."""
    rec = rec or {}
    return " " + " · ".join(f"{n}일 {pct(rec.get(f'r{n}'), '')}" for n in HORIZONS)


def won(v) -> str:
    if v is None or not np.isfinite(v):
        return "-"
    return f"{int(round(float(v))):,}원"


def high_text(rec: dict | None) -> str:
    """"고점 대비 −y%" (신고가면 −0%). 못 구하면 "고점 대비 -"."""
    rec = rec or {}
    if "drop" not in rec:
        return "고점 대비 -"
    return f"고점 대비 {MINUS}{max(0, int(round(rec['drop'] * 100.0)))}%"


def since_close(rec: dict | None, base_d: str):
    """base_d 종가 대비 기준일 종가(%). base_d 봉이 없으면 None."""
    rec = rec or {}
    base = (rec.get("closes") or {}).get(str(base_d)[:10])
    if not base or base <= 0 or "close" not in rec:
        return None
    return float((rec["close"] / base - 1.0) * 100.0)
