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

import pandas as pd

log = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))

__all__ = ["normalize_title", "make_id", "classify", "cluster_items",
           "build_alias_index", "map_tickers", "score_importance",
           "process_and_store", "theme_shift"]


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


def _tokens(title_norm: str) -> set[str]:
    return {w for w in title_norm.split() if len(w) > 1 and w not in _STOP}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    return inter / (len(a) + len(b) - inter)


def cluster_items(items: list[dict], threshold: float = 0.55) -> list[dict]:
    """같은 사건을 묶는다. 탐욕적 단일 패스 클러스터링.

    건수가 하루 수백 건 수준이라 O(n x k) 로 충분하다. 대표 기사는
    클러스터에서 가장 먼저 등장한 항목으로 두고, cluster_n 에 매체 수를 센다.
    """
    reps: list[tuple[set[str], str]] = []   # (토큰, cluster_id)
    sizes: dict[str, int] = {}
    sources: dict[str, set[str]] = {}

    for it in items:
        tok = _tokens(it.get("title_norm", ""))
        cid = None
        best = 0.0
        for rtok, rcid in reps:
            s = _jaccard(tok, rtok)
            if s > best:
                best, cid = s, rcid
        if cid is None or best < threshold:
            cid = it["id"]
            reps.append((tok, cid))
        it["cluster_id"] = cid
        sizes[cid] = sizes.get(cid, 0) + 1
        sources.setdefault(cid, set()).add((it.get("source") or "").strip())

    for it in items:
        cid = it["cluster_id"]
        # 매체 수를 센다. 같은 매체의 후속 기사는 중복으로 보지 않는다.
        it["cluster_n"] = max(len(sources.get(cid, ())), 1)
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


# ══════════════════════════ 6. 파이프라인 ══════════════════════════
def process_and_store(store, raw_items: list[dict],
                      summarize_hook=None) -> dict:
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

    held: set[str] = set()
    try:
        pos = store.list_positions() if hasattr(store, "list_positions") else []
        held = {p.ticker for p in pos}
    except Exception:  # noqa: BLE001 - positions 테이블 미도입 상태 허용
        held = set()

    recommended: set[str] = set()
    try:
        rec = store.reco_history(days=3)
        if not rec.empty:
            recommended = set(rec["ticker"].astype(str))
    except Exception:  # noqa: BLE001
        recommended = set()

    # ── 정규화 + 1차 중복 제거 ──
    seen: dict[str, dict] = {}
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
    items = cluster_items(items)

    # ── 종목 매핑 + 중요도 ──
    links: list[tuple] = []
    for it in items:
        pairs = map_tickers(it["title"], alias_idx,
                            it.get("_extra_names"), it.get("_stock_code"))
        it["tickers"] = pairs
        # 신선도 감점은 tz-aware datetime 이 필요하므로 원본을 따로 넘긴다.
        pub_dt = it.pop("_published_dt", None)
        it["importance"] = score_importance(
            {**it, "published": pub_dt}, held, recommended, universe)
        for code, nm in pairs:
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
