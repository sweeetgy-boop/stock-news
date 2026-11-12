# -*- coding: utf-8 -*-
"""[E] 리밸런싱 점검 — 보유 파일(data/holdings.csv)의 비중 한도 위반과
'한도까지 줄일 금액'만 보인다. 읽기 전용(`--mode rebalance`).

하지 않는 것
------------
  · 매수 제안 · 추천 종목과의 연결 — 이 리포트는 recos/scans 를 읽지 않는다.
  · 손익 금액 — 평단은 시세가 없을 때의 대체 평가에만 쓰고 표시하지 않는다.
  · 텔레그램 발송 — 보유 내역은 개인 정보다. 콘솔(+ --json)에만 낸다.

보유 파일 (data/holdings.csv · 커밋 금지 · 템플릿 data/holdings.csv.example)
  code,qty,avg_price        (한글 머리글 코드/종목코드,수량,평단/평균단가 도 됨)
  005930,10,71000
  069500,5,35000
  CASH,1500000,             ← 현금(원). qty 칸에 금액
  '#' 로 시작하는 줄은 주석.

평가
  · 주식   DB 종가(기준일 이하 마지막 종가)
  · ETF    KRX 오픈API etp/etf_bydd_trd 종가(TDD_CLSPRC)
  · ETN    KRX 오픈API etp/etn_bydd_trd 종가(TDD_CLSPRC)
  · 둘 다 없으면 평단 × 수량(대체 평가 표시), 평단도 없으면 제외
  ETF/ETN 일별시세는 D일분이 익영업일 08:00 이후 열린다. 기준일이 비어
  있으면 그 앞 거래일로 최대 3일 물러난다.

한도 (config E 구역 · 사람이 바꾼다)
  종목 REBAL_MAX_STOCK_PCT(10%) · 섹터 REBAL_MAX_SECTOR_PCT(30%) ·
  레버리지 REBAL_MAX_LEVERAGE_PCT(10%) · 밴드 REBAL_BAND_PCT(±5%p)
  위반 = 비중 > 한도 + 밴드. 줄일 금액 = 평가액 − 한도 × 총평가액
  (판 돈은 현금으로 계좌에 남는다고 보고 총평가액을 유지한다).
  종목 한도는 개별 주식에만 건다 — ETF/ETN 은 그 자체가 묶음이라 섹터·
  레버리지 한도로 본다. 섹터는 주식 = tickers.sector, ETF/ETN =
  ETF_SECTOR_OVERRIDE. 표에 없는 ETF/ETN 은 '미분류' 로 두고 섹터 한도에
  넣지 않는다(개수만 알린다). 레버리지 = 이름 키워드 또는
  REBAL_LEVERAGE_OVERRIDE.
"""
from __future__ import annotations

import csv
import logging
import math
import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from . import config as C

log = logging.getLogger(__name__)

__all__ = ["Holding", "read_holdings", "value_holdings", "check_limits",
           "render_rebalance", "summary", "fetch_etp", "is_leverage",
           "ETP_SERVICES", "FOOTER"]

ETP_API_BASE = "https://data-dbg.krx.co.kr/svc/apis/etp/"
ETP_SERVICES = {"ETF": "etf_bydd_trd", "ETN": "etn_bydd_trd"}
CASH_CODES = {"CASH", "현금"}
# '매수/매도' 단어를 쓰지 않는다(공통 규칙). smoke 가 부재를 검사한다.
FOOTER = ("※ 위반 항목과 줄일 금액만 표시 · 편입 제안 없음 · 손익 미표시 · "
          "검증 중 · 투자 판단 근거 아님")

_COL_CODE = ("code", "코드", "종목코드", "ticker")
_COL_QTY = ("qty", "수량", "quantity")
_COL_AVG = ("avg_price", "평단", "평균단가", "avg")


@dataclass
class Holding:
    code: str
    qty: float
    avg: float | None = None
    name: str = ""
    kind: str = ""            # 주식 / ETF / ETN / 현금 / 미확인
    price: float | None = None
    price_d: str | None = None
    value: float = 0.0
    sector: str | None = None
    leverage: bool = False
    valued_by: str = ""       # 종가 / KRX / 평단(대체) / 현금 / 제외


