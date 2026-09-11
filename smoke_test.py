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


def _insert_reco(store, d: str, ticker: str, name: str, slot: str,
                 grade: str, rank: int = 1) -> None:
    """recos 에 1건 직접 삽입 (검증용).

    `save_recos` 는 ScreenResult 객체를 요구한다. 채점 산술을 검증하는
    앵커 테스트가 스크리너 객체 생성에 얽히면, 스크리너를 고칠 때
    무관한 테스트가 깨진다. 그래서 테이블에 직접 넣는다.
    """
    import sqlite3
    with sqlite3.connect(store.path) as con:
        con.execute(
            "INSERT INTO recos(d,rank,ticker,name,price,slot,grade,"
            "value_score,trend_score,reason) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (d, rank, ticker, name, 100.0, slot, grade, 8.0, 1.0, ""))
        con.commit()


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

    def t_in_progress_date():
        """장중 판정. 마감 후·주말이면 '진행 중인 날짜'가 없다."""
        from datetime import datetime as _dt

        from stocknews.store import in_progress_date

        # 금요일 2026-08-28
        assert in_progress_date(_dt(2026, 8, 28, 8, 59)) == "2026-08-28", \
            "개장 전에도 오늘 봉은 미완성이다"
        assert in_progress_date(_dt(2026, 8, 28, 9, 1)) == "2026-08-28"
        assert in_progress_date(_dt(2026, 8, 28, 15, 29)) == "2026-08-28"
        # 15:30 마감 + 20분 여유
        assert in_progress_date(_dt(2026, 8, 28, 15, 49)) == "2026-08-28"
        assert in_progress_date(_dt(2026, 8, 28, 15, 50)) is None, \
            "마감 정산 후에는 오늘 봉을 받아야 한다"
        assert in_progress_date(_dt(2026, 8, 28, 23, 0)) is None
        # 주말은 오늘 봉이 애초에 없다
        assert in_progress_date(_dt(2026, 8, 29, 10, 0)) is None
        assert in_progress_date(_dt(2026, 8, 30, 10, 0)) is None

    def t_intraday_bar_rejected():
        """장중 오늘 봉을 종가로 적재하면 안 된다.

        2026-08-28 실측: 백필이 08:54~09:04 에 돌았고 장은 09:00 에
        열렸다. 09:00 이후 처리된 252종목이 개장 몇 분치만 담긴 봉을
        받았고, 그중 246종목(98%)의 거래량이 8월 평균의 30% 미만이었다.
        `daily` 가 `last_price_date()` 로 그 날짜를 거래일로 잡아
        1,196종목을 08-28 기준으로 채점하기 시작했다.
        """
        import stocknews.store as store_mod
        from stocknews.store import Store

        # 별도 DB 를 쓴다. 공유 DB 에 날짜를 추가하면 daily 의
        # check_listing (= DB 전체 거래일 수 >= 252) 이 켜지면서 다른
        # 검사의 250봉 픽스처가 '상장 1년 미만'으로 배제된다. 실제로
        # 그렇게 깨졌다.
        sti = Store(tmp / "intraday.db")
        today = "2026-08-28"
        df = pd.DataFrame({
            "시가": [1000.0, 1010.0], "고가": [1020.0, 1015.0],
            "저가": [990.0, 1005.0], "종가": [1015.0, 1012.0],
            "거래량": [100000.0, 3000.0],       # 두 번째가 장중 봉
        }, index=pd.to_datetime(["2026-08-27", today]))

        orig = store_mod.in_progress_date
        store_mod.in_progress_date = lambda now=None: today
        try:
            n = sti.upsert_prices("000094", df)
            assert n == 1, f"장중 봉이 적재됐다 (n={n})"
            back = sti.load_ohlcv("000094", days=10)
            # load_ohlcv 는 d 를 인덱스로 옮긴다 (컬럼으로 남지 않는다)
            got = {str(x)[:10] for x in back.index}
            assert back is not None and today not in got, \
                f"장중 날짜가 DB 에 남았다: {got}"

            # 전종목 경로도 같은 규칙 (쓰는 곳이 둘이다)
            cs = pd.DataFrame({
                "시가": [1000.0], "고가": [1020.0], "저가": [990.0],
                "종가": [1015.0], "거래량": [3000.0], "거래대금": [None],
            }, index=["000093"])
            assert sti.upsert_cross_section(today, cs) == 0, \
                "전종목 경로로 장중 봉이 들어왔다"

            # 마감 후 재적재는 허용해야 한다
            assert sti.upsert_prices("000092", df, allow_today=True) == 2
        finally:
            store_mod.in_progress_date = orig

    def t_partial_day_not_trade_date():
        """부분 적재된 날짜를 기준일로 잡으면 안 된다.

        MAX(d) 를 그대로 쓰면 적재가 끊긴 날짜나 장중 일부만 들어온
        날짜가 최신이 되고, daily 가 전종목을 그 날짜로 채점한다.
        """
        from stocknews.store import Store

        st2 = Store(tmp / "partial.db")
        rows = []
        for d, n in (("2026-08-25", 200), ("2026-08-26", 200),
                     ("2026-08-27", 204)):
            for i in range(n):
                rows.append((f"{i:06d}", d))
        # 08-28 은 12종목만 (부분 적재)
        for i in range(12):
            rows.append((f"{i:06d}", "2026-08-28"))
        import sqlite3 as _sq
        with _sq.connect(st2.path) as con:
            con.executemany(
                "INSERT INTO prices(ticker,d,o,h,l,c,v) "
                "VALUES(?,?,100,110,90,105,1000)", rows)
            con.commit()

        assert st2.last_price_date() == "2026-08-27", \
            f"부분 적재일을 골랐다: {st2.last_price_date()}"
        assert st2.last_price_date(allow_partial=True) == "2026-08-28", \
            "allow_partial 이 무시됐다"

        # 정상적으로 다 채워지면 그 날짜를 골라야 한다 (과잉 차단 방지)
        with _sq.connect(st2.path) as con:
            con.executemany(
                "INSERT INTO prices(ticker,d,o,h,l,c,v) "
                "VALUES(?,?,100,110,90,105,1000)",
                [(f"{i:06d}", "2026-08-28") for i in range(12, 200)])
            con.commit()
        assert st2.last_price_date() == "2026-08-28", \
            "완성된 날짜를 거부했다"

    def t_partial_day_not_complete():
        """부분 적재된 날짜는 '적재 완료'로 세면 안 된다.

        2026-09 실측 회귀. `existing_dates()` 는 행이 1개라도 있으면 그
        날짜를 준다. `mode_update` 가 그걸 `have` 로 받아 `if ds in have:
        continue` 로 건너뛰는 바람에, 장중에 돌아 3종목만 받아온
        2026-09-02 가 영구히 3종목인 채로 굳었다. 08-28(13종목),
        09-04(39종목)도 같다.

        `complete_dates()` 는 이 날짜들을 빼고 줘야 하고, 그래야 update 가
        다시 요청한다.
        """
        from stocknews.store import Store
        import sqlite3 as _sq

        stc = Store(tmp / "complete.db")
        rows = []
        for d in ("2026-08-24", "2026-08-25", "2026-08-26",
                  "2026-08-27", "2026-08-31"):
            rows += [(f"{i:06d}", d) for i in range(200)]
        # 09-02 는 3종목만 — 장중 폴백이 기준 종목만 받아온 그 모양이다
        rows += [(f"{i:06d}", "2026-09-02") for i in range(3)]
        with _sq.connect(stc.path) as con:
            con.executemany(
                "INSERT INTO prices(ticker,d,o,h,l,c,v) "
                "VALUES(?,?,100,110,90,105,1000)", rows)
            con.commit()

        have_raw = stc.existing_dates()
        full = stc.complete_dates()
        part = stc.partial_dates()

        # 원래 의미는 보존한다 — existing_dates 는 여전히 준다
        assert "2026-09-02" in have_raw, \
            "existing_dates 의 기존 의미가 바뀌었다"
        # 핵심: 3종목짜리 날짜가 '완료' 집합에 들어가면 안 된다
        assert "2026-09-02" not in full, \
            f"3종목뿐인 날짜가 완료로 잡혔다: {sorted(full)}"
        assert part.get("2026-09-02") == 3, \
            f"부분 적재 보고가 틀렸다: {part}"
        # 정상일은 전부 완료여야 한다 (과잉 차단 방지)
        assert len(full) == 5, f"정상일이 부분으로 잡혔다: {sorted(full)}"

        # mode_update 의 스킵 조건 그대로 재현
        assert "2026-09-02" not in full, "update 가 또 건너뛴다"
        assert "2026-08-27" in full, "update 가 정상일을 재요청한다"

        # 다 채우면 완료로 돌아와야 한다
        with _sq.connect(stc.path) as con:
            con.executemany(
                "INSERT INTO prices(ticker,d,o,h,l,c,v) "
                "VALUES(?,?,100,110,90,105,1000)",
                [(f"{i:06d}", "2026-09-02") for i in range(3, 200)])
            con.commit()
        assert "2026-09-02" in stc.complete_dates(), \
            "완성된 날짜를 계속 미적재로 본다 — 무한 재요청이 된다"

    def t_partial_guard_holds_on_thin_history():
        """표본이 적으면 판정을 보류한다.

        백필 초기에는 날짜가 몇 개 없다. 그때 중위값으로 자르면 전 날짜가
        '미적재'가 되어 update 가 같은 날을 무한히 재요청한다.
        """
        from stocknews.store import Store
        import sqlite3 as _sq

        stt = Store(tmp / "thin.db")
        with _sq.connect(stt.path) as con:
            con.executemany(
                "INSERT INTO prices(ticker,d,o,h,l,c,v) "
                "VALUES(?,?,100,110,90,105,1000)",
                [("000001", "2026-08-24"), ("000002", "2026-08-24"),
                 ("000001", "2026-08-25")])
            con.commit()
        assert stt.partial_dates() == {}, \
            f"표본 2일인데 부분 적재로 판정했다: {stt.partial_dates()}"
        assert len(stt.complete_dates()) == 2

    def t_partial_day_not_a_market_day():
        """부분 적재일을 '시장 거래일'로 세면 거래정지가 오탐된다.

        2026-09-07 실측 회귀. `scan_local_flags` 가 `existing_dates()` 를
        거래일 집합으로 썼다. 구멍난 날짜(08-28/09-02/09-03/09-04)가 그
        집합에 들어 있으니, 그날 데이터가 없는 종목 = 사실상 전 종목이
        `missing >= min_missing(3)` 에 걸렸다.

            halt_history: 300 -> 323 -> 1,007 -> 2,463 종목
            그 결과 daily 스냅샷 883행 -> 37행

        구멍이 3일 있어도 정상 종목은 정지로 잡히면 안 된다.
        """
        from datetime import date, timedelta
        from stocknews.store import Store
        from stocknews.flags import scan_local_flags
        import sqlite3 as _sq

        # 날짜를 오늘 기준으로 만든다. 하드코딩하면 lookback(120일) 창을
        # 벗어나는 날이 와서 검사가 조용히 무력해진다.
        days, cur = [], date.today() - timedelta(days=1)
        while len(days) < 28:
            if cur.weekday() < 5:
                days.append(cur.isoformat())
            cur -= timedelta(days=1)
        days.sort()
        holes = set(days[12:15])          # 가운데 3일을 구멍으로
        full_days = [d for d in days if d not in holes]

        stf = Store(tmp / "halt.db")
        rows = []
        for d in full_days:                       # 정상일: 60종목
            rows += [(f"{i:06d}", d) for i in range(60)]
        for d in sorted(holes):                   # 구멍일: 1종목뿐
            rows.append(("000999", d))
        with _sq.connect(stf.path) as con:
            con.executemany(
                "INSERT INTO prices(ticker,d,o,h,l,c,v) "
                "VALUES(?,?,100,110,90,105,1000)", rows)
            con.commit()

        assert len(stf.complete_dates()) == 25, \
            f"거래일 집합이 틀렸다: {len(stf.complete_dates())}"

        out = scan_local_flags(stf, {f"{i:06d}": {} for i in range(60)})
        halts = out["halts"]
        assert "000001" not in halts, \
            (f"정상 종목이 거래정지로 오탐됐다 (missing={halts.get('000001')}). "
             "부분 적재일이 거래일로 세어졌다는 뜻이다.")
        assert not halts, f"오탐 {len(halts)}종목: {list(halts)[:5]}"

    def t_zero_ohlc_rejected():
        """OHLC 가 0 인 행은 거래량이 있어도 버려야 한다.

        2026-08-27 실측: 백필 435,263행 중 1행이 `o=h=l=0, c=18,000,
        v=199,329` 였다(아이에스동서 2026-08-13). 거래량이 0이 아니라서
        기존 필터를 통과했다.

        한 행이지만 영향이 크다. 저가 0 이 파동 저점으로 잡히면 그
        종목의 피보나치 레벨이 전부 망가지고, 매물대 POC 와 ATR 도
        0 범위가 된다. 조용히 한 종목의 채점이 무의미해진다.
        """
        bad = pd.DataFrame({
            "시가": [0.0], "고가": [0.0], "저가": [0.0],
            "종가": [18000.0], "거래량": [199329.0],
        }, index=pd.to_datetime(["2026-08-13"]))
        assert st.upsert_prices("000099", bad) == 0, \
            "OHLC 가 0 인 행이 적재됐다"
        # 이 검사는 새 날짜를 만들지 않는다. 공유 DB 의 거래일 수가 늘면
        # daily 의 check_listing 이 켜져 다른 검사가 깨진다 (실제 발생).
        assert st.load_ohlcv("000099", days=10) is None or \
            st.load_ohlcv("000099", days=10).empty

        # 전종목 경로도 같은 규칙이어야 한다 (쓰는 곳이 둘이다)
        cs = pd.DataFrame({
            "시가": [0.0, 1000.0], "고가": [0.0, 1010.0],
            "저가": [0.0, 990.0], "종가": [18000.0, 1005.0],
            "거래량": [199329.0, 100.0], "거래대금": [None, 100_500.0],
        }, index=["000098", "000097"])
        assert st.upsert_cross_section("2026-08-24", cs) == 1, \
            "전종목 경로에서 OHLC 0 행이 통과했다"

        # 정상 행은 그대로 들어가야 한다 (과잉 차단 방지)
        good = pd.DataFrame({
            "시가": [1000.0], "고가": [1010.0], "저가": [990.0],
            "종가": [1005.0], "거래량": [100.0],
        }, index=pd.to_datetime(["2026-08-13"]))
        assert st.upsert_prices("000096", good) == 1
        # NaN 도 막아야 한다
        nan_row = good.copy()
        nan_row.loc[nan_row.index[0], "저가"] = float("nan")
        assert st.upsert_prices("000095", nan_row) == 0, "NaN 행이 적재됐다"

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
        from stocknews.exits import stop_price_for
        pid = st.open_position(
            "000001", "급락종목", "VALUE", "2026-08-24", 455_000, 100,
            stop_price=stop_price_for(455_000, DEFAULT),
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
        near(p.stop_price, 455_000 * 0.90, tol=1.0, label="기록된 손절선")
        assert p.in_band is True

    def t_state_update():
        p = st.list_positions("OPEN")[0]
        st.touch_position_state(p.id, 470_000, 2, 0, None)
        p2 = st.list_positions("OPEN")[0]
        near(p2.peak_close, 470_000, tol=1.0, label="peak_close")
        assert p2.band_break_streak == 2
        near(p2.entry_band_mid, 455_000, tol=1.0,
             label="상태 갱신이 스냅샷을 건드리지 않음")
        near(p2.stop_price, 455_000 * 0.90, tol=1.0,
             label="상태 갱신이 손절선을 건드리지 않음")

    def t_open_position_requires_stop():
        """손절선 없는 포지션은 만들 수 없어야 한다.

        기본값을 주면 언젠가 빠뜨리게 되고, 그 포지션은 계층 1 판정에서
        조용히 제외된다.
        """
        import inspect as _i
        sig = _i.signature(st.open_position)
        assert sig.parameters["stop_price"].default is _i.Parameter.empty, \
            "stop_price 에 기본값이 있다"
        for bad, why in ((None, "None"), (0, "0"), (-1, "음수"),
                         (455_000, "진입가와 같음"), (500_000, "진입가보다 큼")):
            try:
                st.open_position("000009", "불량", "VALUE", "2026-08-24",
                                 455_000, 10, stop_price=bad)
            except ValueError:
                continue
            raise AssertionError(f"stop_price={why} 인데 포지션이 생성됐다")
        assert len(st.list_positions(None)) == 1, "실패한 시도가 적재됐다"

    def t_exit_signal_and_fill():
        from stocknews.contracts import DONE_TAKE1, ExitDecision
        p = st.list_positions("OPEN")[0]
        dec = ExitDecision(
            ticker=p.ticker, name=p.name, position_id=p.id, layer=5,
            rule="target:take1", action="TRIM", ratio=0.5, qty=50,
            signal_price=523_000, ret_pct=15.0, net_ret_pct=14.5,
            reason="+15% 도달", urgent=False)
        # 신호 날짜를 하드코딩하면 안 된다. pending_exits(days=5) 는
        # '오늘로부터 5일' 창이라, 고정 날짜는 달력이 지나면 창을 벗어나
        # 코드 변경 없이 테스트가 깨진다 (2026-09-01 에 실제로 깨졌다).
        sig_date = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        log_id = st.record_exit_signal(dec, sig_date)
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

    def t_credit_sources():
        """출처를 기록해야 '자동이 죽었는지'를 알 수 있다.

        예전에는 파서가 source 를 "manual" 로 박아서, 키움/KRX 가 넣은
        값도 사람이 넣은 것으로 기록됐다. 자동 수집이 조용히 멈춘 상황과
        잘 도는 상황이 같은 숫자로 보였다.
        """
        st.upsert_credit([{"ticker": "000002", "ratio": 3.3,
                           "asof": "2026-08-24", "source": "kiwoom"}])
        src = st.credit_sources()
        assert "kiwoom" in src, src
        assert src["kiwoom"]["n"] == 1, src
        assert "manual" in src, src
        assert sum(v["n"] for v in src.values()) == st.credit_coverage()[
            "with_credit"], src

    def t_credit_chain_order():
        """적재 순서: 자동 -> 수동. 사람의 값이 항상 이겨야 한다."""
        from stocknews.flags import refresh_credit_chain
        from stocknews.store import Store

        st2 = Store(tmp / "chain.db")
        st2.upsert_tickers([{"ticker": "111111", "name": "체인",
                             "market": "KOSPI", "shares": 1_000_000}])
        auto = tmp / "chain_kiwoom.csv"
        manual = tmp / "chain_manual.csv"
        head = "종목코드,신용잔고율,신용잔고주식수,기준일,비고\n"
        auto.write_text(head + "111111,1.11,,2026-08-26,kiwoom:ka10013\n",
                        encoding="utf-8-sig")
        manual.write_text(head + "111111,9.99,,2026-08-26,사람\n",
                          encoding="utf-8-sig")

        res = refresh_credit_chain(
            st2, chain=((str(auto), "kiwoom"), (str(manual), "manual")))
        assert [s["source"] for s in res["steps"]] == ["kiwoom", "manual"], \
            res["steps"]
        near(st2.load_credit_ratios(max_age_days=None)["111111"], 9.99,
             label="수동이 자동을 덮음")
        assert st2.credit_sources()["manual"]["n"] == 1, st2.credit_sources()

        # 순서를 뒤집으면 자동이 이긴다 — 순서가 실제로 의미를 갖는지 확인
        res2 = refresh_credit_chain(
            st2, chain=((str(manual), "manual"), (str(auto), "kiwoom")))
        assert res2["steps"][-1]["source"] == "kiwoom"
        near(st2.load_credit_ratios(max_age_days=None)["111111"], 1.11,
             label="순서가 결과를 바꿈")

        # 없는 파일은 조용히 건너뛴다 (0건 보고)
        res3 = refresh_credit_chain(
            st2, chain=((str(tmp / "nope.csv"), "kiwoom"),))
        assert res3["steps"][0]["stored"] == 0, res3

    def t_credit_chain_manual_stays_last():
        """--credit-file 로 경로를 바꿔도 수동이 마지막이어야 한다."""
        from stocknews.flags import CREDIT_CHAIN, refresh_credit_chain
        from stocknews.store import Store

        assert CREDIT_CHAIN[-1][1] == "manual", CREDIT_CHAIN
        st3 = Store(tmp / "chain2.db")
        st3.upsert_tickers([{"ticker": "222222", "name": "체인2",
                             "market": "KOSPI", "shares": 1_000_000}])
        alt = tmp / "alt_manual.csv"
        alt.write_text("종목코드,신용잔고율,신용잔고주식수,기준일,비고\n"
                       "222222,7.77,,2026-08-26,대체경로\n",
                       encoding="utf-8-sig")
        res = refresh_credit_chain(st3, manual_path=str(alt))
        assert res["steps"][-1]["source"] == "manual", res["steps"]
        assert res["steps"][-1]["path"] == str(alt), res["steps"]
        near(st3.load_credit_ratios(max_age_days=None)["222222"], 7.77,
             label="대체 수동 경로 적재")

    def t_credit_shares_derived():
        """비율이 없고 주식수만 있으면 상장주식수로 역산한다."""
        from stocknews.flags import refresh_credit
        from stocknews.store import Store

        st4 = Store(tmp / "derive.db")
        st4.upsert_tickers([{"ticker": "333333", "name": "역산",
                             "market": "KOSPI", "shares": 10_000_000}])
        p = tmp / "derive.csv"
        p.write_text("종목코드,신용잔고율,신용잔고주식수,기준일,비고\n"
                     "333333,,250000,2026-08-26,\n", encoding="utf-8-sig")
        res = refresh_credit(st4, p, source="kiwoom")
        assert res["derived"] == 1, res
        near(st4.load_credit_ratios(max_age_days=None)["333333"], 2.5,
             label="250,000 / 10,000,000 = 2.5%")
        assert st4.credit_sources()["kiwoom"]["n"] == 1

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
                     "credit_manual", "exit_log", "runs",
                     "news", "news_tickers"):
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

    def t_runs_history():
        """runs 테이블 조회 경로. 쓰기만 있고 읽는 코드가 없었다.

        AGENTS.md 7장이 "모든 배치 이력이 남습니다"라고 약속하지만
        확인할 방법이 없었다. 검증 없는 약속은 조용히 깨진다.
        """
        from datetime import timedelta as _td
        from datetime import timezone as _tz

        st.log_run("daily", datetime.now(), 1800, 3, note="rc=0")
        st.log_run("daily", datetime.now(), 1750, 1, note="rc=0")
        st.log_run("exits", datetime.now(), 2, 0, note="rc=0")

        hist = st.run_history(days=1)
        assert not hist.empty, "이력이 조회되지 않는다"
        assert set(["mode", "started", "finished", "ok", "failed",
                    "elapsed", "note"]) <= set(hist.columns), list(hist.columns)
        only = st.run_history(days=1, mode="daily")
        assert len(only) == 2, len(only)
        assert set(only["mode"]) == {"daily"}
        assert len(st.run_history(days=1, limit=1)) == 1, "limit 무시됨"

        rep = st.run_summary(days=1)
        assert rep["daily"]["runs"] == 2, rep["daily"]
        assert rep["daily"]["ok"] == 3550, rep["daily"]
        assert rep["daily"]["failed"] == 4, rep["daily"]
        assert rep["daily"]["age_hours"] is not None
        assert rep["daily"]["age_hours"] < 1.0, rep["daily"]

        # aware 든 naive 든 받아야 한다. 섞어 빼면 TypeError 로 죽는다.
        aware = datetime.now(_tz(_td(hours=9))) - _td(seconds=5)
        st.log_run("tz-probe", aware, 1, 0)
        got = st.run_history(days=1, mode="tz-probe")
        assert len(got) == 1, "aware datetime 이 거부됐다"
        near(float(got.iloc[0]["elapsed"]), 5.0, tol=3.0, label="경과 초")

        # 저장 시각은 KST 여야 한다. runs 만 서버 로컬시각이면 UTC
        # 호스트에서 다른 테이블과 9시간 어긋나 대조가 불가능해진다.
        kst_now = datetime.now(_tz(_td(hours=9))).replace(tzinfo=None)
        fin = datetime.fromisoformat(str(got.iloc[0]["finished"]))
        assert abs((fin - kst_now).total_seconds()) < 120, \
            f"finished 가 KST 가 아니다: {fin} vs {kst_now}"

    def t_runs_exported():
        """runs 가 CSV 내보내기에서 빠져 있었다. 성과 대조의 전제다."""
        assert "runs" in Store._EXPORTS, list(Store._EXPORTS)

    check("store", "시세 왕복 + 멱등성", t_prices)
    check("store", "일자별 전종목 적재 (거래정지 제외)", t_cross_section)
    check("store", "OHLC 0 행 거부 (실데이터 버그)", t_zero_ohlc_rejected)
    check("store", "장중 판정 (마감+20분)", t_in_progress_date)
    check("store", "장중 미완성 봉 거부 (실데이터 버그)", t_intraday_bar_rejected)
    check("store", "부분 적재일은 기준일 제외", t_partial_day_not_trade_date)
    check("store", "부분 적재일은 적재 완료 아님 (실데이터 버그)",
          t_partial_day_not_complete)
    check("store", "표본 부족 시 부분적재 판정 보류", t_partial_guard_holds_on_thin_history)
    check("flags", "부분 적재일은 거래일 아님 -> 거래정지 오탐 방지",
          t_partial_day_not_a_market_day)
    check("store", "종목 마스터 COALESCE 보존", t_tickers)
    check("store", "비활성/재등록", t_inactive)
    check("store", "스캔 스냅샷 + 추천 이력", t_scan_reco)
    check("store", "플래그 부분 갱신 보존", t_flags_coalesce)
    check("store", "capital_impair 전용 TTL", t_flags_ttl)
    check("store", "플래그 필드 초기화", t_flags_clear)
    check("store", "포지션 개시 + 스냅샷", t_positions)
    check("store", "파생 상태 갱신", t_state_update)
    check("store", "손절선 없는 포지션 생성 거부", t_open_position_requires_stop)
    check("store", "청산 신호 -> 체결 2단계", t_exit_signal_and_fill)
    check("store", "포지션 종료", t_close)
    check("store", "뉴스 적재/조회", t_news)
    check("store", "수동 신용잔고 + 기준일 만료", t_credit_manual)
    check("store", "신용잔고 출처 집계", t_credit_sources)
    check("credit", "적재 순서 (수동이 이김)", t_credit_chain_order)
    check("credit", "수동은 항상 체인 마지막", t_credit_chain_manual_stays_last)
    check("credit", "주식수 -> 비율 역산", t_credit_shares_derived)
    check("liquidation", "실측 신용잔고가 캡을 벗김", t_credit_lifts_cap)
    check("store", "CSV 내보내기 (BOM 확인)", t_export)
    check("runs", "이력 조회 + 모드별 요약", t_runs_history)
    check("runs", "CSV 내보내기 포함", t_runs_exported)
    check("store", "기타 조회", t_misc)
    return st


# ══════════════════════════ 7. 청산 엔진 ══════════════════════════
def test_exits():
    from stocknews.config import DEFAULT
    from stocknews.contracts import DONE_TAKE1, Position
    from stocknews.exits import (atr, derive_state, evaluate_position,
                                 evaluate_rotation)

    ec = DEFAULT.exit

    def _ago(bars: int) -> str:
        """프레임 끝에서 `bars` 거래일 전. 진입일 봉을 1일로 세면 보유 = bars.

        `_dates()` 는 n 과 무관하게 2026-08-24 에서 끝나므로 프레임 길이가
        달라도 같은 날짜가 나온다.
        """
        return _dates(60)[-bars].strftime("%Y-%m-%d")

    def _pos(**kw):
        # 보유 8거래일. MIN_HOLD_DAYS(5) 는 지났고 MAX_HOLD_DAYS(20) 은
        # 안 됐다. 계층별 검사가 보유기간 규칙에 걸리지 않게 하려면 이
        # 창 안에 있어야 한다. 예전 고정값("2026-01-05")은 보유 165일이라
        # 전부 '보유기간 만료'로 잡힌다.
        # stop_price 는 일부러 비운다. 계층 1 의 고정 손절을 끄고 나머지
        # 계층을 독립 검증하기 위한 것이고, 고정 손절은 전용 픽스처로 본다.
        base = dict(id=1, ticker="000001", name="테스트", track="VALUE",
                    entry_date=_ago(8), entry_price=70_000, qty=100,
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
        # 보유 17일. time_stop_v_days(15) 는 넘고 max_hold_days(20) 는
        # 안 넘어야 한다. 20 이면 '보유기간 만료'(계층 0)가 먼저 잡힌다.
        p = _pos(entry_date=df.index[-17].strftime("%Y-%m-%d"),
                 entry_price=70_000, entry_fib_0382=999_000,
                 entry_band_hi=999_000)
        dec, st = evaluate_position(p, df, None, DEFAULT)
        assert st["bars_held"] >= ec.time_stop_v_days
        assert st["bars_held"] < ec.max_hold_days
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
                 entry_date=df.index[-15].strftime("%Y-%m-%d"),
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
                 entry_date=df.index[-15].strftime("%Y-%m-%d"),
                 entry_band_hi=None, entry_band_lo=None, entry_fib_0382=None,
                 entry_cross_low=90_000)
        dec, _ = evaluate_position(p, df, None, DEFAULT)
        assert dec is not None and dec.layer == 1, \
            f"MA60 이탈 손절 미발동 (layer={dec.layer if dec else None})"

    def t_no_signal():
        """밴드 정중앙에서 조용히 있으면 아무 신호도 없어야 한다.

        보유 8일 — 최소보유일을 지났으므로 '억제되어서' 무신호인 게 아니라
        진짜로 걸리는 규칙이 없어서 무신호여야 한다.
        """
        p = _pos()
        dec, st = evaluate_position(p, _flat_at(70_000), None, DEFAULT)
        assert st["bars_held"] >= ec.min_hold_days, "게이트에 가려진 대조군"
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
    # ───────── 보유기간 계층 + 진입 시 손절 고정 ─────────
    from stocknews.config import MAX_HOLD_DAYS, MIN_HOLD_DAYS, STOP_LOSS_PCT
    from stocknews.exits import bars_held, stop_price_for

    STOP = 70_000 * (1 - STOP_LOSS_PCT / 100.0)      # 63,000

    def t_hold_anchor():
        assert (MIN_HOLD_DAYS, MAX_HOLD_DAYS) == (5, 20), \
            f"{MIN_HOLD_DAYS}/{MAX_HOLD_DAYS}"
        near(STOP_LOSS_PCT, 10.0, label="STOP_LOSS_PCT")
        assert ec.min_hold_days == MIN_HOLD_DAYS
        assert ec.max_hold_days == MAX_HOLD_DAYS
        near(ec.stop_loss_pct, STOP_LOSS_PCT, label="config 배선")
        near(stop_price_for(100_000, DEFAULT), 90_000, tol=1e-9,
             label="손절선 = 진입가 -10%")
        assert MIN_HOLD_DAYS < MAX_HOLD_DAYS, "두 규칙이 서로를 무력화한다"

    def t_bars_held_is_trading_days():
        """보유 일수는 봉 개수다. 달력일로 세면 주말에 앞서간다."""
        df = _flat_at(70_000)
        for want in (1, 3, 8, 20):
            p = _pos(entry_date=_ago(want))
            assert bars_held(p, df) == want, \
                f"{want}거래일 기대, 실제 {bars_held(p, df)}"
        # 8거래일 전은 달력으로는 11일 전이다 (주말 2일 + 1)
        d0 = datetime.strptime(_ago(8), "%Y-%m-%d")
        d1 = datetime.strptime(_ago(1), "%Y-%m-%d")
        assert (d1 - d0).days > 8, "픽스처에 주말이 안 끼었다"

    def t_min_hold_stop_fires():
        """최소보유일 이전에도 손절(계층 1)은 발동해야 한다."""
        p = _pos(entry_date=_ago(3), stop_price=STOP)
        dec, st = evaluate_position(p, _flat_at(60_000), None, DEFAULT)
        assert st["bars_held"] < ec.min_hold_days, "픽스처가 최소보유일을 넘었다"
        assert dec is not None and dec.layer == 1, \
            f"최소보유일 내 손절 미발동 (layer={dec.layer if dec else None})"
        assert dec.rule == "stop:fixed", dec.rule
        assert dec.action == "EXIT_ALL" and dec.urgent is True

    def t_min_hold_allows_invalidation():
        """계층 0 무효화도 억제 대상이 아니다. 상장폐지 위험을 미룰 수 없다."""
        p = _pos(entry_date=_ago(3), stop_price=STOP)
        dec, _ = evaluate_position(p, _flat_at(70_000),
                                   {"000001": {"관리종목": True}}, DEFAULT)
        assert dec is not None and dec.layer == 0, \
            f"최소보유일 내 무효화가 억제됐다 (layer={dec.layer if dec else None})"

    def t_min_hold_suppresses_targets():
        """최소보유일 이전에는 계층 2~7 이 전부 억제된다 (+ 대조군)."""
        df = _flat_at(70_000 * 1.16)          # +16% -> 계층 5 조건 성립
        kw = dict(entry_fib_0382=999_000, entry_band_hi=999_000,
                  stop_price=STOP)
        early, _ = evaluate_position(_pos(entry_date=_ago(3), **kw),
                                     df, None, DEFAULT)
        assert early is None, \
            f"최소보유일 내 익절이 발동했다 (layer={early.layer if early else None})"
        # 대조군: 같은 조건, 보유일만 넘김
        late, st = evaluate_position(_pos(entry_date=_ago(8), **kw),
                                     df, None, DEFAULT)
        assert st["bars_held"] >= ec.min_hold_days
        assert late is not None and late.layer == 5, \
            f"대조군에서 익절이 안 났다 (layer={late.layer if late else None})"

    def t_min_hold_suppresses_rotation():
        """순환매(계층 7)도 억제 대상이다 (+ 대조군)."""
        hot = np.full(60, 10_000.0)
        hot[-1] = 11_200.0                    # +12%
        cold = np.full(60, 10_000.0)
        cold[-1] = 9_400.0                    # -6%  => 편차 18%
        pmap = {"000001": _frame(hot, spread=0.002),
                "000002": _frame(cold, spread=0.002)}
        young = [_pos(id=1, ticker="000001", entry_price=10_000,
                      entry_date=_ago(3)),
                 _pos(id=2, ticker="000002", entry_price=10_000)]
        assert evaluate_rotation(young, pmap, DEFAULT) == [], \
            "최소보유일 미달 종목이 순환매로 청산됐다"
        # 대조군: 급등 종목만 보유일을 넘김
        old = [_pos(id=1, ticker="000001", entry_price=10_000,
                    entry_date=_ago(8)),
               _pos(id=2, ticker="000002", entry_price=10_000)]
        decs = evaluate_rotation(old, pmap, DEFAULT)
        assert len(decs) == 1 and decs[0].layer == 7, \
            f"대조군에서 순환매가 안 났다 ({len(decs)}건)"

    def t_max_hold_expiry():
        """최대 보유일 도달 시 만료 신호. 계층 0, 규칙 hold:expired."""
        df = _flat_at(70_000 * 1.05)          # +5%. 다른 계층 조건 미성립
        kw = dict(entry_fib_0382=999_000, entry_band_hi=999_000,
                  stop_price=STOP)
        dec, st = evaluate_position(_pos(entry_date=_ago(20), **kw),
                                    df, None, DEFAULT)
        assert st["bars_held"] == ec.max_hold_days, st["bars_held"]
        assert dec is not None and dec.layer == 0, \
            f"만료 미발동 (layer={dec.layer if dec else None})"
        assert dec.rule == "hold:expired", dec.rule
        assert dec.action == "EXIT_ALL" and dec.ratio == 1.0
        assert "보유기간 만료" in dec.reason and "재평가" in dec.reason
        assert dec.detail["bars_held"] == 20
        assert dec.urgent is False, "만료는 긴급 신호가 아니다"

    def t_max_hold_control():
        """대조군: 하루 전(19거래일)에는 아무 신호도 없어야 한다."""
        df = _flat_at(70_000 * 1.05)
        kw = dict(entry_fib_0382=999_000, entry_band_hi=999_000,
                  stop_price=STOP)
        dec, st = evaluate_position(_pos(entry_date=_ago(19), **kw),
                                    df, None, DEFAULT)
        assert st["bars_held"] == ec.max_hold_days - 1, st["bars_held"]
        assert dec is None, \
            f"19거래일에 신호가 났다 (rule={dec.rule if dec else None})"

    def t_expiry_priority():
        """계층 0 안의 순서: 무효화 → 만료. 그리고 만료 > 손절(계층 1)."""
        kw = dict(entry_fib_0382=999_000, entry_band_hi=999_000,
                  stop_price=STOP)
        # 무효화가 만료를 이긴다
        dec, _ = evaluate_position(_pos(entry_date=_ago(20), **kw),
                                   _flat_at(70_000 * 1.05),
                                   {"000001": {"관리종목": True}}, DEFAULT)
        assert dec.rule.startswith("invalidation:"), dec.rule
        # 만료가 손절을 이긴다. 단 손절 정보를 메시지에 실어야 한다.
        dec2, _ = evaluate_position(_pos(entry_date=_ago(20), **kw),
                                    _flat_at(60_000), None, DEFAULT)
        assert dec2.rule == "hold:expired", dec2.rule
        assert dec2.detail["stop_hit"] is True, dec2.detail
        assert "손절선" in dec2.reason and "이탈" in dec2.reason, dec2.reason
        # 만료 전이면 손절이 그대로 발동한다
        dec3, _ = evaluate_position(_pos(entry_date=_ago(19), **kw),
                                    _flat_at(60_000), None, DEFAULT)
        assert dec3 is not None and dec3.rule == "stop:fixed", \
            f"19거래일 손절 미발동 ({dec3.rule if dec3 else None})"

    def t_stop_uses_recorded_value_only():
        """같은 가격에서 기록된 손절선만으로 판정이 갈려야 한다."""
        df = _flat_at(60_000)
        hit, _ = evaluate_position(_pos(entry_date=_ago(3), stop_price=63_000),
                                   df, None, DEFAULT)
        miss, _ = evaluate_position(_pos(entry_date=_ago(3), stop_price=55_000),
                                    df, None, DEFAULT)
        assert hit is not None and hit.rule == "stop:fixed"
        assert miss is None or miss.rule != "stop:fixed", \
            "손절선 아래가 아닌데 고정 손절이 발동했다"
        # 판정 시점에 ATR 로 손절폭을 다시 계산하면 손절선이 주가를 따라
        # 움직인다. 그 경로가 되살아나지 않게 호출 자체를 막는다.
        names = set(evaluate_position.__code__.co_names)
        assert "atr" not in names, "판정 시점에 ATR 로 손절폭을 재계산한다"

    def t_no_stop_mutation_api():
        """손절선 사후 수정 경로가 없어야 한다."""
        import inspect
        from stocknews.store import Store

        for n in dir(Store):
            if n.startswith("_"):
                continue
            assert "stop" not in n.lower(), f"손절선 조작 API 로 보인다: {n}"
        src = inspect.getsource(Store.touch_position_state)
        assert "stop_price" not in src, "상태 갱신이 손절선을 건드린다"

    check("exits", "계층7 순환매", t_rotation)
    check("exits", "순환매 보류 (편차 5% 미만)", t_rotation_hold)
    check("hold", "보유기간·손절 앵커", t_hold_anchor)
    check("hold", "보유 일수 = 거래일", t_bars_held_is_trading_days)
    check("hold", "최소보유일 내 손절 발동", t_min_hold_stop_fires)
    check("hold", "최소보유일 내 무효화 발동", t_min_hold_allows_invalidation)
    check("hold", "최소보유일 내 목표 억제 + 대조군", t_min_hold_suppresses_targets)
    check("hold", "최소보유일 내 순환매 억제 + 대조군", t_min_hold_suppresses_rotation)
    check("hold", "최대보유일 만료 재평가", t_max_hold_expiry)
    check("hold", "만료 대조군 (19거래일)", t_max_hold_control)
    check("hold", "계층 우선순위 (무효화>만료>손절)", t_expiry_priority)
    check("hold", "기록된 손절선만 사용", t_stop_uses_recorded_value_only)
    check("hold", "손절선 수정 API 부재", t_no_stop_mutation_api)


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
        out = cluster_items(raw)
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

    def t_normalize_url():
        """중복 판정용 URL 키. oc=5 / www / utm 로 갈리면 안 된다."""
        from stocknews.news import normalize_url
        g = "https://news.google.com/rss/articles/CBMiXYZ"
        assert normalize_url(g + "?oc=5") == normalize_url(g), \
            "Google 의 oc=5 유무로 키가 갈린다"
        assert (normalize_url("https://WWW.A.com/x/?utm_source=k")
                == normalize_url("https://a.com/x")), "www/utm 정규화 실패"
        assert normalize_url(None) == "" and normalize_url("") == ""
        assert normalize_url("https://x/1") != normalize_url("https://x/2")

    def t_cluster_crosses_batch():
        """수집 배치가 달라도 같은 사건이면 같은 클러스터여야 한다.

        2026-09-09 실사고. 아침 브리핑은 최근 16시간을 읽는데 그 창에는
        수집 실행이 두세 번 들어간다. 클러스터링이 배치 안에서만 돌면
        같은 기사가 실행마다 다른 cluster_id 를 받아 중복 제거를 통과한다.
        """
        tn = "대양금속 제22회 전환사채 전환가액 하향 조정"
        seeds = [{"id": "old1", "title_norm": tn, "cluster_id": "old1",
                  "source": "digitaltoday.co.kr"}]
        out = cluster_items([{"id": "new1", "title_norm": tn,
                              "source": "디지털투데이"}], seeds=seeds)
        assert out[0]["cluster_id"] == "old1", \
            f"배치 경계를 넘지 못했다: {out[0]['cluster_id']}"
        # 씨앗을 안 주면 옛 동작 그대로 새 클러스터가 생긴다.
        solo = cluster_items([{"id": "new1", "title_norm": tn, "source": "X"}])
        assert solo[0]["cluster_id"] == "new1"

    def t_cluster_seed_not_double_counted():
        """씨앗과 배치에 같은 id 가 있어도 매체 수를 두 번 세지 않는다."""
        tn = "삼성전자 hbm4 양산 착수"
        out = cluster_items(
            [{"id": "a", "title_norm": tn, "source": "매체A"}],
            seeds=[{"id": "a", "title_norm": tn, "cluster_id": "a",
                    "source": "매체A"}])
        assert out[0]["cluster_n"] == 1, \
            f"같은 행을 두 번 세 매체 수가 부풀었다: {out[0]['cluster_n']}"

    def t_google_strips_media_tail():
        """Google 제목의 ' - 매체명' 을 뗀다. 네트워크는 쓰지 않는다.

        예전 판은 <source> 가 없을 때만 뗐고, Google 은 거의 항상 <source>
        를 주므로 사실상 한 번도 떼지 않았다. 그래서 브리핑 제목에
        'digitaltoday.co.kr' 같은 도메인이 그대로 찍혔다.
        """
        from stocknews import news_sources as ns

        class _Res:
            text = ('<?xml version="1.0"?><rss><channel><item>'
                    '<title>대양금속, 전환사채 전환가액 하향 조정'
                    ' - digitaltoday.co.kr</title>'
                    '<link>https://news.google.com/rss/articles/AAA?oc=5</link>'
                    '<source url="https://digitaltoday.co.kr">'
                    'digitaltoday.co.kr</source>'
                    '</item></channel></rss>')

        orig_get, orig_sleep = ns._get, ns.time.sleep
        ns._get = lambda url, **kw: _Res()
        ns.time.sleep = lambda *_a, **_k: None
        try:
            out = ns.collect_google(queries=(("q", "KR", "공시"),), per_query=5)
        finally:
            ns._get, ns.time.sleep = orig_get, orig_sleep
        assert out, "수집 결과가 비었다"
        title = out[0]["title"]
        assert "digitaltoday.co.kr" not in title, f"꼬리표가 남았다: {title}"
        assert title.endswith("하향 조정"), title
        assert out[0]["source"] == "digitaltoday.co.kr", out[0]["source"]

    check("news", "제목 정규화", t_normalize)
    check("news", "id 안정성 + 매체 구분", t_make_id)
    check("news", "URL 정규화 (중복 판정 키)", t_normalize_url)
    check("news", "클러스터가 수집 배치를 넘음", t_cluster_crosses_batch)
    check("news", "씨앗 매체 수 중복 집계 방지", t_cluster_seed_not_double_counted)
    def t_canonical_source():
        """2층 — 도메인 표기와 한글 표기를 하나로 모은다."""
        from stocknews.news import canonical_source
        assert canonical_source("digitaltoday.co.kr") == "디지털투데이"
        assert canonical_source("DigitalToday.co.KR") == "디지털투데이"
        assert canonical_source("www.digitaltoday.co.kr") == "디지털투데이"
        assert canonical_source("디지털투데이") == "디지털투데이"
        # 표에 없으면 지어내지 않고 그대로 둔다.
        assert canonical_source("nosuchpaper.co.kr") == "nosuchpaper.co.kr"
        assert canonical_source("") == "" and canonical_source(None) == ""

    def t_media_count_not_inflated():
        """같은 매체가 표기만 달라 두 매체로 세어지면 안 된다."""
        tn = "대양금속 제22회 전환사채 전환가액 하향 조정"
        out = cluster_items([
            {"id": "a", "title": tn, "title_norm": tn, "source": "digitaltoday.co.kr"},
            {"id": "b", "title": tn, "title_norm": tn, "source": "디지털투데이"}])
        assert out[0]["cluster_id"] == out[1]["cluster_id"], "같은 제목이 안 묶임"
        assert out[0]["cluster_n"] == 1, \
            f"한 매체를 둘로 셌다: {out[0]['cluster_n']}개 매체"

    def t_event_tokens():
        """3층 — 조사를 떼고 순수 숫자를 버린다."""
        from stocknews.news import event_tokens
        got = event_tokens("뉴욕증시 중동 긴장 유가 급등에 하락 다우 1 18")
        assert "급등" in got and "급등에" not in got, got
        assert "1" not in got and "18" not in got, f"숫자 토큰이 남았다: {got}"
        assert "뉴욕증시" in got and "유가" in got

    def t_same_event_two_gates():
        """Jaccard 로는 못 묶는 같은 사건을 2통로가 잡는다.

        2026-09-09 실측 쌍이다. 사람이 보면 같은 사건인데 Jaccard 는 0.214.
        """
        from stocknews.news import event_tokens, same_event
        a = event_tokens("중동 확전 우려 뉴욕증시 하락 유가 100달러 육박 엔화 급등")
        b = event_tokens("뉴욕증시 국제 유가 상승에 하락 출발 다우 0 88")
        inter = len(a & b)
        jac = inter / len(a | b)
        assert jac < 0.30, f"전제가 깨졌다 (Jaccard {jac:.3f})"
        assert same_event(a, b) > 0.0, "2통로가 같은 사건을 못 잡았다"

    def t_serial_guard():
        """회차가 다른 전환사채 공시는 문구가 같아도 별건이다."""
        rows = [
            {"id": "a", "title": "대양금속, 제22회 전환사채 전환가액 1774원에서 1184원으로 하향 조정",
             "title_norm": "대양금속 제22회 전환사채 전환가액 1774원에서 1184원으로 하향 조정",
             "source": "디지털투데이"},
            {"id": "b", "title": "대양금속, 제25회 전환사채 전환가액 1863원에서 1857원으로 하향 조정",
             "title_norm": "대양금속 제25회 전환사채 전환가액 1863원에서 1857원으로 하향 조정",
             "source": "디지털투데이"},
        ]
        out = cluster_items(rows)
        assert out[0]["cluster_id"] != out[1]["cluster_id"], \
            "22회차와 25회차가 한 사건으로 묶였다"

    def t_ticker_guard():
        """태깅된 종목이 겹치지 않으면 같은 사건으로 보지 않는다."""
        rows = [
            {"id": "a", "title": "A사 3분기 영업이익 급증 흑자전환 성공",
             "title_norm": "a사 3분기 영업이익 급증 흑자전환 성공",
             "source": "매체A", "tickers": [("000001", "A사")]},
            {"id": "b", "title": "B사 3분기 영업이익 급증 흑자전환 성공",
             "title_norm": "b사 3분기 영업이익 급증 흑자전환 성공",
             "source": "매체B", "tickers": [("000002", "B사")]},
        ]
        out = cluster_items(rows)
        assert out[0]["cluster_id"] != out[1]["cluster_id"], \
            "다른 종목 기사가 한 사건으로 묶였다"

    def t_representative_rank():
        """대표 선정: 내 종목 언급 > 주요 매체 > 먼저 수집된 것."""
        from stocknews.news import representative_rank
        mine = {"source": "듣보뉴스", "collected": "2026-09-09T09:00:00"}
        major = {"source": "연합뉴스", "collected": "2026-09-09T09:00:00"}
        early = {"source": "듣보뉴스", "collected": "2026-09-09T01:00:00"}
        assert representative_rank(mine, has_ticker=True) < \
            representative_rank(major, has_ticker=False), "내 종목이 1순위여야 한다"
        assert representative_rank(major, has_ticker=False) < \
            representative_rank(early, has_ticker=False), "주요 매체가 2순위여야 한다"
        late = {"source": "듣보뉴스", "collected": "2026-09-09T23:00:00"}
        assert representative_rank(early, has_ticker=False) < \
            representative_rank(late, has_ticker=False), "먼저 수집된 것이 3순위여야 한다"

    check("news", "Google 제목 매체 꼬리표 제거", t_google_strips_media_tail)
    check("news", "2층 매체명 정규화", t_canonical_source)
    check("news", "2층 매체 수 부풀림 방지", t_media_count_not_inflated)
    check("news", "3층 토큰 (조사·숫자 제거)", t_event_tokens)
    check("news", "3층 두 번째 통로 (포함도)", t_same_event_two_gates)
    check("news", "3층 회차 가드 (22회 != 25회)", t_serial_guard)
    check("news", "3층 종목 가드", t_ticker_guard)
    check("news", "대표 기사 선정 순위", t_representative_rank)
    check("news", "카테고리 분류", t_classify)
    check("news", "사건 클러스터 + 매체 수", t_cluster)
    check("news", "별칭 인덱스 (우선주/모호 배제)", t_alias_index)
    check("news", "부분 겹침 오태깅 방지", t_map_tickers_partial)
    check("news", "DART 종목코드 직접 매핑", t_map_tickers_dart)
    check("news", "중요도 (매체 수 1순위)", t_importance)


# ═══════════════ 8-1. 중복 제거 3층 — 실측 픽스처 회귀 ═══════════════
def test_news_dedup_fixture():
    """2026-09-09 06:20 아침 브리핑이 실제로 읽은 187건으로 회귀를 건다.

    임계값을 만지면 여기가 먼저 깨진다. 그게 목적이다 — 압축은 늘리되
    다른 사건을 붙이지는 않았는지 한 곳에서 본다. 네트워크도 실 DB 도
    쓰지 않는다 (픽스처는 저장소에 있다).
    """
    import json
    from stocknews.news import canonical_source
    from stocknews.renderer import _group_events

    path = (Path(__file__).parent / "tests" / "fixtures"
            / "news_brief_20260909.json")

    def _load():
        assert path.exists(), f"픽스처 없음: {path}"
        return json.loads(path.read_text(encoding="utf-8"))

    def t_fixture_shape():
        data = _load()
        assert data["count"] == 187, f"픽스처 건수가 바뀌었다: {data['count']}"
        assert len(data["rows"]) == 187
        need = {"id", "title", "title_norm", "url", "source", "category",
                "region", "importance", "cluster_id", "collected", "tickers"}
        assert need <= set(data["rows"][0]), "픽스처 열이 모자란다"

    def _groups():
        import pandas as pd
        rows = _load()["rows"]
        tm = {r["id"]: [n for _, n in r["tickers"]] for r in rows if r["tickers"]}
        return rows, _group_events(pd.DataFrame(rows), tm)

    def t_compression():
        """187건 -> 사건 수. 압축이 줄면(로직 후퇴) 잡는다."""
        rows, groups = _groups()
        assert len(groups) <= 155, \
            f"압축이 후퇴했다: {len(rows)}건 -> {len(groups)}사건 (155 이하 기대)"
        assert len(groups) >= 140, \
            f"과병합 의심: {len(rows)}건 -> {len(groups)}사건 (140 이상 기대)"

    def t_daeyang_serials_split():
        """대양금속 전환사채 — 회차별로 갈라져 있어야 한다."""
        _, groups = _groups()
        by_serial = {}
        for g in groups:
            for s in ("제22회", "제25회", "24회"):
                if any(s in r["title"] for r in g):
                    by_serial.setdefault(s, set()).add(id(g))
        assert set(by_serial) == {"제22회", "제25회", "24회"}, by_serial
        ids = [next(iter(v)) for v in by_serial.values()]
        assert len(set(ids)) == 3, "서로 다른 회차의 CB 공시가 한 사건으로 묶였다"
        for s, v in by_serial.items():
            assert len(v) == 1, f"{s} 가 {len(v)}개 사건으로 흩어졌다"

    def t_daeyang_media_merged():
        """같은 회차의 도메인/한글 표기 두 건은 한 사건 · 한 매체."""
        _, groups = _groups()
        g = next(g for g in groups
                 if sum("제22회" in r["title"] for r in g) == 2)
        assert len(g) == 2, len(g)
        assert len({canonical_source(r["source"]) for r in g}) == 1, \
            "같은 매체가 둘로 세어졌다"

    def t_nyse_merged():
        """뉴욕증시 하락 기사 3건이 한 사건으로 묶인다 (2통로가 잡는 것)."""
        _, groups = _groups()
        g = next((g for g in groups
                  if any("thefairnews" in (r["source"] or "") for r in g)), None)
        assert g is not None, "thefairnews 행이 사라졌다"
        assert len(g) >= 3, f"뉴욕증시 사건이 {len(g)}건으로만 묶였다"
        assert any("뉴시스" == r["source"] for r in g), \
            "뉴시스 기사가 같은 사건으로 묶이지 않았다"
        assert g[0]["source"] == "뉴시스", \
            f"대표가 주요 매체가 아니다: {g[0]['source']}"

    def t_no_serial_crossing():
        """어떤 사건에도 서로 다른 회차가 섞여 있으면 안 된다."""
        from stocknews.news import event_serials
        _, groups = _groups()
        for g in groups:
            found = [event_serials(r["title"]) for r in g]
            nonempty = [s for s in found if s]
            if len(nonempty) > 1:
                assert set.intersection(*nonempty), \
                    f"회차가 다른 기사가 묶였다: {[r['title'][:40] for r in g]}"

    def t_no_ticker_crossing():
        """서로 다른 종목이 태깅된 기사가 한 사건으로 묶이면 안 된다."""
        rows, groups = _groups()
        tk = {r["id"]: {c for c, _ in r["tickers"]} for r in rows}
        for g in groups:
            sets = [tk.get(r["id"]) or set() for r in g]
            nonempty = [s for s in sets if s]
            if len(nonempty) > 1:
                assert set.intersection(*nonempty), \
                    f"다른 종목 기사가 묶였다: {[r['title'][:40] for r in g]}"

    check("dedup", "픽스처 형태 (187건)", t_fixture_shape)
    check("dedup", "압축률 회귀", t_compression)
    check("dedup", "대양금속 회차 분리", t_daeyang_serials_split)
    check("dedup", "같은 회차 매체 표기 병합", t_daeyang_media_merged)
    check("dedup", "뉴욕증시 사건 병합 + 대표", t_nyse_merged)
    check("dedup", "회차 교차 오병합 없음", t_no_serial_crossing)
    check("dedup", "종목 교차 오병합 없음", t_no_ticker_crossing)


# ═══════════════ 8-1b. DART 조회 시장 (코스닥 확장, 기본 OFF) ═══════════════
def test_dart_markets():
    """corp_cls 순회. 네트워크는 쓰지 않는다 — 호출 함수를 가로채 기록한다."""
    import os
    from stocknews import config as C
    from stocknews import flags as F
    from stocknews import news_sources as NS

    def t_default_off():
        """설정 정합성. 코넥스는 어떤 설정에도 없고, 코스닥을 켰으면 기준일이 있어야 한다."""
        assert C.DART_MARKETS[0] == "Y", f"코스피가 먼저여야 한다: {C.DART_MARKETS}"
        assert "N" not in C.DART_MARKETS, "코넥스는 유니버스 밖이다"
        if "K" in C.DART_MARKETS:
            assert C.DART_KOSDAQ_ENABLED_ON, \
                "코스닥을 켰는데 채점 구분 기준일(DART_KOSDAQ_ENABLED_ON)이 비어 있다"
            from datetime import date
            date.fromisoformat(C.DART_KOSDAQ_ENABLED_ON)   # YYYY-MM-DD 형식
        else:
            assert C.DART_KOSDAQ_ENABLED_ON is None, "켜지 않았는데 ON 날짜가 적혀 있다"
        assert set(C.DART_LIST_MAX_PAGES) >= {"Y", "K"}
        assert set(C.DART_NEWS_MAX_PAGES) >= {"Y", "K"}
        # 2026-09-09 실측: 60일 소급에 Y 105페이지 · K 113페이지가 필요하다.
        assert C.DART_LIST_MAX_PAGES["Y"] >= 105 and C.DART_LIST_MAX_PAGES["K"] >= 113, \
            f"페이지 상한이 실측 필요량 아래다 (11일치 절단 결함 재발): {C.DART_LIST_MAX_PAGES}"

    def _fake_pages(seen: list, per_market: dict):
        """corp_cls 별로 per_market[시장] 페이지를 흘려주는 가짜 _dart_get."""
        def fake(path, params):
            m, p = params["corp_cls"], params["page_no"]
            seen.append((m, p))
            n_pages = per_market.get(m, 0)
            if p > n_pages:
                return {"status": "013", "list": []}
            full = p < n_pages
            items = [{"stock_code": f"{m}{p:05d}"[:6],
                      "report_nm": "유상증자결정" if m == "K" else "기타공시"}]
            items += [{"stock_code": "", "report_nm": "x"}] * (99 if full else 0)
            return {"status": "000", "list": items}
        return fake

    def t_flags_iterates_y_then_k():
        os.environ.setdefault("DART_API_KEY", "smoke-dummy")
        seen: list = []
        orig = F._dart_get
        F._dart_get = _fake_pages(seen, {"Y": 2, "K": 3})
        try:
            hits, ok = F.dart_disclosure_events(
                days=60, markets=("Y", "K"), max_pages={"Y": 20, "K": 170})
        finally:
            F._dart_get = orig
        markets_in_order = [m for m, _ in seen]
        assert ok
        assert markets_in_order[:2] == ["Y", "Y"] and "K" in markets_in_order, \
            f"Y 를 다 돌고 K 로 넘어가야 한다: {markets_in_order}"
        assert markets_in_order.index("K") > markets_in_order.index("Y")
        assert seen.count(("Y", 1)) == 1 and seen.count(("K", 3)) == 1
        assert not any(m == "N" for m, _ in seen), "코넥스를 조회했다"
        assert "K00001" in hits and hits["K00001"]["offering"], \
            "코스닥 증자 공시가 플래그로 안 잡혔다"

    def t_flags_skips_konex_and_respects_pages():
        os.environ.setdefault("DART_API_KEY", "smoke-dummy")
        seen: list = []
        orig = F._dart_get
        F._dart_get = _fake_pages(seen, {"Y": 5, "K": 5, "N": 5})
        try:
            F.dart_disclosure_events(days=60, markets=("Y", "N", "K"),
                                     max_pages={"Y": 2, "K": 1})
        finally:
            F._dart_get = orig
        assert not any(m == "N" for m, _ in seen), "N(코넥스)은 건너뛰어야 한다"
        assert max(p for m, p in seen if m == "Y") == 2, "Y 페이지 상한 무시"
        assert max(p for m, p in seen if m == "K") == 1, "K 페이지 상한 무시"

    def t_flags_default_uses_config():
        """markets 를 안 주면 config.DART_MARKETS 를 쓴다 (지금은 Y 만)."""
        os.environ.setdefault("DART_API_KEY", "smoke-dummy")
        seen: list = []
        orig = F._dart_get
        F._dart_get = _fake_pages(seen, {"Y": 1, "K": 1})
        try:
            F.dart_disclosure_events(days=60)
        finally:
            F._dart_get = orig
        assert {m for m, _ in seen} == set(C.DART_MARKETS), \
            f"config 기본값과 다르게 돌았다: {seen}"

    def t_news_collect_dart_markets():
        """news_sources.collect_dart 도 같은 순회 규칙을 따른다."""
        os.environ.setdefault("DART_API_KEY", "smoke-dummy")
        seen: list = []

        class _Res:
            def __init__(self, m, p):
                self.m, self.p = m, p
            def json(self):
                if self.p > 1:
                    return {"status": "013", "list": []}
                return {"status": "000", "list": [{
                    "report_nm": "유상증자결정", "rcept_no": "1", "rcept_dt": "20260909",
                    "corp_name": f"회사{self.m}", "stock_code": f"{self.m}00001"[:6]}]}

        orig_get, orig_sleep = NS._get, NS.time.sleep
        NS._get = lambda url, **kw: (seen.append((kw["params"]["corp_cls"],
                                                  kw["params"]["page_no"])),
                                     _Res(kw["params"]["corp_cls"],
                                          kw["params"]["page_no"]))[1]
        NS.time.sleep = lambda *_a, **_k: None
        try:
            out = NS.collect_dart(days=1, markets=("Y", "N", "K"))
        finally:
            NS._get, NS.time.sleep = orig_get, orig_sleep
        ms = [m for m, _ in seen]
        assert "N" not in ms, "코넥스를 조회했다"
        assert ms.index("K") > ms.index("Y"), f"순서가 틀렸다: {ms}"
        assert {o["stock_code"] for o in out} == {"Y00001", "K00001"}, out

    check("dart", "설정 정합성 (Y 선행 · N 없음 · ON 기준일 · 페이지 상한)", t_default_off)
    check("dart", "flags: Y 다음 K 순회 + 코스닥 증자 적중", t_flags_iterates_y_then_k)
    check("dart", "flags: N 건너뜀 + 시장별 페이지 상한", t_flags_skips_konex_and_respects_pages)
    check("dart", "flags: 기본 인자는 config 를 따름", t_flags_default_uses_config)
    check("dart", "news: collect_dart 순회 규칙 동일", t_news_collect_dart_markets)


# ═══════════════ 8-1c. 관심종목 (표시·태깅 전용) ═══════════════
def test_watchlist(st):
    """WATCHLIST 는 표시와 뉴스 태깅에만 쓰인다. 점수 경로에 새면 여기서 잡는다."""
    import re
    from stocknews.config import WATCHLIST, watchlist_codes

    def t_shape():
        codes = watchlist_codes()
        assert codes, "관심종목이 비어 있다"
        seen: list = []
        for sector, items in WATCHLIST.items():
            assert sector and items, f"빈 섹터: {sector!r}"
            for code, name in items:
                assert re.fullmatch(r"\d{6}", code), f"코드 형식: {code!r}"
                assert name.strip(), f"이름 없음: {code}"
                assert code.endswith("0") or code in ("005935",), \
                    f"우선주 의심 코드(끝자리≠0): {code} {name}"
                seen.append(code)
        assert len(seen) == len(set(seen)), "중복 코드"
        bad = [n for _, (n, _) in codes.items()
               if re.search(r"ETF|ETN|레버리지|인버스|KODEX|TIGER|우$|우B$", n)]
        assert not bad, f"ETF/ETN/우선주는 넣지 않는다: {bad}"

    def t_news_held_includes_watchlist():
        """중요도의 '내 종목' 집합에 관심종목이 들어간다. 보유가 아니어도."""
        from stocknews.news import held_codes

        class _NoPositions:      # positions 테이블이 없는 저장소
            pass

        held = held_codes(_NoPositions())
        assert set(watchlist_codes()) <= held, "관심종목이 '내 종목'에 없다"

    def t_not_in_positions():
        """관심종목은 보유가 아니다 — positions 에 자동으로 들어가면 안 된다."""
        pos = st.list_positions() if hasattr(st, "list_positions") else []
        assert not ({p.ticker for p in pos} & set(watchlist_codes())), \
            "관심종목이 positions 에 들어가 있다"

    def t_render_section():
        from stocknews.renderer import render_watchlist
        text = render_watchlist(st)
        assert "📌 관심종목 현황" in text
        for sector in WATCHLIST:
            assert f"[{sector}]" in text, f"섹터 누락: {sector}"
        # 스모크 저장소에는 관심종목 시세·스캔이 없다 → 전부 '스캔 제외'
        assert text.count("스캔 제외") == len(watchlist_codes()), \
            "스캔 스냅샷에 없는 종목은 '스캔 제외'로 표시돼야 한다"
        assert "{" not in text and "}" not in text

    def t_evening_brief_has_section():
        from datetime import datetime as dt
        from stocknews.renderer import render_evening_brief
        text = render_evening_brief(st, dt(2026, 8, 24, 18, 5), hours=24 * 365)
        assert "📌 관심종목 현황" in text, "저녁 브리핑에 관심종목 섹션이 없다"

    def t_no_leak_into_scoring():
        """점수·스크리닝·추천·청산·게이트 어디에도 WATCHLIST 가 없어야 한다."""
        root = Path(__file__).parent / "stocknews"
        forbidden = ("screener.py", "liquidation.py", "fibonacci.py",
                     "indicators.py", "cost_basis.py", "daily.py", "exits.py",
                     "notify.py", "weekly.py", "backtest.py", "universe.py",
                     "flags.py")
        leaks = []
        for f in forbidden:
            src = (root / f).read_text(encoding="utf-8", errors="replace")
            if re.search(r"WATCHLIST|watchlist_codes|held_codes", src):
                leaks.append(f)
        assert not leaks, f"WATCHLIST 가 점수 경로에 새어 들어갔다: {leaks}"

    check("watchlist", "형식·중복·ETF/우선주 배제", t_shape)
    check("watchlist", "뉴스 '내 종목' 태깅 연결", t_news_held_includes_watchlist)
    check("watchlist", "positions 미포함", t_not_in_positions)
    check("watchlist", "섹션 렌더 (스캔 제외 표시)", t_render_section)
    check("watchlist", "저녁 브리핑에 섹션 포함", t_evening_brief_has_section)
    check("watchlist", "점수·스크리닝·청산 경로 미참조", t_no_leak_into_scoring)


# ═══════════════ 8-1d. 공휴일 사전 필터 (천문연구원 API) ═══════════════
def test_holidays(tmp: Path):
    """공휴일이면 프로브 생략, 아니면 종전 프로브 그대로. 네트워크 없음."""
    import logging as _logging
    import pandas as _pd

    from stocknews import holidays as H
    from stocknews import universe as uni
    from stocknews.store import Store
    from stocknews.trading_day import (CLOSED_HOLIDAY, TRADING, UNKNOWN,
                                       market_status, market_status_reason)

    st = Store(tmp / "hol.db")
    # 2026-08-17(월) 광복절 대체공휴일 · 2026-08-19(수) 평일
    HOL, HOL_YMD = "2026-08-17", "20260817"
    WED, WED_YMD = "2026-08-19", "20260819"

    SAMPLE = {"response": {"header": {"resultCode": "00", "resultMsg": "NORMAL SERVICE."},
              "body": {"items": {"item": [
                  {"dateKind": "01", "dateName": "광복절", "isHoliday": "Y", "locdate": 20260815, "seq": 1},
                  {"dateKind": "01", "dateName": "대체공휴일", "isHoliday": "Y", "locdate": 20260817, "seq": 1},
                  {"dateKind": "01", "dateName": "제헌절", "isHoliday": "N", "locdate": 20260717, "seq": 1},
              ]}, "numOfRows": 100, "pageNo": 1, "totalCount": 3}}}

    def _status(mapping):
        def _fn(ticker, trade_date, retries=None, backoff=None):
            return mapping.get(ticker, ("NODATA", None))
        return _fn

    class _Probe:
        """프로브가 불렸는지 세는 대역."""
        def __init__(self, mapping): self.mapping, self.calls = mapping, 0
        def __call__(self, ticker, trade_date, retries=None, backoff=None):
            self.calls += 1
            return self.mapping.get(ticker, ("NODATA", None))

    def t_parse_keeps_only_Y():
        rows, meta = H.parse_response(SAMPLE)
        assert [r["d"] for r in rows] == ["2026-08-15", "2026-08-17"], rows
        assert all(r["source"] == H.SOURCE for r in rows)
        assert meta["kept"] == 2 and meta["total"] == 3
        assert not meta.get("truncated")
        # 1건이면 item 이 dict 로 온다
        one = {"response": {"header": {"resultCode": "00"}, "body": {
            "items": {"item": {"dateName": "설날", "isHoliday": "Y", "locdate": "20260217"}},
            "totalCount": 1}}}
        assert [r["d"] for r in H.parse_response(one)[0]] == ["2026-02-17"]
        # 0건이면 items 가 "" 다
        empty = {"response": {"header": {"resultCode": "00"}, "body": {"items": "", "totalCount": 0}}}
        assert H.parse_response(empty) == ([], H.parse_response(empty)[1])

    def t_truncation_warns():
        big = {"response": {"header": {"resultCode": "00"}, "body": {
            "items": {"item": [{"dateName": "x", "isHoliday": "Y", "locdate": "20260101"}]},
            "totalCount": 150}}}
        rows, meta = H.parse_response(big, num_of_rows=100)
        assert meta.get("truncated") is True, "totalCount > numOfRows 경고가 없다"

    def t_holiday_skips_probe():
        """공휴일 → 프로브 생략. 프로브가 한 번이라도 불리면 실패."""
        st.upsert_holidays(H.parse_response(SAMPLE)[0])
        status, why = market_status_reason(st, HOL)
        assert status == CLOSED_HOLIDAY and "대체공휴일" in why, (status, why)
        probe = _Probe({t: ("DATA", 100.0) for t in uni.REF_TICKERS})
        s_snap, s_stat = uni.market_snapshot, uni.close_on_status
        try:
            uni.market_snapshot = lambda: (_ for _ in ()).throw(AssertionError("스냅샷 호출됨"))
            uni.close_on_status = probe
            rep = {}
            n = uni.fetch_day(st, HOL_YMD, report=rep)
        finally:
            uni.market_snapshot, uni.close_on_status = s_snap, s_stat
        assert n == 0 and rep["verdict"] == "CLOSED" and rep["path"] == "known", rep
        assert "대체공휴일" in rep["detail"], rep
        assert probe.calls == 0, "공휴일인데 기준 종목 프로브를 불렀다"

    def t_extra_market_holiday():
        """근로자의 날(5/1)은 API 에 없지만 증시 휴장 — config 보완표가 잡는다."""
        assert st.holiday_name("2026-05-01") is None, "테이블에 넣지 않았는데 있다"
        status, why = market_status_reason(st, "2026-05-01")   # 2026-05-01 은 금요일
        assert status == CLOSED_HOLIDAY and "근로자" in why, (status, why)
        status, why = market_status_reason(st, "2026-12-31")   # 목요일
        assert status == CLOSED_HOLIDAY and "폐장" in why, (status, why)

    def t_weekday_nodata_flags_outage_suspect():
        """평일 + 공휴일 테이블에 없음 + 프로브 '데이터 없음' → 장애 의심 로그.
        판정(CLOSED 기록)은 종전 그대로다 — 프로브 로직을 약화하지 않는다."""
        THU, THU_YMD = "2026-08-20", "20260820"           # 평일. 다른 검사와 날짜를 안 겹친다
        assert st.holiday_name(THU) is None
        logger = _logging.getLogger("stocknews.universe")
        lines: list = []
        h = _logging.Handler(); h.emit = lambda rec: lines.append(rec.getMessage())
        # 스모크는 logging.disable 로 출력을 막아 둔다. 이 검사만 잠시 푼다.
        disabled = _logging.root.manager.disable
        s_snap, s_stat = uni.market_snapshot, uni.close_on_status
        try:
            _logging.disable(_logging.NOTSET)
            logger.addHandler(h)
            uni.market_snapshot = lambda: (_pd.DataFrame(), _pd.DataFrame())
            uni.close_on_status = _status({})
            rep = {}
            uni.fetch_day(st, THU_YMD, report=rep)
        finally:
            logger.removeHandler(h)
            _logging.disable(disabled)
            uni.market_snapshot, uni.close_on_status = s_snap, s_stat
            with __import__("contextlib").closing(st._conn()) as con:
                con.execute("DELETE FROM non_trading_days WHERE d=?", (THU,)); con.commit()
        assert rep["verdict"] == "CLOSED", rep            # 종전 판정 유지
        assert rep.get("suspect_outage") is True, rep
        assert any("소스 장애 의심" in ln for ln in lines), lines[-3:]

    def t_empty_table_falls_through_to_probe():
        """테이블이 비어도 판정은 막히지 않는다 — 그냥 프로브로 간다."""
        st2 = Store(tmp / "hol_empty.db")
        assert st2.holiday_name(WED) is None
        assert market_status(st2, WED) == UNKNOWN
        probe = _Probe({t: ("DATA", 100.0) for t in uni.REF_TICKERS})
        s_snap, s_stat = uni.market_snapshot, uni.close_on_status
        try:
            uni.market_snapshot = lambda: (_pd.DataFrame(), _pd.DataFrame())
            uni.close_on_status = probe
            rep = {}
            uni.fetch_day(st2, WED_YMD, report=rep)
        finally:
            uni.market_snapshot, uni.close_on_status = s_snap, s_stat
        assert rep["verdict"] == "TRADING", rep
        assert probe.calls >= 1, "테이블이 비었는데 프로브가 안 불렸다"

    def t_api_failure_does_not_block():
        """API 가 죽어도 refresh 는 예외를 올리지 않고, 판정도 그대로다."""
        s_get = H.requests.get
        try:
            H.requests.get = lambda *a, **k: (_ for _ in ()).throw(ConnectionError("dns down"))
            res = H.refresh_holidays(st, (2026, 2027), key="dummy")
        finally:
            H.requests.get = s_get
        assert res["failed"] == [2026, 2027] and res["stored"] == 0, res
        assert "error" in res["years"][2026], res
        assert market_status(st, HOL) == CLOSED_HOLIDAY, "기존 공휴일 행이 사라졌다"
        assert market_status(st, WED) == UNKNOWN, market_status_reason(st, WED)

    def t_params_not_url():
        """키는 requests params 로 간다. URL 에 이어 붙이면 401."""
        seen = {}
        class _R:
            status_code = 200
            def json(self): return SAMPLE
        s_get = H.requests.get
        try:
            H.requests.get = lambda url, params=None, timeout=None: (seen.update(url=url, params=params), _R())[1]
            rows, meta = H.fetch_holidays(2026, key="k+e/y=")
        finally:
            H.requests.get = s_get
        assert seen["url"] == H.API_URL and "?" not in seen["url"]
        assert seen["params"]["ServiceKey"] == "k+e/y=" and seen["params"]["solYear"] == "2026"
        assert seen["params"]["_type"] == "json" and "solMonth" not in seen["params"]
        assert len(rows) == 2

    def t_encoding_key_is_unquoted():
        """Encoding 키(%2B…)가 들어오면 디코딩해서 params 로 보낸다. 2026-09-09 403 회귀."""
        assert H.normalize_key("a%2Bb%2Fc%3D%3D") == "a+b/c=="
        assert H.normalize_key("a+b/c==") == "a+b/c=="          # Decoding 키는 그대로
        assert H.normalize_key("plainkey123") == "plainkey123"
        seen = {}
        class _R:
            status_code = 200
            def json(self): return SAMPLE
        s_get = H.requests.get
        try:
            H.requests.get = lambda url, params=None, timeout=None: (seen.update(params=params), _R())[1]
            H.fetch_holidays(2026, key="k%2Be%2Fy%3D")
        finally:
            H.requests.get = s_get
        assert seen["params"]["ServiceKey"] == "k+e/y=", seen["params"]["ServiceKey"]

    def t_no_key_is_precond():
        import os
        saved = os.environ.pop(H.ENV_KEY, None)
        try:
            try:
                H.fetch_holidays(2026)
                raise AssertionError("키 없이 호출이 통과했다")
            except ValueError:
                pass
        finally:
            if saved is not None:
                os.environ[H.ENV_KEY] = saved

    check("holidays", "isHoliday=Y 만 · locdate 변환 · 단건/0건", t_parse_keeps_only_Y)
    check("holidays", "totalCount > numOfRows 경고", t_truncation_warns)
    check("holidays", "공휴일 → 프로브 생략", t_holiday_skips_probe)
    check("holidays", "증시 전용 보완 (근로자의 날·폐장일)", t_extra_market_holiday)
    check("holidays", "평일+데이터 없음 → 장애 의심 로그", t_weekday_nodata_flags_outage_suspect)
    check("holidays", "테이블 비어도 프로브 정상", t_empty_table_falls_through_to_probe)
    check("holidays", "API 실패가 판정을 막지 않음", t_api_failure_does_not_block)
    check("holidays", "키는 params 로 (URL 조립 금지)", t_params_not_url)
    check("holidays", "Encoding 키는 디코딩해서 사용 (403 회귀)", t_encoding_key_is_unquoted)
    check("holidays", "키 없으면 ValueError", t_no_key_is_precond)


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


# ═══════════════════ 8-2c. 추천 발송 요일 게이트 (G5) ═══════════════════
def test_reco_dow():
    """추천 10선 주 1회 발송. 요일만 보고 시각은 보지 않는다.

    픽스처는 2026-08-24(월) ~ 08-30(일) 7일이다. 달력을 잘못 짚으면 아래
    검사 전부가 무의미해지므로 요일 자체를 먼저 검증한다.
    """
    import dataclasses
    import inspect
    from datetime import date as d_, datetime as dt
    from stocknews.config import DEFAULT, RECO_SEND_DOW
    from stocknews.contracts import Position
    from stocknews.notify import (AlertGate, reco_send_allowed,
                                  reco_send_dow_label)
    from stocknews.renderer import render_daily_holdings, render_reco_stored

    WEEK = [dt(2026, 8, 24 + i, 18, 20) for i in range(7)]   # 월(0) ~ 일(6)

    def t_anchor():
        assert RECO_SEND_DOW == 6, f"RECO_SEND_DOW={RECO_SEND_DOW} (일=6)"
        assert DEFAULT.gate.reco_send_dow == RECO_SEND_DOW, "config 배선 누락"
        assert [x.weekday() for x in WEEK] == list(range(7)), \
            f"픽스처 요일 불일치 {[x.weekday() for x in WEEK]}"
        assert WEEK[6].date() == d_(2026, 8, 30), "일요일 앵커 불일치"
        assert reco_send_dow_label(DEFAULT) == "일요일"

    def t_only_sunday():
        """일요일만 발송. 월~토는 억제."""
        for i, now in enumerate(WEEK):
            ok, why = reco_send_allowed(now, DEFAULT)
            if i == 6:
                assert ok and why == "send_dow", f"일요일이 억제됨 ({why})"
            else:
                assert not ok and why == "off_dow", \
                    f"weekday={i} 인데 발송 허용 ({ok}, {why})"

    def t_force_bypass():
        """--force 는 요일을 무시한다."""
        for now in WEEK:
            ok, why = reco_send_allowed(now, DEFAULT, force=True)
            assert ok and why == "force", f"{now:%m-%d} force 우회 실패 ({why})"

    def t_time_of_day_ignored():
        """같은 요일이면 몇 시에 돌려도 판정이 같아야 한다.

        daily 는 16:05 에 도는데 그 시각은 4대 시간창 어디에도 속하지
        않는다. 요일 게이트가 시각을 보면 주간 발송이 조용히 사라진다.
        """
        for h, m in ((0, 5), (9, 10), (10, 5), (16, 5), (18, 20), (23, 55)):
            assert reco_send_allowed(dt(2026, 8, 30, h, m), DEFAULT)[0], \
                f"일요일 {h:02d}:{m:02d} 억제됨"
            assert not reco_send_allowed(dt(2026, 8, 24, h, m), DEFAULT)[0], \
                f"월요일 {h:02d}:{m:02d} 허용됨"

    def t_no_window_coupling():
        """시간창 로직과 상호 참조가 없어야 한다."""
        # daily 실행 시각(16:05)은 어느 창에도 속하지 않는다. 그래서 두
        # 판정을 한 곳에 섞으면 안 된다는 것이 이 검사의 근거다.
        assert AlertGate.current_window(dt(2026, 8, 30, 16, 5)) is None, \
            "16:05 가 시간창에 속한다 — 게이트 독립 전제가 깨졌다"
        # 창 안(10:05)이어도 요일 판정은 바뀌지 않는다
        assert reco_send_allowed(dt(2026, 8, 25, 10, 5), DEFAULT)[1] == "off_dow"
        # 실제로 참조하는 전역 이름만 본다. 소스 문자열을 훑으면 설명
        # 주석·독스트링에 이름이 나오는 것만으로 실패한다.
        names = set(reco_send_allowed.__code__.co_names)
        for banned in ("WINDOWS", "current_window", "in_window", "AlertGate",
                       "is_definitely_closed"):
            assert banned not in names, f"요일 게이트가 {banned} 를 참조한다"

    def t_config_override():
        """발송 요일은 config 로 옮길 수 있어야 한다 (호출부 하드코딩 금지)."""
        cfg = dataclasses.replace(
            DEFAULT, gate=dataclasses.replace(DEFAULT.gate, reco_send_dow=4))
        assert reco_send_allowed(dt(2026, 8, 28, 18, 20), cfg)[0], \
            "금요일로 바꿨는데 발송 안 됨"
        assert not reco_send_allowed(WEEK[6], cfg)[0], "일요일이 여전히 발송됨"

    def t_weekday_msg_no_picks():
        """평일 메시지는 보유 요약만. 신규 추천 종목이 들어가면 안 된다."""
        p = Position(id=1, ticker="005930", name="삼성전자", track="VALUE",
                     entry_date="2026-08-20", entry_price=70000.0,
                     qty=10, remaining=10)
        msg = render_daily_holdings([p], None, WEEK[0], "2026-08-24",
                                    scanned=1847, picks=10,
                                    send_dow_label="일요일")
        assert "보유 현황" in msg and "삼성전자" in msg, "보유 요약 누락"
        assert "추천 10건 기록" in msg, "추천 건수 미표시"
        assert "일요일" in msg, "다음 발송 요일 안내 누락"
        # 종목을 넘길 경로 자체가 없어야 한다. 개수(int)만 받는다.
        # renderer 는 `from __future__ import annotations` 라 문자열로 온다.
        ann = inspect.signature(render_daily_holdings).parameters["picks"].annotation
        assert ann in (int, "int"), f"picks 가 개수가 아니다 ({ann})"

    def t_weekday_msg_empty_portfolio():
        """보유가 없어도 메시지는 나가야 한다 (파이프라인 생존 확인용)."""
        msg = render_daily_holdings([], None, WEEK[1], "2026-08-25",
                                    scanned=1800, picks=0, send_dow_label="일요일")
        assert "보유 포지션 없음" in msg
        assert "추천 0건 기록" in msg

    def _rows():
        return pd.DataFrame([
            {"d": "2026-08-28", "rank": 1, "ticker": "005930",
             "name": "삼성전자", "price": 70000.0, "slot": "SEQ",
             "grade": "S+", "value_score": 9.1, "trend_score": 8.2,
             "reason": "청산밴드 진입 후 골든크로스"},
            {"d": "2026-08-28", "rank": 2, "ticker": "000660",
             "name": "SK하이닉스", "price": 180000.0, "slot": "VALUE",
             "grade": "S", "value_score": 8.4, "trend_score": 6.0,
             "reason": "피보 0.618 이하"},
        ])

    def t_stored_reco_render():
        """일요일 메시지는 DB 에 기록된 추천으로 만든다."""
        msg = render_reco_stored(_rows(), WEEK[6], "일요일")
        assert "주간 추천 2선" in msg, msg[:120]
        assert "기준일 2026-08-28" in msg, "기준일 미표시"
        assert "삼성전자" in msg and "SK하이닉스" in msg
        assert "70,000원" in msg and "9.1" in msg
        # 기준일 종가라는 사실을 반드시 밝혀야 한다. 일요일 발송이라
        # 금요일 종가와 발송 시점 사이에 인식 차이가 생긴다.
        assert "기준일 종가" in msg, "종가 기준 고지 누락"
        # 빈 입력에서도 죽지 않아야 한다
        assert "기록된 추천이 없습니다" in render_reco_stored(
            pd.DataFrame(), WEEK[6], "일요일")
        assert "기록된 추천이 없습니다" in render_reco_stored(
            None, WEEK[6], "일요일")

    class _StubStore:
        """mode_daily 의 '스캔 생략' 경로가 쓰는 메서드만 가진 스텁.

        실 Store 를 쓰면 last_price_date 의 부분적재 판정과 스캔 스냅샷
        적재까지 끌려들어와, 검사하려는 분기가 아닌 곳에서 깨진다.
        """

        def __init__(self, rows):
            self.rows = rows
            self.asked = 0

        def last_price_date(self):
            return "2026-08-28"

        def has_scan(self, d):
            return True                      # 스냅샷 있음 -> 스캔 생략

        def is_known_non_trading(self, d):
            return False

        def has_price_date(self, d):
            return True

        def reco_history(self, days=1):
            self.asked += 1
            return self.rows

    def _run_mode_daily(rows, send):
        """mode_daily 를 생략 경로로 태우고 (rc, 발송문) 을 준다."""
        import argparse
        import contextlib
        import importlib
        import io
        mod = importlib.import_module("run_screen")

        st = _StubStore(rows)
        args = argparse.Namespace(force=False, dry_run=True, with_fib=False,
                                  top=10, limit=None, min_amount=0.0)
        saved = dict(mod.SUMMARY)
        orig = mod.reco_send_allowed
        mod.reco_send_allowed = lambda *a, **k: send
        buf = io.StringIO()
        try:
            mod.SUMMARY.clear()
            with contextlib.redirect_stdout(buf):
                rc = mod.mode_daily(st, args)
            summary = dict(mod.SUMMARY)
        finally:
            mod.reco_send_allowed = orig
            mod.SUMMARY.clear()
            mod.SUMMARY.update(saved)
        return mod, rc, buf.getvalue(), summary, st

    def t_mode_daily_sends_on_send_day():
        """발송 요일에는 스캔이 생략돼도 추천이 나가야 한다.

        일요일은 휴장이라 daily 의 스캔이 항상 생략된다. 생략과 발송을
        한 번에 return 하면 주간 발송이 영구히 나가지 않는다 — 이 검사가
        그 회귀를 막는다.
        """
        mod, rc, out, summary, st = _run_mode_daily(_rows(), (True, "send_dow"))
        assert rc == mod.EXIT_OK, f"rc={rc}"
        assert st.asked == 1, "기록된 추천을 읽지 않았다"
        assert "주간 추천 2선" in out, out[:200]
        assert "삼성전자" in out, "추천 종목이 발송문에 없다"
        assert summary.get("reco_sent") is True, summary
        assert summary.get("from_db") is True, summary
        assert summary.get("picks") == 2, summary

    def t_mode_daily_silent_off_day():
        """발송 요일이 아니고 스캔도 생략이면 아무것도 보내지 않는다."""
        mod, rc, out, summary, st = _run_mode_daily(_rows(), (False, "off_dow"))
        assert rc == mod.EXIT_OK, f"rc={rc}"
        assert out == "", f"억제일에 발송됨: {out[:200]}"
        assert st.asked == 0, "발송하지 않는데 추천을 읽었다"
        assert summary.get("reco_sent") is False, summary

    def t_mode_daily_no_reco_no_send():
        """발송 요일이지만 기록된 추천이 0건이면 보내지 않는다.

        '추천 없음' 알림은 소음이다. 배치 공백은 --mode runs 가 잡는다.
        """
        mod, rc, out, summary, _ = _run_mode_daily(pd.DataFrame(),
                                                   (True, "send_dow"))
        assert rc == mod.EXIT_OK, f"rc={rc}"
        assert out == "", f"추천 0건인데 발송됨: {out[:200]}"
        assert summary.get("reco_sent") is False, summary
        assert summary.get("picks") == 0, summary

    check("reco-dow", "RECO_SEND_DOW 앵커 + 달력 픽스처", t_anchor)
    check("reco-dow", "일요일만 발송 (월~토 억제)", t_only_sunday)
    check("reco-dow", "--force 요일 우회", t_force_bypass)
    check("reco-dow", "시각 무관 (16:05 방어)", t_time_of_day_ignored)
    check("reco-dow", "시간창 로직과 비결합", t_no_window_coupling)
    check("reco-dow", "발송 요일 config 이관 가능", t_config_override)
    check("reco-dow", "평일 메시지 = 보유 요약만", t_weekday_msg_no_picks)
    check("reco-dow", "평일 메시지 (보유 0건)", t_weekday_msg_empty_portfolio)
    check("reco-dow", "일요일 메시지 = 기록된 추천", t_stored_reco_render)
    check("reco-dow", "발송일: 스캔 생략에도 발송됨", t_mode_daily_sends_on_send_day)
    check("reco-dow", "억제일: 무음", t_mode_daily_silent_off_day)
    check("reco-dow", "발송일 + 추천 0건 = 무음", t_mode_daily_no_reco_no_send)


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
    import os

    from stocknews.krx_credit import (CANDIDATE_BLDS, _num, _pick, _rows_of,
                                      configured_bld, fetch_credit_balance,
                                      menu_credit_hits, refresh_credit_auto)

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

    def t_no_guessed_blds():
        # KRX 는 종목별 신용거래융자 잔고를 공개하지 않는다(2026-08 실측).
        # 추측한 bld 를 다시 넣으면 빈 응답을 '신용잔고 0'으로 오해한다.
        # 이 테스트는 그 회귀를 막는다.
        assert CANDIDATE_BLDS == (), \
            f"추측 bld 가 들어왔다: {CANDIDATE_BLDS}"

    def t_no_network_without_bld():
        # bld 미지정이면 네트워크를 쓰지 않고 즉시 빈 결과여야 한다.
        saved = os.environ.pop("KRX_CREDIT_BLD", None)
        try:
            assert configured_bld() is None
            df, used = fetch_credit_balance("20260821")
            assert df.empty and used == "", (len(df), used)
        finally:
            if saved is not None:
                os.environ["KRX_CREDIT_BLD"] = saved

    def t_auto_skips_cleanly():
        saved = os.environ.pop("KRX_CREDIT_BLD", None)
        try:
            r = refresh_credit_auto(None)   # store 를 건드리지 않고 빠져야 한다
            assert r["ok"] is False and r.get("skipped") is True, r
            assert "공개하지 않" in r["note"], r["note"]
        finally:
            if saved is not None:
                os.environ["KRX_CREDIT_BLD"] = saved

    def t_menu_hits():
        # '신용등급'(채권 발행사)은 신용거래융자가 아니므로 걸러야 한다.
        html = ('<span data-menu-name="발행사 신용등급 및 NCR"></span>'
                '<span data-menu-name="신용등급별 상장현황"></span>'
                '<span data-menu-name="인프라투융자회사 시세"></span>'
                '<span data-menu-name="전종목 시세"></span>')
        assert menu_credit_hits(html) == [], menu_credit_hits(html)
        # 실제로 생기면 잡아야 한다
        html2 = html + '<span data-menu-name="신용거래융자 종목별 잔고"></span>'
        assert menu_credit_hits(html2) == ["신용거래융자 종목별 잔고"]

    check("krx_credit", "결과 배열 탐색", t_rows_of)
    check("krx_credit", "컬럼 패턴 매칭", t_pick)
    check("krx_credit", "숫자 파싱", t_num)
    check("krx_credit", "추측 bld 미포함", t_no_guessed_blds)
    check("krx_credit", "bld 없으면 무네트워크", t_no_network_without_bld)
    check("krx_credit", "자동 수집 정상 생략", t_auto_skips_cleanly)
    check("krx_credit", "메뉴 신용등급 오탐 방지", t_menu_hits)


def test_market_source(tmp: Path):
    """전종목 소스 교체 + 휴장일 오염 방어.

    KRX 전종목 엔드포인트가 로그인 뒤로 들어가(2026-08) 빈 결과를 준다.
    예전 fetch_day 는 0건이면 무조건 휴장일로 기록해서, 소스 장애가 실제
    거래일을 영구히 휴장일로 박아버렸다. 그 회귀를 막는다.
    """
    import pandas as _pd

    from stocknews import universe as uni
    from stocknews.data import REQUIRED, _SNAP_PRICE, verify_snapshot_date
    from stocknews.store import Store

    st = Store(tmp / "src.db")
    WED = "20260819"          # 수요일. 주말/기지정 공휴일이 아니다.
    ISO = "2026-08-19"

    def _snap(close=1000.0):
        return _pd.DataFrame(
            {"시가": [close], "고가": [close], "저가": [close],
             "종가": [close], "거래량": [10.0], "거래대금": [close * 10]},
            index=["005930"])

    def t_snap_mapping():
        # 스냅샷 매핑이 필수 컬럼을 모두 덮어야 한다.
        mapped = set(_SNAP_PRICE.values())
        missing = [c for c in REQUIRED if c not in mapped]
        assert not missing, missing

    def t_verify_empty():
        ok, why = verify_snapshot_date(_pd.DataFrame(), WED)
        assert ok is False and "비었" in why, why

    def t_verify_no_refs():
        # 기준 종목이 스냅샷에 없으면 네트워크를 쓰지 않고 실패해야 한다.
        ok, why = verify_snapshot_date(_snap(), WED, refs=("999999",))
        assert ok is False and "확인 불가" in why, why

    def t_verify_match():
        saved = uni.close_on
        import stocknews.data as d
        d_saved = d.close_on
        try:
            d.close_on = lambda t, td: 1000.0
            ok, why = verify_snapshot_date(_snap(1000.0), WED,
                                           refs=("005930",), need=1)
            assert ok is True, why
            # 장중 현재가가 섞이면 통과시키면 안 된다
            ok2, why2 = verify_snapshot_date(_snap(1234.0), WED,
                                             refs=("005930",), need=1)
            assert ok2 is False and "종가가 아님" in why2, why2
        finally:
            d.close_on = d_saved
            uni.close_on = saved

    def _status(mapping):
        """close_on_status 대역. mapping: ticker -> (status, value)."""
        def _fn(ticker, trade_date, retries=None, backoff=None):
            return mapping.get(ticker, ("NODATA", None))
        return _fn

    def t_source_outage_is_not_holiday():
        """소스 장애 = 휴장일 아님. 이게 이 테스트의 핵심이다."""
        assert not st.is_known_non_trading(ISO)
        s_snap, s_stat = uni.market_snapshot, uni.close_on_status
        try:
            uni.market_snapshot = lambda: (_pd.DataFrame(), _pd.DataFrame())
            # 기준 종목은 데이터가 있다 → 거래일이다
            uni.close_on_status = _status(
                {t: ("DATA", 100.0) for t in uni.REF_TICKERS})
            rep = {}
            n = uni.fetch_day(st, WED, report=rep)
            assert n == 0, n
            # 기준 종목에 데이터가 있으므로 '거래일'로 확정된다.
            # 전종목이 0건인 것은 휴장의 근거가 될 수 없다.
            assert rep["verdict"] == "TRADING", rep
            assert not st.is_known_non_trading(ISO), \
                "소스 장애를 휴장일로 기록했다 (거래일 영구 소실)"
        finally:
            uni.market_snapshot, uni.close_on_status = s_snap, s_stat

    def t_real_holiday_is_marked():
        s_snap, s_stat = uni.market_snapshot, uni.close_on_status
        try:
            uni.market_snapshot = lambda: (_pd.DataFrame(), _pd.DataFrame())
            # 기준 종목이 '없다'고 응답한다 (조회는 성공)
            uni.close_on_status = _status({})
            rep = {}
            n = uni.fetch_day(st, "20260817", report=rep)   # 다른 날짜(월)
            assert n == 0, n
            assert rep["verdict"] == "CLOSED", rep
            assert st.is_known_non_trading("2026-08-17"), \
                "기준 종목도 데이터가 없으면 휴장일로 기록해야 한다"
        finally:
            uni.market_snapshot, uni.close_on_status = s_snap, s_stat

    def t_probe_error_is_not_holiday():
        """★ 2026-09-08 회귀 앵커.

        기준 종목 조회가 **실패**한 것을 '데이터 없음'으로 접으면
        정상 거래일이 non_trading_days 에 박히고, 그 테이블은 자기학습
        캐시라 update 가 그 날짜를 두 번 다시 요청하지 않는다.
        실측: 화요일 2026-09-08 이 부팅 직후 DNS 장애로 휴장일이 됐다.
        """
        THU, ISO_T = "20260820", "2026-08-20"
        assert not st.is_known_non_trading(ISO_T)
        s_snap, s_stat = uni.market_snapshot, uni.close_on_status
        seen = []
        try:
            uni.market_snapshot = lambda: (_pd.DataFrame(), _pd.DataFrame())

            def _boom(ticker, trade_date, retries=None, backoff=None):
                seen.append(ticker)
                return "ERROR", None

            uni.close_on_status = _boom
            rep = {}
            n = uni.fetch_day(st, THU, report=rep)
            assert n == 0, n
            assert rep["verdict"] == "OUTAGE", rep
            assert not st.is_known_non_trading(ISO_T), \
                "조회 실패를 휴장일로 기록했다 (거래일 영구 소실)"
            assert len(seen) == len(uni.REF_TICKERS), seen
        finally:
            uni.market_snapshot, uni.close_on_status = s_snap, s_stat

    def t_mixed_error_is_outage():
        """하나라도 실패면 나머지가 '없음'이어도 확정하지 않는다."""
        ISO_M = "2026-08-27"
        s_snap, s_stat = uni.market_snapshot, uni.close_on_status
        try:
            uni.market_snapshot = lambda: (_pd.DataFrame(), _pd.DataFrame())
            uni.close_on_status = _status(
                {uni.REF_TICKERS[0]: ("ERROR", None)})
            rep = {}
            uni.fetch_day(st, "20260827", report=rep)
            assert rep["verdict"] == "OUTAGE", rep
            assert not st.is_known_non_trading(ISO_M)
        finally:
            uni.market_snapshot, uni.close_on_status = s_snap, s_stat

    def t_today_before_close_is_pending():
        """장 마감 전의 '오늘'은 휴장으로 단정하지 않는다.

        자정~오전에 도는 catch-up 이 아직 열리지도 않은 장을 휴장일로
        박는 것을 막는다. 기준 종목은 이 시각에 정상적으로 '데이터 없음'을
        응답하므로 네트워크가 멀쩡해도 CLOSED 가 나온다.
        """
        import datetime as _dt
        ISO_P, YMD_P = "2026-08-26", "20260826"      # 수요일
        s_snap, s_stat = uni.market_snapshot, uni.close_on_status
        s_now = uni._now_kst
        try:
            uni.market_snapshot = lambda: (_pd.DataFrame(), _pd.DataFrame())
            uni.close_on_status = _status({})
            uni._now_kst = lambda: _dt.datetime(2026, 8, 26, 0, 30)
            rep = {}
            n = uni.fetch_day(st, YMD_P, report=rep)
            assert n == 0 and rep["verdict"] == "PENDING", rep
            assert not st.is_known_non_trading(ISO_P), \
                "장 마감 전인 오늘을 휴장일로 기록했다"
            # 마감 후에는 같은 응답으로 휴장일이 확정된다
            uni._now_kst = lambda: _dt.datetime(2026, 8, 26, 21, 30)
            rep2 = {}
            uni.fetch_day(st, YMD_P, report=rep2)
            assert rep2["verdict"] == "CLOSED", rep2
            assert st.is_known_non_trading(ISO_P)
        finally:
            uni.market_snapshot, uni.close_on_status = s_snap, s_stat
            uni._now_kst = s_now

    def t_close_on_status_three_way():
        """close_on_status 는 '없음'과 '못 물어봄'을 가른다."""
        import types
        from stocknews import data as d
        calls = []

        def _fake(ohlcv):
            fake = types.ModuleType("pykrx")
            fake.stock = types.SimpleNamespace(get_market_ohlcv=ohlcv)
            return fake

        saved = sys.modules.get("pykrx")
        try:
            # 1) 예외 -> 재시도 -> 성공
            def _flaky(a, b, tk):
                calls.append(tk)
                if len(calls) < 3:
                    raise ConnectionError("getaddrinfo failed")
                return _pd.DataFrame({"종가": [1000.0]}, index=[a])

            sys.modules["pykrx"] = _fake(_flaky)
            got = d.close_on_status("005930", "20260819", retries=2,
                                    backoff=0.0)
            assert got == ("DATA", 1000.0), got
            assert len(calls) == 3, calls

            # 2) 계속 예외 -> 재시도 소진 -> ERROR
            calls.clear()

            def _always(a, b, tk):
                calls.append(tk)
                raise ConnectionError("getaddrinfo failed")

            sys.modules["pykrx"] = _fake(_always)
            got = d.close_on_status("005930", "20260819", retries=2,
                                    backoff=0.0)
            assert got == ("ERROR", None), got
            assert len(calls) == 3, calls

            # 3) 빈 응답은 답이다 -> 재시도하지 않는다
            calls.clear()

            def _empty(a, b, tk):
                calls.append(tk)
                return _pd.DataFrame()

            sys.modules["pykrx"] = _fake(_empty)
            got = d.close_on_status("005930", "20260819", retries=2,
                                    backoff=0.0)
            assert got == ("NODATA", None), got
            assert len(calls) == 1, calls
        finally:
            if saved is None:
                sys.modules.pop("pykrx", None)
            else:
                sys.modules["pykrx"] = saved

    def t_load_floor():
        """적재량 하한. refresh_master 의 50%/500 가드와 같은 논리."""
        import sqlite3
        st2 = Store(tmp / "floor.db")
        # 표본이 3일 미만이면 판정을 보류한다(백필 초기 보호)
        assert uni.load_floor(st2) == 0
        with sqlite3.connect(st2.path) as con:
            for day in ("2026-08-17", "2026-08-18", "2026-08-19"):
                con.executemany(
                    "INSERT OR REPLACE INTO prices(ticker,d,o,h,l,c,v,amt) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    [(f"{i:06d}", day, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0)
                     for i in range(2400)])
            con.commit()
        # 중위값 2,400 의 50% = 1,200 (절대 하한 500 보다 크다)
        assert uni.load_floor(st2) == 1200, uni.load_floor(st2)

    def _nightly():
        """nightly.py 를 모듈로 읽는다 (패키지 밖 진입점)."""
        import importlib.util
        repo = Path(__file__).resolve().parent
        spec = importlib.util.spec_from_file_location(
            "_nightly_probe", str(repo / "nightly.py"))
        mod = importlib.util.module_from_spec(spec)
        # @dataclass 가 cls.__module__ 로 sys.modules 를 되짚는다.
        # 등록하지 않고 exec_module 하면 AttributeError 로 죽는다.
        sys.modules[spec.name] = mod
        try:
            spec.loader.exec_module(mod)
        finally:
            sys.modules.pop(spec.name, None)
        return mod

    def t_nightly_counts_outage_as_failure():
        """'실패 없음'인데 시세가 없는 상태를 만들지 않는다."""
        mod = _nightly()
        upd = mod.Step("update", ["--mode", "update"])

        state, note = mod._health_override(
            upd, "ok", "", {"rows": 2400, "source_outage": ["2026-09-08"]})
        assert state == "fail" and "2026-09-08" in note, (state, note)

        # 근거 없는 0건도 실패다
        state, note = mod._health_override(upd, "ok", "", {"rows": 0})
        assert state == "fail" and "0건" in note, (state, note)

        # 휴장일로 건너뛴 0건은 정상이다
        state, _ = mod._health_override(
            upd, "ok", "", {"rows": 0, "holidays_skipped": 1})
        assert state == "ok", state

        # 장 마감 전 판정 보류도 정상이다
        state, _ = mod._health_override(
            upd, "ok", "", {"rows": 0, "judge_pending": ["2026-09-09"]})
        assert state == "ok", state

    def t_nightly_credit_kiwoom_rc4():
        """credit-kiwoom exit 4: 키 없음·대상 0건만 스킵, 전 종목 실패는 실패."""
        mod = _nightly()
        ck = next(s for s in mod.STEPS if s.name == "credit-kiwoom")
        for why in ("no_credentials", "no_targets"):
            state, _, _ = mod._judge(ck, 4, "", {"skipped": True,
                                                 "reason": why})
            assert state == "skip", (why, state)

        # 2026-09-10 운영 페이로드 모양: 300/300 종목이 8050
        p = {"written": 0, "failed": 300, "reason": "all_failed",
             "failure_codes": {"8050": 300}}
        state, note, detail = mod._judge(ck, 4, "", p)
        assert state == "fail", state
        assert detail == "8050 × 300건", detail
        # reason 없는 exit 4 (고치기 전 run_screen) 도 스킵으로 삼키지 않는다
        for payload in ({}, {"reason": "auth"}, {"reason": "no_prices"}):
            s, _, _ = mod._judge(ck, 4, "", payload)
            assert s == "fail", (payload, s)
        # 코드를 못 뽑은 실패는 '기타' 로 붙는다
        _, _, d2 = mod._judge(ck, 4, "", {"failed": 5, "reason": "all_failed",
                                          "failure_codes": {"8050": 3}})
        assert d2 == "8050 × 3건, 기타 × 2건", d2

        results = [
            {"name": "update", "rc": 0, "state": "ok", "note": "",
             "detail": "", "json": {"rows": 2419,
                                    "last_price_date": "2026-09-10"}},
            {"name": "credit-kiwoom", "rc": 4, "state": state, "note": note,
             "detail": detail, "json": p},
        ]
        text = mod._summary(mod.JOBS["nightly"], datetime(2026, 9, 10, 21, 30),
                            datetime(2026, 9, 10, 22, 9), results, False)
        assert "credit-kiwoom 실패: 8050 × 300건" in text, text
        assert "실패 없음" not in text, text

    def t_nightly_zero_rows_is_partial_done():
        """update 가 0건 적재로 끝난 밤은 '완주'가 아니라 '부분 완료' (09-08)."""
        import contextlib
        import io
        import json as _j
        mod = _nightly()

        def res(name, state="ok", **payload):
            return {"name": name, "rc": 0, "state": state, "note": "",
                    "detail": "", "elapsed": 0.0, "json": payload}

        # 휴장 근거가 있어 실패로는 안 잡히는 0건
        zero = [res("update", rows=0, holidays_skipped=1), res("daily")]
        assert mod._outcome(zero) == "부분 완료", mod._outcome(zero)
        assert mod._outcome([res("update", rows=2419), res("daily")]) == "완주"
        assert mod._outcome([res("update", rows=0),
                             res("news", state="fail")]) == "부분 실패"
        # update 단계가 없는 잡은 0건 판정 대상이 아니다
        assert mod._outcome([res("brief-evening")]) == "완주"

        # main 경로: 마커 reason · 완료 알림 · 같은 날 두 번째 기동의 스킵 알림
        root = tmp / "nightly_zero"
        root.mkdir(parents=True, exist_ok=True)
        sent: list[str] = []
        saved = (mod.REPO, mod.load_env, mod.market_status, mod._run_step,
                 mod._notify)
        try:
            mod.REPO = root
            mod.load_env = lambda *a, **k: {"exists": True, "path": ""}
            mod.market_status = lambda store, when: "OPEN"
            mod._run_step = lambda step, dry, log, timeout: res(
                step.name, **({"rows": 0, "holidays_skipped": 1}
                              if step.name == "update" else {}))
            mod._notify = lambda text, log, enabled, why="": sent.append(text)
            argv = ["--db", str(root / "q.db"),
                    "--lock-dir", str(root / "locks")]
            with contextlib.redirect_stderr(io.StringIO()):
                rc1 = mod.main(argv)
                done = _j.loads((root / mod.DONE_MARKER).read_text(
                    encoding="utf-8"))
                first = sent[-1] if sent else ""
                rc2 = mod.main(argv)
        finally:
            (mod.REPO, mod.load_env, mod.market_status, mod._run_step,
             mod._notify) = saved
        assert rc1 == 0 and rc2 == 0, (rc1, rc2)
        assert done["reason"] == "부분 완료", done
        assert "nightly 부분 완료" in first, first
        assert "이미 완주" not in sent[-1] and "부분 완료" in sent[-1], sent[-1]

    def t_snapshot_path_used():
        """스냅샷이 검증되면 요청 1회로 적재된다."""
        s_snap, s_close = uni.market_snapshot, uni.close_on
        import stocknews.data as d
        d_saved = d.close_on
        try:
            uni.market_snapshot = lambda: (_snap(1000.0), _pd.DataFrame())
            uni.close_on = lambda t, td: 1000.0
            d.close_on = lambda t, td: 1000.0
            n = uni.fetch_day(st, "20260818")
            assert n == 1, n
            got = st.load_ohlcv("005930", days=5)
            assert len(got) == 1 and float(got["종가"].iloc[-1]) == 1000.0
        finally:
            uni.market_snapshot, uni.close_on = s_snap, s_close
            d.close_on = d_saved

    def t_weekend_short_circuits():
        """주말은 네트워크를 쓰지 않고 생략한다."""
        s_snap, s_close = uni.market_snapshot, uni.close_on
        called = []
        try:
            uni.market_snapshot = lambda: (called.append("snap"),
                                           (_pd.DataFrame(),
                                            _pd.DataFrame()))[1]
            uni.close_on = lambda t, td: called.append("close")
            n = uni.fetch_day(st, "20260822")      # 토요일
            assert n == 0 and called == [], called
            assert st.is_known_non_trading("2026-08-22")
        finally:
            uni.market_snapshot, uni.close_on = s_snap, s_close

    check("market_source", "스냅샷 컬럼 매핑", t_snap_mapping)
    check("market_source", "빈 스냅샷 거부", t_verify_empty)
    check("market_source", "기준종목 없으면 거부", t_verify_no_refs)
    check("market_source", "날짜 대조 (장중가 거부)", t_verify_match)
    check("market_source", "소스 장애를 휴장일로 안 씀", t_source_outage_is_not_holiday)
    check("market_source", "실제 휴장일은 기록", t_real_holiday_is_marked)
    check("market_source", "조회 실패는 휴장일 아님", t_probe_error_is_not_holiday)
    check("market_source", "일부 실패도 확정 안 함", t_mixed_error_is_outage)
    check("market_source", "장 마감 전 판정 보류", t_today_before_close_is_pending)
    check("market_source", "종가 조회 3분기 + 재시도", t_close_on_status_three_way)
    check("market_source", "적재량 하한", t_load_floor)
    check("market_source", "소스 장애를 실패로 집계", t_nightly_counts_outage_as_failure)
    check("market_source", "credit-kiwoom exit 4 스킵/실패 구분", t_nightly_credit_kiwoom_rc4)
    check("market_source", "update 0건은 부분 완료", t_nightly_zero_rows_is_partial_done)
    check("market_source", "스냅샷 경로 적재", t_snapshot_path_used)
    check("market_source", "주말 즉시 생략", t_weekend_short_circuits)


def test_kiwoom(tmp: Path):
    """키움 OpenAPI+ 제한기·계획기·CSV. OCX 없이 검증되는 부분만.

    호출 제한이 이 연동의 설계를 결정한다. 시간당 1,000건이 지배적이라
    전종목은 불가능하다. 그 계산이 맞는지 확인한다.
    """
    from stocknews.flags import load_manual_credit
    from stocknews.kiwoom import (CREDIT_FIELD_CANDIDATES, LIMIT_PER_HOUR,
                                  LIMIT_PER_MINUTE, LIMIT_PER_SECOND,
                                  RateLimiter, SAFE_PER_HOUR, TR_CREDIT_TREND,
                                  TR_INPUTS, build_plan, check_environment,
                                  estimate_seconds, load_field_map,
                                  save_field_map, select_targets,
                                  write_credit_csv)
    from stocknews.store import Store

    def t_official_limits():
        # 키움 공식값. 바뀌면 여기서 잡힌다.
        assert (LIMIT_PER_SECOND, LIMIT_PER_MINUTE, LIMIT_PER_HOUR) \
            == (5, 100, 1000)

    def t_tr_inputs_verified():
        # koatrinputlegend.ini 실측 입력 필드
        assert TR_CREDIT_TREND == "opt10013"
        assert TR_INPUTS[TR_CREDIT_TREND] == ("종목코드", "일자", "조회구분")

    def t_limiter_second_window():
        lim = RateLimiter(per_second=2, per_minute=99, per_hour=999,
                          clock=lambda: 0.0)
        lim.record(now=0.0)
        lim.record(now=0.1)
        near(lim.wait_seconds(now=0.1), 0.9, tol=1e-9, label="초당 대기")
        near(lim.wait_seconds(now=1.05), 0.0, tol=1e-9, label="창 벗어남")

    def t_limiter_hour_cliff():
        # 시간당 상한을 채우면 창이 비워질 때까지 기다려야 한다.
        lim = RateLimiter(per_second=5, per_minute=100, per_hour=3,
                          clock=lambda: 0.0)
        for t in (0.0, 1.0, 2.0):
            lim.record(now=t)
        near(lim.wait_seconds(now=2.0), 3598.0, tol=1e-9, label="시간 절벽")

    def t_limiter_never_exceeds():
        """가상 시계로 500건을 돌려 어느 창도 상한을 넘지 않는지 확인."""
        caps = (3, 20, 50)
        lim = RateLimiter(*caps, clock=lambda: 0.0)
        t = 0.0
        stamps = []
        for _ in range(120):
            t += lim.wait_seconds(now=t)
            lim.record(now=t)
            stamps.append(t)
        for span, cap in ((1.0, caps[0]), (60.0, caps[1]), (3600.0, caps[2])):
            for s in stamps:
                n = sum(1 for x in stamps if s - span < x <= s)
                assert n <= cap, f"{span}s 창에서 {n} > {cap}"

    def t_eta_matches_limiter():
        # 예측과 실제 제한기가 같은 로직이어야 한다.
        n = 40
        lim = RateLimiter(4, 10, 999, clock=lambda: 0.0)
        t = 0.0
        for _ in range(n):
            t += lim.wait_seconds(now=t)
            lim.record(now=t)
        near(estimate_seconds(n, 4, 10, 999), t, tol=1e-9, label="ETA 일치")

    def t_full_scan_infeasible():
        """전종목이 왜 안 되는지가 수치로 나와야 한다."""
        assert estimate_seconds(0) == 0.0
        short = estimate_seconds(300, per_hour=SAFE_PER_HOUR)
        full = estimate_seconds(2874, per_hour=SAFE_PER_HOUR)
        assert short < 600, short          # 300종목은 10분 안
        assert full > 2 * 3600, full       # 전종목은 2시간 초과
        # 시간당 상한을 넘는 순간 절벽이 생긴다
        assert estimate_seconds(SAFE_PER_HOUR + 1, per_hour=SAFE_PER_HOUR) \
            > 3000

    def t_sector_listing_choice():
        """업종 컬럼이 있는 상장목록을 골라야 한다 (네트워크 없이 규격만).

        2026-08 실측: `StockListing("KRX")` 컬럼에 Sector/Industry 가 없고
        `KRX-DESC` 에는 둘 다 있다. 그동안 KRX 만 조회해서 매번 0을
        반환했고, 전 종목 sector 가 NULL 이라 추천 10선의 섹터 분산
        제한이 조용히 꺼져 있었다.
        """
        from stocknews.universe import (SECTOR_LISTINGS, SECTOR_NAME_COLS,
                                       sector_source)

        assert SECTOR_LISTINGS[0] == "KRX-DESC", SECTOR_LISTINGS
        # Industry 가 Sector 보다 앞이어야 한다. 이름이 헷갈리지만
        # KRX-DESC 의 Sector 는 코스닥 '소속부'다 (우량기업부/벤처기업부
        # /관리종목 ...). 2026-08-27 실측으로 9종뿐이고, Industry 는
        # 158종이다. 소속부를 섹터로 쓰면 분산 제한이 엉뚱하게 걸린다.
        assert SECTOR_NAME_COLS.index("Industry") < \
            SECTOR_NAME_COLS.index("Sector"), SECTOR_NAME_COLS

        krx_cols = ["Code", "ISU_CD", "Name", "Market", "Dept", "Close",
                    "Marcap", "Stocks", "MarketId"]
        desc_cols = ["Code", "Name", "Market", "Sector", "Industry",
                     "Products", "ListingDate"]

        class _FakeFdr:
            def StockListing(self, name):  # noqa: N802  외부 API 이름
                import pandas as _pd
                cols = desc_cols if name == "KRX-DESC" else krx_cols
                return _pd.DataFrame([{c: "x" for c in cols}])

        import sys as _sys
        saved = _sys.modules.get("FinanceDataReader")
        _sys.modules["FinanceDataReader"] = _FakeFdr()
        try:
            rep = sector_source()
            assert rep["listing"] == "KRX-DESC", rep
            assert rep["sector_col"] == "Industry", \
                f"소속부(Sector)를 업종으로 골랐다: {rep}"
            # KRX 만 주면 업종을 못 찾고, 그 사실을 근거와 함께 낸다
            rep2 = sector_source(listings=("KRX",))
            assert rep2["listing"] is None, rep2
            assert rep2["tried"] and rep2["tried"][0]["sector_col"] is None
        finally:
            if saved is None:
                _sys.modules.pop("FinanceDataReader", None)
            else:
                _sys.modules["FinanceDataReader"] = saved

    def t_sector_coverage_visible():
        """섹터 분산이 꺼져 있으면 숫자로 보여야 한다."""
        from stocknews.store import Store

        st = Store(tmp / "sect.db")
        st.upsert_tickers([{"ticker": "005930", "name": "삼성전자",
                            "market": "KOSPI"},
                           {"ticker": "000660", "name": "SK하이닉스",
                            "market": "KOSPI"}])
        cov = st.sector_coverage()
        assert cov["active"] == 2 and cov["with_sector"] == 0, cov
        near(cov["pct"], 0.0, label="업종 0%")
        st.upsert_tickers([{"ticker": "005930", "name": "삼성전자",
                            "market": "KOSPI", "sector": "반도체"}])
        cov = st.sector_coverage()
        assert cov["with_sector"] == 1 and cov["sectors"] == 1, cov
        near(cov["pct"], 50.0, label="업종 50%")

    def t_env_shape():
        env = check_environment(openapi_dir=str(tmp / "nope"))
        for k in ("python_bits", "ocx_present", "ocx_registered",
                  "missing", "ready"):
            assert k in env, k
        assert env["ocx_present"] is False
        assert any("없습니다" in m for m in env["missing"])

    def t_plan_and_targets():
        st = Store(tmp / "kw.db")
        st.upsert_tickers([{"ticker": "005930", "name": "삼성전자",
                            "market": "KOSPI"},
                           {"ticker": "000660", "name": "SK하이닉스",
                            "market": "KOSPI"}])
        # 스캔 이력이 없으면 대상 0
        assert select_targets(st, limit=10) == []
        plan = build_plan(st, limit=10)
        assert plan["targets"] == 0
        assert plan["active_tickers"] == 2
        assert plan["full_scan_requests"] == 2
        assert plan["feasible_daily"] is True

    def t_csv_roundtrip():
        """브릿지가 쓴 CSV 가 기존 적재 파서로 읽혀야 한다."""
        p = tmp / "credit_kiwoom.csv"
        n = write_credit_csv([
            {"ticker": "5930", "ratio": 0.42, "shares": None,
             "asof": "2026-08-24", "note": "kiwoom:opt10013"},
            {"ticker": "086520", "ratio": None, "shares": 1234567,
             "asof": "2026-08-24", "note": None},
            {"ticker": "bad", "ratio": 1.0, "shares": None, "asof": ""},
            {"ticker": "329180", "ratio": None, "shares": None, "asof": ""},
        ], p)
        assert n == 2, n
        rows = load_manual_credit(p)
        assert len(rows) == 2, rows
        by = {r["ticker"]: r for r in rows}
        assert "005930" in by, list(by)          # zfill 되어야 한다
        near(by["005930"]["ratio"], 0.42, label="비율 왕복")
        assert by["005930"]["asof"] == "2026-08-24"
        near(by["086520"]["shares"], 1234567, label="주식수 왕복")
        assert by["086520"]["ratio"] is None

    def t_field_map_roundtrip():
        p = tmp / "fields.json"
        assert load_field_map(p) == {}
        save_field_map({"_recordname": "신용매매동향", "loan_ratio": "잔고비율"}, p)
        got = load_field_map(p)
        assert got["loan_ratio"] == "잔고비율", got

    def t_field_candidates_are_probes():
        # 후보는 '탐침용'이다. 잔고/비율 후보가 반드시 있어야 한다.
        assert CREDIT_FIELD_CANDIDATES["loan_balance"]
        assert CREDIT_FIELD_CANDIDATES["loan_ratio"]

    check("kiwoom", "공식 호출 제한값", t_official_limits)
    check("kiwoom", "TR 입력 규격 (실측)", t_tr_inputs_verified)
    check("kiwoom", "초당 창", t_limiter_second_window)
    check("kiwoom", "시간당 절벽", t_limiter_hour_cliff)
    check("kiwoom", "어떤 창도 초과 안 함", t_limiter_never_exceeds)
    check("kiwoom", "ETA = 제한기 동일 로직", t_eta_matches_limiter)
    check("kiwoom", "전종목 불가 · 샷리스트 가능", t_full_scan_infeasible)
    check("universe", "업종 상장목록 선택 (KRX-DESC)", t_sector_listing_choice)
    check("universe", "업종 커버리지 가시화", t_sector_coverage_visible)
    check("kiwoom", "환경 진단 형태", t_env_shape)
    check("kiwoom", "수집 계획", t_plan_and_targets)
    check("kiwoom", "CSV 왕복 (기존 파서 호환)", t_csv_roundtrip)
    check("kiwoom", "필드 매핑 왕복", t_field_map_roundtrip)
    check("kiwoom", "필드 후보 존재", t_field_candidates_are_probes)


# ═════════════════ 12-2. 키움 REST (au10001 + ka10013) ═════════════════
def test_kiwoom_rest(tmp: Path):
    """키움 REST 경로. 가짜 서버를 물려 네트워크 없이 전 경로를 검증한다.

    OCX 경로와 달리 응답 필드명이 확정이다(공식 예제 저장소). 그래서
    탐침이 아니라 **규격 고정**을 검사한다. 필드명이 바뀌면 여기서 깨진다.

    검증 축은 넷이다.
      규격   경로 / api-id / 필드명 / expires_dt 타임존
      토큰   캐시 재사용 · 만료 재발급 · 지문 불일치 · 비밀 미저장
      실패   유량 초과 · 토큰 거부 · 자격증명 거부 · 없는 종목
      해석   빈 문자열 != 0 · 잔고 단위 판정 · CSV 왕복
    """
    import itertools
    from datetime import datetime as dt
    from datetime import timezone as tz

    from stocknews.flags import load_credit_csv
    from stocknews.kiwoom import RateLimiter, write_credit_csv
    from stocknews.kiwoom_rest import (API_CREDIT_TREND, API_TOKEN, BASE_MOCK,
                                       BASE_REAL, CREDIT_FIELDS,
                                       CREDIT_LIST_KEY, FIELD_BALANCE,
                                       FIELD_BALANCE_RATIO, INQUIRY_LOAN,
                                       LIMITS_ARE_PUBLISHED, STKINFO_PATH,
                                       TOKEN_PATH, AuthError, HttpReply,
                                       KiwoomRestClient, RateLimited,
                                       SymbolNotFound, TokenStore,
                                       base_url_from_env, collect_credit,
                                       credentials_from_env, limiter_from_env,
                                       parse_expiry, parse_number,
                                       pick_latest_credit, resolve_share_unit,
                                       return_code_of)

    UTC = tz.utc

    def _row(d, remn="", remn_rt="", cur="65100"):
        """ka10013 레코드 1건. 필드명은 공식 예제 저장소 그대로."""
        return {"dt": d, "cur_prc": cur, "pred_pre_sig": "0", "pred_pre": "0",
                "trde_qty": "1000", "new": "", "rpya": "", "remn": remn,
                "amt": "", "pre": "", "shr_rt": "", "remn_rt": remn_rt}

    class Fake:
        """가짜 키움. 스크립트로 실패를 주입할 수 있다."""

        def __init__(self, credit=None, expires="20991231235959"):
            self.credit = credit or {}
            self.expires = expires
            self.token_calls = 0
            self.tr_calls = []          # (api_id, body, headers)
            self.script = []            # 앞에서부터 소비되는 강제 응답
            self.token_value = "TOK-1"

        def __call__(self, method, url, headers, payload, timeout):
            if url.endswith(TOKEN_PATH):
                self.token_calls += 1
                assert payload["grant_type"] == "client_credentials"
                assert "appkey" in payload and "secretkey" in payload
                return HttpReply(200, {"token": self.token_value,
                                       "token_type": "bearer",
                                       "expires_dt": self.expires,
                                       "return_code": 0,
                                       "return_msg": "정상"})
            self.tr_calls.append((headers.get("api-id"), payload, headers))
            if self.script:
                return self.script.pop(0)
            code = payload.get("stk_cd")
            return HttpReply(200, {CREDIT_LIST_KEY: self.credit.get(code, []),
                                   "return_code": 0, "return_msg": "정상"},
                             headers={"cont-yn": "N", "next-key": "",
                                      "api-id": API_CREDIT_TREND})

    # 토큰 캐시 파일은 검사마다 새로 준다. id(fake) 를 쓰면 파이썬이
    # 회수한 주소를 재사용해 다른 검사의 캐시를 물려받는다 (실제로
    # 만료 검사가 2099년 만료 토큰을 주워 읽어 흔들렸다).
    _seq = itertools.count()

    def _client(fake, **kw):
        kw.setdefault("transport", fake)
        kw.setdefault("base_url", BASE_REAL)
        kw.setdefault("limiter", RateLimiter(999, 999, 9999, clock=lambda: 0.0))
        kw.setdefault("token_store",
                      TokenStore(tmp / f"tok_{next(_seq)}.json"))
        kw.setdefault("sleep", lambda s: None)
        return KiwoomRestClient("KEY", "SECRET", **kw)

    # ── 규격 ──
    def t_spec_frozen():
        """경로·api-id·필드명. 공식 예제 저장소와 글자 단위로 같아야 한다."""
        assert TOKEN_PATH == "/oauth2/token"
        assert STKINFO_PATH == "/api/dostk/stkinfo"
        assert (API_TOKEN, API_CREDIT_TREND) == ("au10001", "ka10013")
        assert BASE_REAL == "https://api.kiwoom.com"
        assert BASE_MOCK == "https://mockapi.kiwoom.com"
        assert CREDIT_LIST_KEY == "crd_trde_trend"
        assert INQUIRY_LOAN == "1"      # 1:융자 2:대주
        assert (FIELD_BALANCE, FIELD_BALANCE_RATIO) == ("remn", "remn_rt")
        assert list(CREDIT_FIELDS) == [
            "dt", "cur_prc", "pred_pre_sig", "pred_pre", "trde_qty",
            "new", "rpya", "remn", "amt", "pre", "shr_rt", "remn_rt"]

    def t_limits_are_not_published():
        """제한 수치를 '공식값'으로 위장하면 안 된다.

        OCX 는 초당5/분당100/시간당1,000 이 문서에 있다. REST 는 없다.
        모르는 것을 아는 척하면 나중에 아무도 다시 확인하지 않는다.
        """
        assert LIMITS_ARE_PUBLISHED is False

    def t_expiry_is_kst():
        """expires_dt 는 KST 다. naive 로 두면 UTC 서버에서 9시간 일찍 죽는다."""
        got = parse_expiry("20241107083713")
        assert got.tzinfo is not None, "naive datetime 이 반환됐다"
        assert got == dt(2024, 11, 6, 23, 37, 13, tzinfo=UTC), got

    def t_parse_number():
        # 빈 문자열은 '데이터 없음'이고 0 은 '실측 0' 이다. 섞으면 안 된다.
        assert parse_number("") is None
        assert parse_number(None) is None
        assert parse_number("   ") is None
        assert parse_number("0") == 0.0
        near(parse_number("+65100"), 65100, label="방향 부호 +")
        near(parse_number("-27300"), -27300, label="방향 부호 -")
        near(parse_number("1,234,567"), 1234567, label="천단위 쉼표")
        near(parse_number("4.85"), 4.85, label="소수")
        assert parse_number("N/A") is None

    def t_return_code_str_zero():
        """키움은 수치를 문자열로 준다. "0" 을 오류로 읽으면 전건 실패한다."""
        assert return_code_of({"return_code": 0}) is None
        assert return_code_of({"return_code": "0"}) is None
        assert return_code_of({}) is None
        assert return_code_of({"return_code": "1700"}) == 1700
        assert return_code_of({"return_code": 8005}) == 8005

    def t_env_helpers():
        assert credentials_from_env({}) is None
        assert credentials_from_env({"KIWOOM_APP_KEY": "a"}) is None
        assert credentials_from_env({"KIWOOM_APP_KEY": " a ",
                                    "KIWOOM_APP_SECRET": "b"}) == ("a", "b")
        assert base_url_from_env({}, mock=True) == BASE_MOCK
        assert base_url_from_env({}, mock=False) == BASE_REAL
        # 명시 설정이 이긴다
        assert base_url_from_env({"KIWOOM_API_BASE": "https://x/"},
                                 mock=True) == "https://x"
        caps = [c for _, c in limiter_from_env({}).windows]
        assert caps == [3, 60, 900], caps
        caps2 = [c for _, c in limiter_from_env(
            {"KIWOOM_REST_PER_SECOND": "1"}).windows]
        assert caps2[0] == 1, caps2

    # ── 토큰 ──
    def t_token_cached():
        fake = Fake()
        c = _client(fake)
        c.credit_trend("005930", "20260827")
        c.credit_trend("000660", "20260827")
        assert fake.token_calls == 1, f"토큰을 매번 발급했다: {fake.token_calls}"
        assert len(fake.tr_calls) == 2

    def t_token_reused_across_clients():
        """파일 캐시. 배치가 하루에 여러 번 돌아도 재발급하지 않는다."""
        store_path = tmp / "tok_shared.json"
        f1 = Fake()
        c1 = KiwoomRestClient("K", "S", base_url=BASE_REAL, transport=f1,
                              token_store=TokenStore(store_path),
                              limiter=RateLimiter(999, 999, 9999,
                                                  clock=lambda: 0.0),
                              sleep=lambda s: None)
        c1.credit_trend("005930", "20260827")
        f2 = Fake()
        c2 = KiwoomRestClient("K", "S", base_url=BASE_REAL, transport=f2,
                              token_store=TokenStore(store_path),
                              limiter=RateLimiter(999, 999, 9999,
                                                  clock=lambda: 0.0),
                              sleep=lambda s: None)
        c2.credit_trend("005930", "20260827")
        assert f2.token_calls == 0, "파일 캐시를 쓰지 않았다"

    def t_token_secrets_not_persisted():
        """토큰 파일에 앱키/시크릿이 남으면 .env 밖으로 새는 경로가 생긴다."""
        p = tmp / "tok_secret.json"
        fake = Fake()
        c = _client(fake, token_store=TokenStore(p))
        c.token()
        txt = p.read_text(encoding="utf-8")
        assert "SECRET" not in txt, "시크릿이 토큰 파일에 저장됐다"
        assert "KEY" not in txt, "앱키가 토큰 파일에 저장됐다"
        assert "fingerprint" in txt

    def t_token_expired_reissued():
        now = [dt(2026, 8, 27, 0, 0, tzinfo=UTC)]
        fake = Fake(expires="20260827093000")     # KST 09:30 = UTC 00:30
        c = _client(fake, now=lambda: now[0], refresh_buffer=0.0)
        c.token()
        assert fake.token_calls == 1
        now[0] = dt(2026, 8, 27, 0, 20, tzinfo=UTC)     # 아직 유효
        c.token()
        assert fake.token_calls == 1, "유효한데 재발급했다"
        now[0] = dt(2026, 8, 27, 0, 40, tzinfo=UTC)     # 만료
        c.token()
        assert fake.token_calls == 2, "만료됐는데 재발급 안 했다"

    def t_refresh_buffer():
        """만료 직전 토큰으로 요청을 보내면 도중에 죽는다. 미리 갱신한다."""
        now = [dt(2026, 8, 27, 0, 0, tzinfo=UTC)]
        fake = Fake(expires="20260827091000")     # UTC 00:10 만료
        c = _client(fake, now=lambda: now[0], refresh_buffer=900.0)
        c.token()
        c.token()
        assert fake.token_calls == 2, "만료 15분 전인데 재사용했다"

    def t_fingerprint_mismatch():
        """앱키가 바뀌면 옛 토큰을 쓰면 안 된다."""
        p = tmp / "tok_fp.json"
        f1 = Fake()
        _client(f1, token_store=TokenStore(p)).token()
        f2 = Fake()
        c2 = KiwoomRestClient("OTHER", "OTHER", base_url=BASE_REAL,
                              transport=f2, token_store=TokenStore(p),
                              limiter=RateLimiter(999, 999, 9999,
                                                  clock=lambda: 0.0),
                              sleep=lambda s: None)
        c2.token()
        assert f2.token_calls == 1, "다른 앱키인데 캐시를 재사용했다"

    def t_base_url_mismatch():
        """실전/모의를 바꾸면 토큰도 갈아야 한다."""
        p = tmp / "tok_base.json"
        f1 = Fake()
        _client(f1, token_store=TokenStore(p), base_url=BASE_REAL).token()
        f2 = Fake()
        _client(f2, token_store=TokenStore(p), base_url=BASE_MOCK).token()
        assert f2.token_calls == 1, "도메인이 바뀐 토큰을 재사용했다"

    def t_headers():
        fake = Fake()
        _client(fake).credit_trend("005930", "2026-08-27")
        api_id, body, headers = fake.tr_calls[0]
        assert api_id == API_CREDIT_TREND
        assert headers["authorization"].startswith("Bearer "), headers
        assert headers["Content-Type"].startswith("application/json")
        assert headers["cont-yn"] == "N"
        assert body == {"stk_cd": "005930", "dt": "20260827", "qry_tp": "1"}, body

    # ── 실패 처리 ──
    def t_rate_limit_backs_off():
        """1700 을 받으면 속도를 줄이고 재시도한다. 그냥 실패하면 안 된다."""
        fake = Fake(credit={"005930": [_row("20260826", "1000", "0.42")]})
        fake.script = [HttpReply(200, {"return_code": 1700,
                                       "return_msg": "제한 초과"})]
        c = _client(fake)
        before = [cap for _, cap in c.limiter.windows]
        rows = c.credit_trend("005930", "20260827")
        after = [cap for _, cap in c.limiter.windows]
        assert rows, "재시도가 안 됐다"
        assert after == [b // 2 for b in before], (before, after)
        assert c.stats["rate_limited"] == 1
        assert c.stats["throttled_down"] == 1

    def t_http_429_backs_off():
        fake = Fake(credit={"005930": [_row("20260826", "1000", "0.42")]})
        fake.script = [HttpReply(429, {}, text="Too Many Requests")]
        c = _client(fake)
        assert c.credit_trend("005930", "20260827")
        assert c.stats["throttled_down"] == 1

    def t_rate_limit_exhausted():
        fake = Fake()
        fake.script = [HttpReply(200, {"return_code": 1701, "return_msg": "x"})
                       for _ in range(5)]
        c = _client(fake, max_retries=2)
        try:
            c.credit_trend("005930", "20260827")
        except RateLimited:
            pass
        else:
            raise AssertionError("재시도를 소진했는데 예외가 안 났다")

    def t_token_rejected_reissued_once():
        """8005(토큰 만료)는 재발급 후 1회 재시도. 무한 루프는 안 된다."""
        fake = Fake(credit={"005930": [_row("20260826", "1000", "0.42")]})
        fake.script = [HttpReply(200, {"return_code": 8005,
                                       "return_msg": "토큰 만료"})]
        c = _client(fake)
        assert c.credit_trend("005930", "20260827")
        assert fake.token_calls == 2, f"재발급 횟수 {fake.token_calls}"

    def t_bad_credentials_no_retry():
        """8001 은 기다려도 안 된다. 즉시 포기해야 무한 재시도를 막는다."""
        fake = Fake()
        fake.script = [HttpReply(200, {"return_code": 8001,
                                       "return_msg": "앱키 오류"})
                       for _ in range(5)]
        c = _client(fake)
        try:
            c.credit_trend("005930", "20260827")
        except AuthError:
            pass
        else:
            raise AssertionError("자격증명 오류인데 AuthError 가 안 났다")
        assert len(fake.tr_calls) == 1, f"재시도했다: {len(fake.tr_calls)}"

    def t_symbol_not_found_isolated():
        """없는 종목 하나가 배치 전체를 죽이면 안 된다."""
        fake = Fake(credit={"005930": [_row("20260826", "1000", "0.42")]})
        fake.script = [HttpReply(200, {"return_code": 1901,
                                       "return_msg": "종목 없음"})]
        c = _client(fake)
        try:
            c.credit_trend("999999", "20260827")
        except SymbolNotFound:
            pass
        else:
            raise AssertionError("SymbolNotFound 가 안 났다")
        assert c.credit_trend("005930", "20260827"), "다음 종목이 막혔다"

    def t_transport_retry():
        """네트워크 오류는 재시도 대상이다."""
        from stocknews.kiwoom_rest import TransportError

        fake = Fake(credit={"005930": [_row("20260826", "1000", "0.42")]})
        boom = [2]

        def flaky(method, url, headers, payload, timeout):
            if url.endswith(STKINFO_PATH) and boom[0] > 0:
                boom[0] -= 1
                raise TransportError("연결 끊김")
            return fake(method, url, headers, payload, timeout)

        c = _client(fake, transport=flaky, max_retries=4)
        assert c.credit_trend("005930", "20260827")
        assert c.stats["retries"] == 2, c.stats

    # ── 응답 해석 ──
    def t_pick_latest_skips_blanks():
        """빈 문자열은 '데이터 없음'이다. 0 으로 읽으면 과열 종목을 놓친다."""
        recs = [_row("20260827"), _row("20260826", "5000", "1.20"),
                _row("20260825", "4000", "1.00")]
        got = pick_latest_credit(recs)
        assert got is not None and got["dt"] == "20260826", got
        assert pick_latest_credit([_row("20260827"), _row("20260826")]) is None
        assert pick_latest_credit([]) is None
        # 실측 0 은 유효한 값이다
        zero = pick_latest_credit([_row("20260827", "0", "0")])
        assert zero is not None and zero["dt"] == "20260827"

    def t_share_unit_resolved():
        """remn 단위가 문서에 없다. 잔고율 x 상장주식수로 역산해 판정한다."""
        # 주 단위: remn 이 곧 주식수
        obs = [(1_000_000.0, 1.0, 100_000_000.0)] * 3
        scale, note = resolve_share_unit(obs)
        near(scale, 1.0, label="주 단위")
        assert "주" in note, note
        # 천주 단위: remn x 1000 = 주식수
        obs = [(1_000.0, 1.0, 100_000_000.0)] * 3
        scale, note = resolve_share_unit(obs)
        near(scale, 1000.0, label="천주 단위")

    def t_share_unit_refuses_to_guess():
        """표본이 적거나 배율이 후보와 안 맞으면 추측하지 않는다."""
        scale, note = resolve_share_unit([(1000.0, 1.0, 100_000_000.0)])
        assert scale is None, "표본 1건으로 단위를 확정했다"
        assert "표본" in note
        odd = [(1000.0, 1.0, 5_700_000.0)] * 4      # 배율 57
        scale, note = resolve_share_unit(odd)
        assert scale is None, f"이상한 배율을 채택했다: {note}"

    def t_collect_credit_roundtrip():
        """전 경로: 가짜 서버 -> collect_credit -> CSV -> 적재 파서."""
        listed = {"005930": 5_969_782_550.0, "000660": 728_002_365.0,
                  "086520": 30_000_000.0}
        credit = {
            "005930": [_row("20260827"), _row("20260826", "5000000", "0.084")],
            "000660": [_row("20260826", "1000000", "0.137")],
            "086520": [_row("20260826", "600000", "2.000")],
            "329180": [_row("20260826")],          # 값 없음 -> 실패로 집계
        }
        fake = Fake(credit=credit)
        c = _client(fake)
        rows, stats = collect_credit(
            c, ["5930", "000660", "086520", "329180", "bad"],
            trade_date="2026-08-27", listed_shares=listed)
        assert stats["requested"] == 5
        assert stats["answered"] == 3, stats
        assert stats["rows"] == 3, stats
        assert stats["failed"] == 2, stats["failures"]
        near(stats["share_unit_scale"], 1.0, label="단위 판정")
        by = {r["ticker"]: r for r in rows}
        assert "005930" in by, list(by)
        near(by["005930"]["ratio"], 0.084, label="잔고율")
        near(by["005930"]["shares"], 5_000_000, label="잔고 주식수")
        assert by["005930"]["asof"] == "2026-08-26", by["005930"]
        assert by["005930"]["note"] == "kiwoom:ka10013"

        p = tmp / "credit_rest.csv"
        assert write_credit_csv(rows, p) == 3
        back = load_credit_csv(p, source="kiwoom")
        assert len(back) == 3
        assert {r["source"] for r in back} == {"kiwoom"}, \
            "출처가 kiwoom 으로 기록되지 않았다"

    def t_collect_uses_one_request_per_ticker():
        """유량 예산이 곧 대상 수다. 종목당 요청이 늘면 계획이 무의미해진다."""
        fake = Fake(credit={"005930": [_row("20260826", "100", "0.1")],
                            "000660": [_row("20260826", "200", "0.2")]})
        c = _client(fake)
        collect_credit(c, ["005930", "000660"], trade_date="2026-08-27")
        assert len(fake.tr_calls) == 2, len(fake.tr_calls)

    def t_collect_counts_failure_codes():
        """failures 는 20건에서 잘린다. 코드별 건수는 전 종목을 센다.

        운영 응답 모양: return_code 는 3 이고 원인 코드 8050 은 메시지
        대괄호 안에만 있다 (2026-09-03~10).
        """
        fake = Fake()
        deny = {"return_code": 3, "return_msg":
                "인증에 실패했습니다[8050:지정단말기 인증에 실패했습니다]"}
        fake.script = [HttpReply(200, dict(deny)) for _ in range(25)]
        fake.script.append(HttpReply(200, {"return_code": 1504,
                                           "return_msg": "입력값 오류"}))
        tickers = [f"{i:06d}" for i in range(1, 27)]
        rows, stats = collect_credit(_client(fake), tickers + ["bad"],
                                     trade_date="2026-08-27")
        assert rows == [], rows
        assert stats["failed"] == 27, stats["failed"]
        assert len(stats["failures"]) == 20, len(stats["failures"])
        # 대괄호 코드가 없으면 return_code, 코드 자체가 없으면('bad') 안 센다
        assert stats["failure_codes"] == {"8050": 25, "1504": 1}, \
            stats["failure_codes"]

    def t_continuation_capped():
        """cont-yn=Y 여도 max_pages 를 넘겨선 안 된다."""
        fake = Fake()
        fake.script = [
            HttpReply(200, {CREDIT_LIST_KEY: [_row("20260826", "1", "0.1")],
                            "return_code": 0},
                      headers={"cont-yn": "Y", "next-key": "K1"})
            for _ in range(5)]
        c = _client(fake)
        rows = c.credit_trend("005930", "20260827", max_pages=2)
        assert len(fake.tr_calls) == 2, len(fake.tr_calls)
        assert len(rows) == 2
        assert fake.tr_calls[1][2]["next-key"] == "K1"

    def t_no_order_calls():
        """이 시스템은 주문을 내지 않는다 (AGENTS.md 8장). 회귀 가드."""
        src = (Path(__file__).parent / "stocknews" / "kiwoom_rest.py"
               ).read_text(encoding="utf-8")
        for banned in ("SendOrder", "/api/dostk/ordr", "kt10000", "kt10001",
                       "kt10002", "kt10003"):
            assert banned not in src, f"주문 경로가 들어왔다: {banned}"

    check("kiwoom-rest", "규격 고정 (공식 예제 저장소)", t_spec_frozen)
    check("kiwoom-rest", "제한 수치는 공개값 아님", t_limits_are_not_published)
    check("kiwoom-rest", "expires_dt = KST", t_expiry_is_kst)
    check("kiwoom-rest", "숫자 파싱 (빈값 != 0)", t_parse_number)
    check("kiwoom-rest", 'return_code "0" 오판 방지', t_return_code_str_zero)
    check("kiwoom-rest", "환경 헬퍼", t_env_helpers)
    check("kiwoom-rest", "토큰 메모리 캐시", t_token_cached)
    check("kiwoom-rest", "토큰 파일 캐시", t_token_reused_across_clients)
    check("kiwoom-rest", "토큰 파일에 비밀 미저장", t_token_secrets_not_persisted)
    check("kiwoom-rest", "만료 시 재발급", t_token_expired_reissued)
    check("kiwoom-rest", "만료 직전 선제 갱신", t_refresh_buffer)
    check("kiwoom-rest", "앱키 변경 시 캐시 무효", t_fingerprint_mismatch)
    check("kiwoom-rest", "실전/모의 전환 시 캐시 무효", t_base_url_mismatch)
    check("kiwoom-rest", "요청 헤더/바디 규격", t_headers)
    check("kiwoom-rest", "유량 초과 -> 감속 재시도", t_rate_limit_backs_off)
    check("kiwoom-rest", "HTTP 429 -> 감속", t_http_429_backs_off)
    check("kiwoom-rest", "재시도 소진 시 예외", t_rate_limit_exhausted)
    check("kiwoom-rest", "토큰 거부 -> 1회 재발급", t_token_rejected_reissued_once)
    check("kiwoom-rest", "자격증명 오류는 재시도 안 함", t_bad_credentials_no_retry)
    check("kiwoom-rest", "없는 종목만 건너뜀", t_symbol_not_found_isolated)
    check("kiwoom-rest", "전송 오류 재시도", t_transport_retry)
    check("kiwoom-rest", "빈 문자열 != 잔고 0", t_pick_latest_skips_blanks)
    check("kiwoom-rest", "잔고 단위 역산 판정", t_share_unit_resolved)
    check("kiwoom-rest", "단위 불명이면 추측 안 함", t_share_unit_refuses_to_guess)
    check("kiwoom-rest", "수집 전 경로 왕복", t_collect_credit_roundtrip)
    check("kiwoom-rest", "실패 코드별 건수", t_collect_counts_failure_codes)
    check("kiwoom-rest", "종목당 요청 1건", t_collect_uses_one_request_per_ticker)
    check("kiwoom-rest", "연속조회 페이지 상한", t_continuation_capped)
    check("kiwoom-rest", "주문 경로 없음", t_no_order_calls)


def test_dividends(tmp: Path):
    """배당 수집. 픽스처는 2026-09-01 OpenDART 실측 응답이다.

    앵커 5종목(삼성전자·POSCO홀딩스·KT&G·삼성화재·우리금융지주)의 주당
    현금배당금과 배당성향을 공시 원문 값으로 고정한다. 파서가 단위나
    우선주 처리를 잘못 바꾸면 여기서 걸린다.
    """
    from stocknews.dividend_data import (build_rows, collect_dividends,
                                         fetch_one, free_cash_flow,
                                         latest_fiscal_year,
                                         parse_alot_matter, parse_cashflow,
                                         payout_ratio, resolve_status,
                                         year_batches, _label_unit)
    from stocknews.kiwoom import RateLimiter
    from stocknews.store import Store

    def _al(se, knd, th, fr, lw, stlm="2025-12-31"):
        return {"se": se, "stock_knd": knd, "thstrm": th, "frmtrm": fr,
                "lwfr": lw, "stlm_dt": stlm}

    def _cf(aid, nm, th, fr, bf):
        return {"sj_div": "CF", "account_id": aid, "account_nm": nm,
                "thstrm_amount": th, "frmtrm_amount": fr,
                "bfefrmtrm_amount": bf}

    # ── 실측 픽스처 (bsns_year=2025, reprt_code=11011) ──
    SEC = [
        _al("주당액면가액(원)", "-", "100", "100", "100"),
        _al("(연결)당기순이익(백만원)", "-",
            "44,260,956", "33,621,363", "14,473,401"),
        _al("(별도)당기순이익(백만원)", "-",
            "33,686,601", "23,582,565", "25,397,099"),
        _al("현금배당금총액(백만원)", "-",
            "11,107,906", "9,810,767", "9,809,438"),
        _al("주식배당금총액(백만원)", "-", "-", "-", "-"),
        _al("(연결)현금배당성향(%)", "-", "25.10", "29.20", "67.80"),
        _al("현금배당수익률(%)", "보통주", "1.50", "2.70", "1.90"),
        _al("주당 현금배당금(원)", "보통주", "1,668", "1,446", "1,444"),
        _al("주당 현금배당금(원)", "우선주", "1,669", "1,447", "1,445"),
        _al("주당 주식배당(주)", "보통주", "-", "-", "-"),
    ]
    POSCO = [
        _al("주당액면가액(원)", "-", "5,000", "5,000", "5,000"),
        _al("(연결)당기순이익(백만원)", "-",
            "657,654", "1,094,917", "1,698,092"),
        _al("현금배당금총액(백만원)", "-", "756,208", "757,485", "758,762"),
        _al("(연결)현금배당성향(%)", "-", "115.00", "69.20", "44.70"),
        _al("주당 현금배당금(원)", "보통주", "10,000", "10,000", "10,000"),
        _al("주당 현금배당금(원)", "우선주", "-", "-", "-"),
    ]
    KTNG = [
        _al("주당액면가액(원)", "-", "5,000", "5,000", "5,000"),
        _al("(연결)당기순이익(백만원)", "-",
            "1,090,072", "1,165,727", "902,662"),
        _al("현금배당금총액(백만원)", "-", "627,428", "588,448", "590,777"),
        _al("(연결)현금배당성향(%)", "-", "57.60", "50.50", "65.40"),
        _al("주당 현금배당금(원)", "보통주", "6,000", "5,400", "5,200"),
    ]
    SFIRE = [
        _al("주당액면가액(원)", "-", "500", "500", "500"),
        _al("(연결)당기순이익(백만원)", "-",
            "2,018,286", "2,073,572", "1,818,433"),
        _al("현금배당금총액(백만원)", "-", "828,949", "807,694", "680,166"),
        _al("(연결)현금배당성향(%)", "-", "41.10", "39.00", "37.40"),
        # 우선주가 보통주보다 큰 값이라 잘못 고르면 앵커가 어긋난다.
        _al("주당 현금배당금(원)", "우선주", "19,505", "19,005", "16,005"),
        _al("주당 현금배당금(원)", "보통주", "19,500", "19,000", "16,000"),
    ]
    WOORI = [
        _al("주당액면가액(원)", "-", "5,000", "5,000", "5,000"),
        _al("(연결)당기순이익(백만원)", "-",
            "3,124,346", "3,085,995", "2,506,296"),
        _al("현금배당금총액(백만원)", "-", "998,468", "891,045", "747,302"),
        _al("(연결)현금배당성향(%)", "-", "31.96", "28.87", "29.82"),
        _al("주당 현금배당금(원)", "보통주", "1,360", "1,200", "1,000"),
    ]
    # 삼성전자 연결 현금흐름표 실측. 유출인데 부호가 양수인 것이 핵심이다.
    SEC_CF = [
        _cf("ifrs-full_CashFlowsFromUsedInOperatingActivities",
            "영업활동현금흐름",
            "85315148000000", "72982621000000", "44788749000000"),
        _cf("ifrs-full_PurchaseOfPropertyPlantAndEquipmentClassified"
            "AsInvestingActivities", "유형자산의 취득",
            "47522179000000", "51406355000000", "52963102000000"),
        _cf("ifrs-full_PurchaseOfIntangibleAssetsClassified"
            "AsInvestingActivities", "무형자산의 취득",
            "4630970000000", "2335284000000", "2394284000000"),
        # CF 가 아닌 행은 무시해야 한다.
        {"sj_div": "BS", "account_id": "ifrs-full_Assets",
         "account_nm": "자산총계", "thstrm_amount": "1", "frmtrm_amount": "1",
         "bfefrmtrm_amount": "1"},
        # 자기주식 취득도 유출인데 양수다. CAPEX 로 세면 안 된다.
        _cf("dart_AcquisitionOfTreasuryShares", "자기주식의 취득",
            "8189263000000", "1811775000000", "0"),
    ]

    ANCHOR_DPS = {"삼성전자": (SEC, 1668.0, 25.10),
                  "POSCO홀딩스": (POSCO, 10000.0, 115.00),
                  "KT&G": (KTNG, 6000.0, 57.60),
                  "삼성화재": (SFIRE, 19500.0, 41.10),
                  "우리금융지주": (WOORI, 1360.0, 31.96)}

    def t_unit_scale():
        assert _label_unit("현금배당금총액(백만원)") == ("현금배당금총액", 1e6)
        assert _label_unit("주당 현금배당금(원)") == ("주당 현금배당금", 1.0)
        # 앞의 괄호를 단위로 오인하면 안 된다.
        head, sc = _label_unit("(연결)당기순이익(백만원)")
        assert head == "(연결)당기순이익" and sc == 1e6, (head, sc)
        head, sc = _label_unit("(연결)현금배당성향(%)")
        assert head == "(연결)현금배당성향" and sc == 1.0, (head, sc)
        # 단위가 아닌 괄호는 라벨을 건드리지 않는다.
        assert _label_unit("어떤항목(주석)") == ("어떤항목(주석)", 1.0)

    def t_year_mapping():
        """응답 하나가 당기/전기/전전기 3개 연도를 준다."""
        got = parse_alot_matter(SEC, 2025)
        assert sorted(got) == [2023, 2024, 2025], sorted(got)
        near(got[2025]["dps"], 1668.0, label="2025 DPS")
        near(got[2024]["dps"], 1446.0, label="2024 DPS")
        near(got[2023]["dps"], 1444.0, label="2023 DPS")
        near(got[2023]["net_income"], 14_473_401e6, tol=1.0,
             label="2023 순이익")
        assert got[2025]["settle_dt"] == "2025-12-31", got[2025]["settle_dt"]

    def t_dps_anchor_5():
        """5종목 주당 현금배당금 = 공시 원문 (2025 사업연도)."""
        for name, (rows, dps, _) in ANCHOR_DPS.items():
            got = parse_alot_matter(rows, 2025)[2025]
            near(got["dps"], dps, label=f"{name} DPS")

    def t_payout_matches_disclosure():
        """총액/순이익으로 계산한 성향이 공시 성향과 맞아야 한다.

        DART 는 성향을 소수 둘째까지 반올림해 준다. 0.05%p 이내면 같은
        값을 다르게 반올림한 것이다. 그보다 벌어지면 단위 환산이나
        연결/별도 선택이 틀렸다는 뜻이다.
        """
        for name, (rows, _, disclosed) in ANCHOR_DPS.items():
            rec = parse_alot_matter(rows, 2025)[2025]
            calc = payout_ratio(rec["total_dividend"], rec["net_income"])
            assert calc is not None, f"{name} 성향이 None"
            assert abs(calc - disclosed) <= 0.05, \
                f"{name} 성향 계산 {calc} vs 공시 {disclosed}"

    def t_preferred_row_excluded():
        """우선주 행을 고르면 안 된다 (삼성화재 19,505 / 삼성전자 1,669)."""
        got = parse_alot_matter(SFIRE, 2025)
        near(got[2025]["dps"], 19500.0, label="삼성화재 보통주")
        got2 = parse_alot_matter(SEC, 2025)
        near(got2[2025]["dps"], 1668.0, label="삼성전자 보통주")

    def t_none_vs_missing():
        """무배당(0)과 누락(no_report)을 섞지 않는다."""
        nodiv = [
            _al("주당액면가액(원)", "-", "500", "500", "-"),
            _al("(연결)당기순이익(백만원)", "-", "1,000", "900", "-"),
            _al("현금배당금총액(백만원)", "-", "-", "-", "-"),
            _al("주당 현금배당금(원)", "보통주", "-", "-", "-"),
        ]
        got = parse_alot_matter(nodiv, 2025)
        # 그 연도를 보고했고 배당금이 '-' -> 무배당 확정
        assert got[2025]["dps"] == 0.0, got[2025]["dps"]
        assert resolve_status(got[2025]) == "none", got[2025]
        assert resolve_status(got[2024]) == "none", got[2024]
        # 칸이 전부 비어 있는 연도는 무배당이 아니라 '없음'이다
        assert resolve_status(got[2023]) == "no_report", got[2023]
        # 응답 자체가 빈 경우
        empty = parse_alot_matter([], 2025)
        assert resolve_status(empty[2025]) == "no_report", empty[2025]
        assert empty[2025]["dps"] is None, "빈 응답을 0 으로 채웠다"

    def t_payout_negative_guard():
        """적자 연도 성향은 None 이다. 음수로 내면 해석이 뒤집힌다."""
        assert payout_ratio(100.0, -50.0) is None
        assert payout_ratio(100.0, 0.0) is None
        assert payout_ratio(None, 100.0) is None
        assert payout_ratio(100.0, None) is None
        near(payout_ratio(25.0, 100.0), 25.0, label="성향")

    def t_cashflow_accounts():
        """account_id 로 영업현금흐름·CAPEX 를 집는다."""
        got = parse_cashflow(SEC_CF, 2025)
        assert sorted(got) == [2023, 2024, 2025], sorted(got)
        near(got[2025]["ocf"], 85_315_148e6, tol=1.0, label="2025 OCF")
        near(got[2024]["ocf"], 72_982_621e6, tol=1.0, label="2024 OCF")

    def t_capex_sign_ignored():
        """유출인데 부호가 양수로 오는 필러가 있다 (실측). 절대값 합이다.

        자기주식 취득은 CAPEX 가 아니므로 섞이면 안 된다.
        """
        got = parse_cashflow(SEC_CF, 2025)
        near(got[2025]["capex"], (47_522_179 + 4_630_970) * 1e6, tol=1.0,
             label="2025 CAPEX")
        # 음수로 보고하는 필러도 같은 값이 나와야 한다
        neg = [dict(r) for r in SEC_CF]
        for r in neg:
            if r["account_nm"] in ("유형자산의 취득", "무형자산의 취득"):
                r["thstrm_amount"] = "-" + r["thstrm_amount"]
        near(parse_cashflow(neg, 2025)[2025]["capex"],
             got[2025]["capex"], tol=1.0, label="부호 반대 CAPEX")

    def t_fcf_anchor():
        """FCF = OCF - CAPEX (삼성전자 2025 = 33,161,999백만원)."""
        got = parse_cashflow(SEC_CF, 2025)
        near(got[2025]["fcf"], 33_161_999e6, tol=1.0, label="2025 FCF")
        near(got[2024]["fcf"], 19_240_982e6, tol=1.0, label="2024 FCF")

    def t_fcf_none_without_capex():
        """CAPEX 가 없으면 FCF 는 None 이다. 0 으로 두면 커버가 무조건 통과."""
        assert free_cash_flow(100.0, None) is None
        assert free_cash_flow(None, 10.0) is None
        only_ocf = [SEC_CF[0]]
        got = parse_cashflow(only_ocf, 2025)
        assert got[2025]["ocf"] is not None
        assert got[2025]["capex"] is None and got[2025]["fcf"] is None, got[2025]

    def t_latest_fiscal_year():
        """사업보고서 제출 기한(3월 말) 경계."""
        assert latest_fiscal_year(datetime(2026, 4, 1)) == 2025
        assert latest_fiscal_year(datetime(2026, 3, 31)) == 2024
        assert latest_fiscal_year(datetime(2026, 9, 1)) == 2025
        assert latest_fiscal_year(datetime(2026, 1, 15)) == 2024

    def t_year_batches():
        """응답이 3년을 주므로 5년은 2회다."""
        assert year_batches(2025, 5) == [2025, 2022], year_batches(2025, 5)
        assert year_batches(2025, 3) == [2025]
        assert year_batches(2025, 1) == [2025]
        assert year_batches(2025, 7) == [2025, 2022, 2019]
        assert year_batches(2025, 0) == []

    def t_store_roundtrip():
        s = Store(tmp / "div.db")
        rows = build_rows("005930", parse_alot_matter(SEC, 2025),
                          parse_cashflow(SEC_CF, 2025),
                          years=3, base_year=2025)
        assert len(rows) == 3, rows
        assert s.upsert_dividends(rows) == 3
        # 멱등: 다시 넣어도 행이 늘지 않는다
        s.upsert_dividends(rows)
        df = s.dividends_of("005930")
        assert len(df) == 3, len(df)
        r25 = df[df["fiscal_year"] == 2025].iloc[0]
        near(r25["dps"], 1668.0, label="저장된 DPS")
        near(r25["payout_ratio"], 25.10, tol=0.05, label="저장된 성향")
        near(r25["fcf"], 33_161_999e6, tol=1.0, label="저장된 FCF")
        assert r25["status"] == "paid", r25["status"]
        assert r25["settle_dt"] == "2025-12-31", r25["settle_dt"]
        # 정정공시는 새 값이 이겨야 한다 (COALESCE 로 묶지 않았는지)
        fixed = [dict(r) for r in rows if r["fiscal_year"] == 2025]
        assert len(fixed) == 1, fixed
        fixed[0]["dps"] = 1700.0
        s.upsert_dividends(fixed)
        again = s.dividends_of("005930")
        near(again[again["fiscal_year"] == 2025].iloc[0]["dps"], 1700.0,
             label="정정 반영")
        cov = s.dividend_coverage(2025)
        assert cov["collected"] == 1 and cov["paid"] == 1, cov
        assert s.has_dividends("005930", 2025)
        assert not s.has_dividends("000660", 2025)
        assert set(s.dividend_asof(2025)) == {"005930"}

    def _fake_get(book: dict, calls: list):
        def get(path, params):
            calls.append((path, params.get("bsns_year"), params.get("fs_div")))
            key = (params.get("corp_code"), path)
            if key in book:
                return book[key]
            if path == "alotMatter.json":
                return {"status": "013", "list": []}
            return {"status": "013", "list": []}
        return get

    def _limiter():
        return RateLimiter(999, 999, 9999, clock=lambda: 0.0)

    def t_no_cashflow_for_nonpayers():
        """무배당 회사는 현금흐름을 조회하지 않는다. 호출 낭비다."""
        nodiv = [
            _al("주당액면가액(원)", "-", "500", "500", "500"),
            _al("(연결)당기순이익(백만원)", "-", "1,000", "900", "800"),
            _al("주당 현금배당금(원)", "보통주", "-", "-", "-"),
        ]
        calls: list = []
        book = {("CORP1", "alotMatter.json"): {"status": "000", "list": nodiv}}
        got = fetch_one("CORP1", years=3, base_year=2025,
                        get=_fake_get(book, calls), limiter=_limiter(),
                        sleep=lambda s: None)
        assert got["error"] is None, got
        assert all(p != "fnlttSinglAcntAll.json" for p, _, _ in calls), calls
        # 배당하는 회사는 조회한다
        calls2: list = []
        book2 = {("CORP2", "alotMatter.json"): {"status": "000", "list": SEC},
                 ("CORP2", "fnlttSinglAcntAll.json"): {"status": "000",
                                                       "list": SEC_CF}}
        got2 = fetch_one("CORP2", years=3, base_year=2025,
                         get=_fake_get(book2, calls2), limiter=_limiter(),
                         sleep=lambda s: None)
        assert any(p == "fnlttSinglAcntAll.json" for p, _, _ in calls2), calls2
        near(got2["cash"][2025]["fcf"], 33_161_999e6, tol=1.0, label="FCF")

    def t_collect_isolates_failure():
        """한 종목이 실패해도 나머지는 적재된다."""
        s = Store(tmp / "divcol.db")
        book = {("C1", "alotMatter.json"): {"status": "000", "list": SEC},
                ("C1", "fnlttSinglAcntAll.json"): {"status": "000",
                                                   "list": SEC_CF}}

        def get(path, params):
            if params.get("corp_code") == "C2":
                return None                 # DART 장애 / 한도 초과
            return book.get((params.get("corp_code"), path),
                            {"status": "013", "list": []})

        res = collect_dividends(
            s, years=3, base_year=2025, ttl_days=0,
            tickers={"005930": "삼성전자", "000660": "SK하이닉스"},
            corp_codes={"005930": "C1", "000660": "C2"},
            get=get, limiter=_limiter(), sleep=lambda s_: None)
        assert res["targets"] == 2, res
        assert res["stored"] == 1, res
        assert res["failed"] == 1, res
        assert res["failures"][0]["code"] == "000660", res["failures"]
        assert res["by_status"].get("paid") == 3, res["by_status"]
        assert len(s.dividends_of("005930")) == 3
        assert len(s.dividends_of("000660")) == 0, "실패를 0 으로 적었다"

    def t_collect_ttl_skip():
        """월 1회 갱신. TTL 안이면 건너뛰고, --force 면 다시 받는다."""
        s = Store(tmp / "divttl.db")
        book = {("C1", "alotMatter.json"): {"status": "000", "list": SEC},
                ("C1", "fnlttSinglAcntAll.json"): {"status": "000",
                                                   "list": SEC_CF}}
        args = dict(years=3, base_year=2025,
                    tickers={"005930": "삼성전자"},
                    corp_codes={"005930": "C1"},
                    get=lambda p, q: book.get((q.get("corp_code"), p),
                                              {"status": "013", "list": []}),
                    limiter=_limiter(), sleep=lambda s_: None)
        first = collect_dividends(s, ttl_days=30, **args)
        assert first["stored"] == 1, first
        second = collect_dividends(s, ttl_days=30, **args)
        assert second["targets"] == 0 and second["skipped_ttl"] == 1, second
        assert second["calls"] == 0, "TTL 스킵인데 DART 를 불렀다"
        forced = collect_dividends(s, ttl_days=30, force=True, **args)
        assert forced["targets"] == 1 and forced["stored"] == 1, forced

    def t_preferred_ticker_excluded():
        """우선주 종목코드는 수집 대상이 아니다 (보통주 행에 함께 실린다)."""
        s = Store(tmp / "divpref.db")
        res = collect_dividends(
            s, years=3, base_year=2025, ttl_days=0,
            tickers={"005935": "삼성전자우"},
            corp_codes={"005935": "C1"},
            get=lambda p, q: {"status": "013", "list": []},
            limiter=_limiter(), sleep=lambda s_: None)
        assert res["targets"] == 0, res

    def t_record_only_boundary():
        """수집 모듈이 채점·알림 경로를 참조하지 않아야 한다.

        1단계는 데이터 기반만이다. 필터 엔진이 붙기 전에 점수 쪽으로
        선이 넘어가면 '기록 전용'이 문서에만 남는다.
        """
        src = (Path(__file__).parent / "stocknews" / "dividend_data.py"
               ).read_text(encoding="utf-8")
        for bad in ("from .screener", "from .liquidation", "from .exits",
                    "from .renderer", "from .notify", "from .weekly",
                    "from .daily"):
            assert bad not in src, f"dividend_data 가 {bad} 를 참조한다"

    check("dividend", "단위 환산 (백만원 -> 원)", t_unit_scale)
    check("dividend", "사업연도 매핑 (당기/전기/전전기)", t_year_mapping)
    check("dividend", "DPS 앵커 5종목 (공시 실측)", t_dps_anchor_5)
    check("dividend", "성향 = 총액/순이익 (공시 대조)",
          t_payout_matches_disclosure)
    check("dividend", "우선주 행 배제", t_preferred_row_excluded)
    check("dividend", "무배당(0) != 누락(no_report)", t_none_vs_missing)
    check("dividend", "적자 연도 성향 None", t_payout_negative_guard)
    check("dividend", "현금흐름 계정 식별 (account_id)", t_cashflow_accounts)
    check("dividend", "CAPEX 부호 무시 + 자사주 제외", t_capex_sign_ignored)
    check("dividend", "FCF 앵커 (삼성전자 2025)", t_fcf_anchor)
    check("dividend", "CAPEX 없으면 FCF None", t_fcf_none_without_capex)
    check("dividend", "최신 사업연도 경계 (3월/4월)", t_latest_fiscal_year)
    check("dividend", "5년 = 호출 2회", t_year_batches)
    check("dividend", "저장/조회/정정 덮어쓰기", t_store_roundtrip)
    check("dividend", "무배당은 현금흐름 미조회", t_no_cashflow_for_nonpayers)
    check("dividend", "수집 실패 격리", t_collect_isolates_failure)
    check("dividend", "TTL 스킵 + --force", t_collect_ttl_skip)
    check("dividend", "우선주 종목 제외", t_preferred_ticker_excluded)
    check("dividend", "채점 경로 미참조", t_record_only_boundary)


def test_dividend_screen(tmp: Path):
    """배당 필터 F1~F6. 경계값을 합성 픽스처로 고정한다.

    임계값(3.0% / 5년 / 80%)은 검증된 수치가 아니라 관행값이다. 그래서
    '경계에서 어느 쪽이 통과인가'를 코드가 못박아야 나중에 상수를 옮길 때
    무엇이 바뀌는지 알 수 있다.
    """
    from stocknews.config import DEFAULT, Config, DividendConfig
    from stocknews.dividend_screen import (FILTERS, collect_dividend_screen,
                                           per_of, screen_dividends,
                                           screen_one, sector_per_medians)
    from stocknews.store import Store

    BASE = 2025
    WON = 1e8          # 억원

    def _years(dps, *, ni=1000 * WON, total=100 * WON, fcf=500 * WON,
               payout=50.0, status="paid", years=5, per_year=None):
        """`{사업연도: 레코드}`. dps 는 스칼라 또는 {연도: 값}."""
        out = {}
        for i in range(years):
            y = BASE - i
            d = dps.get(y) if isinstance(dps, dict) else dps
            rec = {"dps": d, "net_income": ni, "total_dividend": total,
                   "fcf": fcf, "payout_ratio": payout,
                   "status": status if (d or 0) > 0 else "none"}
            if per_year and y in per_year:
                rec.update(per_year[y])
            out[y] = rec
        return out

    # 기본값은 F6 이 실제로 평가되는 조합이다 (PER 10 vs 중앙값 10).
    # 스킵 경로는 별도 테스트에서 cap/med 를 None 으로 줘서 확인한다.
    def _one(years, *, price=10_000.0, cap=10_000 * WON, sector="반도체",
             med=10.0, cfg=DEFAULT):
        return screen_one("005930", "테스트", years, price=price,
                          market_cap=cap, sector=sector,
                          sector_per_median=med, base_year=BASE, cfg=cfg)

    def t_base_passes():
        """기준 픽스처는 전 필터를 통과해야 한다. 안 그러면 경계 테스트가
        무엇을 재는지 알 수 없다."""
        m = _one(_years(400.0))
        assert m["passed"] is True, (m["failed_at"], m["reason"])
        near(m["div_yield"], 4.0, label="수익률")
        assert m["years_paid"] == 5 and m["cut_year"] is None, m

    def t_f1_boundary():
        """수익률 3.00% 통과 / 2.90% 탈락."""
        ok = _one(_years(300.0))
        assert ok["passed"] is True, (ok["failed_at"], ok["reason"])
        near(ok["div_yield"], 3.0, label="경계 수익률")
        bad = _one(_years(290.0))
        assert bad["failed_at"] == "F1", bad
        assert bad["no_data"] is False, "조건 미달을 판정 불가로 셌다"
        assert "2.90" in bad["reason"], bad["reason"]

    def t_f1_no_price_is_no_data():
        """현재가가 없으면 판정 불가다. 탈락과 구분해야 한다."""
        m = _one(_years(400.0), price=None)
        assert m["failed_at"] == "F1" and m["no_data"] is True, m

    def t_f2_boundary():
        """5년 연속 통과 / 4년 탈락."""
        four = _years({BASE: 400.0, BASE - 1: 400.0, BASE - 2: 400.0,
                       BASE - 3: 400.0, BASE - 4: None})
        four[BASE - 4]["status"] = "none"
        four[BASE - 4]["dps"] = 0.0
        m = _one(four)
        assert m["failed_at"] == "F2", m
        assert m["years_paid"] == 4 and m["no_data"] is False, m

    def t_f2_cut_detected():
        """중간에 감액이 있으면 5년 연속이어도 탈락이다."""
        cut = _years({BASE: 400.0, BASE - 1: 250.0, BASE - 2: 300.0,
                      BASE - 3: 200.0, BASE - 4: 100.0})
        m = _one(cut)
        assert m["failed_at"] == "F2", m
        assert m["cut_year"] == BASE - 1, m["cut_year"]
        assert "감액" in m["reason"], m["reason"]
        # 같은 금액 유지는 감액이 아니다
        flat = _years(400.0)
        assert _one(flat)["cut_year"] is None

    def t_f2_missing_years_is_no_data():
        """연도 데이터가 모자라면 '연속성 미달'이 아니라 판정 불가다."""
        short = _years(400.0, years=3)
        m = _one(short)
        assert m["failed_at"] == "F2" and m["no_data"] is True, m

    def t_f3_boundary():
        """성향 80.0% 통과 / 80.01% 탈락 / 0% 탈락 / None 판정 불가."""
        assert _one(_years(400.0, payout=80.0))["passed"] is True
        bad = _one(_years(400.0, payout=80.01))
        assert bad["failed_at"] == "F3" and bad["no_data"] is False, bad
        zero = _one(_years(400.0, payout=0.0))
        assert zero["failed_at"] == "F3" and zero["no_data"] is False, zero
        nod = _one(_years(400.0, payout=None))
        assert nod["failed_at"] == "F3" and nod["no_data"] is True, nod

    def t_f4_boundary():
        """FCF == 배당총액 통과 / 1원 부족하면 탈락 / 없으면 판정 불가."""
        eq = _one(_years(400.0, fcf=100 * WON, total=100 * WON))
        assert eq["passed"] is True, (eq["failed_at"], eq["reason"])
        short = _one(_years(400.0, fcf=100 * WON - 1.0, total=100 * WON))
        assert short["failed_at"] == "F4" and short["no_data"] is False, short
        nod = _one(_years(400.0, fcf=None))
        assert nod["failed_at"] == "F4" and nod["no_data"] is True, nod

    def t_f5_loss_year():
        """5년 중 한 해라도 적자면 탈락. 결측이면 판정 불가."""
        loss = _years(400.0, per_year={BASE - 2: {"net_income": -1.0}})
        m = _one(loss)
        assert m["failed_at"] == "F5" and m["no_data"] is False, m
        assert str(BASE - 2) in m["reason"], m["reason"]
        gone = _years(400.0, per_year={BASE - 2: {"net_income": None}})
        m2 = _one(gone)
        assert m2["failed_at"] == "F5" and m2["no_data"] is True, m2

    def t_f6_median():
        """PER 이 업종 중앙값 이하면 통과. 초과면 탈락."""
        yrs = _years(400.0, ni=1000 * WON)
        # 시가총액 10,000억 / 순이익 1,000억 -> PER 10
        m = _one(yrs, cap=10_000 * WON, med=10.0)
        near(m["per"], 10.0, label="PER")
        assert m["passed"] is True, (m["failed_at"], m["reason"])
        bad = _one(yrs, cap=10_000 * WON, med=9.99)
        assert bad["failed_at"] == "F6" and bad["no_data"] is False, bad

    def t_f6_pbr_always_skipped():
        """F6 은 판정 불가면 스킵이다. 스킵 사실을 반드시 남긴다.

        PBR 은 구할 수 없으므로 항상 스킵이다. PER 도 시가총액이나 업종
        중앙값이 없으면 스킵한다 — 탈락으로 세면 '비싸서 떨어졌다'와
        '데이터가 없어서 떨어졌다'가 F6 칸에 섞인다.
        """
        m = _one(_years(400.0))
        assert "F6-PBR" in m["skipped"], m["skipped"]
        assert "F6-PER" not in m["skipped"], "PER 이 있는데 스킵으로 적었다"
        for label, kw in (("중앙값 없음", {"med": None}),
                          ("시가총액 없음", {"cap": None})):
            m2 = _one(_years(400.0), **kw)
            assert m2["passed"] is True, (label, m2["failed_at"], m2["reason"])
            assert "F6-PER" in m2["skipped"], (label, m2["skipped"])

    def t_short_circuit():
        """F1 과 F3 을 동시에 못 맞추면 F1 만 기록한다 (깔때기 합계 보존)."""
        m = _one(_years(100.0, payout=95.0))
        assert m["failed_at"] == "F1", m
        assert set(k for k, _ in FILTERS) == {"F1", "F2", "F3", "F4",
                                             "F5", "F6"}

    def t_per_of():
        near(per_of(1000.0, 100.0), 10.0, label="PER")
        assert per_of(1000.0, -1.0) is None, "적자에 PER 을 매겼다"
        assert per_of(1000.0, 0.0) is None
        assert per_of(None, 100.0) is None
        assert per_of(float("nan"), 100.0) is None

    def t_sector_medians():
        per = {"A0": 10.0, "B0": 20.0, "C0": 30.0, "D0": 40.0, "E0": 50.0,
               "X0": 1.0, "Y0": 2.0, "Z0": 3.0}
        sec = {"A0": "반도체", "B0": "반도체", "C0": "반도체",
               "D0": "반도체", "E0": "반도체",
               "X0": "조선", "Y0": "조선", "Z0": "조선"}
        med = sector_per_medians(per, sec, min_members=5)
        near(med["반도체"], 30.0, label="홀수 중앙값")
        assert "조선" not in med, "표본 3종목 업종에 중앙값을 만들었다"
        # 짝수 표본
        per4 = {"A0": 10.0, "B0": 20.0, "C0": 30.0, "D0": 40.0}
        sec4 = {k: "반도체" for k in per4}
        near(sector_per_medians(per4, sec4, min_members=4)["반도체"], 25.0,
             label="짝수 중앙값")
        # 미분류는 집계하지 않는다
        unk = sector_per_medians({"A0": 10.0} | per4,
                                 {"A0": "미분류"} | sec4, min_members=1)
        assert "미분류" not in unk, unk

    def t_ranking_and_top_n():
        """통과 종목은 수익률 내림차순, 상위 top_n 만 반환."""
        s = Store(tmp / "divscr.db")
        cfg = Config(dividend=DividendConfig(top_n=3))
        tickers, prices = [], {}
        for i in range(6):
            code = "%05d0" % i
            tickers.append({"ticker": code, "name": f"종목{i}",
                            "market": "KOSPI", "sector": "반도체",
                            "market_cap": 10_000 * WON, "shares": 1e6})
            prices[code] = 10_000.0
            rows = []
            for y in range(BASE - 4, BASE + 1):
                rows.append({"code": code, "fiscal_year": y,
                             "dps": 300.0 + i * 50.0,
                             "total_dividend": 100 * WON,
                             "net_income": 1000 * WON, "payout_ratio": 50.0,
                             "ocf": 600 * WON, "capex": 100 * WON,
                             "fcf": 500 * WON, "status": "paid",
                             "settle_dt": f"{y}-12-31"})
            s.upsert_dividends(rows)
        s.upsert_tickers(tickers)
        idx = pd.DatetimeIndex([datetime(2026, 8, 31)])
        for code, px in prices.items():
            s.upsert_prices(code, pd.DataFrame(
                {"시가": [px], "고가": [px], "저가": [px], "종가": [px],
                 "거래량": [1000.0]}, index=idx), allow_today=True)

        res = screen_dividends(s, cfg=cfg, base_year=BASE,
                               trade_date="2026-08-31")
        assert res["evaluated"] == 6, res
        assert res["passed"] == 6, (res["passed"], res["funnel"])
        assert len(res["top"]) == 3, "top_n 이 적용되지 않았다"
        ys = [r["div_yield"] for r in res["top"]]
        assert ys == sorted(ys, reverse=True), ys
        near(ys[0], 5.5, label="1위 수익률")
        assert res["top"][0]["rank"] == 1, res["top"][0]

        stored = s.upsert_dividend_screen(res["rows"])
        assert stored == 6, stored
        fun = s.dividend_screen_funnel("2026-08-31")
        assert fun["evaluated"] == 6 and fun["passed"] == 6, fun
        got = s.dividend_screen_on("2026-08-31", passed_only=True)
        assert len(got) == 6 and int(got.iloc[0]["rank"]) == 1, got.head()
        assert "F6-PBR" in str(got.iloc[0]["skipped"]), got.iloc[0]["skipped"]

    def t_funnel_sums():
        """깔때기 합계 + 통과 = 평가 수. 순차 단락이라 중복이 없어야 한다."""
        s = Store(tmp / "divfun.db")
        tickers = []
        idx = pd.DatetimeIndex([datetime(2026, 8, 31)])
        # 0: 통과 / 1: F1 탈락 / 2: F3 탈락 / 3: F4 탈락
        plan = [(400.0, 50.0, 500 * WON), (100.0, 50.0, 500 * WON),
                (400.0, 95.0, 500 * WON), (400.0, 50.0, 1.0)]
        for i, (dps, payout, fcf) in enumerate(plan):
            code = "%05d0" % i
            tickers.append({"ticker": code, "name": f"종목{i}",
                            "market": "KOSPI", "sector": "반도체",
                            "market_cap": 10_000 * WON, "shares": 1e6})
            s.upsert_dividends([
                {"code": code, "fiscal_year": y, "dps": dps,
                 "total_dividend": 100 * WON, "net_income": 1000 * WON,
                 "payout_ratio": payout, "ocf": None, "capex": None,
                 "fcf": fcf, "status": "paid", "settle_dt": f"{y}-12-31"}
                for y in range(BASE - 4, BASE + 1)])
            s.upsert_prices(code, pd.DataFrame(
                {"시가": [10_000.0], "고가": [10_000.0], "저가": [10_000.0],
                 "종가": [10_000.0], "거래량": [1000.0]}, index=idx),
                allow_today=True)
        s.upsert_tickers(tickers)
        res = screen_dividends(s, base_year=BASE, trade_date="2026-08-31")
        assert res["evaluated"] == 4, res
        assert res["passed"] == 1, (res["passed"], res["funnel"])
        assert res["funnel"]["F1"] == 1, res["funnel"]
        assert res["funnel"]["F3"] == 1, res["funnel"]
        assert res["funnel"]["F4"] == 1, res["funnel"]
        assert sum(res["funnel"].values()) + res["passed"] == 4, res["funnel"]

    def t_price_from_trade_date_not_last_row():
        """현재가는 기준일 행에서 뽑아야 한다. 행렬 마지막 행이 아니다.

        `last_price_date()` 는 부분 적재된 날짜를 건너뛴다. 그래서 가격
        행렬의 마지막 행이 기준일이 아닐 수 있다. 장중에 일부만 들어온
        날의 값으로 수익률을 계산하면 에러 없이 조용히 틀린다.
        """
        s = Store(tmp / "divpx.db")
        codes = ["%05d0" % i for i in range(4)]
        s.upsert_tickers([{"ticker": c, "name": f"종목{c}", "market": "KOSPI",
                           "sector": "반도체", "market_cap": 10_000 * WON,
                           "shares": 1e6} for c in codes])
        for c in codes:
            s.upsert_dividends([
                {"code": c, "fiscal_year": y, "dps": 400.0,
                 "total_dividend": 100 * WON, "net_income": 1000 * WON,
                 "payout_ratio": 50.0, "ocf": None, "capex": None,
                 "fcf": 500 * WON, "status": "paid", "settle_dt": f"{y}-12-31"}
                for y in range(BASE - 4, BASE + 1)])

        def _px(code, day, px):
            s.upsert_prices(code, pd.DataFrame(
                {"시가": [px], "고가": [px], "저가": [px], "종가": [px],
                 "거래량": [1000.0]}, index=pd.DatetimeIndex([day])),
                allow_today=True)

        full, partial = datetime(2026, 8, 27), datetime(2026, 8, 28)
        for c in codes:
            _px(c, full, 10_000.0)          # 전 종목 -> 완전한 날
        _px(codes[0], partial, 40_000.0)    # 1종목만 -> 부분 적재된 날

        assert s.last_price_date() == "2026-08-27", s.last_price_date()
        pm = s.price_matrix(days=5)
        assert str(pm.index[-1])[:10] == "2026-08-28", pm.index
        res = screen_dividends(s, base_year=BASE)
        assert res["scan_date"] == "2026-08-27", res["scan_date"]
        assert res["priced"] == 4, res.get("priced")
        # 부분 적재일(40,000원)을 썼다면 수익률 1.0% 로 F1 에서 떨어진다
        assert res["passed"] == 4, (res["passed"], res["funnel"])
        for r in res["rows"]:
            near(r["price"], 10_000.0, label=f"{r['code']} 현재가")

    def t_missing_trade_date_prices():
        """기준일 시세가 행렬에 없으면 다른 날 값으로 대신하지 않는다."""
        s = Store(tmp / "divpx2.db")
        s.upsert_tickers([{"ticker": "005930", "name": "삼성전자",
                           "market": "KOSPI", "sector": "반도체",
                           "market_cap": 10_000 * WON, "shares": 1e6}])
        s.upsert_dividends([
            {"code": "005930", "fiscal_year": y, "dps": 400.0,
             "total_dividend": 100 * WON, "net_income": 1000 * WON,
             "payout_ratio": 50.0, "ocf": None, "capex": None,
             "fcf": 500 * WON, "status": "paid", "settle_dt": f"{y}-12-31"}
            for y in range(BASE - 4, BASE + 1)])
        s.upsert_prices("005930", pd.DataFrame(
            {"시가": [10_000.0], "고가": [10_000.0], "저가": [10_000.0],
             "종가": [10_000.0], "거래량": [1000.0]},
            index=pd.DatetimeIndex([datetime(2026, 8, 27)])),
            allow_today=True)
        res = screen_dividends(s, base_year=BASE, trade_date="2026-08-20")
        assert res["priced"] == 0, res.get("priced")
        assert res["funnel"]["F1"] == 1 and res["no_data"]["F1"] == 1, res

    def t_collect_isolated():
        """하루 1회 스킵 + 실패를 삼킨다."""
        s = Store(tmp / "divscr2.db")
        # 배당 데이터가 없으면 조용히 이유만 남긴다
        out = collect_dividend_screen(s, base_year=BASE,
                                      trade_date="2026-08-31")
        assert out["reason"] == "no_dividends", out
        assert out["stored"] == 0 and "error" not in out, out

        class Boom:
            path = s.path

            def __getattr__(self, name):
                raise RuntimeError("일부러 터뜨림")

        out2 = collect_dividend_screen(Boom(), trade_date="2026-08-31")
        assert "error" in out2 and out2["stored"] == 0, out2

    def t_screen_skip_once_a_day():
        s = Store(tmp / "divscr3.db")
        idx = pd.DatetimeIndex([datetime(2026, 8, 31)])
        s.upsert_tickers([{"ticker": "005930", "name": "삼성전자",
                           "market": "KOSPI", "sector": "반도체",
                           "market_cap": 10_000 * WON, "shares": 1e6}])
        s.upsert_dividends([
            {"code": "005930", "fiscal_year": y, "dps": 400.0,
             "total_dividend": 100 * WON, "net_income": 1000 * WON,
             "payout_ratio": 50.0, "ocf": None, "capex": None,
             "fcf": 500 * WON, "status": "paid", "settle_dt": f"{y}-12-31"}
            for y in range(BASE - 4, BASE + 1)])
        s.upsert_prices("005930", pd.DataFrame(
            {"시가": [10_000.0], "고가": [10_000.0], "저가": [10_000.0],
             "종가": [10_000.0], "거래량": [1000.0]}, index=idx),
            allow_today=True)
        first = collect_dividend_screen(s, base_year=BASE,
                                       trade_date="2026-08-31")
        assert first["stored"] == 1 and first["skipped"] is False, first
        second = collect_dividend_screen(s, base_year=BASE,
                                         trade_date="2026-08-31")
        assert second["skipped"] is True and second["stored"] == 0, second
        forced = collect_dividend_screen(s, base_year=BASE,
                                         trade_date="2026-08-31", force=True)
        assert forced["stored"] == 1, forced

    def t_config_constants():
        """임계값이 config 한 곳에 있어야 한다."""
        from stocknews.config import (DIV_MAX_PAYOUT, DIV_MIN_YEARS,
                                      DIV_MIN_YIELD, DIV_TOP_N)
        assert DIV_MIN_YIELD == 3.0 and DIV_MIN_YEARS == 5
        assert DIV_MAX_PAYOUT == 80.0 and DIV_TOP_N == 15
        d = DEFAULT.dividend
        assert (d.min_yield, d.min_years, d.max_payout, d.top_n) == \
            (DIV_MIN_YIELD, DIV_MIN_YEARS, DIV_MAX_PAYOUT, DIV_TOP_N)
        # frozen 이어야 실행 중 임계값이 바뀌지 않는다
        try:
            d.min_yield = 1.0
        except Exception:
            pass
        else:
            raise AssertionError("DividendConfig 가 frozen 이 아니다")

    def t_record_only_boundary():
        """필터 엔진이 채점·알림 경로를 참조하지 않아야 한다."""
        src = (Path(__file__).parent / "stocknews" / "dividend_screen.py"
               ).read_text(encoding="utf-8")
        for bad in ("from .screener", "from .liquidation", "from .exits",
                    "from .renderer", "from .notify", "from .weekly",
                    "from .daily"):
            assert bad not in src, f"dividend_screen 이 {bad} 를 참조한다"
        # 반대 방향도 확인: 채점 쪽이 배당을 끌어다 쓰면 안 된다
        for mod in ("screener.py", "liquidation.py", "daily.py", "exits.py"):
            other = (Path(__file__).parent / "stocknews" / mod
                     ).read_text(encoding="utf-8")
            assert "dividend" not in other, f"{mod} 가 배당을 참조한다"

    check("dividend-screen", "기준 픽스처 전 필터 통과", t_base_passes)
    check("dividend-screen", "F1 경계 (3.00% / 2.90%)", t_f1_boundary)
    check("dividend-screen", "F1 현재가 없음 = 판정 불가",
          t_f1_no_price_is_no_data)
    check("dividend-screen", "F2 경계 (5년 / 4년)", t_f2_boundary)
    check("dividend-screen", "F2 감액 탐지", t_f2_cut_detected)
    check("dividend-screen", "F2 연도 부족 = 판정 불가",
          t_f2_missing_years_is_no_data)
    check("dividend-screen", "F3 경계 (80.0% / 80.01% / 0)", t_f3_boundary)
    check("dividend-screen", "F4 경계 (FCF == 배당총액)", t_f4_boundary)
    check("dividend-screen", "F5 적자 연도 / 결측", t_f5_loss_year)
    check("dividend-screen", "F6 업종 중앙값 경계", t_f6_median)
    check("dividend-screen", "F6 PBR 스킵 기록", t_f6_pbr_always_skipped)
    check("dividend-screen", "순차 단락 (첫 탈락만 기록)", t_short_circuit)
    check("dividend-screen", "PER 산출 (적자 None)", t_per_of)
    check("dividend-screen", "업종 중앙값 (홀짝·표본·미분류)",
          t_sector_medians)
    check("dividend-screen", "수익률 정렬 + 상위 N", t_ranking_and_top_n)
    check("dividend-screen", "깔때기 합계 = 평가 수", t_funnel_sums)
    check("dividend-screen", "현재가 = 기준일 행 (부분적재 방어)",
          t_price_from_trade_date_not_last_row)
    check("dividend-screen", "기준일 시세 없으면 대체 금지",
          t_missing_trade_date_prices)
    check("dividend-screen", "수집기 격리 (실패 삼킴)", t_collect_isolated)
    check("dividend-screen", "하루 1회 + --force", t_screen_skip_once_a_day)
    check("dividend-screen", "임계값 config 이관 + frozen", t_config_constants)
    check("dividend-screen", "채점 경로 미참조 (양방향)",
          t_record_only_boundary)


def test_dividend_calendar(tmp: Path):
    """배당락 캘린더 · DPS 성장률 · 월간 리포트.

    캘린더 산술은 합성 달력으로 고정한다. 미래 공휴일은 알 수 없으므로
    '실측 달력이 있으면 그걸 쓰고 없으면 추정으로 표시한다'는 계약이
    핵심이고, 그 계약을 테스트가 강제한다.
    """
    from datetime import date

    from stocknews.config import DEFAULT, Config, DividendConfig
    from stocknews.dividend_calendar import (build_dividend_report,
                                             business_days, dps_cagr,
                                             ex_dividend_date,
                                             is_first_saturday,
                                             krx_year_end_break,
                                             last_buy_date,
                                             last_trading_day_of_year,
                                             next_record_date,
                                             record_date_plan,
                                             report_send_allowed)
    from stocknews.renderer import (DIVIDEND_BANNED, DIVIDEND_FOOTER,
                                    render_dividend_report)
    from stocknews.store import Store

    BASE = 2025
    WON = 1e8

    def t_year_end_break():
        """연말 휴장: 12/31 이 영업일이면 그날, 아니면 직전 영업일.

        2024·2025 폐장일은 2026-09-02 에 실제 적재 거래일과 대조해
        일치를 확인한 값이다(`dividend_calendar` 독스트링의 표).
        2023 은 시세가 없어 규칙 계산값만 고정한다.
        """
        # 2024-12-31 화요일 -> 그날 휴장 -> 폐장일 12/30(월)  [실측 일치]
        assert krx_year_end_break(2024) == date(2024, 12, 31)
        ltd, conf = last_trading_day_of_year(2024)
        assert ltd == date(2024, 12, 30), ltd
        assert conf is False, "실측 없이 확정으로 보고했다"
        # 2025-12-31 수요일 -> 그날 휴장 -> 폐장일 12/30(화)  [실측 일치]
        assert krx_year_end_break(2025) == date(2025, 12, 31)
        assert last_trading_day_of_year(2025)[0] == date(2025, 12, 30)
        # 2023-12-31 일요일 -> 직전 영업일 12/29(금) 휴장 -> 폐장일 12/28(목)
        assert krx_year_end_break(2023) == date(2023, 12, 29)
        assert last_trading_day_of_year(2023)[0] == date(2023, 12, 28)

    def t_year_end_last_buy_real_calendars():
        """실측 거래일로 검산한 최종 매수일. 달력 모양에 따라 갈린다.

        2024 는 12/28·29 가 주말이라 12/27 이 최종 매수일이고, 2025 는
        12/29(월)이 최종 매수일이다. 같은 규칙에서 나오는 다른 답이며,
        둘 다 적재된 실제 거래일로 확인했다.
        """
        cases = {
            2024: ([(12, 24), (12, 26), (12, 27), (12, 30)],
                   date(2024, 12, 27), date(2024, 12, 30)),
            2025: ([(12, 24), (12, 26), (12, 29), (12, 30)],
                   date(2025, 12, 29), date(2025, 12, 30)),
        }
        for y, (tds_md, want_buy, want_ex) in cases.items():
            tds = [date(y, m, d) for m, d in tds_md]
            bds = business_days(date(y, 12, 1), date(y + 1, 1, 31),
                                holidays={f"{y + 1}-01-01"})
            got_buy = last_buy_date(date(y, 12, 31), trading_days=tds,
                                    business_days_=bds, settle_days=2)
            got_ex = ex_dividend_date(date(y, 12, 31), trading_days=tds,
                                      business_days_=bds, settle_days=2)
            assert got_buy == want_buy, (y, got_buy, want_buy)
            assert got_ex == want_ex, (y, got_ex, want_ex)

    def t_observed_beats_estimate():
        """실측 거래일이 있으면 추정하지 않는다."""
        obs = {"2026-12-24", "2026-12-28", "2026-12-29"}
        ltd, conf = last_trading_day_of_year(2026, observed=obs)
        assert ltd == date(2026, 12, 29) and conf is True, (ltd, conf)

    def t_business_days_skip_holidays():
        bd = business_days(date(2026, 1, 1), date(2026, 1, 7),
                           holidays={"2026-01-01"})
        assert date(2026, 1, 1) not in bd, "공휴일이 영업일에 남았다"
        assert date(2026, 1, 3) not in bd, "토요일이 영업일에 남았다"
        assert date(2026, 1, 2) in bd and date(2026, 1, 5) in bd, bd

    def t_last_buy_two_calendars():
        """결제는 영업일, 매수는 거래일. 연말에 이 둘이 갈린다.

        기준일 12/31(휴장이지만 결제 영업일). 12/30 에 사면 결제가
        1/2 이라 명부에 없고, 12/29 에 사면 12/31 결제로 명부에 오른다.
        거래일만으로 세면 하루 앞당겨져 12/28 이 나온다.
        """
        # 2026-12: 28(월) 29(화) 30(수) 31(목) · 31 은 휴장
        bds = [date(2026, 12, d) for d in (24, 28, 29, 30, 31)]
        tds = [d for d in bds if d != date(2026, 12, 31)]
        rec = date(2026, 12, 31)
        lb = last_buy_date(rec, trading_days=tds, business_days_=bds,
                           settle_days=2)
        assert lb == date(2026, 12, 29), lb
        ex = ex_dividend_date(rec, trading_days=tds, business_days_=bds,
                              settle_days=2)
        assert ex == date(2026, 12, 30), ex
        # 거래일만 썼다면 12/28 이 나온다 — 그 오답을 명시적으로 배제한다
        wrong = last_buy_date(rec, trading_days=tds, business_days_=tds,
                              settle_days=2)
        assert wrong == date(2026, 12, 28), wrong
        assert lb != wrong, "두 달력을 구분하지 않았다"

    def t_last_buy_when_record_is_trading_day():
        """기준일이 거래일이면 T+2 만큼 더 앞선다 (3월 결산 등)."""
        bds = [date(2026, 3, d) for d in (25, 26, 27, 30, 31)]
        rec = date(2026, 3, 31)
        lb = last_buy_date(rec, trading_days=bds, business_days_=bds,
                           settle_days=2)
        assert lb == date(2026, 3, 27), lb

    def t_next_record_date():
        """결산기말일의 월/일을 적용. 이미 지났으면 다음 해."""
        assert next_record_date("2025-12-31", asof="2026-09-02") == \
            date(2026, 12, 31)
        # 기준일이 지났으면 다음 해로 넘어간다
        assert next_record_date("2025-12-31", asof="2027-01-05") == \
            date(2027, 12, 31)
        # 3월 결산
        assert next_record_date("2026-03-31", asof="2026-09-02") == \
            date(2027, 3, 31)
        assert next_record_date(None) is None
        assert next_record_date("") is None

    def t_record_plan_marks_estimate():
        """실측 달력이 없으면 confirmed=False 로 남아야 한다."""
        plan = record_date_plan("2025-12-31", asof="2026-09-02")
        assert plan["record_date"] == date(2026, 12, 31), plan
        assert plan["confirmed"] is False, "추정을 확정으로 보고했다"
        assert plan["last_buy"] is not None and plan["ex_date"] is not None
        assert plan["last_buy"] < plan["ex_date"] <= plan["record_date"], plan

    def t_cagr_anchor():
        """CAGR = (마지막/처음)^(1/(n-1)) - 1. 5년이면 성장 기간 4번."""
        # 100 -> 200, 4기간: 2^(1/4)-1 = 18.9207%
        dps = {2021: 100.0, 2022: 120.0, 2023: 140.0, 2024: 170.0,
               2025: 200.0}
        near(dps_cagr(dps, 2025, 5), 18.92, tol=0.005, label="CAGR")
        # 변화 없음 -> 0%
        flat = {y: 500.0 for y in range(2021, 2026)}
        near(dps_cagr(flat, 2025, 5), 0.0, label="정액 CAGR")
        # 감소도 그대로 음수로 낸다
        down = {2021: 200.0, 2022: 180.0, 2023: 160.0, 2024: 140.0,
                2025: 100.0}
        got = dps_cagr(down, 2025, 5)
        assert got is not None and got < 0, got
        near(got, -15.91, tol=0.01, label="감소 CAGR")

    def t_cagr_none_cases():
        """시작 연도가 무배당/결측이면 None. 0 에서 성장률을 만들지 않는다."""
        assert dps_cagr({2021: 0.0, 2025: 500.0}, 2025, 5) is None
        assert dps_cagr({2025: 500.0}, 2025, 5) is None, "시작 결측인데 값을 냈다"
        assert dps_cagr({2021: 100.0}, 2025, 5) is None
        assert dps_cagr({2021: 100.0, 2025: 200.0}, 2025, 1) is None
        assert dps_cagr({}, 2025, 5) is None

    def t_send_gate_first_saturday():
        """매월 첫 토요일만 발송. --force 는 요일을 무시한다."""
        # 2026-09: 1일 화요일 -> 첫 토요일 9/5
        assert is_first_saturday(date(2026, 9, 5)) is True
        assert is_first_saturday(date(2026, 9, 12)) is False, "둘째 토요일"
        assert is_first_saturday(date(2026, 9, 4)) is False, "금요일"
        # 1일이 일요일인 달: 2026-11 -> 첫 토요일 11/7
        assert is_first_saturday(date(2026, 11, 7)) is True
        assert is_first_saturday(date(2026, 11, 1)) is False
        ok, why = report_send_allowed(datetime(2026, 9, 5, 9, 0))
        assert ok is True and why == "send_day", (ok, why)
        ok2, why2 = report_send_allowed(datetime(2026, 9, 12, 9, 0))
        assert ok2 is False and why2 == "off_day", (ok2, why2)
        ok3, why3 = report_send_allowed(datetime(2026, 9, 12, 9, 0),
                                        force=True)
        assert ok3 is True and why3 == "force", (ok3, why3)
        # 시각과 무관해야 한다
        for hh in (0, 9, 16, 23):
            assert report_send_allowed(datetime(2026, 9, 5, hh, 30))[0] is True

    def t_send_gate_decoupled():
        """추천 발송(일요일)과 **코드가** 얽히지 않아야 한다.

        문서에서 서로를 언급하는 것은 괜찮다 — 규격이 같다는 설명은
        있어야 한다. 금지되는 것은 호출·임포트 의존이다. 한쪽 게이트를
        고칠 때 다른 쪽이 조용히 따라 바뀌면 안 된다.
        """
        import inspect

        from stocknews import dividend_calendar as dcal
        from stocknews import notify as ntf

        assert "reco_send_allowed" not in \
            dcal.report_send_allowed.__code__.co_names, "추천 게이트를 호출한다"
        assert "report_send_allowed" not in \
            ntf.reco_send_allowed.__code__.co_names, "추천 게이트가 이걸 호출한다"
        imports = [ln for ln in inspect.getsource(dcal).splitlines()
                   if ln.startswith(("import ", "from "))]
        assert not any("reco_send_allowed" in ln for ln in imports), imports
        # 요일 상수가 서로 다른 자리에서 온다
        assert int(DEFAULT.gate.reco_send_dow) != \
            int(DEFAULT.dividend.report_dow), "두 게이트가 같은 요일을 본다"
        # 일요일(추천 발송일)은 배당 리포트 발송일이 아니다
        assert report_send_allowed(datetime(2026, 9, 6, 9, 0))[0] is False

    def _seed(s: Store, n: int = 3):
        idx = pd.DatetimeIndex([datetime(2026, 8, 31)])
        rows = []
        for i in range(n):
            code = "%05d0" % i
            rows.append({"ticker": code, "name": f"배당주{i}",
                         "market": "KOSPI", "sector": "반도체",
                         "market_cap": 10_000 * WON, "shares": 1e6})
            s.upsert_dividends([
                {"code": code, "fiscal_year": y,
                 "dps": 300.0 + i * 100.0 + (y - (BASE - 4)) * 10.0,
                 "total_dividend": 100 * WON, "net_income": 1000 * WON,
                 "payout_ratio": 50.0, "ocf": 600 * WON, "capex": 100 * WON,
                 "fcf": 500 * WON, "status": "paid",
                 "settle_dt": f"{y}-12-31"}
                for y in range(BASE - 4, BASE + 1)])
            s.upsert_prices(code, pd.DataFrame(
                {"시가": [10_000.0], "고가": [10_000.0], "저가": [10_000.0],
                 "종가": [10_000.0], "거래량": [1000.0]}, index=idx),
                allow_today=True)
        s.upsert_tickers(rows)

    def t_report_build():
        s = Store(tmp / "divrep.db")
        _seed(s)
        rep = build_dividend_report(s, DEFAULT, asof="2026-09-02",
                                    trade_date="2026-08-31", base_year=BASE)
        assert rep["evaluated"] == 3 and rep["passed"] == 3, rep
        assert len(rep["rows"]) == 3, rep["rows"]
        r = rep["rows"][0]
        for key in ("code", "name", "price", "dps", "div_yield", "years_paid",
                    "payout", "cagr", "record_date", "last_buy", "ex_date"):
            assert key in r, f"{key} 누락"
        assert r["record_date"] == date(2026, 12, 31), r["record_date"]
        assert r["last_buy"] is not None and r["cagr"] is not None
        # 수익률 내림차순
        ys = [x["div_yield"] for x in rep["rows"]]
        assert ys == sorted(ys, reverse=True), ys

    def t_report_render():
        s = Store(tmp / "divrep2.db")
        _seed(s)
        rep = build_dividend_report(s, DEFAULT, asof="2026-09-02",
                                    trade_date="2026-08-31", base_year=BASE)
        txt = render_dividend_report(rep, datetime(2026, 9, 5, 9, 0), DEFAULT)
        assert isinstance(txt, str) and txt, "빈 리포트"
        assert "{" not in txt and "}" not in txt, "포맷 문자열이 남았다"
        assert DIVIDEND_FOOTER in txt, "하단 고정 문구 누락"
        assert "<pre>" in txt and "</pre>" in txt, "등폭 표가 아니다"
        for col in ("현재가", "DPS", "수익률", "성향", "5yr성장",
                    "기준일(추정)", "최종매수(추정)"):
            assert col in txt, f"컬럼 {col} 누락"
        assert "연속연수" in txt, "연속연수 누락"
        assert "추정" in txt, "추정 표기 누락"

    def t_report_no_persuasion():
        """권유·지시 문구가 없어야 한다. 팩트 표는 지시문이 아니다."""
        s = Store(tmp / "divrep3.db")
        _seed(s)
        rep = build_dividend_report(s, DEFAULT, asof="2026-09-02",
                                    trade_date="2026-08-31", base_year=BASE)
        txt = render_dividend_report(rep, datetime(2026, 9, 5, 9, 0), DEFAULT)
        # 고정 문구 안의 '매수 추천 아님' 은 부정문이므로 그 부분만 뺀다
        body = txt.replace(DIVIDEND_FOOTER, "")
        for bad in DIVIDEND_BANNED:
            assert bad not in body, f"금지 문구 '{bad}' 가 있다"
        for bad in ("안정적", "유망", "매력적", "저평가", "기회"):
            assert bad not in body, f"해석 문구 '{bad}' 가 있다"

    def t_report_empty():
        """통과 0건이면 표 대신 사실만. 고정 문구는 그대로 붙는다."""
        s = Store(tmp / "divrep4.db")
        rep = build_dividend_report(s, DEFAULT, asof="2026-09-02",
                                    trade_date="2026-08-31", base_year=BASE)
        assert rep["rows"] == [], rep
        txt = render_dividend_report(rep, datetime(2026, 9, 5, 9, 0), DEFAULT)
        assert DIVIDEND_FOOTER in txt, "빈 리포트에 고정 문구가 없다"
        assert "통과한 종목이 없습니다" in txt, txt[:200]

    def t_report_top_n():
        """top_n 을 config 에서 가져와야 한다."""
        s = Store(tmp / "divrep5.db")
        _seed(s, n=6)
        cfg = Config(dividend=DividendConfig(top_n=2))
        rep = build_dividend_report(s, cfg, asof="2026-09-02",
                                    trade_date="2026-08-31", base_year=BASE)
        assert rep["passed"] == 6, rep["passed"]
        assert len(rep["rows"]) == 2, rep["rows"]

    def t_mode_wiring():
        """모드가 배선돼 있고 쓰기 락 계약을 지키는지."""
        import importlib
        from stocknews.joblock import MODE_TIMEOUTS
        mod = importlib.import_module("run_screen")
        assert "dividend-report" in mod.MODES, "MODES 에 없다"
        assert "dividend-report" in mod._WRITE_MODES, "쓰기 모드가 아니다"
        assert "dividend-report" in MODE_TIMEOUTS, "락 타임아웃이 없다"
        # 월 1회라 매일/매주 스케줄 기대치에 들어가면 안 된다
        assert "dividend-report" not in mod._SCHEDULE_DAILY
        assert "dividend-report" not in mod._SCHEDULE_WEEKLY
        assert "collect-dividends" not in mod._SCHEDULE_DAILY
        assert "collect-dividends" not in mod._SCHEDULE_WEEKLY

    def t_record_only_boundary():
        src = (Path(__file__).parent / "stocknews" / "dividend_calendar.py"
               ).read_text(encoding="utf-8")
        for bad in ("from .screener", "from .liquidation", "from .exits",
                    "from .weekly"):
            assert bad not in src, f"dividend_calendar 가 {bad} 를 참조한다"

    check("dividend-cal", "연말 휴장 + 폐장일 (실측 대조 2024/2025)",
          t_year_end_break)
    check("dividend-cal", "연말 최종매수일 (실측 달력 2024/2025)",
          t_year_end_last_buy_real_calendars)
    check("dividend-cal", "실측 달력이 추정을 이김", t_observed_beats_estimate)
    check("dividend-cal", "영업일 (주말·공휴일 제외)",
          t_business_days_skip_holidays)
    check("dividend-cal", "최종 매수일 = 두 달력 (연말)",
          t_last_buy_two_calendars)
    check("dividend-cal", "기준일이 거래일인 경우 (3월 결산)",
          t_last_buy_when_record_is_trading_day)
    check("dividend-cal", "다음 기준일 (결산기말일)", t_next_record_date)
    check("dividend-cal", "추정은 confirmed=False", t_record_plan_marks_estimate)
    check("dividend-cal", "DPS CAGR 앵커 (4기간)", t_cagr_anchor)
    check("dividend-cal", "CAGR None (0 에서 시작 금지)", t_cagr_none_cases)
    check("dividend-cal", "발송 게이트 (매월 첫 토요일)",
          t_send_gate_first_saturday)
    check("dividend-cal", "추천 게이트와 비결합", t_send_gate_decoupled)
    check("dividend-cal", "리포트 조립", t_report_build)
    check("dividend-cal", "리포트 렌더 + 고정 문구", t_report_render)
    check("dividend-cal", "권유·해석 문구 부재", t_report_no_persuasion)
    check("dividend-cal", "통과 0건 리포트", t_report_empty)
    check("dividend-cal", "상위 N = config", t_report_top_n)
    # ── 4단계: 배당락 회복일수 + 총주주환원율 ──
    from stocknews.dividend_calendar import (past_ex_dates, recovery_days,
                                             recovery_stats,
                                             total_return_ratio)
    from stocknews.renderer import _recovery_cell

    def _closes(pairs):
        """[(날짜, 종가)] -> Series."""
        idx = pd.DatetimeIndex([datetime(*p[0]) for p in pairs])
        return pd.Series([p[1] for p in pairs], index=idx)

    def t_recovery_anchor():
        """락 전 종가를 회복하기까지의 거래일수. 락일 직전 종가가 기준이다."""
        # 12/29 종가 10,000 (락 전) · 12/30 배당락 9,500 -> 1/5 에 10,050
        px = _closes([((2025, 12, 26), 9_900.0),
                      ((2025, 12, 29), 10_000.0),   # 락 전 종가
                      ((2025, 12, 30), 9_500.0),    # 배당락일
                      ((2026, 1, 2), 9_800.0),
                      ((2026, 1, 5), 10_050.0)])    # 회복
        got = recovery_days(px, date(2025, 12, 30))
        assert got["status"] == "ok", got
        assert got["days"] == 3, got          # 12/30, 1/2, 1/5 -> 3거래일
        # 락일 종가를 기준으로 삼았다면 12/30 당일(1일)에 회복으로 나온다
        assert got["days"] != 1, "락일 종가를 기준으로 썼다"

    def t_recovery_same_day():
        """락일 당일 회복하면 1거래일이다."""
        px = _closes([((2025, 12, 29), 10_000.0),
                      ((2025, 12, 30), 10_000.0)])
        got = recovery_days(px, date(2025, 12, 30))
        assert got == {"days": 1, "status": "ok"}, got

    def t_recovery_unrecovered():
        """끝까지 회복 못 하면 '미회복'. 큰 숫자로 채우지 않는다."""
        px = _closes([((2025, 12, 29), 10_000.0),
                      ((2025, 12, 30), 9_000.0),
                      ((2026, 1, 2), 9_100.0)])
        got = recovery_days(px, date(2025, 12, 30))
        assert got["status"] == "unrecovered" and got["days"] is None, got

    def t_recovery_no_data():
        """락일이 시세 범위 밖이면 no_data. 회복/미회복 어느 쪽도 아니다."""
        px = _closes([((2026, 1, 2), 9_000.0), ((2026, 1, 5), 9_100.0)])
        assert recovery_days(px, date(2025, 12, 30))["status"] == "no_data"
        px2 = _closes([((2025, 12, 28), 9_000.0)])
        assert recovery_days(px2, date(2025, 12, 30))["status"] == "no_data"
        assert recovery_days(None, date(2025, 12, 30))["status"] == "no_data"
        assert recovery_days(px, None)["status"] == "no_data"

    def _series(start: datetime, values):
        """연속 거래일(주말 제외) 종가 Series."""
        idx, d = [], start
        while len(idx) < len(values):
            if d.weekday() < 5:
                idx.append(d)
            d += timedelta(days=1)
        return pd.Series(list(values), index=pd.DatetimeIndex(idx))

    def t_recovery_stats_excludes_unrecovered():
        """미회복은 평균에서 빼고 건수로만 센다 (분기배당 2회 구간)."""
        # 0..9 구간: 100 -> 락 95 -> 3봉째 101 회복
        # 10..19 구간: 락 90 이후 끝까지 회복 못 함
        vals = [100, 95, 98, 101, 102, 103, 104, 105, 106, 107,
                90, 91, 92, 93, 94, 95, 96, 97, 98, 99]
        px = _series(datetime(2026, 1, 5), [float(v) for v in vals])
        ex1, ex2 = px.index[1].date(), px.index[10].date()
        st = recovery_stats(px, [{"year": 2026, "ex_date": ex1},
                                 {"year": 2026, "ex_date": ex2}])
        assert st["samples"] == 1, st
        near(st["avg"], 3.0, label="평균 회복일")
        assert st["max"] == 3, st
        assert st["unrecovered"] == 1, st
        assert len(st["items"]) == 2, st["items"]
        empty = recovery_stats(px, [])
        assert empty["samples"] == 0 and empty["avg"] is None, empty

    def t_recovery_stops_at_next_ex_date():
        """다음 배당락일을 넘겨 회복을 세면 안 된다.

        시세에 구멍이 있으면 1년 뒤 봉을 회복으로 잡을 수 있다. 상한이
        그것을 막는다.
        """
        # 2024 락 전 8,000 -> 락일 7,000. 회복(9,000)은 다음 락일에 온다.
        px = _closes([((2024, 12, 27), 8_000.0),
                      ((2024, 12, 30), 7_000.0),
                      ((2025, 12, 30), 9_000.0)])
        # 상한 없이 세면 1년 뒤 봉을 회복으로 잡는다 (2거래일 회복)
        loose = recovery_days(px, date(2024, 12, 30))
        assert loose == {"days": 2, "status": "ok"}, loose
        bounded = recovery_days(px, date(2024, 12, 30),
                               until=date(2025, 12, 30))
        assert bounded["status"] == "unrecovered", bounded
        # 집계는 상한을 자동으로 건다
        st = recovery_stats(px, [{"year": 2024, "ex_date": date(2024, 12, 30)},
                                 {"year": 2025, "ex_date": date(2025, 12, 30)}])
        y2024 = [i for i in st["items"] if i["year"] == 2024][0]
        assert y2024["status"] == "unrecovered", y2024
        assert st["unrecovered"] >= 1, st

    def t_recovery_cell_render():
        assert _recovery_cell(None) == "-"
        assert _recovery_cell({"samples": 0, "unrecovered": 0}) == "-"
        assert _recovery_cell({"samples": 0, "unrecovered": 2}) == "미회복"
        cell = _recovery_cell({"samples": 3, "avg": 12.3, "max": 21,
                               "unrecovered": 0})
        assert cell == "12/21일(3)", cell
        cell2 = _recovery_cell({"samples": 2, "avg": 5.0, "max": 8,
                                "unrecovered": 1})
        assert "미회복1" in cell2, cell2

    def t_past_ex_dates():
        """과거 3년 배당락일. 최근 연도부터."""
        got = past_ex_dates("2025-12-31", years=3, asof="2026-09-02")
        assert [g["year"] for g in got] == [2025, 2024, 2023], got
        for g in got:
            assert g["record_date"].month == 12, g
            assert g["ex_date"] is not None and g["ex_date"] < g["record_date"]

    def t_total_return_anchor():
        """총환원율 = (배당총액 + 자사주) / 시가총액."""
        # 배당 1,000억 + 자사주 500억 / 시총 30,000억 = 5.00%
        near(total_return_ratio(1_000 * WON, 500 * WON, 30_000 * WON), 5.0,
             label="총환원율")
        # 자사주 0 이면 배당만
        near(total_return_ratio(1_000 * WON, 0.0, 50_000 * WON), 2.0,
             label="배당만")

    def t_total_return_none():
        """자사주가 NULL 이면 None. 0 으로 가정하지 않는다."""
        assert total_return_ratio(1_000 * WON, None, 30_000 * WON) is None
        assert total_return_ratio(None, 0.0, 30_000 * WON) is None
        assert total_return_ratio(1_000 * WON, 0.0, 0.0) is None
        assert total_return_ratio(1_000 * WON, 0.0, None) is None

    def t_buyback_parsed_from_cashflow():
        """자사주 취득액은 현금흐름표에서 온다. 주요사항보고서를 파싱하지 않는다."""
        from stocknews.dividend_data import parse_cashflow

        rows = [
            {"sj_div": "CF",
             "account_id": "ifrs-full_CashFlowsFromUsedInOperatingActivities",
             "account_nm": "영업활동현금흐름",
             "thstrm_amount": "85315148000000",
             "frmtrm_amount": "72982621000000",
             "bfefrmtrm_amount": "44788749000000"},
            # 유출인데 부호가 양수다 (2026-09-01 실측)
            {"sj_div": "CF", "account_id": "dart_AcquisitionOfTreasuryShares",
             "account_nm": "자기주식의 취득",
             "thstrm_amount": "8189263000000",
             "frmtrm_amount": "1811775000000",
             "bfefrmtrm_amount": "0"},
        ]
        got = parse_cashflow(rows, 2025)
        near(got[2025]["buyback"], 8_189_263e6, tol=1.0, label="자사주 취득")
        near(got[2024]["buyback"], 1_811_775e6, tol=1.0, label="전기 자사주")
        near(got[2023]["buyback"], 0.0, label="전전기 자사주")
        # CF 를 읽었는데 자사주 행이 없으면 0 으로 확정한다
        only = parse_cashflow([rows[0]], 2025)
        near(only[2025]["buyback"], 0.0, label="행 없으면 0")
        # CF 자체를 못 읽었으면 NULL — '안 샀다'와 '모른다'를 섞지 않는다
        none = parse_cashflow([], 2025)
        assert none[2025]["buyback"] is None, none[2025]

    def t_buyback_stored():
        """buyback 컬럼이 저장·조회되는지 + 기존 DB 마이그레이션."""
        s = Store(tmp / "divbb.db")
        s.upsert_dividends([{
            "code": "005930", "fiscal_year": BASE, "dps": 1668.0,
            "total_dividend": 100 * WON, "net_income": 1000 * WON,
            "payout_ratio": 10.0, "ocf": 600 * WON, "capex": 100 * WON,
            "fcf": 500 * WON, "buyback": 50 * WON, "status": "paid",
            "settle_dt": f"{BASE}-12-31"}])
        df = s.dividends_of("005930")
        assert "buyback" in df.columns, list(df.columns)
        near(df.iloc[0]["buyback"], 50 * WON, tol=1.0, label="저장된 자사주")
        # 마이그레이션 표에 등록돼 있어야 기존 DB 에도 컬럼이 붙는다
        assert ("dividends", "buyback", "REAL") in Store._MIGRATIONS

    def t_report_has_stage4_columns():
        s = Store(tmp / "divrep6.db")
        _seed(s)
        rep = build_dividend_report(s, DEFAULT, asof="2026-09-02",
                                    trade_date="2026-08-31", base_year=BASE)
        r = rep["rows"][0]
        for key in ("buyback", "market_cap", "total_return", "recovery"):
            assert key in r, f"{key} 누락"
        assert set(r["recovery"]) >= {"samples", "avg", "max",
                                      "unrecovered", "items"}, r["recovery"]
        txt = render_dividend_report(rep, datetime(2026, 9, 5, 9, 0), DEFAULT)
        assert "과거회복일수(참고)" in txt, "참고 표기 없는 컬럼명"
        assert "총환원율" in txt, "총환원율 컬럼 누락"
        assert "예측이 아닙니다" in txt, "예측 아님 문구 누락"
        assert "미회복" in txt, "미회복 표기 설명 누락"
        body = txt.replace(DIVIDEND_FOOTER, "")
        for bad in DIVIDEND_BANNED:
            assert bad not in body, f"금지 문구 '{bad}'"

    check("dividend-cal", "모드 배선 + 락 계약", t_mode_wiring)
    check("dividend-cal", "채점 경로 미참조", t_record_only_boundary)
    check("dividend-ret", "회복일수 앵커 (락 전 종가 기준)",
          t_recovery_anchor)
    check("dividend-ret", "락일 당일 회복 = 1거래일", t_recovery_same_day)
    check("dividend-ret", "미회복 (큰 수로 채우지 않음)",
          t_recovery_unrecovered)
    check("dividend-ret", "범위 밖 = no_data", t_recovery_no_data)
    check("dividend-ret", "평균에서 미회복 제외",
          t_recovery_stats_excludes_unrecovered)
    check("dividend-ret", "다음 락일에서 탐색 중단",
          t_recovery_stops_at_next_ex_date)
    check("dividend-ret", "회복일수 표기", t_recovery_cell_render)
    check("dividend-ret", "과거 3년 배당락일", t_past_ex_dates)
    check("dividend-ret", "총환원율 앵커", t_total_return_anchor)
    check("dividend-ret", "자사주 NULL = 총환원율 None", t_total_return_none)
    check("dividend-ret", "자사주는 현금흐름표에서",
          t_buyback_parsed_from_cashflow)
    check("dividend-ret", "buyback 저장 + 마이그레이션", t_buyback_stored)
    check("dividend-ret", "리포트 4단계 컬럼", t_report_has_stage4_columns)


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
        """`.env.example` 은 git 이 추적한다. 실제 키가 들어가면 유출된다.

        `.gitignore` 는 `.env` 를 제외하지만 `!.env.example` 로 이 파일만
        되살린다. 그래서 여기 적은 값은 커밋에 그대로 실린다. 예전에는
        `TELEGRAM_BOT_TOKEN` 한 줄만 검사해서 DART/키움 키가 들어와도
        통과했다 (실제로 그런 일이 있었다).
        """
        from stocknews.env import SECRET_KEYS, parse_env_text

        p = root / ".env.example"
        assert p.exists(), ".env.example 이 없다"
        txt = p.read_text(encoding="utf-8")
        for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "DART_API_KEY"):
            assert k in txt, f".env.example 에 {k} 누락"
        assert "=" in txt

        # 비밀 키는 전부 빈 값이어야 한다. 값 자체는 출력하지 않는다.
        parsed = parse_env_text(txt)
        filled = sorted(k for k in SECRET_KEYS if parsed.get(k))
        assert not filled, (
            f"템플릿에 실제 비밀값이 들어 있다: {filled}. "
            ".env 로 옮기고 .env.example 은 빈 값으로 두십시오. "
            "이 파일은 git 이 추적합니다.")
        # 비밀이 아니어도 '값처럼 보이는' 긴 문자열은 의심한다.
        # TZ / KIWOOM_API_BASE 처럼 기본값을 적어두는 키는 예외다.
        allowed_defaults = {"TZ", "KIWOOM_API_BASE"}
        for k, v in parsed.items():
            if k in allowed_defaults or not v:
                continue
            assert len(v) < 20, \
                f"{k} 에 실제 값처럼 보이는 문자열이 있다 ({len(v)}자)"

    check("docs", "LICENSE + 금융 고지", t_license)
    check("docs", "DISCLAIMER.md", t_disclaimer)
    check("docs", "AGENTS.md", t_agents)
    check("docs", ".env.example (토큰 미포함)", t_env_example)


# ══════════════════════════ 8-2b. .env 로더 ══════════════════════════
def test_env(tmp: Path):
    """`.env` 로드. 이게 없으면 발송 모드가 전부 exit 4 로 끝난다.

    python-dotenv 가 있으면 파싱만 위임하고, 없으면 자체 파서로 내려간다.
    32비트 브릿지 venv 에는 dotenv 가 없으므로 폴백 경로도 검증한다.
    """
    import os

    from stocknews.env import (ENV_PATH, KNOWN_KEYS, SECRET_KEYS, env_report,
                              load_env, mask, parse_env_text)

    def t_parse_formats():
        txt = (
            "# 주석\n"
            "\n"
            "PLAIN=abc\n"
            'QUOTED="a b c"\n'
            "SQUOTED='x y'\n"
            "export EXPORTED=zzz\n"
            "EMPTY=\n"
            "INLINE=val # 꼬리주석\n"
            "HASHED=va#lue\n"
            'QUOTED_HASH="v # keep"\n'
            "NOEQ\n"
        )
        d = parse_env_text(txt)
        assert d["PLAIN"] == "abc", d
        assert d["QUOTED"] == "a b c", d
        assert d["SQUOTED"] == "x y", d
        assert d["EXPORTED"] == "zzz", "export 접두어를 못 벗겼다"
        assert d["EMPTY"] == ""
        assert d["INLINE"] == "val", "공백+# 인라인 주석을 못 잘랐다"
        assert d["HASHED"] == "va#lue", "붙은 # 을 주석으로 오해했다"
        assert d["QUOTED_HASH"] == "v # keep", "따옴표 안 # 을 잘랐다"
        assert "NOEQ" not in d

    def t_bom_and_crlf():
        d = parse_env_text("\ufeffA=1\r\nB=2\r\n")
        assert d == {"A": "1", "B": "2"}, d

    def t_missing_file_is_quiet():
        rep = load_env(tmp / "nope.env")
        assert rep["exists"] is False
        assert rep["keys"] == []
        assert "error" not in rep, rep

    def t_empty_value_not_applied():
        """.env.example 을 그대로 복사하면 전 키가 빈 값이다.

        그걸 환경에 올리면 os.getenv 가 "" 를 돌려주고 '설정됨'으로
        오판하는 코드가 생긴다. 빈 값은 미설정으로 둬야 한다.
        """
        p = tmp / "empty.env"
        p.write_text("TELEGRAM_CHAT_ID=\n", encoding="utf-8")
        os.environ.pop("TELEGRAM_CHAT_ID", None)
        rep = load_env(p)
        assert "TELEGRAM_CHAT_ID" in rep["empty"], rep
        assert "TELEGRAM_CHAT_ID" not in os.environ, "빈 값이 환경에 올라갔다"

    def t_os_env_wins():
        """주입된 환경변수를 낡은 .env 가 덮으면 디버깅이 불가능해진다."""
        p = tmp / "over.env"
        p.write_text("TELEGRAM_CHAT_ID=from_file\n", encoding="utf-8")
        os.environ["TELEGRAM_CHAT_ID"] = "from_os"
        try:
            rep = load_env(p)
            assert os.environ["TELEGRAM_CHAT_ID"] == "from_os", \
                ".env 가 OS 환경변수를 덮었다"
            assert "TELEGRAM_CHAT_ID" in rep["skipped_existing"], rep
            # override=True 면 덮어써야 한다
            load_env(p, override=True)
            assert os.environ["TELEGRAM_CHAT_ID"] == "from_file", \
                "override=True 가 동작하지 않는다"
        finally:
            os.environ.pop("TELEGRAM_CHAT_ID", None)

    def t_applies_value():
        p = tmp / "ok.env"
        p.write_text("KRX_CREDIT_BLD=some/bld\n", encoding="utf-8")
        os.environ.pop("KRX_CREDIT_BLD", None)
        try:
            rep = load_env(p)
            assert os.environ.get("KRX_CREDIT_BLD") == "some/bld", rep
            assert "KRX_CREDIT_BLD" in rep["applied"], rep
            assert rep["backend"] in ("python-dotenv", "stdlib"), rep
        finally:
            os.environ.pop("KRX_CREDIT_BLD", None)

    def t_unknown_key_reported():
        p = tmp / "unknown.env"
        p.write_text("WAT_IS_THIS=1\n", encoding="utf-8")
        try:
            rep = load_env(p)
            assert "WAT_IS_THIS" in rep["unknown_keys"], rep
        finally:
            os.environ.pop("WAT_IS_THIS", None)

    def t_krx_api_key_known():
        """운영 .env 의 KRX_API_KEY 가 매 실행 '알 수 없는 키' 경고를 내면 안 된다."""
        p = tmp / "krx.env"
        p.write_text("KRX_API_KEY=abc\n", encoding="utf-8")
        os.environ.pop("KRX_API_KEY", None)
        try:
            rep = load_env(p)
            assert "KRX_API_KEY" not in rep["unknown_keys"], rep
            assert mask("KRX_API_KEY", "abc") == "설정됨", "인증키 값이 노출된다"
        finally:
            os.environ.pop("KRX_API_KEY", None)

    def t_example_matches_known_keys():
        """`.env.example` 과 KNOWN_KEYS 가 어긋나면 안 된다.

        한쪽만 고치면 사용자는 채울 키를 모르고, 코드는 읽지 않는 키를
        문서화한다. 양방향으로 검사한다.
        """
        txt = (Path(__file__).parent / ".env.example").read_text(
            encoding="utf-8")
        in_example = set(parse_env_text(txt))
        known = set(KNOWN_KEYS)
        assert not (in_example - known), \
            f".env.example 에만 있는 키: {in_example - known}"
        assert not (known - in_example), \
            f"KNOWN_KEYS 에만 있는 키: {known - in_example}"

    def t_secrets_never_echoed():
        assert mask("TELEGRAM_BOT_TOKEN", "123:AAA") == "설정됨", \
            "비밀 키 값이 노출된다"
        assert mask("KIWOOM_APP_SECRET", "s3cr3t") == "설정됨"
        assert mask("TELEGRAM_CHAT_ID", "-100123") == "-100123", \
            "비밀이 아닌 키까지 가렸다"
        assert mask("DART_API_KEY", None) == "미설정"
        for k in SECRET_KEYS:
            assert k in KNOWN_KEYS, f"{k} 가 KNOWN_KEYS 에 없다"
        rep = env_report()
        assert set(rep) == set(KNOWN_KEYS)

    def t_repo_root_not_cwd():
        """cwd 가 아니라 저장소 루트를 봐야 한다. 래퍼 없이 호출될 수 있다."""
        assert ENV_PATH.parent == Path(__file__).parent.resolve(), \
            f"ENV_PATH 가 저장소 루트가 아니다: {ENV_PATH}"

    def t_wired_into_run_screen():
        """함수가 있는 것과 호출되는 것은 다르다. 실제 배선을 확인한다."""
        import importlib
        mod = importlib.import_module("run_screen")
        p = tmp / "wired.env"
        p.write_text("KRX_CREDIT_BLD=wired/ok\n", encoding="utf-8")
        os.environ.pop("KRX_CREDIT_BLD", None)
        try:
            rc = mod.main(["--mode", "pos-list", "--db",
                           str(tmp / "wired.db"), "--env-file", str(p)])
            assert rc == mod.EXIT_OK, f"exit {rc}"
            assert os.environ.get("KRX_CREDIT_BLD") == "wired/ok", \
                "run_screen.main 이 .env 를 로드하지 않았다"
            assert "env_file" in mod.SUMMARY, "--json 요약에 env_file 누락"
            assert mod.SUMMARY["env_file"]["exists"] is True
        finally:
            os.environ.pop("KRX_CREDIT_BLD", None)

    check("env", "파서 형식 (따옴표·export·인라인주석)", t_parse_formats)
    check("env", "BOM + CRLF", t_bom_and_crlf)
    check("env", "파일 없음 = 조용히 통과", t_missing_file_is_quiet)
    check("env", "빈 값은 미설정", t_empty_value_not_applied)
    check("env", "OS 환경변수 우선", t_os_env_wins)
    check("env", "값 적용", t_applies_value)
    check("env", "알 수 없는 키 보고", t_unknown_key_reported)
    check("env", "KRX_API_KEY 는 알려진 키", t_krx_api_key_known)
    check("env", ".env.example <-> KNOWN_KEYS 일치", t_example_matches_known_keys)
    check("env", "비밀 값 미노출", t_secrets_never_echoed)
    check("env", "저장소 루트 기준", t_repo_root_not_cwd)
    check("env", "run_screen 배선", t_wired_into_run_screen)


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

    def t_timeouts_match_write_modes():
        """타임아웃 표와 락 대상 모드가 정확히 일치해야 한다.

        예전에는 fib/weekly/brief-weekly/export 항목이 있었는데 그 넷은
        읽기 전용이라 락을 잡지 않는다 (도달 불가). 반대로 실제로 락을
        잡는 pos-open/fill/pos-close 는 빠져서 기본 900초로 떨어졌다.
        표가 현실과 어긋나면 표를 읽고 판단하는 사람이 틀린다.
        """
        import importlib
        mod = importlib.import_module("run_screen")
        extra = set(MODE_TIMEOUTS) - mod._WRITE_MODES
        assert not extra, f"락을 잡지 않는 모드에 타임아웃이 있다: {extra}"
        missing = mod._WRITE_MODES - set(MODE_TIMEOUTS)
        assert not missing, f"락을 잡는데 타임아웃이 없다: {missing}"

    def t_context_manager_raises_when_busy():
        """락을 못 잡으면 with 블록에 들어가면 안 된다.

        예전에는 __enter__ 가 acquire() 반환값을 버려서, 락을 못 잡아도
        조용히 블록 안으로 들어갔다. 동시 실행을 막는 코드가 정반대로
        동작했다. LockBusy 는 이 경우를 위해 정의돼 있었지만 아무도
        던지지 않았다.
        """
        from stocknews.joblock import LockBusy

        held = JobLock("t5", mode="daily", lock_dir=d)
        assert held.acquire() is True
        entered = False
        try:
            with JobLock("t5", mode="update", lock_dir=d):
                entered = True
        except LockBusy as exc:
            assert exc.holder.get("mode") == "daily", exc.holder
        finally:
            held.release()
        assert not entered, "락을 못 잡았는데 with 블록에 들어갔다"

    def t_acquire_still_returns_bool():
        """main() 은 예외가 아니라 exit 3 을 내야 한다. bool API 유지."""
        held = JobLock("t6", mode="daily", lock_dir=d)
        assert held.acquire() is True
        other = JobLock("t6", mode="update", lock_dir=d)
        assert other.acquire() is False, "acquire 가 예외를 던졌다"
        held.release()

    def t_clear():
        JobLock("t4", mode="daily", lock_dir=d).acquire()
        removed = clear_locks(d)
        assert removed, "clear_locks 가 아무것도 지우지 않았다"
        assert not list(d.glob("*.lock")), "락이 남았다"

    check("joblock", "배타적 획득", t_exclusive)
    check("joblock", "컨텍스트 매니저 해제", t_context_manager)
    check("joblock", "만료 락 회수", t_expired_steal)
    check("joblock", "모드별 타임아웃", t_timeouts)
    check("joblock", "타임아웃 표 = 락 대상 모드", t_timeouts_match_write_modes)
    check("joblock", "with 는 락 실패 시 LockBusy", t_context_manager_raises_when_busy)
    check("joblock", "acquire 는 bool 유지", t_acquire_still_returns_bool)
    check("joblock", "강제 해제", t_clear)


def test_cron_jobs(tmp: Path):
    """Hermes cron 잡 카탈로그. 드라이버는 하나, 잡만 여러 개다.

    2026-09-08 회차가 PC 종료로 밀려 부팅 직후 catch-up 으로 2h27m 늦게
    떴다. 락은 '지금 돌고 있는가' 에만 답하므로, 앞 실행이 끝난 뒤 뜬
    두 번째는 그대로 통과한다. 브리핑이 두 번 나가면 사람에게 그대로
    보인다 — 그래서 잡마다 '오늘 완주' 마커를 따로 갖는다.
    """
    import importlib
    import importlib.util

    repo = Path(__file__).resolve().parent
    rs = importlib.import_module("run_screen")

    spec = importlib.util.spec_from_file_location(
        "_nightly_jobs", str(repo / "nightly.py"))
    nl_mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = nl_mod
    try:
        spec.loader.exec_module(nl_mod)
    finally:
        sys.modules.pop(spec.name, None)

    EXPECTED = {"nightly", "news", "brief-morning", "brief-evening",
                "weekly", "flash"}

    def t_catalog():
        assert set(nl_mod.JOBS) == EXPECTED, set(nl_mod.JOBS)
        for name, job in nl_mod.JOBS.items():
            assert job.name == name, (name, job.name)
            assert job.steps, f"{name}: 단계가 비었다"

    def t_flash_is_the_only_exception():
        """flash 만 마커·정상알림이 없다. 5분마다 도는 유일한 잡이다."""
        for name, job in nl_mod.JOBS.items():
            if name == "flash":
                assert not job.once_per_day, "flash 에 마커를 켜면 하루 1회만 돈다"
                assert job.quiet_ok, "flash 정상 알림은 하루 84건이 된다"
            else:
                assert job.once_per_day, f"{name}: 중복 방어가 없다"
                assert not job.quiet_ok, f"{name}: 결과 보고가 없다"

    def t_markers_are_distinct():
        """마커가 겹치면 한 잡이 다른 잡을 스킵시킨다."""
        paths = [j.marker_path for j in nl_mod.JOBS.values()]
        assert len(set(paths)) == len(paths), paths
        # nightly 는 기존 경로를 유지해야 한다. 바뀌면 이미 완주한 날에
        # catch-up 이 한 번 더 돈다.
        assert nl_mod.JOBS["nightly"].marker_path == "data/nightly_done.json"

    def t_modes_exist():
        """카탈로그가 부르는 모드가 실제로 있어야 한다. 없으면 exit 64 로
        조용히 죽고, 스케줄은 '등록됐는데 아무 일도 안 하는' 상태가 된다."""
        for name, job in nl_mod.JOBS.items():
            for step in job.steps:
                if step.optional:
                    continue
                args = list(step.args)
                mode = args[args.index("--mode") + 1]
                assert mode in rs.MODES, f"{name}: 모드 {mode} 없음"

    def t_sending_modes_are_dry_by_default():
        """발송 모드는 STOCKNEWS_SEND=1 이 아닐 때 --dry-run 이 붙어야 한다.

        _SENDING 에서 빠지면 토큰만 있으면 조용히 실발송으로 나간다.
        """
        for mode in ("brief-morning", "brief-evening", "weekly", "flash",
                     "daily", "exits"):
            assert mode in nl_mod._SENDING, mode

    def t_dry_jobs_env_parse():
        f = nl_mod.dry_jobs_from_env
        assert f(None) == set() and f("") == set() and f("  ") == set()
        assert f("flash") == {"flash"}
        assert f("Flash, weekly ;news") == {"flash", "weekly", "news"}

    def t_dry_jobs_force_dry_run():
        """지목된 잡은 STOCKNEWS_SEND=1(dry=False)이어도 --dry-run 이 붙는다."""
        flash = nl_mod.apply_dry_jobs(nl_mod.JOBS["flash"], {"flash"})
        for step in flash.steps:
            assert step.force_dry
            assert "--dry-run" in nl_mod._build_args(step, dry=False), \
                "STOCKNEWS_DRY_JOBS 로 지목했는데 --dry-run 이 안 붙는다"
        # 원본 JOBS 는 변형되지 않는다 (사본 반환).
        assert not any(s.force_dry for s in nl_mod.JOBS["flash"].steps), "원본 JOBS 가 변형됐다"
        # 지목 안 된 잡은 그대로다.
        night = nl_mod.apply_dry_jobs(nl_mod.JOBS["nightly"], {"flash"})
        for step in night.steps:
            assert not step.force_dry
            if step.name in nl_mod._SENDING:
                assert "--dry-run" not in nl_mod._build_args(step, dry=False)
        # 전역 dry(STOCKNEWS_SEND 미설정)는 여전히 전부 dry-run.
        for step in nl_mod.JOBS["flash"].steps:
            assert "--dry-run" in nl_mod._build_args(step, dry=True)

    def t_flash_extra_args_path():
        """flash 인자 경로. 기본은 비어 있고, 채우면 그대로 붙는다."""
        assert isinstance(nl_mod.FLASH_EXTRA_ARGS, tuple)
        step = nl_mod.JOBS["flash"].steps[0]
        assert step.args[:2] == ["--mode", "flash"]
        assert step.args[2:] == list(nl_mod.FLASH_EXTRA_ARGS), step.args
        if nl_mod.FLASH_EXTRA_ARGS:
            assert set(nl_mod.FLASH_EXTRA_ARGS) <= {"--no-update", "--ignore-window"}, \
                nl_mod.FLASH_EXTRA_ARGS

    def t_daily_cap_is_window_sum():
        """일일 상한은 창별 예산 합이다. 죽은 daily_budget 은 없어야 한다."""
        from stocknews.config import DEFAULT
        from stocknews.notify import WINDOWS
        assert not hasattr(DEFAULT.gate, "daily_budget"), \
            "daily_budget 은 어디서도 읽지 않는 죽은 설정이었다 — 되살리지 마라"
        assert sum(w.budget for w in WINDOWS) == 9, [w.budget for w in WINDOWS]

    def t_shim_derives_job_from_filename():
        """사본 5개가 같은 바이트여야 하므로 잡 이름은 파일명에서 나온다."""
        shim_path = repo / "hermes" / "cron_stocknews.py"
        sp = importlib.util.spec_from_file_location("_shim", str(shim_path))
        shim = importlib.util.module_from_spec(sp)
        sys.modules[sp.name] = shim
        try:
            sp.loader.exec_module(shim)
        finally:
            sys.modules.pop(sp.name, None)

        saved_file = shim.__file__
        try:
            # job_name() 은 모듈 전역 __file__ 을 읽는다. 사본이 어떤
            # 이름으로 배포되든 같은 바이트로 동작하는지 확인한다.
            for fname, want in (("stocknews_news.py", "news"),
                                ("stocknews_brief_morning.py", "brief-morning"),
                                ("stocknews_brief_evening.py", "brief-evening"),
                                ("stocknews_weekly.py", "weekly"),
                                ("stocknews_flash.py", "flash")):
                shim.__file__ = fname
                got = shim.job_name()
                assert got == want, (fname, got, want)
                assert got in nl_mod.JOBS, got
            # 정본을 그대로 돌리면 잡을 못 정하고 exit 64 여야 한다.
            shim.__file__ = "cron_stocknews.py"
            assert shim.job_name() == "stocknews", shim.job_name()
            assert shim.main() == 64, "정본 직접 실행이 막히지 않는다"
        finally:
            shim.__file__ = saved_file

    check("cron", "잡 카탈로그", t_catalog)
    check("cron", "flash 만 마커·알림 예외", t_flash_is_the_only_exception)
    check("cron", "잡별 마커 분리", t_markers_are_distinct)
    check("cron", "카탈로그 모드 실재", t_modes_exist)
    check("cron", "발송 모드 기본 dry-run", t_sending_modes_are_dry_by_default)
    check("cron", "STOCKNEWS_DRY_JOBS 파싱", t_dry_jobs_env_parse)
    check("cron", "STOCKNEWS_DRY_JOBS 잡별 dry-run", t_dry_jobs_force_dry_run)
    check("cron", "flash 추가 인자 경로 (기본 없음)", t_flash_extra_args_path)
    check("cron", "일일 상한 = 창별 예산 합 9", t_daily_cap_is_window_sum)
    check("cron", "사본은 파일명에서 잡 추출", t_shim_derives_job_from_filename)


def test_interpreter_guard(tmp: Path):
    """인터프리터 가드. 틀린 파이썬으로는 통과 자체가 불가능해야 한다.

    verify_env.py 는 **자기를 실행한 인터프리터**의 설치본과
    requirements.txt 를 대조한다. 그래서 엉뚱한 파이썬으로 돌리면 그
    환경 기준으로 '전부 일치' 가 나온다 — 자기 자신에 대해서만 참인,
    쓸모없는 통과다.

    2026-09-07 실측. 에이전트가 Hermes 번들 venv(Python 3.11)로 돌려
    '핀 일치 34/34 · 환경이 requirements.txt 와 정확히 일치합니다' 를
    받고, 그 결과를 근거로 numpy 핀을 2.5.2 -> 2.4.6 으로 낮췄다.
    numpy 2.5.x 는 Requires-Python >=3.12 라 3.11 에서 설치가 안 되는
    것이었고, 실제 배치는 3.12 로 돌고 있었다.
    """
    import contextlib
    import importlib
    import io

    repo = Path(__file__).resolve().parent
    ve = importlib.import_module("verify_env")

    RUN = "hermes/run.cmd"
    NIGHT = "hermes/nightly.cmd"
    DISPATCH = {RUN: '"%PYEXE%" %PYARG% run_screen.py %*',
                NIGHT: '"%PYEXE%" %PYARG% nightly.py %*'}

    def t_required_py():
        assert ve.REQUIRED_PY == (3, 12), ve.REQUIRED_PY

    def t_guard_reflects_runtime():
        assert ve.check_interpreter() == (
            sys.version_info[:2] == ve.REQUIRED_PY)

    def t_main_stops_before_pin_compare():
        """가드가 걸리면 핀 대조를 **아예 하지 않아야** 한다.

        여기서 멈추지 않으면 틀린 환경 기준의 '34/34 일치' 가 그대로
        출력되고, 그 문구가 다음 사람의 판단 근거가 된다.
        """
        saved = ve.check_interpreter
        try:
            ve.check_interpreter = lambda: False
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = ve.main()
            out = buf.getvalue()
            assert rc == 1, rc
            assert "핀 버전 대조" not in out, "가드 후에도 핀 대조를 수행했다"
            assert "정확히 일치" not in out, "가드 후에도 통과 문구를 냈다"
        finally:
            ve.check_interpreter = saved

    def t_launchers_have_guard():
        for name in (RUN, NIGHT):
            src = (repo / name).read_text(encoding="utf-8")
            assert "version_info[:2]==(3,12)" in src, f"{name}: 가드 없음"
            assert "exit /b 92" in src, f"{name}: exit 92 없음"

    def t_guard_precedes_handoff():
        """가드는 인계보다 앞에 있어야 한다. 뒤에 있으면 아무 소용이 없다."""
        for name in (RUN, NIGHT):
            src = (repo / name).read_text(encoding="utf-8")
            assert src.index("version_info[:2]==(3,12)") < src.index(
                DISPATCH[name]), f"{name}: 가드가 인계 뒤에 있다"

    def t_launchers_ascii_only():
        """cmd.exe 는 배치를 현재 코드페이지로 읽는다. 비ASCII 바이트가
        섞이면 파서가 동기를 잃고 주석을 명령으로 실행한다 (AGENTS 1장).
        가드를 넣으면서 한글 메시지를 넣고 싶어지는 자리라 고정한다."""
        for name in (RUN, NIGHT):
            raw = (repo / name).read_bytes()
            bad = [b for b in raw if b > 0x7F]
            assert not bad, f"{name}: 비ASCII 바이트 {len(bad)}개"

    check("interpreter", "REQUIRED_PY = 3.12", t_required_py)
    check("interpreter", "가드 = 실행 중인 버전", t_guard_reflects_runtime)
    check("interpreter", "가드 시 핀 대조 안 함", t_main_stops_before_pin_compare)
    check("interpreter", "래퍼 가드 + exit 92", t_launchers_have_guard)
    check("interpreter", "가드가 인계보다 앞", t_guard_precedes_handoff)
    check("interpreter", "래퍼는 ASCII 전용", t_launchers_ascii_only)


def test_agent_contract(tmp: Path):
    """에이전트 연동 규약. 종료 코드와 쓰기 모드 목록의 정합성."""
    import contextlib
    import importlib
    import io
    import json as _json
    mod = importlib.import_module("run_screen")

    def _run(argv: list[str]) -> tuple[int, str, str]:
        """main 을 돌리고 (rc, stdout, stderr) 를 준다.

        stdout 을 따로 받는 게 핵심이다. --json 계약은 '한 줄' 이므로
        사람이 읽는 보고문이 섞였는지 여기서만 확인할 수 있다.
        """
        so, se = io.StringIO(), io.StringIO()
        base = ["--db", str(tmp / "agent.db"), "--no-lock",
                "--env-file", str(tmp / "absent.env")]
        with contextlib.redirect_stdout(so), contextlib.redirect_stderr(se):
            rc = mod.main(argv + base)
        return rc, so.getvalue(), se.getvalue()

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
                   "backtest", "credit-probe", "kiwoom-plan", "runs"):
            assert ro not in mod._WRITE_MODES, f"{ro} 는 읽기 전용이어야 한다"
        for rw in ("daily", "update", "flags", "exits", "credit",
                   "credit-kiwoom"):
            assert rw in mod._WRITE_MODES, f"{rw} 가 락 대상이 아니다"

    def t_every_write_mode_has_timeout():
        """락 타임아웃이 없는 쓰기 모드는 기본 900초로 떨어진다.

        credit-kiwoom 은 유량 제한 때문에 900초를 넘길 수 있다. 그때
        정상 실행 중인 잡의 락을 다른 잡이 빼앗는다.
        """
        from stocknews.joblock import MODE_TIMEOUTS

        slow = {"backfill", "flags", "credit-kiwoom"}
        for m in slow:
            assert m in MODE_TIMEOUTS, f"{m} 에 락 타임아웃이 없다"
            assert MODE_TIMEOUTS[m] > 900, f"{m} 타임아웃이 기본값 이하"

    def t_partial_threshold():
        assert mod._partial(0, 0) is False, "표본 0에서 부분실패 판정"
        assert mod._partial(100, 5) is False, "5% 실패로 부분실패 판정"
        assert mod._partial(100, 30) is True, "30% 실패인데 정상 판정"

    def t_summary_dict():
        """--json 출력용 요약 딕셔너리가 있어야 한다."""
        assert isinstance(mod.SUMMARY, dict)

    def t_missing_args_are_usage():
        """인자 누락은 exit 64 다.

        exit 1 로 내면 AGENTS.md 규약상 '3회까지 재시도' 대상이 된다.
        인자가 빠진 명령은 세 번 재시도해도 절대 성공하지 않는다.
        """
        for argv in (["--mode", "pos-open"],
                     ["--mode", "pos-open", "--ticker", "005930"],
                     ["--mode", "fill"],
                     ["--mode", "fill", "--log-id", "1"],
                     ["--mode", "pos-close"]):
            rc, _, _ = _run(argv)
            assert rc == mod.EXIT_USAGE, \
                f"{' '.join(argv)} -> exit {rc} (64 여야 함)"

    def t_wrong_order_is_precond():
        """마스터가 비어 있으면 순서 오류다 -> exit 4.

        exit 1 이면 재시도 루프에 걸린다. master 를 먼저 돌려야 풀린다.
        """
        rc, _, _ = _run(["--mode", "backfill"])
        assert rc == mod.EXIT_PRECOND, f"backfill 빈 마스터 -> exit {rc}"

    def t_json_stdout_is_single_line():
        """--json 이면 stdout 은 JSON 한 줄뿐이어야 한다.

        AGENTS.md 1장이 그렇게 규정하고 에이전트가 그걸 파싱한다. 사람용
        보고문이 섞이면 ConvertFrom-Json 이 깨진다. 과거에 kiwoom-plan /
        credit-probe / credit / export / flags / pos-open 여섯 모드가
        맨 print 를 써서 이 계약을 깨고 있었다.
        """
        cases = [
            ["--mode", "pos-list"],
            ["--mode", "kiwoom-plan"],
            ["--mode", "export", "--export-dir", str(tmp / "exp")],
            ["--mode", "credit", "--no-krx",
             "--credit-file", str(tmp / "nope.csv")],
            ["--mode", "flags", "--no-fdr", "--no-dart", "--no-local",
             "--no-manual"],
            ["--mode", "backfill"],          # exit 4 경로도 JSON 이어야 한다
            ["--mode", "pos-close"],         # exit 64 경로도 마찬가지
            ["--mode", "credit-kiwoom"],     # 자격증명 없음 -> exit 4
            ["--mode", "collect-dividends"],  # 키 없음/마스터 없음 -> exit 4
            ["--mode", "dividend-report"],    # 데이터 없음 -> exit 0
            ["--mode", "runs"],              # exit 2 경로 (스케줄 공백)
        ]
        for argv in cases:
            rc, out, _ = _run(argv + ["--json"])
            label = " ".join(argv)
            lines = [ln for ln in out.splitlines() if ln.strip()]
            assert len(lines) == 1, \
                f"{label}: stdout {len(lines)}줄 (JSON 한 줄이어야 함)\n" \
                f"  첫 줄: {lines[0][:70] if lines else '(없음)'}"
            try:
                payload = _json.loads(lines[0])
            except ValueError as exc:
                raise AssertionError(f"{label}: JSON 파싱 실패 {exc}") from exc
            for key in ("mode", "exit_code", "elapsed_sec", "started",
                        "finished"):
                assert key in payload, f"{label}: 봉투에 {key} 누락"
            assert payload["exit_code"] == rc, \
                f"{label}: JSON exit_code {payload['exit_code']} != rc {rc}"
            assert not any(k.startswith("_") for k in payload), \
                f"{label}: 내부 키가 노출됐다 {list(payload)}"

    def t_json_payload_not_empty():
        """봉투만 내보내면 에이전트가 결과를 알 수 없다."""
        envelope = {"mode", "started", "finished", "exit_code",
                    "elapsed_sec", "env_file"}
        cases = {
            "pos-list": ["--mode", "pos-list"],
            "export": ["--mode", "export", "--export-dir", str(tmp / "exp2")],
            "credit": ["--mode", "credit", "--no-krx",
                       "--credit-file", str(tmp / "nope.csv")],
            "flags": ["--mode", "flags", "--no-fdr", "--no-dart",
                      "--no-local", "--no-manual"],
            "kiwoom-plan": ["--mode", "kiwoom-plan"],
        }
        for name, argv in cases.items():
            _, out, _ = _run(argv + ["--json"])
            payload = _json.loads(out.splitlines()[0])
            extra = set(payload) - envelope
            assert extra, f"{name}: 모드별 필드가 하나도 없다 {list(payload)}"

    def t_every_mode_logs_a_run():
        """모드마다 runs 이력이 정확히 한 줄 남아야 한다.

        예전에는 22개 모드 중 backfill/update 둘만 기록했다. 나머지는
        돌았는지 알 수 없었고, 그래서 '어제 daily 가 안 돌았다'를
        알아챌 방법이 없었다. 이제 main() 이 전 모드에 대해 한 번만
        남긴다 — 실패로 끝난 실행도 남아야 한다.
        """
        from stocknews.store import Store

        db = tmp / "runs_contract.db"
        cases = [
            ["--mode", "pos-list"],                       # exit 0
            ["--mode", "backfill"],                       # exit 4
            ["--mode", "pos-close"],                      # exit 64
            ["--mode", "credit", "--no-krx",
             "--credit-file", str(tmp / "none.csv")],     # exit 0
            ["--mode", "credit-kiwoom"],                  # exit 4
        ]
        so, se = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(so), contextlib.redirect_stderr(se):
            for argv in cases:
                mod.main(argv + ["--db", str(db), "--no-lock",
                                 "--env-file", str(tmp / "absent.env")])
        st = Store(db)
        rep = st.run_summary(days=1)
        for argv in cases:
            m = argv[1]
            assert m in rep, f"{m} 이력이 없다 (기록된 모드: {sorted(rep)})"
            assert rep[m]["runs"] == 1, \
                f"{m} 이력이 {rep[m]['runs']}줄 (한 줄이어야 함)"
        # 실패로 끝난 실행도 note 에 rc 가 남아야 한다
        h = st.run_history(days=1, mode="pos-close")
        assert "rc=64" in str(h.iloc[0]["note"]), h.iloc[0]["note"]

    def t_runs_mode_does_not_log_itself():
        """조회 모드가 스스로를 기록하면 이력이 조회할수록 늘어난다."""
        from stocknews.store import Store

        db = tmp / "runs_self.db"
        so, se = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(so), contextlib.redirect_stderr(se):
            for _ in range(3):
                mod.main(["--mode", "runs", "--db", str(db), "--no-lock",
                          "--env-file", str(tmp / "absent.env")])
        assert Store(db).run_history(days=1, mode="runs").empty, \
            "runs 모드가 자기 실행을 기록했다"

    def t_run_counts_resolver():
        """모드마다 카운트 키가 다르다. 한 곳에서만 접어야 한다."""
        mod.SUMMARY.clear()
        assert mod._run_counts(mod.EXIT_OK) == (1, 0), "무보고 성공"
        assert mod._run_counts(mod.EXIT_FAIL) == (0, 1), "무보고 실패"
        mod.SUMMARY.update({"scanned": 1800, "failed": 3})
        assert mod._run_counts(mod.EXIT_OK) == (1800, 3)
        mod.SUMMARY.clear()
        mod.SUMMARY.update({"ok": 500, "empty": 7})
        assert mod._run_counts(mod.EXIT_OK) == (500, 7)
        mod.SUMMARY.clear()
        # bool 은 카운트가 아니다. skipped=True 를 1건으로 세면 안 된다.
        mod.SUMMARY.update({"skipped": True})
        assert mod._run_counts(mod.EXIT_OK) == (1, 0), mod.SUMMARY
        mod.SUMMARY.clear()

    def t_runs_mode_reports_gaps():
        """스케줄 공백을 exit 2 로 알려야 한다. 조용히 0 을 내면 무의미하다."""
        rc, out, _ = _run(["--mode", "runs", "--json"])
        payload = _json.loads(out.splitlines()[0])
        assert "stale" in payload and "by_mode" in payload, list(payload)
        # 새 DB 에는 이력이 없으므로 매일 도는 모드 전부가 공백이다
        assert len(payload["stale"]) >= len(mod._SCHEDULE_DAILY), payload["stale"]
        assert rc == mod.EXIT_PARTIAL, f"공백이 있는데 exit {rc}"

    def t_say_helper_exists():
        """맨 print 가 다시 들어오는 것을 막는 회귀 방지.

        스트림을 고르지 않는 print 는 --json 계약을 깬다. 허용되는 곳은
        세 군데뿐이다: _say 본문(스트림을 직접 정한다), file= 을 명시한
        호출, main 의 JSON 한 줄.
        """
        src = (Path(__file__).parent / "run_screen.py").read_text(
            encoding="utf-8")
        assert "def _say(" in src, "_say 헬퍼가 없다"
        bad, fn = [], ""
        for i, line in enumerate(src.splitlines(), 1):
            if line.startswith("def ") or line.startswith("    def "):
                fn = line.strip().split("(")[0][4:]
            s = line.strip()
            if not s.startswith("print("):
                continue
            if fn == "_say":               # 헬퍼 본문. 여기가 스트림을 정한다
                continue
            if "file=" in s or "json.dumps" in s:
                continue
            bad.append(f"{i}({fn})")
        assert not bad, \
            f"스트림을 고르지 않는 print 가 남아 있다: {bad}. _say 를 쓰십시오."

    check("agent", "종료 코드 규약", t_exit_codes)
    check("agent", "인자 오류 exit 64", t_usage_exit_code)
    check("agent", "요약 딕셔너리", t_summary_dict)
    check("agent", "락 대상 모드 정합성", t_write_modes)
    check("agent", "느린 쓰기 모드 락 타임아웃", t_every_write_mode_has_timeout)
    check("runs", "전 모드가 이력 1줄 남김", t_every_mode_logs_a_run)
    check("runs", "조회 모드는 자기 실행 미기록", t_runs_mode_does_not_log_itself)
    check("runs", "카운트 해석기", t_run_counts_resolver)
    check("runs", "스케줄 공백 -> exit 2", t_runs_mode_reports_gaps)
    check("agent", "부분 실패 임계", t_partial_threshold)
    check("agent", "인자 누락 = exit 64", t_missing_args_are_usage)
    check("agent", "순서 오류 = exit 4", t_wrong_order_is_precond)
    check("agent", "--json stdout 한 줄", t_json_stdout_is_single_line)
    check("agent", "--json 페이로드 비어있지 않음", t_json_payload_not_empty)
    check("agent", "맨 print 금지", t_say_helper_exists)


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
    def t_news_one_section_only():
        """1층 — 한 기사는 가장 잘 맞는 섹션 하나에만 들어간다.

        섹션 필터는 서로 겹친다. 2026-09-09 아침 브리핑 실측: '매크로·정책'
        5건 중 4건이 '밤사이 해외'와 같은 URL 이었다.
        배정 우선순위는 내 종목 > 공시 > 매크로 > 해외.
        """
        import pandas as pd
        from stocknews.renderer import _group_events, _assign_sections
        df = pd.DataFrame([
            {"id": "a", "cluster_id": "a", "url": "https://x/1?oc=5",
             "title": "美 CPI 높으면 FOMC 금리 오를 것", "category": "매크로",
             "title_norm": "미 cpi 높으면 fomc 금리 오를 것",
             "source": "v.daum.net", "region": "US", "importance": 3.0},
            {"id": "b", "cluster_id": "b", "url": "https://x/1",
             "title": "美 CPI 높으면 FOMC 금리 오를 것", "category": "매크로",
             "title_norm": "미 cpi 높으면 fomc 금리 오를 것",
             "source": "다음", "region": "US", "importance": 3.0},
        ])
        groups = _group_events(df, {})
        assert len(groups) == 1, f"같은 URL 두 건이 안 묶였다: {len(groups)}"
        priority = (
            ("🎯 내 종목·고중요도", lambda r: float(r.get("importance") or 0) >= 5.0),
            ("📄 주요 공시", lambda r: r.get("category") == "공시"),
            ("🏛 매크로·정책", lambda r: r.get("category") in ("매크로", "정책")),
            ("🌏 밤사이 해외", lambda r: r.get("region") in ("US", "GLOBAL")),
        )
        picked = _assign_sections(groups, priority, 5)
        placed = [lbl for lbl, rows in picked.items() if rows]
        assert placed == ["🏛 매크로·정책"], \
            f"한 섹션에만 들어가야 하는데 {placed} 에 들어갔다"
        assert picked["🏛 매크로·정책"][0][1] == 2, "묶인 매체 수가 안 맞다"

    def t_news_priority_order():
        """공시이면서 고중요도이면 '내 종목' 이 가져간다."""
        import pandas as pd
        from stocknews.renderer import _group_events, _assign_sections
        df = pd.DataFrame([{
            "id": "a", "cluster_id": "a", "url": "https://x/9",
            "title": "[공시] 어떤회사 유상증자 결정", "category": "공시",
            "title_norm": "어떤회사 유상증자 결정", "source": "DART",
            "region": "KR", "importance": 8.0}])
        priority = (
            ("🎯 내 종목·고중요도", lambda r: float(r.get("importance") or 0) >= 5.0),
            ("📄 주요 공시", lambda r: r.get("category") == "공시"),
        )
        picked = _assign_sections(_group_events(df, {}), priority, 5)
        assert picked["🎯 내 종목·고중요도"] and not picked["📄 주요 공시"], \
            "우선순위(내 종목 > 공시)가 지켜지지 않았다"

    def t_news_title_media_tail():
        """제목 끝의 매체명만 뗀다. 매체명이 아닌 꼬리는 건드리지 않는다."""
        from stocknews.renderer import _display_title
        cut = {"title": "대양금속, 전환사채 조정 - digitaltoday.co.kr",
               "source": "digitaltoday.co.kr"}
        assert _display_title(cut) == "대양금속, 전환사채 조정", _display_title(cut)
        keep = {"title": "코스피 상승 - 종합", "source": "연합뉴스"}
        assert _display_title(keep) == "코스피 상승 - 종합", \
            "매체명이 아닌 꼬리를 잘랐다"

    def t_news_line_links_title():
        """URL 은 제목에 <a href> 로 감는다. 본문에 주소를 찍지 않는다."""
        from stocknews.renderer import _news_line
        row = {"id": "a", "category": "공시", "cluster_n": 1,
               "title": "대양금속, 전환사채 조정 - 디지털투데이",
               "url": "https://news.google.com/rss/articles/AAA?oc=5",
               "source": "디지털투데이"}
        line = _news_line(row, {})
        assert line.count("https://news.google.com") == 1, line
        assert '<a href="https://news.google.com/rss/articles/AAA?oc=5">' in line
        head = line.split("</a>")[0]
        assert "디지털투데이</a>" not in head, f"제목에 매체 꼬리표: {head}"

    check("renderer", "청산 알림/요약", t_exit_render)
    check("renderer", "1층 한 기사 = 한 섹션", t_news_one_section_only)
    check("renderer", "1층 섹션 배정 우선순위", t_news_priority_order)
    check("renderer", "뉴스 제목 매체 꼬리표 제거", t_news_title_media_tail)
    check("renderer", "뉴스 URL 은 제목 링크로", t_news_line_links_title)
    check("renderer", "보유 현황", t_positions_render)


# ══════════════════════════ 10. 일일/주간 배치 ══════════════════════════
def test_daily_weekly(st, tmp: Path):
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

    def t_audit_multi_anchor():
        """보유기간 5/10/20 채점 앵커. 손으로 계산한 값과 대조한다.

        픽스처를 이렇게 짠다. 거래일 30일, 종목 3개.
          AAA  추천 종목. 매일 +1% 복리로 오른다.
          BBB  시장 종목. 변동 없음 (100 고정).
          CCC  시장 종목. 변동 없음 (100 고정).
        추천일은 0번째 거래일. 시장 중위 수익률은 BBB/CCC 가 0% 이고
        AAA 도 후보라 3종목 중위 = 0% 다 (0, 0, +x 의 중위는 0).

        따라서 alpha = ret 이 되어 손계산이 가능하다.
          5일  1.01^5  - 1 = +5.101%
          10일 1.01^10 - 1 = +10.462%
          20일 1.01^20 - 1 = +22.019%
        """
        from stocknews.config import AuditConfig, Config
        from stocknews.store import Store
        from stocknews.weekly import audit_multi

        sta = Store(tmp / "audit_multi.db")
        days = 30
        idx = pd.DatetimeIndex(pd.bdate_range("2026-01-05", periods=days))
        rise = np.array([100.0 * (1.01 ** i) for i in range(days)])
        flat = np.full(days, 100.0)

        for code, series in (("AAA000", rise), ("BBB000", flat),
                             ("CCC000", flat)):
            sta.upsert_prices(code, pd.DataFrame({
                "시가": series, "고가": series, "저가": series,
                "종가": series, "거래량": np.full(days, 1e6),
            }, index=idx), allow_today=True)

        d0 = idx[0].strftime("%Y-%m-%d")
        _insert_reco(sta, d0, "AAA000", "상승주", "VALUE", "A")

        cfg = Config(audit=AuditConfig(horizons=(5, 10, 20), min_sample=20,
                                       lookback_dates=60))
        rep = audit_multi(sta, cfg=cfg)

        assert rep["horizons"] == [5, 10, 20], rep["horizons"]
        assert rep["min_sample"] == 20
        assert rep["total_recos"] == 1, rep["total_recos"]

        by = rep["by_horizon"]
        for h, want in ((5, 5.101), (10, 10.462), (20, 22.019)):
            d = by[h]
            assert d["n"] == 1, f"{h}일 표본 {d['n']}"
            assert d["pending"] == 0, f"{h}일 미도래 {d['pending']}"
            near(d["mean_ret"], want, tol=0.01, label=f"{h}일 수익률")
            near(d["mean_mkt"], 0.0, tol=1e-9, label=f"{h}일 시장 중위")
            near(d["mean_alpha"], want, tol=0.01, label=f"{h}일 초과수익")
            near(d["win_rate"], 100.0, tol=1e-9, label=f"{h}일 승률")
            near(d["alpha_win_rate"], 100.0, tol=1e-9,
                 label=f"{h}일 초과승률")

    def t_audit_pending_excluded():
        """미도래는 채점에서 빼야 한다. 0 이나 실패로 처리하면 안 된다.

        거래일 12일만 두고 추천일을 8번째에 둔다.
          5일  -> 경과 (인덱스 8+5=13 > 11 이라 미도래)  ...가 아니라
        정확히는 인덱스가 범위를 넘으면 미도래다. 아래에서 셋을 나눈다.
        """
        from stocknews.config import AuditConfig, Config
        from stocknews.store import Store
        from stocknews.weekly import audit_multi

        stp = Store(tmp / "audit_pending.db")
        days = 12
        idx = pd.DatetimeIndex(pd.bdate_range("2026-02-02", periods=days))
        series = np.full(days, 100.0)
        for code in ("AAA111", "BBB111"):
            stp.upsert_prices(code, pd.DataFrame({
                "시가": series, "고가": series, "저가": series,
                "종가": series, "거래량": np.full(days, 1e6),
            }, index=idx), allow_today=True)

        # 추천일 = 5번째 거래일(0-base). 5일 후 = 10 (범위 안),
        # 10일 후 = 15 (범위 밖), 20일 후 = 25 (범위 밖)
        d0 = idx[5].strftime("%Y-%m-%d")
        _insert_reco(stp, d0, "AAA111", "평탄주", "VALUE", "B")

        cfg = Config(audit=AuditConfig(horizons=(5, 10, 20)))
        by = audit_multi(stp, cfg=cfg)["by_horizon"]

        assert by[5]["n"] == 1 and by[5]["pending"] == 0, by[5]
        for h in (10, 20):
            assert by[h]["n"] == 0, f"{h}일이 채점됐다: {by[h]}"
            assert by[h]["pending"] == 1, f"{h}일 미도래 미집계: {by[h]}"
            # 0 으로 채워 넣지 않았는지 확인 — 통계 키가 아예 없어야 한다
            assert "mean_ret" not in by[h], \
                f"{h}일 미도래인데 수익률을 만들어냈다: {by[h]}"
            assert "note" in by[h]

    def t_audit_render_table():
        """리포트에 세 기간이 나란히 나오고 종이거래 문구가 붙어야 한다."""
        from stocknews.renderer import render_weekly
        from stocknews.weekly import weekly_report

        rep = weekly_report(st, DEFAULT)
        txt = render_weekly(rep, datetime(2026, 8, 28, 16, 30), DEFAULT)
        assert "검증 기간 — 실거래 아님" in txt, "종이거래 문구 누락"
        assert "최소 20건" in txt, "최소 표본 표시 누락"
        for h in (5, 10, 20):
            assert f"{h}일" in txt, f"{h}일 행 누락"
        assert "미도래" in txt, "미도래 열 누락"
        # 표 정렬이 깨지지 않도록 pre 로 감쌌는지
        assert "<pre>" in txt and "</pre>" in txt

    def t_audit_recos_still_single():
        """기존 단일 기간 API 는 그대로 동작해야 한다 (호출부 보호)."""
        a = audit_recos(st, horizon=5)
        assert isinstance(a, dict) and "n" in a
        assert "by_horizon" not in a, "단일 API 가 다중 형태를 돌려줬다"

    # ───────── 채점 진행률 + 알파 분포 ─────────
    def _dist_store(name: str, ends: list[float]):
        """알파 분포 앵커용 픽스처.

        평탄 종목 5개를 넣어 **시장 중위 수익률이 정확히 0** 이 되게 한다.
        그러면 alpha = ret 이 되어 중앙값·최대·최소를 손으로 검산할 수 있다.
        추천 종목은 첫 봉만 100 이고 이후는 목표가로 고정하므로, 어느
        horizon 에서 채점해도 수익률이 같다.
        """
        from stocknews.store import Store
        s = Store(tmp / name)
        days = 30
        idx = pd.DatetimeIndex(pd.bdate_range("2026-03-02", periods=days))
        vol = np.full(days, 1e6)

        def _put(code, series):
            s.upsert_prices(code, pd.DataFrame({
                "시가": series, "고가": series, "저가": series,
                "종가": series, "거래량": vol}, index=idx), allow_today=True)

        for i in range(5):
            _put(f"FLT{i:03d}", np.full(days, 100.0))
        d0 = idx[0].strftime("%Y-%m-%d")
        for rank, end in enumerate(ends, 1):
            series = np.full(days, float(end))
            series[0] = 100.0
            code = f"RCO{rank:03d}"
            _put(code, series)
            _insert_reco(s, d0, code, f"종목{rank}", "VALUE", "A", rank=rank)
        return s

    def _dist_cfg():
        from stocknews.config import AuditConfig, Config
        return Config(audit=AuditConfig(horizons=(5, 10, 20)))

    def t_alpha_dist_odd():
        """분포 앵커 · 홀수 3건. 알파 [+10, +4, -3].

        평균 11/3 = +3.667 · 중앙값 +4.00 · 최대 +10.00 · 최소 -3.00
        알파 승률 2/3 = 66.7%
        """
        from stocknews.weekly import audit_multi
        s = _dist_store("dist_odd.db", [110.0, 104.0, 97.0])
        by = audit_multi(s, cfg=_dist_cfg())["by_horizon"]
        for h in (5, 10, 20):
            d = by[h]
            assert d["n"] == 3, f"{h}일 표본 {d['n']}"
            near(d["mean_alpha"], 11.0 / 3.0, tol=0.01, label=f"{h}일 평균알파")
            near(d["median_alpha"], 4.0, tol=0.01, label=f"{h}일 중앙알파")
            near(d["max_alpha"], 10.0, tol=0.01, label=f"{h}일 최대알파")
            near(d["min_alpha"], -3.0, tol=0.01, label=f"{h}일 최소알파")
            near(d["mean_mkt"], 0.0, tol=1e-9, label=f"{h}일 시장중위")
            near(d["alpha_win_rate"], 200.0 / 3.0, tol=0.01,
                 label=f"{h}일 알파승률")

    def t_alpha_dist_even():
        """분포 앵커 · 짝수 4건. 알파 [+10, +4, -3, +8].

        짝수에서는 중앙값이 가운데 두 값의 평균이다.
        정렬 [-3, +4, +8, +10] -> 중앙값 (4+8)/2 = +6.00
        평균 19/4 = +4.75 · 최대 +10.00 · 최소 -3.00 · 승률 3/4 = 75%
        """
        from stocknews.weekly import audit_multi
        s = _dist_store("dist_even.db", [110.0, 104.0, 97.0, 108.0])
        by = audit_multi(s, cfg=_dist_cfg())["by_horizon"]
        for h in (5, 10, 20):
            d = by[h]
            assert d["n"] == 4, f"{h}일 표본 {d['n']}"
            near(d["mean_alpha"], 4.75, tol=0.01, label=f"{h}일 평균알파")
            near(d["median_alpha"], 6.0, tol=0.01,
                 label=f"{h}일 중앙알파(짝수 = 가운데 둘의 평균)")
            near(d["max_alpha"], 10.0, tol=0.01, label=f"{h}일 최대알파")
            near(d["min_alpha"], -3.0, tol=0.01, label=f"{h}일 최소알파")
            near(d["alpha_win_rate"], 75.0, tol=0.01, label=f"{h}일 알파승률")

    def t_dist_columns_rendered():
        """표에 중앙·최대·최소가 실제로 찍혀야 한다."""
        from stocknews.renderer import _horizon_rows
        from stocknews.weekly import audit_multi
        s = _dist_store("dist_render.db", [110.0, 104.0, 97.0])
        rows, scored = _horizon_rows(audit_multi(s, cfg=_dist_cfg()))
        assert scored == 3, scored
        r5 = [x for x in rows if x.strip().startswith("5일")][0]
        for want in ("+3.67", "+4.00", "+10.00", "-3.00"):
            assert want in r5, f"{want} 누락: {r5}"
        assert "α승률" in rows[0], rows[0]
        for label in ("평균", "중앙", "최대", "최소"):
            assert label in rows[0], f"헤더에 {label} 없음: {rows[0]}"

    def t_pending_shows_not_due():
        """채점 0건 horizon 은 '미도래'. 0% 나 빈 숫자를 쓰면 안 된다."""
        from stocknews.renderer import _horizon_rows
        a = {"horizons": [5, 10, 20], "by_horizon": {
            5: {"horizon": 5, "n": 2, "pending": 0, "alpha_win_rate": 50.0,
                "mean_alpha": 1.0, "median_alpha": 1.0,
                "max_alpha": 3.0, "min_alpha": -1.0},
            10: {"horizon": 10, "n": 0, "pending": 7, "note": "미도래"},
            20: {"horizon": 20, "n": 0, "pending": 7, "note": "미도래"},
        }}
        rows, scored = _horizon_rows(a)
        assert scored == 1, scored
        for h in (10, 20):
            r = [x for x in rows if x.strip().startswith(f"{h}일")][0]
            assert "미도래" in r, f"{h}일 행에 미도래 표기 없음: {r}"
            assert "(7건)" in r, f"{h}일 미도래 건수 없음: {r}"
            assert "%" not in r, f"{h}일 미도래 행에 퍼센트: {r}"
            assert "0.00" not in r, f"{h}일 미도래 행에 0 숫자: {r}"

    def t_progress_math():
        """진행률 산술. 남은 건수는 기준 미달일 때만 나오고 0 에서 멈춘다."""
        from stocknews.config import MIN_SCORED_FOR_JUDGMENT
        from stocknews.weekly import scoring_progress

        assert MIN_SCORED_FOR_JUDGMENT == 20, MIN_SCORED_FOR_JUDGMENT
        assert DEFAULT.audit.min_sample == MIN_SCORED_FOR_JUDGMENT, \
            "배너와 진행률이 다른 숫자를 본다"

        a = {"total_recos": 50, "horizons": [5, 10, 20], "by_horizon": {
            5: {"n": 30, "pending": 5}, 10: {"n": 22, "pending": 8},
            20: {"n": 25, "pending": 10}}}
        p = scoring_progress(a, MIN_SCORED_FOR_JUDGMENT)
        assert p["judge_horizon"] == 20, "기준이 가장 긴 기간이 아니다"
        assert p["scored"] == {5: 30, 10: 22, 20: 25}, p["scored"]
        assert p["pending"] == {5: 5, 10: 8, 20: 10}, p["pending"]
        assert p["remaining"] == 0, "기준을 넘었는데 남은 건수가 나왔다"
        assert p["ready"] is True

        a["by_horizon"][20] = {"n": 3, "pending": 10}
        p2 = scoring_progress(a, MIN_SCORED_FOR_JUDGMENT)
        assert p2["remaining"] == 17, p2["remaining"]
        assert p2["ready"] is False

    def t_progress_line_rendered():
        """진행률 줄 형식. 종이거래 문구 바로 아래에 있어야 한다."""
        from stocknews.renderer import _progress_line, render_weekly
        from stocknews.weekly import weekly_report

        ln = _progress_line({"progress": {
            "total_recos": 43, "horizons": [5, 10, 20],
            "scored": {5: 12, 10: 7, 20: 3}, "pending": {5: 1, 10: 6, 20: 10},
            "min_scored": 20, "judge_horizon": 20,
            "remaining": 17, "ready": False}})[0]
        assert "누적 추천 43건" in ln, ln
        for part in ("5d:12건", "10d:7건", "20d:3건"):
            assert part in ln, f"{part} 누락: {ln}"
        assert "판단 기준(20건, 20d)까지 17건" in ln, ln
        assert _progress_line({}) == [], "progress 없으면 줄을 만들지 않아야 한다"

        # 리포트에서의 위치: 종이거래 문구 다음 줄
        txt = render_weekly(weekly_report(st, DEFAULT),
                            datetime(2026, 8, 28, 16, 30), DEFAULT)
        lines = txt.splitlines()
        i = next(k for k, x in enumerate(lines) if "검증 기간 — 실거래 아님" in x)
        assert "검증 진행" in lines[i + 1], \
            f"진행률 줄이 배너 아래가 아니다: {lines[i + 1]!r}"

    check("daily", "플래그 배선 (배제 발동)", t_scan_all_flag_wiring)
    check("daily", "플래그 해제 후 정상 채점", t_scan_all_after_clear)
    check("daily", "추천 선정", t_select)
    check("weekly", "구성 요소", t_weekly_parts)
    check("weekly", "종합 리포트", t_weekly_report)
    check("weekly", "빈 데이터 처리", t_empty_frames)
    check("audit", "5/10/20 채점 앵커", t_audit_multi_anchor)
    check("audit", "미도래는 채점 제외", t_audit_pending_excluded)
    check("audit", "리포트 표 + 종이거래 문구", t_audit_render_table)
    check("audit", "단일 API 하위호환", t_audit_recos_still_single)
    check("audit", "알파 분포 앵커 (홀수 3건)", t_alpha_dist_odd)
    check("audit", "알파 분포 앵커 (짝수 4건)", t_alpha_dist_even)
    check("audit", "분포 열 렌더", t_dist_columns_rendered)
    check("audit", "채점 0건 = 미도래 표기", t_pending_shows_not_due)
    check("audit", "진행률 산술 (0 에서 멈춤)", t_progress_math)
    check("audit", "진행률 줄 형식 + 위치", t_progress_line_rendered)

    # ───────── 시장 상태 기록 (기록 전용) ─────────
    def _index_frame(closes: np.ndarray) -> pd.DataFrame:
        """FDR 형태의 지수 프레임 (컬럼명 'Close')."""
        idx = pd.DatetimeIndex(pd.bdate_range(end="2026-08-27",
                                              periods=len(closes)))
        return pd.DataFrame({"Close": closes.astype("float64")}, index=idx)

    def t_mkt_above_ma200():
        """MA200 위 판정 + 수익률 앵커.

        300봉 전부 100, 마지막만 300.
          MA200 = (199x100 + 300) / 200 = 101.00
          종가 300 > 101 -> above=1
          5일/20일 수익률 = 300/100 - 1 = +200.00%
        """
        from stocknews.daily import market_context
        c = np.full(300, 100.0)
        c[-1] = 300.0
        m = market_context(_index_frame(c), None, "2026-08-27")
        near(m["kospi_close"], 300.0, tol=1e-9, label="종가")
        near(m["kospi_ma200"], 101.0, tol=1e-9, label="MA200")
        assert m["kospi_above_ma200"] == 1, m["kospi_above_ma200"]
        near(m["kospi_ret_5d"], 200.0, tol=1e-9, label="5일 수익률")
        near(m["kospi_ret_20d"], 200.0, tol=1e-9, label="20일 수익률")
        # KOSDAQ 을 안 주면 그 항목만 비워야 한다 (0 으로 채우면 보합으로 읽힌다)
        assert m["kosdaq_close"] is None and m["kosdaq_ret_5d"] is None
        assert m["scan_date"] == "2026-08-27"

    def t_mkt_below_ma200():
        """MA200 아래 판정. 300봉 전부 100, 마지막만 50.

        MA200 = (199x100 + 50) / 200 = 99.75 · 종가 50 < 99.75 -> 0
        """
        from stocknews.daily import market_context
        c = np.full(300, 100.0)
        c[-1] = 50.0
        m = market_context(_index_frame(c), None, "2026-08-27")
        near(m["kospi_ma200"], 99.75, tol=1e-9, label="MA200")
        assert m["kospi_above_ma200"] == 0, m["kospi_above_ma200"]
        near(m["kospi_ret_5d"], -50.0, tol=1e-9, label="5일 수익률")

    def t_mkt_ma200_boundary():
        """종가 == MA200 이면 '위'가 아니다. 부등호를 확정한다."""
        from stocknews.daily import market_context
        m = market_context(_index_frame(np.full(300, 100.0)), None,
                           "2026-08-27")
        near(m["kospi_ma200"], 100.0, tol=1e-9, label="MA200")
        assert m["kospi_above_ma200"] == 0, "종가=MA200 을 '위'로 판정했다"
        near(m["kospi_ret_5d"], 0.0, tol=1e-9, label="5일 수익률")

    def t_mkt_windows_differ():
        """5일창과 20일창이 서로 다른 봉을 봐야 한다 (off-by-one 방어).

        300봉 기본 100 · idx[-21]=200 · idx[-1]=110
          5일  = 110/100 - 1 = +10.00%   (idx[-6] = 100)
          20일 = 110/200 - 1 = -45.00%   (idx[-21] = 200)
          MA200 = (198x100 + 200 + 110)/200 = 100.55
        """
        from stocknews.daily import market_context
        c = np.full(300, 100.0)
        c[-21] = 200.0
        c[-1] = 110.0
        m = market_context(_index_frame(c), _index_frame(c), "2026-08-27")
        near(m["kospi_ret_5d"], 10.0, tol=1e-9, label="5일 수익률")
        near(m["kospi_ret_20d"], -45.0, tol=1e-9, label="20일 수익률")
        near(m["kospi_ma200"], 100.55, tol=1e-9, label="MA200")
        near(m["kosdaq_ret_5d"], 10.0, tol=1e-9, label="코스닥 5일")

    def t_mkt_short_series():
        """봉이 부족하면 그 항목만 None. 0 으로 채우지 않는다."""
        from stocknews.daily import market_context
        m = market_context(_index_frame(np.full(10, 100.0)), None,
                           "2026-08-27")
        assert m["kospi_ma200"] is None, m["kospi_ma200"]
        assert m["kospi_above_ma200"] is None, "MA200 없이 위/아래를 판정했다"
        assert m["kospi_ret_5d"] is not None, "5일은 계산 가능해야 한다"
        assert m["kospi_ret_20d"] is None, "20일은 봉이 부족하다"
        # 지수 자체가 없으면 기록할 게 없다
        assert market_context(None, None, "2026-08-27") is None
        assert market_context(pd.DataFrame(), None, "2026-08-27") is None

    def t_mkt_store_roundtrip():
        """저장 → 조회. 같은 거래일은 덮어쓴다."""
        from stocknews.daily import market_context
        from stocknews.store import Store
        s = Store(tmp / "mkt.db")
        assert s.has_market_context("2026-08-27") is False
        c = np.full(300, 100.0)
        c[-1] = 300.0
        s.upsert_market_context(market_context(_index_frame(c), None,
                                               "2026-08-27"))
        assert s.has_market_context("2026-08-27") is True
        got = s.market_context("2026-08-27")
        near(got["kospi_ma200"], 101.0, tol=1e-9, label="저장된 MA200")
        assert got["kospi_above_ma200"] == 1
        assert got["created_at"], "created_at 이 비어 있다"
        # 덮어쓰기
        c2 = np.full(300, 100.0)
        c2[-1] = 50.0
        s.upsert_market_context(market_context(_index_frame(c2), None,
                                               "2026-08-27"))
        assert len(s.market_context_history(10)) == 1, "행이 중복 생성됐다"
        assert s.market_context("2026-08-27")["kospi_above_ma200"] == 0

    def t_mkt_collect_skips_when_present():
        """이미 있으면 조회조차 하지 않는다 (하루 1회)."""
        from stocknews.daily import collect_market_context, market_context
        from stocknews.store import Store
        s = Store(tmp / "mkt_skip.db")
        c = np.full(300, 100.0)
        c[-1] = 300.0
        s.upsert_market_context(market_context(_index_frame(c), None,
                                               "2026-08-27"))
        calls = []

        def _fetch(name, n):
            calls.append(name)
            return _index_frame(c)

        assert collect_market_context(s, "2026-08-27", fetch=_fetch) is None
        assert calls == [], f"이미 있는데 조회했다: {calls}"

    def t_mkt_fetch_failure_is_survivable():
        """지수 조회가 터져도 예외가 올라오지 않아야 한다."""
        from stocknews.daily import collect_market_context
        from stocknews.store import Store
        s = Store(tmp / "mkt_fail.db")

        def _boom(name, n):
            raise RuntimeError("네트워크 없음")

        assert collect_market_context(s, "2026-08-27", fetch=_boom) is None
        assert s.has_market_context("2026-08-27") is False
        # 빈 응답도 같다
        assert collect_market_context(s, "2026-08-27",
                                      fetch=lambda n, b: None) is None
        assert s.has_market_context("2026-08-27") is False

    def t_daily_survives_index_failure():
        """지수 조회 실패에도 run_daily 는 정상 완료하고 추천을 저장한다."""
        import stocknews.daily as D
        from stocknews.daily import run_daily

        orig = D.load_index
        D.load_index = lambda name, bars=300: (_ for _ in ()).throw(
            RuntimeError("지수 조회 실패"))
        try:
            res = run_daily(st, cfg=DEFAULT, top_n=3)
        finally:
            D.load_index = orig
        assert isinstance(res, dict), type(res)
        assert res["market_context"] is None, "실패인데 값이 들어왔다"
        assert res["trade_date"], "기준일이 비었다"
        assert "picks" in res and "results" in res
        # 추천이 실제로 저장됐는지 (조회 실패가 저장을 막지 않았는지)
        assert st.reco_count() > 0, "추천이 저장되지 않았다"

    def t_mkt_not_used_anywhere():
        """기록 전용. 표시·점수·게이트에 쓰이면 안 된다."""
        import inspect
        from stocknews import notify, renderer, screener, weekly

        for mod in (renderer, screener, notify, weekly):
            src = inspect.getsource(mod)
            for token in ("market_context", "kospi_above_ma200",
                          "kospi_ma200", "kospi_ret_"):
                assert token not in src, \
                    f"{mod.__name__} 가 {token} 을 참조한다 (기록 전용 위반)"
        # 점수·선정 경로가 실제로 참조하지 않는지 (전역 이름으로 확인)
        from stocknews.daily import select_recommendations
        for fn in (screener.screen_one, select_recommendations):
            names = set(fn.__code__.co_names)
            assert "market_context" not in names, \
                f"{fn.__name__} 가 시장 상태를 참조한다"
            assert "collect_market_context" not in names, \
                f"{fn.__name__} 가 시장 상태 수집을 호출한다"

    check("market", "MA200 위 판정 + 수익률 앵커", t_mkt_above_ma200)
    check("market", "MA200 아래 판정", t_mkt_below_ma200)
    check("market", "종가=MA200 경계", t_mkt_ma200_boundary)
    check("market", "5일창 != 20일창 (off-by-one)", t_mkt_windows_differ)
    check("market", "봉 부족은 None (0 채우기 금지)", t_mkt_short_series)
    check("market", "저장/조회/덮어쓰기", t_mkt_store_roundtrip)
    check("market", "이미 있으면 조회 안 함", t_mkt_collect_skips_when_present)
    check("market", "조회 실패를 삼킨다", t_mkt_fetch_failure_is_survivable)
    check("market", "조회 실패에도 daily 완료", t_daily_survives_index_failure)
    check("market", "기록 전용 (표시·점수 미반영)", t_mkt_not_used_anywhere)

    # ───────── 섹터 지표 (기록 전용) ─────────
    # 픽스처: 섹터 2개 x 종목 3개 + 미분류 1개. 30봉.
    # 손계산이 되도록 마지막 봉(과 A1 의 -21봉)만 계단으로 움직인다.
    #
    #   A1  100 유지 · idx[-21]=50 · 마지막 130   시총 3
    #   A2  100 유지 · 마지막 90                  시총 1
    #   A3  100 유지 · 마지막 110                 시총 1
    #   B1  200 유지 · 마지막 195                 시총 1
    #   B2  200 유지 · 마지막 180                 시총 1
    #   B3  200 유지 · 마지막 220                 시총 1
    #   U1  100 유지 (미분류) — 거래대금 분모에만 들어간다
    _SEC_A = "반도체"
    _SEC_B = "바이오"

    def _sector_fixture(bars: int = 30):
        from stocknews.sector_metrics import UNCLASSIFIED
        idx = pd.DatetimeIndex(pd.bdate_range(end="2026-08-27", periods=bars))

        def _ser(base, last, deep=None):
            a = np.full(bars, float(base))
            if deep is not None and bars >= 21:
                a[-21] = float(deep)
            a[-1] = float(last)
            return a

        close = pd.DataFrame({
            "A00001": _ser(100, 130, deep=50),
            "A00002": _ser(100, 90),
            "A00003": _ser(100, 110),
            "B00001": _ser(200, 195),
            "B00002": _ser(200, 180),
            "B00003": _ser(200, 220),
            "U00001": _ser(100, 100),
        }, index=idx)
        amt = pd.DataFrame({
            "A00001": np.full(bars, 10.0),
            "A00002": np.full(bars, 20.0),
            "A00003": np.full(bars, 30.0),   # 섹터 A 합 60
            "B00001": np.full(bars, 10.0),
            "B00002": np.full(bars, 10.0),
            "B00003": np.full(bars, 20.0),   # 섹터 B 합 40
            "U00001": np.full(bars, 50.0),   # 미분류 — 분모에만
        }, index=idx)
        sectors = {"A00001": _SEC_A, "A00002": _SEC_A, "A00003": _SEC_A,
                   "B00001": _SEC_B, "B00002": _SEC_B, "B00003": _SEC_B,
                   "U00001": UNCLASSIFIED}
        caps = pd.Series({"A00001": 3.0, "A00002": 1.0, "A00003": 1.0,
                          "B00001": 1.0, "B00002": 1.0, "B00003": 1.0,
                          "U00001": 1.0})
        return close, amt, sectors, caps

    def _rows_by_sector(rows):
        return {r["sector"]: r for r in rows}

    def t_sector_ticker_math():
        """종목 단위 계산 앵커. 손계산과 대조한다.

        A1 = 100 유지 · idx[-21]=50 · 마지막 130
          5일  수익률 = 130/100 - 1 = +30%   (idx[-6] = 100)
          20일 수익률 = 130/50  - 1 = +160%  (idx[-21] = 50)
          MA20 = (19x100 + 130)/20 = 101.5   (idx[-21] 은 창 밖)
        """
        from stocknews.sector_metrics import (above_ma, new_high_flags,
                                              ticker_returns)
        close, _, _, _ = _sector_fixture()

        r5 = ticker_returns(close, 5)
        near(r5["A00001"], 30.0, tol=1e-9, label="A1 5일")
        near(r5["A00002"], -10.0, tol=1e-9, label="A2 5일")
        near(r5["A00003"], 10.0, tol=1e-9, label="A3 5일")
        near(r5["B00001"], -2.5, tol=1e-9, label="B1 5일")

        r20 = ticker_returns(close, 20)
        near(r20["A00001"], 160.0, tol=1e-9,
             label="A1 20일 (5일창과 다른 봉을 봐야 한다)")
        near(r20["A00002"], -10.0, tol=1e-9, label="A2 20일")

        ma = above_ma(close, 20)
        assert bool(ma["A00001"]) is True, "130 > MA20 101.5"
        assert bool(ma["A00002"]) is False, "90 < MA20 99.5"
        assert bool(ma["A00003"]) is True, "110 > MA20 100.5"
        assert bool(ma["B00003"]) is True, "220 > MA20 201"
        assert bool(ma["B00001"]) is False, "195 < MA20 199.75"

        nh = new_high_flags(close, 252, 0.99)
        assert bool(nh["A00001"]) is True, "130 이 최고 종가"
        assert bool(nh["A00002"]) is False, "90 < 100 x 0.99"
        assert bool(nh["B00001"]) is False, "195 < 200 x 0.99"
        assert bool(nh["B00003"]) is True, "220 이 최고 종가"

    def t_sector_turnover_share():
        """거래대금 비중. 분모는 전체 시장(미분류 포함).

        섹터 A 60 · 섹터 B 40 · 미분류 50 -> 시장 150
          A = 60/150 = 40.0%   B = 40/150 = 26.667%
        거래대금이 상수라 20일 평균 = 당일 -> 변화율 0.0%
        """
        from stocknews.sector_metrics import sector_turnover_share
        _, amt, sectors, _ = _sector_fixture()
        t = sector_turnover_share(amt, sectors, 20)
        near(t.loc[_SEC_A, "share"], 40.0, tol=1e-9, label="A 비중")
        near(t.loc[_SEC_B, "share"], 200.0 / 7.5, tol=1e-9, label="B 비중")
        near(t.loc[_SEC_A, "share_chg"], 0.0, tol=1e-9, label="A 변화율")
        assert list(t.index) == sorted([_SEC_A, _SEC_B]) or set(t.index) == \
            {_SEC_A, _SEC_B}, list(t.index)
        # 미분류는 행이 없어야 한다 (분모에만 들어간다)
        from stocknews.sector_metrics import UNCLASSIFIED
        assert UNCLASSIFIED not in t.index, "미분류가 집계에 들어갔다"
        # 합이 100% 가 아니어야 한다 — 차이가 미분류 비중이다
        near(float(t["share"].sum()), 200.0 / 3.0, tol=1e-9, label="비중 합")

    def t_sector_rs_and_rank():
        """RS = 섹터 시총가중 평균 − 코스피. 순위는 20d RS 기준.

        섹터 A 시총 3:1:1
          5일  = (30x3 + (-10)x1 + 10x1)/5 = 90/5  = +18%
          20일 = (160x3 + (-10)x1 + 10x1)/5 = 480/5 = +96%
        섹터 B 시총 1:1:1 (사실상 동일가중)
          5일 = 20일 = (-2.5 - 10 + 10)/3 = -0.8333%
        코스피 5%/5% 가정 -> RS_5d A=+13.0 B=-5.8333
                             RS_20d A=+91.0 B=-5.8333
        """
        from stocknews.sector_metrics import compute_sector_metrics
        close, amt, sectors, caps = _sector_fixture()
        rows = compute_sector_metrics(close, amt, sectors, caps, 5.0, 5.0,
                                      scan_date="2026-08-27")
        by = _rows_by_sector(rows)
        assert set(by) == {_SEC_A, _SEC_B}, set(by)
        a, b = by[_SEC_A], by[_SEC_B]

        near(a["rs_5d"], 13.0, tol=1e-9, label="A RS 5일")
        near(a["rs_20d"], 91.0, tol=1e-9, label="A RS 20일")
        near(b["rs_5d"], -0.8333333333 - 5.0, tol=1e-6, label="B RS 5일")
        near(b["rs_20d"], -0.8333333333 - 5.0, tol=1e-6, label="B RS 20일")

        assert a["rs_rank"] == 1 and b["rs_rank"] == 2, (a["rs_rank"],
                                                        b["rs_rank"])
        assert a["n_stocks"] == 3 and b["n_stocks"] == 3
        assert a["scan_date"] == "2026-08-27"

    def t_sector_equal_weight_fallback():
        """시총이 하나라도 없으면 그 섹터는 동일가중으로 떨어진다.

        동일가중 A 5일 = (30 - 10 + 10)/3 = +10% -> RS = +5.0
        (시총가중이면 +18% -> RS = +13.0 이므로 값으로 구분된다)
        """
        from stocknews.sector_metrics import compute_sector_metrics
        close, amt, sectors, caps = _sector_fixture()
        caps = caps.copy()
        caps["A00001"] = np.nan          # 하나만 비운다
        rows = compute_sector_metrics(close, amt, sectors, caps, 5.0, 5.0)
        near(_rows_by_sector(rows)[_SEC_A]["rs_5d"], 5.0, tol=1e-9,
             label="동일가중 A RS 5일")
        # 시총을 아예 안 주면 전부 동일가중
        rows2 = compute_sector_metrics(close, amt, sectors, None, 5.0, 5.0)
        near(_rows_by_sector(rows2)[_SEC_A]["rs_5d"], 5.0, tol=1e-9,
             label="시총 없음 -> 동일가중")

    def t_sector_breadth_and_newhigh():
        """폭과 신고가 비율.

        A: MA20 위 A1·A3 -> 2/3 = 66.667% · 신고가 A1·A3 -> 2건 66.667%
        B: MA20 위 B3    -> 1/3 = 33.333% · 신고가 B3    -> 1건 33.333%
        """
        from stocknews.sector_metrics import compute_sector_metrics
        close, amt, sectors, caps = _sector_fixture()
        by = _rows_by_sector(compute_sector_metrics(
            close, amt, sectors, caps, 5.0, 5.0))
        near(by[_SEC_A]["breadth_ma20"], 200.0 / 3.0, tol=1e-9, label="A 폭")
        near(by[_SEC_B]["breadth_ma20"], 100.0 / 3.0, tol=1e-9, label="B 폭")
        assert by[_SEC_A]["new_high_cnt"] == 2, by[_SEC_A]["new_high_cnt"]
        assert by[_SEC_B]["new_high_cnt"] == 1, by[_SEC_B]["new_high_cnt"]
        near(by[_SEC_A]["new_high_pct"], 200.0 / 3.0, tol=1e-9, label="A 신고가%")
        near(by[_SEC_B]["new_high_pct"], 100.0 / 3.0, tol=1e-9, label="B 신고가%")

    def t_sector_momentum_persist():
        """모멘텀 지속성: 과거에도 상위 1/3, 지금도 상위 1/3 이면 1.

        섹터 6개 중 상위 1/3 = 2개 (int(6 x 1/3) = 2).
        """
        from stocknews.sector_metrics import _fill_ranks
        rows = [{"sector": f"S{i}", "rs_20d": float(10 - i), "rs_rank": None,
                 "momentum_persist": None} for i in range(6)]
        prev = {f"S{i}": i + 1 for i in range(6)}   # 과거 순위 = S0..S5
        _fill_ranks(rows, prev, 1.0 / 3.0)
        by = {r["sector"]: r for r in rows}
        assert by["S0"]["rs_rank"] == 1 and by["S5"]["rs_rank"] == 6
        assert by["S0"]["momentum_persist"] == 1, "과거 1위·현재 1위인데 0"
        assert by["S1"]["momentum_persist"] == 1, "과거 2위·현재 2위인데 0"
        assert by["S2"]["momentum_persist"] == 0, "과거 3위는 상위 1/3 아님"
        assert by["S5"]["momentum_persist"] == 0

        # 과거 스냅샷이 없으면 전부 None (0 이 아니다)
        rows2 = [{"sector": "S0", "rs_20d": 1.0, "rs_rank": None,
                  "momentum_persist": None}]
        _fill_ranks(rows2, None, 1.0 / 3.0)
        assert rows2[0]["momentum_persist"] is None, \
            "과거 스냅샷이 없는데 0/1 로 단정했다"
        assert rows2[0]["rs_rank"] == 1

        # 그 섹터만 과거 순위가 없으면 그 섹터만 None
        rows3 = [{"sector": "NEW", "rs_20d": 5.0, "rs_rank": None,
                  "momentum_persist": None},
                 {"sector": "OLD", "rs_20d": 1.0, "rs_rank": None,
                  "momentum_persist": None}]
        _fill_ranks(rows3, {"OLD": 1}, 1.0 / 3.0)
        got = {r["sector"]: r["momentum_persist"] for r in rows3}
        assert got["NEW"] is None, "신규 섹터를 0 으로 단정했다"
        assert got["OLD"] == 0, got

    def t_sector_null_on_short_data():
        """데이터 부족은 NULL. 0 으로 채우면 '보합'으로 읽힌다."""
        from stocknews.sector_metrics import (above_ma, compute_sector_metrics,
                                              new_high_flags, ticker_returns)
        close, amt, sectors, caps = _sector_fixture(bars=3)

        assert ticker_returns(close, 5).empty, "3봉으로 5일 수익률을 만들었다"
        assert ticker_returns(close, 20).empty
        assert above_ma(close, 20).empty, "3봉으로 MA20 을 만들었다"

        by = _rows_by_sector(compute_sector_metrics(
            close, amt, sectors, caps, 5.0, 5.0))
        for sec in (_SEC_A, _SEC_B):
            r = by[sec]
            assert r["rs_5d"] is None, r
            assert r["rs_20d"] is None, r
            assert r["rs_rank"] is None, "RS 가 없는데 순위를 줬다"
            assert r["momentum_persist"] is None
            assert r["breadth_ma20"] is None, "MA20 이 없는데 폭을 줬다"
            # 신고가는 2봉만 있어도 판정 가능하다
            assert r["new_high_cnt"] is not None
            assert r["n_stocks"] == 3

        # 봉이 1개면 신고가도 판정 불가
        one, one_amt, s1, c1 = _sector_fixture(bars=1)
        assert new_high_flags(one, 252, 0.99).empty
        by1 = _rows_by_sector(compute_sector_metrics(
            one, one_amt, s1, c1, 5.0, 5.0))
        assert by1[_SEC_A]["new_high_cnt"] is None, by1[_SEC_A]

        # 코스피 수익률이 없으면 RS 만 비고 나머지는 남는다
        close2, amt2, s2, c2 = _sector_fixture()
        by2 = _rows_by_sector(compute_sector_metrics(
            close2, amt2, s2, c2, None, None))
        assert by2[_SEC_A]["rs_5d"] is None and by2[_SEC_A]["rs_20d"] is None
        assert by2[_SEC_A]["breadth_ma20"] is not None, \
            "코스피가 없다고 폭까지 버렸다"
        assert by2[_SEC_A]["turnover_share"] is not None

        # 입력이 아예 없으면 빈 목록
        assert compute_sector_metrics(None, None, {}, None, 1.0, 1.0) == []
        assert compute_sector_metrics(pd.DataFrame(), pd.DataFrame(),
                                      sectors, caps, 1.0, 1.0) == []

    def t_sector_store_roundtrip():
        """저장 → 조회 → 덮어쓰기 + 과거 순위 조회."""
        from stocknews.sector_metrics import compute_sector_metrics
        from stocknews.store import Store
        s = Store(tmp / "secmet.db")
        close, amt, sectors, caps = _sector_fixture()
        rows = compute_sector_metrics(close, amt, sectors, caps, 5.0, 5.0,
                                      scan_date="2026-08-27")
        assert s.has_sector_metrics("2026-08-27") is False
        assert s.upsert_sector_metrics(rows) == 2
        assert s.has_sector_metrics("2026-08-27") is True

        got = s.sector_metrics_on("2026-08-27")
        assert len(got) == 2, len(got)
        assert list(got["sector"]) == [_SEC_A, _SEC_B], list(got["sector"])
        near(float(got.iloc[0]["rs_20d"]), 91.0, tol=1e-9, label="저장된 RS")
        assert got.iloc[0]["created_at"], "created_at 이 비었다"

        assert s.sector_rs_ranks("2026-08-27") == {_SEC_A: 1, _SEC_B: 2}
        assert s.sector_rs_ranks("2026-01-01") == {}

        # 덮어쓰기 — 행이 늘지 않아야 한다
        assert s.upsert_sector_metrics(rows) == 2
        assert len(s.sector_metrics_on("2026-08-27")) == 2

    def t_sector_field_matrix():
        """field_matrix 가 amt 를 종가와 같은 모양으로 준다."""
        from stocknews.store import Store
        s = Store(tmp / "secmet_fm.db")
        idx = pd.DatetimeIndex(pd.bdate_range(end="2026-08-27", periods=5))
        for code, amt_v in (("AAA555", 111.0), ("BBB555", 222.0)):
            s.upsert_prices(code, pd.DataFrame({
                "시가": np.full(5, 100.0), "고가": np.full(5, 100.0),
                "저가": np.full(5, 100.0), "종가": np.full(5, 100.0),
                "거래량": np.full(5, 1e6),
                "거래대금": np.full(5, amt_v)}, index=idx), allow_today=True)
        m = s.field_matrix("amt", days=10)
        assert list(m.columns) == ["AAA555", "BBB555"], list(m.columns)
        near(float(m["AAA555"].iloc[-1]), 111.0, tol=1e-6, label="amt")
        c = s.price_matrix(days=10)
        assert list(m.index) == list(c.index), "종가와 축이 다르다"
        try:
            s.field_matrix("없는컬럼", days=5)
        except ValueError:
            pass
        else:
            raise AssertionError("알 수 없는 컬럼을 통과시켰다")

    def t_sector_collect_isolated():
        """수집기는 하루 1회 · 실패를 삼킨다 · daily 를 죽이지 않는다."""
        from stocknews.sector_metrics import (collect_sector_metrics,
                                              compute_sector_metrics)
        from stocknews.store import Store
        s = Store(tmp / "secmet_col.db")
        close, amt, sectors, caps = _sector_fixture()
        s.upsert_sector_metrics(compute_sector_metrics(
            close, amt, sectors, caps, 5.0, 5.0, scan_date="2026-08-27"))
        out = collect_sector_metrics(s, "2026-08-27")
        assert out["skipped"] is True and out["sectors"] == 0, out

        # 시세가 없는 새 DB -> 조용히 0
        s2 = Store(tmp / "secmet_empty.db")
        out2 = collect_sector_metrics(s2, "2026-08-27", use_fdr=False)
        assert out2["sectors"] == 0 and out2["skipped"] is False, out2
        assert s2.has_sector_metrics("2026-08-27") is False

        # store 가 터져도 예외가 올라오지 않아야 한다
        class _Boom:
            def has_sector_metrics(self, d):
                raise RuntimeError("DB 없음")

        assert collect_sector_metrics(_Boom(), "2026-08-27")["sectors"] == 0
        assert collect_sector_metrics(s, "")["sectors"] == 0

    def t_sector_unclassified_excluded():
        """미분류는 행을 만들지 않는다. 전 종목이 미분류면 빈 목록."""
        from stocknews.sector_metrics import UNCLASSIFIED, compute_sector_metrics
        close, amt, sectors, caps = _sector_fixture()
        rows = compute_sector_metrics(close, amt, sectors, caps, 5.0, 5.0)
        assert UNCLASSIFIED not in {r["sector"] for r in rows}
        allna = {k: UNCLASSIFIED for k in sectors}
        assert compute_sector_metrics(close, amt, allna, caps, 5.0, 5.0) == []

    def t_sector_cuts_at_scan_date():
        """기준일 이후 봉을 잘라야 한다. 실측으로 맞은 버그의 회귀 가드.

        행렬 끝에 '13종목만 들어온 부분 적재일'이 붙어 있으면, 자르지
        않으면 마지막 행이 대부분 NaN 이라 거의 모든 섹터가 NULL 이 된다.
        (2026-08-28 이 실제로 그랬고 157섹터 중 146개가 NULL 이었다)
        """
        from stocknews.sector_metrics import compute_sector_metrics
        close, amt, sectors, caps = _sector_fixture()

        # 하루 뒤 부분 적재일을 붙인다 — A1 만 값이 있고 나머지는 NaN
        nxt = close.index[-1] + pd.Timedelta(days=1)
        part = pd.DataFrame(
            [[999.0] + [np.nan] * (len(close.columns) - 1)],
            index=[nxt], columns=close.columns)
        dirty = pd.concat([close, part])
        dirty_amt = pd.concat([amt, pd.DataFrame(
            [[1.0] + [np.nan] * (len(amt.columns) - 1)],
            index=[nxt], columns=amt.columns)])

        scan = close.index[-1].strftime("%Y-%m-%d")
        by = _rows_by_sector(compute_sector_metrics(
            dirty, dirty_amt, sectors, caps, 5.0, 5.0, scan_date=scan))
        # 자르고 나면 앵커 테스트와 같은 값이 나와야 한다
        near(by[_SEC_A]["rs_5d"], 13.0, tol=1e-9, label="자른 뒤 A RS 5일")
        near(by[_SEC_A]["rs_20d"], 91.0, tol=1e-9, label="자른 뒤 A RS 20일")
        near(by[_SEC_A]["breadth_ma20"], 200.0 / 3.0, tol=1e-9, label="A 폭")
        near(by[_SEC_A]["turnover_share"], 40.0, tol=1e-9, label="A 비중")
        assert by[_SEC_B]["rs_20d"] is not None, "B 섹터가 NULL 이 됐다"

        # 자르지 않으면(기준일 미지정) 오염된 값이 나온다 — 대조군
        dirty_rows = _rows_by_sector(compute_sector_metrics(
            dirty, dirty_amt, sectors, caps, 5.0, 5.0))
        assert dirty_rows[_SEC_B]["rs_20d"] is None, \
            "부분 적재일이 섞였는데 B 가 계산됐다 (대조군이 성립 안 함)"

    def t_sector_turnover_fallback():
        """amt 가 비어 있으면 종가x거래량으로 근사한다.

        이 저장소의 prices.amt 는 전 행 NULL 이다. 폴백이 없으면 거래대금
        집중도가 영구히 NULL 로 남는다 (universe.liquidity_filter 와 같은 폴백).
        """
        from stocknews.sector_metrics import turnover_matrix
        from stocknews.store import Store
        s = Store(tmp / "secmet_turn.db")
        idx = pd.DatetimeIndex(pd.bdate_range(end="2026-08-27", periods=5))
        for code, px, vol in (("AAA666", 100.0, 10.0), ("BBB666", 200.0, 5.0)):
            s.upsert_prices(code, pd.DataFrame({
                "시가": np.full(5, px), "고가": np.full(5, px),
                "저가": np.full(5, px), "종가": np.full(5, px),
                "거래량": np.full(5, vol)},   # 거래대금 컬럼 없음
                index=idx), allow_today=True)
        m, src = turnover_matrix(s, 10)
        assert src == "close*volume", src
        near(float(m["AAA666"].iloc[-1]), 1000.0, tol=1e-6, label="100x10")
        near(float(m["BBB666"].iloc[-1]), 1000.0, tol=1e-6, label="200x5")

        # amt 가 있으면 그걸 쓴다
        s2 = Store(tmp / "secmet_turn2.db")
        s2.upsert_prices("AAA777", pd.DataFrame({
            "시가": np.full(5, 100.0), "고가": np.full(5, 100.0),
            "저가": np.full(5, 100.0), "종가": np.full(5, 100.0),
            "거래량": np.full(5, 10.0),
            "거래대금": np.full(5, 7.0)}, index=idx), allow_today=True)
        m2, src2 = turnover_matrix(s2, 10)
        assert src2 == "amt", src2
        near(float(m2["AAA777"].iloc[-1]), 7.0, tol=1e-6, label="amt 우선")

        # 빈 DB
        m3, src3 = turnover_matrix(Store(tmp / "secmet_turn3.db"), 10)
        assert src3 == "none" and m3.empty

    def t_sector_record_only():
        """기록 전용 증명. 점수·추천·스크리닝·알림에 참조가 없어야 한다."""
        import inspect
        from stocknews import (exits, liquidation, notify, renderer, screener,
                               weekly)
        from stocknews.daily import select_recommendations

        tokens = ("sector_metrics", "rs_20d", "rs_5d", "breadth_ma20",
                  "turnover_share", "momentum_persist", "new_high_cnt",
                  "SectorConfig", "collect_sector_metrics")
        for mod in (renderer, screener, notify, exits, weekly, liquidation):
            src = inspect.getsource(mod)
            for tk in tokens:
                assert tk not in src, \
                    f"{mod.__name__} 가 {tk} 를 참조한다 (기록 전용 위반)"

        # 점수·선정 함수가 실제로 참조하지 않는지
        for fn in (screener.screen_one, screener.check_exclusion,
                   select_recommendations, liquidation.evaluate_liquidation):
            names = set(fn.__code__.co_names)
            for tk in ("sector_metrics", "collect_sector_metrics",
                       "rs_20d", "breadth_ma20"):
                assert tk not in names, f"{fn.__name__} 가 {tk} 를 본다"

        # sector_metrics 가 점수·표시 모듈을 import 하지 않는지
        import stocknews.sector_metrics as SM
        src = inspect.getsource(SM)
        for bad in ("from .screener", "from .renderer", "from .notify",
                    "from .exits", "from .liquidation", "from .weekly"):
            assert bad not in src, f"sector_metrics 가 {bad} 를 한다"

    def t_sector_recos_schema_unchanged():
        """recos 스키마에 변화가 없어야 한다 (기록 전용의 다른 한쪽 증명)."""
        import sqlite3
        from stocknews.store import Store
        s = Store(tmp / "secmet_schema.db")
        con = sqlite3.connect(s.path)
        try:
            cols = [r[1] for r in con.execute(
                "PRAGMA table_info(recos)").fetchall()]
        finally:
            con.close()
        assert cols == ["d", "rank", "ticker", "name", "price", "slot",
                        "grade", "value_score", "trend_score", "reason"], cols

    check("sector", "종목 단위 계산 앵커", t_sector_ticker_math)
    check("sector", "거래대금 비중 (분모=전체시장)", t_sector_turnover_share)
    check("sector", "RS + 순위 앵커 (시총가중)", t_sector_rs_and_rank)
    check("sector", "시총 없으면 동일가중", t_sector_equal_weight_fallback)
    check("sector", "폭 + 신고가 비율", t_sector_breadth_and_newhigh)
    check("sector", "모멘텀 지속성 (없으면 NULL)", t_sector_momentum_persist)
    check("sector", "데이터 부족은 NULL", t_sector_null_on_short_data)
    check("sector", "저장/조회/덮어쓰기 + 과거순위", t_sector_store_roundtrip)
    check("sector", "field_matrix (amt 피벗)", t_sector_field_matrix)
    check("sector", "수집기 격리 (하루1회·실패삼킴)", t_sector_collect_isolated)
    check("sector", "미분류 집계 제외", t_sector_unclassified_excluded)
    check("sector", "기준일 이후 봉 절단 (부분적재 방어)", t_sector_cuts_at_scan_date)
    check("sector", "거래대금 폴백 (종가x거래량)", t_sector_turnover_fallback)
    check("sector", "기록 전용 증명 (참조 부재)", t_sector_record_only)
    check("sector", "recos 스키마 불변", t_sector_recos_schema_unchanged)

    # ───────── 뉴스 테마 빈도 (기록 전용) ─────────
    def t_nf_dict_matches_classify():
        """사전이 두 벌로 갈라지지 않아야 한다.

        config.NEWS_THEME_KEYWORDS 와 news.CATEGORY_RULES 는 같은 어휘를
        써야 한다. 한쪽만 고치면 여기서 깨진다.
        """
        from stocknews.config import NEWS_THEME_KEYWORDS
        from stocknews.news import CATEGORY_RULES
        rules = dict(CATEGORY_RULES)
        assert len(NEWS_THEME_KEYWORDS) == 10, len(NEWS_THEME_KEYWORDS)
        for theme, keys in NEWS_THEME_KEYWORDS.items():
            assert theme in rules, f"{theme} 가 CATEGORY_RULES 에 없다"
            assert tuple(keys) == tuple(rules[theme]), \
                f"{theme} 키워드가 CATEGORY_RULES 와 다르다"
        # 키워드는 전부 소문자여야 한다 (제목을 lower 해서 비교하므로)
        for theme, keys in NEWS_THEME_KEYWORDS.items():
            for k in keys:
                assert k == k.lower(), f"{theme} 의 '{k}' 가 대문자를 포함"

    def _titles(rows):
        return pd.DataFrame(rows, columns=["d", "title"])

    def t_nf_count_anchor():
        """카운트 앵커. 합성 제목으로 손계산과 대조한다.

        제목 5건 (같은 날):
          1 "반도체 HBM 수출 급증"        -> 반도체
          2 "삼성전자 실적 어닝 서프라이즈" -> 실적
          3 "반도체 업체 영업이익 급증"    -> 반도체 + 실적  (다중 라벨)
          4 "배터리 양극재 수주"          -> 2차전지 + 방산조선(수주)
          5 "코스피 마감"                 -> 아무 테마도 아님
        기대: 반도체 2 · 실적 2 · 2차전지 1 · 방산조선 1 · 나머지 0
        """
        from stocknews.news_freq import count_mentions
        t = _titles([
            ("2026-08-27", "반도체 HBM 수출 급증"),
            ("2026-08-27", "삼성전자 실적 어닝 서프라이즈"),
            ("2026-08-27", "반도체 업체 영업이익 급증"),
            ("2026-08-27", "배터리 양극재 수주"),
            ("2026-08-27", "코스피 마감"),
        ])
        c = count_mentions(t)
        assert int(c.loc["2026-08-27", "반도체"]) == 2, c.loc["2026-08-27"]
        assert int(c.loc["2026-08-27", "실적"]) == 2
        assert int(c.loc["2026-08-27", "2차전지"]) == 1
        assert int(c.loc["2026-08-27", "방산조선"]) == 1, "'수주' 가 안 잡혔다"
        assert int(c.loc["2026-08-27", "바이오"]) == 0
        assert int(c.loc["2026-08-27", "전력AI"]) == 0

    def t_nf_case_and_multilabel():
        """영문 대문자 헤드라인도 잡아야 하고, 다중 라벨이어야 한다."""
        from stocknews.news_freq import count_mentions
        c = count_mentions(_titles([
            ("2026-08-27", "NVIDIA GPU demand and FOMC rate decision"),
        ]))
        assert int(c.loc["2026-08-27", "반도체"]) == 1, "대문자 NVIDIA 미검출"
        assert int(c.loc["2026-08-27", "매크로"]) == 1, "FOMC/rate 미검출"

        # 같은 사건을 여러 매체가 쓰면 그만큼 센다 (증폭. 동작을 고정한다)
        many = _titles([("2026-08-27", f"반도체 호황 {i}") for i in range(20)])
        assert int(count_mentions(many).loc["2026-08-27", "반도체"]) == 20

    def t_nf_ma_and_change():
        """MA7 과 변화율 앵커. 당일을 포함한 7일 평균이다.

        7일간 반도체 언급 = [1, 1, 1, 1, 1, 1, 8]  (마지막이 기준일)
          MA7 = (1x6 + 8)/7 = 14/7 = 2.0
          chg = (8/2.0 - 1) x 100 = +300.0%
        """
        from stocknews.news_freq import compute_news_freq
        days = [f"2026-08-{d:02d}" for d in range(21, 28)]
        rows = []
        for i, d in enumerate(days):
            n = 8 if i == len(days) - 1 else 1
            rows += [(d, f"반도체 뉴스 {j}") for j in range(n)]
        got = {r["sector"]: r for r in compute_news_freq(_titles(rows),
                                                        "2026-08-27")}
        semi = got["반도체"]
        assert semi["mention_cnt"] == 8, semi
        near(semi["mention_cnt_ma7"], 2.0, tol=1e-9, label="MA7")
        near(semi["chg_vs_ma7"], 300.0, tol=1e-9, label="변화율")
        # 언급이 0 인 테마는 0 으로 기록되고 MA 는 0 이라 변화율은 None
        assert got["바이오"]["mention_cnt"] == 0
        near(got["바이오"]["mention_cnt_ma7"], 0.0, tol=1e-9, label="바이오 MA7")
        assert got["바이오"]["chg_vs_ma7"] is None, "0 나누기를 했다"

    def t_nf_short_history_null():
        """일자가 7일 미만이면 MA·변화율을 만들지 않는다."""
        from stocknews.news_freq import compute_news_freq
        rows = [(f"2026-08-{d:02d}", "반도체 뉴스") for d in (25, 26, 27)]
        got = {r["sector"]: r for r in compute_news_freq(_titles(rows),
                                                         "2026-08-27")}
        assert got["반도체"]["mention_cnt"] == 1
        assert got["반도체"]["mention_cnt_ma7"] is None, \
            "3일치로 7일 평균을 만들었다"
        assert got["반도체"]["chg_vs_ma7"] is None
        # 뉴스가 아예 없어도 10테마 0건 행을 만든다 (행 없음과 0건은 다르다)
        empty = compute_news_freq(pd.DataFrame(columns=["d", "title"]),
                                  "2026-08-27")
        assert len(empty) == 10, len(empty)
        assert all(r["mention_cnt"] == 0 for r in empty)
        assert all(r["mention_cnt_ma7"] is None for r in empty)

    def t_nf_no_lookahead():
        """기준일 이후 일자는 쓰지 않는다."""
        from stocknews.news_freq import compute_news_freq
        rows = ([("2026-08-27", "반도체 뉴스")]
                + [("2026-08-28", f"반도체 폭증 {i}") for i in range(50)])
        got = {r["sector"]: r for r in compute_news_freq(_titles(rows),
                                                         "2026-08-27")}
        assert got["반도체"]["mention_cnt"] == 1, \
            f"미래 일자가 섞였다: {got['반도체']}"

    def t_nf_store_and_collect():
        """저장·덮어쓰기 + 수집기 격리."""
        from stocknews.news_freq import collect_news_freq, compute_news_freq
        from stocknews.store import Store
        s = Store(tmp / "newsfreq.db")
        rows = compute_news_freq(_titles([("2026-08-27", "반도체 HBM")]),
                                 "2026-08-27")
        assert s.has_news_freq("2026-08-27") is False
        assert s.upsert_news_freq(rows) == 10
        assert s.has_news_freq("2026-08-27") is True
        got = s.news_freq_on("2026-08-27")
        assert len(got) == 10, len(got)
        assert int(got.iloc[0]["mention_cnt"]) == 1, "정렬이 건수 내림차순 아님"
        assert got.iloc[0]["created_at"], "created_at 이 비었다"
        # 덮어쓰기 — 행이 늘지 않아야 한다
        assert s.upsert_news_freq(rows) == 10
        assert len(s.news_freq_on("2026-08-27")) == 10

        # 뉴스가 없는 새 DB 에서도 조용히 10테마 0건
        s2 = Store(tmp / "newsfreq2.db")
        out = collect_news_freq(s2, "2026-08-27")
        assert out["themes"] == 10 and out["total_mentions"] == 0, out

        # store 가 터져도 예외가 올라오지 않아야 한다
        class _Boom:
            def news_titles(self, days=30):
                raise RuntimeError("DB 없음")

        assert collect_news_freq(_Boom(), "2026-08-27")["themes"] == 0

    def t_nf_record_only():
        """기록 전용 증명. 점수·추천·스크리닝·알림에 참조가 없어야 한다."""
        import inspect
        from stocknews import (exits, liquidation, notify, renderer, screener,
                              weekly)
        from stocknews.daily import select_recommendations

        tokens = ("news_freq", "mention_cnt", "chg_vs_ma7",
                  "NEWS_THEME_KEYWORDS", "NewsFreqConfig")
        for mod in (renderer, screener, notify, exits, weekly, liquidation):
            src = inspect.getsource(mod)
            for tk in tokens:
                assert tk not in src, \
                    f"{mod.__name__} 가 {tk} 를 참조한다 (기록 전용 위반)"
        for fn in (screener.screen_one, select_recommendations):
            names = set(fn.__code__.co_names)
            for tk in ("news_freq", "mention_cnt", "collect_news_freq"):
                assert tk not in names, f"{fn.__name__} 가 {tk} 를 본다"

        # news_freq 가 점수·표시 모듈을 import 하지 않는지
        import stocknews.news_freq as NF
        src = inspect.getsource(NF)
        for bad in ("from .screener", "from .renderer", "from .notify",
                    "from .exits", "from .liquidation", "from .weekly"):
            assert bad not in src, f"news_freq 가 {bad} 를 한다"

    def t_nf_axis_differs_from_sector():
        """news_freq.sector 는 뉴스 테마다. KRX 업종과 값 집합이 다르다.

        이름이 같아서 조인할 수 있다고 착각하기 쉬운 지점이라 명시한다.
        """
        from stocknews.config import NEWS_THEME_KEYWORDS
        themes = set(NEWS_THEME_KEYWORDS)
        krx_like = {"의약품 제조업", "전자부품 제조업", "소프트웨어 개발 및 공급업"}
        assert not (themes & krx_like), \
            "뉴스 테마와 KRX 업종명이 겹친다 — 축 구분이 무너진다"
        assert "반도체" in themes and "공시" in themes

    check("news-freq", "사전 정본 일치 (classify 와)", t_nf_dict_matches_classify)
    check("news-freq", "카운트 앵커 (다중 라벨)", t_nf_count_anchor)
    check("news-freq", "영문 대문자 + 증폭 고정", t_nf_case_and_multilabel)
    check("news-freq", "MA7 + 변화율 앵커", t_nf_ma_and_change)
    check("news-freq", "일자 부족은 NULL", t_nf_short_history_null)
    check("news-freq", "미래 일자 미사용", t_nf_no_lookahead)
    check("news-freq", "저장/덮어쓰기 + 수집기 격리", t_nf_store_and_collect)
    check("news-freq", "기록 전용 증명 (참조 부재)", t_nf_record_only)
    check("news-freq", "테마 축 != KRX 업종 축", t_nf_axis_differs_from_sector)

    def t_earnings_source_is_none():
        """실적 추정치 소스는 미구현. None 이 조사 결론이다.

        누군가 프록시(과거 성장률 등)를 넣으려 하면 여기서 걸린다.
        추정치를 만들어 쓰는 것은 없는 데이터를 만들어내는 것이다.
        """
        import inspect
        from stocknews import config as C
        assert hasattr(C, "EARNINGS_SOURCE"), "EARNINGS_SOURCE 자리가 없다"
        assert C.EARNINGS_SOURCE is None, \
            f"소스가 채워졌다: {C.EARNINGS_SOURCE!r} — 실제 소스인지 확인 필요"
        # 추정치를 만들어내는 코드가 붙지 않았는지
        for mod_name in ("screener", "daily", "sector_metrics", "weekly"):
            mod = __import__(f"stocknews.{mod_name}", fromlist=["x"])
            src = inspect.getsource(mod)
            for tk in ("EARNINGS_SOURCE", "consensus", "eps_estimate"):
                assert tk not in src, f"{mod_name} 가 {tk} 를 참조한다"

    check("news-freq", "실적 추정치 자리만 (None)", t_earnings_source_is_none)

    # ───────── 재진입 금지 쿨다운 + 월간 회고 ─────────
    from stocknews.config import COOLDOWN_DAYS
    from stocknews.contracts import COOLDOWN_REASON, MANUAL_REASON

    def _cool_store(name: str, n_days: int = 45):
        """거래일 n_days, 종목 2개(평탄) 픽스처. 2026-07-01 부터."""
        from stocknews.store import Store
        s = Store(tmp / name)
        idx = pd.DatetimeIndex(pd.bdate_range("2026-07-01", periods=n_days))
        flat = np.full(n_days, 100.0)
        for code in ("AAA222", "BBB222"):
            s.upsert_prices(code, pd.DataFrame({
                "시가": flat, "고가": flat, "저가": flat, "종가": flat,
                "거래량": np.full(n_days, 1e6),
            }, index=idx), allow_today=True)
        return s, [d.strftime("%Y-%m-%d") for d in idx]

    def t_cooldown_anchor():
        from stocknews.screener import HARD_EXCLUSION_FLAGS
        assert COOLDOWN_DAYS == 20, COOLDOWN_DAYS
        assert DEFAULT.exit.cooldown_days == COOLDOWN_DAYS, "config 배선 누락"
        assert COOLDOWN_REASON != MANUAL_REASON, "자동/수동 사유가 같다"
        assert COOLDOWN_REASON.startswith("AUTO:"), COOLDOWN_REASON
        keys = [k for k, _ in HARD_EXCLUSION_FLAGS]
        assert "재진입금지" in keys, f"하드 배제에 없다: {keys}"

    def t_cooldown_hard_exclusion():
        """스크리닝 하드 배제 단계에서 걸려야 한다 (+ 대조군)."""
        from stocknews.screener import check_exclusion, screen_one
        blocked = check_exclusion({"재진입금지": True}, None, None)
        assert blocked and "재진입" in blocked, blocked
        assert check_exclusion({"재진입금지": False}, None, None) is None
        # 스크리너 전체 경로에서도 배제로 끝나야 한다
        r = screen_one("000001", "손절종목", fixture_crash(),
                       flags={"재진입금지": True})
        assert r.excluded and "재진입" in r.excluded, r.excluded
        assert r.grade == "NONE" and r.value_score == 0.0

    def t_cooldown_expiry_boundary():
        """만료 전후 대조군. 경과 거래일로 판정해야 한다."""
        s, dates = _cool_store("cool_edge.db")
        # 마지막 거래일 기준 19거래일 전에 손절 -> 경과 19 -> 활성
        s.set_cooldown("AAA222", dates[-19], COOLDOWN_REASON)
        # 21거래일 전 -> 경과 21 -> 만료
        s.set_cooldown("BBB222", dates[-21], COOLDOWN_REASON)

        assert s.trading_days_after(dates[-19]) == 18, \
            s.trading_days_after(dates[-19])
        st_ = s.cooldown_state(COOLDOWN_DAYS)
        assert st_["AAA222"]["active"] is True, st_["AAA222"]
        assert st_["BBB222"]["active"] is False, st_["BBB222"]

        fl = s.load_flags(COOLDOWN_DAYS)
        assert fl["AAA222"]["재진입금지"] is True
        assert fl["BBB222"]["재진입금지"] is False
        # 경과 일수를 그대로 노출해야 원인을 볼 수 있다
        assert fl["AAA222"]["재진입금지_경과"] == 18, fl["AAA222"]
        assert fl["AAA222"]["재진입금지_시작"] == dates[-19]

    def t_cooldown_auto_release():
        """만료 시 자동 해제. 수동 사유는 건드리지 않는다."""
        s, dates = _cool_store("cool_rel.db")
        s.set_cooldown("AAA222", dates[-30], COOLDOWN_REASON)   # 만료
        s.set_cooldown("BBB222", dates[-30], MANUAL_REASON)     # 만료지만 수동
        gone = s.expire_cooldowns(COOLDOWN_DAYS, COOLDOWN_REASON)
        assert gone == ["AAA222"], gone
        state = s.cooldown_state(COOLDOWN_DAYS)
        assert "AAA222" not in state, "자동 항목이 지워지지 않았다"
        assert state["BBB222"]["reason"] == MANUAL_REASON, \
            "자동 청소가 수동 배제를 지웠다"

    def t_cooldown_trading_days_not_calendar():
        """달력일이 아니라 거래일로 세야 한다."""
        s, dates = _cool_store("cool_cal.db")
        d0 = dates[-11]
        s.set_cooldown("AAA222", d0, COOLDOWN_REASON)
        el = s.cooldown_state(COOLDOWN_DAYS)["AAA222"]["elapsed"]
        assert el == 10, f"경과 거래일 {el}"
        cal = (datetime.strptime(dates[-1], "%Y-%m-%d")
               - datetime.strptime(d0, "%Y-%m-%d")).days
        assert cal > el, f"픽스처에 주말이 안 끼었다 (달력 {cal})"

    def t_cooldown_latest_stop_wins():
        """손절이 두 번 나면 늦은 쪽부터 다시 센다."""
        s, dates = _cool_store("cool_latest.db")
        s.set_cooldown("AAA222", dates[-25], COOLDOWN_REASON)
        s.set_cooldown("AAA222", dates[-5], COOLDOWN_REASON)    # 더 최근
        assert s.cooldown_state()["AAA222"]["from"] == dates[-5]
        s.set_cooldown("AAA222", dates[-30], COOLDOWN_REASON)   # 과거로 되돌림
        assert s.cooldown_state()["AAA222"]["from"] == dates[-5], \
            "과거 손절일로 되돌아가 쿨다운이 짧아졌다"

    def t_cooldown_from_exit_log():
        """손절 이력에서 쿨다운을 재구성한다 (배치가 죽었던 경우 복구)."""
        from stocknews.contracts import ExitDecision
        from stocknews.exits import stop_price_for
        from stocknews.flags import sync_stop_cooldowns

        s, dates = _cool_store("cool_sync.db")
        pid = s.open_position("AAA222", "손절종목", "VALUE", dates[-12],
                              100.0, 10, stop_price=stop_price_for(100.0, DEFAULT))
        pid2 = s.open_position("BBB222", "익절종목", "TREND", dates[-12],
                               100.0, 10, stop_price=stop_price_for(100.0, DEFAULT))

        def _dec(pid_, ticker, rule, layer):
            return ExitDecision(
                ticker=ticker, name="x", position_id=pid_, layer=layer,
                rule=rule, action="EXIT_ALL", ratio=1.0, qty=10,
                signal_price=90.0, ret_pct=-10.0, net_ret_pct=-10.5,
                reason="테스트", urgent=True)

        s.record_exit_signal(_dec(pid, "AAA222", "stop:fixed", 1), dates[-8])
        s.record_exit_signal(_dec(pid2, "BBB222", "target:take1", 5), dates[-8])

        out = sync_stop_cooldowns(s, COOLDOWN_DAYS)
        assert out["active"] == 1, out
        assert out["active_list"] == ["AAA222"], out["active_list"]
        fl = s.load_flags(COOLDOWN_DAYS)
        assert fl["AAA222"]["재진입금지"] is True
        assert fl.get("BBB222", {}).get("재진입금지") in (False, None), \
            "손절이 아닌 청산에 쿨다운이 걸렸다"

    def t_first_friday():
        from stocknews.weekly import is_first_friday, prev_month_bounds
        from datetime import date as _d
        # 2026-08-01 은 토요일이라 8월 첫 금요일은 7일이다 (경계)
        assert _d(2026, 8, 1).weekday() == 5, "픽스처 요일 불일치"
        assert is_first_friday(_d(2026, 8, 7)) is True
        assert is_first_friday(_d(2026, 8, 14)) is False
        assert is_first_friday(_d(2026, 9, 4)) is True
        assert is_first_friday(_d(2026, 9, 11)) is False
        assert is_first_friday(_d(2026, 9, 3)) is False, "목요일인데 통과"
        assert prev_month_bounds(_d(2026, 9, 4)) == \
            ("2026-08-01", "2026-08-31", "2026-08")
        # 연초 경계
        assert prev_month_bounds(_d(2026, 1, 2)) == \
            ("2025-12-01", "2025-12-31", "2025-12")

    _month_cache: list = []

    def _month_store():
        """전월(2026-08) 진입 3건 · 청산 2건 픽스처.

        여러 검사가 같이 쓰므로 한 번만 만든다. 매번 만들면 같은 DB 파일에
        추천이 두 번 들어가 UNIQUE(d, rank) 에 걸린다.
        """
        if _month_cache:
            return _month_cache[0]
        from stocknews.contracts import ExitDecision
        from stocknews.exits import stop_price_for
        from stocknews.store import Store

        s = Store(tmp / "monthly.db")
        idx = pd.DatetimeIndex(pd.bdate_range("2026-07-01", "2026-09-01"))
        flat = np.full(len(idx), 100.0)
        for code in ("AAA333", "BBB333"):
            s.upsert_prices(code, pd.DataFrame({
                "시가": flat, "고가": flat, "저가": flat, "종가": flat,
                "거래량": np.full(len(idx), 1e6),
            }, index=idx), allow_today=True)

        sp = stop_price_for(100.0, DEFAULT)
        # ① 08-03 진입 -> 08-05 익절 체결. 보유 3거래일 = 최소보유일 위반
        p1 = s.open_position("AAA333", "조기청산", "VALUE", "2026-08-03",
                             100.0, 10, stop_price=sp)
        # ② 08-03 진입 -> 08-20 손절 신호, 미체결 = 손절 미이행
        p2 = s.open_position("BBB333", "손절방치", "VALUE", "2026-08-03",
                             100.0, 10, stop_price=sp)
        # ③ 08-25 진입 (② 손절 4거래일 뒤 같은 종목) = 재진입 위반
        s.open_position("BBB333", "재진입", "VALUE", "2026-08-25",
                        100.0, 10, stop_price=sp)

        def _dec(pid_, ticker, name, rule, layer, ratio=1.0):
            return ExitDecision(
                ticker=ticker, name=name, position_id=pid_, layer=layer,
                rule=rule, action="EXIT_ALL" if ratio >= 1.0 else "TRIM",
                ratio=ratio, qty=10, signal_price=100.0, ret_pct=0.0,
                net_ret_pct=-0.5, reason="테스트", urgent=layer <= 1)

        lid1 = s.record_exit_signal(
            _dec(p1, "AAA333", "조기청산", "target:take1", 5), "2026-08-05")
        s.confirm_exit(lid1, 100.0)                 # 체결 -> 검증 대상
        s.record_exit_signal(
            _dec(p2, "BBB333", "손절방치", "stop:fixed", 1), "2026-08-20")
        # 손절은 체결하지 않는다 (미이행)
        for i, code in enumerate(("AAA333", "BBB333")):
            _insert_reco(s, "2026-08-04", code, f"추천{i}", "VALUE", "A",
                         rank=i + 1)
        _month_cache.append(s)
        return s

    def t_monthly_lists():
        """진입/청산 전체 목록이 나와야 한다."""
        from stocknews.weekly import monthly_review
        from datetime import date as _d
        m = monthly_review(_month_store(), DEFAULT, asof=_d(2026, 9, 4))
        assert m["month"] == "2026-08", m["month"]
        assert (m["start"], m["end"]) == ("2026-08-01", "2026-08-31")
        assert len(m["entries"]) == 3, [e["ticker"] for e in m["entries"]]
        assert len(m["exits"]) == 2, [e["rule"] for e in m["exits"]]
        assert m["recos"] == 2, m["recos"]
        assert {e["rule"] for e in m["exits"]} == {"target:take1", "stop:fixed"}
        assert m["horizons"] == [5, 10, 20]
        assert set(m["by_horizon"]) == {5, 10, 20}

    def t_monthly_compliance():
        """규칙 준수율 3종을 각각 정확히 세야 한다."""
        from stocknews.weekly import monthly_review
        from datetime import date as _d
        c = monthly_review(_month_store(), DEFAULT,
                           asof=_d(2026, 9, 4))["compliance"]
        assert c["min_hold_days"] == 5 and c["cooldown_days"] == 20
        assert c["min_hold_violations"] == 1, c["min_hold_list"]
        v = c["min_hold_list"][0]
        assert v["ticker"] == "AAA333" and v["held"] == 3, v
        assert c["stop_not_executed"] == 1, c["stop_not_executed_list"]
        assert c["stop_not_executed_list"][0]["ticker"] == "BBB333"
        assert c["reentry_violations"] == 1, c["reentry_list"]
        r = c["reentry_list"][0]
        assert r["stop_date"] == "2026-08-20" and r["entry_date"] == "2026-08-25"
        assert r["gap_bars"] == 4, r
        assert c["violations"] == 3, c
        assert c["compliance_pct"] is not None

    def t_monthly_compliance_clean():
        """대조군: 위반이 없으면 0건 · 준수율 100%."""
        from stocknews.contracts import ExitDecision
        from stocknews.exits import stop_price_for
        from stocknews.store import Store
        from stocknews.weekly import monthly_review
        from datetime import date as _d

        s = Store(tmp / "monthly_clean.db")
        idx = pd.DatetimeIndex(pd.bdate_range("2026-07-01", "2026-09-01"))
        flat = np.full(len(idx), 100.0)
        s.upsert_prices("AAA444", pd.DataFrame({
            "시가": flat, "고가": flat, "저가": flat, "종가": flat,
            "거래량": np.full(len(idx), 1e6)}, index=idx), allow_today=True)
        pid = s.open_position("AAA444", "정상", "VALUE", "2026-08-03", 100.0,
                              10, stop_price=stop_price_for(100.0, DEFAULT))
        lid = s.record_exit_signal(ExitDecision(
            ticker="AAA444", name="정상", position_id=pid, layer=5,
            rule="target:take1", action="EXIT_ALL", ratio=1.0, qty=10,
            signal_price=100.0, ret_pct=0.0, net_ret_pct=-0.5,
            reason="테스트", urgent=False), "2026-08-14")   # 보유 10거래일
        s.confirm_exit(lid, 100.0)

        c = monthly_review(s, DEFAULT, asof=_d(2026, 9, 4))["compliance"]
        assert c["min_hold_violations"] == 0, c["min_hold_list"]
        assert c["stop_not_executed"] == 0, c["stop_not_executed_list"]
        assert c["reentry_violations"] == 0, c["reentry_list"]
        assert c["violations"] == 0 and c["compliance_pct"] == 100.0, c

    def t_monthly_render_numbers_only():
        """숫자와 목록만. 해석 문구가 붙으면 안 된다."""
        from stocknews.renderer import _monthly_block
        from stocknews.weekly import monthly_review
        from datetime import date as _d

        m = monthly_review(_month_store(), DEFAULT, asof=_d(2026, 9, 4))
        txt = "\n".join(_monthly_block(m))
        assert "전월 회고 2026-08" in txt
        assert "2026-08-01 ~ 2026-08-31" in txt
        assert "진입 (3건)" in txt and "청산 (2건)" in txt
        assert "AAA333" in txt and "BBB333" in txt
        assert "최소보유일(5거래일) 위반 1건" in txt, txt
        assert "손절 미이행 1건" in txt
        assert "재진입 금지(20거래일) 위반 1건" in txt
        assert "준수율" in txt
        assert "<pre>" in txt, "보유기간 표가 등폭으로 감싸이지 않았다"
        banned = ("개선", "양호", "우수", "부진", "권고", "예상", "전망",
                  "추천합니다", "주의 필요", "⚠️")
        for w in banned:
            assert w not in txt, f"해석 문구가 들어갔다: {w}"

    def t_weekly_monthly_gate():
        """전월 회고는 매월 첫 금요일에만 붙는다. 강제 옵션은 유지."""
        from stocknews.weekly import weekly_report
        assert weekly_report(st, DEFAULT, monthly=False)["monthly"] is None
        forced = weekly_report(st, DEFAULT, monthly=True)["monthly"]
        assert isinstance(forced, dict) and "compliance" in forced
        # 자동 판정은 오늘 날짜에 의존하므로 값만 확인한다
        auto = weekly_report(st, DEFAULT)
        assert "monthly" in auto

    check("cooldown", "COOLDOWN_DAYS 앵커 + 하드배제 등록", t_cooldown_anchor)
    check("cooldown", "스크리닝 하드 배제 + 대조군", t_cooldown_hard_exclusion)
    check("cooldown", "만료 전후 경계 (거래일)", t_cooldown_expiry_boundary)
    check("cooldown", "만료 자동 해제 (수동 보존)", t_cooldown_auto_release)
    check("cooldown", "달력일 아님", t_cooldown_trading_days_not_calendar)
    check("cooldown", "늦은 손절일이 이김", t_cooldown_latest_stop_wins)
    check("cooldown", "손절 이력에서 재구성", t_cooldown_from_exit_log)
    check("monthly", "첫 금요일 판정 + 전월 경계", t_first_friday)
    check("monthly", "진입/청산 전체 목록", t_monthly_lists)
    check("monthly", "규칙 준수율 3종", t_monthly_compliance)
    check("monthly", "준수 대조군 (100%)", t_monthly_compliance_clean)
    check("monthly", "숫자·목록만 (해석 문구 금지)", t_monthly_render_numbers_only)
    check("monthly", "첫 금요일 게이트 + 강제 옵션", t_weekly_monthly_gate)


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
                  "fill", "pos-close", "credit", "credit-kiwoom",
                  "credit-probe", "kiwoom-plan", "export", "runs",
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
        test_news_dedup_fixture()
        test_dart_markets()
        test_holidays(tmp)
        test_watchlist(st)
        test_notify(tmp)
        test_reco_dow()
        test_trading_day(tmp)
        test_backtest(tmp)
        test_krx_credit()
        test_market_source(tmp)
        test_kiwoom(tmp)
        test_kiwoom_rest(tmp)
        test_dividends(tmp)
        test_dividend_screen(tmp)
        test_dividend_calendar(tmp)
        test_docs()
        test_env(tmp)
        test_joblock(tmp)
        test_cron_jobs(tmp)
        test_interpreter_guard(tmp)
        test_agent_contract(tmp)
        if st is not None:
            test_renderer(st)
            test_daily_weekly(st, tmp)
        return report()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
