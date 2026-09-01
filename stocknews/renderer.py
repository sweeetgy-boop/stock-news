# -*- coding: utf-8 -*-
"""텔레그램 메시지 조판. 표(table) 미사용, 텍스트 기호 규격.

parse_mode='HTML' 을 쓰므로 종목명/뉴스제목은 반드시 escape 한다.
이걸 빼면 '&'나 '<'가 들어간 제목에서만 조용히 전송이 실패한다.
"""
from __future__ import annotations

import html

from .config import Config, DEFAULT
from .contracts import RULE_NAME, ScreenResult

__all__ = ["bar", "render_detail", "render_digest", "render_fib_list"]


def _e(s) -> str:
    return html.escape(str(s), quote=False)


def bar(r: float, n: int = 10) -> str:
    """0~1 위치를 막대로. 숫자를 읽기 전에 위치를 파악할 수 있게 한다."""
    if r != r:  # NaN
        return "[" + "?" * n + "]"
    r = max(0.0, min(1.0, r))
    filled = int(round(r * n))
    return "[" + "\u2593" * filled + "\u2591" * (n - filled) + "]"


def _fmt(v, unit: str = "원") -> str:
    try:
        if v != v:
            return "-"
        return f"{v:,.0f}{unit}"
    except (TypeError, ValueError):
        return "-"


def _pct(v) -> str:
    try:
        if v != v:
            return "-"
        return f"{v:+.1f}%"
    except (TypeError, ValueError):
        return "-"


def _trend_block(r: ScreenResult, cfg: Config) -> str:
    t = r.trend
    if t is None:
        return "━━ 이평선 20/40/60 ━━\n• 데이터 부족\n"
    ma = cfg.ma
    align_kr = {"GOLDEN": "정배열", "DEAD": "역배열",
                "MIXED": "혼재", "UNKNOWN": "판정불가"}.get(t.alignment, t.alignment)
    lines = [
        "━━ 이평선 20/40/60 ━━",
        f"• 배열: <b>{align_kr}</b>  (추세점수 {t.score:.1f}/10.0)",
        f"• MA{ma.short} {_fmt(t.ma_short)} / MA{ma.mid} {_fmt(t.ma_mid)} / "
        f"MA{ma.long} {_fmt(t.ma_long)}",
        f"• 3선 밀집도: {t.convergence_pct:.2f}%   "
        f"MA{ma.long} 기울기: {_pct(t.slope_long_pct)}",
    ]
    if t.best_cross and t.best_cross.kind == "GOLDEN":
        lines.append(
            f"• 골든크로스: <b>{t.best_cross.pair}</b>  "
            f"D+{t.best_cross.bars_ago} ({t.best_cross.date:%m/%d})"
        )
        lines.append(f"• 크로스일 거래량: 20일 평균 {t.vol_ratio_at_cross:.1f}배")
        if t.whipsaw_count:
            lines.append(f"• 크로스 후 60선 이탈: {t.whipsaw_count}회 (휩쏘 감점)")
    else:
        lines.append("• 골든크로스: 미발생")
    return "\n".join(lines) + "\n"


def _fib_block(r: ScreenResult, cfg: Config) -> str:
    f = r.fib
    if f is None:
        return "━━ 1년 고점 피보나치 되돌림 ━━\n• 데이터 부족\n"
    tgt = cfg.fib.target
    lines = [
        "━━ 1년 고점 피보나치 되돌림 ━━",
        f"• 1년 고점: <b>{_fmt(f.high)}</b> ({f.high_date:%y/%m/%d}, "
        f"{f.high_age}거래일 경과)",
        f"• 파동 저점: {_fmt(f.swing_low)}  → 파동 폭 {_fmt(f.swing)}",
        f"• 되돌림 진행률: <b>{f.ratio:.3f}</b>  구간 {f.zone}",
        f"• {tgt:.3f} 지지선: <b>{_fmt(f.levels.get(tgt))}</b>  "
        f"{'✅ 이하 진입' if f.below_target else '미도달'}",
        f"• 근접 레벨: {f.nearest_level:.3f} (이격 {f.nearest_gap_pct:.2f}%)",
    ]
    ladder = "  ".join(
        f"{k:.3f}={v:,.0f}" for k, v in sorted(f.levels.items()) if k in (0.382, 0.5, 0.618, 0.786)
    )
    lines.append(f"• 레벨: {ladder}")
    if f.wave_broken:
        lines.append("• ⚠️ 전 파동 저점 붕괴 — 되돌림 아님, 추세 파괴로 분류")
    lines.append(f"• 신뢰도: {f.confidence}  (되돌림점수 {f.score:.1f}/10.0)")
    return "\n".join(lines) + "\n"


