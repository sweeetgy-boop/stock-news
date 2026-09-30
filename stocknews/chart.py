# -*- coding: utf-8 -*-
"""추천 종목 차트 PNG + 텔레그램 캡션.

추천 10선 텍스트는 숫자만 준다. '밴드 중심까지 -12%' 는 읽어야 알지만
차트는 보면 안다. 그래서 발송하는 날 상위 등급 몇 종목만 그림을 붙인다.

그리는 것은 **채점에 실제로 쓴 값뿐이다.** 밴드·피보·P0·매물대를 차트에서
따로 계산하면 발송된 점수와 그림이 어긋나도 알 수 없다. 그래서
  - 밴드·피보·P0 는 ScreenResult 의 값을 그대로 쓰고,
  - 매물대는 P0(방법 C)와 같은 함수·같은 구간·같은 칸 수로 센다
    (`cost_basis.volume_profile`).

실패는 종목 단위로 격리한다. 차트는 부가 정보이고, 차트 한 장 때문에
텍스트 발송이 막히면 주객이 바뀐다.
"""
from __future__ import annotations

import html
import logging
import os
import re
import shutil
import warnings
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from .config import Config, DEFAULT
from .contracts import ScreenResult
from .cost_basis import POC_MIN_LOOKBACK, volume_profile
from .indicators import moving_averages
from .notify import TELEGRAM_CAPTION_MAX, caption_units, fit_caption, now_kst
from .renderer import bar
from .screener import HARD_EXCLUSION_FLAGS

log = logging.getLogger(__name__)

__all__ = ["GRADE_RANK", "grade_at_least", "ChartJob", "ChartOut",
           "jobs_from_picks", "jobs_from_rows", "select_targets",
           "korean_font", "render_chart", "chart_caption", "chart_path",
           "prune_chart_dirs", "build_charts", "line_levels", "album_items"]

# 등급 순서. 작을수록 높다. NONE 은 차트 대상이 아니므로 없다.
GRADE_RANK = {"S+": 0, "S": 1, "A": 2, "B": 3}

# 색. 캔들은 국내 관례(상승 빨강 · 하락 파랑)를 따른다. 나머지는
# dataviz 기본 팔레트의 범주 슬롯이고, 선마다 선 모양도 달리해
# 색만으로 구분하지 않는다(범례 + 우측 직접 라벨).
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK2 = "#52514e"
GRID = "#e4e3df"
UP = "#e34948"
DOWN = "#2a78d6"
MA_COLORS = ("#eb6834", "#1baf7a", "#4a3aa7")   # MA 단·중·장
BAND = "#d55181"
FIB = "#008300"
VP = "#9c9a92"

TRACK_KR = {"VALUE": "매집", "TREND": "추세", "BOTH": "시퀀스"}


def grade_at_least(grade: str | None, min_grade: str) -> bool:
    """grade 가 min_grade 이상인가. 모르는 등급(NONE 포함)은 False."""
    if min_grade not in GRADE_RANK:
        raise ValueError(f"CHART_MIN_GRADE 는 {list(GRADE_RANK)} 중 하나여야"
                         f" 합니다: {min_grade!r}")
    g = GRADE_RANK.get(str(grade or "").strip())
    return g is not None and g <= GRADE_RANK[min_grade]


# ══════════════════════════ 대상 선정 ══════════════════════════
@dataclass(frozen=True)
class ChartJob:
    """차트 1장의 주문. 추천 행 하나에 대응한다.

    result   : 방금 채점한 ScreenResult (스캔 경로). None 이면 DB 시세로
               재계산한다 (일요일 경로 — recos 에는 원본 객체가 없다).
    recorded : recos 에 기록된 (매집, 추세). 재계산 값과 다르면 캡션에 적는다.
    """

    rank: int
    ticker: str
    name: str
    grade: str
    slot: str = ""
    result: ScreenResult | None = None
    recorded: tuple[float, float] | None = None


