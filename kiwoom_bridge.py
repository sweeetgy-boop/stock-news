# -*- coding: utf-8 -*-
"""키움 OpenAPI+ 브릿지 — **32비트 파이썬 전용**.

왜 별도 프로세스인가
==================
키움 OpenAPI+ 는 32비트 OCX(COM)입니다. 이 저장소 본체는 64비트
파이썬으로 돌아가고, 64비트 프로세스는 32비트 OCX 를 인프로세스로
로드할 수 없습니다. 그래서 OCX 호출만 이 파일이 담당하고, 결과는
CSV 로 떨어뜨립니다. 본체는 이미 검증된 경로로 그 CSV 를 읽습니다.

    [32비트] kiwoom_bridge.py  --> data/credit_kiwoom.csv
    [64비트] run_screen.py --mode credit   (기존 경로, 테스트됨)

`data/credit_manual.csv` 와 **다른 파일**에 씁니다. 사람이 넣은 값을
덮어쓰지 않기 위해서입니다. 적재 순서가 자동 -> 수동이라 사람 값이 이깁니다.

준비 (한 번만)
------------
    py -3.12-32 -m venv venv32          32비트 파이썬 필요
    venv32\\Scripts\\pip install pywin32 PyQt5

    venv32\\Scripts\\python kiwoom_bridge.py --check     환경 확인
    venv32\\Scripts\\python kiwoom_bridge.py --discover  출력 필드 확정
    venv32\\Scripts\\python kiwoom_bridge.py --plan-file data/kiwoom_targets.txt

출력 필드명은 확정하지 못했습니다
-------------------------------
`C:\\OpenAPI\\data\\opt10013.enc` 로 암호화돼 있어 오프라인으로 읽을 수
없습니다. 그래서 하드코딩하지 않고 `--discover` 로 후보를 실제 응답에
대보고, 맞는 것만 `data/kiwoom_fields.json` 에 고정합니다. 다음 실행부터는
탐침을 건너뜁니다. KRX 컬럼을 다룰 때와 같은 원칙입니다.

주의
----
- 로그인은 **실서버**로 하십시오. 모의투자만 3개월 접속하면 서비스가
  자동 해지됩니다 (키움 정책).
- 이 브릿지는 **조회만** 합니다. 주문 함수(SendOrder/SendOrderCredit)를
  호출하지 않습니다.
- 키움 OpenAPI 사용 시 관련 규정에 따라 계좌가 한국거래소에 알고리즘
  계좌로 등록될 수 있습니다. 조회만 해도 해당됩니다.
"""
from __future__ import annotations

import argparse
import logging
import struct
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from stocknews.kiwoom import (CREDIT_CSV, CREDIT_FIELD_CANDIDATES,  # noqa: E402
                             FIELD_MAP_PATH, INQUIRY_LOAN, RateLimiter,
                             SCREEN_CREDIT_TREND, TR_CREDIT_TREND,
                             check_environment, estimate_seconds,
                             load_field_map, save_field_map,
                             write_credit_csv)

log = logging.getLogger("kiwoom_bridge")
KST = timezone(timedelta(hours=9))

EXIT_OK, EXIT_FAIL, EXIT_PRECOND, EXIT_USAGE = 0, 1, 4, 64


