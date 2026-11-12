# -*- coding: utf-8 -*-
"""[L] v2 운영 플래그 .env 오버라이드 (config.apply_env_flags · env.load_env 배선)."""
from __future__ import annotations

import os

S = "v2 플래그 .env 오버라이드"


def _snapshot():
    from stocknews import config as C
    names = list(C.ENV_OVERRIDE_FLAGS)
    return ({n: getattr(C, n) for n in names},
            {n: os.environ.get(n) for n in names})


def _restore(snap):
    from stocknews import config as C
    attrs, envs = snap
    for n, v in attrs.items():
        setattr(C, n, v)
    for n, v in envs.items():
        if v is None:
            os.environ.pop(n, None)
        else:
            os.environ[n] = v


def run(check, near, tmp):
    def defaults_off():
        from stocknews import config as C
        from stocknews.env import KNOWN_KEYS
        for n, typ in C.ENV_OVERRIDE_FLAGS.items():
            assert n in KNOWN_KEYS, f"{n} 가 KNOWN_KEYS 에 없다"
            v = getattr(C, n)
            assert isinstance(v, typ), (n, v)
            # 기본값이 OFF 여야 개발 클론·스모크가 플래그 없이 기존 출력을 낸다.
            assert v in (False, 0), f"{n} 기본값이 OFF 가 아니다: {v!r}"
    check(S, "목록 = KNOWN_KEYS 부분집합 · 기본값 전부 OFF", defaults_off)

    def parse_values():
        from stocknews import config as C
        snap = _snapshot()
        try:
            env = {"PAPER_TRACKING": "1", "NEWS_V2": "TRUE", "RISK_INFO": "on",
                   "REGIME_BANNER": "0", "DAILY_LIST_V2": "maybe",
                   "RECO_COOLDOWN_DAYS": "20", "INDICATOR_LOG": " ",
                   "EXCLUDE_SEQ_FROM_SEND": "yes"}
            out = C.apply_env_flags(env)
            assert C.PAPER_TRACKING is True and C.NEWS_V2 is True
            assert C.RISK_INFO is True and C.REGIME_BANNER is False
            assert C.EXCLUDE_SEQ_FROM_SEND is True
            assert C.RECO_COOLDOWN_DAYS == 20
            assert C.DAILY_LIST_V2 is snap[0]["DAILY_LIST_V2"], "오타 값이 적용됐다"
            assert C.INDICATOR_LOG is snap[0]["INDICATOR_LOG"], "빈 값이 적용됐다"
            assert out["invalid"] == {"DAILY_LIST_V2": "maybe"}, out
            assert "INDICATOR_LOG" not in out["applied"]
            # 없는 키는 건드리지 않는다
            assert C.REGIME_TREND_CUT is snap[0]["REGIME_TREND_CUT"]
            out = C.apply_env_flags({"RECO_COOLDOWN_DAYS": "-3"})
            assert C.RECO_COOLDOWN_DAYS == 20 and "RECO_COOLDOWN_DAYS" in out["invalid"]
            out = C.apply_env_flags({"RECO_COOLDOWN_DAYS": "x"})
            assert C.RECO_COOLDOWN_DAYS == 20 and "RECO_COOLDOWN_DAYS" in out["invalid"]
        finally:
            _restore(snap)
    check(S, "1/true/on 켬 · 0 끔 · 오타·음수·빈 값은 기본값 유지", parse_values)

    def wired_load_env():
        from stocknews import config as C
        from stocknews.env import load_env
        snap = _snapshot()
        p = tmp / "flags.env"
        p.write_text("PAPER_TRACKING=1\nRECO_COOLDOWN_DAYS=20\n"
                     "REGIME_TREND_CUT=0\nNEWS_V2=nah\n", encoding="utf-8")
        for n in C.ENV_OVERRIDE_FLAGS:
            os.environ.pop(n, None)
        try:
            rep = load_env(p)
            assert C.PAPER_TRACKING is True and C.RECO_COOLDOWN_DAYS == 20
            assert C.REGIME_TREND_CUT is False
            assert rep["flags"] == {"PAPER_TRACKING": True,
                                    "RECO_COOLDOWN_DAYS": 20,
                                    "REGIME_TREND_CUT": False}, rep["flags"]
            assert rep["flags_invalid"] == ["NEWS_V2"], rep
            assert not rep["unknown_keys"], rep["unknown_keys"]
        finally:
            _restore(snap)
    check(S, "load_env 가 .env 플래그를 config 에 적용 · 보고에 싣는다", wired_load_env)