@dataclass(frozen=True)
class ChartOut:
    rank: int
    ticker: str
    name: str
    path: Path | None
    caption: str = ""
    error: str | None = None


def jobs_from_picks(picks: list) -> list[ChartJob]:
    """[(slot, ScreenResult), ...] -> ChartJob. 순위는 1부터."""
    return [ChartJob(rank=i, ticker=r.ticker, name=r.name, grade=r.grade,
                     slot=str(slot), result=r)
            for i, (slot, r) in enumerate(picks, 1)]


def jobs_from_rows(rows) -> list[ChartJob]:
    """recos 행(DataFrame) -> ChartJob."""
    if rows is None or len(rows) == 0:
        return []
    out = []
    for _, r in rows.iterrows():
        out.append(ChartJob(
            rank=int(r["rank"]), ticker=str(r["ticker"]),
            name=str(r["name"] or r["ticker"]), grade=str(r["grade"]),
            slot=str(r["slot"] or ""),
            recorded=(float(r["value_score"]), float(r["trend_score"]))))
    return out


def select_targets(jobs: list[ChartJob], cfg: Config = DEFAULT) -> list[ChartJob]:
    """추천 순위 순으로 max_count 까지. min_grade 가 있으면 그 이상만.

    기본은 등급 필터 없음 · 10장 = 10선 전부 (config CHART_* 주석 참조).
    """
    c = cfg.chart
    ok = [j for j in jobs
          if c.min_grade is None or grade_at_least(j.grade, c.min_grade)]
    ok.sort(key=lambda j: j.rank)
    return ok[:max(0, int(c.max_count))]


# ══════════════════════════ 폰트 ══════════════════════════
_FONT: dict = {}


def korean_font(cfg: Config = DEFAULT) -> str | None:
    """한글 폰트 이름. 없으면 경고 한 번 남기고 None (기본 폰트로 그린다).

    matplotlib 기본 폰트(DejaVu Sans)에는 한글이 없어 □ 로 나온다. 에러가
    아니라 경고만 나고 PNG 는 멀쩡히 생기므로, 보기 전에는 모른다.
    """
    if "name" in _FONT:
        return _FONT["name"]
    from matplotlib import font_manager

    want = cfg.chart.font
    name = want if any(f.name == want for f in font_manager.fontManager.ttflist) else None
    if name is None:
        # 폰트 캐시가 시스템 폰트를 못 본 경우. 파일을 직접 등록한다.
        windir = os.environ.get("WINDIR") or os.environ.get("SystemRoot") or r"C:\Windows"
        p = Path(windir) / "Fonts" / "malgun.ttf"
        if p.is_file():
            try:
                font_manager.fontManager.addfont(str(p))
                name = font_manager.FontProperties(fname=str(p)).get_name()
            except Exception as exc:  # noqa: BLE001
                log.warning("맑은 고딕 등록 실패 (%s): %s", p, exc)
    if name is None:
        log.warning("한글 폰트 '%s' 없음 — 차트의 한글이 □ 로 깨집니다. "
                    "맑은 고딕(malgun.ttf)을 설치하십시오.", want)
    _FONT["name"] = name
    return name


# ══════════════════════════ 차트 ══════════════════════════
def line_levels(res: ScreenResult, cfg: Config = DEFAULT) -> list[dict]:
    """차트에 긋는 수평선. 채점 결과 값을 그대로 옮긴다 (재계산 없음)."""
    out: list[dict] = []
    q, f = res.liq, res.fib
    if q is not None:
        out += [
            {"kind": "band", "key": "hi", "price": q.band_hi, "label": "-16% 마진콜"},
            {"kind": "band", "key": "mid", "price": q.band_mid, "label": "-30% 청산중심"},
            {"kind": "band", "key": "lo", "price": q.band_lo, "label": "-44% 연쇄청산"},
            {"kind": "p0", "key": "p0", "price": q.cost_basis,
             "label": f"P0 평균단가({q.basis_method})"},
        ]
    if f is not None:
        for k in cfg.chart.fib_levels:
            if k in f.levels:
                out.append({"kind": "fib", "key": k, "price": f.levels[k],
                            "label": f"피보 {k:.3f}"})
    return [x for x in out if x["price"] is not None and np.isfinite(x["price"])
            and x["price"] > 0]


