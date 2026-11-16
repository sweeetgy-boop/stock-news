# -*- coding: utf-8 -*-
"""[D·F] 평일 저녁 종목 부분 · 일요일 주간 성적표 · 주간 그림자 청산 표.

평일 저녁 브리핑의 종목 부분 (PAPER_TRACKING 또는 DAILY_LIST_V2 가 켜졌을 때)
  ① 오늘 새 추천 — 종목마다 선정 이유(점수 기여 상위 3개, 계산값 그대로)
                    + 위험 1줄. 슬롯 제목 옆에 그 유형 실전 성적.
  ② 오늘 만기     — 20거래일 확정 수익률, 지수 대비 (PAPER_TRACKING)
  ③ 추적 중 요약  — 종목 수 · 동일 비중 평균 · 지수 대비 · 무작위 백분위 ·
                    상위 3 · 하위 3 (PAPER_TRACKING)
     PAPER_TRACKING 꺼짐 → ②③ 대신 유지 종목 "추천 유지 n일째" 한 줄씩
  ④ B 의 "오늘 제외" 한 줄 (훅이 None 이면 생략)

문장 생성·해석·전망을 넣지 않는다. 숫자와 조건 이름만 적는다.
"""
from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
import pandas as pd

from . import config as C
from . import events as E
from . import paper as P
from .renderer import SLOT_LABEL, _e, stock_label

log = logging.getLogger(__name__)

__all__ = ["weekday_enabled", "contributions", "reasons_for", "slot_perf",
           "weekday_block", "sunday_scorecard", "curve_chart",
           "weekly_shadow_block", "sunday_detail", "TEXT_LIMIT", "SEND_LIMIT"]

TEXT_LIMIT = 4096
# 실제로 한 메시지에 담는 상한. notify._split 이 이 길이를 넘으면 줄 경계에서
# 임의로 자른다(TELEGRAM_MAX - 128). 일요일 성적표는 그 전에 ③을 따로 떼어
# 낸다 — 그래야 ③이 아닌 엉뚱한 곳에서 잘리지 않는다.
SEND_LIMIT = TEXT_LIMIT - 128
DISCLAIMER = "※ 검증 중 · 투자 판단 근거 아님"
FOOTER = ("※ 추적 = 종이 편입(실제 보유 아님) · 다음 거래일 시가 · 왕복 "
          f"{C.ROUND_TRIP_COST_PCT:g}%p 차감 · 검증 중 · 투자 판단 근거 아님")
FOOTER_LITE = "※ 검증 중 · 투자 판단 근거 아님"

# 점수 구성요소 이름 (screener.screen_one 의 가중치 그대로)
LPS_W, FIB_W, CONF_BONUS = 0.55, 0.45, 0.5
_LPS = {"credit_heat": "신용잔고율", "band": "청산밴드 진입도",
        "panic_volume": "투매 거래량", "panic_gap": "투매 갭",
        "margin_due": "반대매매 예정일", "short_cover": "숏커버 전환"}
_FIB = {"depth": "피보 되돌림 깊이", "touch": "피보 레벨 터치",
        "high_fresh": "고점 신선도", "rebound": "반등 조짐"}
_TRD = {"cross_pair": "교차 쌍", "freshness": "크로스 신선도",
        "convergence": "이평 밀집", "slope_long": "장기선 기울기",
        "volume": "거래량 확증", "whipsaw_penalty": "휩쏘 감점",
        "below_ma_long": "장기선 아래 감점"}


def weekday_enabled() -> bool:
    return bool(getattr(C, "PAPER_TRACKING", False)
                or getattr(C, "DAILY_LIST_V2", False))


def _pct(v, unit: str = "%") -> str:
    if v is None or not np.isfinite(v):
        return "-"
    return f"{v:+.2f}{unit}"


def _slot_label(slot) -> str:
    s = str(slot or "")
    return SLOT_LABEL.get(s, s or "-")


# ══════════════════════════ ① 선정 이유 ══════════════════════════
def _track_of(slot: str, value: float, trend: float) -> str:
    if slot == "TREND":
        return "TREND"
    if slot == "VALUE":
        return "VALUE"
    return "TREND" if trend > value else "VALUE"


def contributions(res, track: str) -> tuple[list[tuple[str, float, str]], float]:
    """ScreenResult → [(조건, 기여 점수, 계산식)], 합계.

    매집 = 0.55×LPS 구성 + 0.45×피보 구성 + 겹침 보너스(상한 10)
    추세 = 추세 구성 합(감점 포함, 0~10 으로 자름)
    """
    out: list[tuple[str, float, str]] = []
    if track == "TREND":
        bd = dict(res.trend.breakdown) if res.trend else {}
        for k, lab in _TRD.items():
            v = float(bd.get(k, 0.0) or 0.0)
            if v:
                out.append((lab, v, f"{v:+.2f}"))
        total = float(res.trend_score)
        return out, total
    lb = dict(res.liq.breakdown) if res.liq else {}
    fb = dict(res.fib.breakdown) if res.fib else {}
    raw = 0.0
    for k, lab in _LPS.items():
        v = float(lb.get(k, 0.0) or 0.0)
        if v:
            out.append((lab, LPS_W * v, f"{v:g}×{LPS_W}"))
    for k, lab in _FIB.items():
        v = float(fb.get(k, 0.0) or 0.0)
        if v:
            out.append((lab, FIB_W * v, f"{v:g}×{FIB_W}"))
    lps = float(res.liq.score) if res.liq else 0.0
    fs = float(res.fib.score) if res.fib else 0.0
    raw = LPS_W * lps + FIB_W * fs
    if res.confluence:
        bonus = min(10.0, raw + CONF_BONUS) - raw
        if bonus:
            out.append(("피보-청산선 겹침", bonus, f"+{bonus:.2f}"))
    return out, float(res.value_score)


