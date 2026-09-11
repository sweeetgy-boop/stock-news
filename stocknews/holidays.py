# -*- coding: utf-8 -*-
"""공휴일 사전 필터 — 한국천문연구원 특일정보 API.

역할
----
휴장일 판정(`trading_day.market_status`)의 **앞단**이다. 공휴일이면 기준
종목 프로브를 건너뛰고 CLOSED 로 확정한다. 프로브 로직 자체는 그대로다.

절대 하지 않는 것
----------------
- 공휴일 API 만으로 **거래일을 확정하지 않는다.** 임시휴장·조기폐장은
  이 API 에 없다. 공휴일이 아니면 종전대로 기준 종목 프로브가 판정한다.
- 테이블이 비어 있거나 API 가 죽어도 판정을 막지 않는다. 그냥 프로브로 간다.
- getHoliDeInfo(국경일)는 쓰지 않는다. 제헌절이 isHoliday=N 으로 섞인다.
  getRestDeInfo 의 isHoliday == "Y" 만 쓴다.

호출
----
  GET https://apis.data.go.kr/B090041/openapi/service/SpcdeInfoService/getRestDeInfo
      ServiceKey=<키>  solYear=YYYY  _type=json  numOfRows=100
  solMonth 는 주지 않는다 — 1년치를 한 번에 받는다. 키는 .env 의
  DATA_GO_KR_KEY. **requests params 로 넘긴다.** URL 에 직접 이어 붙이면
  키의 특수문자가 다시 인코딩돼 401 이 난다.
"""
from __future__ import annotations

import logging
import os
from urllib.parse import unquote
from datetime import datetime, timedelta, timezone

import requests

from .config import EXTRA_MARKET_HOLIDAYS

log = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))
API_URL = ("https://apis.data.go.kr/B090041/openapi/service/"
           "SpcdeInfoService/getRestDeInfo")
ENV_KEY = "DATA_GO_KR_KEY"
SOURCE = "data.go.kr:getRestDeInfo"
NUM_OF_ROWS = 100
TIMEOUT = 15.0

__all__ = ["API_URL", "ENV_KEY", "SOURCE", "fetch_holidays", "parse_response",
           "normalize_key",
           "refresh_holidays", "extra_market_holiday", "holiday_name"]


def parse_response(data: dict, num_of_rows: int = NUM_OF_ROWS) -> tuple[list[dict], dict]:
    """응답 JSON → (행 목록, 메타). isHoliday == "Y" 만 남긴다.

    body.items 는 결과가 0건이면 "" 이고, 1건이면 item 이 dict, 여러 건이면
    list 다. 셋 다 받는다.
    """
    body = ((data or {}).get("response") or {}).get("body") or {}
    header = ((data or {}).get("response") or {}).get("header") or {}
    total = int(body.get("totalCount") or 0)
    items = body.get("items") or {}
    raw = items.get("item") if isinstance(items, dict) else None
    if raw is None:
        raw = []
    elif isinstance(raw, dict):
        raw = [raw]

    rows: list[dict] = []
    for it in raw:
        if str(it.get("isHoliday", "")).strip().upper() != "Y":
            continue
        loc = str(it.get("locdate", "")).strip()
        if len(loc) != 8 or not loc.isdigit():
            continue
        rows.append({"d": f"{loc[:4]}-{loc[4:6]}-{loc[6:]}",
                     "name": str(it.get("dateName") or "").strip(),
                     "source": SOURCE})
    meta = {"total": total, "returned": len(raw), "kept": len(rows),
            "result_code": str(header.get("resultCode", "")),
            "result_msg": str(header.get("resultMsg", ""))}
    if total > num_of_rows:
        # 한 해 공휴일은 20건 남짓이라 100 이면 남는다. 넘으면 페이지가
        # 잘린 것이니 사람이 봐야 한다.
        log.warning("공휴일 API totalCount=%d > numOfRows=%d — 뒷 페이지 미조회",
                    total, num_of_rows)
        meta["truncated"] = True
    return rows, meta


