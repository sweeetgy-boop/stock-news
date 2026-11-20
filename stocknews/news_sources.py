# -*- coding: utf-8 -*-
"""뉴스 수집기 (국내 + 해외).

의존성 정책
----------
feedparser 를 새로 넣지 않고 이미 있는 lxml + BeautifulSoup 로 RSS 를 판다.
RSS 2.0 과 Atom 태그 이름 차이만 흡수하면 되고, 그러면 requirements 를
건드릴 필요가 없다.

수집 경로
--------
  NAVER  : 네이버 증권 주요뉴스·시황·종목분석 (JSON API)
  GOOGLE : Google News RSS 검색 (한국어. 국내외 모두 커버, 가장 안정적)
  YAHOO  : Yahoo Finance 종목별 RSS (미국 개별주 원문)
  DART   : Open DART 공시 목록 API (DART_API_KEY 필요, 없으면 건너뜀)

주의: 모든 수집기는 실패해도 빈 리스트를 돌려준다. 한 소스가 죽어도
아침 브리핑은 나가야 한다.
"""
from __future__ import annotations

import html
import json
import logging
import os
import random
import re
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus

import requests
from bs4 import BeautifulSoup

from .config import DART_MARKETS, DART_NEWS_MAX_PAGES

log = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
TIMEOUT = 8.0

__all__ = ["NewsItem", "GOOGLE_QUERIES", "YAHOO_SYMBOLS", "collect_all",
           "collect_naver", "collect_google", "collect_yahoo", "collect_dart",
           "enabled_sources", "SOURCE_LABELS", "LAST_WARN"]


class NewsItem(dict):
    """수집 원본 1건. dict 그대로 쓰되 필수 키를 명시한다.

    keys: title, url, source, origin, region, published(datetime|None), lang
    """


# ── Google News RSS 검색어. 카테고리 힌트를 함께 준다 ──
GOOGLE_QUERIES: tuple[tuple[str, str, str], ...] = (
    # (검색어, region, category_hint)
    ("코스피 증시", "KR", "국내시황"),
    ("코스닥 시황", "KR", "국내시황"),
    ("외국인 기관 순매수", "KR", "수급"),
    ("공매도 잔고", "KR", "수급"),
    ("신용융자 반대매매", "KR", "수급"),
    ("유상증자 전환사채", "KR", "공시"),
    ("어닝 서프라이즈 실적", "KR", "실적"),
    ("반도체 HBM 수출", "KR", "반도체"),
    ("2차전지 배터리 수주", "KR", "2차전지"),
    ("방산 수출 계약", "KR", "방산조선"),
    ("조선 수주 LNG운반선", "KR", "방산조선"),
    ("바이오 기술수출 임상", "KR", "바이오"),
    ("전력기기 변압기 데이터센터", "KR", "전력AI"),
    ("원달러 환율 국고채 금리", "KR", "매크로"),
    ("미국 증시 나스닥 다우", "US", "해외시황"),
    ("FOMC 연준 금리", "US", "매크로"),
    ("엔비디아 테슬라 실적", "US", "해외개별주"),
    ("관세 수출규제 무역", "GLOBAL", "정책"),
    ("국제유가 원자재 구리", "GLOBAL", "매크로"),
)

# ── Yahoo Finance 종목별 RSS (미국 개별주 원문) ──
YAHOO_SYMBOLS: tuple[tuple[str, str], ...] = (
    ("TSLA", "테슬라"), ("NVDA", "엔비디아"), ("AAPL", "애플"),
    ("MSFT", "마이크로소프트"), ("AMD", "AMD"), ("AVGO", "브로드컴"),
    ("TSM", "TSMC"), ("PLTR", "팔란티어"),
)


# ══════════════════════════ 공통 유틸 ══════════════════════════
def _get(url: str, **kw) -> requests.Response | None:
    for attempt in range(3):
        try:
            res = requests.get(url, headers={"User-Agent": UA,
                                             "Accept-Language": "ko,en;q=0.8"},
                               timeout=TIMEOUT, **kw)
            if res.status_code == 200:
                return res
            if res.status_code in (429, 503):
                if attempt == 2:
                    log.warning("요청 실패 %s: HTTP %d (재시도 소진)",
                                url[:70], res.status_code)
                time.sleep(2 ** attempt + random.random())
                continue
            # 예전에는 조용히 None 이었다 — 원인이 '0건' 뒤로 숨었다.
            log.warning("요청 실패 %s: HTTP %d", url[:70], res.status_code)
            return None
        except requests.RequestException as exc:
            if attempt == 2:
                log.warning("요청 실패 %s: %s", url[:70], exc)
            time.sleep(1.0 + attempt)
    return None