def _liq_block(r: ScreenResult) -> str:
    q = r.liq
    if q is None:
        return "━━ 신용청산 밴드 ━━\n• 평균단가 추정 불가\n"
    cr = "-" if q.credit_ratio != q.credit_ratio else f"{q.credit_ratio:.2f}%"
    lines = [
        "━━ 신용 강제청산 밴드 ━━",
        f"• 추정 평균단가: <b>{_fmt(q.cost_basis)}</b> (방법 {q.basis_method}, "
        f"신뢰도 {q.confidence})",
        f"• 신용잔고율: {cr}",
        f"• 마진콜 개시 -16%: {_fmt(q.band_hi)}",
        f"• 대량청산 -30%: <b>{_fmt(q.band_mid)}</b>",
        f"• 연쇄청산 -44%: {_fmt(q.band_lo)}",
        f"• 밴드 위치: <b>{q.band_pos * 100:.0f}%</b> {bar(q.band_pos)}",
        f"• 거래량 {q.vol_ratio:.1f}배 · 공매도 {q.short_trend} · "
        f"D+2 반대매매 {'해당' if q.is_margin_due else '미해당'}",
        f"• 청산압력점수: {q.score:.1f}/10.0",
    ]
    return "\n".join(lines) + "\n"


def render_detail(r: ScreenResult, news_title: str | None = None,
                  cfg: Config = DEFAULT) -> str:
    """티어 1 즉시 속보."""
    track_kr = {"VALUE": "매집(역추세)", "TREND": "확증(순추세)",
                "BOTH": "시퀀스 결합"}.get(r.track, r.track)
    head = [
        f"🚨 <b>[{r.grade} 등급 시그널]</b> {r.mark}",
        "",
        f"📌 종목: <b>{_e(r.name)} ({r.ticker})</b>",
        f"📌 현재가: <b>{_fmt(r.price)}</b>",
        f"📌 트랙: {track_kr}",
        f"📌 매집 {r.value_score:.1f} / 추세 {r.trend_score:.1f}",
    ]
    if news_title:
        head.append(f"📢 트리거: {_e(news_title)}")
    head.append("")

    body = [_trend_block(r, cfg), _fib_block(r, cfg), _liq_block(r)]

    tail = ["━━ 판정 근거 ━━"]
    tail += [f"• {_e(x)}" for x in r.reasons] or ["• 특이사항 없음"]
    tail.append("")
    tail.append(f"⏰ {r.asof:%Y-%m-%d %H:%M:%S}")

    return "\n".join(head) + "\n".join(body) + "\n" + "\n".join(tail)


def render_digest(items: list[ScreenResult], asof, cfg: Config = DEFAULT) -> str:
    """티어 2 장마감 다이제스트. 종목당 3줄, 스마트폰 한 화면."""
    if not items:
        return (f"📋 <b>[장마감 스크리닝]</b> {asof:%m/%d}\n\n"
                "• 기준 통과 종목 없음. 관망 유지.")
    head = f"📋 <b>[장마감 스크리닝 TOP {len(items)}]</b> {asof:%m/%d}\n"
    out = [head]
    for i, r in enumerate(items, 1):
        f = r.fib
        q = r.liq
        line2 = ""
        if f:
            line2 = (f"   피보 {f.ratio:.3f}"
                     f"{' ✅' if f.below_target else ''} "
                     f"({cfg.fib.target:.3f}선 {f.levels.get(cfg.fib.target, 0):,.0f})")
        line3 = ""
        if q:
            line3 = (f"   청산중심 {q.band_mid:,.0f} "
                     f"({(r.price / q.band_mid - 1) * 100:+.1f}%) "
                     f"{bar(q.band_pos, 6)}")
        cross = ""
        if r.trend and r.trend.best_cross and r.trend.best_cross.kind == "GOLDEN":
            cross = f" · {r.trend.best_cross.pair}GC D+{r.trend.best_cross.bars_ago}"
        out.append(
            f"\n{i}. <b>{_e(r.name)}</b> {r.mark} [{r.grade}]  "
            f"V{r.value_score:.1f}/T{r.trend_score:.1f}\n"
            f"   {r.price:,.0f}원{cross}\n"
            + (line2 + "\n" if line2 else "")
            + (line3 if line3 else "")
        )
    out.append(f"\n\n※ {cfg.gate.value_threshold:.1f}점 이상만 즉시 속보 발송됩니다.")
    return "".join(out)


def render_fib_list(items: list[ScreenResult], asof,
                    cfg: Config = DEFAULT) -> str:
    """'1년 고점 대비 피보나치 레벨 이하' 전용 목록.

    골든크로스 여부를 함께 붙여 매복 대상(아직 크로스 전)과
    진입 대상(크로스 발생)을 한눈에 구분한다.
    """
    tgt = cfg.fib.target
    if not items:
        return (f"🎯 <b>[피보 {tgt:.3f} 이하 종목]</b> {asof:%m/%d}\n\n"
                "• 해당 종목 없음.")
    waiting, ready = [], []
    for r in items:
        gc = (r.trend and r.trend.best_cross
              and r.trend.best_cross.kind == "GOLDEN"
              and r.trend.best_cross.bars_ago <= cfg.ma.stale_days)
        (ready if gc else waiting).append(r)

    def _row(r: ScreenResult) -> str:
        f = r.fib
        gcs = ""
        if r.trend and r.trend.best_cross and r.trend.best_cross.kind == "GOLDEN":
            gcs = f" · {r.trend.best_cross.pair}GC D+{r.trend.best_cross.bars_ago}"
        return (f"• <b>{_e(r.name)}</b> ({r.ticker}) {r.mark}\n"
                f"  {r.price:,.0f}원 · 되돌림 {f.ratio:.3f} · "
                f"고점 {f.high:,.0f}({f.high_age}일전){gcs}")

    parts = [f"🎯 <b>[1년 고점 피보 {tgt:.3f} 이하 스크리닝]</b> {asof:%m/%d}\n"]
    if ready:
        parts.append(f"\n■ 골든크로스 발생 ({len(ready)}건) — 진입 검토")
        parts += [_row(r) for r in ready]
    if waiting:
        parts.append(f"\n■ 크로스 대기 ({len(waiting)}건) — 매복 관찰")
        parts += [_row(r) for r in waiting]
    return "\n".join(parts)


