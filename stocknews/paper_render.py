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
from .renderer import SLOT_LABEL, _e

log = logging.getLogger(__name__)

__all__ = ["weekday_enabled", "contributions", "reasons_for", "slot_perf",
           "weekday_block", "sunday_scorecard", "curve_chart",
           "weekly_shadow_block", "TEXT_LIMIT"]

TEXT_LIMIT = 4096
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
            out.append(f"{i}. <b>{_e(r.get('name') or t)}</b>"
                       f" ({t}) [{_e(r.get('grade'))}·{_e(_slot_label(r.get('slot')))}]"
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
        out.append(f"• {_e(r.get('name') or r['ticker'])} ({r['ticker']}) — "
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
        out.append(f"• <b>{_e(r.get('name') or r['ticker'])}</b> ({r['ticker']}) "
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
        return (f"  {_e(r.get('name') or r['ticker'])} {_pct(P._nan(r['net']))}"
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
    while True:
        sec3, tinfo = _tracking_section(pd_, d, topn)
        text = "\n\n".join("\n".join(p) for p in (sec1, sec2, sec3, sec4) if p) \
            + "\n" + FOOTER
        if len(text) <= limit_room or topn <= 0:
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


def sunday_scorecard(store, asof, trade_d: str, pdata=None, index=None
                     ) -> tuple[str, dict]:
    """일요일 주간 성적표 (PAPER_TRACKING). 새 추천 목록은 보내지 않는다."""
    pd_ = pdata or P.PaperData(store, trade_d, index=index)
    mon, fri = _week_bounds(trade_d, pd_.cal)
    pos = pd_.book.positions()
    pos = pos[pos["signal_d"] <= trade_d] if len(pos) else pos
    wk = pos[(pos["signal_d"] >= mon)] if len(pos) else pos
    m_all = P.matured_on(pd_, trade_d)
    m_wk = m_all[m_all["exit_date"] >= mon] if len(m_all) else m_all
    skipped_all = int((pos["enrolled"] == 0).sum()) if len(pos) else 0
    skipped_wk = int((wk["enrolled"] == 0).sum()) if len(wk) else 0
    info = {"week": [mon, fri], "new": int(len(wk)), "matured_week": int(len(m_wk)),
            "matured_all": int(len(m_all)), "not_enrolled": skipped_all,
            "week_tickers": [str(t) for t in wk["ticker"]] if len(wk) else []}
    H = int(C.PAPER_DISPLAY_DAYS)
    out = [f"🗓 <b>[주간 성적표]</b> {asof:%Y-%m-%d} · 기준 거래일 {trade_d}",
           f"• 이번 주({mon}~{fri}) 새 추천 {len(wk)}건 · 만기 {len(m_wk)}건 · "
           f"미편입 {skipped_wk}건 (누적 {skipped_all}건)", ""]
    out.append(f"━━ 만기분 누적 ({H}거래일, n={len(m_all)}) ━━")
    if m_all.empty:
        out.append("• 만기된 추적 종목 없음")
    else:
        c = E.stat_cell(m_all["net"])
        x = E.stat_cell(m_all["ex_idx"])
        pct, rank = P.pooled_percentile(
            m_all, P.matured_random(pd_, set(m_all["signal_d"]), H))
        mdd = m_all["mdd"].astype(float).dropna()
        if c["n"]:
            out.append(f"• 평균 {_pct(c['mean'])} · 중앙 {_pct(c['median'])} · "
                       f"승률 {c['win']:.0f}%")
        out.append(f"• 지수 대비 평균 {_pct(x['mean'], '%p')} · 무작위 대비 백분위 "
                   f"{'-' if pct is None else f'{pct:.0f}'}")
        if len(mdd):
            out.append(f"• 최대 낙폭 중앙 {mdd.median():+.2f}% · 최악 {mdd.min():+.2f}%")
        info.update({"mean": c["mean"], "median": c["median"], "win": c["win"],
                     "ex_idx": x["mean"], "pct": pct})
    if len(m_all) < int(C.MIN_SCORED_FOR_JUDGMENT):
        out.append("• 표본 부족 — 판단 보류")
    if len(m_all):
        out.append("")
        out.append("━━ 슬롯별 ━━")
        m_all = m_all.assign(slot_key=m_all["slot"].astype(str).str.rstrip("*"))
        for k, g in m_all.groupby("slot_key"):
            out.append(f"• {_e(_slot_label(k))}: {E.fmt_cell(E.stat_cell(g['net']))}")
        out.append("━━ 국면별 ━━")
        reg = m_all["regime"].fillna("국면 없음")
        for k, g in m_all.groupby(reg):
            out.append(f"• {_e(k)}: {E.fmt_cell(E.stat_cell(g['net']))}")
    out.append("")
    out.append(FOOTER)
    out.append(f"⏰ {asof:%Y-%m-%d %H:%M:%S}")
    return "\n".join(out), info


def curve_points(pd_, d: str) -> pd.DataFrame:
    """만기 코호트(신호일)별 평균 → 누적합. 추천 / 지수 / 무작위 중앙값."""
    m = P.matured_on(pd_, d)
    if m.empty:
        return pd.DataFrame()
    H = int(C.PAPER_DISPLAY_DAYS)
    rnd = P.matured_random(pd_, set(m["signal_d"]), H)
    rows = []
    for sd, g in m.groupby("signal_d"):
        lst = [x[0] for x in rnd.get(sd, []) if x[0] is not None]
        rows.append({"signal_d": sd, "reco": P._nan(g["net"].astype(float).mean()),
                     "index": P._nan(g["idx"].astype(float).mean()),
                     "random": float(np.median(lst)) if lst else np.nan})
    df = pd.DataFrame(rows).sort_values("signal_d")
    for c in ("reco", "index", "random"):
        df[f"cum_{c}"] = df[c].fillna(0.0).cumsum()
    return df


def curve_chart(pd_, d: str, path) -> Path | None:
    """누적 곡선 1장 (추천 vs 시장 지수 vs 무작위 중앙값). 없으면 None."""
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
    fig, ax = plt.subplots(figsize=(8, 4.5), dpi=110)
    x = pd.to_datetime(df["signal_d"])
    ax.plot(x, df["cum_reco"], marker="o", lw=2, color="#2a6fdb", label="추천 (종이 편입)")
    ax.plot(x, df["cum_index"], marker="s", lw=1.5, color="#7a7a7a", label="시장 지수")
    ax.plot(x, df["cum_random"], marker="^", lw=1.5, color="#e08a2a",
            label="무작위 중앙값")
    ax.axhline(0, color="#bbbbbb", lw=0.8)
    ax.set_title(f"만기 코호트 누적 ({int(C.PAPER_DISPLAY_DAYS)}거래일 수익률 합, %p)")
    ax.set_xlabel("신호일")
    ax.grid(alpha=0.3)
    ax.legend(loc="best", fontsize=9)
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)
    return path


def curve_caption(df: pd.DataFrame, d: str) -> str:
    last = df.iloc[-1]
    return (f"📈 <b>추천 성적 누적 곡선</b> (기준 {d}, 코호트 {len(df)}개)\n"
            f"추천 {last['cum_reco']:+.2f}%p · 지수 {last['cum_index']:+.2f}%p · "
            f"무작위 중앙 {last['cum_random']:+.2f}%p\n"
            "코호트 = 같은 날 편입된 묶음의 20거래일 평균 수익률, 단순 누적합\n"
            f"<i>{_e(C.CHART_DISCLAIMER)}</i>")


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
