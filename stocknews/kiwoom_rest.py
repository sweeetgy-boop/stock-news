# -*- coding: utf-8 -*-
"""키움 REST OpenAPI — 종목별 신용잔고 실측 경로 (au10001 + ka10013).

왜 REST 로 옮겼는가
==================
OCX(OpenAPI+) 경로는 32비트 파이썬 · pywin32 · PyQt5 · **로그인 창**이
전부 필요하다. 사람이 앉아 있어야 돌아가므로 에이전트가 무인으로 쓸 수
없다. REST 는 앱키/시크릿만 있으면 64비트 본체에서 그대로 돈다.

`kiwoom.py`(OCX 계획·환경진단)는 지우지 않는다. 재사용 자산이 있다.

    select_targets / build_plan   대상 선정 (시간당 제한 때문에 전종목 불가)
    RateLimiter / estimate_seconds  슬라이딩 윈도 제한기 + 소요 예측
    write_credit_csv              credit CSV 규격 (기존 적재 파서와 호환)

규격 출처 — 추측 아님
--------------------
키움증권 공식 예제 저장소와 공개 가이드 미러가 **글자 단위로 일치**한다.

    github.com/Kiwoom-Securities/Kiwoom-REST-API
      examples/국내주식/종목정보/get_domestic_credit_trade_trend.py
      kiwoom/core/auth.py       (expires_dt 파싱 · 토큰 필드명)
      kiwoom/core/errors.py     (return_code 분류표)

    POST /oauth2/token                       (au10001)
         {grant_type:"client_credentials", appkey, secretkey}
      -> {token, token_type, expires_dt, return_code, return_msg}
         expires_dt = YYYYMMDDHHMMSS, **KST**

    POST /api/dostk/stkinfo   header api-id: ka10013
         {stk_cd, dt:YYYYMMDD, qry_tp}        qry_tp 1:융자 2:대주
      -> {crd_trde_trend:[{dt,cur_prc,pred_pre_sig,pred_pre,trde_qty,
                           new,rpya,remn,amt,pre,shr_rt,remn_rt}],
          return_code, return_msg}

OCX 쪽이 후보 탐침(`--discover`)을 하는 이유는 출력 필드명이
`C:\\OpenAPI\\data\\opt10013.enc` 로 암호화돼 있어 오프라인 확인이
불가능했기 때문이다. REST 는 그 제약이 없으므로 필드명을 고정한다.

호출 제한 수치는 공개돼 있지 않다
-------------------------------
키움 REST 가이드에 유량 제한 수치가 없다. 대신 초과를 `return_code`
1700/1701/1702 로 알려준다. 그래서 **추측한 수치를 상수로 박지 않고**
보수적 기본값으로 스스로 조절하다가, 그 코드나 HTTP 429 를 받으면
속도를 절반으로 줄이고 백오프한다. 실제 제한을 알게 되면 환경변수만
바꾸면 된다.

    KIWOOM_REST_PER_SECOND / _PER_MINUTE / _PER_HOUR

주문은 하지 않는다
-----------------
이 모듈은 조회 TR 만 부른다. 주문 계열 TR 과 주문 엔드포인트는 여기에
등장하지 않으며, 스모크 테스트가 그 문자열의 부재를 회귀 가드로 검사한다.
AGENTS.md 8장의 '신호만 낸다' 원칙 그대로다.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from stocknews.kiwoom import RateLimiter

log = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))
UTC = timezone.utc

# ── 도메인 ──
BASE_REAL = "https://api.kiwoom.com"
BASE_MOCK = "https://mockapi.kiwoom.com"      # KRX 만 지원

# ── 경로 / TR ──
TOKEN_PATH = "/oauth2/token"
REVOKE_PATH = "/oauth2/revoke"
STKINFO_PATH = "/api/dostk/stkinfo"

API_TOKEN = "au10001"          # 접근토큰 발급
API_REVOKE = "au10002"         # 접근토큰 폐기
API_CREDIT_TREND = "ka10013"   # 신용매매동향요청

CONTENT_TYPE = "application/json;charset=UTF-8"

# 조회구분. ka10013 Request Body: "1:융자, 2:대주"
INQUIRY_LOAN = "1"
INQUIRY_SHORT_LOAN = "2"

# ── ka10013 응답 필드 (공식 예제 저장소 확정) ──
CREDIT_LIST_KEY = "crd_trde_trend"
CREDIT_FIELDS: dict[str, str] = {
    "dt": "일자",
    "cur_prc": "현재가",
    "pred_pre_sig": "전일대비기호",
    "pred_pre": "전일대비",
    "trde_qty": "거래량",
    "new": "신규",
    "rpya": "상환",
    "remn": "잔고",
    "amt": "금액",
    "pre": "대비",
    "shr_rt": "공여율",
    "remn_rt": "잔고율",
}
# 우리가 실제로 쓰는 둘. 나머지는 진단·검증용이다.
FIELD_BALANCE = "remn"        # 잔고 (주식수. 단위는 런타임에 검증한다)
FIELD_BALANCE_RATIO = "remn_rt"   # 잔고율 (%) — 단위 모호성이 없다

# ── return_code 분류 (공식 errors.py) ──
INPUT_VALIDATION_CODES = frozenset({1501, 1504, 1505, 1511, 1512, 1513,
                                    1514, 1515, 1516, 1517, 1687, 8020})
RATE_LIMIT_CODES = frozenset({1700, 1701, 1702})
SYMBOL_NOT_FOUND_CODES = frozenset({1901, 1902, 1903})
INVALID_CREDENTIAL_CODES = frozenset({8001, 8002, 8011, 8012})
INVALID_TOKEN_CODES = frozenset({8003, 8005, 8006, 8009, 8015, 8016})
MODE_MISMATCH_CODES = frozenset({8030, 8031})
DEVICE_AUTH_CODES = frozenset({8010, 8040, 8050, 8103})
DEMO_UNSUPPORTED_CODES = frozenset({8104})
# 토큰이 죽은 경우. 재발급 후 1회 재시도할 값들.
AUTH_RETRY_CODES = INVALID_TOKEN_CODES | MODE_MISMATCH_CODES | frozenset({8103})

# ── 유량 제한 기본값 (공개값 아님. 보수적 추정) ──
# 근거를 밝힌다: 키움은 REST 제한 수치를 공개하지 않는다. OCX 문서의
# '초당 5건'보다 낮게 시작해 실패 신호를 보고 줄인다.
DEFAULT_PER_SECOND = 3
DEFAULT_PER_MINUTE = 60
DEFAULT_PER_HOUR = 900
LIMITS_ARE_PUBLISHED = False

DEFAULT_TIMEOUT = 15.0
DEFAULT_MAX_RETRIES = 3
DEFAULT_REFRESH_BUFFER = 300.0    # 만료 5분 전에 미리 재발급
DEFAULT_BACKOFF_BASE = 2.0
DEFAULT_BACKOFF_CAP = 60.0

TOKEN_PATH_LOCAL = "data/kiwoom_token.json"
CREDIT_CSV = "data/credit_kiwoom.csv"

# 잔고(remn) 단위 후보. 주 또는 천주.
SHARE_UNIT_CANDIDATES = (1.0, 1000.0)
SHARE_UNIT_TOLERANCE = 0.30       # 관측 배율이 후보의 ±30% 안이면 채택
SHARE_UNIT_MIN_SAMPLES = 3

__all__ = [
    "BASE_REAL", "BASE_MOCK", "TOKEN_PATH", "STKINFO_PATH",
    "API_TOKEN", "API_CREDIT_TREND", "INQUIRY_LOAN", "INQUIRY_SHORT_LOAN",
    "CREDIT_LIST_KEY", "CREDIT_FIELDS", "FIELD_BALANCE",
    "FIELD_BALANCE_RATIO", "RATE_LIMIT_CODES", "AUTH_RETRY_CODES",
    "INVALID_CREDENTIAL_CODES", "LIMITS_ARE_PUBLISHED",
    "DEFAULT_PER_SECOND", "DEFAULT_PER_MINUTE", "DEFAULT_PER_HOUR",
    "CREDIT_CSV", "TOKEN_PATH_LOCAL",
    "KiwoomRestError", "AuthError", "CredentialsMissing", "RateLimited",
    "TokenRejected", "SymbolNotFound", "TransportError",
    "HttpReply", "RestReply", "TokenRecord", "TokenStore",
    "KiwoomRestClient", "credentials_from_env", "base_url_from_env",
    "limiter_from_env", "parse_expiry", "parse_number",
    "pick_latest_credit", "resolve_share_unit", "collect_credit",
    "classify_return_code", "return_code_of", "requests_transport",
]


# ══════════════════════════ 예외 ══════════════════════════
class KiwoomRestError(RuntimeError):
    """키움 REST 호출 실패의 기반 예외."""

    def __init__(self, message: str, *, return_code: int | None = None,
                 status: int | None = None):
        self.return_code = return_code
        self.status = status
        super().__init__(message)


class CredentialsMissing(KiwoomRestError):
    """앱키/시크릿이 없다. 전제조건 미충족(exit 4)에 해당한다."""


class AuthError(KiwoomRestError):
    """토큰 발급 실패. 키가 틀렸거나 실전/모의 모드가 안 맞는다."""


class TokenRejected(KiwoomRestError):
    """발급된 토큰이 거부됐다. 재발급 후 재시도 대상."""


class RateLimited(KiwoomRestError):
    """유량 제한 초과."""


class SymbolNotFound(KiwoomRestError):
    """존재하지 않는 종목코드. 이 종목만 건너뛰면 된다."""


class TransportError(KiwoomRestError):
    """네트워크/HTTP 레벨 실패."""


def return_code_of(body: dict) -> int | None:
    """`return_code` 를 int 로. 정상(0)/없음(None)은 None 으로 접는다.

    문서 예시는 정수지만 키움은 다른 필드를 전부 문자열로 준다.
    `"0"` 이 오면 `!= 0` 이 참이 돼 정상 응답을 오류로 만든다.
    """
    raw = body.get("return_code")
    if raw is None or raw == "":
        return None
    try:
        code = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return None if code == 0 else code


def classify_return_code(code: int, msg: str, *,
                         status: int | None = None) -> KiwoomRestError:
    """`return_code` 를 대응이 갈리는 예외로 바꾼다.

    분류 기준은 공식 `kiwoom/core/errors.py` 를 그대로 따른다. 우리가
    대응을 달리하는 축은 셋이다.

      재시도해도 소용없음   자격증명·입력 오류      -> AuthError / KiwoomRestError
      재발급하면 살아남     토큰 만료·모드 불일치   -> TokenRejected
      기다리면 살아남       유량 초과               -> RateLimited
    """
    if code in RATE_LIMIT_CODES:
        return RateLimited(f"유량 제한 초과 ({code}) {msg}",
                           return_code=code, status=status)
    if code in INVALID_CREDENTIAL_CODES:
        return AuthError(f"앱키/시크릿이 거부됐습니다 ({code}) {msg}",
                         return_code=code, status=status)
    if code in AUTH_RETRY_CODES:
        return TokenRejected(f"토큰이 거부됐습니다 ({code}) {msg}",
                             return_code=code, status=status)
    if code in SYMBOL_NOT_FOUND_CODES:
        return SymbolNotFound(f"종목을 찾을 수 없습니다 ({code}) {msg}",
                              return_code=code, status=status)
    if code in DEMO_UNSUPPORTED_CODES:
        return KiwoomRestError(f"모의투자 미지원 API ({code}) {msg}",
                               return_code=code, status=status)
    if code in INPUT_VALIDATION_CODES:
        return KiwoomRestError(f"요청 값 오류 ({code}) {msg}",
                               return_code=code, status=status)
    return KiwoomRestError(f"키움 오류 ({code}) {msg}",
                           return_code=code, status=status)


# ══════════════════════════ 환경 ══════════════════════════
def credentials_from_env(env: dict | None = None) -> tuple[str, str] | None:
    """(appkey, secretkey). 하나라도 비면 None.

    `.env` 는 `run_screen.py` 가 시작할 때 이미 환경으로 올려놨다
    (`stocknews.env.load_env`). 여기서 파일을 다시 읽지 않는다.
    """
    src = os.environ if env is None else env
    key = (src.get("KIWOOM_APP_KEY") or "").strip()
    sec = (src.get("KIWOOM_APP_SECRET") or "").strip()
    if not key or not sec:
        return None
    return key, sec


def base_url_from_env(env: dict | None = None, *, mock: bool = False) -> str:
    """호출 도메인. 명시 설정이 이기고, 없으면 실전/모의 기본값."""
    src = os.environ if env is None else env
    explicit = (src.get("KIWOOM_API_BASE") or "").strip().rstrip("/")
    if explicit:
        return explicit
    return BASE_MOCK if mock else BASE_REAL


def _env_int(name: str, default: int, env: dict | None = None) -> int:
    src = os.environ if env is None else env
    raw = (src.get(name) or "").strip()
    if not raw:
        return default
    try:
        v = int(raw)
    except ValueError:
        log.warning("%s=%r 를 정수로 읽을 수 없습니다. 기본값 %d 사용",
                    name, raw, default)
        return default
    return v if v >= 1 else default


def limiter_from_env(env: dict | None = None, clock=time.monotonic) -> RateLimiter:
    """유량 제한기. 수치는 공개값이 아니므로 환경변수로 덮을 수 있게 둔다."""
    return RateLimiter(
        per_second=_env_int("KIWOOM_REST_PER_SECOND", DEFAULT_PER_SECOND, env),
        per_minute=_env_int("KIWOOM_REST_PER_MINUTE", DEFAULT_PER_MINUTE, env),
        per_hour=_env_int("KIWOOM_REST_PER_HOUR", DEFAULT_PER_HOUR, env),
        clock=clock)


# ══════════════════════════ 값 파싱 ══════════════════════════
def parse_number(value) -> float | None:
    """키움 문자열 숫자 -> float. 못 읽으면 None.

    키움은 모든 수치를 문자열로 준다. 부호가 방향 표시로 앞에 붙고
    (`"+65100"`, `"-65000"`), 없는 값은 빈 문자열이다. `0` 과 `""` 는
    의미가 다르므로 빈 문자열을 0 으로 만들면 안 된다.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "").replace(" ", "")
    if not text or text in {"+", "-", ".", "--"}:
        return None
    if text.startswith("+"):
        text = text[1:]
    try:
        return float(text)
    except ValueError:
        return None