def _spread(ys: list[float], gap: float) -> list[float]:
    """라벨 y 를 최소 간격 gap 으로 벌린다 (아래에서 위로 한 번, 중심 보정)."""
    if not ys:
        return []
    order = sorted(range(len(ys)), key=lambda i: ys[i])
    placed = [ys[i] for i in order]
    for i in range(1, len(placed)):
        placed[i] = max(placed[i], placed[i - 1] + gap)
    # 위로만 밀면 무리가 통째로 올라간다. 평균 이동만큼 되돌린다.
    shift = (sum(placed) - sum(ys[i] for i in order)) / len(placed)
    placed = [p - shift for p in placed]
    out = [0.0] * len(ys)
    for j, i in enumerate(order):
        out[i] = placed[j]
    return out


def render_chart(res: ScreenResult, ohlcv: pd.DataFrame, path,
                 cfg: Config = DEFAULT, trade_date: str | None = None) -> Path:
    """최근 N봉 캔들 + MA + 청산밴드 + 피보 + P0 + 매물대(상위 3) PNG."""
    from matplotlib import rc_context
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure
    from matplotlib.ticker import FuncFormatter

    c = cfg.chart
    if ohlcv is None or len(ohlcv) < 30:
        raise ValueError(f"시세 부족 ({0 if ohlcv is None else len(ohlcv)}봉)")
    df = ohlcv.tail(c.bars)
    n = len(df)
    x = np.arange(n)
    o = df["시가"].to_numpy(float)
    h = df["고가"].to_numpy(float)
    lo = df["저가"].to_numpy(float)
    cl = df["종가"].to_numpy(float)
    vol = df["거래량"].to_numpy(float)
    up = cl >= o
    colors = np.where(up, UP, DOWN)
    mas = moving_averages(ohlcv["종가"].astype("float64"), cfg.ma).loc[df.index]

    levels = line_levels(res, cfg)
    pmin, pmax = float(np.nanmin(lo)), float(np.nanmax(h))
    span = max(pmax - pmin, pmax * 0.02)
    reach_lo = pmin - max(0.5 * span, 0.10 * pmin)
    reach_hi = pmax + max(0.5 * span, 0.10 * pmax)
    shown = [lv for lv in levels if reach_lo <= lv["price"] <= reach_hi]
    hidden = [lv for lv in levels if lv not in shown]
    ymin = min([pmin] + [lv["price"] for lv in shown])
    ymax = max([pmax] + [lv["price"] for lv in shown])
    pad = (ymax - ymin) * 0.04
    ylim = (ymin - pad, ymax + pad)

    gutter = max(14, int(n * 0.17))   # 우측 라벨 자리 (빈 봉)
    font = korean_font(cfg)
    rc = {"axes.unicode_minus": False, "font.size": 9,
          "axes.edgecolor": GRID, "axes.labelcolor": INK2,
          "xtick.color": INK2, "ytick.color": INK2}
    if font:
        rc["font.family"] = font

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with rc_context(rc), warnings.catch_warnings():
        # 폰트가 없으면 글리프마다 경고가 난다. 위에서 한 번 알렸다.
        warnings.filterwarnings("ignore", message=r"Glyph .* missing")
        fig = Figure(figsize=(12, 7.4), dpi=c.dpi, facecolor=SURFACE)
        FigureCanvasAgg(fig)
        gs = fig.add_gridspec(2, 2, height_ratios=[4, 1], width_ratios=[6.2, 1],
                              hspace=0.04, wspace=0.015,
                              left=0.075, right=0.985, top=0.855, bottom=0.07)
        ax = fig.add_subplot(gs[0, 0])
        axv = fig.add_subplot(gs[1, 0], sharex=ax)
        axp = fig.add_subplot(gs[0, 1], sharey=ax)
        fig.add_subplot(gs[1, 1]).axis("off")
        for a in (ax, axv, axp):
            a.set_facecolor(SURFACE)
            for s in a.spines.values():
                s.set_color(GRID)
            a.grid(True, color=GRID, linewidth=0.6)
            a.set_axisbelow(True)

        # ── 캔들 ──
        body = np.abs(cl - o)
        body = np.maximum(body, (ylim[1] - ylim[0]) * 0.0015)   # 도지도 보이게
        ax.vlines(x, lo, h, colors=colors, linewidth=0.8, zorder=2)
        ax.bar(x, body, bottom=np.minimum(o, cl), width=0.62, color=colors,
               linewidth=0, zorder=3)

        # ── 이평선 ──
        for col, color in zip(mas.columns, MA_COLORS):
            ax.plot(x, mas[col].to_numpy(float), color=color, linewidth=1.4,
                    label=col.upper(), zorder=4)

        # ── 청산 밴드 영역 ──
        band = {lv["key"]: lv["price"] for lv in levels if lv["kind"] == "band"}
        if "hi" in band and "lo" in band:
            ax.fill_between([-0.5, n - 0.5], band["lo"], band["hi"],
                            color=BAND, alpha=0.07, linewidth=0, zorder=1)

        style = {
            "band": {"hi": ("--", 1.1), "mid": ("-", 1.7), "lo": ("--", 1.1)},
            "fib": ("dotted", 1.3),
            "p0": ("-.", 1.6),
        }
        right = n + 0.8
        lab_ys = _spread([lv["price"] for lv in shown] + [cl[-1]],
                         (ylim[1] - ylim[0]) * 0.036)
        for lv, ly in zip(shown, lab_ys):
            if lv["kind"] == "band":
                ls, lw = style["band"][lv["key"]]
                color = BAND
            elif lv["kind"] == "fib":
                (ls, lw), color = style["fib"], FIB
            else:
                (ls, lw), color = style["p0"], INK
            ax.hlines(lv["price"], -0.5, n - 0.5, colors=color, linestyles=ls,
                      linewidth=lw, zorder=5)
            # 라벨이 밀려났으면 선 끝에서 라벨까지 가는 짧은 연결선
            ax.plot([n - 0.5, right - 0.3], [lv["price"], ly], color=color,
                    linewidth=0.8, zorder=5)
            ax.text(right, ly, f"{lv['label']} {lv['price']:,.0f}",
                    va="center", ha="left", fontsize=8, color=INK2, zorder=6)
        # 종가 라벨
        ax.plot([n - 1, right - 0.3], [cl[-1], lab_ys[-1]], color=INK,
                linewidth=0.8, zorder=5)
        ax.text(right, lab_ys[-1], f"종가 {cl[-1]:,.0f}", va="center", ha="left",
                fontsize=8.5, color=INK, fontweight="bold", zorder=6)

        if hidden:
            above = [lv for lv in hidden if lv["price"] > ylim[1]]
            below = [lv for lv in hidden if lv["price"] < ylim[0]]
            note = []
            if above:
                note.append("범위 위 ↑ " + " · ".join(
                    f"{lv['label']} {lv['price']:,.0f}" for lv in above))
            if below:
                note.append("범위 아래 ↓ " + " · ".join(
                    f"{lv['label']} {lv['price']:,.0f}" for lv in below))
            ax.text(0.005, 0.985, "\n".join(note), transform=ax.transAxes,
                    va="top", ha="left", fontsize=8, color=INK2, zorder=7,
                    bbox={"facecolor": SURFACE, "edgecolor": GRID,
                          "boxstyle": "round,pad=0.3", "alpha": 0.9})

        ax.set_xlim(-1, n + gutter)
        ax.set_ylim(*ylim)
        ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _p: f"{v:,.0f}"))
        ax.tick_params(labelbottom=False)

        # ── 거래량 ──
        axv.bar(x, vol, width=0.62, color=colors, alpha=0.6, linewidth=0)
        axv.set_ylabel("거래량")
        axv.yaxis.set_major_formatter(
            FuncFormatter(lambda v, _p: f"{v / 1e4:,.0f}만" if v else "0"))
        axv.locator_params(axis="y", nbins=3)
        ticks = sorted(set(np.linspace(0, n - 1, 7).astype(int).tolist()))
        axv.set_xticks(ticks)
        axv.set_xticklabels([df.index[i].strftime("%y/%m/%d" if k == 0 else "%m/%d")
                             for k, i in enumerate(ticks)])

        # ── 매물대 (상위 구간만) ──
        vp = volume_profile(ohlcv, max(cfg.credit.lookback, POC_MIN_LOOKBACK),
                            c.vp_bins)
        axp.tick_params(labelleft=False, labelbottom=False, length=0)
        axp.set_title(f"매물대 상위 {c.vp_top}", fontsize=9, color=INK2)
        if vp is not None:
            edges, agg = vp
            share = agg / agg.sum() * 100.0
            top = np.argsort(agg)[::-1][:c.vp_top]
            centers = (edges[:-1] + edges[1:]) / 2.0
            width = float(edges[1] - edges[0])
            axp.barh(centers[top], share[top], height=width * 0.9, color=VP,
                     linewidth=0)
            # 상위 구간끼리 붙어 있으면 라벨이 겹친다. 막대는 제자리, 라벨만 벌린다.
            ys = _spread([float(centers[k]) for k in top],
                         (ylim[1] - ylim[0]) * 0.04)
            for rank_i, (k, ly) in enumerate(zip(top, ys)):
                tag = " POC" if rank_i == 0 else ""
                axp.text(share[k], ly, f" {share[k]:.1f}%{tag}",
                         va="center", ha="left", fontsize=8, color=INK)
            axp.set_xlim(0, float(share[top].max()) * 1.9)
        else:
            axp.text(0.5, 0.5, "계산 불가", transform=axp.transAxes,
                     ha="center", fontsize=8, color=INK2)

        # ── 제목 · 범례 ──
        fig.text(0.075, 0.955,
                 f"{res.name} ({res.ticker})   {res.grade} 등급   "
                 f"매집 {res.value_score:.2f} · 추세 {res.trend_score:.2f}",
                 fontsize=14, fontweight="bold", color=INK, ha="left")
        sub = [f"기준일 {trade_date or df.index[-1].strftime('%Y-%m-%d')}",
               f"종가 {res.price:,.0f}원", f"트랙 {TRACK_KR.get(res.track, res.track)}",
               f"최근 {n}봉"]
        if res.fib is not None:
            sub.append(f"피보 진행률 {res.fib.ratio:.3f}")
        if res.liq is not None and np.isfinite(res.liq.band_pos):
            sub.append(f"밴드 위치 {res.liq.band_pos * 100:.0f}%")
        fig.text(0.075, 0.918, " · ".join(sub), fontsize=9.5, color=INK2, ha="left")

        from matplotlib.lines import Line2D
        handles = [Line2D([], [], color=cc, lw=1.4, label=f"MA{m}")
                   for cc, m in zip(MA_COLORS, (cfg.ma.short, cfg.ma.mid, cfg.ma.long))]
        handles += [Line2D([], [], color=BAND, lw=1.4, ls="--", label="청산밴드 -16/-30/-44%"),
                    Line2D([], [], color=FIB, lw=1.3, ls="dotted", label="피보 되돌림"),
                    Line2D([], [], color=INK, lw=1.4, ls="-.", label="P0 신용 평균단가(추정)")]
        fig.legend(handles=handles, loc="upper left", bbox_to_anchor=(0.07, 0.905),
                   ncol=6, frameon=False, fontsize=8.5, handlelength=2.2,
                   columnspacing=1.4, labelcolor=INK2)

        fig.savefig(path, facecolor=SURFACE)
    return path