def _coarse(sc: dict, track: str) -> list[tuple[str, float, str]]:
    """재계산이 기록과 다를 때의 대체 — scans 에 기록된 구성 점수 그대로."""
    if track == "TREND":
        v = float(sc.get("trend_raw") or sc.get("trend_score") or 0.0)
        return [("추세 점수(기록)", v, f"{v:.2f}")]
    out = []
    lps = float(sc.get("lps") or 0.0)
    fib = float(sc.get("fib_score") or 0.0)
    if lps:
        out.append(("청산압력 LPS(기록)", LPS_W * lps, f"{lps:g}×{LPS_W}"))
    if fib:
        out.append(("피보 점수(기록)", FIB_W * fib, f"{fib:g}×{FIB_W}"))
    if int(sc.get("confluence") or 0):
        out.append(("피보-청산선 겹침", CONF_BONUS, f"+{CONF_BONUS}"))
    return out


def reasons_for(store, d: str, rows: list[dict]) -> dict[str, dict]:
    """{ticker: {"items": [(조건, 점수, 식)] 상위 3, "track", "source", "total"}}.

    스캔과 같은 입력(daily.screen_ticker, 기준일까지의 봉)으로 다시 계산해
    구성요소를 꺼낸다. 다시 계산한 점수가 recos 기록과 0.01 넘게 다르면
    재계산을 쓰지 않고 scans 에 기록된 구성 점수로 대신한다(source=scans).
    """
    from .daily import scan_context, screen_ticker
    out: dict[str, dict] = {}
    if not rows:
        return out
    import sqlite3
    from contextlib import closing
    with closing(sqlite3.connect(store.path, timeout=30.0)) as con:
        sc = pd.read_sql_query("SELECT * FROM scans WHERE d=?", con, params=(d,))
    smap = {str(r["ticker"]): r for r in sc.to_dict("records")}
    try:
        ctx = scan_context(store, quiet=True)
    except Exception as exc:  # noqa: BLE001
        log.warning("선정 이유 재계산 준비 실패: %s", exc)
        ctx = None
    for r in rows:
        t = str(r["ticker"])
        vs, ts = float(r.get("value_score") or 0), float(r.get("trend_score") or 0)
        track = _track_of(str(r.get("slot") or ""), vs, ts)
        items, src, total = None, "scans", None
        if ctx is not None:
            try:
                res, _o = screen_ticker(store, t, str(r.get("name") or t), ctx,
                                        until=d, apply_exclusion=False)
                if (res is not None and abs(res.value_score - vs) <= 0.01
                        and abs(res.trend_score - ts) <= 0.01):
                    items, total = contributions(res, track)
                    src = "calc"
            except Exception as exc:  # noqa: BLE001 - 표시 부가 정보
                log.debug("선정 이유 재계산 실패 %s: %s", t, exc)
        if items is None:
            items = _coarse(smap.get(t, {}), track)
            total = ts if track == "TREND" else vs
        pos = sorted([x for x in items if x[1] > 0], key=lambda x: -x[1])[:3]
        out[t] = {"items": pos, "all": items, "track": track, "source": src,
                  "total": total}
    return out


def _reason_line(info: dict | None) -> str:
    if not info or not info.get("items"):
        return "   이유: -"
    tr = "추세" if info["track"] == "TREND" else "매집"
    bits = [f"{_e(lab)} +{v:.2f}" for lab, v, _f in info["items"]]
    return f"   이유({tr} {info['total']:.2f}): " + " · ".join(bits)


def _risk_line(store, d: str, r: dict) -> str:
    from . import v2_hooks
    price = float(r.get("price") or 0)
    pct = float(C.STOP_LOSS_PCT)
    if price <= 0:
        return "   위험: -"
    stop = price * (1 - pct / 100.0)
    line = (f"   위험: 손절선 편입가 −{pct:.0f}% (기준일 종가 {price:,.0f}원 → "
            f"{stop:,.0f}원)")
    if getattr(C, "RISK_INFO", False):
        try:
            p = v2_hooks.stop_hit_prob(store, d, str(r["ticker"]), price, stop,
                                       int(C.PAPER_DISPLAY_DAYS))
        except Exception:  # noqa: BLE001
            p = None
        if p is not None and np.isfinite(p):
            line += f" · {int(C.PAPER_DISPLAY_DAYS)}일 내 도달 확률 {p * 100:.0f}%"
    return line