def parse_expiry(value: str) -> datetime:
    """`expires_dt` (YYYYMMDDHHMMSS, KST) -> UTC aware datetime.

    KST 라는 사실이 중요하다. naive 로 두면 서버가 UTC 일 때 토큰을
    9시간 일찍 죽은 것으로 보고 매번 재발급한다.
    """
    text = str(value).strip()
    parsed = datetime.strptime(text, "%Y%m%d%H%M%S")
    return parsed.replace(tzinfo=KST).astimezone(UTC)


def _fingerprint(app_key: str, app_secret: str) -> str:
    """자격증명 지문. 키를 저장하지 않고 '같은 키인가'만 판정한다."""
    raw = f"{app_key}\x00{app_secret}".encode()
    return hashlib.sha256(raw).hexdigest()[:16]


# ══════════════════════════ 토큰 ══════════════════════════
@dataclass(frozen=True)
class TokenRecord:
    token: str
    token_type: str
    expires_at: datetime          # UTC aware
    base_url: str
    fingerprint: str

    def header_value(self) -> str:
        """`authorization` 헤더 값. 가이드가 "Bearer" 를 요구한다."""
        return f"Bearer {self.token}"

    def valid_at(self, now: datetime, buffer_sec: float = 0.0) -> bool:
        return now < (self.expires_at - timedelta(seconds=buffer_sec))

    def to_json(self) -> dict:
        return {"token": self.token, "token_type": self.token_type,
                "expires_at": self.expires_at.astimezone(UTC).isoformat(),
                "base_url": self.base_url, "fingerprint": self.fingerprint}

    @classmethod
    def from_json(cls, data: dict) -> "TokenRecord":
        return cls(token=str(data["token"]),
                   token_type=str(data.get("token_type") or "bearer"),
                   expires_at=datetime.fromisoformat(
                       str(data["expires_at"])).astimezone(UTC),
                   base_url=str(data.get("base_url") or ""),
                   fingerprint=str(data.get("fingerprint") or ""))