# ══════════════════════════ OCX 래퍼 ══════════════════════════
class Kiwoom:
    """OCX 최소 래퍼. 조회만 한다.

    OpenAPI 는 요청/응답이 비동기 이벤트다. PyQt 이벤트 루프를 돌려
    응답이 올 때까지 기다린다. 동기 함수처럼 쓰기 위한 표준 패턴이다.
    """

    def __init__(self, timeout_ms: int = 20000):
        from PyQt5.QAxContainer import QAxWidget
        from PyQt5.QtCore import QEventLoop, QTimer
        from PyQt5.QtWidgets import QApplication

        self._QEventLoop = QEventLoop
        self._QTimer = QTimer
        self.timeout_ms = timeout_ms
        self.app = QApplication.instance() or QApplication(sys.argv)
        self.ocx = QAxWidget("KHOPENAPI.KHOpenAPICtrl.1")
        self.ocx.OnEventConnect.connect(self._on_connect)
        self.ocx.OnReceiveTrData.connect(self._on_tr)
        self._loop = None
        self._connect_err = None
        self._tr = None

    # ── 이벤트 ──
    def _spin(self):
        loop = self._QEventLoop()
        self._loop = loop
        t = self._QTimer()
        t.setSingleShot(True)
        t.timeout.connect(loop.quit)
        t.start(self.timeout_ms)
        loop.exec_()
        t.stop()
        self._loop = None

    def _wake(self):
        if self._loop is not None:
            self._loop.quit()

    def _on_connect(self, err_code):
        self._connect_err = int(err_code)
        self._wake()

    def _on_tr(self, screen, rqname, trcode, recordname, prev, *_):
        self._tr = {"screen": screen, "rqname": rqname, "trcode": trcode,
                    "recordname": recordname, "prev": prev}
        self._wake()

    # ── 접속 ──
    def connect(self) -> int:
        if int(self.ocx.dynamicCall("GetConnectState()")) == 1:
            return 0
        self.ocx.dynamicCall("CommConnect()")
        self._spin()
        if self._connect_err is None:
            raise TimeoutError("로그인 응답이 오지 않았습니다 "
                               "(로그인 창을 확인하십시오)")
        return self._connect_err

    def connected(self) -> bool:
        return int(self.ocx.dynamicCall("GetConnectState()")) == 1

    # ── 조회 ──
    def request(self, trcode: str, rqname: str, inputs: dict,
                screen: str) -> dict | None:
        for k, v in inputs.items():
            self.ocx.dynamicCall("SetInputValue(QString, QString)", k, str(v))
        rc = int(self.ocx.dynamicCall(
            "CommRqData(QString, QString, int, QString)",
            rqname, trcode, 0, screen))
        if rc != 0:
            log.warning("CommRqData 실패 rc=%s (%s)", rc, _rq_err(rc))
            return {"error": rc, "note": _rq_err(rc)}
        self._tr = None
        self._spin()
        return self._tr

    def count(self, trcode: str, recordname: str) -> int:
        return int(self.ocx.dynamicCall(
            "GetRepeatCnt(QString, QString)", trcode, recordname))

    def value(self, trcode: str, recordname: str, index: int,
              field: str) -> str:
        return str(self.ocx.dynamicCall(
            "GetCommData(QString, QString, int, QString)",
            trcode, recordname, index, field)).strip()


def _rq_err(rc: int) -> str:
    return {
        0: "정상",
        -200: "시세과부하 — 조회 제한 초과. 프로그램을 재실행해야 합니다.",
        -201: "조회전문작성 오류",
        -202: "전문작성 초기화 오류",
    }.get(rc, "알 수 없는 오류")


# ══════════════════════════ 필드 탐침 ══════════════════════════
def discover_fields(kw: Kiwoom, ticker: str, ymd: str,
                    limiter: RateLimiter) -> dict:
    """출력 필드명을 실제 응답으로 확정한다.

    후보를 하나씩 GetCommData 로 물어보고, 값이 돌아오는 이름만 채택한다.
    추측을 사실로 가정하지 않기 위한 절차다. 요청은 1건만 쓴다.
    """
    limiter.acquire()
    res = kw.request(TR_CREDIT_TREND, "필드탐침",
                     {"종목코드": ticker, "일자": ymd,
                      "조회구분": INQUIRY_LOAN}, SCREEN_CREDIT_TREND)
    if not res or res.get("error"):
        raise RuntimeError(f"탐침 조회 실패: {res}")

    rec = res["recordname"] or ""
    n = kw.count(TR_CREDIT_TREND, rec)
    log.info("탐침 응답: recordname=%r rows=%d", rec, n)
    if n <= 0:
        raise RuntimeError("응답 행이 0건입니다. 종목코드/일자를 확인하십시오.")

    mapping: dict[str, str] = {"_recordname": rec}
    found, missed = {}, {}
    for key, cands in CREDIT_FIELD_CANDIDATES.items():
        for name in cands:
            v = kw.value(TR_CREDIT_TREND, rec, 0, name)
            if v != "":
                mapping[key] = name
                found[key] = (name, v)
                break
        else:
            missed[key] = cands

    for k, (name, v) in found.items():
        log.info("  %-13s <- %-12s  예: %s", k, name, v[:20])
    for k, c in missed.items():
        log.warning("  %-13s 후보 전부 빈 값: %s", k, ", ".join(c))

    if "loan_balance" not in mapping and "loan_ratio" not in mapping:
        raise RuntimeError(
            "잔고/잔고비율 필드를 찾지 못했습니다. KOA Studio 로 "
            "OPT10013 의 출력 항목명을 확인해 "
            f"{FIELD_MAP_PATH} 에 직접 넣으십시오.")
    return mapping