# ══════════════════════════ 캡션 ══════════════════════════
def _e(s) -> str:
    return html.escape(str(s), quote=False)


def active_flags(flags: dict | None) -> list[str]:
    """켜져 있는 하드 배제 플래그 이름."""
    flags = flags or {}
    return [key for key, _label in HARD_EXCLUSION_FLAGS if flags.get(key)]


def chart_caption(res: ScreenResult, rank: int | None = None,
                  flags: dict | None = None, exclusion: str | None = None,
                  recorded: tuple[float, float] | None = None,
                  trade_date: str | None = None, cfg: Config = DEFAULT,
                  limit: int = TELEGRAM_CAPTION_MAX) -> str:
    """사진 1장의 캡션: 등급 · 점수 · 밴드 위치 · 피보 위치.

    앨범에서 사진을 넘길 때마다 보이는 글이라 4줄로 줄였다. 세부 구성요소
    (LPS 항목별 점수·P0·추세)는 차트 제목과 선이 대신한다. 배제 플래그와
    '기록과 다른 재계산'은 있을 때만 경고로 붙인다 — 없을 때 '없음'을 매
    장마다 쓰면 10장이 같은 줄로 채워진다.
    """
    head = f"{rank}. " if rank is not None else ""
    lines = [
        f"📈 <b>{head}{_e(res.name)}</b> ({res.ticker}) [{res.grade}] {res.mark}".rstrip(),
        f"매집 {res.value_score:.2f} · 추세 {res.trend_score:.2f} · "
        f"{TRACK_KR.get(res.track, res.track)} · 종가 {res.price:,.0f}원"
        + (f" ({trade_date})" if trade_date else ""),
    ]

    # 경고는 앞에 둔다. 캡션이 잘려도 남아야 한다.
    on = active_flags(flags)
    if exclusion and exclusion not in on:
        on.append(exclusion)
    if on:
        lines.append("⚠️ 배제 플래그: " + ", ".join(_e(x) for x in on))
    if recorded is not None:
        rv, rt = recorded
        if abs(rv - res.value_score) > 0.005 or abs(rt - res.trend_score) > 0.005:
            lines.append(f"※ 기록 점수 매집 {rv:.2f} · 추세 {rt:.2f} — 재계산과 다름")

    q = res.liq
    if q is not None and np.isfinite(q.band_pos) and q.band_mid > 0:
        gap = (res.price / q.band_mid - 1.0) * 100.0
        lines.append(f"밴드 위치 {q.band_pos * 100:.0f}% {bar(q.band_pos)} · "
                     f"청산중심 {q.band_mid:,.0f} ({gap:+.1f}%)")
    else:
        lines.append("밴드 위치 - (평균단가 추정 불가)")

    f = res.fib
    if f is not None:
        tgt = cfg.fib.target
        lines.append(
            f"피보 진행률 {f.ratio:.3f} · 구간 {_e(f.zone)} · {tgt:.3f} "
            f"{'이하✅' if f.below_target else '미도달'}"
            + (" · ⚠️ 파동 붕괴" if f.wave_broken else ""))
    else:
        lines.append("피보 - (데이터 부족)")

    text, _mode = fit_caption("\n".join(lines), limit)
    return text