# ══════════════════════════ 슬롯 성적 ══════════════════════════
def _cell_text(sub: pd.DataFrame) -> str:
    c20 = E.stat_cell(sub["ex_idx_20"]) if len(sub) else E.stat_cell([])
    if not c20.get("enough"):
        return f"표본 부족 (n={c20.get('n', 0)})"
    c10 = E.stat_cell(sub["ex_idx_10"])
    w = E.stat_cell(sub["net_20"])
    return (f"10일 초과 {_pct(c10['mean'], '%p')} · 20일 초과 "
            f"{_pct(c20['mean'], '%p')} · 승률 {w['win']:.0f}% (n={c20['n']})")


def slot_perf(pd_: "P.PaperData", d: str) -> dict[str, str]:
    """{슬롯: "현재 국면 … | 전체 …"} — 과거 이벤트(event_slot 기준)를 [0] 채점."""
    from . import v2_hooks
    ev = E.events_only(pd_.assigned)
    if ev is None or ev.empty:
        return {}
    ev = ev[ev["event_date"] < P._ds(d)]
    if ev.empty:
        return {}
    sc = pd_.score(ev, (10, 20))
    try:
        cur = v2_hooks.regime_on(pd_.store, d)
    except Exception:  # noqa: BLE001
        cur = None
    if cur is not None:
        regs = []
        for x in sc["event_date"]:
            try:
                regs.append(v2_hooks.regime_on(pd_.store, x))
            except Exception:  # noqa: BLE001
                regs.append(None)
        sc["regime"] = regs
    out = {}
    sc["slot_key"] = sc["event_slot"].astype(str).str.rstrip("*")
    for slot, g in sc.groupby("slot_key"):
        whole = f"전체 {_cell_text(g)}"
        if cur is None:
            out[slot] = f"국면 없음 | {whole}"
        else:
            out[slot] = (f"현재 국면({_e(cur)}) {_cell_text(g[g['regime'] == cur])}"
                         f" | {whole}")
    return out


# ══════════════════════════ 평일 블록 ══════════════════════════
def _new_section(store, d: str, rows: list[dict], perf: dict, paper_on: bool,
                 stale: str | None) -> list[str]:
    out = [f"━━ ① 오늘 새 추천 ({len(rows)}) · 기준일 {d} ━━"]
    if stale:
        out.append(f"• 오늘 새 추천 없음 ({_e(stale)})")
        return out
    if not rows:
        out.append("• 오늘 새 추천 없음")
        return out
    reasons = reasons_for(store, d, rows)
    order = []
    for r in rows:
        k = str(r.get("slot") or "").rstrip("*")
        if k not in order:
            order.append(k)
    i = 0
    for k in order:
        out.append(f"▸ <b>{_e(_slot_label(k))}</b> · {perf.get(k, '실전 성적 없음')}")
        for r in [x for x in rows if str(x.get("slot") or "").rstrip("*") == k]:
            t = str(r["ticker"])
            i += 1
            out.append(f"{i}. {stock_label(r.get('name'), t, bold=True)}"
                       f" [{_e(r.get('grade'))}·{_e(_slot_label(r.get('slot')))}]"
                       f" 매집 {float(r.get('value_score') or 0):.1f} / 추세 "
                       f"{float(r.get('trend_score') or 0):.1f}")
            out.append(_reason_line(reasons.get(t)))
            out.append(_risk_line(store, d, r))
            if paper_on:
                out.append("   ↳ 내일 시가부터 추적 시작")
    return out


def _keep_section(rows: list[dict]) -> list[str]:
    keep = [r for r in rows if not r.get("is_new_event")]
    if not keep:
        return []
    out = [f"━━ 추천 유지 ({len(keep)}) ━━"]
    for r in keep:
        out.append(f"• {stock_label(r.get('name'), r['ticker'])} — "
                   f"추천 유지 {int(r.get('day_in_window') or 1)}일째")
    return out


def _matured_section(pd_, d: str) -> list[str]:
    m = P.matured_on(pd_, d, start=d)
    out = [f"━━ ② 오늘 만기 ({len(m)}) · {int(C.PAPER_DISPLAY_DAYS)}거래일 확정 ━━"]
    if m.empty:
        out.append("• 오늘 만기 없음")
        return out
    for r in m.sort_values("net", ascending=False).to_dict("records"):
        tag = " [소급]" if int(r.get("backfill") or 0) else ""
        st = "" if r["status"] == E.ST_OK else f" ({_e(r['status'])})"
        out.append(f"• {stock_label(r.get('name'), r['ticker'], bold=True)} "
                   f"{_pct(P._nan(r['net']))} · 지수 대비 "
                   f"{_pct(P._nan(r['ex_idx']), '%p')}{st}{tag}")
    return out


def _move_x(store, r) -> str:
    """[E] RISK_INFO ON 일 때 " · 평소 출렁임의 n배". σ 는 신호일(편입 기준일) 값.

    n = ln(1+R) / (σ·√h) — `risk.move_multiple` 정의 그대로(R = 비용 차감 전
    보유 수익률, h = 보유 거래일). σ 없음·플래그 OFF 면 빈 문자열.
    """
    if not getattr(C, "RISK_INFO", False):
        return ""
    try:
        from . import v2_hooks
        from .risk import move_multiple
        sig = v2_hooks.sigma_on(store, r.get("signal_d"), str(r["ticker"]))
        ret = P._nan(r.get("ret"))
        if ret is None:
            ret = P._nan(r.get("net"))
        if sig is None or ret is None:
            return ""
        n = move_multiple(float(ret) / 100.0, sig, max(1, int(r.get("day_n") or 1)))
        return "" if n is None else f" · 평소 출렁임의 {abs(n):.1f}배"
    except Exception:  # noqa: BLE001 - 부가 표기
        return ""


