# -*- coding: utf-8 -*-
"""키움 OpenAPI+ 연동 — 계획·제한·환경 진단.

이 모듈은 OCX 를 건드리지 않습니다
==================================
키움 OpenAPI+ 는 **32비트 OCX(COM)** 입니다. 64비트 프로세스는 32비트
OCX 를 인프로세스로 로드할 수 없습니다. 이 저장소의 본체는 64비트
파이썬으로 돌아가므로, OCX 호출은 별도 32비트 프로세스
(`kiwoom_bridge.py`)가 맡고 결과를 CSV 로 떨어뜨립니다. 본체는 이미
검증된 `--mode credit` 경로로 그 CSV 를 읽습니다.

    [32비트] kiwoom_bridge.py --> data/credit_kiwoom.csv
                                        |
    [64비트] run_screen.py --mode credit  (기존 경로, 테스트됨)

이 파일에는 **OCX 없이 검증 가능한 부분만** 둡니다. 그래서 지금 이
환경에서도 스모크 테스트로 전부 확인됩니다.

TR 규격은 확정입니다 (추측 아님)
-------------------------------
`C:\\OpenAPI` 의 키움 정의 파일에서 직접 읽었습니다.

    koatrinputlegend.ini   TR 입력 필드
    koascreentrmap.ini     화면번호 <-> TR 매핑

    OPT10013  신용매매동향요청   화면 0141
              종목코드 / 일자(YYYYMMDD) / 조회구분(1:융자, 2:대주)
    OPT10033  신용비율상위요청
              시장구분 / 신용조건 / 거래량조건 ...  (상위 랭킹)
    OPT10014  공매도추이요청     화면 0142
    OPW20016  신용융자 가능종목요청

**출력 필드명은 확정하지 못했습니다.** `C:\\OpenAPI\\data\\opt10013.enc`
로 암호화돼 있어 오프라인으로 읽을 수 없습니다. 그래서 하드코딩하지
않고 런타임에 후보를 탐침해 매핑을 만들고 파일에 고정합니다
(`data/kiwoom_fields.json`). KRX 컬럼을 다룰 때와 같은 원칙입니다.

호출 제한이 설계를 결정합니다
---------------------------
    초당 5건 / 분당 100건 / 시간당 1,000건

**시간당 1,000건이 지배적입니다.** 평균 3.6초에 1건입니다. 전종목
2,800개를 종목별로 받으면 2.8시간이 걸립니다. 매일 돌릴 수 없습니다.
게다가 키움 문서상 분당·시간당 제한에 걸리면 **프로그램을 재실행해야**
합니다. 그래서 제한에 닿지 않도록 안전마진을 두고 스스로 조절합니다.

전략은 전수조사가 아니라 이렇습니다.

    1) OPT10033 신용비율상위  요청 1회로 신용 과열 상위 종목을 받는다.
                              전략이 원하는 건 애초에 '과열' 쪽이다.
    2) OPT10013 종목별        보유 포지션 + 최근 추천 + 밴드 후보만.
                              수백 종목이면 수십 분에 끝난다.

나머지 종목은 지금처럼 매물대 POC 프록시로 남습니다. 그게 정직합니다.
"""
from __future__ import annotations

import csv
import json
import logging
import os
import platform
import struct
import time
from collections import deque
from pathlib import Path

log = logging.getLogger(__name__)

# ── TR 코드 (키움 정의 파일에서 확인) ──
TR_CREDIT_TREND = "opt10013"      # 신용매매동향요청
TR_CREDIT_TOP = "opt10033"        # 신용비율상위요청
TR_SHORT_TREND = "opt10014"       # 공매도추이요청
TR_CREDIT_ELIGIBLE = "opw20016"   # 신용융자 가능종목요청

SCREEN_CREDIT_TREND = "0141"
SCREEN_SHORT_TREND = "0142"

# ── TR 입력 필드 (koatrinputlegend.ini 실측) ──
TR_INPUTS: dict[str, tuple[str, ...]] = {
    TR_CREDIT_TREND: ("종목코드", "일자", "조회구분"),
    TR_CREDIT_TOP: ("시장구분", "신용조건", "거래량조건",
                    "종목조건", "가격조건"),
    TR_SHORT_TREND: ("종목코드", "시간구분", "시작일자", "종료일자"),
}

