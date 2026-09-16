# -*- coding: utf-8 -*-
"""뉴스 정리 엔진.

교리: 뉴스 자체에는 점수가 없다
------------------------------
호재/악재를 기계가 판정하지 않는다. 이 모듈이 하는 일은 '정리'다.

  1) 정규화   제목에서 매체 꼬리표·대괄호 태그·중복 공백을 벗긴다
  2) 중복제거 정규화 제목 해시로 같은 기사를 합친다
  3) 클러스터 여러 매체가 다룬 같은 사건을 하나로 묶는다
  4) 종목매핑 제목에서 유니버스 종목명을 찾아 태깅한다
  5) 분류     키워드 룰로 카테고리를 붙인다
  6) 중요도   '몇 개 매체가 다뤘나' 를 1순위 신호로 쓴다

중요도 설계의 핵심
-----------------
헤드라인의 자극성은 중요도가 아니다. 가장 신뢰할 만한 신호는
**같은 사건을 몇 개 매체가 동시에 다뤘는가**다. 단독 기사는 낮게,
여러 매체가 붙은 사건은 높게 본다. 여기에 '내 보유·추천 종목인가'를
얹으면 형님이 실제로 읽어야 할 순서가 나온다.

감정 분석이나 목표가 추정은 하지 않는다. 대신 뉴스가 붙은 종목의
차트 점수를 브리핑에 병기해서, 판단은 사람이 하게 한다.
"""
from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlsplit, urlunsplit

import pandas as pd

from .config import (DEFAULT, MAJOR_MEDIA, MEDIA_ALIASES,
                     Config, NewsDedupConfig, watchlist_codes)

log = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))

__all__ = ["normalize_title", "make_id", "normalize_url", "canonical_source",
           "event_tokens", "same_event", "representative_rank", "classify",
           "cluster_items", "build_alias_index", "map_tickers",
           "score_importance", "held_codes", "process_and_store",
           "theme_shift"]


def _to_kst(dt: datetime | None) -> datetime | None:
    """tz 정보가 없는 값이 흘러들어오면 UTC 로 간주해 붙인다.

    naive datetime 에 astimezone() 을 걸면 파이썬이 '서버 로컬 시간'으로
    해석한다. UTC 서버에서 발행시각이 9시간 어긋나므로 명시적으로 막는다.
    """
    if not isinstance(dt, datetime):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(KST)


# ══════════════════════════ 1. 정규화 ══════════════════════════
_BRACKET = re.compile(r"[\[\(【〔<]([^\]\)】〕>]{0,20})[\]\)】〕>]")
_MEDIA_TAIL = re.compile(r"\s*[-|·–—]\s*[가-힣A-Za-z0-9. ]{2,20}$")
_NOISE = re.compile(r"(종합|1보|2보|3보|속보|단독|영상|포토|표|LIVE|마감|"
                    r"업데이트|재송|정정)")
_NONWORD = re.compile(r"[^0-9A-Za-z가-힣 ]+")
_SPACES = re.compile(r"\s+")


def normalize_title(title: str, strip_media_tail: bool = True) -> str:
    """제목 정규화. 중복 판정과 클러스터링의 기준이 된다."""
    if not title:
        return ""
    t = title.strip()
    if strip_media_tail:
        t = _MEDIA_TAIL.sub("", t)
    t = _BRACKET.sub(" ", t)
    t = _NOISE.sub(" ", t)
    t = _NONWORD.sub(" ", t)
    t = _SPACES.sub(" ", t).strip().lower()
    return t


def make_id(title_norm: str, source: str) -> str:
    """같은 매체의 같은 기사는 하나. 매체가 다르면 별건으로 남긴다.

    매체까지 해시에 넣는 이유는 클러스터 크기(= 다룬 매체 수)를
    중요도 신호로 써야 하기 때문이다. 매체를 합쳐버리면 그 정보가 사라진다.
    """
    raw = f"{title_norm}|{(source or '').strip().lower()}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]