_RSS_DATE_FORMATS = (
    "%a, %d %b %Y %H:%M:%S %z",
    "%a, %d %b %Y %H:%M:%S %Z",
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%dT%H:%M:%SZ",
)


def _parse_rss_date(text: str | None) -> datetime | None:
    if not text:
        return None
    t = text.strip().replace("GMT", "+0000").replace("UTC", "+0000")
    for fmt in _RSS_DATE_FORMATS:
        try:
            dt = datetime.strptime(t, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(KST)
        except ValueError:
            continue
    return None


def _parse_feed(xml_text: str) -> list[dict]:
    """RSS 2.0 / Atom 공통 파서."""
    try:
        soup = BeautifulSoup(xml_text, "xml")
    except Exception:  # noqa: BLE001 - lxml-xml 미설치 등
        soup = BeautifulSoup(xml_text, "html.parser")

    out: list[dict] = []
    nodes = soup.find_all("item") or soup.find_all("entry")
    for n in nodes:
        title_el = n.find("title")
        title = title_el.get_text(strip=True) if title_el else ""
        if not title:
            continue

        link = ""
        link_el = n.find("link")
        if link_el is not None:
            link = (link_el.get_text(strip=True)
                    or link_el.get("href", "") or "")
        if not link:
            gid = n.find("guid")
            link = gid.get_text(strip=True) if gid else ""

        date_el = (n.find("pubDate") or n.find("published")
                   or n.find("updated") or n.find("dc:date"))
        published = _parse_rss_date(date_el.get_text(strip=True)) if date_el else None

        src_el = n.find("source")
        source = src_el.get_text(strip=True) if src_el else ""

        out.append({"title": title, "url": link,
                    "published": published, "source": source})
    return out


# ══════════════════════════ 네이버 증권 (JSON API) ══════════════════════════
# 2026-09-09~10 사이 finance.naver.com/news/* 가 302 → stock.naver.com(Next.js
# SPA)로 넘어갔다. 그 HTML 에는 기사가 없어 예전 셀렉터 5개가 전부 0건이었고,
# 경고는 '셀렉터 미매칭' 뿐이라 원인(리다이렉트)이 가려졌다 — news 단계는 63일
# 동안 rc 0 이었다. 이제 SPA 가 부르는 JSON API 를 직접 부른다.
#
#   list  : /api/domestic/news/list?category=mainnews&page=1&pageSize=N
#           {"articles":[{officeId, officeHname, articleId, title,
#                         "datetime":"2026-11-12 15:14:14", ...}]}
#   focus : /api/domestic/news/focus?sid=S&page=1&pageSize=N&date=YYYYMMDD
#           &enableFallback=true&maxDays=7   (date·enableFallback 없으면 빈 결과)
#           {"articles":[{officeID, officeHName, articleID, title,
#                         "date":"20261112154213", "url":"https://n.news..."}]}
#
# 두 엔드포인트의 키 대소문자가 다르다(officeId vs officeID). 파서가 둘 다 받는다.
# focus 의 sid 는 새 체계다: 401 시황·전망 · 402 종목분석 (예전 '시황·전망'은 258).
# source 라벨은 예전 그대로 "네이버금융" — make_id·클러스터·애그리게이터 처리를
# 바꾸지 않기 위해서다(매체명으로 바꾸면 다른 출처와의 중복 판정이 달라진다).
_NAVER_API = "https://stock.naver.com/api/domestic/news"
_NAVER_FEEDS: tuple[tuple[str, dict, str], ...] = (
    ("list", {"category": "mainnews"}, "주요뉴스"),
    ("focus", {"sid": "401"}, "시황·전망"),
    ("focus", {"sid": "402"}, "종목분석"),
)
_NAVER_HEADERS = {"Accept": "application/json",
                  "Referer": "https://stock.naver.com/news/mainnews"}
_NAVER_ARTICLE = "https://n.news.naver.com/mnews/article/{office}/{article}"
_NAVER_TIME_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y%m%d%H%M%S", "%Y%m%d%H%M",
                       "%Y-%m-%dT%H:%M:%S")

# 소스 라벨 → 이번 수집의 경고(원인). collect_all 이 매번 비운다.
# 관리자 알림(news_health)이 '왜 0건인가' 를 이 값으로 적는다.
LAST_WARN: dict[str, str] = {}


