# -*- coding: utf-8 -*-
"""뉴스 수집기 (국내 + 해외).

의존성 정책
----------
feedparser 를 새로 넣지 않고 이미 있는 lxml + BeautifulSoup 로 RSS 를 판다.
RSS 2.0 과 Atom 태그 이름 차이만 흡수하면 되고, 그러면 requirements 를
건드릴 필요가 없다.

수집 경로
--------
  NAVER  : 네이버 금융 주요뉴스/실시간 속보 (HTML)
  GOOGLE : Google News RSS 검색 (한국어. 국내외 모두 커버, 가장 안정적)
  YAHOO  : Yahoo Finance 종목별 RSS (미국 개별주 원문)
  DART   : Open DART 공시 목록 API (DART_API_KEY 필요, 없으면 건너뜀)

주의: 모든 수집기는 실패해도 빈 리스트를 돌려준다. 한 소스가 죽어도
아침 브리핑은 나가야 한다.
"""
from __future__ import annotations

import logging
import os
import random
import re
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus, urljoin

import requests
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
TIMEOUT = 8.0

__all__ = ["NewsItem", "GOOGLE_QUERIES", "YAHOO_SYMBOLS", "collect_all",
           "collect_naver", "collect_google", "collect_yahoo", "collect_dart"]


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
                time.sleep(2 ** attempt + random.random())
                continue
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


# ══════════════════════════ 네이버 금융 ══════════════════════════
_NAVER_PAGES = (
    ("https://finance.naver.com/news/mainnews.naver", "주요뉴스"),
    ("https://finance.naver.com/news/news_list.naver"
     "?mode=LSS2D&section_id=101&section_id2=258", "시황·전망"),
    ("https://finance.naver.com/news/news_list.naver"
     "?mode=LSS2D&section_id=101&section_id2=402", "종목분석"),
)


def collect_naver(limit_per_page: int = 40) -> list[dict]:
    """네이버 금융 뉴스 목록 (HTML 파싱).

    네이버는 마크업을 자주 바꾼다. 셀렉터를 여러 개 시도하고
    전부 실패하면 빈 리스트를 돌려 다른 소스로 넘긴다.
    """
    out: list[dict] = []
    selectors = (".newsList .articleSubject a", "dl.newsList dd.articleSubject a",
                 ".block1 .articleSubject a", "ul.newsList li a", "td.title a")
    for url, cat in _NAVER_PAGES:
        res = _get(url)
        if res is None:
            continue
        res.encoding = res.apparent_encoding or "euc-kr"
        soup = BeautifulSoup(res.text, "lxml")
        links = []
        for sel in selectors:
            links = soup.select(sel)
            if links:
                break
        if not links:
            log.warning("네이버 뉴스 셀렉터 미매칭: %s", url[:60])
            continue
        for a in links[:limit_per_page]:
            title = a.get_text(strip=True)
            href = a.get("href", "")
            if not title or not href:
                continue
            out.append({
                "title": title,
                "url": urljoin("https://finance.naver.com", href),
                "source": "네이버금융",
                "origin": "NAVER",
                "region": "KR",
                "category_hint": cat,
                "published": None,
                "lang": "ko",
            })
        time.sleep(0.4)
    log.info("네이버 금융 %d건", len(out))
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
            # Google 은 "제목 - 매체명" 형태로 붙여주는 경우가 많다
            if not source and " - " in title:
                head, _, tail = title.rpartition(" - ")
                if head and len(tail) <= 20:
                    title, source = head, tail
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
                 max_pages: int = 5) -> list[dict]:
    """Open DART 공시 목록. DART_API_KEY 없으면 건너뛴다.

    전체 공시는 하루 수백 건이라 다 보내면 소음이다. 주가에 직접
    영향을 주는 유형만 정규식으로 걸러낸다.
    """
    key = os.getenv("DART_API_KEY")
    if not key:
        log.info("DART_API_KEY 미설정 → 공시 수집 건너뜀")
        return []

    end = datetime.now(KST)
    start = end - timedelta(days=days)
    out: list[dict] = []
    for page in range(1, max_pages + 1):
        res = _get("https://opendart.fss.or.kr/api/list.json",
                   params={"crtfc_key": key,
                           "bgn_de": start.strftime("%Y%m%d"),
                           "end_de": end.strftime("%Y%m%d"),
                           "page_no": page, "page_count": page_count,
                           "corp_cls": "Y"})
        if res is None:
            break
        try:
            data = res.json()
        except ValueError:
            break
        if data.get("status") != "000":
            log.warning("DART 응답 코드 %s: %s",
                        data.get("status"), data.get("message"))
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
    log.info("DART 공시 %d건 (필터 통과)", len(out))
    return out


# ══════════════════════════ 통합 ══════════════════════════
def collect_all(use_naver: bool = True, use_google: bool = True,
                use_yahoo: bool = True, use_dart: bool = True) -> list[dict]:
    """전 소스 수집. 소스별 실패를 개별 격리한다."""
    items: list[dict] = []
    jobs = (
        ("naver", use_naver, collect_naver),
        ("google", use_google, collect_google),
        ("yahoo", use_yahoo, collect_yahoo),
        ("dart", use_dart, collect_dart),
    )
    for label, enabled, fn in jobs:
        if not enabled:
            continue
        try:
            items.extend(fn())
        except Exception as exc:  # noqa: BLE001 - 소스 격리
            log.warning("수집기 %s 실패: %s", label, exc)
    log.info("뉴스 수집 합계 %d건", len(items))
    return items
