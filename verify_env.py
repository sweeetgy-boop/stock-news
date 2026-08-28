# -*- coding: utf-8 -*-
"""requirements.txt 핀 버전과 실제 설치본을 대조한다.

pip install 이 성공했다고 핀이 맞는 건 아니다. 다른 패키지의 의존성
해석 과정에서 조용히 다른 버전이 올라가는 경우가 있어서, 실제로 무엇이
import 되는지를 확인해야 한다.

  python verify_env.py
"""
from __future__ import annotations

import re
import sys
from importlib import metadata
from pathlib import Path

REQ = Path(__file__).with_name("requirements.txt")

# 배포명(distribution name) != import 이름인 것들
IMPORT_NAME = {
    "beautifulsoup4": "bs4",
    "finance-datareader": "FinanceDataReader",
    "python-dateutil": "dateutil",
    "python-dotenv": "dotenv",
    "pillow": "PIL",
    "typing_extensions": "typing_extensions",
    "Deprecated": "deprecated",
}

# 이 프로젝트가 직접 쓰는 패키지. 나머지는 전이 의존성이다.
DIRECT = {
    "pandas", "numpy", "requests", "beautifulsoup4", "lxml",
    "pykrx", "finance-datareader", "python-dotenv",
}


def parse_requirements(path: Path) -> list[tuple[str, str]]:
    out = []
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        m = re.match(r"^([A-Za-z0-9._\-]+)\s*==\s*(.+)$", line)
        if not m:
            print(f"  [경고] 핀 형식이 아닌 줄 무시: {raw!r}")
            continue
        out.append((m.group(1), m.group(2).strip()))
    return out


def normalize(name: str) -> str:
    """PEP 503 정규화. Deprecated / typing_extensions 표기 차이를 흡수한다."""
    return re.sub(r"[-_.]+", "-", name).lower()