def _num(x) -> float | None:
    if x is None:
        return None
    s = str(x).strip().replace(",", "")
    if not s:
        return None
    try:
        v = float(s)
    except ValueError:
        return None
    return v if math.isfinite(v) else None


def _norm_code(x: str) -> str:
    s = str(x).strip().upper()
    if s in CASH_CODES:
        return "CASH"
    if s.startswith("A") and len(s) == 7:      # HTS 표기 A005930
        s = s[1:]
    return s.zfill(6) if s.isdigit() else s


def read_holdings(path) -> list[Holding]:
    """보유 파일을 읽는다. 없으면 FileNotFoundError. 형식 오류는 ValueError."""
    p = Path(path)
    text = p.read_text(encoding="utf-8-sig")
    lines = [ln for ln in text.splitlines()
             if ln.strip() and not ln.lstrip().startswith("#")]
    if not lines:
        return []
    rdr = csv.reader(lines)
    head = [h.strip().lower() for h in next(rdr)]

    def _idx(names) -> int | None:
        for n in names:
            if n.lower() in head:
                return head.index(n.lower())
        return None

    ic, iq, ia = _idx(_COL_CODE), _idx(_COL_QTY), _idx(_COL_AVG)
    if ic is None or iq is None:
        raise ValueError(f"{p}: 머리글에 code·qty(코드·수량) 열이 없습니다 "
                         f"— 받은 머리글 {head}")
    out: dict[str, Holding] = {}
    for n, row in enumerate(rdr, 2):
        if not row or not str(row[ic]).strip():
            continue
        code = _norm_code(row[ic])
        qty = _num(row[iq]) if iq < len(row) else None
        if qty is None or qty < 0:
            raise ValueError(f"{p}:{n} 수량이 숫자가 아닙니다 ({code})")
        avg = _num(row[ia]) if (ia is not None and ia < len(row)) else None
        if code in out:                 # 같은 코드 두 줄 = 합산(평단 가중)
            h = out[code]
            if h.avg is not None and avg is not None and h.qty + qty > 0:
                h.avg = (h.avg * h.qty + avg * qty) / (h.qty + qty)
            h.qty += qty
        else:
            out[code] = Holding(code=code, qty=qty, avg=avg)
    return list(out.values())


def is_leverage(code: str, name: str) -> bool:
    ov = getattr(C, "REBAL_LEVERAGE_OVERRIDE", {}) or {}
    if code in ov:
        return bool(ov[code])
    up = str(name or "").upper()
    return any(k.upper() in up for k in C.REBAL_LEVERAGE_KEYWORDS)


def fetch_etp(kind: str, bas_dd: str, key: str | None = None,
              timeout: float = 30.0) -> list[dict]:
    """KRX 오픈API ETF/ETN 일별매매정보. 실패는 예외로 올린다."""
    import requests
    key = key or os.getenv("KRX_API_KEY", "").strip()
    if not key:
        raise RuntimeError("KRX_API_KEY 미설정")
    res = requests.get(ETP_API_BASE + ETP_SERVICES[kind],
                       params={"basDd": bas_dd}, headers={"AUTH_KEY": key},
                       timeout=timeout)
    if res.status_code != 200:
        raise RuntimeError(f"HTTP {res.status_code}: {res.text[:120]}")
    rows = res.json().get("OutBlock_1")
    if not isinstance(rows, list):
        raise ValueError(f"OutBlock_1 없음: {res.text[:120]}")
    return rows


def _db_prices(store, codes: list[str], asof: str) -> dict:
    """주식: 기준일 이하 마지막 종가 + 이름·섹터."""
    if not codes:
        return {}
    q = ",".join("?" * len(codes))
    with closing(sqlite3.connect(store.path, timeout=30.0)) as con:
        px = con.execute(
            f"SELECT p.ticker, p.d, p.c FROM prices p JOIN ("
            f"  SELECT ticker, MAX(d) md FROM prices WHERE d<=? AND c>0 "
            f"  AND ticker IN ({q}) GROUP BY ticker) m "
            f"ON p.ticker=m.ticker AND p.d=m.md", [asof, *codes]).fetchall()
        meta = con.execute(
            f"SELECT ticker, name, sector FROM tickers WHERE ticker IN ({q})",
            codes).fetchall()
    m = {t: (n, s) for t, n, s in meta}
    return {t: {"d": d, "c": float(c), "name": (m.get(t) or ("", None))[0],
                "sector": (m.get(t) or ("", None))[1]} for t, d, c in px}