SLOT_LABEL = {
    "SEQ": "시퀀스",
    "VALUE": "매집",
    "TREND": "추세",
    "FILL": "보충",
    "FILL*": "보충",
}


def render_top10(picks: list, asof, trade_date: str, scanned: int,
                 cfg: Config = DEFAULT) -> str:
    """매일 저녁 추천 10선.

    한 종목당 3줄로 제한한다. 10종목이면 30줄 남짓, 스마트폰 한두 화면이다.
    이 분량 제약을 지키지 않으면 결국 안 읽는다.
    """
    if not picks:
        return (f"🌙 <b>[저녁 전종목 스캔]</b> {trade_date}\n\n"
                f"• {scanned:,}종목 스캔 완료\n"
                "• 기준 통과 종목 없음. 현금 보존 유지.")

    head = [
        f"🌙 <b>[저녁 스캔 추천 {len(picks)}선]</b> {trade_date}",
        f"• 스캔 {scanned:,}종목 · 슬롯 시퀀스2/매집5/추세3 · 동일업종 최대 2",
        "",
    ]
    body = []
    for i, (slot, r) in enumerate(picks, 1):
        f, q, t = r.fib, r.liq, r.trend
        l1 = (f"{i}. <b>{_e(r.name)}</b> ({r.ticker}) {r.mark} "
              f"[{r.grade}·{SLOT_LABEL.get(slot, slot)}]")
        l2 = f"   {r.price:,.0f}원 · 매집 {r.value_score:.1f} / 추세 {r.trend_score:.1f}"
        bits = []
        if f is not None:
            bits.append(f"피보 {f.ratio:.3f}{'✅' if f.below_target else ''}")
        if t is not None and t.best_cross and t.best_cross.kind == "GOLDEN":
            bits.append(f"{t.best_cross.pair}GC D+{t.best_cross.bars_ago}")
        elif t is not None:
            bits.append({"GOLDEN": "정배열", "DEAD": "역배열",
                         "MIXED": "혼재"}.get(t.alignment, "-"))
        if q is not None:
            bits.append(f"청산중심 {q.band_mid:,.0f}({(r.price / q.band_mid - 1) * 100:+.0f}%)")
        l3 = "   " + " · ".join(bits) if bits else ""
        body.append("\n".join(x for x in (l1, l2, l3) if x))

    tail = [
        "",
        "※ 매집=역추세 바닥 그물 / 추세=골든크로스 확증",
        "※ 시퀀스 = 청산밴드 진입 후 골든크로스 (최상위 등급)",
        f"⏰ {asof:%Y-%m-%d %H:%M:%S}",
    ]
    return "\n".join(head) + "\n\n".join(body) + "\n" + "\n".join(tail)


def render_reco_stored(rows, asof, send_dow_label: str = "일요일") -> str:
    """DB 에 쌓인 추천으로 만드는 주간 10선.

    발송 요일(기본 일요일)은 휴장일이라 그날 스캔이 돌지 않는다. 그래서
    새로 스캔하지 않고 마지막 거래일에 이미 기록된 recos 를 그대로 낸다.
    `render_top10` 은 `ScreenResult` 전체(피보/추세/유동성 객체)를 요구하지만
    `recos` 테이블에는 그 원본이 없다. 없는 값을 추정해 채우면 발송된 숫자와
    채점되는 숫자가 달라지므로, 저장된 열만 쓰는 별도 렌더러로 분리했다.

    rows : store.reco_history(days=1) 결과 (DataFrame)
    """
    if rows is None or len(rows) == 0:
        return ("🌙 <b>[주간 추천]</b>\n\n"
                "• 기록된 추천이 없습니다. daily 를 먼저 실행하십시오.")

    trade_date = str(rows.iloc[0]["d"])
    head = [
        f"🗓 <b>[주간 추천 {len(rows)}선]</b> 기준일 {trade_date}",
        f"• 발송은 {send_dow_label} 1회 · 스캔과 기록은 매일 진행됩니다",
        "",
    ]
    body = []
    for _, r in rows.iterrows():
        slot = str(r["slot"])
        l1 = (f"{int(r['rank'])}. <b>{_e(str(r['name']))}</b> ({r['ticker']}) "
              f"[{r['grade']}·{SLOT_LABEL.get(slot, slot)}]")
        l2 = (f"   {float(r['price']):,.0f}원 · 매집 {float(r['value_score']):.1f}"
              f" / 추세 {float(r['trend_score']):.1f}")
        reason = str(r["reason"] or "").strip()
        if len(reason) > 90:
            reason = reason[:89] + "…"
        l3 = f"   {_e(reason)}" if reason else ""
        body.append("\n".join(x for x in (l1, l2, l3) if x))

    tail = [
        "",
        "※ 매집=역추세 바닥 그물 / 추세=골든크로스 확증",
        "※ 기준일 종가 기준입니다. 발송 시점 가격과 다릅니다.",
        f"⏰ {asof:%Y-%m-%d %H:%M:%S}",
    ]
    return "\n".join(head) + "\n\n".join(body) + "\n" + "\n".join(tail)