def album_items(outs: list, cfg: Config = DEFAULT,
                limit: int = TELEGRAM_CAPTION_MAX) -> list[tuple[Path, str]]:
    """생성된 차트 -> 앨범 항목 [(경로, 캡션)]. 순위 순.

    첫 사진 캡션 맨 끝에 면책 문구를 붙인다. 본문을 (상한 - 문구 길이)로
    먼저 줄이고 붙이므로, 합쳐도 1,024자를 넘지 않고 문구는 잘리지 않는다.
    """
    ready = sorted((o for o in outs if o.path is not None), key=lambda o: o.rank)
    items = [(o.path, o.caption) for o in ready]
    if items and cfg.chart.disclaimer:
        tail = f"\n\n<i>{_e(cfg.chart.disclaimer)}</i>"
        body, _m = fit_caption(items[0][1], limit - caption_units(tail))
        items[0] = (items[0][0], body + tail)
    return items


# ══════════════════════════ 파일 ══════════════════════════
def chart_path(trade_date, rank: int, ticker: str, cfg: Config = DEFAULT,
               root=None) -> Path:
    """data/charts/YYYYMMDD/NN_티커.png — 폴더 날짜는 기준 거래일."""
    ymd = pd.Timestamp(str(trade_date)).strftime("%Y%m%d")
    return Path(root or cfg.chart.out_dir) / ymd / f"{int(rank):02d}_{ticker}.png"