# ─────────────── URL 정규화 (중복 판정 전용) ───────────────
# 같은 기사가 매체 표기만 달리해 두 건으로 남는다. 2026-09-09 실측:
# 'digitaltoday.co.kr' 과 '디지털투데이' 가 **같은 Google URL** 로 각각
# 저장돼 아침 브리핑 '주요 공시' 5칸 중 4칸을 기사 두 개가 차지했다.
# 같은 URL 인데 행이 갈린 것이 120건이었다.
#
# make_id 에서 매체를 빼면 해결되지만 그러면 매체 수 집계가 죽는다 —
# '몇 개 매체가 다뤘나' 가 중요도 1순위 신호다. 그래서 매체는 해시에
# 그대로 두고, URL 을 **별도 키**로 세워 같은 URL 을 한 건만 남긴다.
#
# 추적 파라미터는 뗀다. Google News RSS 는 같은 기사에 oc=5 를 붙였다
# 말았다 하고, utm_* 는 유입 경로일 뿐 기사 동일성과 무관하다.
_TRACKING = re.compile(r"(?:oc|ved|usg|fbclid|gclid|utm_[a-z_]+)\Z", re.I)


def normalize_url(url: str | None) -> str:
    """중복 판정용 URL 키. 표시와 링크에는 언제나 원본을 쓴다."""
    if not url:
        return ""
    raw = str(url).strip()
    try:
        p = urlsplit(raw)
    except ValueError:
        return raw.lower()
    if not p.netloc:
        return raw.lower()
    host = p.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    query = "&".join(
        f"{k}={v}" for k, v in sorted(parse_qsl(p.query, keep_blank_values=True))
        if not _TRACKING.match(k))
    return urlunsplit((p.scheme.lower() or "https", host,
                       p.path.rstrip("/"), query, ""))


# ─────────────── 2층: 매체명 정규화 ───────────────
_DOMAINISH = re.compile(r"^[a-z0-9][a-z0-9.-]*\.[a-z]{2,}$", re.I)


def canonical_source(source: str | None,
                     aliases: dict | None = None) -> str:
    """매체 표기를 하나로 모은다. 'digitaltoday.co.kr' -> '디지털투데이'.

    Google News 는 같은 매체를 피드마다 도메인으로도, 한글 이름으로도 준다.
    표기가 갈리면 (1) 같은 기사가 두 건으로 남고 (2) 클러스터 매체 수가
    부풀어 중요도가 따라 오른다.

    표(config.MEDIA_ALIASES)에 없는 도메인은 **그대로 돌려준다.** 도메인에서
    한글 매체명을 기계적으로 만들 방법은 없다. 틀린 이름을 지어내느니
    도메인이 보이는 편이 낫고, 새 짝은 표에 한 줄 넣으면 된다.
    """
    s = (source or "").strip()
    if not s:
        return ""
    table = MEDIA_ALIASES if aliases is None else aliases
    key = s.lower()
    if key in table:
        return table[key]
    if _DOMAINISH.match(s):
        bare = key[4:] if key.startswith("www.") else key
        if bare in table:
            return table[bare]
    return s