def render_daily_holdings(positions: list, price_map: dict | None, asof,
                          trade_date: str, scanned: int, picks: int,
                          send_dow_label: str = "일요일") -> str:
    """발송 요일이 아닌 날의 daily 메시지. 보유 종목 상태만.

    신규 추천 종목은 한 종목도 넣지 않는다. 개수만 알린다 — 파이프라인이
    돌았는지는 알아야 하지만, 종목을 보여주면 주 1회 리듬이 무의미해진다.
    """
    out = [render_positions(positions, price_map), ""]
    out.append("━━ 오늘 스캔 ━━")
    out.append(f"• 기준일 {trade_date} · {scanned:,}종목 스캔 · "
               f"추천 {picks}건 기록")
    out.append(f"• 추천 목록은 {send_dow_label}에 발송됩니다")
    out.append(f"⏰ {asof:%Y-%m-%d %H:%M:%S}")
    return "\n".join(out)


def _paper_banner(a: dict) -> list[str]:
    """종이거래 고정 문구. 리포트 맨 위에 붙는다.

    검증 표본이 최소치에 닿기 전에 이 숫자를 실거래 판단에 쓰면 안 된다.
    그 사실을 매번 명시한다 — 문서에만 적어두면 시간이 지나며 잊힌다.
    """
    n = int(a.get("total_recos") or 0)
    need = int(a.get("min_sample") or 0)
    return [f"📋 검증 기간 — 실거래 아님. alpha_win_rate 확보까지 "
            f"기록만 누적 중 (현재 {n}건/최소 {need}건)"]


def _audit_single(a: dict) -> list[str]:
    """단일 보유기간 블록 (audit_recos 반환 형태)."""
    if not a or a.get("n", 0) == 0:
        return [f"• {a.get('note', '데이터 부족')}"]
    lines = [
        f"• 표본 {a['n']}건 · 보유 {a['horizon']}거래일 가정",
        f"• 승률: <b>{a['win_rate']:.0f}%</b>  "
        f"(시장 대비 초과 승률 {a['alpha_win_rate']:.0f}%)",
        f"• 평균 수익률: <b>{a['mean_ret']:+.2f}%</b>  "
        f"(중위 {a['median_ret']:+.2f}%)",
        f"• 시장 중위: {a['mean_mkt']:+.2f}%  → "
        f"초과수익 <b>{a['mean_alpha']:+.2f}%p</b>",
    ]
    if a["mean_alpha"] <= 0:
        lines.append("• ⚠️ 초과수익이 0 이하다. 하락장 반등을 전략 실력으로")
        lines.append("  착각하고 있을 수 있다. 파라미터 재검토 필요.")
    if a.get("by_slot"):
        parts = [f"{SLOT_LABEL.get(k, k)} {v['mean']:+.2f}%p({v['count']})"
                 for k, v in a["by_slot"].items()]
        lines.append("• 슬롯별 초과수익: " + " · ".join(parts))
    if a.get("best"):
        lines.append("• 최고: " + ", ".join(
            f"{_e(x['name'])} {x['ret']:+.1f}%" for x in a["best"]))
    if a.get("worst"):
        lines.append("• 최악: " + ", ".join(
            f"{_e(x['name'])} {x['ret']:+.1f}%" for x in a["worst"]))
    return lines