class TokenStore:
    """토큰 파일 캐시.

    왜 파일에 두는가: 배치가 하루에 여러 번 돌고, 매번 재발급하면
    발급 요청이 유량을 먹는다. 만료 시각이 응답에 오므로 그때까지 재사용한다.

    **앱키와 시크릿은 저장하지 않는다.** 지문(sha256 앞 16자)만 남겨
    '다른 키로 바뀌었는지'를 판정한다. 저장하면 `.env` 밖으로 비밀이
    새는 두 번째 경로가 생긴다(AGENTS.md 6장).
    """

    def __init__(self, path: str | Path = TOKEN_PATH_LOCAL):
        self.path = Path(path)

    def load(self) -> TokenRecord | None:
        if not self.path.exists():
            return None
        try:
            with self.path.open(encoding="utf-8") as fh:
                data = json.load(fh)
            return TokenRecord.from_json(data)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            log.debug("토큰 캐시 무시 (%s): %s", self.path, exc)
            return None

    def save(self, record: TokenRecord) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(record.to_json(), fh, ensure_ascii=False, indent=1)
        tmp.replace(self.path)
        # 토큰도 비밀이다. 되는 플랫폼에서만 권한을 좁힌다.
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    def clear(self) -> None:
        try:
            self.path.unlink()
        except OSError:
            pass