# ══════════════════════════ 2. 카테고리 ══════════════════════════
# 순서가 우선순위다. 앞에서 걸리면 뒤는 안 본다.
CATEGORY_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("공시", ("유상증자", "무상증자", "전환사채", "신주인수권", "교환사채",
             "자기주식", "자사주", "감사의견", "감사보고서", "관리종목",
             "상장폐지", "합병", "분할", "감자", "공급계약", "단일판매",
             "주요사항보고", "공시")),
    ("수급", ("공매도", "대차", "신용융자", "반대매매", "순매수", "순매도",
             "외국인", "기관", "프로그램매매", "블록딜", "지분")),
    ("실적", ("실적", "어닝", "영업이익", "매출", "적자", "흑자", "턴어라운드",
             "컨센서스", "가이던스", "earnings", "guidance", "revenue")),
    ("매크로", ("금리", "환율", "원달러", "국고채", "연준", "fomc", "cpi",
               "물가", "유가", "원자재", "구리", "달러", "인플레이션",
               "fed", "rate", "inflation", "yield")),
    ("정책", ("관세", "수출규제", "제재", "보조금", "규제", "법안", "정부",
             "국회", "세제", "tariff", "sanction", "subsidy")),
    ("반도체", ("반도체", "hbm", "d램", "디램", "낸드", "파운드리", "웨이퍼",
               "asml", "tsmc", "nvidia", "엔비디아", "gpu", "chip")),
    ("2차전지", ("2차전지", "이차전지", "배터리", "양극재", "음극재", "전해질",
                "리튬", "니켈", "캐즘", "전기차", "ess", "battery")),
    ("방산조선", ("방산", "무기", "미사일", "전차", "잠수함", "조선", "수주",
                 "lng운반선", "컨테이너선", "mro", "defense", "shipbuild")),
    ("바이오", ("바이오", "임상", "신약", "기술수출", "라이선스", "fda",
               "adc", "cdmo", "제약", "biotech", "clinical")),
    ("전력AI", ("전력", "변압기", "송전", "배전", "데이터센터", "원전",
               "smr", "ai인프라", "전선", "grid", "transformer")),
    ("해외시황", ("나스닥", "다우", "s&p", "미국증시", "뉴욕증시", "니케이",
                 "상하이", "항셍", "nasdaq", "dow")),
    ("국내시황", ("코스피", "코스닥", "증시", "지수", "장마감", "장중")),
)


def classify(title: str, hint: str | None = None) -> str:
    """카테고리 판정. 힌트가 있고 룰이 안 걸리면 힌트를 쓴다."""
    low = (title or "").lower()
    for cat, keys in CATEGORY_RULES:
        for k in keys:
            if k in low:
                return cat
    return hint or "기타"


# ══════════════════════════ 3. 클러스터링 ══════════════════════════
_STOP = {"이", "그", "저", "및", "등", "the", "a", "of", "to", "in", "for",
         "on", "and", "is", "억", "원", "만", "천", "조"}

# 조사·어미. 긴 것부터 떼야 '에서'가 '에'로 먼저 잘리지 않는다.
#
# 한국어 헤드라인은 같은 사건을 써도 조사가 달라붙어 토큰이 어긋난다.
# '유가 급등에' 와 '유가 상승에' 는 '급등에'/'상승에' 로 남아 '유가' 만
# 겹치는 식이다. 형태소 분석기를 넣지 않고(의존성 추가 금지) 꼬리만 뗀다.
_JOSA = ("으로부터", "에서부터", "에서는", "으로는", "에서도", "으로", "에서",
         "에는", "에게", "한테", "까지", "부터", "보다", "이나", "마다",
         "처럼", "같이", "은", "는", "이", "가", "을", "를", "에", "의",
         "와", "과", "도", "만", "로")
# 순수 숫자 토큰(등락률·지수·시각)은 사건 식별에 쓸모가 없다. 같은 사건을
# 다룬 기사들이 '1 18' / '0 88' 처럼 서로 다른 숫자를 달고 나오므로
# 남겨두면 분모만 키워 유사도를 떨어뜨린다.
_NUMERIC = re.compile(r"^\d+$")


def _strip_josa(word: str) -> str:
    for j in _JOSA:
        if len(word) > len(j) + 1 and word.endswith(j):
            return word[: -len(j)]
    return word


def event_tokens(title_norm: str) -> set[str]:
    """사건 비교용 토큰. 조사를 떼고 순수 숫자를 버린다."""
    out: set[str] = set()
    for w in (title_norm or "").split():
        if _NUMERIC.match(w):
            continue
        w = _strip_josa(w)
        if len(w) > 1 and w not in _STOP:
            out.add(w)
    return out


def _tokens(title_norm: str) -> set[str]:
    """옛 이름. event_tokens 로 대체됐다."""
    return event_tokens(title_norm)


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    return inter / (len(a) + len(b) - inter)