def naver_feed_url(kind: str, params: dict, page_size: int,
                   day: str | None = None, max_days: int = 7) -> str:
    """엔드포인트 URL. focus 는 date·enableFallback 이 없으면 빈 결과다."""
    q = dict(params)
    q.update({"page": 1, "pageSize": int(page_size)})
    if kind == "focus":
        q.update({"date": day or datetime.now(KST).strftime("%Y%m%d"),
                  "enableFallback": "true", "maxDays": int(max_days)})
    return f"{_NAVER_API}/{kind}?" + "&".join(f"{k}={v}" for k, v in q.items())


def naver_fetch_json(url: str) -> tuple[object | None, str | None]:
    """JSON 을 받는다. 반환 (data, 실패 원인). 성공이면 원인 None.

    리다이렉트를 따라가지 않는다 — 따라가면 SPA HTML 이 200 으로 와서
    '기사 없음' 으로 보이고 원인이 사라진다. 2026-09 에 그렇게 63일을 놓쳤다.
    """
    why = None
    for attempt in range(3):
        try:
            res = requests.get(url, headers={"User-Agent": UA,
                                             "Accept-Language": "ko,en;q=0.8",
                                             **_NAVER_HEADERS},
                               timeout=TIMEOUT, allow_redirects=False)
        except requests.RequestException as exc:
            why = f"{type(exc).__name__}: {str(exc)[:120]}"
            time.sleep(1.0 + attempt)
            continue
        code = res.status_code
        if code in (429, 503):
            why = f"HTTP {code}"
            time.sleep(2 ** attempt + random.random())
            continue
        return naver_decode(code, res.headers or {}, res.content)
    return None, why


def naver_decode(status: int, headers, body
                 ) -> tuple[object | None, str | None]:
    """응답 → (data, 원인). 네트워크 없이 검사할 수 있게 분리했다."""
    if 300 <= int(status) < 400:
        loc = headers.get("Location") or headers.get("location") or "?"
        return None, f"리다이렉트 {status} → {str(loc)[:100]}"
    if int(status) != 200:
        return None, f"HTTP {status}"
    try:
        text = body.decode("utf-8") if isinstance(body, bytes) else str(body)
        return json.loads(text), None
    except (ValueError, UnicodeDecodeError):
        ctype = headers.get("Content-Type") or headers.get("content-type") or "?"
        return None, f"JSON 아님 (content-type={str(ctype)[:60]})"


def _parse_naver_time(text) -> datetime | None:
    """'2026-11-12 15:14:14'(list) · '20261112154213'(focus) → KST aware."""
    if not text:
        return None
    t = str(text).strip()
    for fmt in _NAVER_TIME_FORMATS:
        try:
            return datetime.strptime(t, fmt).replace(tzinfo=KST)
        except ValueError:
            continue
    return None


def parse_naver_articles(data, cat: str, limit: int | None = None
                         ) -> tuple[list[dict], str | None]:
    """API 응답 → NewsItem 목록. 반환 (items, 원인). 0건이면 원인을 적는다."""
    if not isinstance(data, dict) or not isinstance(data.get("articles"), list):
        keys = list(data)[:6] if isinstance(data, dict) else type(data).__name__
        return [], f"응답 구조 변경 (articles 없음 · {keys})"
    out: list[dict] = []
    for a in data["articles"]:
        if not isinstance(a, dict):
            continue
        title = html.unescape(str(a.get("title") or "")).strip()
        office = a.get("officeId") or a.get("officeID")
        article = a.get("articleId") or a.get("articleID")
        url = a.get("url") or (_NAVER_ARTICLE.format(office=office,
                                                     article=article)
                               if office and article else "")
        if not title or not url:
            continue
        out.append({
            "title": title,
            "url": url,
            "source": "네이버금융",
            "origin": "NAVER",
            "region": "KR",
            "category_hint": cat,
            "published": _parse_naver_time(a.get("datetime") or a.get("date")),
            "lang": "ko",
        })
        if limit is not None and len(out) >= limit:
            break
    return out, (None if out else "기사 0건")


def collect_naver(limit_per_page: int = 40) -> list[dict]:
    """네이버 증권 뉴스 (JSON API). 엔드포인트별로 실패를 격리한다.

    실패·0건이면 원인(리다이렉트 302 → …, HTTP 404, JSON 아님, 구조 변경)을
    경고로 남기고 LAST_WARN["naver"] 에 모은다.
    """
    from .config import NAVER_API_PAGE_SIZE, NAVER_FOCUS_MAX_DAYS
    out: list[dict] = []
    warns: list[str] = []
    size = max(1, min(int(limit_per_page), int(NAVER_API_PAGE_SIZE)))
    for i, (kind, params, cat) in enumerate(_NAVER_FEEDS):
        if i:
            time.sleep(0.4)
        url = naver_feed_url(kind, params, size, max_days=NAVER_FOCUS_MAX_DAYS)
        data, why = naver_fetch_json(url)
        items: list[dict] = []
        if why is None:
            items, why = parse_naver_articles(data, cat, limit=limit_per_page)
        if why:
            log.warning("네이버 뉴스 %s 0건 — %s (%s)", cat, why, url[:90])
            warns.append(f"{cat}: {why}")
        out.extend(items)
    if warns:
        LAST_WARN["naver"] = " · ".join(warns)[:300]
    log.info("네이버 증권 %d건", len(out))
    return out