def _audit_table(a: dict) -> list[str]:
    """보유기간별 성적을 나란히. 어느 기간에서 알파가 나는지 비교용.

    표는 `<pre>` 로 감싼다. 텔레그램 HTML 파스모드에서 등폭이 유지되지
    않으면 열이 어긋나 비교가 불가능해진다.
    """
    by = a.get("by_horizon") or {}
    hs = a.get("horizons") or sorted(by)
    if not hs:
        return ["• 데이터 부족"]

    head = f"{'보유':>4} {'표본':>5} {'미도래':>6} {'승률':>6} {'초과승률':>8} {'평균':>7} {'초과':>8}"
    rows = [head, "-" * len(head)]
    scored = 0
    for h in hs:
        d = by.get(h) or by.get(str(h)) or {}
        n = int(d.get("n") or 0)
        pend = int(d.get("pending") or 0)
        if n == 0:
            rows.append(f"{h:>3}일 {'-':>5} {pend:>6} {'-':>6} {'-':>8} "
                        f"{'-':>7} {'-':>8}")
            continue
        scored += 1
        rows.append(
            f"{h:>3}일 {n:>5} {pend:>6} {d['win_rate']:>5.0f}% "
            f"{d['alpha_win_rate']:>7.0f}% {d['mean_ret']:>+6.2f}% "
            f"{d['mean_alpha']:>+7.2f}%p")

    out = ["<pre>" + "\n".join(rows) + "</pre>"]
    if scored == 0:
        pend_total = sum(int((by.get(h) or {}).get("pending") or 0) for h in hs)
        out.append(f"• 채점 가능한 표본이 없다 (미도래 {pend_total}건). "
                   "보유기간이 경과해야 성적이 나온다.")
        return out

    # 가장 짧은 채점 가능 기간의 상세만 덧붙인다. 세 기간 전부 풀어쓰면
    # 리포트가 표보다 길어져 비교라는 목적을 잃는다.
    for h in hs:
        d = by.get(h) or by.get(str(h)) or {}
        if int(d.get("n") or 0) > 0:
            out.append(f"• {h}거래일 상세")
            out += ["  " + ln for ln in _audit_single(d)]
            break
    return out


def _audit_block(a: dict) -> list[str]:
    """자기검증 블록. 다중 기간(audit_multi)과 단일(audit_recos) 둘 다 받는다."""
    head = ["━━ ① 지난 추천 자기검증 ━━"]
    if not a:
        return head + ["• 데이터 부족"]
    if "by_horizon" in a:
        return head + _audit_table(a)
    return head + _audit_single(a)


def render_weekly(rep: dict, asof, cfg: Config = DEFAULT) -> str:
    """금요일 주간 누적 분석 리포트."""
    audit = rep.get("audit") or {}
    out: list[str] = [
        f"📅 <b>[주간 누적 분석]</b> {rep.get('trade_date')}",
    ]
    out += _paper_banner(audit)
    out += [
        f"• 집계 {rep.get('days_covered', 0)}거래일 · "
        f"관측 {rep.get('universe_size', 0):,}종목",
        "",
    ]
    out += _audit_block(audit)
    out.append("")

    mom = rep.get("momentum")
    out.append("━━ ② 매집점수 상승 상위 ━━")
    if mom is not None and len(mom):
        for _, r in mom.iterrows():
            out.append(
                f"• <b>{_e(r['name'])}</b> {r['last_v']:.1f}점 "
                f"(주간 {r['delta_v']:+.1f}) · {r['price']:,.0f}원"
                + (f" · 피보 {r['fib_ratio']:.3f}" if r['fib_ratio'] == r['fib_ratio'] else "")
            )
    else:
        out.append("• 해당 없음")
    out.append("")

    per = rep.get("persistence")
    out.append("━━ ③ 연속 등재 (구조적 신호) ━━")
    if per is not None and len(per):
        for _, r in per.head(8).iterrows():
            price = r.get("price")
            ps = f" · {price:,.0f}원" if price == price and price else ""
            out.append(f"• <b>{_e(r['name'])}</b> {int(r['hits'])}회 등재 "
                       f"(최고 {int(r['best_rank'])}위) · 평균 매집 {r['avg_v']:.1f}{ps}")
    else:
        out.append("• 3회 이상 반복 등재 종목 없음")
    out.append("")

    ch = rep.get("churn") or {}
    out.append("━━ ④ 신규 진입 / 이탈 ━━")
    ent = ch.get("entered") or []
    drp = ch.get("dropped") or []
    out.append("• 신규: " + (", ".join(_e(n) for _, n in ent[:10]) if ent else "없음"))
    out.append("• 이탈: " + (", ".join(_e(n) for _, n in drp[:10]) if drp else "없음"))
    out.append("")

    eta = rep.get("eta")
    out.append("━━ ⑤ 청산 중심선(-30%) 도달 예상 ━━")
    if eta is not None and len(eta):
        for _, r in eta.iterrows():
            mid = r["band_mid"]
            ms = f"{mid:,.0f}원" if mid == mid else "-"
            out.append(f"• <b>{_e(r['name'])}</b> {r['price']:,.0f}원 → {ms} "
                       f"· 약 {int(r['eta_days'])}거래일 후 "
                       f"(위치 {r['band_pos']:.2f})")
        out.append("• ※ 선형 외삽 추정치다. 매복 우선순위용이며 예측이 아니다.")
    else:
        out.append("• 하락 접근 중인 종목 없음")
    out.append("")

    ev = rep.get("events") or {}
    out.append(f"━━ ⑥ 주중 이벤트 ━━")
    fb = ev.get("fib_breaks") or []
    cr = ev.get("cross") or []
    out.append(f"• 피보 {cfg.fib.target:.3f} 하향 이탈 ({len(fb)}건)")
    for x in fb[:8]:
        out.append(f"  - {_e(x['name'])} {x['price']:,.0f}원 "
                   f"(진행률 {x['ratio']:.3f})")
    out.append(f"• 골든크로스 발생 ({len(cr)}건)")
    for x in cr[:8]:
        out.append(f"  - {_e(x['name'])} {x['pair']} D+{x['bars_ago']} "
                   f"· 추세 {x['trend_score']:.1f}")
    out.append("")

    sec = rep.get("sector") or {}
    out.append("━━ ⑦ 업종 분포 ━━")
    dist = sec.get("dist") or {}
    if dist:
        top = sorted(dist.items(), key=lambda kv: -kv[1])[:6]
        out.append("• " + " · ".join(f"{_e(k)} {v}" for k, v in top))
        if sec.get("warn"):
            out.append(f"• ⚠️ 편중 경고: {_e(sec['warn'])} — 분산 배분 권고")
    else:
        out.append("• 업종 정보 없음")

    out.append("")
    out.append(f"⏰ {asof:%Y-%m-%d %H:%M:%S}")
    return "\n".join(out)