# 회차 식별자. '제22회 전환사채' 와 '제25회 전환사채' 는 문구가 거의
# 같지만 **다른 공시**다. 회차가 서로 다르면 유사도가 아무리 높아도
# 묶지 않는다. 이 가드가 없으면 두 건의 Jaccard 가 0.45, 포함도가 0.625 라
# 2통로로 붙어버린다.
_SERIAL = re.compile(r"(?:제\s*)?(\d+)\s*(?:회차|회|차)(?![가-힣])")


def event_serials(title: str | None) -> set[str]:
    """제목에서 회차 번호를 뽑는다. 없으면 빈 집합."""
    return set(_SERIAL.findall(title or ""))


def same_event(a: set[str], b: set[str],
               cfg: NewsDedupConfig | None = None) -> float:
    """두 제목이 같은 사건인가. 0.0 이면 아니다.

    통로가 둘이다.
      ① 토큰 Jaccard >= sim_threshold
      ② 공통 토큰 >= min_shared_tokens **그리고**
         짧은 쪽 기준 포함도 >= min_containment

    ②가 필요한 이유는 NewsDedupConfig 독스트링에 실측과 함께 적어 뒀다.
    한 줄로 줄이면: 같은 사건이라도 한쪽 제목이 길면 Jaccard 의 분모가
    커져 0.21 까지 떨어진다.
    """
    cfg = cfg or DEFAULT.news_dedup
    if not a or not b:
        return 0.0
    inter = len(a & b)
    if not inter:
        return 0.0
    jac = inter / (len(a) + len(b) - inter)
    if jac >= cfg.sim_threshold:
        return jac
    cont = inter / min(len(a), len(b))
    if inter >= cfg.min_shared_tokens and cont >= cfg.min_containment:
        return cont
    return 0.0


def representative_rank(row, has_ticker: bool = False,
                        major: frozenset | None = None) -> tuple:
    """대표 기사 정렬 키. **작을수록** 대표에 가깝다.

    내 종목 언급 > 주요 매체 > 먼저 수집된 것.

    매체에 점수를 매기는 게 아니다. 같은 사건을 여럿이 썼을 때 어느 한
    줄을 보여줄지 고르는 것뿐이고, 중요도 산정에는 관여하지 않는다.
    """
    major = MAJOR_MEDIA if major is None else major
    src = canonical_source(row.get("source"))
    when = str(row.get("collected") or row.get("published") or "~")
    return (0 if has_ticker else 1, 0 if src in major else 1, when)