# ══════════════════════════ 전송 ══════════════════════════
@dataclass(frozen=True)
class HttpReply:
    status: int
    body: dict
    headers: dict = field(default_factory=dict)
    text: str = ""

    def header(self, name: str) -> str | None:
        """헤더 대소문자 무관 조회. HTTP 헤더는 대소문자를 구분하지 않는다."""
        low = name.lower()
        for k, v in self.headers.items():
            if str(k).lower() == low:
                return v
        return None


Transport = Callable[[str, str, dict, dict, float], HttpReply]


def requests_transport(method: str, url: str, headers: dict, payload: dict,
                       timeout: float) -> HttpReply:
    """기본 전송. `requests` 를 지연 임포트한다.

    지연 임포트 이유: 이 모듈을 `requests` 없는 환경(32비트 브릿지용
    venv 등)에서 임포트만 해도 터지는 일을 막는다.
    """
    try:
        import requests
    except ImportError as exc:      # pragma: no cover
        raise TransportError(f"requests 가 없습니다: {exc}") from exc

    try:
        resp = requests.request(method, url, headers=headers, json=payload,
                                timeout=timeout)
    except Exception as exc:        # noqa: BLE001  requests 예외 계층 전체
        raise TransportError(f"{method} {url} 실패: {exc}") from exc

    text = resp.text or ""
    try:
        body = resp.json()
    except ValueError:
        body = {}
    if not isinstance(body, dict):
        body = {"_raw": body}
    return HttpReply(status=int(resp.status_code), body=body,
                     headers=dict(resp.headers), text=text)