# 조회구분 값. koatrinputlegend.ini: "조회구분=1:융자, 2:대주"
INQUIRY_LOAN = "1"      # 융자
INQUIRY_SHORT_LOAN = "2"   # 대주

# ── 호출 제한 (키움 공식) ──
LIMIT_PER_SECOND = 5
LIMIT_PER_MINUTE = 100
LIMIT_PER_HOUR = 1000

# 안전마진. 제한에 닿으면 프로그램 재실행이 강제되므로 여유를 둔다.
SAFE_PER_SECOND = 4
SAFE_PER_MINUTE = 90
SAFE_PER_HOUR = 900

DEFAULT_OPENAPI_DIR = r"C:\OpenAPI"
OCX_PROGID = "KHOPENAPI.KHOpenAPICtrl.1"
CREDIT_CSV = "data/credit_kiwoom.csv"
FIELD_MAP_PATH = "data/kiwoom_fields.json"

# 출력 필드 후보. **확정된 값이 아니다.** 런타임 탐침용 목록이며,
# 무엇이 맞는지는 bridge 가 실제 응답으로 판정해 파일에 고정한다.
CREDIT_FIELD_CANDIDATES: dict[str, tuple[str, ...]] = {
    "date": ("일자", "체결일", "거래일"),
    "close": ("현재가", "종가"),
    "volume": ("거래량",),
    "loan_new": ("신규", "융자신규", "신규금액"),
    "loan_repay": ("상환", "융자상환", "상환금액"),
    "loan_balance": ("잔고", "융자잔고", "잔고금액", "신용잔고"),
    "loan_ratio": ("잔고비율", "신용비율", "융자잔고비율", "신용잔고비율"),
}

__all__ = [
    "TR_CREDIT_TREND", "TR_CREDIT_TOP", "TR_SHORT_TREND",
    "TR_CREDIT_ELIGIBLE", "TR_INPUTS", "INQUIRY_LOAN",
    "LIMIT_PER_SECOND", "LIMIT_PER_MINUTE", "LIMIT_PER_HOUR",
    "SAFE_PER_SECOND", "SAFE_PER_MINUTE", "SAFE_PER_HOUR",
    "CREDIT_CSV", "FIELD_MAP_PATH", "CREDIT_FIELD_CANDIDATES",
    "RateLimiter", "estimate_seconds", "check_environment",
    "select_targets", "build_plan", "write_credit_csv",
    "load_field_map", "save_field_map",
]


# ══════════════════════════ 호출 제한 ══════════════════════════
class RateLimiter:
    """초/분/시간 3중 슬라이딩 윈도 제한기.

    시계를 주입할 수 있어 테스트에서 실제로 기다리지 않는다. 또
    `estimate_seconds()` 가 같은 로직으로 소요시간을 미리 계산한다.
    제한 로직과 예측 로직이 갈라지면 예측이 무의미해지므로 하나만 둔다.
    """

    def __init__(self, per_second: int = SAFE_PER_SECOND,
                 per_minute: int = SAFE_PER_MINUTE,
                 per_hour: int = SAFE_PER_HOUR,
                 clock=time.monotonic):
        if per_second < 1 or per_minute < 1 or per_hour < 1:
            raise ValueError("제한값은 1 이상이어야 합니다")
        self.windows = ((1.0, int(per_second)), (60.0, int(per_minute)),
                        (3600.0, int(per_hour)))
        self._clock = clock
        self._hits: deque[float] = deque()

    # ── 내부 ──
    def _prune(self, now: float) -> None:
        longest = max(span for span, _ in self.windows)
        while self._hits and self._hits[0] <= now - longest:
            self._hits.popleft()

    def wait_seconds(self, now: float | None = None) -> float:
        """지금 호출하면 제한을 넘는가. 넘으면 필요한 대기 초."""
        now = self._clock() if now is None else now
        self._prune(now)
        need = 0.0
        for span, cap in self.windows:
            start = now - span
            inside = [t for t in self._hits if t > start]
            if len(inside) < cap:
                continue
            # cap 번째로 오래된 호출이 창을 벗어나야 한다.
            oldest = inside[len(inside) - cap]
            need = max(need, oldest + span - now)
        return max(0.0, need)

    def record(self, now: float | None = None) -> None:
        now = self._clock() if now is None else now
        self._hits.append(now)
        self._prune(now)

    def acquire(self, sleep=time.sleep) -> float:
        """제한을 지킬 만큼 기다린 뒤 1건을 기록. 기다린 초를 반환."""
        waited = 0.0
        while True:
            w = self.wait_seconds()
            if w <= 0:
                break
            sleep(w)
            waited += w
        self.record()
        return waited

    def used(self, now: float | None = None) -> dict:
        """창별 사용량. 진행 로그에 찍어 사람이 확인할 수 있게."""
        now = self._clock() if now is None else now
        self._prune(now)
        out = {}
        for span, cap in self.windows:
            key = {1.0: "second", 60.0: "minute", 3600.0: "hour"}[span]
            out[key] = {"used": sum(1 for t in self._hits if t > now - span),
                        "cap": cap}
        return out


