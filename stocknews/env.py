# -*- coding: utf-8 -*-
"""`.env` 로드.

왜 별도 모듈인가
==============
`.env` 를 읽는 코드가 저장소에 없었다. `python-dotenv` 는 핀으로 박혀
있고 `verify_env.py` 가 import 까지 확인하는데, 정작 `load_dotenv()` 를
부르는 곳이 없었다. 래퍼(`run.cmd`/`run.ps1`)도 `.env` 를 읽지 않는다.

그래서 `.env` 를 만든 사람에게 "`.env` 를 만드십시오"라는 에러가 계속
나왔다. `TelegramNotConfigured` -> exit 4 로 끝나고, 원인이 설정 누락이
아니라 로더 부재였다.

의존성을 stdlib 로 제한한 이유
---------------------------
`kiwoom_bridge.py` 는 32비트 venv(`venv32`)에서 돌고 거기에는 pywin32 와
PyQt5 만 있다. `python-dotenv` 가 없다. 그래서 dotenv 가 있으면 쓰고,
없으면 자체 파서로 내려간다. 파서 동작은 스모크 테스트가 검증한다.

OS 환경변수가 항상 이긴다
----------------------
`override=False` 가 기본이다. Hermes 나 CI 가 환경변수를 직접 주입하는
경우 `.env` 의 낡은 값이 그것을 덮어쓰면 안 된다. 디버깅이 불가능해진다.

값은 절대 로그에 남기지 않는다. 어떤 키가 채워졌는지만 보고한다.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

# 저장소 루트 = 이 패키지의 부모. cwd 에 의존하지 않는다. 래퍼가 cd 를
# 해주지만, 직접 호출이나 테스트에서는 cwd 가 다를 수 있다.
REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = REPO_ROOT / ".env"

# 이 저장소가 실제로 읽는 환경변수 전체. `.env.example` 과 이 목록이
# 어긋나면 스모크 테스트가 잡는다.
KNOWN_KEYS: tuple[str, ...] = (
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "DART_API_KEY",
    "DATA_GO_KR_KEY",
    "KRX_CREDIT_BLD",
    "KIWOOM_APP_KEY",
    "KIWOOM_APP_SECRET",
    "KIWOOM_API_BASE",
    "KIWOOM_REST_PER_SECOND",
    "KIWOOM_REST_PER_MINUTE",
    "KIWOOM_REST_PER_HOUR",
    "KIWOOM_OPENAPI_DIR",
    "TZ",
)

# 값을 절대 출력하면 안 되는 키. 부분 마스킹조차 하지 않는다.
SECRET_KEYS: frozenset[str] = frozenset({
    "TELEGRAM_BOT_TOKEN", "DART_API_KEY", "DATA_GO_KR_KEY",
    "KIWOOM_APP_KEY", "KIWOOM_APP_SECRET",
})

__all__ = ["load_env", "parse_env_text", "env_report", "mask",
           "REPO_ROOT", "ENV_PATH", "KNOWN_KEYS", "SECRET_KEYS"]


def parse_env_text(text: str) -> dict[str, str]:
    """`.env` 본문 파싱. python-dotenv 없이도 되게 stdlib 만 쓴다.

    지원 형식:
        KEY=value
        KEY="value with spaces"      따옴표 안은 그대로 (# 도 값으로 본다)
        KEY='value'
        export KEY=value             셸 습관 흡수
        # 주석 / 빈 줄               무시

    따옴표 없는 값의 인라인 주석(` #` 이후)은 잘라낸다. dotenv 와 같은
    규칙이다. 공백 없이 붙은 `#` 은 값의 일부로 본다 (토큰에 들어갈 수 있음).
    """
    out: dict[str, str] = {}
    for raw in (text or "").splitlines():
        line = raw.strip().lstrip("\ufeff")
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        if not key or not key.replace("_", "").isalnum():
            continue
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        else:
            # 공백 + # 이후는 주석. 붙어 있으면 값으로 둔다.
            cut = val.find(" #")
            if cut >= 0:
                val = val[:cut].rstrip()
        out[key] = val
    return out


def load_env(path: str | Path | None = None, override: bool = False) -> dict:
    """`.env` 를 프로세스 환경변수로 올린다.

    빈 값(`KEY=`)은 설정하지 않는다. `.env.example` 을 그대로 복사하면
    전 키가 빈 문자열인데, 그걸 환경에 올리면 `os.getenv` 가 `""` 를
    돌려주고 "설정됨"으로 오판하는 코드가 생긴다. 빈 값은 미설정이다.

    반환: {"path", "exists", "applied", "skipped_existing", "empty",
           "keys", "unknown_keys", "backend"}
    값은 담지 않는다.
    """
    p = Path(path) if path else ENV_PATH
    rep: dict = {"path": str(p), "exists": p.exists(), "applied": [],
                 "skipped_existing": [], "empty": [], "unknown_keys": [],
                 "backend": None}
    if not p.exists():
        rep["keys"] = []
        return rep

    try:
        text = p.read_text(encoding="utf-8-sig")
    except OSError as exc:
        log.warning(".env 읽기 실패 %s: %s", p, exc)
        rep["error"] = f"{type(exc).__name__}: {exc}"
        rep["keys"] = []
        return rep

    # dotenv 가 있으면 파싱만 위임한다. 환경 적용은 직접 한다. 빈 값
    # 처리와 보고 형식을 우리가 통제해야 하기 때문이다.
    parsed: dict[str, str]
    try:
        from dotenv import dotenv_values
        parsed = {k: (v or "") for k, v in dotenv_values(p).items()}
        rep["backend"] = "python-dotenv"
    except ImportError:
        parsed = parse_env_text(text)
        rep["backend"] = "stdlib"

    for key, val in parsed.items():
        if not val:
            rep["empty"].append(key)
            continue
        if key not in KNOWN_KEYS:
            rep["unknown_keys"].append(key)
        if not override and os.environ.get(key):
            # OS 환경변수가 이긴다. 주입된 값을 낡은 파일이 덮으면 안 된다.
            rep["skipped_existing"].append(key)
            continue
        os.environ[key] = val
        rep["applied"].append(key)

    rep["keys"] = sorted(set(rep["applied"]) | set(rep["skipped_existing"]))
    if rep["applied"]:
        log.debug(".env 적용 %d키: %s", len(rep["applied"]),
                  ", ".join(sorted(rep["applied"])))
    return rep


def mask(key: str, value: str | None) -> str:
    """보고용 표기. 비밀 키는 길이조차 노출하지 않는다."""
    if not value:
        return "미설정"
    return "설정됨" if key in SECRET_KEYS else value


def env_report() -> dict[str, str]:
    """현재 프로세스에서 각 키가 채워졌는지. 값은 마스킹한다."""
    return {k: mask(k, os.environ.get(k)) for k in KNOWN_KEYS}