@dataclass(frozen=True)
class RestReply:
    body: dict
    cont_yn: str | None
    next_key: str | None
    status: int

    @property
    def has_next(self) -> bool:
        return str(self.cont_yn or "").upper() == "Y"


# ══════════════════════════ 클라이언트 ══════════════════════════
class KiwoomRestClient:
    """ka10013 하나를 위해 필요한 최소 클라이언트.

    설계 판단 셋.

      1. `transport` 를 주입할 수 있게 했다. 네트워크 없이 스모크
         테스트로 재시도·백오프·토큰 갱신 경로를 전부 검증한다.
      2. `now` 도 주입한다. 토큰 만료 로직은 시간이 지나야 검증되는데
         테스트가 15분을 기다릴 수는 없다.
      3. 유량 초과를 만나면 제한기를 **영구적으로 절반으로** 줄인다.
         한 번 걸린 속도로 계속 밀면 남은 종목 전부가 실패한다.
    """

    def __init__(self, app_key: str, app_secret: str, *,
                 base_url: str | None = None, mock: bool = False,
                 limiter: RateLimiter | None = None,
                 token_store: TokenStore | None = None,
                 transport: Transport | None = None,
                 timeout: float = DEFAULT_TIMEOUT,
                 max_retries: int = DEFAULT_MAX_RETRIES,
                 refresh_buffer: float = DEFAULT_REFRESH_BUFFER,
                 backoff_base: float = DEFAULT_BACKOFF_BASE,
                 backoff_cap: float = DEFAULT_BACKOFF_CAP,
                 sleep: Callable[[float], None] = time.sleep,
                 now: Callable[[], datetime] | None = None):
        if not app_key or not app_secret:
            raise CredentialsMissing(
                "KIWOOM_APP_KEY / KIWOOM_APP_SECRET 가 필요합니다.")
        self.app_key = app_key
        self.app_secret = app_secret
        self.base_url = (base_url or base_url_from_env(mock=mock)).rstrip("/")
        self.limiter = limiter if limiter is not None else limiter_from_env()
        self.token_store = (token_store if token_store is not None
                            else TokenStore())
        self.transport = transport or requests_transport
        self.timeout = float(timeout)
        self.max_retries = max(1, int(max_retries))
        self.refresh_buffer = float(refresh_buffer)
        self.backoff_base = float(backoff_base)
        self.backoff_cap = float(backoff_cap)
        self._sleep = sleep
        self._now = now or (lambda: datetime.now(UTC))
        # 제한기를 교체할 때 같은 시계를 물려줘야 한다. 테스트가 가상
        # 시계를 주입해 놓고 교체 후 실제 시간으로 돌아가면 안 된다.
        self._clock = getattr(self.limiter, "_clock", time.monotonic)
        self._fp = _fingerprint(app_key, app_secret)
        self._record: TokenRecord | None = None
        self.stats: dict[str, int] = {"requests": 0, "token_issued": 0,
                                      "retries": 0, "rate_limited": 0,
                                      "throttled_down": 0}

    # ── 토큰 ──
    def token(self) -> TokenRecord:
        """유효한 토큰. 캐시 -> 파일 -> 발급 순으로 찾는다."""
        now = self._now()
        if self._record and self._usable(self._record, now):
            return self._record
        cached = self.token_store.load()
        if cached and self._usable(cached, now):
            self._record = cached
            log.debug("토큰 캐시 재사용 (만료 %s)", cached.expires_at)
            return cached
        return self._issue_token()

    def _usable(self, rec: TokenRecord, now: datetime) -> bool:
        if rec.fingerprint != self._fp:
            return False
        if rec.base_url and rec.base_url.rstrip("/") != self.base_url:
            return False
        return rec.valid_at(now, self.refresh_buffer)

    def _issue_token(self) -> TokenRecord:
        """au10001. 토큰 발급도 유량을 먹으므로 제한기를 통과시킨다."""
        self.limiter.acquire(self._sleep)
        reply = self.transport(
            "POST", self.base_url + TOKEN_PATH,
            {"Content-Type": CONTENT_TYPE},
            {"grant_type": "client_credentials", "appkey": self.app_key,
             "secretkey": self.app_secret},
            self.timeout)
        self.stats["requests"] += 1

        code = return_code_of(reply.body)
        if reply.status != 200 or code is not None:
            msg = str(reply.body.get("return_msg") or reply.text)[:200]
            if code is not None:
                raise classify_return_code(code, msg, status=reply.status)
            raise AuthError(f"토큰 발급 실패 (HTTP {reply.status}) {msg}",
                            status=reply.status)

        token = reply.body.get("token")
        expires_dt = reply.body.get("expires_dt")
        if not token or not expires_dt:
            raise AuthError("토큰 응답에 token/expires_dt 가 없습니다.",
                            status=reply.status)
        try:
            expires_at = parse_expiry(expires_dt)
        except (TypeError, ValueError) as exc:
            raise AuthError(
                f"expires_dt 형식을 읽을 수 없습니다: {expires_dt!r}") from exc

        rec = TokenRecord(token=str(token),
                          token_type=str(reply.body.get("token_type")
                                         or "bearer").lower(),
                          expires_at=expires_at, base_url=self.base_url,
                          fingerprint=self._fp)
        self._record = rec
        self.token_store.save(rec)
        self.stats["token_issued"] += 1
        # 토큰 값은 절대 로그에 남기지 않는다.
        log.info("접근토큰 발급 (만료 %s KST)",
                 expires_at.astimezone(KST).strftime("%Y-%m-%d %H:%M:%S"))
        return rec

    # ── 유량 ──
    def _throttle_down(self) -> None:
        """제한에 걸렸다. 속도를 절반으로 줄이고 계속 간다."""
        caps = [max(1, cap // 2) for _, cap in self.limiter.windows]
        self.limiter = RateLimiter(per_second=caps[0], per_minute=caps[1],
                                   per_hour=caps[2], clock=self._clock)
        self.stats["throttled_down"] += 1
        log.warning("유량 제한 감지 — 속도를 초당 %d / 분당 %d / 시간당 %d "
                    "로 낮춥니다", *caps)

    def _backoff(self, attempt: int) -> float:
        return min(self.backoff_cap, self.backoff_base ** max(0, attempt))

    # ── 호출 ──
    def call(self, api_id: str, path: str, body: dict, *,
             cont_yn: str | None = None,
             next_key: str | None = None) -> RestReply:
        """TR 1건. 재시도·백오프·토큰 갱신을 여기서 흡수한다."""
        url = self.base_url + path
        reissued = False
        last: Exception | None = None

        for attempt in range(self.max_retries):
            rec = self.token()
            headers = {
                "Content-Type": CONTENT_TYPE,
                "authorization": rec.header_value(),
                "api-id": api_id,
                "cont-yn": cont_yn or "N",
                "next-key": next_key or "",
            }
            self.limiter.acquire(self._sleep)
            try:
                reply = self.transport("POST", url, headers, body, self.timeout)
            except TransportError as exc:
                last = exc
                self.stats["retries"] += 1
                log.warning("전송 실패 (%d/%d): %s", attempt + 1,
                            self.max_retries, exc)
                self._sleep(self._backoff(attempt))
                continue
            self.stats["requests"] += 1

            # HTTP 레벨 유량/일시장애
            if reply.status == 429:
                self.stats["rate_limited"] += 1
                self._throttle_down()
                last = RateLimited("HTTP 429", status=429)
                self._sleep(self._backoff(attempt))
                continue
            if reply.status >= 500:
                last = TransportError(f"서버 오류 HTTP {reply.status}",
                                      status=reply.status)
                self.stats["retries"] += 1
                self._sleep(self._backoff(attempt))
                continue
            if reply.status in (401, 403) and not reissued:
                reissued = True
                self.token_store.clear()
                self._record = None
                last = TokenRejected(f"HTTP {reply.status}", status=reply.status)
                continue
            if reply.status != 200:
                raise TransportError(
                    f"HTTP {reply.status} {reply.text[:200]}",
                    status=reply.status)

            code = return_code_of(reply.body)
            if code is not None:
                msg = str(reply.body.get("return_msg") or "")[:200]
                err = classify_return_code(code, msg, status=reply.status)
                if isinstance(err, RateLimited):
                    self.stats["rate_limited"] += 1
                    self._throttle_down()
                    last = err
                    self._sleep(self._backoff(attempt))
                    continue
                if isinstance(err, TokenRejected) and not reissued:
                    reissued = True
                    self.token_store.clear()
                    self._record = None
                    last = err
                    log.info("토큰이 거부돼 재발급합니다 (%s)", code)
                    continue
                raise err

            return RestReply(body=reply.body,
                             cont_yn=reply.header("cont-yn"),
                             next_key=reply.header("next-key"),
                             status=reply.status)

        raise last if last else TransportError("재시도를 모두 소진했습니다.")

    # ── ka10013 ──
    def credit_trend(self, ticker: str, ymd: str, *,
                     qry_tp: str = INQUIRY_LOAN,
                     max_pages: int = 1) -> list[dict]:
        """신용매매동향 레코드. 최신순으로 내려온다.

        `max_pages` 기본 1: 응답이 최신순이라 첫 페이지에 원하는 날짜가
        들어 있다. 페이지를 더 넘기면 종목당 요청이 늘어 유량 예산을
        그만큼 더 먹는다.
        """
        code = str(ticker).strip().zfill(6)
        body = {"stk_cd": code, "dt": str(ymd).replace("-", "")[:8],
                "qry_tp": str(qry_tp)}
        rows: list[dict] = []
        cont, key = None, None
        for _ in range(max(1, int(max_pages))):
            reply = self.call(API_CREDIT_TREND, STKINFO_PATH, body,
                              cont_yn=cont, next_key=key)
            chunk = reply.body.get(CREDIT_LIST_KEY)
            if isinstance(chunk, list):
                rows.extend(r for r in chunk if isinstance(r, dict))
            if not reply.has_next:
                break
            cont, key = reply.cont_yn, reply.next_key
        return rows


# ══════════════════════════ 신용잔고 해석 ══════════════════════════
def pick_latest_credit(records: list[dict]) -> dict | None:
    """신용잔고가 실제로 들어 있는 가장 최근 레코드.

    키움은 값이 없는 날을 빈 문자열로 준다(문서 예시가 그렇다).
    `0` 과 `""` 는 다르다. 잔고 0 은 '신용이 없다'는 실측이고 빈
    문자열은 '데이터가 없다'다. 후자를 0 으로 읽으면 신용 과열 종목을
    깨끗한 종목으로 오판한다.
    """
    best, best_dt = None, ""
    for rec in records or []:
        if not isinstance(rec, dict):
            continue
        ratio = parse_number(rec.get(FIELD_BALANCE_RATIO))
        shares = parse_number(rec.get(FIELD_BALANCE))
        if ratio is None and shares is None:
            continue
        dt = str(rec.get("dt") or "").strip()
        if dt >= best_dt:      # YYYYMMDD 는 문자열 비교로도 시간순이다
            best, best_dt = rec, dt
    return best


def resolve_share_unit(observations: list[tuple[float, float, float]],
                       ) -> tuple[float | None, str]:
    """`remn`(잔고) 의 단위를 관측으로 결정한다.

    문서에 단위가 없다. 주일 수도, 천주일 수도 있다. 그런데 같은 응답에
    `remn_rt`(잔고율 %)가 함께 오고 상장주식수는 우리 마스터에 있다.
    그러면 배율이 계산된다.

        implied_shares = remn_rt / 100 * 상장주식수
        scale          = implied_shares / remn

    scale 이 1 이면 주, 1000 이면 천주다. 여러 종목의 중위값으로
    판정하고, 후보에서 벗어나면 **추측하지 않고 None** 을 낸다. 그
    경우 주식수는 비워두고 비율만 쓴다. 비율은 단위 모호성이 없다.

    observations: [(remn, remn_rt, 상장주식수)]
    반환: (배율 또는 None, 사람이 읽을 근거)
    """
    scales = []
    for remn, remn_rt, listed in observations:
        if not remn or remn <= 0 or not remn_rt or remn_rt <= 0:
            continue
        if not listed or listed <= 0:
            continue
        scales.append((remn_rt / 100.0 * listed) / remn)
    if len(scales) < SHARE_UNIT_MIN_SAMPLES:
        return None, (f"표본 {len(scales)}건 (최소 {SHARE_UNIT_MIN_SAMPLES}건) "
                      "— 단위 미확정, 잔고율만 사용")
    scales.sort()
    median = scales[len(scales) // 2]
    for cand in SHARE_UNIT_CANDIDATES:
        if abs(median - cand) <= cand * SHARE_UNIT_TOLERANCE:
            unit = "주" if cand == 1.0 else "천주"
            return cand, (f"표본 {len(scales)}건 · 중위배율 {median:.3f} "
                          f"-> {unit} 단위로 확정")
    return None, (f"표본 {len(scales)}건 · 중위배율 {median:.3f} 가 후보 "
                  f"{SHARE_UNIT_CANDIDATES} 와 맞지 않음 — 잔고율만 사용")


def _asof(rec: dict, fallback: str) -> str:
    dt = str(rec.get("dt") or "").strip()
    if len(dt) == 8 and dt.isdigit():
        return f"{dt[:4]}-{dt[4:6]}-{dt[6:]}"
    return str(fallback)[:10]


def collect_credit(client: KiwoomRestClient, tickers: list[str], *,
                   trade_date: str, listed_shares: dict | None = None,
                   qry_tp: str = INQUIRY_LOAN, max_pages: int = 1,
                   progress: Callable[[int, int, str], None] | None = None,
                   ) -> tuple[list[dict], dict]:
    """대상 종목의 신용잔고를 실측한다.

    2패스다. 먼저 응답을 모으고, 그 다음 `remn` 단위를 결정해서 행을
    만든다. 종목별로 즉석 판정하면 첫 종목의 우연한 값이 단위를 정해
    나머지가 전부 틀어진다.

    반환: (write_credit_csv 용 행 목록, 통계)
    """
    ymd = str(trade_date).replace("-", "")[:8]
    shares_map = listed_shares or {}
    latest: dict[str, dict] = {}
    failures: list[dict] = []
    total = len(tickers)

    for i, raw in enumerate(tickers, 1):
        code = str(raw).strip().zfill(6)
        if len(code) != 6 or not code.isdigit():
            failures.append({"ticker": str(raw), "reason": "종목코드 형식"})
            continue
        if progress:
            progress(i, total, code)
        try:
            records = client.credit_trend(code, ymd, qry_tp=qry_tp,
                                          max_pages=max_pages)
        except SymbolNotFound as exc:
            failures.append({"ticker": code, "reason": f"없는 종목: {exc}"})
            continue
        except KiwoomRestError as exc:
            failures.append({"ticker": code, "reason": str(exc)})
            log.warning("%s 신용잔고 조회 실패: %s", code, exc)
            continue
        rec = pick_latest_credit(records)
        if rec is None:
            failures.append({"ticker": code, "reason": "신용잔고 값 없음"})
            continue
        latest[code] = rec

    obs = []
    for code, rec in latest.items():
        remn = parse_number(rec.get(FIELD_BALANCE))
        ratio = parse_number(rec.get(FIELD_BALANCE_RATIO))
        listed = shares_map.get(code)
        if remn is not None and ratio is not None and listed:
            obs.append((remn, ratio, float(listed)))
    scale, scale_note = resolve_share_unit(obs)

    rows: list[dict] = []
    for code, rec in sorted(latest.items()):
        ratio = parse_number(rec.get(FIELD_BALANCE_RATIO))
        remn = parse_number(rec.get(FIELD_BALANCE))
        shares = None if (scale is None or remn is None) else remn * scale
        if ratio is None and shares is None:
            failures.append({"ticker": code, "reason": "비율·주식수 모두 없음"})
            continue
        rows.append({"ticker": code, "ratio": ratio, "shares": shares,
                     "asof": _asof(rec, trade_date),
                     "note": f"kiwoom:{API_CREDIT_TREND}"})

    stats = {
        "requested": total,
        "answered": len(latest),
        "rows": len(rows),
        "failed": len(failures),
        "failures": failures[:20],
        "share_unit_scale": scale,
        "share_unit_note": scale_note,
        "api_stats": dict(client.stats),
        "limits_published": LIMITS_ARE_PUBLISHED,
    }
    log.info("신용잔고 실측 %d/%d종목 · 실패 %d · 단위 %s",
             len(rows), total, len(failures), scale_note)
    return rows, stats
