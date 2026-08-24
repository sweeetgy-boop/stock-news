# -*- coding: utf-8 -*-
"""배제 플래그 공급원.

이 모듈이 없으면 청산 계층 0(무효화)이 영원히 발동하지 않는다.
"-30% 청산선에서 사면 절대 실패하지 않는다"는 믿음이 깨지는 유일한
경로가 여기다. 밴드를 뚫고 영구히 내려간 종목은 예외 없이 이 목록에 있었다.

공급원 4단
---------
  ① FDR    관리종목 (StockListing('KRX-ADMINISTRATIVE'))
  ② DART   자본잠식률 · 대규모 증자/CB · 감사의견 (DART_API_KEY 필요)
  ③ 로컬   거래정지 흔적 · 동전주 위험 · 시세 불연속 (외부 데이터 불필요)
  ④ 수동   data/flags_manual.csv (항상 마지막에 덮어씀)

설계 원칙: 공급원이 실패하면 그 필드를 건드리지 않는다
--------------------------------------------------
갱신 전에 필드를 NULL 로 비우는 이유는 '관리종목 해제'를 반영하기
위함이다. 비우지 않으면 한 번 켜진 플래그가 영구히 남아 정상화된
종목이 계속 배제된다.

그런데 조회가 실패했을 때도 비우면 멀쩡한 데이터를 날린다. 그래서
**공급원이 성공했을 때만 해당 필드를 초기화한다.** 실패하면 이전 값을
그대로 유지하고 경고만 남긴다. 보수적으로 틀리는 쪽이 안전하다.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import os
import random
import re
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from xml.etree import ElementTree

import pandas as pd
import requests

log = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))
TIMEOUT = 10.0

__all__ = [
    "fetch_admin_issues", "dart_corp_codes", "dart_capital_impairment",
    "dart_disclosure_events", "scan_local_flags", "detect_halt_history",
    "detect_penny_risk", "detect_price_discontinuity", "load_manual_flags",
    "refresh_flags", "flag_summary",
    "load_manual_credit", "refresh_credit",
]

# 자본잠식 판정 임계 (%). 50% 이상이면 관리종목 지정 사유.
IMPAIR_WARN = 50.0
# 동전주: 종가 1,000원 미만이 이 일수 이상 이어지면 관리종목 지정 위험
PENNY_PRICE = 1_000.0
PENNY_DAYS = 30


# ══════════════════════════ ① 관리종목 (FDR) ══════════════════════════
def fetch_admin_issues() -> tuple[set[str], bool]:
    """관리종목 코드 집합. (코드집합, 조회성공여부) 반환.

    FinanceDataReader 의 'KRX-ADMINISTRATIVE' 상장목록을 쓴다.
    실패하면 (빈집합, False) 를 돌려 호출부가 필드를 비우지 않게 한다.
    """
    try:
        import FinanceDataReader as fdr
        df = fdr.StockListing("KRX-ADMINISTRATIVE")
    except Exception as exc:  # noqa: BLE001
        log.warning("관리종목 조회 실패 (기존 플래그 유지): %s", exc)
        return set(), False

    if df is None or len(df) == 0:
        # 관리종목이 0건인 경우는 현실적으로 없다. 스키마 변경으로 본다.
        log.warning("관리종목 조회 결과 0건 — 스키마 변경 의심, 플래그 유지")
        return set(), False

    col = next((c for c in ("Code", "Symbol", "종목코드") if c in df.columns), None)
    if col is None:
        log.warning("관리종목 코드 컬럼 없음: %s", list(df.columns)[:10])
        return set(), False

    codes = {str(v).zfill(6) for v in df[col].dropna().tolist()}
    log.info("관리종목 %d건 조회", len(codes))
    return codes, True


# ══════════════════════════ ② DART ══════════════════════════
def _dart_get(path: str, params: dict) -> dict | None:
    key = os.getenv("DART_API_KEY")
    if not key:
        return None
    url = f"https://opendart.fss.or.kr/api/{path}"
    for attempt in range(3):
        try:
            res = requests.get(url, params={**params, "crtfc_key": key},
                               timeout=TIMEOUT)
            if res.status_code != 200:
                time.sleep(2 ** attempt)
                continue
            data = res.json()
            status = data.get("status")
            if status == "000":
                return data
            if status == "013":       # 조회 데이터 없음 — 정상 응답
                return {"status": "013", "list": []}
            if status in ("020", "021"):   # 사용 한도 초과
                log.error("DART 사용 한도 초과 (status=%s)", status)
                return None
            return {"status": status, "list": []}
        except (requests.RequestException, ValueError):
            time.sleep(1.0 + attempt)
    return None


def dart_corp_codes(cache_path: str | Path = "data/dart_corp_codes.json",
                    ttl_days: int = 7) -> dict:
    """{종목코드(6): DART corp_code(8)} 매핑. ZIP 을 받아 캐시한다."""
    p = Path(cache_path)
    if p.exists():
        age = datetime.now() - datetime.fromtimestamp(p.stat().st_mtime)
        if age < timedelta(days=ttl_days):
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass

    key = os.getenv("DART_API_KEY")
    if not key:
        return {}
    try:
        res = requests.get("https://opendart.fss.or.kr/api/corpCode.xml",
                           params={"crtfc_key": key}, timeout=60.0)
        res.raise_for_status()
        with zipfile.ZipFile(io.BytesIO(res.content)) as zf:
            name = next((n for n in zf.namelist() if n.lower().endswith(".xml")),
                        None)
            if not name:
                raise ValueError("corpCode ZIP 안에 XML 이 없음")
            xml = zf.read(name)
    except Exception as exc:  # noqa: BLE001
        log.warning("DART corp_code 조회 실패: %s", exc)
        return {}

    out: dict[str, str] = {}
    try:
        root = ElementTree.fromstring(xml)
        for item in root.iter("list"):
            stock = (item.findtext("stock_code") or "").strip()
            corp = (item.findtext("corp_code") or "").strip()
            if stock and corp and stock != " ":
                out[stock.zfill(6)] = corp
    except ElementTree.ParseError as exc:
        log.warning("corpCode XML 파싱 실패: %s", exc)
        return {}

    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    log.info("DART corp_code %d종목 캐시", len(out))
    return out


def _to_num(s) -> float | None:
    if s is None:
        return None
    t = str(s).replace(",", "").replace(" ", "").strip()
    if not t or t in ("-", "--"):
        return None
    neg = t.startswith("(") and t.endswith(")")
    if neg:
        t = t[1:-1]
    try:
        v = float(t)
    except ValueError:
        return None
    return -v if neg else v


def _report_candidates(n: int = 5) -> list[tuple[str, str]]:
    """최신 보고서부터 역순 후보. (사업연도, 보고서코드)"""
    now = datetime.now(KST)
    y = now.year
    seq = [("11014", 3), ("11012", 2), ("11013", 1), ("11011", 0)]
    out: list[tuple[str, str]] = []
    for back in (0, 1):
        for code, _ in seq:
            out.append((str(y - back), code))
    return out[:n * 2]


def dart_capital_impairment(corp_code: str) -> tuple[float | None, str]:
    """자본잠식률(%) 산출. (잠식률, 근거메모) 반환.

        자본잠식률 = (자본금 - 자본총계) / 자본금 x 100
        자본총계 <= 0 이면 완전자본잠식(100%)

    거래소 관리종목 지정 기준은 별도(개별)재무제표이므로 OFS 를 먼저 본다.
    """
    for year, reprt in _report_candidates():
        data = _dart_get("fnlttSinglAcnt.json",
                         {"corp_code": corp_code, "bsns_year": year,
                          "reprt_code": reprt})
        if not data or not data.get("list"):
            continue

        picked: dict[str, dict] = {}
        for row in data["list"]:
            nm = (row.get("account_nm") or "").strip()
            if nm not in ("자본금", "자본총계"):
                continue
            fs = (row.get("fs_div") or "").upper()
            # OFS(별도) 우선. 없으면 CFS(연결).
            cur = picked.get(nm)
            if cur is None or (fs == "OFS" and cur.get("fs") != "OFS"):
                picked[nm] = {"amt": _to_num(row.get("thstrm_amount")), "fs": fs}

        cap = (picked.get("자본금") or {}).get("amt")
        equity = (picked.get("자본총계") or {}).get("amt")
        fs_used = (picked.get("자본총계") or {}).get("fs") or "?"
        if cap is None or equity is None or cap <= 0:
            continue

        if equity <= 0:
            return 100.0, f"{year}/{reprt} {fs_used} 완전자본잠식"
        rate = (cap - equity) / cap * 100.0
        return round(rate, 2), f"{year}/{reprt} {fs_used}"
    return None, "재무데이터 없음"


_OFFERING_RE = re.compile(r"유상증자|전환사채|신주인수권부사채|교환사채|무상감자|유상감자")
_AUDIT_RE = re.compile(r"의견거절|한정의견|감사범위제한|부적정")


def dart_disclosure_events(days: int = 60, page_count: int = 100,
                           max_pages: int = 20) -> tuple[dict, bool]:
    """최근 공시에서 증자/감자·감사의견 이슈 종목을 추출.

    반환: ({종목코드: {"offering": bool, "audit": bool, "titles": [...]}}, 성공여부)
    """
    if not os.getenv("DART_API_KEY"):
        log.info("DART_API_KEY 미설정 → 증자/감사의견 플래그 건너뜀")
        return {}, False

    end = datetime.now(KST)
    start = end - timedelta(days=days)
    out: dict[str, dict] = {}
    ok = False
    for page in range(1, max_pages + 1):
        data = _dart_get("list.json", {
            "bgn_de": start.strftime("%Y%m%d"),
            "end_de": end.strftime("%Y%m%d"),
            "page_no": page, "page_count": page_count, "corp_cls": "Y",
        })
        if data is None:
            break
        ok = True
        items = data.get("list") or []
        for it in items:
            code = (it.get("stock_code") or "").strip()
            if not code:
                continue
            code = code.zfill(6)
            title = (it.get("report_nm") or "").strip()
            rec = out.setdefault(code, {"offering": False, "audit": False,
                                        "titles": []})
            hit = False
            if _OFFERING_RE.search(title):
                rec["offering"] = True
                hit = True
            if _AUDIT_RE.search(title):
                rec["audit"] = True
                hit = True
            if hit and len(rec["titles"]) < 3:
                rec["titles"].append(title[:60])
        if len(items) < page_count:
            break
        time.sleep(0.3 + random.random() * 0.2)

    hits = {k: v for k, v in out.items() if v["offering"] or v["audit"]}
    log.info("DART 공시 스캔: 증자/감사의견 이슈 %d종목", len(hits))
    return hits, ok


# ══════════════════════════ ③ 로컬 시세 기반 ══════════════════════════
def detect_halt_history(store, tickers: dict, lookback_days: int = 90,
                        min_missing: int = 3) -> dict:
    """거래정지 흔적. 시장 거래일 대비 누락일이 있으면 정지로 본다.

    신규 상장 종목은 초기 구간이 비어 있는 게 정상이므로, 해당 종목의
    첫 거래일 이후 구간만 비교한다.
    """
    since = (datetime.now(KST) - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    market_days = sorted(store.existing_dates(since=since))
    if len(market_days) < 20:
        return {}

    mset = set(market_days)
    out: dict[str, int] = {}
    for code in tickers:
        df = store.load_ohlcv(code, days=lookback_days)
        if df is None or len(df) < 10:
            continue
        have = {d.strftime("%Y-%m-%d") for d in df.index}
        first = min(have)
        window = {d for d in mset if d >= first}
        missing = window - have
        if len(missing) >= min_missing:
            out[code] = len(missing)
    log.info("거래정지 흔적 %d종목 (최근 %d일)", len(out), lookback_days)
    return out


def detect_penny_risk(store, tickers: dict) -> dict:
    """1,000원 미만 장기 지속 종목.

    2026년 상장폐지 규정 강화로 1,000원 미만이 30거래일 이어지면
    관리종목 지정 사유가 된다. 외부 데이터 없이 로컬 시세로 선제 판정한다.
    """
    out: dict[str, float] = {}
    for code in tickers:
        df = store.load_ohlcv(code, days=PENNY_DAYS + 10)
        if df is None or len(df) < PENNY_DAYS:
            continue
        tail = df["종가"].tail(PENNY_DAYS)
        if float(tail.max()) < PENNY_PRICE:
            out[code] = float(tail.iloc[-1])
    log.info("동전주 위험 %d종목 (%d일 연속 %d원 미만)",
             len(out), PENNY_DAYS, int(PENNY_PRICE))
    return out


def scan_local_flags(store, tickers: dict, lookback_days: int = 120,
                     min_missing: int = 3,
                     drop_pct: float = -0.45) -> dict:
    """로컬 판정 3종을 시세 1회 조회로 한 번에 처리한다.

    개별 함수를 따로 부르면 종목당 3회, 전종목 8,400회 조회가 된다.
    한 번만 읽고 세 가지를 동시에 판정한다.
    """
    since = (datetime.now(KST) - timedelta(days=lookback_days)
             ).strftime("%Y-%m-%d")
    market_days = set(store.existing_dates(since=since))
    can_check_halt = len(market_days) >= 20
    if not can_check_halt:
        log.warning("DB 거래일 %d일 (<20) — 거래정지 판정 생략", len(market_days))

    halts: dict[str, int] = {}
    pennies: dict[str, float] = {}
    disc: dict[str, str] = {}

    for code in tickers:
        df = store.load_ohlcv(code, days=lookback_days)
        if df is None or len(df) < 10:
            continue

        # ① 거래정지 흔적 — 신규 상장은 초기 구간이 비는 게 정상이므로
        #    해당 종목의 첫 거래일 이후 구간만 비교한다.
        if can_check_halt:
            have = {d.strftime("%Y-%m-%d") for d in df.index}
            first = min(have)
            missing = {d for d in market_days if d >= first} - have
            if len(missing) >= min_missing:
                halts[code] = len(missing)

        close = df["종가"]

        # ② 동전주 위험
        if len(close) >= PENNY_DAYS:
            tail = close.tail(PENNY_DAYS)
            if float(tail.max()) < PENNY_PRICE:
                pennies[code] = float(tail.iloc[-1])

        # ③ 시세 불연속 (액면분할·감자 흔적)
        if len(close) >= 20:
            ret = close.pct_change()
            bad = ret[ret <= drop_pct]
            if len(bad):
                d = bad.index[-1].strftime("%Y-%m-%d")
                disc[code] = f"{d} {float(bad.iloc[-1]) * 100:.1f}%"

    log.info("로컬 판정: 거래정지 %d · 동전주 %d · 시세불연속 %d",
             len(halts), len(pennies), len(disc))
    if disc:
        log.warning("시세 불연속 재적재 권고: %s", list(disc.items())[:5])
    return {"halts": halts, "pennies": pennies, "discontinuity": disc}


def detect_price_discontinuity(store, tickers: dict, days: int = 120,
                               drop_pct: float = -0.45) -> dict:
    """액면분할·감자로 인한 시세 불연속 탐지.

    배제 플래그는 아니지만 반드시 잡아야 한다. 분할이 나면 캐시된 과거
    봉과 신규 봉이 어긋나 1년 고점이 통째로 틀어지고, 피보나치와 청산
    밴드가 전부 엉뚱한 값이 된다. 해당 종목은 재적재해야 한다.
    """
    out: dict[str, str] = {}
    for code in tickers:
        df = store.load_ohlcv(code, days=days)
        if df is None or len(df) < 20:
            continue
        ret = df["종가"].pct_change()
        bad = ret[ret <= drop_pct]
        if len(bad):
            d = bad.index[-1].strftime("%Y-%m-%d")
            out[code] = f"{d} {float(bad.iloc[-1]) * 100:.1f}%"
    if out:
        log.warning("시세 불연속 의심 %d종목 — 재적재 권고: %s",
                    len(out), list(out.items())[:5])
    return out


# ══════════════════════════ ④ 수동 오버라이드 ══════════════════════════
MANUAL_HEADER = ("종목코드", "관리종목", "투자주의환기", "감사의견거절",
                 "자본잠식률", "대규모증자", "거래정지", "동전주위험", "비고")


def load_manual_flags(path: str | Path = "data/flags_manual.csv") -> list[dict]:
    """수동 지정 플래그. 항상 마지막에 적용되어 자동 판정을 덮어쓴다.

    형식(헤더 필수):
      종목코드,관리종목,투자주의환기,감사의견거절,자본잠식률,대규모증자,거래정지,동전주위험,비고
      123456,1,0,0,,0,0,0,직접 확인
    빈 칸은 '건드리지 않음'을 의미한다.
    """
    p = Path(path)
    if not p.exists():
        return []
    rows: list[dict] = []
    try:
        with p.open(encoding="utf-8-sig", newline="") as fh:
            for r in csv.DictReader(fh):
                code = str(r.get("종목코드", "")).strip().zfill(6)
                if len(code) != 6 or not code.isdigit():
                    continue

                def _b(key):
                    v = (r.get(key) or "").strip()
                    return int(v) if v in ("0", "1") else None

                impair = (r.get("자본잠식률") or "").strip()
                rows.append({
                    "ticker": code,
                    "admin_issue": _b("관리종목"),
                    "alert_issue": _b("투자주의환기"),
                    "audit_refusal": _b("감사의견거절"),
                    "capital_impair": float(impair) if impair else None,
                    "recent_offering": _b("대규모증자"),
                    "halt_history": _b("거래정지"),
                    "penny_risk": _b("동전주위험"),
                    "source": "manual",
                    "note": (r.get("비고") or "").strip() or None,
                })
    except (OSError, ValueError) as exc:
        log.warning("수동 플래그 파일 읽기 실패 %s: %s", p, exc)
        return []
    log.info("수동 플래그 %d종목 적용", len(rows))
    return rows


# ══════════════════════════ 통합 갱신 ══════════════════════════
def refresh_flags(store, tickers: dict | None = None,
                  use_fdr: bool = True, use_dart: bool = True,
                  use_local: bool = True, use_manual: bool = True,
                  dart_limit: int = 0, dart_ttl_days: int = 30,
                  offering_days: int = 60) -> dict:
    """전 공급원 갱신. 성공한 공급원의 필드만 초기화 후 재기록한다."""
    started = datetime.now(KST)
    tickers = tickers or store.active_tickers()
    stats: dict = {"tickers": len(tickers)}

    # ── ① 관리종목 ──
    if use_fdr:
        codes, ok = fetch_admin_issues()
        if ok:
            store.clear_flag_field("admin_issue")
            rows = [{"ticker": c, "admin_issue": 1, "source": "fdr"}
                    for c in codes if c in tickers]
            # 목록에 없는 종목은 0 으로 명시해 해제를 반영한다
            rows += [{"ticker": c, "admin_issue": 0, "source": "fdr"}
                     for c in tickers if c not in codes]
            store.upsert_flags(rows)
            stats["admin_issue"] = sum(1 for c in codes if c in tickers)
        else:
            stats["admin_issue"] = "조회 실패 (기존 유지)"

    # ── ② DART 공시 (증자/감사의견) ──
    if use_dart:
        events, ok = dart_disclosure_events(days=offering_days)
        if ok:
            store.clear_flag_field("recent_offering")
            store.clear_flag_field("audit_refusal")
            rows = []
            for c in tickers:
                ev = events.get(c)
                rows.append({
                    "ticker": c,
                    "recent_offering": int(bool(ev and ev["offering"])),
                    "audit_refusal": int(bool(ev and ev["audit"])),
                    "source": "dart",
                    "note": " / ".join(ev["titles"]) if ev and ev["titles"] else None,
                })
            store.upsert_flags(rows)
            stats["recent_offering"] = sum(
                1 for c in tickers if events.get(c, {}).get("offering"))
            stats["audit_refusal"] = sum(
                1 for c in tickers if events.get(c, {}).get("audit"))
        else:
            stats["recent_offering"] = "조회 실패 (기존 유지)"

    # ── ② DART 자본잠식 (TTL 기반 부분 갱신) ──
    if use_dart:
        stats.update(_refresh_impairment(store, tickers, dart_limit,
                                         dart_ttl_days))

    # ── ③ 로컬 판정 (시세 1회 조회로 3종 동시) ──
    if use_local:
        local = scan_local_flags(store, tickers)
        halts, pennies = local["halts"], local["pennies"]
        store.clear_flag_field("halt_history")
        store.clear_flag_field("penny_risk")
        store.upsert_flags([{
            "ticker": c,
            "halt_history": int(c in halts),
            "penny_risk": int(c in pennies),
            "source": "local",
        } for c in tickers])
        stats["halt_history"] = len(halts)
        stats["penny_risk"] = len(pennies)
        stats["price_discontinuity"] = len(local["discontinuity"])
        stats["_discontinuity_list"] = list(local["discontinuity"].items())[:20]

    # ── ④ 수동 오버라이드 (항상 마지막) ──
    if use_manual:
        manual = load_manual_flags()
        if manual:
            store.upsert_flags(manual)
        stats["manual"] = len(manual)

    store.log_run("flags", started,
                  sum(v for v in stats.values() if isinstance(v, int)), 0,
                  note=json.dumps({k: v for k, v in stats.items()
                                   if not k.startswith("_")},
                                  ensure_ascii=False)[:400])
    return stats


def _refresh_impairment(store, tickers: dict, limit: int,
                        ttl_days: int) -> dict:
    """자본잠식률 갱신. 재무는 분기 단위라 TTL 캐시로 호출을 줄인다.

    2,800종목 x 최대 8회 조회는 DART 일일 한도를 압박한다. TTL 이 지난
    종목만 조회하고, --limit 으로 하루 분량을 나눠 돌릴 수 있게 한다.
    """
    corp = dart_corp_codes()
    if not corp:
        return {"capital_impair": "corp_code 없음 (기존 유지)"}

    fresh = store.flag_staleness("capital_impair")
    cutoff = datetime.now(KST).replace(tzinfo=None) - timedelta(days=ttl_days)
    todo = []
    for code in tickers:
        if code not in corp:
            continue
        seen = fresh.get(code)
        if seen:
            try:
                if datetime.fromisoformat(seen) >= cutoff:
                    continue
            except ValueError:
                pass
        todo.append(code)

    if limit:
        todo = todo[:limit]
    if not todo:
        return {"capital_impair": "전부 최신 (조회 생략)"}

    log.info("자본잠식 조회 대상 %d종목 (TTL %d일)", len(todo), ttl_days)
    rows, hits, fails = [], 0, 0
    for i, code in enumerate(todo, 1):
        rate, memo = dart_capital_impairment(corp[code])
        if rate is None:
            fails += 1
        else:
            rows.append({"ticker": code, "capital_impair": rate,
                         "source": "dart", "note": memo})
            if rate >= IMPAIR_WARN:
                hits += 1
        time.sleep(0.25 + random.random() * 0.1)
        if i % 200 == 0:
            store.upsert_flags(rows)
            rows = []
            log.info("  자본잠식 %d/%d (잠식 %d, 데이터없음 %d)",
                     i, len(todo), hits, fails)
    if rows:
        store.upsert_flags(rows)
    return {"capital_impair_checked": len(todo),
            "capital_impair_over50": hits,
            "capital_impair_nodata": fails}


def flag_summary(store) -> pd.DataFrame:
    """플래그 현황 요약. 어떤 종목이 왜 배제되는지 확인용."""
    flags = store.load_flags()
    if not flags:
        return pd.DataFrame()
    names = store.active_tickers()
    rows = []
    for code, f in flags.items():
        reasons = [k for k in ("관리종목", "투자주의환기", "감사의견거절",
                               "자본잠식", "대규모증자", "거래정지이력",
                               "동전주위험") if f.get(k)]
        if not reasons:
            continue
        rows.append({"ticker": code, "name": names.get(code, code),
                     "reasons": ", ".join(reasons),
                     "capital_impair": f.get("자본잠식률")})
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).sort_values("reasons")


# ══════════════════════════ 수동 신용잔고 주입 ══════════════════════════
CREDIT_HEADER = ("종목코드", "신용잔고율", "신용잔고주식수", "기준일", "비고")


def load_manual_credit(path: str | Path = "data/credit_manual.csv") -> list[dict]:
    """수동 주입 신용잔고 CSV 파싱.

    이 시스템의 핵심 가설이 '신용 강제청산'인데 종목별 신용잔고 실측을
    자동으로 확보하지 못하고 있다. 그 사이 구멍을 메우는 경로다.
    한 사람이 KRX/증권사 화면에서 관심종목분만 받아 CSV 로 넣으면
    그 종목들은 프록시가 아니라 실측으로 채점된다.

    형식(헤더 필수):
      종목코드,신용잔고율,신용잔고주식수,기준일,비고
      329180,4.85,1234567,2026-08-22,김차장 제공
      086520,6.20,,2026-08-22,

    신용잔고율만 있어도 동작한다. 상장주식수를 아는 종목은 주식수만
    넣어도 되지만, 그 경우 마스터의 shares 로 비율을 계산한다.
    """
    p = Path(path)
    if not p.exists():
        return []
    rows: list[dict] = []
    try:
        with p.open(encoding="utf-8-sig", newline="") as fh:
            for r in csv.DictReader(fh):
                code = str(r.get("종목코드", "")).strip().zfill(6)
                if len(code) != 6 or not code.isdigit():
                    continue
                ratio = (r.get("신용잔고율") or "").strip().replace("%", "")
                shares = (r.get("신용잔고주식수") or "").strip().replace(",", "")
                asof = (r.get("기준일") or "").strip()[:10]
                if not ratio and not shares:
                    continue
                rows.append({
                    "ticker": code,
                    "ratio": float(ratio) if ratio else None,
                    "shares": float(shares) if shares else None,
                    "asof": asof or None,
                    "source": "manual",
                    "note": (r.get("비고") or "").strip() or None,
                })
    except (OSError, ValueError) as exc:
        log.warning("신용잔고 CSV 읽기 실패 %s: %s", p, exc)
        return []
    log.info("수동 신용잔고 %d종목 파싱", len(rows))
    return rows


def refresh_credit(store, path: str | Path = "data/credit_manual.csv") -> dict:
    """수동 신용잔고를 적재하고, 비율이 빈 종목은 상장주식수로 역산한다."""
    rows = load_manual_credit(path)
    if not rows:
        return {"parsed": 0, "stored": 0, "derived": 0,
                "note": f"{path} 없음 또는 유효 행 없음"}

    meta = store.ticker_meta()
    derived = 0
    for r in rows:
        if r["ratio"] is None and r["shares"] and r["ticker"] in meta.index:
            listed = meta.at[r["ticker"], "shares"]
            if pd.notna(listed) and float(listed) > 0:
                r["ratio"] = round(float(r["shares"]) / float(listed) * 100.0, 3)
                derived += 1

    usable = [r for r in rows if r["ratio"] is not None]
    stored = store.upsert_credit(usable)
    cov = store.credit_coverage()
    log.info("신용잔고 적재 %d종목 (주식수 역산 %d) · 커버리지 %d/%d · 기준일 %s",
             stored, derived, cov["with_credit"], cov["active"], cov["asof"])
    return {"parsed": len(rows), "stored": stored, "derived": derived,
            "coverage": cov}
