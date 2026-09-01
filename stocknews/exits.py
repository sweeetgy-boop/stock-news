# -*- coding: utf-8 -*-
"""청산 규칙 엔진.

핵심 원칙: 청산은 진입 근거를 거울처럼 따라간다
----------------------------------------------
진입이 두 트랙이면 청산도 두 갈래여야 한다. 하나의 청산 룰을 두 트랙에
공통 적용하는 것이 가장 흔하고 비싼 실수다.

  트랙 V (매집·역추세) 진입 근거 = "신용 청산으로 과하게 밀렸다"
      -> 그 왜곡이 해소되면 나온다. 목표가 고정 %가 아니라 '밴드 복귀'다.
  트랙 T (확증·순추세) 진입 근거 = "추세가 전환됐다"
      -> 추세가 살아있는 동안 들고 간다. +15%로 끊으면 큰 파동을 놓친다.
         트레일링이어야 한다.

우선순위 (작을수록 우선). 같은 날 여러 개가 걸리면 하나만 집행한다.
  0 무효화 · 보유기간 만료 / 1 손절 / 2 트레일링 / 3 목표3차 / 4 목표2차
  5 목표1차 / 6 시간 / 7 순환매

보유기간 규칙과 기존 8계층의 관계 (명시적 정의)
-----------------------------------------------
계층 0 은 두 규칙이 공유하고, 그 안의 순서는 고정이다.

  0-a  무효화 (invalidation:*)   관리종목·자본잠식 등. 손익 무관 즉시 전량
  0-b  보유기간 만료 (hold:expired)  MAX_HOLD_DAYS 도달 → 재평가

무효화를 먼저 보는 이유: 둘 다 전량 청산이지만 무효화는 '지금 나가라'이고
만료는 '유효기간이 끝났으니 다시 판단하라'다. 상장폐지 위험 종목에
'재평가' 라고 알리면 긴급성이 사라진다.

만료가 손절(계층 1)보다 앞서는 이유: 두 신호의 집행 내용이 같다(전량).
따라서 순서가 손익을 바꾸지 않는다. 대신 만료 메시지에 그 시점의
수익률과 손절선 접촉 여부를 함께 담아, 만료가 손절 상황을 가리지 않게 한다.

MIN_HOLD_DAYS 게이트는 계층 2~7 에만 걸린다.

  계층 0, 1   억제하지 않음. 무효화와 손절을 며칠간 막는 것은 최소보유일
              규칙이 안전장치를 무력화하는 것이다.
  계층 2~7    억제. 진입 직후의 흔들림에 반응해 나가면 그물을 던진
              의미가 없다. 트레일링·목표·시간·순환매가 전부 여기다.

보유 일수는 **거래일 기준**이다(`bars_held`). 진입일 봉을 1일로 센다.

진입 시 손절 고정
----------------
`positions.stop_price` 는 진입 시 `entry_price × (1 - STOP_LOSS_PCT/100)`
으로 확정 기록된다. 계층 1 의 가격 기준 손절은 **그 기록된 값만** 본다.
예전에는 판정 시점의 ATR 로 손절폭을 다시 계산했는데, 그러면 변동성이
커질 때 손절선이 함께 내려가 손절이 발동하지 않는다.

수정 API 는 없다. 손절선을 옮길 수 있으면 손절은 규칙이 아니라 기분이 된다.

상태 관리 원칙
-------------
peak_close, 연속 이탈 일수는 DB 카운터를 증감시키지 않고 **매 실행 시
시세에서 다시 계산한다.** 배치가 하루 빠지거나 두 번 돌아도 결과가
같아야 하기 때문이다(멱등성).

반대로 진입 시점의 밴드/피보 레벨은 절대 재계산하지 않는다. 매일 P0 를
다시 구하면 주가 하락에 맞춰 손절선도 내려가 손절이 영원히 발동하지 않는다.
"""
from __future__ import annotations

import logging
from datetime import timedelta