def estimate_seconds(n_requests: int, per_second: int = SAFE_PER_SECOND,
                     per_minute: int = SAFE_PER_MINUTE,
                     per_hour: int = SAFE_PER_HOUR) -> float:
    """요청 n건에 걸리는 시간(초). 실제로 기다리지 않고 계산한다.

    제한기와 같은 로직을 가상 시계로 돌린다. 별도 공식을 쓰면 둘이
    갈라져서 예측이 틀린다.
    """
    if n_requests <= 0:
        return 0.0
    lim = RateLimiter(per_second, per_minute, per_hour, clock=lambda: 0.0)
    t = 0.0
    for _ in range(int(n_requests)):
        t += lim.wait_seconds(now=t)
        lim.record(now=t)
    return t


# ══════════════════════════ 환경 진단 ══════════════════════════
def check_environment(openapi_dir: str | None = None) -> dict:
    """OCX 를 실제로 쓸 수 있는 상태인지 확인한다.

    64비트 파이썬으로도 돌아간다. 안 되는 이유를 정확히 알려주는 것이
    목적이다. '실패'가 아니라 '무엇이 빠졌는지'를 낸다.
    """
    d = Path(openapi_dir or os.getenv("KIWOOM_OPENAPI_DIR")
             or DEFAULT_OPENAPI_DIR)
    bits = struct.calcsize("P") * 8
    ocx = d / "khopenapi.ocx"

    rep: dict = {
        "os": platform.system(),
        "python_bits": bits,
        "python_version": platform.python_version(),
        "openapi_dir": str(d),
        "ocx_present": ocx.exists(),
        "koa_studio": (d / "KOAStudioSA.exe").exists(),
        "tr_defs": (d / "koatrinputlegend.ini").exists(),
    }
    reg = _ocx_registration()
    rep.update(reg)
    for mod in ("win32com", "pythoncom", "PyQt5"):
        rep[mod] = _importable(mod)

    missing = []
    if rep["os"] != "Windows":
        missing.append("Windows 가 아닙니다. OCX 를 쓸 수 없습니다.")
    if bits != 32:
        why = ""
        if rep.get("ocx_wow64_only"):
            why = (" 레지스트리 확인 결과 InprocServer32 가 WOW6432Node "
                   "아래에만 있습니다(32비트 전용 등록).")
        missing.append(
            f"현재 파이썬이 {bits}비트입니다. OCX 는 32비트라 인프로세스 "
            f"로드가 불가능합니다.{why} 브릿지용 32비트 파이썬이 "
            "필요합니다 (본체는 64비트 유지).")
    if not rep["ocx_present"]:
        missing.append(f"{ocx} 가 없습니다. OpenAPI+ 모듈을 설치하십시오.")
    elif not rep["ocx_registered"]:
        missing.append(
            "OCX 가 레지스트리에 등록되지 않았습니다. 관리자 권한으로 "
            f'regsvr32 "{ocx}" 를 실행하십시오.')
    if not rep["win32com"]:
        missing.append("pywin32 가 없습니다 (32비트 환경에 설치).")
    if not rep["PyQt5"]:
        missing.append("PyQt5 가 없습니다. 로그인/조회 이벤트 루프에 "
                       "필요합니다 (32비트 환경에 설치).")
    if not rep["koa_studio"]:
        missing.append("KOA Studio 가 없습니다(선택). 출력 필드명을 눈으로 "
                       "확인할 때 씁니다. 자료실에서 별도 다운로드.")

    rep["missing"] = missing
    rep["ready"] = not [m for m in missing if "선택" not in m]
    return rep