def _tracking_section(pd_, d: str, topn: int) -> tuple[list[str], dict]:
    a = P.active_marks(pd_, d)
    info: dict = {"n": 0}
    head = "━━ ③ 추적 중 ━━"
    if a is None or a.empty:
        return [head, "• 추적 중인 종목 없음"], info
    v = a["net"].astype(float)
    pct, rank = P.pooled_percentile(a, a.attrs.get("random") or {})
    info = {"n": int(len(a)), "mean": P._nan(v.mean()),
            "ex_idx": P._nan(a["ex_idx"].astype(float).mean()),
            "pct": pct, "rank": rank}
    out = [f"━━ ③ 추적 중 {len(a)}종목 (1~{int(C.PAPER_DISPLAY_DAYS) - 1}일째) ━━",
           f"• 동일 비중 평균 {_pct(info['mean'])} · 지수 대비 "
           f"{_pct(info['ex_idx'], '%p')} · 무작위 대비 백분위 "
           f"{'-' if pct is None else f'{pct:.0f}'}"]
    s = a.dropna(subset=["net"]).sort_values("net", ascending=False)

    def _ln(r):
        tag = " [소급]" if int(r.get("backfill") or 0) else ""
        return (f"  {stock_label(r.get('name'), r['ticker'])} {_pct(P._nan(r['net']))}"
                f"{_move_x(pd_.store, r)} ({int(r['day_n'])}일째){tag}")
    if topn > 0 and len(s):
        top = s.head(topn)
        bot = s.tail(min(topn, max(0, len(s) - len(top))))
        out.append("▲ 상위")
        out += [_ln(r) for r in top.to_dict("records")]
        if len(bot):
            out.append("▼ 하위")
            out += [_ln(r) for r in bot.iloc[::-1].to_dict("records")]
    return out, info


def weekday_block(store, d: str, *, paper_on: bool | None = None,
                  pdata=None, index=None, limit_room: int = TEXT_LIMIT
                  ) -> tuple[str, dict]:
    """평일 저녁 종목 부분 텍스트와 부가 정보.

    info: new_rows(① 대상 dict), new_tickers, tracking, stale
    limit_room: 브리핑 나머지(뉴스)와 합쳐 4,096자를 넘으면 ③을 먼저 줄인다.
    """
    from . import v2_hooks
    paper_on = bool(getattr(C, "PAPER_TRACKING", False)) if paper_on is None \
        else paper_on
    pd_ = pdata or P.PaperData(store, d, index=index)
    import sqlite3
    from contextlib import closing
    with closing(sqlite3.connect(store.path, timeout=30.0)) as con:
        last = con.execute("SELECT MAX(d) FROM recos WHERE d<=?", (d,)).fetchone()[0]
    stale = None
    rows: list[dict] = []
    if last != d:
        stale = f"기준일 {d} 추천 기록 없음 · 마지막 기록 {last or '없음'}"
    else:
        rows = P.targets_for(store, d, cal=pd_.cal, assigned=pd_.assigned)
    new = [r for r in rows if r.get("is_new_event")]
    perf = slot_perf(pd_, d)
    sec1 = _new_section(store, d, new, perf, paper_on, stale)
    try:
        ex = v2_hooks.exclusion_line(store, d)
    except Exception:  # noqa: BLE001
        ex = None
    sec4 = [f"━━ ④ 오늘 제외 ━━", f"• {_e(ex)}"] if ex else []
    info: dict = {"new_rows": new, "new_tickers": [str(r["ticker"]) for r in new],
                  "stale": stale, "rows": len(rows)}
    if not paper_on:
        parts = [sec1, _keep_section(rows), sec4]
        text = "\n\n".join("\n".join(p) for p in parts if p)
        return text + "\n" + FOOTER_LITE, info
    sec2 = _matured_section(pd_, d)
    topn = int(C.PAPER_SUMMARY_TOPN)
    floor = max(0, min(topn, int(getattr(C, "PAPER_SUMMARY_MIN_TOPN", 0))))
    while True:
        sec3, tinfo = _tracking_section(pd_, d, topn)
        text = "\n\n".join("\n".join(p) for p in (sec1, sec2, sec3, sec4) if p) \
            + "\n" + FOOTER
        if len(text) <= limit_room or topn <= floor:
            break
        topn -= 1                       # ③을 먼저 줄인다 (상위·하위 줄 수)
    info["tracking"] = tinfo
    info["topn"] = topn
    return text, info


# ══════════════════════════ 일요일 성적표 ══════════════════════════
def _week_bounds(trade_d: str, cal: list[str]) -> tuple[str, str]:
    from datetime import date, timedelta
    t = date.fromisoformat(trade_d)
    mon = (t - timedelta(days=t.weekday())).isoformat()
    return mon, trade_d