_YMD = re.compile(r"^\d{8}$")


def prune_chart_dirs(cfg: Config = DEFAULT, root=None,
                     today: date | None = None) -> list[str]:
    """이름이 YYYYMMDD 인 폴더 중 keep_days 보다 오래된 것을 지운다.

    이름이 날짜가 아닌 것은 건드리지 않는다 — 사람이 따로 둔 파일일 수 있다.
    정리 실패는 경고만 남긴다. 디스크 정리 때문에 발송이 멈추면 안 된다.
    """
    base = Path(root or cfg.chart.out_dir)
    if not base.is_dir():
        return []
    today = today or now_kst().date()
    cutoff = today - timedelta(days=int(cfg.chart.keep_days))
    removed = []
    for child in sorted(base.iterdir()):
        if not child.is_dir() or not _YMD.match(child.name):
            continue
        try:
            d = datetime.strptime(child.name, "%Y%m%d").date()
        except ValueError:
            continue
        if d < cutoff:
            try:
                shutil.rmtree(child)
                removed.append(child.name)
            except OSError as exc:
                log.warning("차트 폴더 정리 실패 %s: %s", child, exc)
    if removed:
        log.info("차트 폴더 정리 %d개 (%s 이전): %s", len(removed),
                 cutoff, ", ".join(removed))
    return removed