# ══════════════════════════ 뉴스 브리핑 ══════════════════════════
CATEGORY_ICON = {
    "공시": "📄", "수급": "💧", "실적": "📈", "매크로": "🌍", "정책": "🏛",
    "반도체": "🔲", "2차전지": "🔋", "방산조선": "🚢", "바이오": "🧬",
    "전력AI": "⚡", "해외시황": "🇺🇸", "국내시황": "🇰🇷",
    "해외개별주": "🏦", "기타": "•",
}


def _news_line(row, tick_map: dict, show_source: bool = True) -> str:
    """뉴스 1건 1~2줄. 클러스터 크기와 종목 태그를 붙인다."""
    icon = CATEGORY_ICON.get(row["category"], "•")
    n = int(row.get("cluster_n") or 1)
    multi = f" <i>({n}개 매체)</i>" if n >= 2 else ""
    title = _e(str(row["title"])[:110])
    line = f"{icon} <a href=\"{_e(row['url'] or '')}\">{title}</a>{multi}"
    tags = tick_map.get(row["id"]) or []
    sub = []
    if tags:
        sub.append("· " + " ".join(f"<b>{_e(t)}</b>" for t in tags[:4]))
    if show_source and row.get("source"):
        sub.append(f"({_e(row['source'])})")
    if row.get("summary"):
        sub.insert(0, "· " + _e(str(row["summary"])[:120]))
    return line + ("\n   " + " ".join(sub) if sub else "")


def _dedup_clusters(df, limit: int):
    """클러스터당 대표 1건만 남긴다. 같은 사건이 반복 노출되는 걸 막는다."""
    if df is None or df.empty:
        return []
    seen: set = set()
    out = []
    for _, r in df.iterrows():
        cid = r.get("cluster_id") or r["id"]
        if cid in seen:
            continue
        seen.add(cid)
        out.append(r)
        if len(out) >= limit:
            break
    return out


def _tick_map(store, rows) -> dict:
    ids = [r["id"] for r in rows]
    if not ids:
        return {}
    m = store.news_ticker_map(ids)
    out: dict = {}
    if m is None or m.empty:
        return out
    for nid, grp in m.groupby("news_id"):
        out[nid] = list(dict.fromkeys(grp["name"].tolist()))
    return out


def render_morning_brief(store, asof, hours: int = 16,
                         per_section: int = 5) -> str:
    """아침 브리핑 (개장 전). 밤사이 해외 + 매크로 + 내 종목 순."""
    df = store.news_since(hours=hours)
    if df is None or df.empty:
        return (f"☀️ <b>[아침 브리핑]</b> {asof:%m/%d}\n\n"
                "• 수집된 뉴스가 없습니다. 수집 배치를 확인하십시오.")

    overseas = df[df["region"].isin(["US", "GLOBAL"])]
    macro = df[df["category"].isin(["매크로", "정책"])]
    mine = df[df["importance"] >= 5.0]
    disclosure = df[df["category"] == "공시"]

    out = [f"☀️ <b>[아침 브리핑]</b> {asof:%Y-%m-%d %H:%M}",
           f"• 최근 {hours}시간 {len(df)}건 · "
           f"사건 {df['cluster_id'].nunique()}건", ""]

    sections = (
        ("🌏 밤사이 해외", overseas),
        ("🏛 매크로·정책", macro),
        ("🎯 내 종목·고중요도", mine),
        ("📄 주요 공시", disclosure),
    )
    for label, sub in sections:
        rows = _dedup_clusters(sub, per_section)
        if not rows:
            continue
        tm = _tick_map(store, rows)
        out.append(f"━━ {label} ({len(rows)}건) ━━")
        out += [_news_line(r, tm) for r in rows]
        out.append("")

    out.append("※ 뉴스에는 점수를 매기지 않습니다. 중요도는 '몇 개 매체가")
    out.append("  다뤘는가 + 내 종목인가'로만 정렬됩니다.")
    return "\n".join(out)