def value_holdings(store, holdings: list[Holding], asof: str | None = None,
                   fetch=None, calendar: list[str] | None = None) -> dict:
    """평가. fetch(kind, bas_dd) -> rows 는 smoke 가 바꿔 끼운다."""
    from .events import trading_calendar
    asof = asof or store.last_price_date()
    fetch = fetch or (lambda kind, bas: fetch_etp(kind, bas))
    notes: list[str] = []
    stocks = [h.code for h in holdings if h.code != "CASH"]
    db = _db_prices(store, stocks, asof) if asof else {}
    etp_need = [h for h in holdings if h.code != "CASH" and h.code not in db]
    etp: dict[str, dict] = {}
    etp_d = None
    etp_err = None
    if etp_need:
        cal = calendar if calendar is not None else trading_calendar(store)
        cands = [d for d in cal if d <= (asof or "9999")][-4:][::-1] or \
            ([asof] if asof else [])
        for d in cands:
            bas = d.replace("-", "")
            got: dict[str, dict] = {}
            try:
                for kind in ("ETF", "ETN"):
                    for r in fetch(kind, bas) or []:
                        code = str(r.get("ISU_CD", "")).strip().zfill(6)
                        px = _num(r.get("TDD_CLSPRC"))
                        if not px:
                            px = _num(r.get("NAV")) or _num(
                                r.get("PER1SECU_INDIC_VAL"))
                        if px and px > 0:
                            got[code] = {"kind": kind, "c": px,
                                         "name": str(r.get("ISU_NM", ""))}
            except Exception as exc:  # noqa: BLE001
                etp_err = f"{type(exc).__name__}: {exc}"[:160]
                log.warning("ETF/ETN 시세 조회 실패 basDd=%s: %s", bas, etp_err)
                break
            if got:
                etp, etp_d = got, d
                break
        if etp_err:
            notes.append(f"ETF/ETN 시세 조회 실패 — 평단 대체 ({etp_err})")

    for h in holdings:
        if h.code == "CASH":
            h.kind, h.value, h.valued_by = "현금", float(h.qty), "현금"
            continue
        if h.code in db:
            r = db[h.code]
            h.kind, h.name, h.sector = "주식", r["name"] or h.code, r["sector"]
            h.price, h.price_d, h.valued_by = r["c"], r["d"], "종가"
        elif h.code in etp:
            r = etp[h.code]
            h.kind, h.name, h.price = r["kind"], r["name"], r["c"]
            h.price_d, h.valued_by = etp_d, "KRX"
            ov = getattr(C, "ETF_SECTOR_OVERRIDE", {}) or {}
            h.sector = ov.get(h.code)
            h.leverage = is_leverage(h.code, h.name)
        elif h.avg:
            h.kind, h.name, h.price, h.valued_by = "미확인", h.code, h.avg, \
                "평단(대체)"
            ov = getattr(C, "ETF_SECTOR_OVERRIDE", {}) or {}
            h.sector = ov.get(h.code)
            h.leverage = is_leverage(h.code, "")
        else:
            h.kind, h.valued_by = "미확인", "제외"
            notes.append(f"{h.code}: 시세·평단 모두 없음 — 평가 제외")
            continue
        h.value = float(h.qty) * float(h.price)
    stale = [h for h in holdings if h.kind == "주식" and h.price_d
             and asof and h.price_d < asof]
    if stale:
        notes.append("기준일 종가 없음(직전 종가로 평가): "
                     + ", ".join(f"{h.name}({h.price_d})" for h in stale[:5]))
    sub = [h for h in holdings if h.valued_by == "평단(대체)"]
    if sub:
        notes.append(f"시세 없음 → 평단 대체 평가 {len(sub)}종목: "
                     + ", ".join(h.code for h in sub[:5]))
    return {"asof": asof, "etp_d": etp_d, "holdings": holdings,
            "notes": notes}