def _md(x) -> str:
    s = str(x or "")
    return s[5:] if len(s) >= 10 else (s or "-")


def _week_new(pd_, mon: str, fri: str, marks: pd.DataFrame) -> list[dict]:
    """이번 주(신호일 mon~fri) 새 추천 — 편입 행 + 아직 편입 전인 발송 기록.

    발송(paper_sends)은 됐는데 다음 거래일 시가가 아직 없으면 편입 행이
    없다(금요일 저녁 발송분은 월요일 시가에 편입). 그것도 '이번 주 새
    추천'이므로 '편입 대기'로 싣는다.
    """
    pos = pd_.book.positions()
    pos = pos[(pos["signal_d"] >= mon) & (pos["signal_d"] <= fri)] \
        if len(pos) else pos
    cur = {}
    if marks is not None and len(marks):
        cur = {int(r["id"]): P._nan(r.get("net")) for r in marks.to_dict("records")}
    rows: list[dict] = []
    seen: set = set()
    for r in (pos.to_dict("records") if len(pos) else []):
        seen.add((str(r["signal_d"]), str(r["ticker"])))
        if int(r.get("enrolled") or 0) and str(r.get("entry_date") or "") > fri:
            # 기준 거래일 뒤에 편입된 행(월요일 catch-up 등) — 그 날 기준으론 대기.
            rows.append({"ticker": r["ticker"], "name": r.get("name"),
                         "slot": r.get("slot"), "signal_d": r["signal_d"],
                         "entry": "편입 대기(다음 거래일 시가)", "ret": "-"})
        elif int(r.get("enrolled") or 0):
            ret = cur.get(int(r["id"]))
            rows.append({"ticker": r["ticker"], "name": r.get("name"),
                         "slot": r.get("slot"), "signal_d": r["signal_d"],
                         "entry": f"편입 {_md(r.get('entry_date'))}",
                         "ret": _pct(ret) if ret is not None else "-"})
        else:
            rows.append({"ticker": r["ticker"], "name": r.get("name"),
                         "slot": r.get("slot"), "signal_d": r["signal_d"],
                         "entry": f"미편입({r.get('skip_reason') or r.get('entry_status')})",
                         "ret": "-"})
    sends = pd_.book.sends()
    if len(sends):
        sends = sends[(sends["d"] >= mon) & (sends["d"] <= fri)]
    a = pd_.assigned
    amap = {(str(x["d"]), str(x["ticker"])): x for x in a.to_dict("records")} \
        if a is not None and len(a) else {}
    for s in (sends.to_dict("records") if len(sends) else []):
        k = (str(s["d"]), str(s["ticker"]))
        if k in seen:
            continue
        rec = amap.get(k)
        if rec is not None and (str(rec.get("event_date")), k[1]) in seen:
            continue                     # 같은 이벤트가 이미 편입돼 있다
        seen.add(k)
        rows.append({"ticker": k[1], "name": (rec or {}).get("name"),
                     "slot": (rec or {}).get("slot"), "signal_d": k[0],
                     "entry": "편입 대기(다음 거래일 시가)", "ret": "-"})
    rows.sort(key=lambda r: (str(r["signal_d"]), str(r["ticker"])))
    return rows


def _win(c: dict) -> str:
    w = c.get("win")
    return "-" if w is None or not np.isfinite(w) else f"{w:.0f}%"


def _join_rows(items: list[str], per_line: int) -> list[str]:
    per_line = max(1, int(per_line))
    return [" · ".join(items[i:i + per_line])
            for i in range(0, len(items), per_line)]


def _cum_lines(pd_, m_all: pd.DataFrame, H: int) -> tuple[list[str], dict]:
    """④ 누적 성적 두 줄 — 만기(H거래일) 확정분, 이벤트 기준."""
    if m_all is None or m_all.empty:
        return ["없음"], {}
    c = E.stat_cell(m_all["net"])
    x = E.stat_cell(m_all["ex_idx"])
    pct, _rank = P.pooled_percentile(
        m_all, P.matured_random(pd_, set(m_all["signal_d"]), H))
    few = " · 표본 부족" if len(m_all) < int(C.MIN_SCORED_FOR_JUDGMENT) else ""
    l1 = (f"• 만기 {len(m_all)}건 · 지수 대비 평균 {_pct(x['mean'], '%p')} · "
          f"승률 {_win(c)}"
          f" · 무작위 대비 백분위 {'-' if pct is None else f'{pct:.0f}'}{few}")
    parts = []
    mm = m_all.assign(slot_key=m_all["slot"].astype(str).str.rstrip("*"))
    order = [k for k in ("VALUE", "TREND", "SEQ", "FILL") if k in set(mm["slot_key"])]
    order += sorted(set(mm["slot_key"]) - set(order))
    for k in order:
        g = mm[mm["slot_key"] == k]
        cx = E.stat_cell(g["ex_idx"])
        cw = E.stat_cell(g["net"])
        lab = _e(_slot_label(k))
        if not cx.get("enough"):
            parts.append(f"{lab} 표본 부족(n={cx.get('n', len(g))})")
        else:
            parts.append(f"{lab} {_pct(cx['mean'], '%p')}·승률 {cw['win']:.0f}%"
                         f"(n={cx['n']})")
    l2 = "• 유형별 지수 대비: " + " · ".join(parts)
    info = {"mean": c.get("mean"), "win": c.get("win"), "ex_idx": x.get("mean"),
            "pct": pct}
    return [l1, l2], info