def _ocx_registration() -> dict:
    """COM 등록 상태와 **비트니스**를 레지스트리에서 읽는다.

    `InprocServer32` 가 `WOW6432Node` 아래에만 있으면 32비트로만 등록된
    것이다. 이 저장소 실측값이 그랬다. '32비트가 필요하다'를 주장이 아니라
    근거로 말하기 위해 이 값을 낸다.
    """
    out = {"ocx_registered": False, "ocx_clsid": None,
           "ocx_inproc_path": None, "ocx_wow64_only": None}
    if platform.system() != "Windows":
        return out
    try:
        import winreg
    except ImportError:
        return out

    clsid = None
    try:
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT,
                            OCX_PROGID + r"\CLSID") as k:
            clsid = winreg.QueryValueEx(k, "")[0]
        out["ocx_registered"] = True
        out["ocx_clsid"] = clsid
    except OSError:
        return out

    def _inproc(hive, path):
        try:
            with winreg.OpenKey(hive, path) as k:
                return winreg.QueryValueEx(k, "")[0]
        except OSError:
            return None

    native = _inproc(winreg.HKEY_CLASSES_ROOT,
                     rf"CLSID\{clsid}\InprocServer32")
    wow = (_inproc(winreg.HKEY_LOCAL_MACHINE,
                   rf"SOFTWARE\Classes\WOW6432Node\CLSID\{clsid}"
                   r"\InprocServer32")
           or _inproc(winreg.HKEY_LOCAL_MACHINE,
                      rf"SOFTWARE\WOW6432Node\Classes\CLSID\{clsid}"
                      r"\InprocServer32"))
    out["ocx_inproc_path"] = native or wow
    out["ocx_wow64_only"] = bool(wow and not native)
    return out


def _importable(name: str) -> bool:
    import importlib.util
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


# ══════════════════════════ 수집 계획 ══════════════════════════
def select_targets(store, limit: int = 300, scan_days: int = 5) -> list[dict]:
    """신용잔고를 실측할 종목을 고른다. 우선순위가 곧 설계다.

    시간당 1,000건 제한 때문에 전종목은 불가능하다. 그래서 이 값이
    판정을 바꾸는 종목만 고른다.

      1순위 보유 포지션    청산 계층 6(신용 재급증)이 여기서만 작동한다.
                          틀린 프록시로 실탄을 굴리면 안 된다.
      2순위 최근 추천 종목 진입 후보다. 진입 전에 실측이 있어야 한다.
      3순위 밴드 근처 종목 band_pos 가 낮은 순. 다음 후보군이다.

    반환: [{"ticker","reason","priority"}] · 중복 없음 · limit 이내
    """
    picked: dict[str, dict] = {}

    def add(ticker, reason, pri):
        t = str(ticker).zfill(6)
        if len(t) != 6 or not t.isdigit():
            return
        if t not in picked:
            picked[t] = {"ticker": t, "reason": reason, "priority": pri}

    try:
        for p in store.list_positions("OPEN") or []:
            add(getattr(p, "ticker", None), "보유 포지션", 1)
    except Exception as exc:  # noqa: BLE001
        log.debug("포지션 조회 실패: %s", exc)

    try:
        recos = store.reco_history(days=scan_days)
        if recos is not None and not recos.empty and "ticker" in recos:
            for t in recos["ticker"]:
                add(t, "최근 추천", 2)
    except Exception as exc:  # noqa: BLE001
        log.debug("추천 이력 조회 실패: %s", exc)

    try:
        scans = store.scan_history(days=scan_days)
        if scans is not None and not scans.empty and "ticker" in scans:
            df = scans
            if "d" in df.columns:
                df = df[df["d"] == df["d"].max()]
            if "band_pos" in df.columns:
                df = df.sort_values("band_pos", na_position="last")
            elif "value_score" in df.columns:
                df = df.sort_values("value_score", ascending=False)
            for t in df["ticker"]:
                if len(picked) >= limit:
                    break
                add(t, "밴드 근처", 3)
    except Exception as exc:  # noqa: BLE001
        log.debug("스캔 이력 조회 실패: %s", exc)

    out = sorted(picked.values(), key=lambda r: r["priority"])
    return out[:max(0, int(limit))]