def build_charts(store, jobs: list[ChartJob], trade_date: str,
                 cfg: Config = DEFAULT, root=None) -> list[ChartOut]:
    """jobs 마다 PNG + 캡션. 한 종목의 실패는 그 종목의 ChartOut.error 로만 남는다."""
    from .daily import exclusion_of, scan_context, screen_ticker

    out: list[ChartOut] = []
    ctx = None
    for job in jobs:
        try:
            if ctx is None:
                ctx = scan_context(store, quiet=True)
            res, ohlcv = screen_ticker(store, job.ticker, job.name, ctx, cfg,
                                       until=trade_date, apply_exclusion=False)
            if res is None:
                raise ValueError(f"시세 부족 ({0 if ohlcv is None else len(ohlcv)}봉)")
            if job.result is not None and not job.result.excluded:
                res = job.result          # 방금 채점한 그 객체를 그린다
            path = render_chart(res, ohlcv,
                                chart_path(trade_date, job.rank, job.ticker, cfg, root),
                                cfg, trade_date=trade_date)
            cap = chart_caption(res, rank=job.rank,
                                flags=ctx["flags"].get(job.ticker),
                                exclusion=exclusion_of(job.ticker, ohlcv, ctx),
                                recorded=job.recorded, trade_date=trade_date,
                                cfg=cfg)
            out.append(ChartOut(job.rank, job.ticker, job.name, path, cap))
            log.info("차트 생성 %d. %s(%s) -> %s", job.rank, job.name,
                     job.ticker, path)
        except Exception as exc:  # noqa: BLE001 - 종목 단위 격리
            msg = f"{type(exc).__name__}: {exc}"
            log.warning("차트 실패 %d. %s(%s) — 건너뜀: %s", job.rank, job.name,
                        job.ticker, msg)
            out.append(ChartOut(job.rank, job.ticker, job.name, None, error=msg))
    return out