def render_evening_brief(store, asof, picks: list | None = None,
                         hours: int = 12, per_section: int = 6) -> str:
    """저녁 브리핑 (장마감 후). 국내 뉴스 정리 + 추천 10선 교차."""
    df = store.news_since(hours=hours, region="KR")
    out = [f"🌙 <b>[저녁 뉴스 정리]</b> {asof:%Y-%m-%d %H:%M}"]
    if df is None or df.empty:
        out.append("\n• 국내 뉴스 수집분이 없습니다.")
        return "\n".join(out)

    out.append(f"• 최근 {hours}시간 {len(df)}건 · "
               f"사건 {df['cluster_id'].nunique()}건")
    out.append("")

    disclosure = df[df["category"] == "공시"]
    flow = df[df["category"] == "수급"]
    theme = df[df["category"].isin(
        ["반도체", "2차전지", "방산조선", "바이오", "전력AI"])]
    market = df[df["category"].isin(["국내시황", "실적"])]

    for label, sub in (("📄 공시", disclosure), ("💧 수급", flow),
                       ("🏭 테마·업종", theme), ("📊 시황·실적", market)):
        rows = _dedup_clusters(sub, per_section)
        if not rows:
            continue
        tm = _tick_map(store, rows)
        out.append(f"━━ {label} ({len(rows)}건) ━━")
        out += [_news_line(r, tm) for r in rows]
        out.append("")

    # 추천 10선과 뉴스 교차 — 뉴스가 붙은 추천 종목을 표시한다
    if picks:
        codes = {r.ticker for _, r in picks}
        m = store.news_ticker_map(df["id"].tolist())
        hit: dict = {}
        if m is not None and not m.empty:
            for _, r in m[m["ticker"].isin(codes)].iterrows():
                hit.setdefault(r["ticker"], 0)
                hit[r["ticker"]] += 1
        out.append("━━ 🔗 추천 10선 뉴스 연동 ━━")
        if hit:
            for _, r in picks:
                n = hit.get(r.ticker)
                if n:
                    out.append(f"• <b>{_e(r.name)}</b> 관련 뉴스 {n}건 "
                               f"· 매집 {r.value_score:.1f}/추세 {r.trend_score:.1f}")
        else:
            out.append("• 추천 종목 관련 뉴스 없음 (조용한 바닥일 수 있음)")
        out.append("")

    out.append(f"⏰ {asof:%Y-%m-%d %H:%M:%S}")
    return "\n".join(out)


def render_news_weekly(shift, asof, top_n: int = 8) -> str:
    """주간 테마 부침. 개별 뉴스보다 이 흐름이 순환매 판단에 쓸모 있다."""
    out = [f"📰 <b>[주간 뉴스 테마 변화]</b> {asof:%m/%d}", ""]
    if shift is None or len(shift) == 0:
        out.append("• 비교할 누적 데이터가 부족합니다 (2주 이상 필요).")
        return "\n".join(out)

    rising = shift[shift["delta"] > 0].head(top_n)
    falling = shift[shift["delta"] < 0].tail(top_n).iloc[::-1]

    out.append("━━ 🔥 부상 테마 ━━")
    if len(rising):
        for cat, r in rising.iterrows():
            icon = CATEGORY_ICON.get(cat, "•")
            pct = "" if r["delta_pct"] != r["delta_pct"] else f" ({r['delta_pct']:+.0f}%)"
            out.append(f"{icon} {_e(cat)}: {int(r['last_week'])} → "
                       f"<b>{int(r['this_week'])}</b>건 "
                       f"({r['delta']:+d}){pct}")
    else:
        out.append("• 없음")
    out.append("")

    out.append("━━ 🧊 식는 테마 ━━")
    if len(falling):
        for cat, r in falling.iterrows():
            icon = CATEGORY_ICON.get(cat, "•")
            out.append(f"{icon} {_e(cat)}: {int(r['last_week'])} → "
                       f"{int(r['this_week'])}건 ({r['delta']:+d})")
    else:
        out.append("• 없음")
    out.append("")
    out.append("※ 건수는 '사건 수'(중복 매체 합산 전) 기준입니다.")
    out.append("※ 부상 테마의 급등 종목을 식는 테마로 스위칭하는 것이")
    out.append("  교리 규칙 01 대칭 순환매의 1차 후보입니다.")
    return "\n".join(out)


# ══════════════════════════ 청산 신호 ══════════════════════════
LAYER_ICON = {
    0: "🛑", 1: "❌", 2: "📉", 3: "🎯", 4: "🎯", 5: "💰", 6: "⏳", 7: "🔄",
}
LAYER_LABEL = {
    0: "무효화", 1: "손절", 2: "트레일링", 3: "목표3차",
    4: "목표2차", 5: "목표1차", 6: "시간", 7: "순환매",
}


def _dec_label(dec) -> str:
    """계층 이름. 계층 0 은 무효화와 보유기간 만료가 공유하므로 규칙을 본다."""
    return RULE_NAME.get(getattr(dec, "rule", ""),
                         LAYER_LABEL.get(dec.layer, str(dec.layer)))


def _dec_icon(dec) -> str:
    if getattr(dec, "rule", "") == "hold:expired":
        return "🗓"
    return LAYER_ICON.get(dec.layer, "•")