# ══════════════════════════ 수집 ══════════════════════════
def _num(s):
    t = str(s).replace(",", "").replace("%", "").replace("+", "").strip()
    if not t or t in ("-", "--"):
        return None
    try:
        return abs(float(t))
    except ValueError:
        return None


def collect(kw: Kiwoom, tickers: list[str], ymd: str, fields: dict,
            limiter: RateLimiter) -> list[dict]:
    """종목별 신용잔고를 모은다. 최신 1행만 쓴다."""
    rec = fields.get("_recordname", "")
    f_bal = fields.get("loan_balance")
    f_rto = fields.get("loan_ratio")
    f_date = fields.get("date")

    rows, failed = [], 0
    total = len(tickers)
    for i, t in enumerate(tickers, 1):
        waited = limiter.acquire()
        res = kw.request(TR_CREDIT_TREND, f"신용{t}",
                         {"종목코드": t, "일자": ymd,
                          "조회구분": INQUIRY_LOAN}, SCREEN_CREDIT_TREND)
        if not res or res.get("error"):
            if res and res.get("error") == -200:
                log.error("조회 제한 초과. 여기서 중단합니다 (%d/%d 수집).",
                          len(rows), total)
                break
            failed += 1
            continue
        if kw.count(TR_CREDIT_TREND, rec) <= 0:
            failed += 1
            continue

        ratio = _num(kw.value(TR_CREDIT_TREND, rec, 0, f_rto)) if f_rto else None
        bal = _num(kw.value(TR_CREDIT_TREND, rec, 0, f_bal)) if f_bal else None
        asof = kw.value(TR_CREDIT_TREND, rec, 0, f_date) if f_date else ""
        asof = _iso(asof) or f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:8]}"
        if ratio is None and bal is None:
            failed += 1
            continue
        rows.append({"ticker": t, "ratio": ratio, "shares": bal,
                     "asof": asof, "note": "kiwoom:opt10013"})

        if i % 25 == 0 or i == total:
            u = limiter.used()
            log.info("  %d/%d 수집 %d 실패 %d · 시간당 %d/%d (대기 %.1fs)",
                     i, total, len(rows), failed,
                     u["hour"]["used"], u["hour"]["cap"], waited)
    return rows


def _iso(s: str) -> str | None:
    d = "".join(ch for ch in str(s) if ch.isdigit())
    if len(d) == 8:
        return f"{d[:4]}-{d[4:6]}-{d[6:8]}"
    return None