def cluster_items(items: list[dict], cfg: NewsDedupConfig | None = None,
                  seeds: list[dict] | None = None) -> list[dict]:
    """같은 사건을 묶는다 (3층). 탐욕적 단일 패스 클러스터링.

    건수가 하루 수백 건 수준이라 O(n x k) 로 충분하다. 대표 기사는
    클러스터에서 가장 먼저 등장한 항목으로 두고, cluster_n 에 매체 수를 센다.

    seeds
      이미 DB 에 있는 최근 항목 `[{id, title_norm, cluster_id, source}]`.

      **클러스터가 배치 경계를 넘게 하는 장치다.** 이 인자가 없던 판에서는
      수집 실행마다 클러스터를 처음부터 다시 만들었다. 아침 브리핑은 최근
      16시간을 읽는데 그 창에는 수집 실행이 두세 번 들어간다. 그래서 같은
      기사가 실행마다 다른 cluster_id 를 받았고, 렌더러의 클러스터 중복
      제거를 그대로 통과했다. 2026-09-09 실측: 같은 URL 인데 클러스터가
      갈린 것 120건, 같은 정규화 제목인데 갈린 것 142건.

      배치에 이미 있는 id 는 씨앗에서 건너뛴다. 같은 행을 두 번 세면
      매체 수가 부풀고 중요도가 따라 오른다.

    가드
      회차가 서로 다르거나(제22회 CB vs 제25회 CB), 태깅된 종목이 겹치지
      않으면 유사도를 보지 않고 자른다. 3층 임계를 낮추면 이 두 가지가
      가장 먼저 오병합으로 나타난다.
    """
    cfg = cfg or DEFAULT.news_dedup
    # (토큰, 회차, 종목코드, cluster_id)
    reps: list[tuple[set[str], set[str], set[str], str]] = []
    sizes: dict[str, int] = {}
    sources: dict[str, set[str]] = {}

    def _codes(d) -> set[str]:
        return {c for c, _ in (d.get("tickers") or ())}

    batch_ids = {it.get("id") for it in items}
    for sd in (seeds or []):
        cid = sd.get("cluster_id") or sd.get("id")
        if not cid or sd.get("id") in batch_ids:
            continue
        reps.append((event_tokens(sd.get("title_norm") or ""),
                     event_serials(sd.get("title")), _codes(sd), cid))
        sizes[cid] = sizes.get(cid, 0) + 1
        sources.setdefault(cid, set()).add(canonical_source(sd.get("source")))

    for it in items:
        tok = event_tokens(it.get("title_norm", ""))
        ser = event_serials(it.get("title"))
        cod = _codes(it)
        cid, best = None, 0.0
        for rtok, rser, rcod, rcid in reps:
            # 가드 — 유사도를 보기 전에 자른다.
            if ser and rser and not (ser & rser):
                continue          # 회차가 다르면 다른 공시다
            if cod and rcod and not (cod & rcod):
                continue          # 태깅된 종목이 겹치지 않으면 다른 사건이다
            s = same_event(tok, rtok, cfg)
            if s > best:
                best, cid = s, rcid
        if cid is None:
            cid = it["id"]
            reps.append((tok, ser, cod, cid))
        it["cluster_id"] = cid
        sizes[cid] = sizes.get(cid, 0) + 1
        sources.setdefault(cid, set()).add(canonical_source(it.get("source")))

    for it in items:
        cid = it["cluster_id"]
        # 매체 수를 센다. 같은 매체의 후속 기사는 중복으로 보지 않는다.
        # 표기 정규화를 거치므로 '디지털투데이'와 'digitaltoday.co.kr'이
        # 두 매체로 세어지지 않는다.
        it["cluster_n"] = max(len({s for s in sources.get(cid, ()) if s}), 1)
        it["cluster_items"] = sizes.get(cid, 1)
    return items


# ══════════════════════════ 4. 종목 매핑 ══════════════════════════
# 2글자 종목명은 일반 단어와 충돌해 오탐이 심하다. 화이트리스트만 허용.
_SHORT_NAME_OK = {"한올", "대주", "삼진", "일진", "동원", "선진"}
# 아래 이름은 문맥 없이 매칭하면 거의 항상 오탐이다.
_AMBIGUOUS = {"미래", "대한", "한국", "우리", "신한", "하나", "삼성", "현대",
              "국제", "동양", "서울", "부산", "제일", "고려", "태양", "자이언트"}


class AliasIndex:
    """종목명 -> 종목코드 역인덱스.

    이름 목록을 길이 내림차순으로 **한 번만** 정렬해 둔다. 뉴스 건당
    3,000개 이름을 매번 정렬하면 수백 건 처리에 수십 초가 날아간다.
    긴 이름을 먼저 맞추는 이유는 '현대차'가 '현대차증권'을 잘라먹는
    부분 겹침을 막기 위함이다.
    """

    __slots__ = ("map", "names_by_len", "by_code")

    def __init__(self, mapping: dict):
        self.map = mapping
        self.names_by_len = sorted(mapping, key=len, reverse=True)
        self.by_code = {code: name for code, name in mapping.values()}

    def __len__(self) -> int:
        return len(self.map)


def build_alias_index(tickers: dict) -> AliasIndex:
    """우선주(코드 끝자리 != 0)는 본주와 중복 신호를 만들므로 제외한다."""
    idx: dict[str, tuple[str, str]] = {}
    for code, name in tickers.items():
        if not code.endswith("0"):
            continue
        n = (name or "").strip()
        if len(n) < 2:
            continue
        if len(n) == 2 and n not in _SHORT_NAME_OK:
            continue
        if n in _AMBIGUOUS:
            continue
        idx[n] = (code, n)
        # 공백 제거형도 함께 (예: 'HD 현대중공업' 표기 대응)
        compact = n.replace(" ", "")
        if compact != n and len(compact) >= 3:
            idx[compact] = (code, n)
    return AliasIndex(idx)


