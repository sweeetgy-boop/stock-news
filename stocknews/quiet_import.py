# -*- coding: utf-8 -*-
"""import 시점에 stdout 으로 안내문을 찍는 라이브러리를 조용히 import 한다.

pykrx 1.2.8 은 `pykrx.website.comm.webio` 를 import 하는 순간
`auth.py:184` 에서 KRX_ID/KRX_PW 가 없으면 stdout 에
"KRX 로그인 실패: KRX_ID 또는 KRX_PW 환경 변수가 설정되지 않았습니다." 를
print 한다. 실패한 호출이 아니라 안내문이고(우리 시세 경로는 네이버를 쓴다),
`--json` 계약(stdout = JSON 한 줄)에 섞이고 매 평일 로그를 오염시킨다.

import 하는 동안 stdout 을 가로채 **지정한 안내문 줄만 버리고**, 나머지
줄은 로그(WARNING)로 넘긴다 — 다른 경고를 숨기지 않기 위해서다.
"""
from __future__ import annotations

import contextlib
import importlib
import io
import logging
import sys

log = logging.getLogger(__name__)

__all__ = ["quiet_import", "pykrx_stock", "DROP_LINES"]

DROP_LINES = ("KRX 로그인",)


def quiet_import(modname: str, drop: tuple[str, ...] = DROP_LINES):
    """modname 을 import 해 돌려준다. import 중 stdout 의 drop 접두 줄은 버린다."""
    if modname in sys.modules:
        return sys.modules[modname]
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        mod = importlib.import_module(modname)
    for line in buf.getvalue().splitlines():
        if not line.strip() or any(line.strip().startswith(p) for p in drop):
            continue
        log.warning("%s import 출력: %s", modname, line)
    return mod


def pykrx_stock():
    """`from pykrx import stock` 의 조용한 버전 (같은 의미: 속성 먼저, 없으면 하위 모듈)."""
    pk = quiet_import("pykrx")
    st = getattr(pk, "stock", None)
    return st if st is not None else quiet_import("pykrx.stock")