# ══════════════════════════ Google News RSS ══════════════════════════
def collect_google(queries=GOOGLE_QUERIES, per_query: int = 12) -> list[dict]:
    """Google News RSS 검색. 국내외를 한국어로 함께 커버한다."""
    out: list[dict] = []
    for query, region, cat in queries:
        url = ("https://news.google.com/rss/search?q="
               f"{quote_plus(query)}+when:2d&hl=ko&gl=KR&ceid=KR:ko")
        res = _get(url)
        if res is None:
            continue
        for it in _parse_feed(res.text)[:per_query]:
            title = it["title"]
            source = it.get("source") or ""
            # Google 은 제목 끝에 " - 매체명" 을 붙인다. <source> 가 있든
            # 없든 제목에서는 뗀다.
            #
            # 예전 판은 `not source` 일 때만 뗐다. 그런데 Google 은 거의
            # 항상 <source> 를 주므로 사실상 한 번도 떼지 않았다 —
            # 2026-09-09 실측으로 GOOGLE 출처 1,254건 전부가 꼬리표를 달고
            # 저장돼 있었다. 그래서 아침 브리핑 제목에 'digitaltoday.co.kr',
            # 'v.daum.net' 같은 도메인이 그대로 찍혔다.
            head, sep, tail = title.rpartition(" - ")
            if sep and head and len(tail) <= 20:
                title = head
                source = source or tail
            out.append({
                "title": title,
                "url": it["url"],
                "source": source or "GoogleNews",
                "origin": "GOOGLE",
                "region": region,
                "category_hint": cat,
                "published": it["published"],
                "lang": "ko",
            })
        time.sleep(0.5)
    log.info("Google News %d건", len(out))
    return out


# ══════════════════════════ Yahoo Finance RSS ══════════════════════════
def collect_yahoo(symbols=YAHOO_SYMBOLS, per_symbol: int = 6) -> list[dict]:
    """미국 개별주 원문 헤드라인. 번역은 하지 않고 종목 태깅만 붙인다."""
    out: list[dict] = []
    for sym, kr_name in symbols:
        url = ("https://feeds.finance.yahoo.com/rss/2.0/headline"
               f"?s={sym}&region=US&lang=en-US")
        res = _get(url)
        if res is None:
            continue
        for it in _parse_feed(res.text)[:per_symbol]:
            out.append({
                "title": it["title"],
                "url": it["url"],
                "source": it.get("source") or "Yahoo Finance",
                "origin": "YAHOO",
                "region": "US",
                "category_hint": "해외개별주",
                "published": it["published"],
                "lang": "en",
                "extra_names": [sym, kr_name],
            })
        time.sleep(0.4)
    log.info("Yahoo Finance %d건", len(out))
    return out


# ══════════════════════════ Open DART 공시 ══════════════════════════
_DART_KEEP = re.compile(
    r"유상증자|무상증자|전환사채|신주인수권|교환사채|자기주식|"
    r"단일판매|공급계약|수주|영업정지|감사보고서|감사의견|"
    r"관리종목|상장폐지|합병|분할|무상감자|유상감자|배당|"
    r"주요사항보고|실적|영업실적|정정")


def collect_dart(days: int = 1, page_count: int = 100,
                 max_pages=None,
                 markets: tuple[str, ...] | None = None) -> list[dict]:
    """Open DART 공시 목록. DART_API_KEY 없으면 건너뛴다.

    전체 공시는 하루 수백 건이라 다 보내면 소음이다. 주가에 직접
    영향을 주는 유형만 정규식으로 걸러낸다.

    markets 는 corp_cls 튜플(기본 config.DART_MARKETS)이고 순서대로 돈다.
    코넥스("N")는 유니버스 밖이라 건너뛴다. max_pages 는 int 또는
    {시장: 상한} dict (기본 config.DART_NEWS_MAX_PAGES).
    """
    key = os.getenv("DART_API_KEY")
    if not key:
        log.info("DART_API_KEY 미설정 → 공시 수집 건너뜀")
        return []

    markets = tuple(markets) if markets is not None else tuple(DART_MARKETS)
    max_pages = DART_NEWS_MAX_PAGES if max_pages is None else max_pages
    end = datetime.now(KST)
    start = end - timedelta(days=days)
    out: list[dict] = []
    for market in markets:
        if market == "N":
            continue
        limit = (int(max_pages.get(market, 5)) if isinstance(max_pages, dict)
                 else int(max_pages))
        out.extend(_collect_dart_market(key, market, start, end,
                                        page_count, limit))
    log.info("DART 공시 %d건 (필터 통과 · 시장 %s)", len(out), ",".join(markets))
    return out