import numpy as np
import pandas as pd

from .config import Config, DEFAULT
from .contracts import DONE_TAKE1, DONE_TAKE2, ExitDecision, Position
from .indicators import cross_series, moving_averages
from .store import _now_kst

log = logging.getLogger(__name__)

__all__ = ["atr", "bars_held", "stop_price_for", "derive_state",
           "market_return_pct", "evaluate_position", "evaluate_rotation",
           "run_exits"]


# 계층 0. 진입 전제가 붕괴한 경우. 손익과 무관하게 전량 청산한다.
# "-30% 청산선에서 사면 절대 실패하지 않는다"는 믿음이 깨지는 유일한
# 경로가 여기다. 밴드를 뚫고 영구히 내려간 종목은 예외 없이 이 목록에 있었다.
INVALIDATION = (
    ("관리종목", "관리종목 지정"),
    ("투자주의환기", "투자주의환기종목 지정"),
    ("감사의견거절", "감사의견 거절·한정"),
    ("자본잠식", "자본잠식률 50% 돌파"),
    ("대규모증자", "대규모 유상증자·CB 공시 (지분 희석)"),
    ("거래정지이력", "거래정지"),
)


# ══════════════════════════ 보조 계산 ══════════════════════════
def atr(ohlcv: pd.DataFrame, window: int = 14) -> float:
    """Average True Range. 밴드 밖 진입 종목의 손절 여유를 정한다."""
    if len(ohlcv) < window + 2:
        return float("nan")
    h = ohlcv["고가"].astype("float64")
    l = ohlcv["저가"].astype("float64")
    c = ohlcv["종가"].astype("float64")
    prev = c.shift(1)
    tr = pd.concat([h - l, (h - prev).abs(), (l - prev).abs()], axis=1).max(axis=1)
    v = tr.rolling(window).mean().iloc[-1]
    return float(v) if pd.notna(v) else float("nan")


def stop_price_for(entry_price: float, cfg: Config = DEFAULT) -> float:
    """진입 시 기록할 손절선. 진입가 대비 `STOP_LOSS_PCT` 아래.

    포지션을 만드는 모든 경로가 이 함수를 써야 한다. 호출부마다 따로
    계산하면 언젠가 한 곳이 달라지고, 그때 어느 쪽이 맞는지 알 수 없다.
    """
    return float(entry_price) * (1.0 - cfg.exit.stop_loss_pct / 100.0)


def bars_held(pos: Position, ohlcv: pd.DataFrame) -> int:
    """진입일 이후 경과 거래일. 진입일 봉을 1일로 센다.

    달력일이 아니라 봉 개수로 세는 것이 핵심이다. 달력일로 세면 주말과
    연휴에 카운터가 앞서가서, 실제로는 3거래일밖에 안 지난 포지션이
    최소보유일을 통과하거나 만료 처리된다.
    """
    if ohlcv is None or len(ohlcv) == 0:
        return 0
    seg = ohlcv[ohlcv.index >= pd.Timestamp(pos.entry_date)]
    return int(len(seg)) if len(seg) else 1


def _tail_streak(mask: pd.Series) -> int:
    """시계열 끝에서 연속으로 True 인 개수."""
    if mask is None or len(mask) == 0:
        return 0
    n = 0
    for v in mask.to_numpy()[::-1]:
        if bool(v):
            n += 1
        else:
            break
    return n