def normalize_key(key: str) -> str:
    """data.go.kr 는 키를 Encoding/Decoding 두 형태로 준다. 여기서는 **Decoding
    키**가 필요하다 — requests 가 params 를 인코딩하므로 Encoding 키를 주면
    '%2B' 가 '%252B' 로 두 번 인코딩돼 '등록되지 않은 서비스키'(403)가 난다.
    2026-09-09 실측. '%' 가 있고 '+', '/' 가 없으면 Encoding 키로 보고 디코딩한다.
    """
    k = (key or "").strip()
    if "%" in k and not any(ch in k for ch in "+/"):
        log.info("%s 가 Encoding 형태라 디코딩해서 씁니다 (.env 에는 Decoding 키 권장)", ENV_KEY)
        return unquote(k)
    return k


def fetch_holidays(year: int, key: str | None = None) -> tuple[list[dict], dict]:
    """한 해 공휴일. (행 목록, 메타). 키가 없으면 ValueError."""
    key = key or os.getenv(ENV_KEY)
    if not key:
        raise ValueError(f"{ENV_KEY} 미설정 — .env 에 넣으십시오 (발급: data.go.kr)")
    key = normalize_key(key)
    res = requests.get(API_URL, params={
        "ServiceKey": key,            # 명세대로 대문자 S
        "solYear": f"{int(year):04d}",
        "_type": "json",
        "numOfRows": NUM_OF_ROWS,
    }, timeout=TIMEOUT)
    if res.status_code != 200:
        raise RuntimeError(f"공휴일 API HTTP {res.status_code} ({year})")
    try:
        data = res.json()
    except ValueError as exc:
        raise RuntimeError(f"공휴일 API 응답이 JSON 이 아님 ({year}): "
                           f"{res.text[:120]!r}") from exc
    rows, meta = parse_response(data)
    if meta["result_code"] not in ("00", "0", ""):
        raise RuntimeError(f"공휴일 API resultCode={meta['result_code']} "
                           f"{meta['result_msg']} ({year})")
    return rows, meta


def refresh_holidays(store, years: tuple[int, ...] | list[int],
                     key: str | None = None) -> dict:
    """연도별 조회 → holidays 테이블 적재. 한 해가 실패해도 나머지는 진행.

    반환 {"years": {연도: {"kept": n, "total": n} | {"error": 사유}},
          "stored": 총 적재 행, "failed": [연도...]}
    """
    out: dict = {"years": {}, "stored": 0, "failed": []}
    for y in years:
        try:
            rows, meta = fetch_holidays(y, key=key)
            n = store.upsert_holidays(rows)
            out["years"][int(y)] = {"kept": meta["kept"], "total": meta["total"],
                                    "truncated": bool(meta.get("truncated"))}
            out["stored"] += n
            log.info("공휴일 %d년: %d건 적재 (API total %d)", y, n, meta["total"])
        except Exception as exc:  # noqa: BLE001 - API 실패가 판정을 막으면 안 된다
            out["years"][int(y)] = {"error": f"{type(exc).__name__}: {exc}"}
            out["failed"].append(int(y))
            log.warning("공휴일 %d년 조회 실패 (기존 테이블 유지): %s", y, exc)
    return out


def extra_market_holiday(d) -> str | None:
    """config.EXTRA_MARKET_HOLIDAYS 에 해당하면 명칭, 아니면 None."""
    day = d if hasattr(d, "month") else datetime.strptime(str(d)[:10], "%Y-%m-%d").date()
    return EXTRA_MARKET_HOLIDAYS.get((day.month, day.day))


def holiday_name(store, d) -> str | None:
    """그 날이 휴장일이면 명칭. 공휴일 테이블 → 증시 전용 보완 순.

    store 에 holiday_name 이 없어도(구형 저장소·테스트 대역) 죽지 않는다.
    """
    ds = str(d)[:10]
    getter = getattr(store, "holiday_name", None)
    if callable(getter):
        try:
            name = getter(ds)
            if name:
                return name
        except Exception as exc:  # noqa: BLE001
            log.warning("holidays 테이블 조회 실패(무시): %s", exc)
    return extra_market_holiday(ds)