def sunday_scorecard(store, asof, trade_d: str, pdata=None, index=None,
                     limit: int | None = None) -> tuple[str, dict]:
    """일요일 주간 성적표 (PAPER_TRACKING) — 주간 메시지는 이것 하나만 나간다.

      ① 이번 주 새 추천   종목명(종목코드) · 유형 · 편입일 · 현재 수익률
      ② 이번 주 만기      종목명(종목코드) · 최종 수익률 · 지수 대비
      ③ 추적 중 전체      종목명(종목코드) 수익률, 높은 순, 가운뎃점으로 이어
                          한 줄에 여러 종목. 생략 없음
      ④ 누적 성적 두 줄   지수 대비·승률·무작위 대비 백분위 / 유형별
      ⑤ 면책 문구
    0건인 칸은 "없음" 한 줄. 합쳐서 SEND_LIMIT 를 넘으면 ③을 다음 메시지로
    떼어 보낸다(info["messages"]). 종목은 빼지 않는다 — ③ 자체가 길면 ③을
    여러 메시지로 나눈다. 성적은 전부 이벤트 기준(paper_positions: 중복 제거 ·
    다음 거래일 시가 편입 · 왕복 비용 차감)이다.
    """
    limit = int(SEND_LIMIT if limit is None else limit)
    pd_ = pdata or P.PaperData(store, trade_d, index=index)
    mon, fri = _week_bounds(trade_d, pd_.cal)
    H = int(C.PAPER_DISPLAY_DAYS)
    marks = P.active_marks(pd_, trade_d)
    new = _week_new(pd_, mon, fri, marks)
    m_all = P.matured_on(pd_, trade_d)
    m_wk = m_all[m_all["exit_date"] >= mon] if len(m_all) else m_all

    head = [f"🗓 <b>[주간 성적표]</b> {asof:%Y-%m-%d} · 기준 거래일 {trade_d}",
            f"• 이번 주 {mon}~{fri} · 성적은 이벤트 기준(같은 종목 재추천 1건) · "
            f"다음 거래일 시가 편입 · 왕복 {C.ROUND_TRIP_COST_PCT:g}%p 차감"]
    s1 = [f"━━ ① 이번 주 새 추천 ({len(new)}) ━━"]
    s1 += [f"• {stock_label(r['name'], r['ticker'])} · {_e(_slot_label(r['slot']))}"
           f" · {_e(r['entry'])} · {r['ret']}" for r in new] or ["없음"]
    s2 = [f"━━ ② 이번 주 만기 ({len(m_wk)}) · {H}거래일 확정 ━━"]
    if len(m_wk):
        for r in m_wk.sort_values("net", ascending=False).to_dict("records"):
            st = "" if r["status"] == E.ST_OK else f" ({_e(r['status'])})"
            s2.append(f"• {stock_label(r.get('name'), r['ticker'])} · 최종 "
                      f"{_pct(P._nan(r['net']))} · 지수 대비 "
                      f"{_pct(P._nan(r['ex_idx']), '%p')}{st}")
    else:
        s2.append("없음")
    n3 = 0 if marks is None else int(len(marks))
    s3_head = f"━━ ③ 추적 중 전체 ({n3}종목 · 수익률 높은 순) ━━"
    s3_body: list[str] = []
    if n3:
        mk = marks.assign(_v=marks["net"].astype(float))
        mk = mk.sort_values("_v", ascending=False, na_position="last")
        items = [f"{stock_label(r.get('name'), r['ticker'])} "
                 f"{_pct(P._nan(r['net']))}" for r in mk.to_dict("records")]
        s3_body = _join_rows(items, int(getattr(C, "PAPER_SUNDAY_PER_LINE", 3)))
    else:
        s3_body = ["없음"]
    cum, cinfo = _cum_lines(pd_, m_all, H)
    s4 = [f"━━ ④ 누적 성적 (만기 {H}거래일) ━━", *cum]
    s5 = [DISCLAIMER]

    def _join(blocks):
        return "\n\n".join("\n".join(b) for b in blocks if b)

    full = _join([head, s1, s2, [s3_head, *s3_body], s4, s5])
    messages = [full]
    split = False
    if len(full) > limit:
        split = True
        first = _join([head, s1, s2, [s3_head, "• 다음 메시지로 보냄"], s4, s5])
        messages = [first]
        chunk: list[str] = []
        parts: list[list[str]] = []
        for ln in s3_body:
            cand = chunk + [ln]
            if chunk and len("\n".join(["X" * 60, s3_head, *cand])) > limit:
                parts.append(chunk)
                chunk = [ln]
            else:
                chunk = cand
        if chunk:
            parts.append(chunk)
        for i, p in enumerate(parts, 1):
            tag = f" ({i}/{len(parts)})" if len(parts) > 1 else ""
            messages.append("\n".join(
                [f"🗓 <b>[주간 성적표 ③]</b> 기준 거래일 {trade_d}{tag}",
                 s3_head, *p]))
    info = {"week": [mon, fri], "new": len(new), "matured_week": int(len(m_wk)),
            "matured_all": int(len(m_all)), "tracking": n3,
            "week_tickers": [str(r["ticker"]) for r in new
                             if not str(r["entry"]).startswith("미편입")],
            "split": split, "messages": messages,
            "lengths": [len(x) for x in messages], **cinfo}
    return "\n\n".join(messages) if split else full, info