def map_tickers(title: str, alias_idx: AliasIndex,
                extra_names: list | None = None,
                stock_code: str | None = None) -> list[tuple[str, str]]:
    """제목에서 종목을 찾는다. 긴 이름을 먼저 맞춰 부분 겹침을 피한다."""
    found: dict[str, str] = {}

    # DART 공시는 종목코드를 직접 준다. 가장 정확한 경로다.
    if stock_code and stock_code in alias_idx.by_code:
        found[stock_code] = alias_idx.by_code[stock_code]

    # 매칭된 구간은 마스킹한다. 이걸 빼면 '현대차증권' 기사에 '현대차'가
    # 함께 태깅된다. 긴 이름 우선 정렬만으로는 막을 수 없다. 코드 기준
    # setdefault 는 같은 종목의 중복만 막고, 다른 종목의 부분 겹침은 못 막는다.
    hay = title or ""
    for name in alias_idx.names_by_len:
        if name in hay:
            code, official = alias_idx.map[name]
            found.setdefault(code, official)
            hay = hay.replace(name, "\x00" * len(name))

    for extra in (extra_names or []):
        e = (extra or "").strip()
        if e and e in alias_idx.map:
            code, official = alias_idx.map[e]
            found.setdefault(code, official)

    return list(found.items())


# ══════════════════════════ 5. 중요도 ══════════════════════════
KEYWORD_WEIGHT: tuple[tuple[float, tuple[str, ...]], ...] = (
    (2.0, ("상장폐지", "감사의견", "관리종목", "거래정지", "횡령", "배임")),
    (1.8, ("유상증자", "전환사채", "무상감자", "유상감자")),
    (1.6, ("수주", "공급계약", "단일판매", "기술수출", "라이선스")),
    (1.4, ("어닝 서프라이즈", "어닝쇼크", "적자전환", "흑자전환", "턴어라운드")),
    (1.2, ("fomc", "금리 인하", "금리 인상", "관세", "수출규제", "제재")),
    (1.0, ("공매도", "반대매매", "블록딜", "자사주", "무상증자", "배당")),
    (0.8, ("상한가", "하한가", "급등", "급락", "신고가", "신저가")),
)


def score_importance(item: dict, held: set[str], recommended: set[str],
                     universe: set[str]) -> float:
    """0~10 중요도.

    1순위 신호는 매체 수(cluster_n)다. 2순위는 내 종목인지 여부다.
    헤드라인의 자극성은 마지막에 조금만 반영한다.
    """
    low = (item.get("title") or "").lower()
    score = 1.0

    # ① 매체 수 (최대 2.0) — 여러 매체가 붙은 사건일수록 실체가 있다
    score += 0.5 * min(max(int(item.get("cluster_n", 1)) - 1, 0), 4)

    # ② 내 종목인가 (최대 2.5)
    codes = {c for c, _ in item.get("tickers", ())}
    if codes & held:
        score += 2.5
    elif codes & recommended:
        score += 1.5
    elif codes & universe:
        score += 0.8

    # ③ 키워드 강도 (최대 2.0)
    kw = 0.0
    for w, keys in KEYWORD_WEIGHT:
        if any(k in low for k in keys):
            kw = max(kw, w)
    score += min(kw, 2.0)

    # ④ 공시·매크로 가산 (최대 1.0)
    cat = item.get("category")
    if cat == "공시":
        score += 1.0
    elif cat in ("매크로", "정책"):
        score += 0.6

    # ⑤ 신선도 감점 — 24시간 넘은 기사는 브리핑 가치가 떨어진다
    pub = _to_kst(item.get("published"))
    if pub is not None:
        age_h = (datetime.now(KST) - pub).total_seconds() / 3600
        if age_h > 24:
            score -= 1.0
        elif age_h > 12:
            score -= 0.5

    return float(max(0.0, min(10.0, round(score, 2))))