def _collect_dart_market(key: str, market: str, start, end,
                         page_count: int, max_pages: int) -> list[dict]:
    out: list[dict] = []
    for page in range(1, max_pages + 1):
        res = _get("https://opendart.fss.or.kr/api/list.json",
                   params={"crtfc_key": key,
                           "bgn_de": start.strftime("%Y%m%d"),
                           "end_de": end.strftime("%Y%m%d"),
                           "page_no": page, "page_count": page_count,
                           "corp_cls": market})
        if res is None:
            break
        try:
            data = res.json()
        except ValueError:
            break
        if data.get("status") != "000":
            log.warning("DART 응답 코드 %s (corp_cls=%s): %s",
                        data.get("status"), market, data.get("message"))
            break
        items = data.get("list") or []
        for it in items:
            name = (it.get("report_nm") or "").strip()
            if not _DART_KEEP.search(name):
                continue
            rcp = it.get("rcept_no", "")
            pub = None
            if it.get("rcept_dt"):
                try:
                    pub = datetime.strptime(it["rcept_dt"], "%Y%m%d").replace(tzinfo=KST)
                except ValueError:
                    pub = None
            out.append({
                "title": f"[공시] {it.get('corp_name', '')} {name}",
                "url": f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcp}",
                "source": "DART",
                "origin": "DART",
                "region": "KR",
                "category_hint": "공시",
                "published": pub,
                "lang": "ko",
                "extra_names": [it.get("corp_name", "")],
                "stock_code": (it.get("stock_code") or "").zfill(6)
                if it.get("stock_code") else None,
            })
        if len(items) < page_count:
            break
        time.sleep(0.4)
    return out


# ══════════════════════════ 통합 ══════════════════════════
# 소스 라벨(수집 순서). runs.note 의 news_sources= 와 관리자 알림이 이 이름을 쓴다.
SOURCE_LABELS: tuple[str, ...] = ("naver", "google", "yahoo", "dart")


def enabled_sources(use_naver: bool = True, use_google: bool = True,
                    use_yahoo: bool = True, use_dart: bool = True
                    ) -> tuple[str, ...]:
    """켜진 소스. DART 는 키(DART_API_KEY)가 없으면 꺼진 것으로 본다 —
    collect_dart 가 조회 없이 건너뛰므로 '0건' 으로 세면 매일 오탐이 된다."""
    flags = {"naver": use_naver, "google": use_google, "yahoo": use_yahoo,
             "dart": use_dart and bool(os.getenv("DART_API_KEY"))}
    return tuple(k for k in SOURCE_LABELS if flags[k])


def collect_all(use_naver: bool = True, use_google: bool = True,
                use_yahoo: bool = True, use_dart: bool = True,
                counts: dict | None = None) -> list[dict]:
    """전 소스 수집. 소스별 실패를 개별 격리한다.

    counts 를 주면 **켜진 소스**의 수집 건수(중복 제거 전 원본)를 채운다.
    예외로 죽은 소스는 0 이다. 끈 소스(--no-naver, DART 키 없음)는 넣지 않는다.
    """
    items: list[dict] = []
    LAST_WARN.clear()
    active = enabled_sources(use_naver, use_google, use_yahoo, use_dart)
    jobs = (
        ("naver", use_naver, collect_naver),
        ("google", use_google, collect_google),
        ("yahoo", use_yahoo, collect_yahoo),
        ("dart", use_dart, collect_dart),
    )
    for label, enabled, fn in jobs:
        if not enabled:
            continue
        got: list = []
        try:
            got = fn() or []
        except Exception as exc:  # noqa: BLE001 - 소스 격리
            log.warning("수집기 %s 실패: %s", label, exc)
            LAST_WARN[label] = f"{type(exc).__name__}: {str(exc)[:200]}"
        items.extend(got)
        if counts is not None and label in active:
            counts[label] = len(got)
    log.info("뉴스 수집 합계 %d건", len(items))
    return items