def curve_points(pd_, d: str) -> pd.DataFrame:
    """만기 코호트(신호일)별 20거래일(PAPER_DISPLAY_DAYS) 수익률 — 추천 / 시장 지수 / 무작위 중앙값.

    코호트 = 같은 날 편입된 묶음. 추천 = 그 묶음 확정 수익률(net) 동일 비중
    평균, 지수 = 같은 구간 소속 시장 지수 수익률(idx) 평균, 무작위 = 같은
    신호일 무작위 묶음 100개 평균의 중앙값. 코호트끼리 보유 구간이 겹치므로
    이어 붙이지 않고 나란히 놓는다. 최근 PAPER_CURVE_COHORTS 개만.
    """
    m = P.matured_on(pd_, d)
    if m.empty:
        return pd.DataFrame()
    H = int(C.PAPER_DISPLAY_DAYS)
    rnd = P.matured_random(pd_, set(m["signal_d"]), H)
    rows = []
    for sd, g in m.groupby("signal_d"):
        lst = [x[0] for x in rnd.get(sd, []) if x[0] is not None]
        rows.append({"signal_d": sd, "n": int(len(g)),
                     "reco": P._nan(g["net"].astype(float).mean()),
                     "index": P._nan(g["idx"].astype(float).mean()),
                     "random": float(np.median(lst)) if lst else np.nan})
    df = pd.DataFrame(rows).sort_values("signal_d").reset_index(drop=True)
    keep = int(getattr(C, "PAPER_CURVE_COHORTS", 12))
    return df.tail(keep).reset_index(drop=True) if keep > 0 else df


def curve_chart(pd_, d: str, path) -> Path | None:
    """코호트별 20거래일 수익률 막대 1장 (추천 · 시장 지수 · 무작위 중앙값)."""
    df = curve_points(pd_, d)
    if df.empty:
        return None
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from .chart import korean_font
    font = korean_font()
    if font:
        plt.rcParams["font.family"] = font
    plt.rcParams["axes.unicode_minus"] = False
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    n = len(df)
    fig, ax = plt.subplots(figsize=(max(6.0, 1.6 * n + 2.5), 4.5), dpi=110)
    x = np.arange(n)
    w = 0.26
    series = (("reco", "추천 (종이 편입)", "#2a6fdb"),
              ("index", "시장 지수", "#7a7a7a"),
              ("random", "무작위 중앙값", "#e08a2a"))
    for k, (col, lab, color) in enumerate(series):
        vals = df[col].astype(float).to_numpy()
        bars = ax.bar(x + (k - 1) * w, np.nan_to_num(vals), w, label=lab,
                      color=color)
        for b, v in zip(bars, vals):
            if np.isfinite(v):
                ax.annotate(f"{v:+.1f}", (b.get_x() + b.get_width() / 2, v),
                            xytext=(0, 3 if v >= 0 else -11),
                            textcoords="offset points", ha="center",
                            fontsize=8, color="#333333")
    ax.axhline(0, color="#999999", lw=0.8)
    ax.set_xticks(x)
    ax.set_xticklabels([f"{str(s)[5:]}\n({int(c)}종목)"
                        for s, c in zip(df["signal_d"], df["n"])], fontsize=8)
    ax.set_ylabel("%")
    ax.set_xlabel("코호트 신호일")
    ax.set_title(f"코호트별 {int(C.PAPER_DISPLAY_DAYS)}거래일 수익률 (%)",
                 fontsize=11)
    lo = float(np.nanmin(df[["reco", "index", "random"]].to_numpy(dtype=float)))
    hi = float(np.nanmax(df[["reco", "index", "random"]].to_numpy(dtype=float)))
    pad = max(2.0, (max(hi, 0) - min(lo, 0)) * 0.12)
    ax.set_ylim(min(lo, 0) - pad, max(hi, 0) + pad)
    ax.grid(axis="y", alpha=0.3)
    ax.legend(loc="best", fontsize=9)
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)
    return path


def curve_caption(df: pd.DataFrame, d: str) -> str:
    """코호트별 숫자 + '코호트 = 같은 날 편입된 묶음' 한 줄."""
    out = [f"📊 <b>코호트별 {int(C.PAPER_DISPLAY_DAYS)}거래일 수익률</b> (기준 {d})"]
    for r in df.to_dict("records"):
        out.append(f"{str(r['signal_d'])[5:]} ({int(r['n'])}종목): 추천 "
                   f"{_pct(r['reco'])} · 지수 {_pct(r['index'])} · 무작위 "
                   f"{_pct(r['random'])}")
    out.append("코호트 = 같은 날 편입된 묶음")
    out.append(f"<i>{_e(C.CHART_DISCLAIMER)}</i>")
    return "\n".join(out)