# ══════════════════════════ CLI ══════════════════════════
def read_targets(path: str) -> list[str]:
    p = Path(path)
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        t = line.strip().split(",")[0].strip()
        if t.isdigit() and len(t.zfill(6)) == 6:
            out.append(t.zfill(6))
    return list(dict.fromkeys(out))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="키움 OpenAPI+ 신용잔고 브릿지 (32비트 전용, 조회만)")
    ap.add_argument("--check", action="store_true", help="환경만 확인")
    ap.add_argument("--discover", action="store_true",
                    help="출력 필드명 탐침 후 고정")
    ap.add_argument("--plan-file", help="수집 대상 종목코드 파일")
    ap.add_argument("--ticker", action="append", default=[],
                    help="대상 종목 직접 지정 (반복 가능)")
    ap.add_argument("--date", help="기준일 YYYYMMDD (기본: 오늘 KST)")
    ap.add_argument("--out", default=CREDIT_CSV, help=f"출력 CSV ({CREDIT_CSV})")
    ap.add_argument("--per-hour", type=int, default=None,
                    help="시간당 상한 직접 지정 (기본 900, 공식 1000)")
    ap.add_argument("--probe-ticker", default="005930",
                    help="--discover 에 쓸 종목 (기본 삼성전자)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S")

    env = check_environment()
    print(f"\n키움 OpenAPI+ 환경  (파이썬 {env['python_bits']}비트)")
    print(f"  OCX          {env['ocx_inproc_path'] or '없음'}")
    print(f"  등록          {env['ocx_registered']}"
          f"{' (32비트 전용)' if env.get('ocx_wow64_only') else ''}")
    print(f"  pywin32      {env['win32com']}    PyQt5 {env['PyQt5']}")
    for m in env["missing"]:
        print(f"  ! {m}")
    print()

    if args.check:
        return EXIT_OK if env["ready"] else EXIT_PRECOND

    if struct.calcsize("P") * 8 != 32:
        log.error("이 스크립트는 32비트 파이썬으로 실행해야 합니다. "
                  "현재 %d비트입니다.", struct.calcsize("P") * 8)
        return EXIT_PRECOND
    if not env["ready"]:
        log.error("환경이 준비되지 않았습니다. 위 항목을 해결하십시오.")
        return EXIT_PRECOND

    ymd = args.date or datetime.now(KST).strftime("%Y%m%d")
    per_hour = args.per_hour or 900
    limiter = RateLimiter(per_hour=per_hour)

    try:
        kw = Kiwoom()
    except Exception as exc:  # noqa: BLE001
        log.error("OCX 생성 실패: %s: %s", type(exc).__name__, exc)
        return EXIT_FAIL

    err = kw.connect()
    if err != 0 or not kw.connected():
        log.error("로그인 실패 (code=%s)", err)
        return EXIT_FAIL
    log.info("로그인 성공")

    if args.discover:
        try:
            mapping = discover_fields(kw, args.probe_ticker, ymd, limiter)
        except Exception as exc:  # noqa: BLE001
            log.error("필드 탐침 실패: %s", exc)
            return EXIT_FAIL
        save_field_map(mapping)
        print(f"\n필드 매핑 확정 -> {FIELD_MAP_PATH}")
        for k, v in sorted(mapping.items()):
            print(f"  {k:14s} {v}")
        print()
        return EXIT_OK

    fields = load_field_map()
    if not fields:
        log.error("출력 필드 매핑이 없습니다. 먼저 --discover 를 "
                  "실행하십시오.")
        return EXIT_PRECOND

    targets = [t.zfill(6) for t in args.ticker if t.isdigit()]
    if args.plan_file:
        targets += read_targets(args.plan_file)
    targets = list(dict.fromkeys(targets))
    if not targets:
        log.error("대상 종목이 없습니다. --plan-file 또는 --ticker 를 "
                  "쓰십시오. 목록은 64비트 쪽에서 "
                  "`--mode kiwoom-plan --write-targets` 로 만듭니다.")
        return EXIT_USAGE

    eta = estimate_seconds(len(targets), per_hour=per_hour)
    log.info("대상 %d종목 · 기준일 %s · 예상 %.1f분 (시간당 상한 %d)",
             len(targets), ymd, eta / 60, per_hour)
    if len(targets) > per_hour:
        log.warning("대상이 시간당 상한(%d)을 넘습니다. %d건째부터 최대 "
                    "1시간씩 대기합니다. --plan-file 을 줄이십시오.",
                    per_hour, per_hour + 1)

    rows = collect(kw, targets, ymd, fields, limiter)
    n = write_credit_csv(rows, args.out)
    print(f"\n신용잔고 {n}종목 -> {args.out}")
    print("  본체에 반영:  hermes\\run.cmd --mode credit\n")
    return EXIT_OK if n else EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
