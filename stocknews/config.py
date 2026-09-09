# -*- coding: utf-8 -*-
"""전 모듈 공용 파라미터.

숫자를 코드에 박지 않고 여기 한 곳에 모아둔 이유는 백테스트로 값을
흔들어보며 튜닝해야 하기 때문이다. -30%, 0.618, 20/40/60 전부
가설이므로 검증 전에는 상수 취급하지 않는다.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MAConfig:
    """이동평균 골든크로스 판정 파라미터."""

    short: int = 20
    mid: int = 40
    long: int = 60

    # 크로스 신선도: fresh_days 이내면 만점, stale_days에서 0점까지 선형 감쇠
    fresh_days: int = 5
    stale_days: int = 20

    # 크로스 시점 3개 이평선 밀집도(%) — 좁을수록 에너지 응축이 큼
    convergence_tight_pct: float = 3.0
    convergence_loose_pct: float = 5.0

    slope_window: int = 5          # 기울기 측정 구간(거래일)
    vol_surge_strong: float = 1.5  # 크로스일 거래량 / 20일 평균
    vol_surge_weak: float = 1.2

    # 크로스 후 되돌림(휩쏘) 판정: 종가가 장기선 아래로 되돌아간 횟수 허용치
    whipsaw_tolerance: int = 1


@dataclass(frozen=True)
class FibConfig:
    """1년 고점 기준 피보나치 되돌림 파라미터."""

    lookback: int = 252            # 1년 ≈ 252거래일
    levels: tuple[float, ...] = (0.236, 0.382, 0.5, 0.618, 0.786, 1.0)
    target: float = 0.618          # "이 값 이하"를 찾는 기준선
    touch_tol_pct: float = 2.5     # 레벨 ±2.5% 이내면 '터치'
    touch_loose_pct: float = 5.0

    high_fresh_days: int = 90      # 고점이 이 이내면 파동이 살아있다고 봄
    high_stale_days: int = 180     # 이보다 오래되면 신뢰도 하향
    min_swing_pct: float = 15.0    # 파동 폭이 고점의 15% 미만이면 레벨 무의미
    min_pre_bars: int = 20         # 고점 앞 구간이 이보다 짧으면 폴백


@dataclass(frozen=True)
class CreditConfig:
    """신용 강제청산 밴드 파라미터.

    담보유지비율 = 주식평가액 / 융자금 >= maint_ratio
      => 청산 트리거가격 Pc = P0 * maint_ratio * 융자비율(L)

    L=0.60 -> 0.840*P0 (-16%)   보증금률 40%, 마진콜 개시
    L=0.50 -> 0.700*P0 (-30%)   대량 청산 집중 구간
    L=0.40 -> 0.560*P0 (-44%)   연쇄청산 언더슈팅
    """

    maint_ratio: float = 1.40
    loan_ratio_hi: float = 0.60
    loan_ratio_mid: float = 0.50
    loan_ratio_lo: float = 0.40

    lookback: int = 90             # 신용 평균단가 추정 구간
    credit_shift: int = 1          # 신용잔고 공시 지연(영업일) — 룩어헤드 방지
    short_shift: int = 2           # 공매도 잔고 공시 지연(영업일)


# 성적을 판단 근거로 쓰기 전에 필요한 **채점 완료** 표본 수.
#
# 누적 추천 건수가 아니라 채점이 도래한 건수를 세야 한다. 추천 100건을
# 쌓아도 보유기간이 지나지 않았으면 성적은 0건이다. 진행률을 추천 건수로
# 표시하면 '거의 다 됐다'고 착각하게 된다.
#
# 20건은 승률이 우연과 구분되기 시작하는 최소선이다. 여전히 적다.
MIN_SCORED_FOR_JUDGMENT = 20


@dataclass(frozen=True)
class AuditConfig:
    """자기검증(사후 채점) 파라미터.

    보유기간을 하나로 고정하면 '이 전략이 5일물인지 20일물인지' 를 알 수
    없다. 5일에서 알파가 없어도 20일에서 나올 수 있고 그 반대도 가능하다.
    그래서 여러 기간을 **병렬로** 채점해 비교한다.

    아직 기간이 경과하지 않은 추천은 '미도래'로 세고 채점에서 뺀다.
    0으로 넣으면 평균이 0쪽으로 끌려가 성적이 실제보다 나빠 보이고,
    실패로 처리하면 표본이 조용히 줄어든다. 둘 다 판단을 흐린다.
    """

    horizons: tuple[int, ...] = (5, 10, 20)   # 채점 보유기간 (거래일)
    # 판단 기준. 종이거래 배너와 진행률 줄이 같은 숫자를 봐야 하므로
    # 상수 하나에서 가져온다. 둘이 갈라지면 어느 쪽이 맞는지 알 수 없다.
    min_sample: int = MIN_SCORED_FOR_JUDGMENT
    lookback_dates: int = 60    # 조회할 추천 일자 수


# 추천 10선을 텔레그램으로 내보내는 요일. `datetime.weekday()` 규격이라
# 월=0 … 일=6 이다. 6 = 일요일.
#
# 왜 주 1회인가: 매일 10선을 보내면 매일 판단을 요구받는다. 그러면 신호가
# 아니라 알림 빈도가 매매를 지배한다. 스캔·기록은 매일 하되(채점 표본 확보)
# 결정은 주 1회로 묶는다. 발송 요일을 일요일로 둔 이유는 장이 닫혀 있어
# 그날 안에 주문을 낼 수 없고, 그래서 반사적 매매가 구조적으로 막히기
# 때문이다. 월요일 개장까지 최소 하룻밤의 숙려 시간이 강제된다.
RECO_SEND_DOW = 6


@dataclass(frozen=True)
class GateConfig:
    """알림 게이트 파라미터."""

    value_threshold: float = 8.0   # 매집 트랙 발송 하한
    trend_threshold: float = 8.0   # 추세 트랙 발송 하한
    # 일일 상한은 여기 없다. **창별 예산의 합이 일일 상한이다** —
    # notify.WINDOWS 의 budget 2+3+2+2 = 9건. 예전에 daily_budget=3 이
    # 있었지만 어디서도 읽지 않는 죽은 값이라 2026-09-09 에 지웠다.
    # "하루 3건"이라고 믿고 있었다면 실제는 9건이었다.
    cooldown_days: int = 5         # 동일 종목 재발송 금지 기간
    rescore_delta: float = 1.0     # 점수가 이만큼 오르면 쿨다운 예외
    sequence_window: int = 20      # 매집 신호 -> 골든크로스 결합 허용 기간
    confluence_tol_pct: float = 3.0  # 피보 0.618선과 청산 중심선 겹침 허용치
    digest_top_n: int = 5
    reco_send_dow: int = RECO_SEND_DOW  # 추천 10선 발송 요일


# 보유기간 규칙. **거래일 기준**이다. 달력일로 세면 주말·연휴에 카운터가
# 앞서가서, 실제로는 3거래일밖에 안 지난 포지션이 만료 처리된다.
#
# MIN_HOLD_DAYS  이 기간 안에는 계층 2~7(트레일링·목표·시간·순환매)을
#                억제한다. 진입 직후의 흔들림에 반응해 나가면 그물을
#                던진 의미가 없다. 단 계층 0(무효화)과 계층 1(손절)은
#                억제하지 않는다 — 상장폐지 위험과 손절선 이탈을 5일간
#                막는 것은 최소보유일 규칙이 안전장치를 무력화하는 것이다.
# MAX_HOLD_DAYS  도달하면 '보유기간 만료 — 재평가'를 계층 0으로 낸다.
#                진입 근거에 유효기간을 주는 장치다. 어떤 규칙도 걸리지
#                않아 무한 보유되는 경로를 막는다.
MIN_HOLD_DAYS = 5
MAX_HOLD_DAYS = 20

# 진입가 대비 고정 손절 비율(%). 진입 시점에 `positions.stop_price` 로
# 확정 기록되고, 청산 판정은 그 기록된 값만 본다. 판정 시점에 다시
# 계산하면(예: 당일 ATR 로 폭을 넓히면) 손절선이 주가를 따라 움직여
# 손절이 영원히 발동하지 않는다.
STOP_LOSS_PCT = 10.0

# 손절 청산 후 재진입 금지 기간(거래일). 만료되면 자동 해제된다.
#
# 손절은 '진입 판단이 틀렸다'는 기록이다. 그런데 손절 직후의 종목은
# 스크리너 눈에 가장 매력적으로 보인다 — 더 싸졌고, 피보 되돌림은 더
# 깊어졌고, 청산 밴드 안으로 더 들어갔다. 그래서 같은 종목을 며칠 만에
# 다시 사는 일이 반복된다. 그물을 치는 게 아니라 물타기가 된다.
# 이 기간은 그 경로를 코드로 막는다.
COOLDOWN_DAYS = 20


@dataclass(frozen=True)
class ExitConfig:
    """청산 규칙 파라미터.

    설계 원칙: 청산은 진입 근거를 거울처럼 따라간다.
      트랙 V(역추세) -> 밴드로 들어가서 밴드로 나온다
      트랙 T(순추세) -> 크로스로 들어가서 트레일링으로 나온다
      공통           -> +15% 절반 기계적 익절만 동일

    우선순위(작을수록 우선). 같은 날 여러 개가 걸리면 하나만 집행한다.
      0 무효화 · 보유기간 만료 / 1 손절 / 2 트레일링 / 3 목표3차
      4 목표2차 / 5 목표1차 / 6 시간 / 7 순환매

    계층 0 안의 순서는 무효화 → 보유기간 만료다. 상세는 `exits.py` 참조.
    """

    # ── 계층 2: 목표(익절) ──
    take1_pct: float = 15.0            # 1차 익절 트리거(%)
    take1_ratio: float = 0.50          # 1차 청산 비율
    take2_ratio: float = 0.30          # 2차 청산 비율 (피보 0.382 회복)

    # ── 계층 2: 트레일링 (트랙 T) ──
    trail_pct: float = 8.0             # 진입 후 최고 '종가' 대비 하락폭
    trail_ma: int = 20                 # 이 이평선 종가 이탈 시 청산

    # ── 계층 1: 손절 ──
    band_break_days: int = 3           # 밴드 하단 종가 연속 이탈 일수
    stop_loss_pct: float = STOP_LOSS_PCT   # 진입가 대비 고정 손절(%)
    atr_window: int = 14
    ma_long_break_days: int = 2        # 종가가 MA60 아래 연속 일수
    cross_low_buffer_pct: float = 3.0  # 골든크로스 봉 저가 이탈 허용폭(%)

    # ── 계층 0-b / 게이트: 보유기간 (거래일 기준) ──
    min_hold_days: int = MIN_HOLD_DAYS
    max_hold_days: int = MAX_HOLD_DAYS

    # ── 손절 후 재진입 금지 (거래일 기준) ──
    cooldown_days: int = COOLDOWN_DAYS

    # ── 계층 3: 시간 ──
    time_stop_v_days: int = 15         # 트랙 V 시간 손절
    time_stop_v_min_ret: float = 3.0   # 이 수익률 미만이면 청산(%)
    time_stop_t_days: int = 20         # 트랙 T 시간 손절
    credit_resurge_pp: float = 1.5     # 신용잔고율 재급증 폭(%p)

    # ── 계층 4: 순환매 (포트폴리오) ──
    rotation_spread_pct: float = 15.0      # 대칭 자산 편차 임계
    rotation_trim_ratio: float = 0.50      # 급등 자산 청산 비율
    rotation_min_spread_pct: float = 5.0   # 이 미만이면 순환매 보류

    # ── 시장 급락일 손절 유예 (기본 비활성) ──
    # 논쟁적 옵션이다. 켜면 패닉 저가 투매를 피하지만 '손절 미루기'가
    # 습관이 될 위험이 있다. 유예는 1일로 고정하고 계층 0은 예외 없다.
    panic_defer_enabled: bool = False
    panic_index_drop_pct: float = -3.0
    panic_defer_days: int = 1

    # ── 체결/비용 가정 ──
    # 종가로 판단하므로 실제 체결은 익일이다. 백테스트도 동일 가정을 써야
    # 성과가 부풀려지지 않는다.
    fill_mode: str = "next_open"
    roundtrip_cost_pct: float = 0.5    # 수수료 + 거래세 왕복
    min_lot: int = 1                   # 최소 주문 단위


# 실적 추정치(컨센서스) 공급원. **미구현이다.**
#
# None 은 '아직 정하지 않았다'가 아니라 **무료로 쓸 수 있는 소스가 없다**는
# 조사 결론이다. 2026-09 확인: FnGuide·WISEfn·에프앤가이드는 유료이고,
# 네이버 금융의 컨센서스 화면은 스크래핑 금지 대상이며, DART 는 확정
# 실적만 준다(추정치 없음). FDR 에도 추정치 API 가 없다.
#
# 자리만 남겨두는 이유는 나중에 소스가 생겼을 때 붙일 지점을 명시하고,
# 그때까지 **추정치를 만들어 쓰지 않는다**는 것을 코드로 못박기 위함이다.
# 과거 실적 성장률로 미래를 대신하면 그건 추정치가 아니라 관성이다.
EARNINGS_SOURCE = None


# 뉴스 테마 키워드 사전. **기록 전용 지표(news_freq)에만 쓴다.**
#
# 키(테마명)는 `news.CATEGORY_RULES` 의 카테고리명과 **같은 어휘**를 쓴다.
# 어휘가 갈라지면 news.category 와 news_freq.sector 를 조인할 수 없고,
# 어느 쪽이 정본인지도 알 수 없게 된다. 값(키워드)도 CATEGORY_RULES 의
# 같은 카테고리와 일치시켰고, 스모크 테스트가 그 일치를 강제한다.
#
# 분류(classify)와 이 집계는 세는 방식이 다르다.
#   classify        첫 매치 하나만 남긴다(단일 라벨). 순서가 우선순위다.
#   news_freq       걸리는 테마마다 센다(다중 라벨). "반도체 실적" 기사는
#                   반도체와 실적 양쪽에 1건씩 잡힌다.
# 충돌이 아니라 의도된 차이다. 빈도는 관심도를 보려는 것이므로 한 기사가
# 두 테마를 건드리면 둘 다 세는 게 맞다.
#
# **주의: 여기서 말하는 '섹터'는 KRX 업종이 아니다.** `sector_metrics` 의
# sector 는 업종명 158종(tickers.sector)이고, 이쪽은 뉴스 테마 10종이다.
# 두 테이블을 scan_date 로 조인해도 sector 는 한 행도 맞지 않는다.
NEWS_THEME_KEYWORDS: dict[str, tuple[str, ...]] = {
    "반도체": ("반도체", "hbm", "d램", "디램", "낸드", "파운드리", "웨이퍼",
               "asml", "tsmc", "nvidia", "엔비디아", "gpu", "chip"),
    "2차전지": ("2차전지", "이차전지", "배터리", "양극재", "음극재", "전해질",
                "리튬", "니켈", "캐즘", "전기차", "ess", "battery"),
    "방산조선": ("방산", "무기", "미사일", "전차", "잠수함", "조선", "수주",
                 "lng운반선", "컨테이너선", "mro", "defense", "shipbuild"),
    "바이오": ("바이오", "임상", "신약", "기술수출", "라이선스", "fda",
               "adc", "cdmo", "제약", "biotech", "clinical"),
    "전력AI": ("전력", "변압기", "송전", "배전", "데이터센터", "원전",
               "smr", "ai인프라", "전선", "grid", "transformer"),
    "수급": ("공매도", "대차", "신용융자", "반대매매", "순매수", "순매도",
             "외국인", "기관", "프로그램매매", "블록딜", "지분"),
    "실적": ("실적", "어닝", "영업이익", "매출", "적자", "흑자", "턴어라운드",
             "컨센서스", "가이던스", "earnings", "guidance", "revenue"),
    "매크로": ("금리", "환율", "원달러", "국고채", "연준", "fomc", "cpi",
               "물가", "유가", "원자재", "구리", "달러", "인플레이션",
               "fed", "rate", "inflation", "yield"),
    "정책": ("관세", "수출규제", "제재", "보조금", "규제", "법안", "정부",
             "국회", "세제", "tariff", "sanction", "subsidy"),
    "공시": ("유상증자", "무상증자", "전환사채", "신주인수권", "교환사채",
             "자기주식", "자사주", "감사의견", "감사보고서", "관리종목",
             "상장폐지", "합병", "분할", "감자", "공급계약", "단일판매",
             "주요사항보고", "공시"),
}


@dataclass(frozen=True)
class NewsFreqConfig:
    """뉴스 빈도 지표 파라미터. **기록 전용이다.**

    사전은 `NEWS_THEME_KEYWORDS` 모듈 상수에 있다. frozen 데이터클래스에
    가변 dict 를 기본값으로 넣을 수 없어서 분리했다.
    """

    ma_days: int = 7      # 이동평균 구간. **달력일이다** (뉴스는 휴장일에도 난다)
    lookback_days: int = 30   # 집계 시 읽어올 뉴스 일자 수


@dataclass(frozen=True)
class SectorConfig:
    """섹터 지표 파라미터. **기록 전용 지표다.**

    여기서 나온 값은 `sector_metrics` 테이블에만 들어간다. 점수·추천·
    스크리닝·알림 어디에도 쓰지 않는다. 채점 표본이 쌓인 뒤 '어떤 섹터
    국면에서 통했나'를 되짚기 위한 것이고, 지금 판단에 쓰면 검증되지
    않은 축을 하나 더 얹는 셈이 된다.
    """

    rs_short: int = 5              # 단기 상대강도 구간 (거래일)
    rs_long: int = 20              # 장기 상대강도 구간. RS 순위의 기준
    breadth_ma: int = 20           # 폭 판정에 쓰는 이동평균
    new_high_lookback: int = 252   # 52주 ≈ 252거래일
    new_high_ratio: float = 0.99   # 최고 종가의 이 비율 이상이면 신고가로 본다
    turnover_ma: int = 20          # 거래대금 비중의 비교 기준 구간
    persist_lookback: int = 20     # 모멘텀 지속성: 몇 거래일 전과 대조하나
    persist_top_fraction: float = 1.0 / 3.0   # 상위 이 비율이면 '상위권'


# 배당주 필터 상수. 단계별 탈락 사유를 기록하려면 임계값이 코드 한 곳에
# 모여 있어야 한다. 필터가 빡빡한지 판단할 때 고칠 곳이 여기 하나다.
DIV_MIN_YIELD = 3.0      # F1 배당수익률 하한(%)
DIV_MIN_YEARS = 5        # F2 연속 배당 연수 (감액 없음 포함)
DIV_MAX_PAYOUT = 80.0    # F3 배당성향 상한(%)
DIV_TOP_N = 15           # 리포트 상위 N
# F6 업종 중앙값 비교에 필요한 최소 표본. 업종에 3종목만 있으면 중앙값이
# 사실상 자기 자신이라 필터가 의미를 잃는다. 그때는 F6 을 스킵한다.
DIV_SECTOR_MIN_MEMBERS = 5

# 주식 결제 주기. **영업일 기준 T+2** 다 (거래일이 아니다).
# 연말이 정확히 이 둘이 갈리는 지점이다 — 12월 31일은 증시는 휴장이지만
# 결제 영업일이다. 그래서 배당락 캘린더는 달력을 두 개 쓴다.
DIV_SETTLE_DAYS = 2

# 월간 배당 리포트 발송 요일. `datetime.weekday()` 규격(월=0 … 일=6).
# 5 = 토요일이고, **매월 첫 토요일**에만 보낸다.
#
# 토요일을 고른 이유는 추천 10선을 일요일로 고른 것과 같다(12장). 장이
# 닫혀 있어 그날 안에 주문을 낼 수 없으므로, 읽고 나서 최소 이틀의 숙려
# 시간이 구조적으로 강제된다. 월 1회인 이유는 배당 데이터가 사업보고서
# 시즌에만 바뀌기 때문이다 — 매주 보내면 같은 표를 네 번 보낸다.
DIV_REPORT_DOW = 5

# DPS 성장률(CAGR) 구간. 사업연도 수다. 구간이 5년이면 성장 기간은 4번이다.
DIV_CAGR_YEARS = 5

# 배당락 회복일수를 볼 과거 연수. **과거 통계이고 예측이 아니다.**
# 표본이 3건도 안 되고 그 사이 시장 국면이 달랐다. 리포트 컬럼명에
# '(참고)' 를 붙이는 이유다.
DIV_RECOVERY_YEARS = 3


@dataclass(frozen=True)
class DividendConfig:
    """배당주 필터 파라미터.

    **이 값들은 검증되지 않은 초기 설정이다.** 배당수익률 3%·성향 80%·
    5년 연속은 흔히 쓰이는 관행값이고, 이 저장소의 채점 표본으로 검증한
    수치가 아니다. 그래서 통과 종목이 0개로 나와도 상수를 먼저 완화하지
    않는다 — 몇 개가 어느 필터에서 떨어졌는지를 먼저 본다.
    """

    min_yield: float = DIV_MIN_YIELD
    min_years: int = DIV_MIN_YEARS
    max_payout: float = DIV_MAX_PAYOUT
    top_n: int = DIV_TOP_N
    sector_min_members: int = DIV_SECTOR_MIN_MEMBERS
    settle_days: int = DIV_SETTLE_DAYS
    report_dow: int = DIV_REPORT_DOW
    cagr_years: int = DIV_CAGR_YEARS
    recovery_years: int = DIV_RECOVERY_YEARS


# ══════════════════════════ 뉴스 중복 제거 ══════════════════════════
# 매체명 정규화표 (도메인 -> 표시명).
#
# Google News RSS 는 같은 매체를 피드마다 도메인으로도 주고 한글 이름으로도
# 준다. 표기가 갈리면 두 가지가 깨진다.
#   (1) 같은 기사가 두 건으로 남는다 — make_id 가 (정규화 제목|매체) 해시다.
#   (2) 클러스터의 '매체 수'가 부풀어 중요도가 따라 오른다.
#
# 이 표는 추측이 아니라 **같은 URL 을 공유한 source 짝**에서 자동 도출했다
# (2026-09-09 · news 2,316행 -> 67쌍). 포털/애그리게이터(네이트 · 네이버금융
# · v.daum.net 등)는 원 기사의 매체가 아니라 재배포처라 도출에서 뺐다.
#
# 표에 없는 도메인은 **그대로 둔다.** 도메인에서 한글 매체명을 기계적으로
# 만들어낼 방법은 없고, 틀린 이름을 지어내는 것보다 도메인이 그대로 보이는
# 편이 낫다. 새 짝이 보이면 여기에 줄을 추가하면 된다.
MEDIA_ALIASES: dict[str, str] = {
    "ajunews.com": "아주경제",
    "asiatoday.co.kr": "아시아투데이",
    "banronbodo.com": "반론보도닷컴",
    "biz.chosun.com": "Chosunbiz",
    "biz.heraldcorp.com": "헤럴드경제",
    "biz.sbs.co.kr": "SBS Biz",
    "blockmedia.co.kr": "블록미디어",
    "businesspost.co.kr": "비즈니스포스트",
    "cbci.co.kr": "CBC뉴스",
    "ceomagazine.co.kr": "CEONEWS",
    "choicenews.co.kr": "초이스경제",
    "chosundaily.com": "미주조선일보",
    "coinreaders.com": "코인리더스",
    "daily25news.com": "데일리25",
    "ddaily.co.kr": "디지털데일리",
    "digitaltoday.co.kr": "디지털투데이",
    "donga.com": "동아일보",
    "econovill.com": "ER 이코노믹리뷰",
    "ekoreanews.co.kr": "이코리아",
    "etoday.co.kr": "이투데이",
    "financialpost.co.kr": "파이낸셜포스트",
    "g-enews.com": "글로벌이코노믹",
    "goodkyung.com": "굿모닝경제",
    "greened.kr": "녹색경제신문",
    "hankyung.com": "한국경제",
    "hansbiz.co.kr": "한스경제",
    "hellot.net": "헬로티",
    "hkn24.com": "헬스코리아뉴스",
    "icnweb.kr": "아이씨엔매거진",
    "idomin.com": "경남도민일보",
    "ikld.kr": "국토일보",
    "infostockdaily.co.kr": "인포스탁데일리",
    "kbthink.com": "KB Think",
    "kidd.co.kr": "산업일보",
    "koreapost.co.kr": "코리아포스트 한글판",
    "kr.benzinga.com": "Benzinga",
    "livebiz.today": "생생비즈플러스",
    "m-economynews.com": "M이코노미뉴스",
    "m-i.kr": "매일일보",
    "market-ink.co.kr": "마켓잉크",
    "medworld.co.kr": "메드월드뉴스",
    "moneyneversleeps.co.kr": "머니네버슬립",
    "mt.co.kr": "머니투데이",
    "namdonews.com": "남도일보",
    "newneek.co": "뉴닉",
    "news.einfomax.co.kr": "연합인포맥스",
    "news.mtn.co.kr": "MTN 머니투데이방송",
    "news2day.co.kr": "뉴스투데이",
    "newspim.com": "뉴스핌",
    "pinpointnews.co.kr": "핀포인트뉴스",
    "polinews.co.kr": "폴리뉴스 Polinews",
    "sankyungtoday.com": "산경투데이",
    "sanupin-news.kr": "산업인뉴스",
    "sedaily.com": "서울경제",
    "segye.com": "세계일보",
    "segyebiz.com": "세계비즈",
    "seoul.co.kr": "서울신문",
    "sisajournal-e.com": "시사저널e",
    "smedaily.co.kr": "중소기업신문",
    "specialtimes.co.kr": "스페셜타임스",
    "speconomy.com": "스페셜경제",
    "straightnews.co.kr": "스트레이트뉴스",
    "techflowpost.com": "深潮TechFlow",
    "thebigdata.co.kr": "빅데이터뉴스",
    "theviewers.co.kr": "뷰어스",
    "topstarnews.net": "톱스타뉴스",
    "youthdaily.co.kr": "청년일보",
    # ── 아래는 위 자동 도출에 잡히지 않았으나 도메인이 자명한 국내 주요
    #    매체다. 같은 URL 짝이 관측되지 않았을 뿐이라 손으로 넣었다. ──
    "edaily.co.kr": "이데일리",
    "mk.co.kr": "매일경제",
    "chosun.com": "조선일보",
    "joongang.co.kr": "중앙일보",
    "yna.co.kr": "연합뉴스",
    "newsis.com": "뉴시스",
    "news1.kr": "뉴스1",
    "fnnews.com": "파이낸셜뉴스",
    "asiae.co.kr": "아시아경제",
    "etnews.com": "전자신문",
    "inews24.com": "아이뉴스24",
    "heraldcorp.com": "헤럴드경제",
    "dt.co.kr": "디지털타임스",
    "hani.co.kr": "한겨레",
    "khan.co.kr": "경향신문",
    "munhwa.com": "문화일보",
}

# 대표 기사 선정에 쓰는 주요 매체. 순위표가 아니라 집합이다 — 이 안에
# 들면 대표 후보에서 한 칸 앞선다. 매체에 점수를 매기는 게 아니므로
# 세부 서열은 두지 않는다. (교리: 뉴스에는 점수를 매기지 않는다)
MAJOR_MEDIA: frozenset[str] = frozenset({
    "연합뉴스", "연합인포맥스", "뉴시스", "뉴스1", "매일경제", "매일경제 마켓",
    "한국경제", "서울경제", "이데일리", "파이낸셜뉴스", "머니투데이",
    "아시아경제", "헤럴드경제", "조선일보", "Chosunbiz", "중앙일보",
    "동아일보", "전자신문", "비즈니스포스트", "인포스탁데일리",
    "DART", "네이버금융", "Yahoo Finance", "Reuters", "Bloomberg",
})


# ══════════════════════════ DART 조회 시장 ══════════════════════════
# Open DART list.json 의 corp_cls.  Y=유가(코스피)  K=코스닥  N=코넥스  E=기타
#
# 기본은 코스피만이다. 코스닥을 켜려면 ("Y", "K") 로 바꾼다. 코넥스(N)는
# 유니버스에 없으므로 어떤 경우에도 넣지 않는다 — flags.dart_disclosure_events
# 와 news_sources.collect_dart 가 이 튜플을 **순서대로** 돈다.
#
# 켜는 절차 (한 줄): 아래 두 값을 바꾸고 커밋하면 다음 nightly(21:30)부터
# 적용된다. 켠 날짜는 채점 데이터를 나누는 기준일이 되므로 반드시 적는다 —
# 그날 이전 스냅샷은 코스닥 DART 플래그(증자·감사의견)가 비어 있는 상태로
# 채점된 것이다.
DART_MARKETS: tuple[str, ...] = ("Y", "K")
DART_KOSDAQ_ENABLED_ON: str | None = "2026-09-10"   # 코스닥 ON + 코스피 페이지 정상화 기준일

# 시장별 list.json 최대 페이지 (page_count=100 기준). flags 가 60일을 소급한다.
#
# 2026-09-09 실측 (60일 소급, 100건/페이지):
#     corp_cls=Y  total_count 10,474  -> 105페이지   1.2초/호출
#     corp_cls=K  total_count 11,266  -> 113페이지   0.9초/호출
#
# ★ 2026-09-09 까지 Y 는 20 이었다 — 실측 필요량(105)의 1/5 라 최근 약
#   11일치만 읽고 나머지 49일은 조용히 버려졌다. 코스닥 확장 중 발견한
#   기존 결함이고, 2026-09-10 코스닥 ON 과 같은 날 160 으로 정상화했다.
#   그래서 그날 이전 스냅샷은 코스피 증자·감사의견 플래그도 11일치 기준이다
#   (DART_KOSDAQ_ENABLED_ON 이 두 변경 공통의 채점 구분 기준일).
# 둘 다 실측의 1.5배. 3월(사업보고서)·8월(반기)에는 flags 로그의
# '페이지 상한 도달' 경고를 보고 올린다.
DART_LIST_MAX_PAGES: dict[str, int] = {"Y": 160, "K": 170}
# 뉴스 수집(news_sources.collect_dart)은 하루치만 본다.
# 2026-09-09 실측(1일): Y 127건=2페이지, K 215건=3페이지. 5 면 충분하다.
DART_NEWS_MAX_PAGES: dict[str, int] = {"Y": 5, "K": 5}


# ══════════════════════════ 관심종목 (표시·태깅 전용) ══════════════════════════
# 외부에서 받은 관심종목. **보유가 아니다** — positions 에 넣지 않는다.
#
# 쓰는 곳은 둘뿐이다.
#   renderer.render_watchlist   저녁 브리핑 '📌 관심종목 현황' 섹션 (팩트만)
#   news.held_codes             뉴스 중요도의 '내 종목' 자리에 연결 (브리핑 정렬)
# 점수·스크리닝·추천·채점·청산 어디에서도 참조하지 않는다. smoke 의
# [watchlist] 검사가 screener/daily/exits 등에서 이 이름이 나오면 실패한다.
#
# 코드는 화면 캡처 OCR 출처라 오독 가능성이 있었다. 2026-09-09 에 43건 전부를
# pykrx(KRX 원천)·네이버금융·로컬 tickers 세 소스로 이름 대조해 43/43 일치를
# 확인하고 등록했다 (FDR StockListing 은 KRX 404 로 당시 사용 불가).
# 이전에 OCR 코드가 실존하는 다른 회사를 가리켜 에러 없이 통과한 사고가
# 있었다 — 코드를 추가할 때는 반드시 이름 대조를 먼저 한다.
#
# ETF/ETN/ADR/레버리지/인버스/우선주는 의도적으로 뺐다. 추가하지 않는다.
WATCHLIST: dict[str, list[tuple[str, str]]] = {
    "반도체": [
        ("403870", "HPSP"),
        ("003160", "디아이"),
        ("009150", "삼성전기"),
    ],
    "전지": [
        ("450080", "에코프로머티"),
        ("054210", "이랜텍"),
        ("093370", "후성"),
        ("005070", "코스모신소재"),
        ("066970", "엘앤에프"),
        ("086520", "에코프로"),
        ("078600", "대주전자재료"),
    ],
    "전력": [
        ("033100", "제룡전기"),
        ("217590", "티엠씨"),
        ("267260", "HD현대일렉트릭"),
        ("298040", "효성중공업"),
        ("034020", "두산에너빌리티"),
        ("001440", "대한전선"),
        ("010120", "LS ELECTRIC"),
    ],
    "방산": [
        ("005810", "풍산홀딩스"),
        ("103140", "풍산"),
        ("079550", "LIG디펜스앤에어로스페이스"),
        ("012450", "한화에어로스페이스"),
        ("005870", "휴니드"),
        ("064350", "현대로템"),
    ],
    "인공지능로봇": [
        ("034220", "LG디스플레이"),
        ("130580", "나이스디앤비"),
        ("454910", "두산로보틱스"),
        ("035420", "NAVER"),
        ("058610", "에스피지"),
        ("282880", "코윈테크"),
        ("389500", "에스비비테크"),
    ],
    "건설자동차": [
        ("018500", "동원모빌리티"),
        ("005380", "현대차"),
        ("071970", "HD현대마린엔진"),
        ("307950", "현대오토에버"),
    ],
    "대형주": [
        ("066570", "LG전자"),
        ("005930", "삼성전자"),
        ("000660", "SK하이닉스"),
    ],
    "바이오": [
        ("298380", "에이비엘바이오"),
        ("397030", "에이프릴바이오"),
        ("196170", "알테오젠"),
        ("141080", "리가켐바이오"),
        ("207940", "삼성바이오로직스"),
        ("068270", "셀트리온"),
    ],
}


def watchlist_codes() -> dict[str, tuple[str, str]]:
    """{종목코드: (이름, 섹터)}. 표시와 뉴스 태깅이 같은 역인덱스를 쓴다."""
    out: dict[str, tuple[str, str]] = {}
    for sector, items in WATCHLIST.items():
        for code, name in items:
            out.setdefault(str(code).zfill(6), (name, sector))
    return out


@dataclass(frozen=True)
class NewsDedupConfig:
    """뉴스 중복 제거 3층 파라미터.

    3층(같은 사건 묶기)의 임계값은 픽스처
    `tests/fixtures/news_brief_20260909.json` (187건) 으로 맞췄다.
    바꾸면 그 회귀 검사부터 다시 보십시오.

    왜 Jaccard 하나로 안 되는가
    --------------------------
    실측한 사례:
      A "중동 확전 우려 뉴욕증시 하락…유가 100달러 육박·엔화 급등"
      B "뉴욕증시, 국제 유가 상승에 하락 출발…다우, 0.88%↓"
    사람이 보면 같은 사건인데 토큰 Jaccard 는 **0.214** 다. 공통 토큰이
    {뉴욕증시, 유가, 하락} 3개인데 합집합이 14개라 분모가 커진다. 이걸
    묶으려고 임계값을 0.21 까지 내리면 관계없는 기사들이 줄줄이 붙는다
    (실측: 오병합 후보가 5개 -> 12개).

    그래서 두 번째 통로를 둔다. **공통 토큰이 충분히 많고(>=3) 짧은 쪽
    제목의 상당 부분(>=0.40)이 겹치면** 같은 사건으로 본다. 위 사례는
    포함도 3/7 = 0.429 로 통과한다. 긴 제목이 짧은 제목을 삼키는 것을
    막기 위해 절대 개수(min_shared_tokens)를 함께 걸었다.
    """

    sim_threshold: float = 0.50       # 1통로: 토큰 Jaccard
    min_shared_tokens: int = 3        # 2통로: 공통 토큰 최소 개수
    min_containment: float = 0.40     # 2통로: 짧은 쪽 기준 포함도
    seed_hours: int = 48              # 배치 경계를 넘기 위한 씨앗 조회 구간


@dataclass(frozen=True)
class Config:
    ma: MAConfig = MAConfig()
    fib: FibConfig = FibConfig()
    credit: CreditConfig = CreditConfig()
    gate: GateConfig = GateConfig()
    exit: ExitConfig = ExitConfig()
    audit: AuditConfig = AuditConfig()
    sector: SectorConfig = SectorConfig()
    news_freq: NewsFreqConfig = NewsFreqConfig()
    news_dedup: NewsDedupConfig = NewsDedupConfig()
    dividend: DividendConfig = DividendConfig()


DEFAULT = Config()
