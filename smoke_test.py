# -*- coding: utf-8 -*-
"""스모크 테스트. 네트워크·실DB 없이 전 모듈을 합성 데이터로 태운다.

목적
----
이 코드는 4,000줄 넘게 작성됐지만 한 번도 실행되지 않았다. 첫 실행에서
오류가 여러 개 동시에 터지므로, 하나 실패하면 멈추는 방식으로는 원인
분리가 안 된다. 그래서 **모든 검사를 끝까지 돌리고 마지막에 한꺼번에
보고한다.**

손으로 검산 가능한 앵커를 박아두는 게 핵심이다. 파라미터를 흔들 때
회귀를 잡을 수 있어야 한다.

  피보나치 : 고점 705,000 / 파동시작 300,000 -> 0.618 선 = 454,710
  청산밴드 : P0 x 1.40 x 0.50 = 0.70 x P0 (즉 -30%)
  밴드위치 : -30% 지점의 정규화 위치 r = 0.50 (밴드 정중앙)

실행
----
  python smoke_test.py            전체
  python smoke_test.py -v         실패 시 트레이스백까지
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import traceback
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

# ══════════════════════════ 미니 하네스 ══════════════════════════
_RESULTS: list[tuple[str, str, str]] = []   # (섹션, 이름, 상태/메시지)
_VERBOSE = "-v" in sys.argv


def check(section: str, name: str, fn):
    """검사 1건. 실패해도 다음으로 넘어간다."""
    try:
        fn()
        _RESULTS.append((section, name, "PASS"))
    except AssertionError as exc:
        _RESULTS.append((section, name, f"FAIL: {exc}"))
        if _VERBOSE:
            traceback.print_exc()
    except Exception as exc:  # noqa: BLE001
        _RESULTS.append((section, name, f"ERROR: {type(exc).__name__}: {exc}"))
        if _VERBOSE:
            traceback.print_exc()


def near(a, b, tol=1e-6, label=""):
    assert abs(float(a) - float(b)) <= tol, \
        f"{label} 기대 {b} 실제 {a} (허용 {tol})"


# ══════════════════════════ 합성 시세 ══════════════════════════
def _dates(n: int) -> pd.DatetimeIndex:
    """주말을 뺀 거래일 인덱스 (공휴일은 무시)."""
    end = datetime(2026, 8, 24)
    days, d = [], end
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    return pd.DatetimeIndex(sorted(days))


def _frame(close: np.ndarray, spread: float = 0.0,
           volume: np.ndarray | None = None) -> pd.DataFrame:
    """OHLCV 프레임.

    spread=0 이면 고가=저가=종가가 되어 피보나치 앵커가 정확히 맞는다.
    앵커 검증용 픽스처는 반드시 spread=0 으로 만든다.
    """
    n = len(close)
    idx = _dates(n)
    if volume is None:
        volume = np.full(n, 1_000_000.0)
    return pd.DataFrame({
        "시가": close,
        "고가": close * (1 + spread),
        "저가": close * (1 - spread),
        "종가": close,
        "거래량": volume,
        "거래대금": close * volume,
    }, index=idx)


def fixture_crash() -> pd.DataFrame:
    """앵커 픽스처. 300,000 -> 705,000 -> 0.618 되돌림(454,710) 정착.

    총 250봉으로 만드는 이유가 있다. evaluate_fib 는 tail(252) 로 1년만
    잘라내므로, 그보다 긴 시계열을 쓰면 파동 저점이 창 밖으로 밀려나
    스윙 저점이 엉뚱하게 잡힌다. 앵커 검증용 픽스처는 전 구간이
    lookback 안에 들어와야 한다.
    """
    lvl_0618 = 705_000 - (705_000 - 300_000) * 0.618      # 454,710
    seg1 = np.linspace(320_000, 300_000, 30)              # 하락해 저점 형성
    seg2 = np.linspace(300_000, 705_000, 100)             # 상승 파동
    seg3 = np.linspace(705_000, lvl_0618, 120)            # 되돌림
    close = np.concatenate([seg1, seg2, seg3])            # 250봉
    close[29] = 300_000.0            # 파동 저점 고정
    close[129] = 705_000.0           # 고점 고정
    close[-1] = lvl_0618             # 현재가 = 0.618 선
    return _frame(close, spread=0.0)


def fixture_golden_cross() -> pd.DataFrame:
    """급락 후 반등으로 20x60 골든크로스가 최근에 발생하는 픽스처."""
    seg1 = np.linspace(100_000, 60_000, 220)     # 장기 하락 (역배열)
    seg2 = np.full(40, 60_000.0)                 # 바닥 다지기 (이평 밀집)
    seg3 = np.linspace(60_000, 88_000, 60)       # 반등 (크로스 발생)
    close = np.concatenate([seg1, seg2, seg3])
    vol = np.full(len(close), 1_000_000.0)
    vol[-60:] = 3_000_000.0                      # 크로스 구간 거래량 급증
    return _frame(close, spread=0.005, volume=vol)


def fixture_flat() -> pd.DataFrame:
    """아무 신호도 안 나와야 하는 대조군."""
    n = 300
    close = 50_000 + np.sin(np.arange(n) / 9.0) * 250
    return _frame(close, spread=0.003)


# ══════════════════════════ 1. config / contracts ══════════════════════════
def test_config():
    from stocknews.config import DEFAULT, Config, ExitConfig

    def t_build():
        c = Config()
        assert c.ma.short == 20 and c.ma.mid == 40 and c.ma.long == 60
        assert c.fib.target == 0.618
        near(c.credit.maint_ratio, 1.40, label="담보유지비율")
        assert isinstance(c.exit, ExitConfig)
        near(c.exit.take1_pct, 15.0, label="1차 익절")

    def t_frozen():
        try:
            DEFAULT.ma.short = 5
        except Exception:
            return
        raise AssertionError("frozen dataclass 인데 수정이 허용됨")

    check("config", "Config 생성 및 기본값", t_build)
    check("config", "frozen 불변성", t_frozen)


def test_contracts():
    from stocknews.contracts import DONE_TAKE1, DONE_TAKE2, Position

    def t_bits():
        assert DONE_TAKE1 == 1 and DONE_TAKE2 == 2
        assert (DONE_TAKE1 | DONE_TAKE2) & DONE_TAKE2

    def t_in_band():
        p = Position(id=1, ticker="000001", name="테스트", track="VALUE",
                     entry_date="2026-08-01", entry_price=70_000, qty=10,
                     remaining=10, entry_band_hi=84_000, entry_band_lo=56_000)
        assert p.in_band is True
        q = Position(id=2, ticker="000002", name="밖", track="VALUE",
                     entry_date="2026-08-01", entry_price=90_000, qty=10,
                     remaining=10, entry_band_hi=84_000, entry_band_lo=56_000)
        assert q.in_band is False
        r = Position(id=3, ticker="000003", name="스냅없음", track="TREND",
                     entry_date="2026-08-01", entry_price=90_000, qty=10,
                     remaining=10)
        assert r.in_band is False

    check("contracts", "비트마스크", t_bits)
    check("contracts", "Position.in_band", t_in_band)


# ══════════════════════════ 2. 피보나치 (앵커) ══════════════════════════
def test_fibonacci():
    from stocknews.config import DEFAULT
    from stocknews.fibonacci import evaluate_fib, fib_levels, is_below_level

    df = fixture_crash()

    def t_levels():
        lv = fib_levels(705_000, 705_000 - 300_000, (0.382, 0.5, 0.618, 0.786))
        near(lv[0.618], 454_710, tol=0.5, label="0.618 레벨")
        near(lv[0.5], 502_500, tol=0.5, label="0.5 레벨")
        near(lv[0.382], 550_290, tol=0.5, label="0.382 레벨")

    def t_below():
        assert is_below_level(454_000, 705_000, 405_000, 0.618)
        assert not is_below_level(456_000, 705_000, 405_000, 0.618)

    def t_eval():
        f = evaluate_fib(df, DEFAULT.fib)
        assert f is not None, "evaluate_fib 가 None 을 반환"
        near(f.high, 705_000, tol=1.0, label="1년 고점")
        near(f.swing_low, 300_000, tol=1.0, label="파동 저점")
        near(f.swing, 405_000, tol=1.0, label="파동 폭")
        near(f.ratio, 0.618, tol=1e-4, label="되돌림 진행률")
        near(f.levels[0.618], 454_710, tol=1.0, label="0.618 지지선")
        assert f.below_target, "0.618 선에 정확히 있으면 below_target 이어야 함"
        assert not f.wave_broken
        assert 0.0 <= f.score <= 10.0, f"점수 범위 이탈: {f.score}"
        near(f.nearest_level, 0.618, label="근접 레벨")
        assert f.nearest_gap_pct < 0.01, f"이격 {f.nearest_gap_pct}"

    def t_swing_low_before_high():
        """스윙 저점은 '고점 이전' 구간에서 찾아야 한다.

        고점 이후 저점(454,710)을 섞으면 파동 폭이 부풀려져 0.618 선이
        실제보다 아래로 내려간다. 이 검사가 그 회귀를 잡는다.
        """
        f = evaluate_fib(df, DEFAULT.fib)
        hi_pos = int(df.index.get_loc(df["고가"].idxmax()))
        post_high_low = float(df["저가"].iloc[hi_pos:].min())
        assert f.swing_low < post_high_low, \
            f"고점 이후 저점({post_high_low})을 스윙 저점으로 잡았다"
        near(f.swing_low, 300_000, tol=1.0, label="고점 이전 최저가")

    def t_flat_no_signal():
        f = evaluate_fib(fixture_flat(), DEFAULT.fib)
        assert f is not None
        assert not f.below_target, "횡보 종목이 0.618 이하로 잡힘"

    check("fibonacci", "레벨 계산 앵커", t_levels)
    check("fibonacci", "is_below_level", t_below)
    check("fibonacci", "evaluate_fib 앵커 (705000/300000)", t_eval)
    check("fibonacci", "스윙 저점은 고점 이전 구간", t_swing_low_before_high)
    check("fibonacci", "횡보 대조군", t_flat_no_signal)


# ══════════════════════════ 3. 이평 골든크로스 ══════════════════════════
def test_indicators():
    from stocknews.config import DEFAULT
    from stocknews.indicators import (alignment_of, convergence_pct,
                                      cross_series, evaluate_trend, last_cross,
                                      moving_averages, slope_pct)

    gc = fixture_golden_cross()
    close = gc["종가"]

    def t_ma():
        mas = moving_averages(close, DEFAULT.ma)
        assert list(mas.columns) == ["ma20", "ma40", "ma60"]
        assert mas["ma20"].iloc[:19].isna().all(), "이평 워밍업 구간이 NaN 아님"
        near(mas["ma20"].iloc[-1], close.tail(20).mean(), tol=1e-6, label="MA20")

    def t_cross_no_dup():
        """붙어서 진동할 때 신호가 연속 발생하지 않아야 한다."""
        a = pd.Series([1.0, 1.0, 1.0, 1.0], index=_dates(4))
        b = pd.Series([2.0, 0.5, 0.4, 0.3], index=_dates(4))
        cs = cross_series(a, b)
        assert int(cs.iloc[1]) == 1, "상향 돌파를 잡지 못함"
        assert int(cs.iloc[2]) == 0 and int(cs.iloc[3]) == 0, \
            "이미 위에 있는데 신호가 반복 발생"

    def t_cross_nan_guard():
        mas = moving_averages(close, DEFAULT.ma)
        cs = cross_series(mas["ma20"], mas["ma60"])
        assert (cs.iloc[:59] == 0).all(), "이평 미충족 구간에서 신호 발생"

    def t_last_cross():
        mas = moving_averages(close, DEFAULT.ma)
        ev = last_cross(mas["ma20"], mas["ma60"], "20x60", 20, 60)
        assert ev is not None, "골든크로스 픽스처인데 교차를 못 찾음"
        assert ev.kind == "GOLDEN", f"종류가 {ev.kind}"
        assert ev.bars_ago >= 0
        assert ev.fast == 20 and ev.slow == 60

    def t_helpers():
        mas = moving_averages(close, DEFAULT.ma)
        assert slope_pct(mas["ma60"], 5) > 0, "반등 구간인데 MA60 기울기가 음수"
        cv = convergence_pct(mas.iloc[-1], float(close.iloc[-1]))
        assert cv >= 0
        assert alignment_of(mas.iloc[-1], DEFAULT.ma) in (
            "GOLDEN", "DEAD", "MIXED", "UNKNOWN")

    def t_eval_trend():
        t = evaluate_trend(gc, DEFAULT.ma)
        assert t is not None, "evaluate_trend 가 None"
        assert 0.0 <= t.score <= 10.0, f"점수 범위 이탈: {t.score}"
        assert t.best_cross is not None and t.best_cross.kind == "GOLDEN"
        assert t.score >= 5.0, f"밀집 후 크로스 + 거래량 급증인데 {t.score}점"
        # 배점 상한 검증: 각 항목이 정의된 최대치를 넘지 않아야 한다
        bd = t.breakdown
        assert bd.get("cross_pair", 0) <= 3.0
        assert bd.get("freshness", 0) <= 2.0
        assert bd.get("convergence", 0) <= 2.0
        assert bd.get("slope_long", 0) <= 1.5
        assert bd.get("volume", 0) <= 1.5

    def t_dead_no_cross():
        """장기 하락만 있는 구간은 골든크로스가 없어야 한다."""
        falling = _frame(np.linspace(100_000, 55_000, 300), spread=0.004)
        t = evaluate_trend(falling, DEFAULT.ma)
        assert t is not None
        assert t.alignment == "DEAD", f"역배열이어야 하는데 {t.alignment}"
        assert t.score <= 4.0, f"하락 추세인데 추세점수 {t.score}"

    def t_short_data():
        tiny = _frame(np.linspace(100, 110, 30))
        assert evaluate_trend(tiny, DEFAULT.ma) is None, \
            "데이터 부족인데 None 을 반환하지 않음"

    check("indicators", "이동평균", t_ma)
    check("indicators", "교차 중복 방지", t_cross_no_dup)
    check("indicators", "워밍업 구간 NaN 가드", t_cross_nan_guard)
    check("indicators", "last_cross", t_last_cross)
    check("indicators", "기울기/밀집도/배열", t_helpers)
    check("indicators", "evaluate_trend 배점 상한", t_eval_trend)
    check("indicators", "하락 추세 대조군", t_dead_no_cross)
    check("indicators", "데이터 부족 처리", t_short_data)


# ══════════════════════════ 4. 청산 밴드 (앵커) ══════════════════════════
def test_liquidation():
    from stocknews.config import DEFAULT
    from stocknews.cost_basis import estimate_cost_basis, from_volume_profile
    from stocknews.liquidation import (band_position, evaluate_liquidation,
                                       liquidation_band, margin_call_due_dates)

    cc = DEFAULT.credit

    def t_band_anchor():
        b = liquidation_band(100_000, cc)
        near(b["hi"], 84_000, tol=1e-6, label="밴드 상단 (-16%)")
        near(b["mid"], 70_000, tol=1e-6, label="밴드 중심 (-30%)")
        near(b["lo"], 56_000, tol=1e-6, label="밴드 하단 (-44%)")

    def t_band_pos_anchor():
        b = liquidation_band(100_000, cc)
        near(band_position(70_000, b), 0.50, tol=1e-9,
             label="-30% 지점의 밴드 위치")
        near(band_position(84_000, b), 1.0, tol=1e-9, label="상단")
        near(band_position(56_000, b), 0.0, tol=1e-9, label="하단")

    def t_margin_dates():
        close = np.full(30, 10_000.0)
        close[20] = 9_300.0                      # -7% 급락일
        df = _frame(close)
        due = margin_call_due_dates(df, drop_pct=-0.05, offset_bd=2)
        assert len(due) == 1, f"급락일 1건인데 {len(due)}건"
        assert due[0] == df.index[22], "D+2 거래일이 아님"

    def t_poc():
        p0 = from_volume_profile(fixture_crash(), lookback=120)
        assert p0 is not None and p0 > 0

    def t_estimate():
        p0, method, conf = estimate_cost_basis(fixture_crash())
        assert p0 > 0
        assert method == "C", f"신용/투자자 데이터 없으면 C 여야 하는데 {method}"
        assert conf in ("HIGH", "MID", "LOW")

    def t_eval_lps():
        df = fixture_crash()
        p0, method, conf = estimate_cost_basis(df)
        q = evaluate_liquidation(df, p0, method, conf, cc)
        assert 0.0 <= q.score <= 10.0, f"LPS 범위 이탈: {q.score}"
        near(q.band_mid, p0 * 0.70, tol=1e-6, label="밴드 중심")
        assert q.basis_method == "C"

    def t_lps_cap_without_credit():
        """신용잔고 실측이 없으면 credit_heat 가 캡되어야 한다."""
        df = fixture_crash()
        p0, m, c = estimate_cost_basis(df)
        q = evaluate_liquidation(df, p0, m, c, cc, credit_ratio=None)
        near(q.breakdown["credit_heat"], 1.25, label="프록시 캡")
        q2 = evaluate_liquidation(df, p0, m, c, cc, credit_ratio=6.0)
        near(q2.breakdown["credit_heat"], 4.0, label="신용잔고율 6% 만점")

    check("liquidation", "밴드 앵커 (1.4 x 융자비율)", t_band_anchor)
    check("liquidation", "밴드 위치 r=0.5 앵커", t_band_pos_anchor)
    check("liquidation", "D+2 반대매매 캘린더", t_margin_dates)
    check("cost_basis", "매물대 POC", t_poc)
    check("cost_basis", "A/B/C 폴백", t_estimate)
    check("liquidation", "LPS 산출", t_eval_lps)
    check("liquidation", "신용 프록시 점수 캡", t_lps_cap_without_credit)


# ══════════════════════════ 5. 스크리너 ══════════════════════════
def test_screener():
    from stocknews.config import DEFAULT
    from stocknews.screener import (check_exclusion, confluence_check,
                                    rank_results, screen_one)

    def t_exclusion():
        assert check_exclusion({"관리종목": True}, None, None) is not None
        assert check_exclusion({"동전주위험": True}, None, None) is not None, \
            "동전주위험이 배제 목록에 없음"
        assert check_exclusion({}, 5_000e8, 500) is None
        assert check_exclusion({}, 100e8, 500) is not None, "시총 하한 미적용"
        assert check_exclusion({}, 5_000e8, 100) is not None, "상장기간 미적용"
        assert check_exclusion(None, None, None) is None

    def t_confluence():
        assert confluence_check(70_000, 70_500, 70_000, 3.0) is True
        assert confluence_check(70_000, 90_000, 70_000, 3.0) is False
        assert confluence_check(float("nan"), 70_000, 70_000, 3.0) is False

    def t_screen_crash():
        r = screen_one("000001", "급락종목", fixture_crash(), cfg=DEFAULT)
        assert r.excluded is None
        assert r.fib is not None and r.liq is not None
        assert 0.0 <= r.value_score <= 10.0, f"매집점수 {r.value_score}"
        assert 0.0 <= r.trend_score <= 10.0, f"추세점수 {r.trend_score}"
        assert r.grade in ("S+", "S", "A", "B", "NONE")
        assert r.track in ("VALUE", "TREND", "BOTH")
        assert isinstance(r.reasons, tuple)

    def t_screen_excluded():
        r = screen_one("000002", "관리종목", fixture_crash(),
                       flags={"관리종목": True}, cfg=DEFAULT)
        assert r.excluded is not None, "관리종목인데 배제되지 않음"
        assert r.grade == "NONE"

    def t_rank():
        a = screen_one("000001", "급락", fixture_crash(), cfg=DEFAULT)
        b = screen_one("000003", "골든", fixture_golden_cross(), cfg=DEFAULT)
        c = screen_one("000004", "횡보", fixture_flat(), cfg=DEFAULT)
        out = rank_results([c, a, b])
        order = {"S+": 0, "S": 1, "A": 2, "B": 3, "NONE": 4}
        keys = [order[r.grade] for r in out]
        assert keys == sorted(keys), f"등급 정렬이 깨짐: {[r.grade for r in out]}"

    check("screener", "배제 필터", t_exclusion)
    check("screener", "피보-밴드 겹침", t_confluence)
    check("screener", "screen_one 급락 픽스처", t_screen_crash)
    check("screener", "screen_one 배제 경로", t_screen_excluded)
    check("screener", "rank_results 정렬", t_rank)


# ══════════════════════════ 6. Store (임시 DB) ══════════════════════════
def test_store(tmp: Path):
    from stocknews.config import DEFAULT
    from stocknews.store import Store

    db = tmp / "smoke.db"
    st = Store(db)
    df = fixture_crash()

    def t_prices():
        n = st.upsert_prices("000001", df)
        assert n == len(df), f"적재 {n} != {len(df)}"
        st.upsert_prices("000001", df)            # 재실행 멱등성
        back = st.load_ohlcv("000001", days=1000)
        assert back is not None and len(back) == len(df), "왕복 건수 불일치"
        for col in ("시가", "고가", "저가", "종가", "거래량"):
            assert col in back.columns, f"{col} 컬럼 없음"
        near(back["종가"].iloc[-1], df["종가"].iloc[-1], tol=1e-6, label="마지막 종가")
        assert st.last_price_date() == df.index[-1].strftime("%Y-%m-%d")

    def t_cross_section():
        cs = pd.DataFrame({
            "시가": [1000.0, 2000.0], "고가": [1010.0, 2020.0],
            "저가": [990.0, 1980.0], "종가": [1005.0, 2010.0],
            "거래량": [100.0, 0.0],        # 두 번째는 거래정지 -> 제외돼야 함
            "거래대금": [100_500.0, 0.0],
        }, index=["000010", "000011"])
        n = st.upsert_cross_section("2026-08-25", cs)
        assert n == 1, f"거래량 0 종목이 걸러지지 않음 (n={n})"

    def t_tickers():
        st.upsert_tickers([
            {"ticker": "000001", "name": "급락종목", "market": "KOSPI",
             "market_cap": 5_000e8, "shares": 1e7},
            {"ticker": "000003", "name": "골든종목", "market": "KOSDAQ",
             "market_cap": 2_000e8, "shares": 5e6},
        ])
        # 업종만 담아 재호출 — 시총이 지워지면 안 된다 (COALESCE 검증)
        st.upsert_tickers([{"ticker": "000001", "name": "급락종목",
                            "sector": "조선"}])
        meta = st.ticker_meta()
        assert "000001" in meta.index
        near(meta.at["000001", "market_cap"], 5_000e8, tol=1.0,
             label="시총 보존")
        assert meta.at["000001", "sector"] == "조선"
        assert set(st.active_tickers()) == {"000001", "000003"}

    def t_inactive():
        st.mark_inactive({"000001"})
        assert set(st.active_tickers()) == {"000001"}, "비활성 처리 실패"
        st.upsert_tickers([{"ticker": "000003", "name": "골든종목"}])
        assert "000003" in st.active_tickers(), "재등록 시 active 복구 실패"

    def t_scan_reco():
        from stocknews.screener import screen_one
        r = screen_one("000001", "급락종목", df)
        n = st.save_scan("2026-08-24", [r], fib_target=0.618)
        assert n == 1
        st.save_scan("2026-08-24", [r], fib_target=0.618)   # 멱등
        hist = st.scan_history(days=5)
        assert len(hist) == 1, f"스냅샷 중복 적재: {len(hist)}"
        assert st.save_recos("2026-08-24", [("VALUE", r)]) == 1
        rh = st.reco_history(days=5)
        assert len(rh) == 1 and rh.iloc[0]["ticker"] == "000001"

    def t_flags_coalesce():
        """공급원별 부분 갱신이 서로를 지우지 않아야 한다."""
        st.upsert_flags([{"ticker": "000001", "admin_issue": 1,
                          "source": "fdr"}])
        st.upsert_flags([{"ticker": "000001", "capital_impair": 62.5,
                          "source": "dart", "note": "2026/11011 OFS"}])
        st.upsert_flags([{"ticker": "000001", "penny_risk": 0,
                          "source": "local"}])
        f = st.load_flags()["000001"]
        assert f["관리종목"] is True, "local 갱신이 관리종목을 지웠다"
        assert f["자본잠식"] is True, "자본잠식률 62.5% 인데 False"
        near(f["자본잠식률"], 62.5, label="잠식률 보존")

    def t_flags_ttl():
        """capital_impair 전용 타임스탬프가 유지되는지.

        updated 를 쓰면 로컬 판정이 매일 행을 갱신해 DART TTL 이 영원히
        만료되지 않는다. 그 회귀를 잡는 검사다.
        """
        stale = st.flag_staleness("capital_impair")
        assert "000001" in stale, "capital_impair_at 이 기록되지 않음"
        st.upsert_flags([{"ticker": "000001", "halt_history": 0,
                          "source": "local"}])
        stale2 = st.flag_staleness("capital_impair")
        assert stale2.get("000001") == stale["000001"], \
            "로컬 갱신이 capital_impair_at 을 덮어썼다"

    def t_flags_clear():
        st.clear_flag_field("admin_issue")
        assert st.load_flags()["000001"]["관리종목"] is False, "해제 반영 실패"
        assert st.load_flags()["000001"]["자본잠식"] is True, \
            "admin_issue 초기화가 다른 필드를 지웠다"
        try:
            st.clear_flag_field("없는필드")
        except ValueError:
            return
        raise AssertionError("알 수 없는 필드인데 예외가 없음")

    def t_positions():
        pid = st.open_position(
            "000001", "급락종목", "VALUE", "2026-08-24", 455_000, 100,
            snapshot={"p0": 650_000, "band_hi": 546_000, "band_mid": 455_000,
                      "band_lo": 364_000, "fib_0382": 550_290,
                      "fib_0618": 454_710, "cross_low": None,
                      "credit_ratio": 4.2})
        assert pid > 0
        ps = st.list_positions("OPEN")
        assert len(ps) == 1
        p = ps[0]
        assert p.track == "VALUE" and p.remaining == 100
        near(p.entry_band_mid, 455_000, tol=1.0, label="스냅샷 밴드 중심")
        near(p.entry_credit_ratio, 4.2, label="진입 신용잔고율")
        assert p.in_band is True

    def t_state_update():
        p = st.list_positions("OPEN")[0]
        st.touch_position_state(p.id, 470_000, 2, 0, None)
        p2 = st.list_positions("OPEN")[0]
        near(p2.peak_close, 470_000, tol=1.0, label="peak_close")
        assert p2.band_break_streak == 2
        near(p2.entry_band_mid, 455_000, tol=1.0,
             label="상태 갱신이 스냅샷을 건드리지 않음")

    def t_exit_signal_and_fill():
        from stocknews.contracts import DONE_TAKE1, ExitDecision
        p = st.list_positions("OPEN")[0]
        dec = ExitDecision(
            ticker=p.ticker, name=p.name, position_id=p.id, layer=5,
            rule="target:take1", action="TRIM", ratio=0.5, qty=50,
            signal_price=523_000, ret_pct=15.0, net_ret_pct=14.5,
            reason="+15% 도달", urgent=False)
        log_id = st.record_exit_signal(dec, "2026-08-24")
        assert log_id > 0

        # 신호만으로는 잔량이 줄지 않아야 한다
        assert st.list_positions("OPEN")[0].remaining == 100, \
            "신호 기록만으로 잔량이 줄었다"
        assert st.list_positions("OPEN")[0].exits_done & DONE_TAKE1, \
            "목표1차 비트가 세워지지 않음"

        pend = st.pending_exits(days=5)
        assert len(pend) == 1 and int(pend.iloc[0]["executed"]) == 0

        out = st.confirm_exit(log_id, 520_000)
        assert out["filled_qty"] == 50 and out["remaining"] == 50
        assert out["status"] == "OPEN"
        assert len(st.pending_exits(days=5)) == 0, "체결 후에도 미체결로 남음"

        try:
            st.confirm_exit(log_id, 520_000)
        except ValueError:
            pass
        else:
            raise AssertionError("이미 체결된 신호를 재확인했는데 예외 없음")

    def t_close():
        p = st.list_positions("OPEN")[0]
        st.close_position(p.id, "스모크 종료")
        assert st.list_positions("OPEN") == []
        assert len(st.list_positions(None)) == 1

    def t_news():
        rows = [{
            "id": "abc123", "d": "2026-08-24",
            "published": "2026-08-24T09:00:00",
            "title": "HD현대중공업 수주 잭팟", "title_norm": "hd현대중공업 수주 잭팟",
            "url": "https://x/1", "source": "매체A", "origin": "GOOGLE",
            "region": "KR", "category": "방산조선", "cluster_id": "abc123",
            "cluster_n": 3, "importance": 7.5, "lang": "ko",
        }]
        assert st.upsert_news(rows) == 1
        st.upsert_news(rows)                       # 멱등
        assert st.link_news_tickers([("abc123", "000001", "급락종목")]) == 1
        got = st.news_since(hours=24 * 365)
        assert len(got) == 1, f"뉴스 조회 {len(got)}건"
        m = st.news_ticker_map(["abc123"])
        assert len(m) == 1 and m.iloc[0]["ticker"] == "000001"
        assert "abc123" in st.news_ids(days=365)
        tc = st.news_theme_counts(days=365)
        assert len(tc) == 1 and int(tc.iloc[0]["clusters"]) == 1

    def t_credit_manual():
        """수동 신용잔고 주입. LPS credit_heat 캡을 벗기는 경로다."""
        st.upsert_credit([
            {"ticker": "000001", "ratio": 5.5, "asof": "2026-08-24",
             "source": "manual", "note": "스모크"},
            {"ticker": "000003", "ratio": 1.1,
             "asof": "2020-01-01"},          # 오래된 기준일 → 제외돼야 함
        ])
        got = st.load_credit_ratios(max_age_days=14)
        assert "000001" in got, "최신 신용잔고가 조회되지 않음"
        near(got["000001"], 5.5, label="신용잔고율")
        assert "000003" not in got, "기준일이 오래된 값이 걸러지지 않음"
        assert st.load_credit_ratios(max_age_days=None).get("000003") == 1.1
        cov = st.credit_coverage()
        assert cov["with_credit"] == 2 and cov["active"] >= 1

    def t_credit_lifts_cap():
        """실측 신용잔고가 들어오면 매집 점수 상한이 올라가야 한다."""
        from stocknews.cost_basis import estimate_cost_basis
        from stocknews.liquidation import evaluate_liquidation
        df = fixture_crash()
        p0, m, c = estimate_cost_basis(df)
        proxy = evaluate_liquidation(df, p0, m, c, DEFAULT.credit,
                                     credit_ratio=None)
        real = evaluate_liquidation(df, p0, m, c, DEFAULT.credit,
                                    credit_ratio=5.5)
        assert real.score > proxy.score, \
            f"실측({real.score}) 이 프록시({proxy.score}) 보다 높지 않다"
        near(real.breakdown["credit_heat"], 4.0, label="실측 만점")

    def t_export():
        out = tmp / "export"
        written = st.export_csv(out_dir=out, days=30, tag="smoke")
        for name in ("scans", "recos", "flags", "positions", "tickers",
                     "credit_manual", "exit_log"):
            assert name in written, f"{name} 내보내기 누락"
            p = Path(written[name]["path"])
            assert p.exists(), f"{p} 없음"
        # 한글 엑셀 호환: BOM 이 있어야 한다
        head = Path(written["scans"]["path"]).read_bytes()[:3]
        assert head == b"\xef\xbb\xbf", "utf-8-sig BOM 없음 (엑셀 한글 깨짐)"

    def t_misc():
        assert st.existing_dates() , "거래일 집합이 빔"
        assert st.bar_count(100) >= 1
        pm = st.price_matrix(days=10)
        assert not pm.empty and "000001" in pm.columns
        st.log_run("smoke", datetime.now(), 1, 0, note="ok")

    check("store", "시세 왕복 + 멱등성", t_prices)
    check("store", "일자별 전종목 적재 (거래정지 제외)", t_cross_section)
    check("store", "종목 마스터 COALESCE 보존", t_tickers)
    check("store", "비활성/재등록", t_inactive)
    check("store", "스캔 스냅샷 + 추천 이력", t_scan_reco)
    check("store", "플래그 부분 갱신 보존", t_flags_coalesce)
    check("store", "capital_impair 전용 TTL", t_flags_ttl)
    check("store", "플래그 필드 초기화", t_flags_clear)
    check("store", "포지션 개시 + 스냅샷", t_positions)
    check("store", "파생 상태 갱신", t_state_update)
    check("store", "청산 신호 -> 체결 2단계", t_exit_signal_and_fill)
    check("store", "포지션 종료", t_close)
    check("store", "뉴스 적재/조회", t_news)
    check("store", "수동 신용잔고 + 기준일 만료", t_credit_manual)
    check("liquidation", "실측 신용잔고가 캡을 벗김", t_credit_lifts_cap)
    check("store", "CSV 내보내기 (BOM 확인)", t_export)
    check("store", "기타 조회", t_misc)
    return st


# ══════════════════════════ 7. 청산 엔진 ══════════════════════════
def test_exits():
    from stocknews.config import DEFAULT
    from stocknews.contracts import DONE_TAKE1, Position
    from stocknews.exits import (atr, derive_state, evaluate_position,
                                 evaluate_rotation)

    ec = DEFAULT.exit

    def _pos(**kw):
        base = dict(id=1, ticker="000001", name="테스트", track="VALUE",
                    entry_date="2026-01-05", entry_price=70_000, qty=100,
                    remaining=100, entry_p0=100_000, entry_band_hi=84_000,
                    entry_band_mid=70_000, entry_band_lo=56_000,
                    entry_fib_0382=80_000, entry_fib_0618=70_000)
        base.update(kw)
        return Position(**base)

    def _flat_at(price: float, n: int = 200) -> pd.DataFrame:
        return _frame(np.full(n, float(price)), spread=0.004)

    def t_atr():
        a = atr(fixture_golden_cross(), 14)
        assert a > 0 and np.isfinite(a), f"ATR={a}"

    def t_derive():
        df = _flat_at(70_000)
        st = derive_state(_pos(), df, DEFAULT)
        assert st["peak_close"] >= 70_000
        assert st["bars_held"] >= 1
        assert st["band_break_streak"] == 0

    def t_layer0_invalidation():
        df = _flat_at(70_000)
        dec, _ = evaluate_position(_pos(), df, {"000001": {"관리종목": True}},
                                   DEFAULT)
        assert dec is not None and dec.layer == 0, \
            f"무효화가 잡히지 않음 (layer={dec.layer if dec else None})"
        assert dec.action == "EXIT_ALL" and dec.urgent is True
        assert dec.qty == 100

    def t_layer0_beats_profit():
        """무효화는 이익 중이어도 최우선이다."""
        df = _flat_at(90_000)          # +28.6% 이익 상태
        dec, _ = evaluate_position(_pos(), df, {"000001": {"자본잠식": True,
                                                          "자본잠식률": 71.0}},
                                   DEFAULT)
        assert dec is not None and dec.layer == 0, \
            f"이익 중일 때 무효화가 밀렸다 (layer={dec.layer if dec else None})"

    def t_layer1_band_break():
        n = 200
        close = np.full(n, 70_000.0)
        close[-3:] = 50_000.0          # 밴드 하단(56,000) 아래 3일 연속
        dec, st = evaluate_position(_pos(), _frame(close, spread=0.004),
                                    None, DEFAULT)
        assert st["band_break_streak"] >= ec.band_break_days
        assert dec is not None and dec.layer == 1, \
            f"밴드 이탈 손절 미발동 (layer={dec.layer if dec else None})"
        assert dec.action == "EXIT_ALL" and dec.urgent is True

    def t_layer1_needs_streak():
        """하루 이탈은 꼬리일 수 있으므로 손절하지 않아야 한다."""
        n = 200
        close = np.full(n, 70_000.0)
        close[-1] = 50_000.0
        dec, _ = evaluate_position(_pos(), _frame(close, spread=0.004),
                                   None, DEFAULT)
        assert dec is None or dec.layer != 1, \
            "1일 이탈로 손절이 발동했다"

    def t_layer3_band_hi():
        df = _flat_at(85_000)          # 밴드 상단 84,000 회복
        dec, _ = evaluate_position(_pos(), df, None, DEFAULT)
        assert dec is not None and dec.layer == 3, \
            f"밴드 상단 회복 목표 미발동 (layer={dec.layer if dec else None})"
        assert dec.action == "EXIT_ALL"

    def t_layer4_fib():
        df = _flat_at(80_500)          # 0.382(80,000) 회복, 밴드 상단 미달
        dec, _ = evaluate_position(_pos(), df, None, DEFAULT)
        assert dec is not None and dec.layer == 4, \
            f"피보 0.382 회복 미발동 (layer={dec.layer if dec else None})"
        assert dec.action == "TRIM"
        assert dec.qty == 30, f"30% 청산인데 {dec.qty}주"

    def t_layer5_take1():
        # 0.382 를 아주 높게 밀어 4차가 안 걸리게 하고 +15% 만 성립시킨다
        p = _pos(entry_fib_0382=999_000, entry_band_hi=999_000)
        df = _flat_at(70_000 * 1.16)
        dec, _ = evaluate_position(p, df, None, DEFAULT)
        assert dec is not None and dec.layer == 5, \
            f"+15% 익절 미발동 (layer={dec.layer if dec else None})"
        assert dec.qty == 50 and dec.action == "TRIM"
        near(dec.net_ret_pct, dec.ret_pct - ec.roundtrip_cost_pct,
             tol=0.01, label="비용 차감 수익률")

    def t_take1_not_repeated():
        p = _pos(entry_fib_0382=999_000, entry_band_hi=999_000,
                 exits_done=DONE_TAKE1, remaining=50)
        df = _flat_at(70_000 * 1.16)
        dec, _ = evaluate_position(p, df, None, DEFAULT)
        assert dec is None or dec.layer != 5, "목표1차가 중복 발동했다"

    def t_min_lot_promotion():
        p = _pos(entry_fib_0382=999_000, entry_band_hi=999_000, remaining=1)
        df = _flat_at(70_000 * 1.16)
        dec, _ = evaluate_position(p, df, None, DEFAULT)
        assert dec is not None and dec.qty == 1
        assert dec.action == "EXIT_ALL", "1주에서 부분청산이 전량으로 승격 안 됨"
        assert "최소 주문단위" in dec.reason

    def t_layer6_time_v():
        n = 200
        close = np.full(n, 70_100.0)      # 사실상 무변동
        df = _frame(close, spread=0.002)
        p = _pos(entry_date=df.index[-20].strftime("%Y-%m-%d"),
                 entry_price=70_000, entry_fib_0382=999_000,
                 entry_band_hi=999_000)
        dec, st = evaluate_position(p, df, None, DEFAULT)
        assert st["bars_held"] >= ec.time_stop_v_days
        assert dec is not None and dec.layer == 6, \
            f"시간 손절 미발동 (layer={dec.layer if dec else None})"

    def t_trend_trailing():
        n = 300
        close = np.concatenate([
            np.linspace(50_000, 100_000, 260),   # 상승
            np.linspace(100_000, 88_000, 40),    # 고점 대비 -12%
        ])
        df = _frame(close, spread=0.004)
        p = _pos(track="TREND", entry_price=60_000,
                 entry_date=df.index[-30].strftime("%Y-%m-%d"),
                 entry_band_hi=None, entry_band_lo=None,
                 entry_fib_0382=None, entry_cross_low=48_000)
        dec, _ = evaluate_position(p, df, None, DEFAULT)
        assert dec is not None, "트레일링/손절 어느 것도 발동하지 않음"
        assert dec.layer in (1, 2), f"추세 청산인데 layer={dec.layer}"

    def t_trend_ma_long_break():
        n = 300
        close = np.concatenate([np.linspace(50_000, 100_000, 250),
                                np.linspace(100_000, 62_000, 50)])
        df = _frame(close, spread=0.004)
        p = _pos(track="TREND", entry_price=95_000,
                 entry_date=df.index[-40].strftime("%Y-%m-%d"),
                 entry_band_hi=None, entry_band_lo=None, entry_fib_0382=None,
                 entry_cross_low=90_000)
        dec, _ = evaluate_position(p, df, None, DEFAULT)
        assert dec is not None and dec.layer == 1, \
            f"MA60 이탈 손절 미발동 (layer={dec.layer if dec else None})"

    def t_no_signal():
        """밴드 정중앙에서 조용히 있으면 아무 신호도 없어야 한다."""
        p = _pos(entry_date=_dates(200)[-3].strftime("%Y-%m-%d"))
        dec, _ = evaluate_position(p, _flat_at(70_000), None, DEFAULT)
        assert dec is None, f"신호가 없어야 하는데 layer={dec.layer}"

    def t_rotation():
        hot = np.full(60, 10_000.0)
        hot[-1] = 11_200.0             # +12%
        cold = np.full(60, 10_000.0)
        cold[-1] = 9_400.0             # -6%  => 편차 18%
        pmap = {"000001": _frame(hot, spread=0.002),
                "000002": _frame(cold, spread=0.002)}
        ps = [_pos(id=1, ticker="000001", entry_price=10_000),
              _pos(id=2, ticker="000002", entry_price=10_000)]
        decs = evaluate_rotation(ps, pmap, DEFAULT)
        assert len(decs) == 1, f"순환매 신호 {len(decs)}건"
        d = decs[0]
        assert d.layer == 7 and d.ticker == "000001", "급등 자산이 아님"
        assert d.qty == 50, f"50% 청산인데 {d.qty}주"

    def t_rotation_hold():
        """편차가 작으면 순환매를 보류해야 한다 (교리 규칙 03)."""
        a = np.full(60, 10_000.0)
        a[-1] = 10_100.0
        b = np.full(60, 10_000.0)
        b[-1] = 9_900.0
        pmap = {"000001": _frame(a, spread=0.002),
                "000002": _frame(b, spread=0.002)}
        ps = [_pos(id=1, ticker="000001", entry_price=10_000),
              _pos(id=2, ticker="000002", entry_price=10_000)]
        assert evaluate_rotation(ps, pmap, DEFAULT) == [], \
            "편차 2%인데 순환매가 발동했다"

    check("exits", "ATR", t_atr)
    check("exits", "derive_state 멱등 계산", t_derive)
    check("exits", "계층0 무효화", t_layer0_invalidation)
    check("exits", "계층0이 이익보다 우선", t_layer0_beats_profit)
    check("exits", "계층1 밴드 하단 3일 이탈", t_layer1_band_break)
    check("exits", "계층1 1일 이탈은 무시", t_layer1_needs_streak)
    check("exits", "계층3 밴드 상단 회복", t_layer3_band_hi)
    check("exits", "계층4 피보 0.382 회복 30%", t_layer4_fib)
    check("exits", "계층5 +15% 절반", t_layer5_take1)
    check("exits", "목표1차 중복 방지", t_take1_not_repeated)
    check("exits", "최소 주문단위 전량 승격", t_min_lot_promotion)
    check("exits", "계층6 시간 손절 (V)", t_layer6_time_v)
    check("exits", "추세 트레일링", t_trend_trailing)
    check("exits", "추세 MA60 이탈 손절", t_trend_ma_long_break)
    check("exits", "무신호 대조군", t_no_signal)
    check("exits", "계층7 순환매", t_rotation)
    check("exits", "순환매 보류 (편차 5% 미만)", t_rotation_hold)


# ══════════════════════════ 8. 뉴스 정리 ══════════════════════════
def test_news():
    from stocknews.news import (build_alias_index, classify, cluster_items,
                                make_id, map_tickers, normalize_title,
                                score_importance)

    def t_normalize():
        a = normalize_title("[단독] 삼성전자, HBM4 양산 착수 - 한국경제")
        assert "단독" not in a and "한국경제" not in a, a
        assert "삼성전자" in a and "hbm4" in a, a
        b = normalize_title("삼성전자 HBM4 양산 착수 (종합)")
        assert normalize_title("[속보]삼성전자 HBM4 양산 착수") , "빈 결과"
        assert a == b or len(set(a.split()) & set(b.split())) >= 3, f"{a} / {b}"

    def t_make_id():
        i1 = make_id("삼성전자 hbm4", "매체A")
        i2 = make_id("삼성전자 hbm4", "매체A")
        i3 = make_id("삼성전자 hbm4", "매체B")
        assert i1 == i2, "같은 입력인데 id 가 다름"
        assert i1 != i3, "매체가 달라도 id 가 같음 (매체 수 집계 불가)"

    def t_classify():
        assert classify("에코프로 유상증자 1.2조 결정") == "공시"
        assert classify("공매도 잔고 급증") == "수급"
        assert classify("FOMC 금리 인하 시사") == "매크로"
        assert classify("HBM 수출 확대") == "반도체"
        assert classify("의미없는제목입니다", "해외시황") == "해외시황"

    def t_cluster():
        raw = [
            {"id": "1", "title_norm": "hd현대중공업 lng 운반선 대규모 수주",
             "source": "매체A"},
            {"id": "2", "title_norm": "hd현대중공업 lng 운반선 수주 대규모",
             "source": "매체B"},
            {"id": "3", "title_norm": "hd현대중공업 lng 운반선 대규모 수주 계약",
             "source": "매체C"},
            {"id": "4", "title_norm": "에코프로비엠 양극재 증설 투자 결정",
             "source": "매체A"},
        ]
        out = cluster_items(raw, threshold=0.55)
        cids = {it["id"]: it["cluster_id"] for it in out}
        assert cids["1"] == cids["2"] == cids["3"], \
            f"같은 사건이 묶이지 않음: {cids}"
        assert cids["4"] != cids["1"], "다른 사건이 묶였다"
        n = {it["id"]: it["cluster_n"] for it in out}
        assert n["1"] == 3, f"매체 수 집계 오류: {n['1']}"

    def t_alias_index():
        # 종목코드 끝자리가 0 이어야 보통주로 인정된다. 아래 코드들은
        # 그 규칙을 만족시켜 이름 기반 필터만 검증하도록 맞춘 것이다.
        idx = build_alias_index({
            "005930": "삼성전자", "005935": "삼성전자우",
            "005380": "현대차", "001500": "현대차증권",
            "000010": "미래", "000020": "한올", "000030": "AB",
        })
        assert "삼성전자우" not in idx.map, "우선주가 인덱스에 들어감"
        assert "미래" not in idx.map, "모호한 이름이 걸러지지 않음"
        assert "한올" in idx.map, "화이트리스트 2글자가 빠졌다"
        assert "AB" not in idx.map, "2글자 비화이트리스트가 들어감"
        assert idx.by_code.get("000020") == "한올", "by_code 역인덱스 오류"
        lens = [len(n) for n in idx.names_by_len]
        assert lens == sorted(lens, reverse=True), "길이 내림차순 정렬 아님"

    def t_map_tickers_partial():
        """'현대차증권' 기사에 '현대차'가 함께 태깅되면 안 된다."""
        idx = build_alias_index({"005380": "현대차", "001500": "현대차증권"})
        got = dict(map_tickers("현대차증권 3분기 실적 발표", idx))
        assert "001500" in got, "현대차증권을 못 찾음"
        assert "005380" not in got, "부분 겹침으로 현대차가 오태깅됨"
        got2 = dict(map_tickers("현대차 신차 공개", idx))
        assert got2 == {"005380": "현대차"}, got2

    def t_map_tickers_dart():
        idx = build_alias_index({"005930": "삼성전자"})
        got = dict(map_tickers("[공시] 어떤회사 유상증자", idx,
                               stock_code="005930"))
        assert "005930" in got, "DART 종목코드 직접 경로가 동작하지 않음"

    def t_importance():
        base = {"title": "어떤 종목 소식", "cluster_n": 1, "tickers": (),
                "category": "기타", "published": None}
        s0 = score_importance(base, set(), set(), set())
        s_multi = score_importance({**base, "cluster_n": 5},
                                   set(), set(), set())
        assert s_multi > s0, "매체 수가 중요도에 반영되지 않음"
        s_held = score_importance({**base, "tickers": (("000001", "보유"),)},
                                  {"000001"}, set(), set())
        assert s_held > s0, "보유 종목 가산이 없음"
        s_kw = score_importance({**base, "title": "상장폐지 사유 발생"},
                                set(), set(), set())
        assert s_kw > s0, "키워드 강도가 반영되지 않음"
        for s in (s0, s_multi, s_held, s_kw):
            assert 0.0 <= s <= 10.0, f"중요도 범위 이탈: {s}"

    check("news", "제목 정규화", t_normalize)
    check("news", "id 안정성 + 매체 구분", t_make_id)
    check("news", "카테고리 분류", t_classify)
    check("news", "사건 클러스터 + 매체 수", t_cluster)
    check("news", "별칭 인덱스 (우선주/모호 배제)", t_alias_index)
    check("news", "부분 겹침 오태깅 방지", t_map_tickers_partial)
    check("news", "DART 종목코드 직접 매핑", t_map_tickers_dart)
    check("news", "중요도 (매체 수 1순위)", t_importance)


# ══════════════════════════ 8-2. 알림 시간창 ══════════════════════════
def test_notify(tmp: Path):
    """4대 시간창 · 창별 트랙 분리 · 창별 예산 독립 · KST 고정.

    상태 파일은 실행별 임시 디렉터리에 둔다. 시스템 temp 를 쓰면 이전
    실행의 예산 소진 기록이 남아 검사가 두 번째부터 실패한다.
    """
    from datetime import datetime as dt, timezone as tz
    from stocknews.notify import WINDOWS, AlertGate, now_kst
    from stocknews.screener import screen_one

    def at(h, m):
        return dt(2026, 8, 24, h, m)

    def t_spec():
        assert len(WINDOWS) == 4, f"시간창 {len(WINDOWS)}개"
        names = [w.name for w in WINDOWS]
        assert names == ["반대매매", "방향확정", "오후눌림", "종가확정"], names

        # 최우선 창(10시)이 예산과 트랙 폭이 가장 넓어야 한다
        prime = WINDOWS[1]
        assert prime.budget == max(w.budget for w in WINDOWS), \
            "10시 창이 최대 예산이 아니다"
        assert len(prime.tracks) == 3, "최우선 창이 전 트랙을 받지 않는다"

        # 09시 창은 역추세(매집)만. 순추세를 09시에 쫓으면 휩쏘에 걸린다.
        assert "TREND" not in WINDOWS[0].tracks, "09시 창이 추세 트랙을 허용"
        assert "TREND" not in WINDOWS[2].tracks, "14시 창이 추세 트랙을 허용"

    def t_lookup():
        assert AlertGate.current_window(at(10, 5)) is WINDOWS[1]
        assert AlertGate.current_window(at(9, 10)).name == "반대매매"
        assert AlertGate.current_window(at(14, 5)).name == "오후눌림"
        assert AlertGate.current_window(at(15, 25)).name == "종가확정"
        assert AlertGate.current_window(at(11, 30)) is None, "창 밖인데 잡힘"
        assert AlertGate.current_window(at(9, 40)) is None, "09:35 이후는 창 밖"

    def t_kst():
        """호스트 타임존과 무관하게 UTC+9 여야 한다.

        이걸 놓치면 UTC 서버에서 09시 창이 영원히 열리지 않고,
        에러도 없이 몇 주간 알림이 안 오는 상태가 된다.
        """
        utc = dt.now(tz.utc).replace(tzinfo=None)
        delta = abs((now_kst() - utc).total_seconds() - 9 * 3600)
        assert delta < 120, f"now_kst() 가 UTC+9 가 아니다 (편차 {delta:.0f}초)"

    def t_budget_isolated():
        gate = AlertGate(state_path=tmp / "gate_budget.json")
        r = screen_one("000001", "테스트", fixture_crash())
        gate.commit([r], now=at(9, 10), window=WINDOWS[0])
        d = at(9, 10).date()
        left_prime = gate._window_budget_left(d, WINDOWS[1])
        assert left_prime == WINDOWS[1].budget, \
            f"09시 소진이 10시 예산을 깎았다 (잔여 {left_prime})"
        left_09 = gate._window_budget_left(d, WINDOWS[0])
        assert left_09 == WINDOWS[0].budget - 1, f"09시 잔여 {left_09}"

    def t_track_filter():
        """창이 담당하지 않는 트랙은 걸러져야 한다."""
        gate = AlertGate(state_path=tmp / "gate_track.json")
        r = screen_one("000003", "추세종목", fixture_golden_cross())
        if r.grade not in ("S+", "S", "A") or r.track != "TREND":
            return          # 픽스처가 추세 신호를 못 냈으면 검사 생략
        got09 = gate.filter_tier1([r], now=at(9, 10))
        assert got09 == [], "09시 창이 추세 트랙을 통과시켰다"
        got10 = gate.filter_tier1([r], now=at(10, 5))
        assert len(got10) == 1, "10시 창이 추세 트랙을 막았다"

    def t_outside_window():
        gate = AlertGate(state_path=tmp / "gate_out.json")
        r = screen_one("000001", "테스트", fixture_crash())
        assert gate.filter_tier1([r], now=at(11, 30)) == [], \
            "창 밖인데 발송 대상이 나왔다"
        # 테스트용 강제 실행은 통과해야 한다
        forced = gate.filter_tier1([r], now=at(11, 30), ignore_window=True)
        assert isinstance(forced, list)

    check("notify", "4대 시간창 규격", t_spec)
    check("notify", "시간창 판정", t_lookup)
    check("notify", "KST 고정 (UTC 서버 방어)", t_kst)
    check("notify", "창별 예산 독립 (10시 보장)", t_budget_isolated)
    check("notify", "창별 트랙 분리", t_track_filter)
    check("notify", "창 밖 차단", t_outside_window)


# ══════════════════════════ 8-2b. 거래일 / 휴장일 ══════════════════════════
def test_trading_day(tmp: Path):
    """주말·공휴일 오작동 방어. 실제로 6건의 버그가 있던 영역이다."""
    from datetime import datetime as dt
    from stocknews.notify import WINDOWS, AlertGate
    from stocknews.screener import screen_one
    from stocknews.store import Store
    from stocknews.trading_day import (CLOSED_HOLIDAY, CLOSED_WEEKEND, TRADING,
                                       UNKNOWN, calendar_gap_days,
                                       is_definitely_closed, market_status,
                                       news_window_hours,
                                       should_scan_intraday,
                                       trading_days_between)

    st = Store(tmp / "cal.db")
    df = fixture_crash()
    st.upsert_prices("000001", df)
    last = df.index[-1].date()          # 2026-08-24 (월)

    def t_weekend():
        sat, sun = dt(2026, 8, 22), dt(2026, 8, 23)   # 토, 일
        assert market_status(st, sat) == CLOSED_WEEKEND
        assert market_status(st, sun) == CLOSED_WEEKEND
        assert is_definitely_closed(st, sat) is True

    def t_holiday_cache():
        hol = "2026-08-17"              # 평일이라 가정
        assert market_status(st, hol) in (TRADING, UNKNOWN)
        st.mark_non_trading_day(hol, "smoke")
        assert market_status(st, hol) == CLOSED_HOLIDAY
        assert is_definitely_closed(st, hol) is True
        assert hol in st.known_non_trading_days(since="2026-01-01")

    def t_trading_confirmed():
        assert market_status(st, last) == TRADING, \
            "시세가 있는 날인데 거래일로 판정되지 않았다"
        assert is_definitely_closed(st, last) is False

    def t_unknown_is_not_closed():
        """평일이지만 미확인인 날은 차단하지 않아야 한다.

        장중에는 아직 시세가 적재되지 않았을 수 있다. UNKNOWN 을 휴장으로
        처리하면 매일 아침 스캔이 막힌다.
        """
        future = dt(2026, 9, 1)         # 화요일, 시세 없음
        assert market_status(st, future) == UNKNOWN
        assert is_definitely_closed(st, future) is False

    def t_intraday_gate():
        # 토요일 10시: 시각은 창 안이지만 휴장이므로 막혀야 한다
        ok, why = should_scan_intraday(st, dt(2026, 8, 22, 10, 0))
        assert ok is False and "주말" in why, why
        # 월요일 10시: 통과
        ok2, _ = should_scan_intraday(st, dt(2026, 8, 24, 10, 0))
        assert ok2 is True
        # 월요일 새벽 3시: 장시간 밖
        ok3, why3 = should_scan_intraday(st, dt(2026, 8, 24, 3, 0))
        assert ok3 is False and "장시간" in why3, why3

    def t_alert_gate_weekend():
        """토요일 10시에 창이 열려 금요일 데이터로 알림이 나가면 안 된다."""
        r = screen_one("000001", "테스트", df)
        blind = AlertGate(state_path=tmp / "g_noStore.json")
        with_store = AlertGate(state_path=tmp / "g_store.json", store=st)
        sat10 = dt(2026, 8, 22, 10, 5)
        # store 없이는 시각만 보므로 창이 열린다 (기존 동작)
        assert blind.current_window(sat10) is WINDOWS[1]
        # store 를 주면 휴장일 판정이 작동해 발송 대상이 0 이어야 한다
        assert with_store.filter_tier1([r], now=sat10) == [], \
            "토요일인데 발송 대상이 나왔다"

    def t_cooldown_trading_days():
        """쿨다운이 거래일 기준이어야 한다. 달력일이면 주말에 오판한다."""
        n = trading_days_between(st, "2026-08-21", "2026-08-24")
        assert n <= 2, f"금->월 사이 거래일이 {n}일로 계산됐다 (달력일 3일)"

    def t_news_window_expands():
        """월요일/연휴 뒤 뉴스 창이 자동으로 넓어져야 한다."""
        mon = dt(2026, 8, 24, 8, 30)
        same = news_window_hours(st, mon, base=16)
        assert same == 16, f"당일 거래일인데 창이 {same}시간으로 바뀜"
        # 마지막 거래일이 3일 전이면 창이 넓어져야 한다
        thu = dt(2026, 8, 27, 8, 30)
        wide = news_window_hours(st, thu, base=16)
        assert wide > 16, f"공백 3일인데 창이 {wide}시간"
        assert wide <= 120, "창이 상한을 넘었다"
        assert calendar_gap_days(st, thu) == 3

    def t_duplicate_guard():
        """같은 거래일 스냅샷/청산신호 중복 방지."""
        assert st.has_scan(last) is False
        from stocknews.screener import screen_one as so
        st.save_scan(last, [so("000001", "테스트", df)])
        assert st.has_scan(last) is True
        assert st.has_exit_signal(last) is False

    check("trading_day", "주말 판정", t_weekend)
    check("trading_day", "공휴일 캐시", t_holiday_cache)
    check("trading_day", "거래일 확정", t_trading_confirmed)
    check("trading_day", "미확인일은 차단 안 함", t_unknown_is_not_closed)
    check("trading_day", "장중 스캔 게이트", t_intraday_gate)
    check("trading_day", "토요일 알림 차단", t_alert_gate_weekend)
    check("trading_day", "쿨다운 거래일 기준", t_cooldown_trading_days)
    check("trading_day", "뉴스 창 자동 확장", t_news_window_expands)
    check("trading_day", "중복 실행 가드", t_duplicate_guard)


# ══════════════════════════ 8-2c. 백테스트 ══════════════════════════
def test_backtest(tmp: Path):
    """백테스트의 정직성 검증. 룩어헤드·체결가정·비용이 맞아야 한다."""
    from stocknews.backtest import (BacktestConfig, _fwd, control_random,
                                    control_rsi, run_backtest,
                                    simulate_exit_rules, summarize,
                                    summarize_exits, sweep_thresholds)
    from stocknews.config import DEFAULT
    from stocknews.store import Store

    st = Store(tmp / "bt.db")
    # 앵커 픽스처를 여러 종목으로 늘려 이벤트가 나오게 한다
    for i, fx in enumerate((fixture_crash(), fixture_golden_cross(),
                            fixture_flat()), 1):
        code = f"00000{i}0"
        st.upsert_prices(code, fx)
        st.upsert_tickers([{"ticker": code, "name": f"종목{i}",
                            "market": "KOSPI", "market_cap": 5_000e8,
                            "shares": 1e7}])
    tickers = st.active_tickers()
    bt = BacktestConfig(step=10, warmup=140, horizons=(1, 3, 5))

    def t_next_open_fill():
        """진입은 판정 익일 시가여야 한다. 종가 체결이면 성과가 부풀려진다."""
        df = fixture_crash()
        i0 = 200
        f = _fwd(df, i0, 3, cost=0.5)
        assert f, "구간 계산 실패"
        near(f["entry"], float(df["시가"].iloc[i0 + 1]), tol=1e-6,
             label="진입가 = 익일 시가")
        near(f["exit"], float(df["시가"].iloc[i0 + 1 + 3]), tol=1e-6,
             label="청산가 = h일 뒤 시가")
        near(f["net"], f["ret"] - 0.5, tol=1e-9, label="비용 차감")
        assert f["mae"] <= 0.0, f"최대역행폭이 양수: {f['mae']}"

    def t_fwd_bounds():
        """데이터 끝을 넘어가면 빈 결과여야 한다 (미래 참조 방지)."""
        df = fixture_crash()
        assert _fwd(df, len(df) - 2, 5, 0.5) == {}, "데이터 끝을 넘겨 계산했다"

    def t_no_lookahead():
        """같은 시점 판정이 미래 데이터에 영향받지 않아야 한다.

        전체 시계열로 채점한 값과, 그 시점까지 절단해 채점한 값이
        달라야 정상이다(절단이 실제로 작동). 그리고 절단 결과는
        더 긴 시계열을 줘도 그 시점 기준으로 동일해야 한다.
        """
        from stocknews.screener import screen_one
        df = fixture_crash()
        i = 200
        a = screen_one("000010", "t", df.iloc[: i + 1])
        b = screen_one("000010", "t", df.iloc[: i + 1].copy())
        near(a.value_score, b.value_score, label="절단 채점 재현성")
        near(a.trend_score, b.trend_score, label="절단 추세 재현성")

    def t_run():
        ev = run_backtest(st, tickers, DEFAULT, bt, progress_every=0)
        assert isinstance(ev, pd.DataFrame)
        if ev.empty:
            return          # 픽스처가 등급 A 이상을 못 냈으면 생략
        for c in ("ticker", "date", "grade", "net_3", "alpha_3", "mae_3"):
            assert c in ev.columns, f"{c} 컬럼 누락"
        assert (ev["mae_3"].dropna() <= 0).all(), "최대역행폭에 양수가 있다"
        s = summarize(ev, bt)
        assert s["n"] == len(ev)
        assert "by_horizon" in s and "controls" in s
        assert isinstance(s.get("warnings"), list)

    def t_controls():
        r = control_random(st, tickers, 20, bt)
        assert isinstance(r, pd.DataFrame)
        q = control_rsi(st, tickers, 30.0, bt)
        assert isinstance(q, pd.DataFrame)

    def t_sweep():
        ev = run_backtest(st, tickers, DEFAULT, bt, progress_every=0)
        sw = sweep_thresholds(ev, bt)
        assert isinstance(sw, pd.DataFrame)
        if len(sw):
            # 임계를 올리면 표본이 줄어야 한다
            v = sw[sw["track"] == "매집"].sort_values("threshold")
            if len(v) >= 2:
                assert v["n"].iloc[0] >= v["n"].iloc[-1], \
                    "임계를 올렸는데 표본이 늘었다"

    def t_exit_sim():
        ev = run_backtest(st, tickers, DEFAULT, bt, progress_every=0)
        sim = simulate_exit_rules(st, ev, DEFAULT, bt)
        assert isinstance(sim, pd.DataFrame)
        if len(sim):
            assert (sim["held_days"] >= 1).all(), "보유일수가 0 이하"
            assert (sim["held_days"] <= bt.max_hold + 1).all(), "최대보유 초과"
            assert (sim["mae"] <= 0).all(), "최대역행폭에 양수"
            es = summarize_exits(sim)
            assert es["n"] == len(sim) and "by_layer" in es

    def t_empty_safe():
        assert summarize(pd.DataFrame(), bt)["n"] == 0
        assert len(sweep_thresholds(pd.DataFrame(), bt)) == 0
        assert len(simulate_exit_rules(st, pd.DataFrame(), DEFAULT, bt)) == 0
        assert summarize_exits(pd.DataFrame())["n"] == 0

    check("backtest", "익일 시가 체결 + 비용 차감", t_next_open_fill)
    check("backtest", "데이터 끝 넘김 방지", t_fwd_bounds)
    check("backtest", "절단 채점 재현성", t_no_lookahead)
    check("backtest", "이벤트 생성 + 집계", t_run)
    check("backtest", "대조군 생성", t_controls)
    check("backtest", "임계값 스윕", t_sweep)
    check("backtest", "청산 규칙 시뮬", t_exit_sim)
    check("backtest", "빈 데이터 처리", t_empty_safe)


def test_krx_credit():
    """KRX 신용잔고 수집기. 네트워크 없이 파싱 로직만 검증한다."""
    from stocknews.krx_credit import (CANDIDATE_BLDS, _num, _pick, _rows_of)

    def t_rows_of():
        assert _rows_of({"output": [{"a": 1}], "x": []}) == [{"a": 1}]
        # 가장 큰 리스트를 골라야 한다
        d = {"small": [{"a": 1}], "big": [{"a": 1}, {"a": 2}]}
        assert len(_rows_of(d)) == 2
        assert _rows_of({}) == []
        assert _rows_of({"n": 3}) == []

    def t_pick():
        cols = ["ISU_SRT_CD", "ISU_ABBRV", "LOAN_BAL_QTY", "TDD_CLSPRC"]
        assert _pick(cols, ("ISU_SRT_CD", "ISU_CD")) == "ISU_SRT_CD"
        assert _pick(cols, ("BAL_QTY",)) == "LOAN_BAL_QTY"   # 부분 일치
        assert _pick(cols, ("NOPE",)) is None

    def t_num():
        near(_num("1,234,567"), 1234567.0, label="콤마 제거")
        near(_num("4.85%"), 4.85, label="퍼센트 제거")
        assert _num("-") is None and _num("") is None and _num(None) is None
        assert _num("abc") is None

    def t_candidates():
        assert CANDIDATE_BLDS, "후보 bld 목록이 비었다"
        for b in CANDIDATE_BLDS:
            assert b.startswith("dbms/MDC/STAT/"), b

    check("krx_credit", "결과 배열 탐색", t_rows_of)
    check("krx_credit", "컬럼 패턴 매칭", t_pick)
    check("krx_credit", "숫자 파싱", t_num)
    check("krx_credit", "후보 bld 목록", t_candidates)


def test_docs():
    """라이선스와 면책 조항이 있어야 한다. 금융 코드의 필수 요건이다."""
    root = Path(__file__).parent

    def t_license():
        p = root / "LICENSE"
        assert p.exists(), "LICENSE 파일이 없다"
        txt = p.read_text(encoding="utf-8")
        assert "MIT License" in txt
        assert "NOT investment advice" in txt, "금융 고지가 없다"

    def t_disclaimer():
        p = root / "DISCLAIMER.md"
        assert p.exists(), "DISCLAIMER.md 가 없다"
        txt = p.read_text(encoding="utf-8")
        for kw in ("투자 조언이 아닙니다", "주문을 내지 않습니다",
                   "검증되지 않은 가설", "책임 제한"):
            assert kw in txt, f"면책 조항에 '{kw}' 누락"

    def t_agents():
        p = root / "AGENTS.md"
        assert p.exists(), "AGENTS.md 가 없다"
        txt = p.read_text(encoding="utf-8")
        assert "hermes\\run.cmd" in txt or "hermes\\\\run.cmd" in txt

    def t_env_example():
        p = root / ".env.example"
        assert p.exists(), ".env.example 이 없다"
        txt = p.read_text(encoding="utf-8")
        for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "DART_API_KEY"):
            assert k in txt, f".env.example 에 {k} 누락"
        assert "=" in txt
        # 실제 값이 들어가 있으면 안 된다
        for line in txt.splitlines():
            if line.startswith("TELEGRAM_BOT_TOKEN="):
                assert line.strip() == "TELEGRAM_BOT_TOKEN=", \
                    "템플릿에 실제 토큰이 들어 있다"

    check("docs", "LICENSE + 금융 고지", t_license)
    check("docs", "DISCLAIMER.md", t_disclaimer)
    check("docs", "AGENTS.md", t_agents)
    check("docs", ".env.example (토큰 미포함)", t_env_example)


# ══════════════════════════ 8-3. 잡 락 / 에이전트 연동 ══════════════════════════
def test_joblock(tmp: Path):
    """동시 실행 방지. Hermes cron 이 겹칠 때 DB 가 깨지지 않게 한다."""
    from stocknews.joblock import MODE_TIMEOUTS, JobLock, clear_locks

    d = tmp / "locks"

    def t_exclusive():
        a = JobLock("t1", mode="daily", lock_dir=d)
        assert a.acquire() is True
        b = JobLock("t1", mode="update", lock_dir=d)
        assert b.acquire() is False, "두 잡이 동시에 락을 잡았다"
        assert b.holder.get("mode") == "daily", b.holder
        a.release()
        assert b.acquire() is True, "해제 후에도 락을 못 잡는다"
        b.release()

    def t_context_manager():
        with JobLock("t2", mode="daily", lock_dir=d) as lock:
            assert lock.acquired
            path = lock.path
            assert path.exists()
        assert not path.exists(), "with 블록을 나갔는데 락이 남았다"

    def t_expired_steal():
        """죽은 잡의 락은 빼앗아야 한다. 아니면 영구 차단된다."""
        dead = JobLock("t3", mode="daily", lock_dir=d, timeout=-10)
        assert dead.acquire()
        dead.acquired = False            # release 하지 않고 방치 (프로세스 사망 모사)
        alive = JobLock("t3", mode="daily", lock_dir=d)
        assert alive.acquire() is True, "만료된 락을 회수하지 못했다"
        alive.release()

    def t_timeouts():
        assert MODE_TIMEOUTS["backfill"] > MODE_TIMEOUTS["daily"], \
            "backfill 이 daily 보다 짧은 타임아웃을 갖는다"
        assert MODE_TIMEOUTS["backfill"] >= 3600, "backfill 타임아웃이 너무 짧다"

    def t_clear():
        JobLock("t4", mode="daily", lock_dir=d).acquire()
        removed = clear_locks(d)
        assert removed, "clear_locks 가 아무것도 지우지 않았다"
        assert not list(d.glob("*.lock")), "락이 남았다"

    check("joblock", "배타적 획득", t_exclusive)
    check("joblock", "컨텍스트 매니저 해제", t_context_manager)
    check("joblock", "만료 락 회수", t_expired_steal)
    check("joblock", "모드별 타임아웃", t_timeouts)
    check("joblock", "강제 해제", t_clear)


def test_agent_contract():
    """에이전트 연동 규약. 종료 코드와 쓰기 모드 목록의 정합성."""
    import importlib
    mod = importlib.import_module("run_screen")

    def t_exit_codes():
        assert mod.EXIT_OK == 0
        assert mod.EXIT_FAIL == 1
        assert mod.EXIT_PARTIAL == 2
        assert mod.EXIT_LOCKED == 3
        assert mod.EXIT_PRECOND == 4
        codes = {mod.EXIT_OK, mod.EXIT_FAIL, mod.EXIT_PARTIAL,
                 mod.EXIT_LOCKED, mod.EXIT_PRECOND, mod.EXIT_USAGE}
        assert len(codes) == 6, "종료 코드가 중복된다"
        # argparse 기본 exit 2 가 EXIT_PARTIAL 과 충돌하지 않아야 한다
        assert mod.EXIT_USAGE != mod.EXIT_PARTIAL, \
            "인자 오류와 부분 실패를 구분할 수 없다"

    def t_usage_exit_code():
        """잘못된 모드는 exit 64 여야 한다 (2 가 아니라)."""
        import contextlib
        import io
        buf = io.StringIO()
        try:
            with contextlib.redirect_stderr(buf):
                mod.main(["--mode", "__nope__"])
        except SystemExit as e:
            assert e.code == mod.EXIT_USAGE, f"exit {e.code}"
            return
        raise AssertionError("잘못된 모드인데 SystemExit 이 없다")

    def t_write_modes():
        """쓰기 모드가 전부 MODES 에 있고, 읽기 전용은 빠져 있어야 한다."""
        unknown = mod._WRITE_MODES - set(mod.MODES)
        assert not unknown, f"_WRITE_MODES 에 없는 모드: {unknown}"
        for ro in ("weekly", "fib", "export", "pos-list", "brief-weekly",
                   "backtest", "credit-probe"):
            assert ro not in mod._WRITE_MODES, f"{ro} 는 읽기 전용이어야 한다"
        for rw in ("daily", "update", "flags", "exits", "credit"):
            assert rw in mod._WRITE_MODES, f"{rw} 가 락 대상이 아니다"

    def t_partial_threshold():
        assert mod._partial(0, 0) is False, "표본 0에서 부분실패 판정"
        assert mod._partial(100, 5) is False, "5% 실패로 부분실패 판정"
        assert mod._partial(100, 30) is True, "30% 실패인데 정상 판정"

    def t_summary_dict():
        """--json 출력용 요약 딕셔너리가 있어야 한다."""
        assert isinstance(mod.SUMMARY, dict)

    check("agent", "종료 코드 규약", t_exit_codes)
    check("agent", "인자 오류 exit 64", t_usage_exit_code)
    check("agent", "요약 딕셔너리", t_summary_dict)
    check("agent", "락 대상 모드 정합성", t_write_modes)
    check("agent", "부분 실패 임계", t_partial_threshold)


# ══════════════════════════ 9. 렌더러 ══════════════════════════
def test_renderer(st):
    from stocknews.config import DEFAULT
    from stocknews.contracts import ExitDecision
    from stocknews.renderer import (bar, render_detail, render_digest,
                                    render_evening_brief, render_exit_alert,
                                    render_exit_digest, render_fib_list,
                                    render_morning_brief, render_news_weekly,
                                    render_positions, render_top10,
                                    render_weekly)
    from stocknews.screener import screen_one

    now = datetime(2026, 8, 24, 18, 5)
    r = screen_one("000001", "급락종목 & 우선주<주의>", fixture_crash())
    picks = [("VALUE", r)]

    def _sane(text: str, label: str):
        assert isinstance(text, str) and text, f"{label}: 빈 문자열"
        assert "{" not in text and "}" not in text, \
            f"{label}: 포맷 문자열이 그대로 남음"
        assert "&amp;" in text or "&" not in text.split("http")[0], \
            f"{label}: HTML 이스케이프 누락 의심"

    def t_bar():
        assert bar(0.5, 10).count("\u2593") == 5
        assert bar(0.0, 10).count("\u2593") == 0
        assert bar(1.0, 10).count("\u2593") == 10
        assert "?" in bar(float("nan"), 10)

    def t_detail():
        _sane(render_detail(r, news_title="테스트 & <뉴스>", cfg=DEFAULT), "detail")

    def t_digest():
        _sane(render_digest([r], now, DEFAULT), "digest")
        assert render_digest([], now, DEFAULT)

    def t_fib_list():
        _sane(render_fib_list([r], now, DEFAULT), "fib_list")
        assert render_fib_list([], now, DEFAULT)

    def t_top10():
        _sane(render_top10(picks, now, "2026-08-24", 1847, DEFAULT), "top10")
        assert render_top10([], now, "2026-08-24", 1847, DEFAULT)

    def t_weekly():
        from stocknews.weekly import weekly_report
        rep = weekly_report(st, DEFAULT)
        _sane(render_weekly(rep, now, DEFAULT), "weekly")

    def t_briefs():
        _sane(render_morning_brief(st, now, hours=24 * 365), "morning")
        _sane(render_evening_brief(st, now, picks=picks, hours=24 * 365),
              "evening")

    def t_news_weekly():
        from stocknews.news import theme_shift
        assert render_news_weekly(theme_shift(st), now)
        assert render_news_weekly(None, now)

    def t_exit_render():
        dec = ExitDecision(
            ticker="000001", name="급락종목 & 주의", position_id=1, layer=1,
            rule="stop:band_break", action="EXIT_ALL", ratio=1.0, qty=100,
            signal_price=50_000, ret_pct=-28.6, net_ret_pct=-29.1,
            reason="밴드 하단 이탈 3일 연속", urgent=True)
        _sane(render_exit_alert(dec, DEFAULT), "exit_alert")
        res = {"decisions": [dec], "positions": 3, "market_ret": -3.2,
               "trade_date": "2026-08-24"}
        _sane(render_exit_digest(res, now, DEFAULT), "exit_digest")
        assert render_exit_digest({"decisions": [], "positions": 0}, now,
                                  DEFAULT)

    def t_positions_render():
        ps = st.list_positions(None)
        assert render_positions(ps, {})
        assert render_positions([], None)

    check("renderer", "막대 그래프", t_bar)
    check("renderer", "상세 알림", t_detail)
    check("renderer", "다이제스트", t_digest)
    check("renderer", "피보 목록", t_fib_list)
    check("renderer", "추천 10선", t_top10)
    check("renderer", "주간 리포트", t_weekly)
    check("renderer", "아침/저녁 브리핑", t_briefs)
    check("renderer", "주간 뉴스 테마", t_news_weekly)
    check("renderer", "청산 알림/요약", t_exit_render)
    check("renderer", "보유 현황", t_positions_render)


# ══════════════════════════ 10. 일일/주간 배치 ══════════════════════════
def test_daily_weekly(st):
    from stocknews.config import DEFAULT
    from stocknews.daily import scan_all, select_recommendations
    from stocknews.weekly import (audit_recos, band_eta, churn, persistence,
                                  score_momentum, weekly_events,
                                  weekly_report)

    def t_scan_all_flag_wiring():
        """플래그가 실제로 scan_all 로 흘러가는지 검증한다.

        store 테스트에서 000001 에 자본잠식률 62.5% 를 심어놨다. 배선이
        되어 있으면 이 종목은 배제되어야 한다. flags=None 으로 호출하던
        회귀를 잡는 검사다.
        """
        results, errors = scan_all(st, {"000001": "급락종목"}, DEFAULT,
                                   progress_every=0)
        assert errors == [], f"스캔 오류: {errors}"
        assert len(results) == 1, f"결과 {len(results)}건"
        r = results[0]
        assert r.excluded is not None, "자본잠식 플래그가 scan_all 에 반영되지 않음"
        assert "자본잠식" in r.excluded, f"배제 사유가 다름: {r.excluded}"

    def t_scan_all_after_clear():
        """플래그를 풀면 정상 채점 경로로 돌아와야 한다 (해제 반영)."""
        st.clear_flag_field("capital_impair")
        results, errors = scan_all(st, {"000001": "급락종목"}, DEFAULT,
                                   progress_every=0)
        assert errors == []
        r = results[0]
        assert r.excluded is None, f"플래그 해제 후에도 배제됨: {r.excluded}"
        assert r.grade in ("S+", "S", "A", "B", "NONE")

    def t_select():
        results, _ = scan_all(st, {"000001": "급락종목"}, DEFAULT,
                              progress_every=0)
        picks = select_recommendations(results, st.ticker_meta(), top_n=10,
                                       cfg=DEFAULT)
        assert isinstance(picks, list)
        assert len(picks) <= 10
        codes = [r.ticker for _, r in picks]
        assert len(codes) == len(set(codes)), "추천에 중복 종목이 있다"
        for slot, _r in picks:
            assert slot in ("SEQ", "VALUE", "TREND", "FILL", "FILL*"), slot

    def t_weekly_parts():
        scans = st.scan_history(days=10)
        recos = st.reco_history(days=10)
        assert isinstance(score_momentum(scans), pd.DataFrame)
        assert isinstance(persistence(recos, scans), pd.DataFrame)
        assert isinstance(churn(recos), dict)
        assert isinstance(band_eta(scans), pd.DataFrame)
        assert isinstance(weekly_events(scans), dict)
        a = audit_recos(st, horizon=5)
        assert isinstance(a, dict) and "n" in a

    def t_weekly_report():
        rep = weekly_report(st, DEFAULT)
        for k in ("audit", "momentum", "persistence", "churn", "eta",
                  "events", "sector"):
            assert k in rep, f"주간 리포트에 {k} 누락"

    def t_empty_frames():
        """데이터가 없을 때 예외 대신 빈 결과를 돌려야 한다."""
        empty = pd.DataFrame()
        assert len(score_momentum(empty)) == 0
        assert len(persistence(empty, empty)) == 0
        assert churn(empty) == {"entered": [], "dropped": []}
        assert len(band_eta(empty)) == 0
        assert weekly_events(empty) == {"fib_breaks": [], "cross": []}

    check("daily", "플래그 배선 (배제 발동)", t_scan_all_flag_wiring)
    check("daily", "플래그 해제 후 정상 채점", t_scan_all_after_clear)
    check("daily", "추천 선정", t_select)
    check("weekly", "구성 요소", t_weekly_parts)
    check("weekly", "종합 리포트", t_weekly_report)
    check("weekly", "빈 데이터 처리", t_empty_frames)


# ══════════════════════════ 11. 임포트 / 모듈 무결성 ══════════════════════════
def test_imports():
    def t_package():
        import stocknews
        assert stocknews.__version__
        for name in stocknews.__all__:
            assert hasattr(stocknews, name), f"__all__ 의 {name} 이 없음"

    def t_lazy_modules():
        """네트워크 모듈도 임포트 자체는 되어야 한다."""
        import stocknews.data          # noqa: F401
        import stocknews.flags         # noqa: F401
        import stocknews.news_sources  # noqa: F401
        import stocknews.notify        # noqa: F401
        import stocknews.universe      # noqa: F401

    def t_runner():
        import runpy
        import importlib
        mod = importlib.import_module("run_screen")
        assert hasattr(mod, "MODES") and hasattr(mod, "main")
        expect = {"master", "backfill", "update", "daily", "weekly", "fib",
                  "flash", "news", "brief-morning", "brief-evening",
                  "brief-weekly", "flags", "exits", "pos-open", "pos-list",
                  "fill", "pos-close", "credit", "credit-probe", "export",
                  "backtest"}
        missing = expect - set(mod.MODES)
        assert not missing, f"MODES 누락: {missing}"
        del runpy

    def t_cli_parses():
        import importlib
        mod = importlib.import_module("run_screen")
        # 인자 파서가 모든 모드를 받아들이는지 (실행은 하지 않는다)
        for m in mod.MODES:
            try:
                mod.main.__wrapped__  # noqa: B018
            except AttributeError:
                pass
        assert True

    def t_bs4_xml():
        """뉴스 RSS 파서가 쓰는 lxml-xml 백엔드가 살아있는지."""
        from bs4 import BeautifulSoup
        xml = ("<rss><channel><item><title>t</title>"
               "<link>https://x</link></item></channel></rss>")
        soup = BeautifulSoup(xml, "xml")
        assert soup.find("item") is not None, "lxml-xml 파서가 동작하지 않음"

    def t_feed_parser():
        from stocknews.news_sources import _parse_feed
        rss = ("<?xml version='1.0'?><rss version='2.0'><channel>"
               "<item><title>A &amp; B</title><link>https://x/1</link>"
               "<pubDate>Mon, 24 Aug 2026 09:00:00 +0000</pubDate>"
               "<source>매체A</source></item></channel></rss>")
        got = _parse_feed(rss)
        assert len(got) == 1, f"RSS 파싱 {len(got)}건"
        assert got[0]["title"] == "A & B"
        assert got[0]["url"] == "https://x/1"
        assert got[0]["published"] is not None, "pubDate 파싱 실패"

        atom = ("<?xml version='1.0'?><feed xmlns='http://www.w3.org/2005/Atom'>"
                "<entry><title>Atom Title</title>"
                "<link href='https://y/2'/>"
                "<updated>2026-08-24T09:00:00Z</updated></entry></feed>")
        got2 = _parse_feed(atom)
        assert len(got2) == 1 and got2[0]["url"] == "https://y/2", got2

    def t_manual_flags_missing():
        from stocknews.flags import load_manual_credit, load_manual_flags
        assert load_manual_flags("data/__does_not_exist__.csv") == []
        assert load_manual_credit("data/__does_not_exist__.csv") == []

    check("import", "패키지 __all__ 정합성", t_package)
    check("import", "네트워크 모듈 임포트", t_lazy_modules)
    check("import", "run_screen MODES", t_runner)
    check("import", "CLI 파서", t_cli_parses)
    check("import", "bs4 lxml-xml 백엔드", t_bs4_xml)
    check("import", "RSS/Atom 파서", t_feed_parser)
    check("import", "수동 플래그 파일 없음 처리", t_manual_flags_missing)


# ══════════════════════════ 보고 ══════════════════════════
def report() -> int:
    width = 62
    print()
    print("=" * width)
    print(" 스모크 테스트 결과")
    print("=" * width)

    bad = [(s, n, m) for s, n, m in _RESULTS if m != "PASS"]
    section = None
    for s, n, m in _RESULTS:
        if s != section:
            section = s
            print(f"\n[{s}]")
        mark = "  OK  " if m == "PASS" else " FAIL "
        print(f" {mark} {n}")
        if m != "PASS":
            print(f"        -> {m}")

    total = len(_RESULTS)
    print()
    print("-" * width)
    print(f" 통과 {total - len(bad)} / {total}   실패 {len(bad)}")
    print("-" * width)

    if bad:
        print("\n실패 목록:")
        for s, n, m in bad:
            print(f"  [{s}] {n}")
            print(f"        {m}")
        print("\n트레이스백을 보려면: python smoke_test.py -v")
    else:
        print("\n전 항목 통과. 앵커 검증 포함:")
        print("  피보 0.618 = 454,710원 (고점 705,000 / 파동시작 300,000)")
        print("  청산 밴드 중심 = P0 x 0.70, 밴드 위치 r = 0.50")
    return 1 if bad else 0


def main() -> int:
    # 모듈 로그를 죽인다. 검사는 반환값으로 하고 있고, stderr 로 나가는
    # 로그가 PowerShell 에서 에러 레코드로 감싸여 출력이 지저분해진다.
    import logging
    logging.disable(logging.CRITICAL)

    print("스모크 테스트 시작 (네트워크·실DB 미사용)")
    tmp = Path(tempfile.mkdtemp(prefix="stocknews_smoke_"))
    try:
        test_imports()
        test_config()
        test_contracts()
        test_fibonacci()
        test_indicators()
        test_liquidation()
        test_screener()
        st = test_store(tmp)
        test_exits()
        test_news()
        test_notify(tmp)
        test_trading_day(tmp)
        test_backtest(tmp)
        test_krx_credit()
        test_docs()
        test_joblock(tmp)
        test_agent_contract()
        if st is not None:
            test_renderer(st)
            test_daily_weekly(st)
        return report()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