def build_plan(store, limit: int = 300, scan_days: int = 5,
               per_hour: int = SAFE_PER_HOUR) -> dict:
    """수집 계획 + 소요시간 + 전수조사 대비 비교.

    '전종목은 왜 안 되는가'를 말이 아니라 수치로 낸다.
    """
    targets = select_targets(store, limit=limit, scan_days=scan_days)
    n = len(targets)
    try:
        active = len(store.active_tickers())
    except Exception:  # noqa: BLE001
        active = 0

    plan = {
        "targets": n,
        "active_tickers": active,
        "requests": n,                      # OPT10013 종목당 1건
        "eta_sec": round(estimate_seconds(n, per_hour=per_hour), 1),
        "full_scan_requests": active,
        "full_scan_eta_sec": round(
            estimate_seconds(active, per_hour=per_hour), 1),
        "limits": {"per_second": LIMIT_PER_SECOND,
                   "per_minute": LIMIT_PER_MINUTE,
                   "per_hour": LIMIT_PER_HOUR},
        "safe_limits": {"per_second": SAFE_PER_SECOND,
                        "per_minute": SAFE_PER_MINUTE,
                        "per_hour": per_hour},
        "by_reason": {},
        "tickers": [t["ticker"] for t in targets],
    }
    for t in targets:
        plan["by_reason"][t["reason"]] = plan["by_reason"].get(t["reason"], 0) + 1
    plan["feasible_daily"] = plan["eta_sec"] <= 3600
    try:
        plan["coverage_now"] = store.credit_coverage()
    except Exception:  # noqa: BLE001
        plan["coverage_now"] = None
    return plan


# ══════════════════════════ 결과 쓰기 ══════════════════════════
CSV_HEADER = ("종목코드", "신용잔고율", "신용잔고주식수", "기준일", "비고")


def write_credit_csv(rows: list[dict], path: str | Path = CREDIT_CSV) -> int:
    """브릿지 결과를 기존 신용잔고 CSV 규격으로 쓴다.

    본체는 `--mode credit` 으로 이 파일을 읽는다. 그 경로는 이미
    테스트돼 있으므로 새 적재 코드를 만들지 않는다.

    `credit_manual.csv` 와 **다른 파일**에 쓴다. 사람이 넣은 값을
    덮어쓰지 않기 위해서다. 적재 순서가 자동 → 수동이므로 사람의 값이
    항상 이긴다.

    rows: [{"ticker","ratio","shares","asof","note"}]
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with p.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(CSV_HEADER)
        for r in rows:
            code = str(r.get("ticker") or "").zfill(6)
            if len(code) != 6 or not code.isdigit():
                continue
            ratio = r.get("ratio")
            shares = r.get("shares")
            if ratio is None and shares is None:
                continue
            w.writerow([
                code,
                "" if ratio is None else f"{float(ratio):.3f}",
                "" if shares is None else f"{float(shares):.0f}",
                str(r.get("asof") or "")[:10],
                str(r.get("note") or "kiwoom:opt10013"),
            ])
            written += 1
    log.info("신용잔고 %d종목 기록: %s", written, p)
    return written


# ══════════════════════════ 필드 매핑 ══════════════════════════
def load_field_map(path: str | Path = FIELD_MAP_PATH) -> dict:
    """런타임에 확정한 출력 필드 매핑. 없으면 빈 dict."""
    p = Path(path)
    if not p.exists():
        return {}
    try:
        with p.open(encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError) as exc:
        log.warning("필드 매핑 읽기 실패 %s: %s", p, exc)
        return {}


def save_field_map(mapping: dict, path: str | Path = FIELD_MAP_PATH) -> None:
    """탐침으로 확정한 매핑을 고정한다. 다음 실행부터 탐침을 건너뛴다."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as fh:
        json.dump(mapping, fh, ensure_ascii=False, indent=1, sort_keys=True)
    log.info("필드 매핑 저장: %s", p)