def sunday_detail(pd_, trade_d: str, asof) -> str:
    """일요일 발송에서 빠진 상세 — 주간 파일 전용(발송 안 함).

    만기분 누적(평균·중앙·승률·지수 대비·무작위 백분위) · 최대 낙폭 · 유형별 ·
    국면별. 2026-11-15 까지 일요일 성적표에 실리던 줄과 같은 계산이다.
    """
    H = int(C.PAPER_DISPLAY_DAYS)
    m_all = P.matured_on(pd_, trade_d)
    out = [f"{SUNDAY_DETAIL_HEAD} (기준 거래일 {trade_d}) ━━",
           f"• 생성 {asof:%Y-%m-%d %H:%M:%S} · 발송 안 함 · 이벤트 기준(중복 제거 · "
           f"다음 거래일 시가 편입 · 왕복 {C.ROUND_TRIP_COST_PCT:g}%p 차감)",
           f"[만기분 누적 ({H}거래일, n={len(m_all)})]"]
    if m_all.empty:
        out.append("• 만기된 추적 종목 없음")
        return "\n".join(out)
    c = E.stat_cell(m_all["net"])
    x = E.stat_cell(m_all["ex_idx"])
    pct, _rank = P.pooled_percentile(
        m_all, P.matured_random(pd_, set(m_all["signal_d"]), H))
    mdd = m_all["mdd"].astype(float).dropna() if "mdd" in m_all else pd.Series(dtype=float)
    if c["n"]:
        out.append(f"• 평균 {_pct(c['mean'])} · 중앙 {_pct(c['median'])} · "
                   f"승률 {_win(c)}")
    out.append(f"• 지수 대비 평균 {_pct(x['mean'], '%p')} · 무작위 대비 백분위 "
               f"{'-' if pct is None else f'{pct:.0f}'}")
    if len(mdd):
        out.append(f"• 최대 낙폭 중앙 {mdd.median():+.2f}% · 최악 {mdd.min():+.2f}%")
    else:
        out.append("• 최대 낙폭 기록 없음")
    if len(m_all) < int(C.MIN_SCORED_FOR_JUDGMENT):
        out.append("• 표본 부족 — 판단 보류")
    mm = m_all.assign(slot_key=m_all["slot"].astype(str).str.rstrip("*"))
    out.append("[유형별 · 수익률(비용 차감)]")
    for k, g in mm.groupby("slot_key"):
        out.append(f"• {_slot_label(k)}: {E.fmt_cell(E.stat_cell(g['net']))}")
    out.append("[유형별 · 지수 대비]")
    for k, g in mm.groupby("slot_key"):
        out.append(f"• {_slot_label(k)}: {E.fmt_cell(E.stat_cell(g['ex_idx']))}")
    out.append("[국면별 · 수익률(비용 차감)]")
    reg = mm["regime"].fillna("국면 없음") if "regime" in mm else \
        pd.Series("국면 없음", index=mm.index)
    for k, g in mm.groupby(reg):
        out.append(f"• {k}: {E.fmt_cell(E.stat_cell(g['net']))}")
    return "\n".join(out)


SUNDAY_DETAIL_HEAD = "━━ 일요일 성적표 상세"


# ══════════════════════════ 주간 그림자 청산 표 ══════════════════════════
_SET_LABEL = {"current": "현행 엔진", "alt": "대안 세트", "hold20": "고정 20일"}


def weekly_shadow_block(pd_, d: str | None = None) -> str:
    t = P.shadow_table(pd_, d)
    out = ["━━ 그림자 청산 비교 (기록 전용) ━━"]
    if not t:
        out.append("• 확정된 그림자 청산 없음 (편입 후 21거래일째 시가가 필요)")
        return "\n".join(out)
    regs = sorted(k for k in t if k != "전체")
    if regs == ["국면 없음"]:
        regs = []          # 국면 기록이 하나도 없으면 전체와 같은 표다
        out.append("• 국면 기록 없음 — 국면별 분리 생략")
    for reg in ["전체"] + regs:
        blk = t[reg]
        out.append(f"[{_e(reg)}] 포지션 {blk['positions']}건")
        for rs in ("current", "alt", "hold20"):
            c = blk["sets"].get(rs)
            if c is not None:
                out.append(f"• {_SET_LABEL[rs]}: {E.fmt_cell(c)}")
        for rs in ("current", "alt"):
            items = sorted(((rule, v) for (s, rule), v in blk["rules"].items()
                            if s == rs), key=lambda x: -x[1]["n"])
            if items:
                out.append(f"  {_SET_LABEL[rs]} 규칙별 발동 · 만기 보유 대비 차이 · 갭")
                for rule, v in items:
                    out.append(f"   {_e(rule)} {v['n']}건 · "
                               f"{_pct(v['diff_mean'], '%p')} · 갭 {_pct(v['gap_mean'])}")
    out.append("※ 발동 차이 = (그 규칙 체결 수익 − 20일 보유 수익) 평균. 갭 = "
               "다음 거래일 시가 ÷ 계획가 − 1 (고정 손절은 손절선, 그 밖은 신호 종가)")
    return "\n".join(out)