def check_limits(holdings: list[Holding], limits: dict | None = None) -> dict:
    """한도 점검. 위반만 돌려준다."""
    lim = {"stock": C.REBAL_MAX_STOCK_PCT, "sector": C.REBAL_MAX_SECTOR_PCT,
           "leverage": C.REBAL_MAX_LEVERAGE_PCT, "band": C.REBAL_BAND_PCT}
    lim.update(limits or {})
    total = sum(h.value for h in holdings if h.value > 0)
    out = {"limits": lim, "total_positive": total > 0, "violations": [],
           "unclassified": 0}
    if total <= 0:
        return out

    def _viol(kind: str, label: str, value: float, limit: float) -> None:
        w = value / total * 100.0
        if w > limit + lim["band"] + 1e-9:
            out["violations"].append({
                "kind": kind, "label": label, "weight_pct": round(w, 2),
                "limit_pct": float(limit),
                "trigger_pct": float(limit + lim["band"]),
                "reduce_amount": int(math.ceil(value - limit / 100.0 * total)),
            })

    for h in holdings:
        if h.kind == "주식" and h.value > 0:
            _viol("종목", f"{h.name}({h.code})", h.value, lim["stock"])
    sectors: dict[str, float] = {}
    for h in holdings:
        if h.kind == "현금" or h.value <= 0:
            continue
        if h.sector:
            sectors[h.sector] = sectors.get(h.sector, 0.0) + h.value
        else:
            out["unclassified"] += 1
    for sec, v in sorted(sectors.items(), key=lambda kv: -kv[1]):
        _viol("섹터", sec, v, lim["sector"])
    lev = sum(h.value for h in holdings if h.leverage and h.value > 0)
    if lev > 0:
        _viol("레버리지", "레버리지·인버스 합계", lev, lim["leverage"])
    order = {"종목": 0, "섹터": 1, "레버리지": 2}
    out["violations"].sort(key=lambda v: (order[v["kind"]], -v["weight_pct"]))
    return out


def render_rebalance(val: dict, chk: dict) -> str:
    """콘솔 리포트. 위반 항목과 줄일 금액만. 손익·매수 제안 없음."""
    lim = chk["limits"]
    hs = [h for h in val["holdings"] if h.kind != "현금"]
    out = [f"⚖️ [리밸런싱 점검] 기준일 {val.get('asof')} · 보유 {len(hs)}종목",
           f"한도 종목 {lim['stock']:g}% · 섹터 {lim['sector']:g}% · 레버리지 "
           f"{lim['leverage']:g}% · 밴드 ±{lim['band']:g}%p "
           f"(한도+밴드 초과 시 표시, 한도까지 줄일 금액)"]
    if not chk.get("total_positive"):
        out.append("• 평가 가능한 보유가 없습니다")
    elif not chk["violations"]:
        out.append("• 위반 없음")
    for v in chk["violations"]:
        out.append(f"• {v['kind']} {v['label']} 비중 {v['weight_pct']:.1f}% "
                   f"(한도 {v['limit_pct']:g}%) → 줄일 금액 "
                   f"{v['reduce_amount']:,}원")
    if chk.get("unclassified"):
        out.append(f"※ 섹터 미분류 ETF/ETN {chk['unclassified']}종목 — 섹터 "
                   "한도 제외 (config ETF_SECTOR_OVERRIDE 에 넣으면 포함)")
    src = "주식 DB 종가"
    if val.get("etp_d"):
        src += f" · ETF/ETN KRX 일별시세({val['etp_d']})"
    out.append(f"※ 평가: {src}")
    for n in val.get("notes") or []:
        out.append(f"※ {n}")
    out.append(FOOTER)
    return "\n".join(out)


def summary(val: dict, chk: dict) -> dict:
    """--json 용. 손익·평단은 넣지 않는다."""
    return {"asof": val.get("asof"), "etp_d": val.get("etp_d"),
            "holdings": sum(1 for h in val["holdings"] if h.kind != "현금"),
            "limits": chk["limits"], "violations": chk["violations"],
            "unclassified": chk.get("unclassified", 0),
            "notes": val.get("notes") or []}

