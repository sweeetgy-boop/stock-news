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

    def feat(label, fn):
        try:
            fn()
            print(f"  OK    {label}")
        except Exception as exc:  # noqa: BLE001
            feature_fail.append((label, f"{type(exc).__name__}: {exc}"))
            print(f" FAIL   {label}  {type(exc).__name__}: {exc}")

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
    print(f" import 실패 {len(import_fail)}   기능 실패 {len(feature_fail)}")
    print("-" * width)
    if bad == 0:
        print("\n환경이 requirements.txt 와 정확히 일치합니다.")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