def render_exit_alert(dec, cfg: Config = DEFAULT) -> str:
    """청산 신호 1건. 무효화·손절은 즉시 나가야 하므로 단건 발송한다."""
    icon = _dec_icon(dec)
    label = _dec_label(dec)
    act = "전량" if dec.action == "EXIT_ALL" else f"{dec.ratio * 100:.0f}% 부분"
    head = "🚨🚨 " if dec.urgent else ""
    lines = [
        f"{head}{icon} <b>[청산 신호 · {label}]</b>",
        "",
        f"📌 종목: <b>{_e(dec.name)} ({dec.ticker})</b>",
        f"📌 조치: <b>{act} 청산</b> · {dec.qty:,}주",
        f"📌 기준 종가: {dec.signal_price:,.0f}원",
        f"📌 수익률: <b>{dec.ret_pct:+.2f}%</b> "
        f"(비용 차감 {dec.net_ret_pct:+.2f}%)",
        "",
        "━━ 판정 근거 ━━",
        f"• {_e(dec.reason)}",
        "",
        f"⚠️ {_e(dec.fill_note)} — 종가로 판정했으므로 실제 체결은 익일입니다.",
        f"   갭하락 시 이 가격보다 낮게 체결됩니다.",
    ]
    if dec.layer <= 1:
        lines.append("   손절·무효화는 시간창과 알림 예산을 무시하고 발송됩니다.")
    lines.append("")
    lines.append(f"체결 후: <code>--mode fill --log-id N "
                 f"--fill-price 실제체결가</code>")
    return "\n".join(lines)


def render_exit_digest(res: dict, asof, cfg: Config = DEFAULT) -> str:
    """청산 판정 요약. 신호가 없어도 보유 현황은 보고한다."""
    decs = res.get("decisions") or []
    n_pos = res.get("positions", 0)
    mret = res.get("market_ret")
    mtxt = "" if mret is None or mret != mret else f" · 시장 중위 {mret:+.2f}%"

    if not decs:
        return (f"🗂 <b>[청산 판정]</b> {res.get('trade_date') or ''}\n\n"
                f"• 보유 {n_pos}건{mtxt}\n"
                f"• 청산 조건 해당 없음. 보유 유지.")

    out = [f"🗂 <b>[청산 판정 {len(decs)}건]</b> {res.get('trade_date') or ''}",
           f"• 보유 {n_pos}건{mtxt}", ""]
    for d in decs:
        icon = _dec_icon(d)
        label = _dec_label(d)
        act = "전량" if d.action == "EXIT_ALL" else f"{d.ratio * 100:.0f}%"
        out.append(f"{icon} <b>{_e(d.name)}</b> [{label}] {act} {d.qty:,}주")
        out.append(f"   {d.signal_price:,.0f}원 · {d.ret_pct:+.2f}%")
        out.append(f"   {_e(d.reason[:90])}")
    out.append("")
    out.append("※ 우선순위: 무효화 > 보유만료 > 손절 > 트레일링 > 목표3차")
    out.append("  > 목표2차 > 목표1차 > 시간 > 순환매. 포지션당 1건만 집행합니다.")
    out.append(f"⏰ {asof:%Y-%m-%d %H:%M:%S}")
    return "\n".join(out)


def render_positions(positions: list, price_map: dict | None = None) -> str:
    """보유 현황."""
    if not positions:
        return "🗂 <b>[보유 현황]</b>\n\n• 보유 포지션 없음"
    out = [f"🗂 <b>[보유 현황 {len(positions)}건]</b>", ""]
    for p in positions:
        cur = None
        if price_map and p.ticker in price_map:
            df = price_map[p.ticker]
            if df is not None and len(df):
                cur = float(df["종가"].iloc[-1])
        ret = f"{(cur / p.entry_price - 1) * 100:+.2f}%" if cur else "-"
        track = {"VALUE": "매집", "TREND": "추세"}.get(p.track, p.track)
        out.append(f"#{p.id} <b>{_e(p.name)}</b> ({p.ticker}) [{track}]")
        out.append(f"   진입 {p.entry_date} @ {p.entry_price:,.0f}원 "
                   f"· 잔량 {p.remaining:,}/{p.qty:,}주 · {ret}")
        stops = []
        # 손절선은 진입 시 기록된 stop_price 다. 밴드 하단을 '손절' 로
        # 표시하면 실제 판정 기준과 다른 값을 보게 된다.
        if p.stop_price:
            stops.append(f"손절 {float(p.stop_price):,.0f}")
        if p.entry_band_lo:
            stops.append(f"밴드하단 {float(p.entry_band_lo):,.0f}")
        if p.entry_band_hi:
            stops.append(f"목표 {float(p.entry_band_hi):,.0f}")
        if p.entry_fib_0382:
            stops.append(f"0.382 {float(p.entry_fib_0382):,.0f}")
        if stops:
            out.append("   " + " · ".join(stops))
    out.append("")
    out.append("※ 청산선은 진입 시점 스냅샷으로 고정됩니다 (재계산 안 함).")
    return "\n".join(out)