def derive_state(pos: Position, ohlcv: pd.DataFrame,
                 cfg: Config = DEFAULT) -> dict:
    """진입 이후 구간에서 파생 상태를 재계산한다 (멱등)."""
    entry = pd.Timestamp(pos.entry_date)
    seg = ohlcv[ohlcv.index >= entry]
    if len(seg) == 0:                    # 진입 당일 봉이 아직 없음
        seg = ohlcv.tail(1)

    close = seg["종가"].astype("float64")
    peak = float(close.max())
    # 진입가도 후보에 넣는다. 진입 직후 하락만 있었다면 진입가가 최고점이다.
    peak = max(peak, float(pos.entry_price))

    band_streak = 0
    if pos.entry_band_lo:
        band_streak = _tail_streak(close < float(pos.entry_band_lo))

    ma_streak = 0
    mas = moving_averages(ohlcv["종가"].astype("float64"), cfg.ma)
    ma_long_col = f"ma{cfg.ma.long}"
    ma_long_now = mas[ma_long_col].iloc[-1]
    if pd.notna(ma_long_now):
        aligned = (ohlcv["종가"].astype("float64") < mas[ma_long_col])
        ma_streak = _tail_streak(aligned[aligned.index >= entry])

    ma_short = mas[f"ma{cfg.exit.trail_ma}"].iloc[-1] \
        if f"ma{cfg.exit.trail_ma}" in mas.columns else np.nan
    if pd.isna(ma_short):
        ma_short = ohlcv["종가"].astype("float64").rolling(
            cfg.exit.trail_ma).mean().iloc[-1]

    return {
        "peak_close": peak,
        "band_break_streak": int(band_streak),
        "ma_break_streak": int(ma_streak),
        "bars_held": bars_held(pos, ohlcv),
        "ma_long": float(ma_long_now) if pd.notna(ma_long_now) else float("nan"),
        "ma_trail": float(ma_short) if pd.notna(ma_short) else float("nan"),
        "defer_until": pos.defer_until,
    }


def market_return_pct(store, days: int = 1) -> float:
    """전종목 종가 등락률의 중위수. 지수 대신 쓰는 시장 프록시.

    별도 지수 데이터를 받지 않고 로컬 시세만으로 계산한다.
    """
    pm = store.price_matrix(days=days + 3)
    if pm is None or pm.empty or len(pm) < days + 1:
        return float("nan")
    a, b = pm.iloc[-(days + 1)], pm.iloc[-1]
    mask = a.notna() & b.notna() & (a > 0)
    if not mask.any():
        return float("nan")
    return float(((b[mask] / a[mask]) - 1.0).median() * 100.0)


