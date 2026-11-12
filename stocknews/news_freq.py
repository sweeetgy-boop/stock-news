# -*- coding: utf-8 -*-
"""뉴스 테마 언급 빈도. **기록 전용이다.**

무엇을 세는가
------------
일별 뉴스 제목에서 테마 키워드가 등장한 **기사 수**를 센다. 사전은
`config.NEWS_THEME_KEYWORDS` 하나가 정본이다.

다중 라벨이다. "반도체 실적 서프라이즈" 한 건은 `반도체` 와 `실적` 양쪽에
1건씩 잡힌다. `news.classify()` 가 첫 매치 하나만 남기는 것(단일 라벨)과
다르다. 충돌이 아니라 의도된 차이다 — 빈도는 관심도를 보려는 것이므로
한 기사가 두 테마를 건드리면 둘 다 세는 게 맞다.

기존 `news_theme_counts` / `theme_shift` 와의 차이
------------------------------------------------
  news_theme_counts   classify 가 부여한 단일 category 기준.
                      clusters(사건 수) 와 items(기사 수) 두 축.
  news_freq (여기)    키워드 매치 기준 다중 라벨. 기사 수 하나.

같은 날 같은 테마에 대해 두 숫자가 다를 수 있다. 세는 단위가 다르기
때문이고, 어느 쪽이 틀린 게 아니다.

**중복 보도는 증폭된다.** 같은 사건을 20개 매체가 쓰면 20으로 센다.
`cluster_id` 로 묶어 세면 증폭이 사라지지만, 그러면 대표 기사 제목에만
키워드가 있는 경우를 놓친다. 기사 수로 세는 쪽을 골랐고 스모크가 그
동작을 고정한다.

날짜 축
------
`scan_date` 는 **달력일**이다 (`news.d` 와 같은 축). 뉴스는 휴장일에도
나므로 거래일로 강제하면 주말 뉴스가 사라지거나 다음 거래일에 몰린다.
`sector_metrics.scan_date`(거래일)와 축이 다르다.

이동평균도 달력일 기준이고 **당일을 포함한** 최근 `ma_days` 일 평균이다
(`sector_metrics` 의 거래대금 변화율과 같은 규격). 당일이 포함돼 변화율이
1/7 만큼 눌리지만, 규격을 두 곳에서 다르게 두는 편이 더 위험하다.
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from .config import NEWS_THEME_KEYWORDS, Config, DEFAULT
from .trading_day import now_kst

log = logging.getLogger(__name__)

__all__ = ["count_mentions", "compute_news_freq", "collect_news_freq"]


def count_mentions(titles: pd.DataFrame,
                   keywords: dict | None = None) -> pd.DataFrame:
    """일자 x 테마 언급 기사 수. index=일자(문자열), columns=테마.

    titles : columns=[d, title]
    제목은 소문자로 맞춰 비교한다. 사전의 영문 키워드가 대문자 헤드라인에
    걸리지 않으면 영문 기사가 통째로 빠진다.
    """
    kw = keywords or NEWS_THEME_KEYWORDS
    themes = list(kw)
    if titles is None or titles.empty or "title" not in titles.columns:
        return pd.DataFrame(columns=themes, dtype="int64")

    lows = titles["title"].fillna("").astype(str).str.lower()
    hit = pd.DataFrame(
        {t: lows.apply(lambda s, ks=kw[t]: any(k in s for k in ks))
         for t in themes})
    hit["d"] = titles["d"].astype(str).str[:10].values
    out = hit.groupby("d")[themes].sum().astype("int64")
    return out.sort_index()


def compute_news_freq(titles: pd.DataFrame, scan_date: str,
                      keywords: dict | None = None,
                      cfg: Config = DEFAULT) -> list[dict]:
    """`scan_date` 하루치 테마 빈도. 순수 계산 — DB·네트워크를 만지지 않는다.

    이동평균은 `scan_date` 이하 구간에서만 구한다. 미래 일자가 섞이면
    그 날 알 수 없던 값으로 변화율을 만든다.

    일자가 `ma_days` 보다 적으면 `mention_cnt_ma7` 과 `chg_vs_ma7` 을
    만들지 않는다(None). 있는 만큼으로 평균을 내면 표본이 3일인 평균과
    7일인 평균이 같은 이름으로 기록돼 구분이 안 된다.
    """
    nc = cfg.news_freq
    kw = keywords or NEWS_THEME_KEYWORDS
    daily = count_mentions(titles, kw)
    if daily.empty:
        # 뉴스가 없는 날도 0 으로 기록한다 — 행이 없는 것과 0건은 다르다.
        return [{"scan_date": scan_date, "sector": t, "mention_cnt": 0,
                 "mention_cnt_ma7": None, "chg_vs_ma7": None} for t in kw]

    daily = daily[daily.index <= str(scan_date)[:10]]
    if daily.empty:
        return [{"scan_date": scan_date, "sector": t, "mention_cnt": 0,
                 "mention_cnt_ma7": None, "chg_vs_ma7": None} for t in kw]

    ma_days = int(nc.ma_days)
    window = daily.tail(ma_days)
    enough = len(window) >= ma_days
    today = (daily.loc[str(scan_date)[:10]]
             if str(scan_date)[:10] in daily.index else None)

    rows = []
    for theme in kw:
        cnt = 0 if today is None else int(today.get(theme, 0))
        ma = float(window[theme].mean()) if enough else None
        chg = None
        if ma is not None and ma > 0:
            chg = float((cnt / ma - 1.0) * 100.0)
        rows.append({"scan_date": str(scan_date)[:10], "sector": theme,
                     "mention_cnt": cnt,
                     "mention_cnt_ma7": (None if ma is None
                                         else float(np.round(ma, 4))),
                     "chg_vs_ma7": chg})
    return rows


def collect_news_freq(store, scan_date: str | None = None,
                      cfg: Config = DEFAULT) -> dict:
    """뉴스 빈도를 계산해 기록한다.

    **실패해도 예외를 올리지 않는다.** 기록 전용 부가 지표라, 이것 때문에
    뉴스 수집 배치가 죽으면 손해가 훨씬 크다.

    같은 일자를 다시 부르면 덮어쓴다. 하루 중 여러 번(아침·저녁 브리핑이
    뉴스를 다시 수집한다) 불릴 수 있고, 그때마다 최신 건수로 갱신되는 게
    맞다.
    """
    ds = str(scan_date or now_kst().strftime("%Y-%m-%d"))[:10]
    out = {"scan_date": ds, "themes": 0, "total_mentions": 0}
    try:
        titles = store.news_titles(days=cfg.news_freq.lookback_days)
        rows = compute_news_freq(titles, ds, cfg=cfg)
        out["themes"] = store.upsert_news_freq(rows)
        out["total_mentions"] = int(sum(r["mention_cnt"] for r in rows))
        out["has_ma"] = any(r["mention_cnt_ma7"] is not None for r in rows)
        top = sorted(rows, key=lambda r: -r["mention_cnt"])[:3]
        out["top"] = [(r["sector"], r["mention_cnt"]) for r in top]
    except Exception as exc:  # noqa: BLE001 - 배치를 죽이지 않는다
        log.warning("뉴스 빈도 기록 실패 — 건너뜁니다 (%s: %s)",
                    type(exc).__name__, exc)
        return out

    log.info("뉴스 빈도 기록 %s · %d테마 · 언급 %d건 (MA %s) · 상위 %s",
             ds, out["themes"], out["total_mentions"],
             "있음" if out.get("has_ma") else "없음(일자 부족)",
             " · ".join(f"{s} {n}" for s, n in out.get("top", ())))
    return out