def main() -> int:
    print(f"Python {sys.version.split()[0]}  ({sys.executable})")
    if not REQ.exists():
        print(f"requirements.txt 를 찾을 수 없습니다: {REQ}")
        return 1

    pins = parse_requirements(REQ)
    print(f"requirements.txt 핀 {len(pins)}개\n")

    installed = {normalize(d.metadata["Name"]): d.version
                 for d in metadata.distributions()
                 if d.metadata.get("Name")}

    ok, mismatch, missing = [], [], []
    for name, want in pins:
        got = installed.get(normalize(name))
        if got is None:
            missing.append((name, want))
        elif got != want:
            mismatch.append((name, want, got))
        else:
            ok.append((name, want))

    width = 60
    print("=" * width)
    print(" 핀 버전 대조")
    print("=" * width)
    for name, want in ok:
        tag = "*" if name in DIRECT else " "
        print(f"  OK  {tag} {name:<26} {want}")
    for name, want, got in mismatch:
        print(f" DIFF  {name:<26} 요구 {want}  설치 {got}")
    for name, want in missing:
        print(f" MISS  {name:<26} 요구 {want}  (미설치)")

    # 실제 import 가 되는지 (휠은 있는데 로드가 깨지는 경우가 있다)
    print("\n" + "=" * width)
    print(" 직접 의존성 import 검증")
    print("=" * width)
    import_fail = []
    for name in sorted(DIRECT):
        mod = IMPORT_NAME.get(name, name.replace("-", "_"))
        try:
            m = __import__(mod)
            ver = getattr(m, "__version__", "-")
            print(f"  OK    import {mod:<22} {ver}")
        except Exception as exc:  # noqa: BLE001
            import_fail.append((mod, f"{type(exc).__name__}: {exc}"))
            print(f" FAIL   import {mod:<22} {type(exc).__name__}: {exc}")

    # 프로젝트가 실제로 의존하는 런타임 기능 점검
    print("\n" + "=" * width)
    print(" 런타임 기능 점검")
    print("=" * width)
    feature_fail = []

    feature_warn = []

    def feat(label, fn, *, fatal: bool = True):
        """기능 1건 점검.

        `fatal=False` 는 '기능이 죽었지만 폴백으로 동작한다'는 뜻이다.
        하드 실패로 내면 고칠 수 없는 환경(예: OS 보안 정책)에서 rc=1 이
        영구히 붙어 정작 진짜 실패를 가린다. 반대로 조용히 통과시키면
        열화 사실을 아무도 모른다. 그래서 등급을 나눈다.
        """
        try:
            fn()
            print(f"  OK    {label}")
        except Exception as exc:  # noqa: BLE001
            detail = f"{type(exc).__name__}: {exc}"
            if fatal:
                feature_fail.append((label, detail))
                print(f" FAIL   {label}  {detail}")
            else:
                feature_warn.append((label, detail))
                print(f" 경고   {label}\n        {exc}")

    def f_bs4_xml():
        from bs4 import BeautifulSoup
        s = BeautifulSoup("<rss><item><title>t</title></item></rss>", "xml")
        assert s.find("item") is not None

    def f_sqlite_upsert():
        import sqlite3
        con = sqlite3.connect(":memory:")
        con.execute("CREATE TABLE t(k TEXT PRIMARY KEY, v INTEGER)")
        con.execute("INSERT INTO t VALUES('a',1) "
                    "ON CONFLICT(k) DO UPDATE SET v=excluded.v")
        con.execute("INSERT INTO t VALUES('a',2) "
                    "ON CONFLICT(k) DO UPDATE SET v=excluded.v")
        assert con.execute("SELECT v FROM t").fetchone()[0] == 2
        con.close()

    def f_pandas_named_agg():
        import pandas as pd
        df = pd.DataFrame({"g": ["a", "a", "b"], "x": [1, 2, 3]})
        out = df.groupby("g").agg(n=("x", "count"), last=("x", "last"))
        assert int(out.at["a", "n"]) == 2

    def f_pandas_pivot():
        import pandas as pd
        df = pd.DataFrame({"d": ["1", "1", "2"], "t": ["A", "B", "A"],
                           "c": [1.0, 2.0, 3.0]})
        m = df.pivot(index="d", columns="t", values="c")
        assert m.shape == (2, 2)

    def f_numpy_addat():
        import numpy as np
        a = np.zeros(3)
        np.add.at(a, [0, 0, 2], 1.0)
        assert a.tolist() == [2.0, 0.0, 1.0]

    def f_zoneinfo():
        from datetime import datetime, timedelta, timezone
        kst = timezone(timedelta(hours=9))
        assert datetime.now(kst).utcoffset() == timedelta(hours=9)

    def f_charset_detector():
        """requests 의 문자 인식 의존성이 실제로 로드되는가.

        핀 대조는 `importlib.metadata` 로 버전만 읽는다. 그래서 설치는
        돼 있는데 **로드가 막힌** 경우를 놓친다. 2026-08-28 실측:

            ImportError: DLL load failed while importing cd:
            애플리케이션 제어 정책에서 이 파일을 차단했습니다.

        Windows 애플리케이션 제어 정책(WDAC/Smart App Control)이
        `charset_normalizer` 의 컴파일 확장(`cd.cp312-win_amd64.pyd`)을
        차단한 것이다. 패키지 파일은 전부 있고 순수 파이썬 폴백(`cd.py`)도
        있지만 `__init__` 이 `.pyd` 를 먼저 잡아 ImportError 로 죽는다.

        영향은 좁다. charset 헤더를 주는 응답(DART·키움 JSON)은 무관하고,
        헤더가 없으면 requests 가 utf-8 로 폴백한다. 우리가 읽는 소스는
        전부 UTF-8 이라 실害가 없다. 다만 '환경이 정확히 일치합니다'가
        거짓이 되는 것을 막아야 한다.
        """
        import requests.compat as _rc
        if getattr(_rc, "chardet", None) is not None:
            return
        try:
            import charset_normalizer  # noqa: F401
            return
        except ImportError as exc:
            raise AssertionError(
                f"charset 인식 의존성 로드 실패 ({exc}). "
                "응답 인코딩 자동 판별이 꺼집니다 (utf-8 폴백). "
                "복구하려면: pip install --force-reinstall "
                "--no-binary :all: charset-normalizer") from exc

    def f_stdlib_zip_xml():
        import io
        import zipfile
        from xml.etree import ElementTree
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("a.xml", "<root><list><stock_code>005930"
                                 "</stock_code></list></root>")
        buf.seek(0)
        with zipfile.ZipFile(buf) as zf:
            xml = zf.read("a.xml")
        root = ElementTree.fromstring(xml)
        assert root.findtext(".//stock_code") == "005930"

    feat("bs4 + lxml-xml 파서 (뉴스 RSS)", f_bs4_xml)
    feat("SQLite UPSERT (store)", f_sqlite_upsert)
    feat("pandas named aggregation (weekly)", f_pandas_named_agg)
    feat("pandas pivot (price_matrix)", f_pandas_pivot)
    feat("numpy add.at (매물대 POC)", f_numpy_addat)
    feat("timezone KST", f_zoneinfo)
    feat("zipfile + ElementTree (DART corpCode)", f_stdlib_zip_xml)
    # 폴백(utf-8)이 있어 배치가 죽지는 않는다. 그래서 경고로만 낸다.
    feat("requests charset 인식 (전이 의존성 로드)", f_charset_detector,
         fatal=False)

    # ── 설정 점검 ──
    # 핀 버전이 맞아도 .env 가 없으면 발송 모드가 전부 exit 4 로 끝난다.
    # 그게 '환경 검증' 에서 안 보이면 원인 추적이 오래 걸린다.
    print("\n" + "=" * width)
    print(" 설정 (.env)")
    print("=" * width)
    config_missing = []
    try:
        from stocknews.env import ENV_PATH, KNOWN_KEYS, load_env, mask
        rep = load_env()
        if rep["exists"]:
            print(f"  OK    {ENV_PATH}  (파서: {rep['backend']})")
        else:
            print(f" MISS   {ENV_PATH} 없음")
            print("        copy .env.example .env  후 값을 채우십시오")
        if rep.get("unknown_keys"):
            print(f"  경고  알 수 없는 키: {', '.join(rep['unknown_keys'])}")
        import os as _os
        for k in KNOWN_KEYS:
            v = _os.environ.get(k)
            src = " (.env)" if k in rep.get("applied", []) else (
                " (OS 환경변수)" if v else "")
            status = mask(k, v)
            tag = "  OK  " if v else " ---- "
            print(f" {tag}  {k:<22} {status}{src}")
        for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
            if not _os.environ.get(k):
                config_missing.append(k)
        if config_missing:
            print(f"\n  {', '.join(config_missing)} 미설정 — 발송 모드는 "
                  "exit 4 로 끝납니다.")
            print("  --dry-run 을 쓰면 토큰 없이도 콘솔로 확인할 수 있습니다.")
    except Exception as exc:  # noqa: BLE001
        print(f" FAIL   .env 점검 불가  {type(exc).__name__}: {exc}")
        feature_fail.append((".env 점검", f"{type(exc).__name__}: {exc}"))

    # 선언 누락된 전이 의존성 경고
    print("\n" + "=" * width)
    print(" 미선언 전이 의존성")
    print("=" * width)
    pinned = {normalize(n) for n, _ in pins}
    undeclared = []
    for name in DIRECT:
        try:
            reqs = metadata.requires(name) or []
        except metadata.PackageNotFoundError:
            continue
        for r in reqs:
            dep = re.split(r"[<>=!;\[ ]", r.strip())[0]
            if not dep:
                continue
            nd = normalize(dep)
            if nd not in pinned and nd in installed:
                undeclared.append((dep, installed[nd], name))
    if undeclared:
        for dep, ver, parent in sorted(set(undeclared)):
            print(f"  경고  {dep}=={ver}  ({parent} 의존, 핀 없음)")
        print("\n  재현성을 위해 위 항목도 requirements.txt 에 고정하는 것이")
        print("  안전합니다. 지금은 pip 이 임의 버전을 가져옵니다.")
    else:
        print("  없음")

    print("\n" + "-" * width)
    bad = len(mismatch) + len(missing) + len(import_fail) + len(feature_fail)
    print(f" 핀 일치 {len(ok)}/{len(pins)}   불일치 {len(mismatch)}   "
          f"미설치 {len(missing)}")
    print(f" import 실패 {len(import_fail)}   기능 실패 {len(feature_fail)}"
          f"   기능 경고 {len(feature_warn)}")
    print("-" * width)
    if bad == 0 and not feature_warn:
        print("\n환경이 requirements.txt 와 정확히 일치합니다.")
    elif bad == 0:
        # '일치합니다' 를 그대로 찍으면 열화 사실이 묻힌다.
        print("\n핀 버전은 일치하지만 열화된 기능이 있습니다:")
        for label, detail in feature_warn:
            print(f"  · {label}")
            print(f"    {detail}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