def _qty_for(remaining: int, ratio: float, min_lot: int) -> tuple[int, bool]:
    """청산 수량. (수량, 최소단위미만_전량전환) 반환.

    1주 포지션에서 50% 청산은 불가능하다. 그 경우 사람이 하듯 전량으로
    올리고 그 사실을 신호에 표시한다.
    """
    if ratio >= 1.0:
        return remaining, False
    q = int(remaining * ratio)
    q = (q // max(min_lot, 1)) * max(min_lot, 1)
    if q <= 0:
        return remaining, True
    return min(q, remaining), False


def _decide(pos: Position, layer: int, rule: str, ratio: float,
            price: float, ret: float, reason: str, cfg: Config,
            urgent: bool, detail: dict | None = None) -> ExitDecision:
    qty, promoted = _qty_for(pos.remaining, ratio, cfg.exit.min_lot)
    eff_ratio = 1.0 if promoted else ratio
    if promoted:
        reason += " (최소 주문단위 미만이라 전량으로 전환)"
    return ExitDecision(
        ticker=pos.ticker, name=pos.name, position_id=pos.id,
        layer=layer, rule=rule,
        action="EXIT_ALL" if eff_ratio >= 1.0 else "TRIM",
        ratio=eff_ratio, qty=qty, signal_price=price,
        ret_pct=round(ret, 2),
        net_ret_pct=round(ret - cfg.exit.roundtrip_cost_pct, 2),
        reason=reason, urgent=urgent, detail=detail or {},
    )


# ══════════════════════════ 개별 포지션 판정 ══════════════════════════
def evaluate_position(pos: Position, ohlcv: pd.DataFrame,
                      flags: dict | None = None,
                      cfg: Config = DEFAULT,
                      market_ret: float | None = None,
                      credit_ratio_now: float | None = None
                      ) -> tuple[ExitDecision | None, dict]:
    """포지션 1건 판정. (결정 또는 None, 갱신할 상태) 반환."""
    if ohlcv is None or len(ohlcv) < cfg.ma.long + 5:
        return None, {}

    ec = cfg.exit
    st = derive_state(pos, ohlcv, cfg)
    close = ohlcv["종가"].astype("float64")
    price = float(close.iloc[-1])
    ret = (price / pos.entry_price - 1.0) * 100.0
    today = _now_kst().strftime("%Y-%m-%d")

    def mk(layer, rule, ratio, reason, urgent=False, detail=None):
        return _decide(pos, layer, rule, ratio, price, ret, reason, cfg,
                       urgent, detail)

    held = int(st["bars_held"])
    # 기록된 손절선 접촉 여부. 만료 메시지에도 실어야 하므로 먼저 구한다.
    stop_level = float(pos.stop_price) if pos.stop_price is not None else None
    stop_hit = stop_level is not None and price <= stop_level
    if stop_level is None:
        # 조용히 넘어가면 그 포지션만 손절 없이 운용된다. open_position 이
        # 필수로 받으므로 정상 경로에서는 나올 수 없는 상태다.
        log.warning("%s(#%s) stop_price 가 없다 — 계층 1 고정 손절 미적용",
                    pos.ticker, pos.id)

    # ── 계층 0-a. 무효화 ──
    f = (flags or {}).get(pos.ticker) or {}
    for key, label in INVALIDATION:
        if f.get(key):
            extra = ""
            if key == "자본잠식" and f.get("자본잠식률") is not None:
                extra = f" (잠식률 {float(f['자본잠식률']):.1f}%)"
            return mk(0, f"invalidation:{key}", 1.0,
                      f"진입 전제 붕괴 — {label}{extra}. 손익 무관 전량 청산",
                      urgent=True, detail={"flag": key}), st

    # ── 계층 0-b. 보유기간 만료 ──
    # 어떤 규칙도 걸리지 않아 무한 보유되는 경로를 막는다. 집행 내용이
    # 손절과 같으므로(전량) 순서가 손익을 바꾸지 않는다. 대신 손절선
    # 접촉 여부를 함께 실어 만료가 손절 상황을 가리지 않게 한다.
    if held >= ec.max_hold_days:
        tail = f" · 손절선 {stop_level:,.0f}원 " + ("이탈" if stop_hit else "미접촉") \
            if stop_level is not None else ""
        return mk(0, "hold:expired", 1.0,
                  f"보유기간 만료 — 재평가 ({held}/{ec.max_hold_days}거래일, "
                  f"수익률 {ret:+.2f}%{tail}). 진입 근거의 유효기간이 "
                  f"끝났다. 계속 들고 갈 이유를 새로 찾지 못하면 나온다",
                  urgent=False,
                  detail={"bars_held": held, "max_hold_days": ec.max_hold_days,
                          "stop_price": stop_level, "stop_hit": stop_hit}), st

    # ── 계층 1. 손절 ──
    stop: ExitDecision | None = None
    # 1-a. 기록된 손절선. 전 트랙 공통이고 가장 먼저 본다.
    #      판정 시점에 다시 계산하지 않는다 — 그게 이 규칙의 전부다.
    if stop_hit:
        stop = mk(1, "stop:fixed", 1.0,
                  f"진입 시 기록한 손절선 {stop_level:,.0f}원 이탈 "
                  f"(진입가 {pos.entry_price:,.0f}원 대비 "
                  f"-{ec.stop_loss_pct:.0f}%)",
                  urgent=True, detail={"stop_price": stop_level})
    elif pos.track == "VALUE":
        if pos.in_band and pos.entry_band_lo:
            if st["band_break_streak"] >= ec.band_break_days:
                stop = mk(1, "stop:band_break", 1.0,
                          f"청산 밴드 하단({pos.entry_band_lo:,.0f}원) 종가 "
                          f"{st['band_break_streak']}일 연속 이탈 — "
                          f"평균단가 추정이 틀렸다는 증거",
                          urgent=True,
                          detail={"streak": st["band_break_streak"]})
    else:  # TREND
        if st["ma_break_streak"] >= ec.ma_long_break_days:
            stop = mk(1, "stop:ma_long", 1.0,
                      f"종가가 MA{cfg.ma.long}({st['ma_long']:,.0f}원) 아래 "
                      f"{st['ma_break_streak']}일 연속 — 추세 전환 실패",
                      urgent=True)
        else:
            mas = moving_averages(close, cfg.ma)
            cs = cross_series(mas[f"ma{cfg.ma.short}"], mas[f"ma{cfg.ma.long}"])
            recent = cs[cs.index >= pd.Timestamp(pos.entry_date)]
            if len(recent) and int(recent.min()) == -1:
                stop = mk(1, "stop:dead_cross", 1.0,
                          f"{cfg.ma.short}x{cfg.ma.long} 데드크로스 발생 — "
                          f"진입 근거 소멸", urgent=True)
            elif pos.entry_cross_low:
                lvl = float(pos.entry_cross_low) * (1 - ec.cross_low_buffer_pct / 100.0)
                if price <= lvl:
                    stop = mk(1, "stop:cross_low", 1.0,
                              f"골든크로스 봉 저가 -{ec.cross_low_buffer_pct:.0f}% "
                              f"({lvl:,.0f}원) 이탈 — 크로스는 가짜였다",
                              urgent=True, detail={"level": lvl})

    if stop is not None:
        # 시장 급락일 손절 유예 (옵션). 유예는 1일, 계층 0 은 예외 없음.
        if (ec.panic_defer_enabled and market_ret is not None
                and np.isfinite(market_ret)
                and market_ret <= ec.panic_index_drop_pct):
            if not pos.defer_until:
                nxt = (_now_kst() + timedelta(days=ec.panic_defer_days)
                       ).strftime("%Y-%m-%d")
                st["defer_until"] = nxt
                log.info("%s 손절 유예 (시장 %.2f%%) → %s 재판정",
                         pos.ticker, market_ret, nxt)
                return None, st
            if pos.defer_until > today:
                return None, st
        return stop, st

    # ── 최소 보유일 게이트 ──
    # 여기서부터(계층 2~7)는 MIN_HOLD_DAYS 이전에 발동하지 않는다.
    # 계층 0·1 은 위에서 이미 판정을 끝냈으므로 이 게이트에 걸리지 않는다.
    if held < ec.min_hold_days:
        log.debug("%s 최소보유일 미달 (%d/%d) — 계층 2~7 억제",
                  pos.ticker, held, ec.min_hold_days)
        return None, st

    # ── 계층 2. 트레일링 (트랙 T) ──
    if pos.track == "TREND":
        peak = float(st["peak_close"])
        activated = peak > pos.entry_price     # 진입가를 넘긴 뒤에만 작동
        if activated:
            trail_level = peak * (1 - ec.trail_pct / 100.0)
            if price <= trail_level:
                return mk(2, "trail:peak", 1.0,
                          f"최고 종가 {peak:,.0f}원 대비 -{ec.trail_pct:.0f}% "
                          f"({trail_level:,.0f}원) 이탈",
                          detail={"peak": peak}), st
            mt = st["ma_trail"]
            if np.isfinite(mt) and price < mt:
                return mk(2, "trail:ma", 1.0,
                          f"MA{ec.trail_ma}({mt:,.0f}원) 종가 이탈 — "
                          f"추세 훼손", detail={"ma": mt}), st

    # ── 계층 3. 목표 3차 (트랙 V: 밴드 상단 회복) ──
    if pos.track == "VALUE" and pos.entry_band_hi and price >= float(pos.entry_band_hi):
        return mk(3, "target:band_hi", 1.0,
                  f"청산 밴드 상단({float(pos.entry_band_hi):,.0f}원) 회복 — "
                  f"신용 청산 압력 소멸, 진입 근거 종료",
                  detail={"band_hi": pos.entry_band_hi}), st

    # ── 계층 4. 목표 2차 (트랙 V: 피보 0.382 회복) ──
    if (pos.track == "VALUE" and pos.entry_fib_0382
            and not (pos.exits_done & DONE_TAKE2)
            and price >= float(pos.entry_fib_0382)):
        return mk(4, "target:fib_0382", ec.take2_ratio,
                  f"피보 0.382 되돌림선({float(pos.entry_fib_0382):,.0f}원) 회복 "
                  f"— {ec.take2_ratio * 100:.0f}% 청산"), st

    # ── 계층 5. 목표 1차 (공통 +15% 절반) ──
    if not (pos.exits_done & DONE_TAKE1) and ret >= ec.take1_pct:
        return mk(5, "target:take1", ec.take1_ratio,
                  f"+{ec.take1_pct:.0f}% 도달 — 기계적 "
                  f"{ec.take1_ratio * 100:.0f}% 익절 (교리 규칙 02)"), st

    # ── 계층 6. 시간 ──
    if (credit_ratio_now is not None and pos.entry_credit_ratio is not None
            and np.isfinite(credit_ratio_now)
            and credit_ratio_now - float(pos.entry_credit_ratio) >= ec.credit_resurge_pp):
        return mk(6, "time:credit_resurge", 1.0,
                  f"신용잔고율 {float(pos.entry_credit_ratio):.2f}% → "
                  f"{credit_ratio_now:.2f}% 재급증 — 이번엔 우리가 "
                  f"청산당할 쪽이다"), st

    if pos.track == "VALUE":
        if held >= ec.time_stop_v_days and ret < ec.time_stop_v_min_ret:
            return mk(6, "time:stall_v", 1.0,
                      f"{held}거래일 경과 & 수익률 {ret:+.1f}% "
                      f"(<{ec.time_stop_v_min_ret:.0f}%) — 청산 반등은 빠르다. "
                      f"세력 매집이 아니었다"), st
    else:
        mt = st["ma_trail"]
        if (held >= ec.time_stop_t_days and np.isfinite(mt) and price < mt):
            return mk(6, "time:stall_t", 1.0,
                      f"{held}거래일 경과 & MA{ec.trail_ma} 하회 — "
                      f"추세 소멸"), st

    return None, st


# ══════════════════════════ 계층 7. 순환매 ══════════════════════════
def evaluate_rotation(positions: list, price_map: dict,
                      cfg: Config = DEFAULT) -> list[ExitDecision]:
    """교리 규칙 01. 보유 종목 간 당일 등락 편차로 시소게임을 판정한다.

    개별 종목 룰이 아니라 포트폴리오 룰이므로 별도로 돌린다.
    """
    ec = cfg.exit
    rows = []
    for p in positions:
        df = price_map.get(p.ticker)
        if df is None or len(df) < 2:
            continue
        c = df["종가"].astype("float64")
        chg = (float(c.iloc[-1]) / float(c.iloc[-2]) - 1.0) * 100.0
        rows.append((p, chg, float(c.iloc[-1])))

    if len(rows) < 2:
        return []

    rows.sort(key=lambda t: -t[1])
    top, top_chg, top_price = rows[0]
    _, bot_chg, _ = rows[-1]
    spread = top_chg - bot_chg

    # 최소 보유일 게이트. 순환매는 계층 7 이라 억제 대상이다.
    # 편차 계산에는 남겨둔다 — 포트폴리오 상태에는 그 종목도 포함되므로
    # 빼면 편차 자체가 왜곡된다. 청산 대상에서만 뺀다.
    held = bars_held(top, price_map.get(top.ticker))
    if held < ec.min_hold_days:
        log.info("순환매 보류: 최고 상승 %s 이 최소보유일 미달 (%d/%d)",
                 top.ticker, held, ec.min_hold_days)
        return []

    if spread < ec.rotation_min_spread_pct:
        log.info("순환매 보류: 편차 %.1f%% (<%.0f%%) — 현금 보존",
                 spread, ec.rotation_min_spread_pct)
        return []
    if spread < ec.rotation_spread_pct:
        return []

    ret = (top_price / top.entry_price - 1.0) * 100.0
    dec = _decide(top, 7, "rotation:seesaw", ec.rotation_trim_ratio,
                  top_price, ret,
                  f"보유 종목 간 당일 편차 {spread:.1f}% "
                  f"(최고 {top_chg:+.1f}% / 최저 {bot_chg:+.1f}%) — "
                  f"급등 자산 {ec.rotation_trim_ratio * 100:.0f}% 현금화 후 "
                  f"진바닥 자산으로 스위칭", cfg, False,
                  {"spread": round(spread, 2), "top_chg": round(top_chg, 2),
                   "bot_chg": round(bot_chg, 2)})
    return [dec]


# ══════════════════════════ 배치 실행 ══════════════════════════
def run_exits(store, cfg: Config = DEFAULT,
              include_rotation: bool = True) -> dict:
    """보유 포지션 전체 판정 → 상태 갱신 → 신호 기록.

    포지션당 결정은 최대 1건이다. 여러 계층이 동시에 걸리면 우선순위가
    가장 높은(번호가 작은) 것만 집행한다.
    """
    started = _now_kst()
    positions = store.list_positions("OPEN")
    if not positions:
        return {"positions": 0, "decisions": [], "errors": []}

    flags = store.load_flags()
    market_ret = market_return_pct(store, days=1)
    trade_date = store.last_price_date()
    # 진입 시점 대비 신용잔고율 재급증을 보려면 '현재' 값이 있어야 한다.
    # 수동 주입분이 없으면 계층 6의 해당 규칙은 발동하지 않는다.
    credit_now = store.load_credit_ratios()

    decisions: list[ExitDecision] = []
    errors: list[tuple] = []
    price_map: dict = {}
    decided_ids: set = set()

    for pos in positions:
        try:
            ohlcv = store.load_ohlcv(pos.ticker, days=420)
            if ohlcv is None or ohlcv.empty:
                errors.append((pos.ticker, "시세 없음"))
                continue
            price_map[pos.ticker] = ohlcv
            dec, st = evaluate_position(pos, ohlcv, flags, cfg, market_ret,
                                        credit_ratio_now=credit_now.get(pos.ticker))
            if st:
                store.touch_position_state(
                    pos.id, st.get("peak_close"),
                    st.get("band_break_streak", 0),
                    st.get("ma_break_streak", 0),
                    st.get("defer_until"))
            if dec is not None:
                decisions.append(dec)
                decided_ids.add(pos.id)
        except Exception as exc:  # noqa: BLE001 - 포지션 격리
            errors.append((pos.ticker, f"{type(exc).__name__}: {exc}"))

    if include_rotation:
        rest = [p for p in positions if p.id not in decided_ids]
        try:
            decisions.extend(evaluate_rotation(rest, price_map, cfg))
        except Exception as exc:  # noqa: BLE001
            errors.append(("rotation", f"{type(exc).__name__}: {exc}"))

    decisions.sort(key=lambda d: (d.layer, -abs(d.ret_pct)))
    for dec in decisions:
        try:
            store.record_exit_signal(dec, trade_date)
        except Exception as exc:  # noqa: BLE001
            errors.append((dec.ticker, f"기록 실패 {exc}"))

    store.log_run("exits", started, len(decisions), len(errors),
                  note=f"positions={len(positions)}, market={market_ret:.2f}%"
                  if np.isfinite(market_ret) else f"positions={len(positions)}")

    log.info("청산 판정: 보유 %d건 → 신호 %d건 (오류 %d)",
             len(positions), len(decisions), len(errors))
    return {"positions": len(positions), "decisions": decisions,
            "errors": errors, "market_ret": market_ret,
            "trade_date": trade_date}