def held_codes(store) -> set[str]:
    """중요도의 '내 종목' 집합 = 보유 종목 ∪ 관심종목.

    관심종목은 보유가 아니다(positions 에 없다). 하지만 브리핑에서 먼저
    읽고 싶은 종목이라는 점은 같으므로, 중요도 규칙 '몇 개 매체 + 내 종목'
    의 '내 종목' 자리에 함께 넣는다. 중요도 공식 자체는 그대로다.
    """
    held: set[str] = set()
    try:
        pos = store.list_positions() if hasattr(store, "list_positions") else []
        held = {p.ticker for p in pos}
    except Exception:  # noqa: BLE001 - positions 테이블 미도입 상태 허용
        held = set()
    return held | set(watchlist_codes())


# ══════════════════════════ 6. 파이프라인 ══════════════════════════
def process_and_store(store, raw_items: list[dict],
                      summarize_hook=None,
                      cfg: Config | None = None) -> dict:
    """수집 원본 -> 정규화/클러스터/태깅/채점 -> DB 저장.

    summarize_hook(clusters: dict[str, list[dict]]) -> dict[cluster_id, str]
      LLM 요약을 붙이고 싶을 때만 주입한다. 기본은 원문 제목 그대로다.
      해외 영문 기사를 번역/요약하려면 여기를 쓰면 된다.
    """
    if not raw_items:
        return {"collected": 0, "stored": 0, "links": 0}

    today = datetime.now(KST).strftime("%Y-%m-%d")
    tickers = store.active_tickers()
    alias_idx = build_alias_index(tickers)
    universe = set(tickers)

    held = held_codes(store)

    recommended: set[str] = set()
    try:
        rec = store.reco_history(days=3)
        if not rec.empty:
            recommended = set(rec["ticker"].astype(str))
    except Exception:  # noqa: BLE001
        recommended = set()

    # ── 정규화 + 1차 중복 제거 ──
    seen: dict[str, dict] = {}
    seen_urls: set[str] = set()
    seen_norm: set[tuple] = set()
    for r in raw_items:
        title = (r.get("title") or "").strip()
        if len(title) < 8:
            continue
        tnorm = normalize_title(title)
        if len(tnorm) < 5:
            continue
        nid = make_id(tnorm, r.get("source", ""))
        if nid in seen:
            continue
        # 1층 — 같은 URL 이면 같은 기사다. 매체 표기가 갈려 id 가 달라져도
        # 여기서 접는다. 먼저 들어온 쪽을 남긴다.
        ukey = normalize_url(r.get("url"))
        if ukey and ukey in seen_urls:
            continue
        # 2층 — URL 이 달라도(추적 파라미터·리다이렉트) 정규화 제목과
        # 정규화 매체명이 같으면 같은 매체의 같은 기사다.
        nkey = (tnorm, canonical_source(r.get("source")))
        if nkey in seen_norm:
            continue
        if ukey:
            seen_urls.add(ukey)
        seen_norm.add(nkey)
        # DB 에는 tz 정보를 뗀 KST naive 문자열로 넣는다. collected 와
        # 형식을 통일해야 news_since 의 문자열 비교가 정상 동작한다.
        pub_kst = _to_kst(r.get("published"))
        seen[nid] = {
            "id": nid,
            "d": today,
            "published": pub_kst.replace(tzinfo=None).isoformat(timespec="seconds")
            if pub_kst else None,
            "_published_dt": pub_kst,
            "title": title,
            "title_norm": tnorm,
            "url": r.get("url"),
            "source": r.get("source"),
            "origin": r.get("origin"),
            "region": r.get("region"),
            "lang": r.get("lang", "ko"),
            "category": classify(title, r.get("category_hint")),
            "_extra_names": r.get("extra_names"),
            "_stock_code": r.get("stock_code"),
        }

    items = list(seen.values())

    # ── 종목 매핑 ──
    # **클러스터링보다 먼저** 한다. 3층의 종목 가드가 이 결과를 쓴다
    # (태깅된 종목이 겹치지 않으면 같은 사건으로 보지 않는다).
    for it in items:
        it["tickers"] = map_tickers(it["title"], alias_idx,
                                    it.get("_extra_names"),
                                    it.get("_stock_code"))

    # ── 3층: 사건 클러스터 ──
    # 클러스터링은 배치 경계를 넘어야 한다. 최근 항목을 씨앗으로 준다.
    # 씨앗을 못 얻어도 정리는 계속한다 — 브리핑이 안 나가는 것보다
    # 중복이 조금 남는 편이 낫다.
    dedup_cfg = cfg.news_dedup if cfg is not None else DEFAULT.news_dedup
    seeds: list[dict] = []
    fetch_seeds = getattr(store, "recent_news_for_cluster", None)
    if callable(fetch_seeds):
        try:
            seeds = fetch_seeds(hours=dedup_cfg.seed_hours) or []
        except Exception as exc:  # noqa: BLE001 - 씨앗은 있으면 좋은 것이다
            log.warning("클러스터 씨앗 조회 실패(무시하고 진행): %s", exc)
    items = cluster_items(items, cfg=dedup_cfg, seeds=seeds)

    # ── 중요도 ──
    # 산정 방식은 그대로다: 매체 수(1순위) + 내 종목(2순위) + 키워드 + 신선도.
    # 매체 수만 정확해졌다 — 표기가 갈려 한 매체를 둘로 세던 것을 고쳤다.
    links: list[tuple] = []
    for it in items:
        # 신선도 감점은 tz-aware datetime 이 필요하므로 원본을 따로 넘긴다.
        pub_dt = it.pop("_published_dt", None)
        it["importance"] = score_importance(
            {**it, "published": pub_dt}, held, recommended, universe)
        for code, nm in it["tickers"]:
            links.append((it["id"], code, nm))

    # ── 선택적 요약 훅 ──
    if summarize_hook:
        try:
            groups: dict[str, list[dict]] = {}
            for it in items:
                groups.setdefault(it["cluster_id"], []).append(it)
            summaries = summarize_hook(groups) or {}
            for it in items:
                s = summaries.get(it["cluster_id"])
                if s:
                    it["summary"] = s
        except Exception as exc:  # noqa: BLE001
            log.warning("요약 훅 실패(무시하고 원문 제목 사용): %s", exc)

    rows = [{k: v for k, v in it.items() if not k.startswith("_")
             and k not in ("tickers", "cluster_items")} for it in items]
    stored = store.upsert_news(rows)
    n_links = store.link_news_tickers(links)

    log.info("뉴스 정리: 수집 %d → 저장 %d, 종목링크 %d, 클러스터 %d",
             len(raw_items), stored, n_links,
             len({it["cluster_id"] for it in items}))
    return {"collected": len(raw_items), "stored": stored,
            "links": n_links, "items": items}


# ══════════════════════════ 7. 주간 테마 변화 ══════════════════════════
def theme_shift(store, week_days: int = 5) -> pd.DataFrame:
    """이번 주 vs 지난 주 카테고리 건수 변화.

    어떤 테마가 뜨고 어떤 테마가 식는지를 본다. 개별 뉴스보다
    이 흐름이 순환매 판단에 쓸모 있다.
    """
    df = store.news_theme_counts(days=week_days * 4)
    if df.empty:
        return pd.DataFrame()
    days = sorted(df["d"].unique())
    if len(days) < 2:
        return pd.DataFrame()
    cur_days = days[-week_days:]
    prev_days = days[-week_days * 2:-week_days] or days[:-week_days]
    cur = (df[df["d"].isin(cur_days)].groupby("category")["clusters"]
           .sum().rename("this_week"))
    prev = (df[df["d"].isin(prev_days)].groupby("category")["clusters"]
            .sum().rename("last_week"))
    out = pd.concat([cur, prev], axis=1).fillna(0).astype(int)
    out["delta"] = out["this_week"] - out["last_week"]
    denom = out["last_week"].replace(0, pd.NA)
    out["delta_pct"] = (out["delta"] / denom * 100).round(0)
    return out.sort_values("delta", ascending=False)
