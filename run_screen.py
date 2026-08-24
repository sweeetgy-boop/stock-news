# -*- coding: utf-8 -*-
"""배치 실행부.

최초 구축 (한 번만, 20~40분)
  python run_screen.py --mode master        # 전종목 마스터 + 업종
  python run_screen.py --mode backfill      # 히스토리 적재 (중단되면 재실행)

매일
  python run_screen.py --mode update        # 당일 시세 증분 (요청 2회)
  python run_screen.py --mode flags         # 배제 플래그 갱신 (청산 계층 0)
  python run_screen.py --mode daily         # 전종목 스캔 + 추천 10선 발송

매일 (뉴스)
  python run_screen.py --mode news            # 수집 + 정리만 (발송 없음)
  python run_screen.py --mode brief-morning   # 아침 브리핑 (개장 전)
  python run_screen.py --mode brief-evening   # 저녁 뉴스 정리

매주 금요일
  python run_screen.py --mode weekly        # 누적 데이터 주간 분석
  python run_screen.py --mode brief-weekly  # 주간 뉴스 테마 부침

포지션 / 청산
  python run_screen.py --mode pos-open --ticker 329180 --qty 100 --price 455500
  python run_screen.py --mode pos-list
  python run_screen.py --mode exits           # 청산 판정 + 발송 (매일)
  python run_screen.py --mode fill --log-id 3 --fill-price 452000
  python run_screen.py --mode pos-close --id 1

수시
  python run_screen.py --mode fib           # 피보 0.618 이하 목록
  python run_screen.py --mode flash         # 아침 즉시 속보

모든 모드에 --dry-run 을 붙이면 텔레그램 대신 콘솔로 출력한다.
환경변수: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from stocknews.config import DEFAULT
from stocknews.daily import run_daily, scan_all, select_recommendations
from stocknews.exits import run_exits
from stocknews.backtest import (BacktestConfig, control_random, control_rsi,
                                run_backtest, simulate_exit_rules,
                                summarize, summarize_exits, sweep_thresholds)
from stocknews.flags import flag_summary, refresh_credit, refresh_flags
from stocknews.kiwoom import build_plan as kiwoom_plan
from stocknews.kiwoom import check_environment as kiwoom_env
from stocknews.krx_credit import diagnose as krx_diagnose
from stocknews.krx_credit import refresh_credit_auto
from stocknews.joblock import JobLock, clear_locks
from stocknews.notify import TelegramNotConfigured, now_kst
from stocknews.trading_day import (is_definitely_closed, market_status,
                                   news_window_hours, should_scan_intraday)
from stocknews.news import process_and_store, theme_shift
from stocknews.news_sources import collect_all
from stocknews.notify import AlertGate, send_telegram  # noqa: F401
from stocknews.renderer import (render_detail, render_evening_brief,
                                render_exit_alert, render_exit_digest,
                                render_fib_list, render_morning_brief,
                                render_news_weekly, render_positions,
                                render_top10, render_weekly)
from stocknews.screener import screen_one
from stocknews.store import Store
from stocknews.universe import (backfill_one, fetch_day, fill_sectors,
                                liquidity_filter, refresh_master)
from stocknews.weekly import weekly_report

# 로그는 stderr 로 보낸다. stdout 은 --json 결과 전용이어야
# 에이전트가 파싱할 수 있다.
logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                    format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("run")

# ── 종료 코드 규약 (Hermes 가 이 값으로 재시도/알림을 판단한다) ──
EXIT_OK = 0            # 정상
EXIT_FAIL = 1          # 예외 또는 치명적 실패
EXIT_PARTIAL = 2       # 부분 실패 (실패율 임계 초과) — 알림 대상
EXIT_LOCKED = 3        # 다른 잡 실행 중 — 재시도 대상
EXIT_PRECOND = 4       # 전제조건 미충족 (마스터/시세/설정 없음)
EXIT_USAGE = 64        # 인자 오류. argparse 기본값 2 는 EXIT_PARTIAL 과
                       # 충돌하므로 EX_USAGE(64) 로 옮긴다.


class _Parser(argparse.ArgumentParser):
    """인자 오류를 exit 64 로 낸다.

    argparse 는 기본적으로 exit 2 를 쓰는데, 그건 우리 규약의
    '부분 실패'와 같은 값이다. 에이전트가 '오타'와 '스캔 실패 다수'를
    구분할 수 없게 되므로 분리한다.
    """

    def error(self, message):
        self.print_usage(sys.stderr)
        print(f"{self.prog}: error: {message}", file=sys.stderr)
        raise SystemExit(EXIT_USAGE)

# 스캔 실패율이 이 비율을 넘으면 부분 실패로 본다.
PARTIAL_FAIL_RATIO = 0.10

# DB 를 쓰는 모드. 락으로 직렬화한다. 읽기 전용 조회는 제외.
_WRITE_MODES = {"master", "backfill", "update", "flags", "credit",
                "daily", "exits", "news", "brief-morning", "brief-evening",
                "flash", "pos-open", "fill", "pos-close"}
# backtest 는 읽기 전용이지만 오래 걸린다. 락을 잡으면 그동안 daily 가
# 막히므로 제외한다. credit-probe 도 조회만 한다.

SUMMARY: dict = {}     # --json 출력용. 각 모드가 여기에 결과를 적는다.


def _emit(text: str, dry: bool) -> None:
    if dry:
        # --json 과 함께 쓸 때 stdout 을 오염시키지 않도록 stderr 로 보낸다.
        out = sys.stderr if SUMMARY.get("_json") else sys.stdout
        print("=" * 62, file=out)
        print(text, file=out)
        print("=" * 62, file=out)
        SUMMARY["messages"] = SUMMARY.get("messages", 0) + 1
        return
    send_telegram(text)
    SUMMARY["messages"] = SUMMARY.get("messages", 0) + 1
    log.info("텔레그램 발송 완료 (%d자)", len(text))


def _partial(ok: int, failed: int) -> bool:
    """실패율이 임계를 넘었는지. 넘으면 exit 2 로 알린다."""
    total = ok + failed
    if total == 0:
        return False
    return (failed / total) > PARTIAL_FAIL_RATIO


# ────────────────────────── 모드 구현 ──────────────────────────
def mode_master(store: Store, args) -> int:
    refresh_master(store, include_preferred=args.include_preferred)
    fill_sectors(store)
    n = len(store.active_tickers())
    log.info("활성 종목 %d개", n)
    SUMMARY["active_tickers"] = n
    # 마스터가 비면 이후 전부가 무의미하다. 조회 실패를 성공으로 보고하면
    # 에이전트가 다음 단계로 넘어가 더 큰 혼란이 생긴다.
    return EXIT_OK if n > 0 else EXIT_FAIL


def mode_backfill(store: Store, args) -> int:
    """히스토리 적재. 체크포인트 기반으로 재개 가능하다."""
    started = datetime.now()
    tickers = store.active_tickers()
    if not tickers:
        log.error("종목 마스터가 비어 있다. --mode master 를 먼저 실행하라.")
        return 1

    done = store.backfill_done()
    todo = [t for t in tickers if t not in done]
    if args.limit:
        todo = todo[: args.limit]
    log.info("백필 대상 %d종목 (완료 %d / 전체 %d)",
             len(todo), len(done), len(tickers))

    ok = fail = 0
    for i, code in enumerate(todo, 1):
        n = backfill_one(store, code, days=args.days, throttle=args.throttle)
        if n > 0:
            ok += 1
        else:
            fail += 1
        if i % 100 == 0:
            elapsed = (datetime.now() - started).total_seconds()
            rate = i / max(elapsed, 1)
            eta = (len(todo) - i) / max(rate, 1e-6) / 60
            log.info("  %d/%d 적재 (성공 %d, 빈값 %d) · 잔여 약 %.0f분",
                     i, len(todo), ok, fail, eta)

    store.log_run("backfill", started, ok, fail)
    bars = store.bar_count(120)
    log.info("백필 완료: 성공 %d / 빈값 %d · 120봉 이상 확보 %d종목",
             ok, fail, bars)
    remaining = len(store.active_tickers()) - len(store.backfill_done())
    SUMMARY.update({"ok": ok, "empty": fail, "with_120_bars": bars,
                    "remaining": max(0, remaining)})
    if remaining > 0:
        log.info("남은 종목 %d개 — 같은 명령을 다시 실행하면 이어서 받습니다",
                 remaining)
    return EXIT_OK


def mode_update(store: Store, args) -> int:
    """당일(또는 최근 누락일) 시세 증분 적재.

    PC/서버가 며칠 꺼져 있었어도 빠진 거래일을 자동으로 메운다.
    """
    started = now_kst()
    since = (started - timedelta(days=args.catchup + 5)).strftime("%Y-%m-%d")
    have = store.existing_dates(since=since)
    # 이미 휴장일로 확인된 날짜는 다시 요청하지 않는다. 이게 없으면
    # 공휴일을 catchup 기간 내내 매번 재요청한다(0건이라 have 에 안 들어감).
    holidays = store.known_non_trading_days(since=since)
    total, skipped = 0, 0
    for back in range(args.catchup, -1, -1):
        day = started - timedelta(days=back)
        if day.weekday() >= 5:            # 토/일
            continue
        ds = day.strftime("%Y-%m-%d")
        if ds in have:
            continue
        if ds in holidays:
            skipped += 1
            continue
        n = fetch_day(store, day.strftime("%Y%m%d"))
        total += n
        if n:
            time.sleep(0.6)
    if skipped:
        log.info("알려진 휴장일 %d일 건너뜀", skipped)
    SUMMARY["holidays_skipped"] = skipped
    store.log_run("update", started, total, 0)
    last = store.last_price_date()
    log.info("증분 적재 %d건 · 최신 거래일 %s", total, last)
    SUMMARY.update({"rows": total, "last_price_date": last})
    if not total and not have:
        # 시세가 하나도 없다. backfill 이 선행돼야 한다.
        return EXIT_PRECOND
    return EXIT_OK


def _scan_pool(store: Store, args):
    """스캔 대상 종목. 유동성 필터를 통과한 것만."""
    tickers = store.active_tickers()
    if not tickers:
        # 예외로 던지면 exit 1(치명적 실패)이 된다. 실제로는 순서가 틀린
        # 것이므로 호출부가 exit 4(전제조건)로 끝내게 빈 dict 를 돌린다.
        log.error("종목 마스터가 비어 있습니다. --mode master 를 먼저 실행하십시오.")
        return {}
    if args.limit:
        tickers = dict(list(tickers.items())[: args.limit])
    before = len(tickers)
    tickers = liquidity_filter(store, tickers, min_amt_20d=args.min_amount)
    log.info("유동성 필터: %d → %d종목 (20일 평균 거래대금 %.1f억 이상)",
             before, len(tickers), args.min_amount / 1e8)
    return tickers


def _closed_guard(store: Store, args, mode: str) -> str | None:
    """휴장일/중복 실행 사유. None 이면 진행해도 된다.

    휴장일에 돌면 last_price_date 가 직전 거래일을 가리키므로 같은
    스냅샷을 재생성하고 같은 알림을 토·일에 반복 발송한다.
    """
    if args.force:
        return None
    if is_definitely_closed(store):
        return f"{market_status(store)} — {mode} 생략"
    return None


def mode_daily(store: Store, args) -> int:
    """저녁 전종목 스캔 + 추천 10선."""
    reason = _closed_guard(store, args, "daily")
    if reason:
        log.info("%s", reason)
        SUMMARY.update({"skipped": True, "reason": reason})
        return EXIT_OK

    trade_date = store.last_price_date()
    if trade_date and store.has_scan(trade_date) and not args.force:
        # 같은 거래일 스냅샷이 이미 있다. 재실행하면 Top-10 이 중복 발송된다.
        log.info("%s 스냅샷이 이미 있습니다 — 생략 (--force 로 재실행)",
                 trade_date)
        SUMMARY.update({"skipped": True, "reason": "snapshot exists",
                        "trade_date": trade_date})
        return EXIT_OK

    tickers = _scan_pool(store, args)
    if not tickers:
        log.error("스캔 대상이 없습니다. master/backfill 을 먼저 실행하십시오.")
        return EXIT_PRECOND

    res = run_daily(store, cfg=DEFAULT, top_n=args.top, tickers=tickers)
    asof = now_kst()

    _emit(render_top10(res["picks"], asof, res["trade_date"],
                       scanned=len(res["results"]), cfg=DEFAULT), args.dry_run)

    if args.with_fib:
        picked = [r for r in res["results"]
                  if r.fib and r.fib.below_target and not r.excluded
                  and r.fib.confidence != "LOW"]
        _emit(render_fib_list(picked[:30], asof, DEFAULT), args.dry_run)

    ok, failed = len(res["results"]), len(res["errors"])
    SUMMARY.update({"trade_date": res["trade_date"], "scanned": ok,
                    "failed": failed, "picks": len(res["picks"]),
                    "top": [{"rank": i, "ticker": r.ticker, "name": r.name,
                             "grade": r.grade, "slot": slot,
                             "value": r.value_score, "trend": r.trend_score}
                            for i, (slot, r) in enumerate(res["picks"], 1)]})
    if failed:
        log.warning("스캔 실패 %d종목, 예시: %s", failed, res["errors"][:3])
    if not ok:
        return EXIT_PRECOND
    return EXIT_PARTIAL if _partial(ok, failed) else EXIT_OK


def mode_weekly(store: Store, args) -> int:
    rep = weekly_report(store, cfg=DEFAULT, week_days=args.week_days,
                        horizon=args.horizon)
    SUMMARY["days_covered"] = rep.get("days_covered", 0)
    _emit(render_weekly(rep, now_kst(), DEFAULT), args.dry_run)
    return EXIT_OK


def mode_fib(store: Store, args) -> int:
    tickers = _scan_pool(store, args)
    if not tickers:
        return EXIT_PRECOND
    results, errors = scan_all(store, tickers, DEFAULT)
    picked = [r for r in results
              if r.fib and r.fib.below_target and not r.excluded
              and r.fib.confidence != "LOW"]
    log.info("피보 %.3f 이하 %d종목 (실패 %d)",
             DEFAULT.fib.target, len(picked), len(errors))
    SUMMARY.update({"below_target": len(picked), "scanned": len(results),
                    "failed": len(errors)})
    _emit(render_fib_list(picked[:30], now_kst(), DEFAULT), args.dry_run)
    return EXIT_PARTIAL if _partial(len(results), len(errors)) else EXIT_OK


def mode_flash(store: Store, args) -> int:
    """장중 즉시 속보. 당일 봉이 필요하므로 시세를 먼저 갱신한다."""
    asof = now_kst()
    # 휴장일·장시간 밖이면 시세 조회조차 하지 않는다. 5분 간격 cron 이
    # 하루 288번 도는데 그때마다 KRX 를 찌르면 차단당한다.
    ok, why = should_scan_intraday(store, asof)
    if not ok and not args.ignore_window:
        log.info("스캔 생략: %s", why)
        SUMMARY.update({"skipped": True, "reason": why,
                        "window": None, "sent": 0})
        return EXIT_OK

    gate = AlertGate(cfg=DEFAULT, store=store)
    win = gate.current_window(asof)
    if win is None and not args.ignore_window:
        # 장중이지만 4대 시간창 밖이다. 스캔하지 않는다.
        log.info("시간창 밖 (%s) — 스캔 생략", asof.strftime("%H:%M"))
        SUMMARY.update({"window": None, "sent": 0, "skipped": True,
                        "reason": "outside alert window"})
        return EXIT_OK

    if not args.no_update:
        fetch_day(store, asof.strftime("%Y%m%d"))

    tickers = _scan_pool(store, args)
    if not tickers:
        return EXIT_PRECOND
    results, errs = scan_all(store, tickers, DEFAULT, progress_every=0)
    picked = gate.filter_tier1(results, now=asof,
                              ignore_window=args.ignore_window)
    SUMMARY.update({"window": win.name if win else "forced",
                    "scanned": len(results), "failed": len(errs),
                    "sent": len(picked),
                    "tickers": [r.ticker for r in picked]})
    if not picked:
        log.info("즉시 속보 대상 없음 (정상)")
        return EXIT_OK
    for r in picked:
        _emit(render_detail(r, cfg=DEFAULT), args.dry_run)
    if not args.dry_run:
        gate.commit(picked, now=asof, window=win)
    return EXIT_OK


def mode_news(store: Store, args) -> int:
    """뉴스 수집 + 정리만 수행 (발송 없음).

    브리핑 전에 먼저 돌려두는 배치다. 수집과 발송을 분리한 이유는
    한 소스가 느려도 브리핑 시각이 밀리지 않게 하기 위함이다.
    """
    raw = collect_all(use_naver=not args.no_naver,
                      use_google=not args.no_google,
                      use_yahoo=not args.no_yahoo,
                      use_dart=not args.no_dart)
    res = process_and_store(store, raw)
    log.info("뉴스 정리 완료: 수집 %d → 저장 %d · 종목링크 %d",
             res["collected"], res["stored"], res["links"])
    return 0


def mode_brief_morning(store: Store, args) -> int:
    """아침 브리핑. 개장 전 밤사이 해외 + 매크로 + 내 종목."""
    if not args.no_collect:
        raw = collect_all(use_naver=not args.no_naver,
                          use_google=not args.no_google,
                          use_yahoo=not args.no_yahoo,
                          use_dart=not args.no_dart)
        process_and_store(store, raw)
    # 월요일이나 연휴 뒤에는 '최근 16시간'만 보면 주말 뉴스를 통째로
    # 놓친다. 마지막 거래일 마감 이후 전부를 덮도록 자동 확장한다.
    hours = args.news_hours
    if not args.no_auto_window:
        hours = news_window_hours(store, now_kst(), base=args.news_hours)
        if hours > args.news_hours:
            log.info("뉴스 창 확장 %d → %d시간 (마지막 거래일 %s)",
                     args.news_hours, hours, store.last_price_date())
    SUMMARY["news_hours"] = hours
    _emit(render_morning_brief(store, now_kst(), hours=hours), args.dry_run)
    return EXIT_OK


def mode_brief_evening(store: Store, args) -> int:
    """저녁 뉴스 정리. 오늘 추천 10선과 교차해서 보여준다."""
    if not args.no_collect:
        raw = collect_all(use_naver=not args.no_naver,
                          use_google=not args.no_google,
                          use_yahoo=not args.no_yahoo,
                          use_dart=not args.no_dart)
        process_and_store(store, raw)

    picks = None
    rec = store.reco_history(days=1)
    if rec is not None and not rec.empty:
        # 오늘 추천 목록을 렌더러가 쓰는 최소 형태로 재구성한다.
        picks = [(r["slot"], _RecoRow(r)) for _, r in rec.iterrows()]

    _emit(render_evening_brief(store, datetime.now(), picks=picks,
                               hours=args.news_hours), args.dry_run)
    return 0


class _RecoRow:
    """recos 테이블 행을 렌더러가 기대하는 속성 형태로 감싼다.

    ScreenResult 전체를 다시 계산하지 않기 위한 얇은 어댑터다.
    저녁 브리핑은 종목명/코드/점수만 필요하다.
    """

    __slots__ = ("ticker", "name", "price", "grade",
                 "value_score", "trend_score")

    def __init__(self, row):
        self.ticker = str(row["ticker"])
        self.name = str(row["name"])
        self.price = float(row["price"]) if row["price"] is not None else 0.0
        self.grade = str(row["grade"])
        self.value_score = float(row["value_score"] or 0.0)
        self.trend_score = float(row["trend_score"] or 0.0)


def mode_brief_weekly(store: Store, args) -> int:
    """주간 뉴스 테마 부침."""
    shift = theme_shift(store, week_days=args.week_days)
    _emit(render_news_weekly(shift, datetime.now()), args.dry_run)
    return 0


def mode_credit_probe(store: Store, args) -> int:
    """신용잔고 자동 수집 가능성 진단.

    2026-08 조사 결론은 '불가'다. KRX 는 종목별 신용거래융자 잔고를
    공개하지 않는다. 그 결론을 문서에만 적어두면 KRX 가 정책을 바꿨을 때
    아무도 모르므로, 실행 시점에 엔드포인트와 메뉴를 다시 확인한다.
    """
    td = (args.date or store.last_price_date() or
          now_kst().strftime("%Y-%m-%d"))
    ymd = str(td).replace("-", "")[:8]
    extra = (args.bld,) if args.bld else ()
    rep = krx_diagnose(ymd, extra_blds=extra)
    SUMMARY["diagnose"] = rep

    print(f"\n신용잔고 자동 수집 진단 (기준일 {ymd})\n")

    ep = rep.get("endpoint", {})
    if ep.get("reachable"):
        print(f"  엔드포인트   응답함 (HTTP {ep.get('status')})")
    elif ep.get("login_required"):
        print(f"  엔드포인트   로그인 필요 — HTTP {ep.get('status')} "
              f"{ep.get('body')}")
        print("               익명 조회 경로가 닫혔습니다.")
    else:
        print(f"  엔드포인트   실패 — {ep.get('status') or ep.get('note')}")

    mn = rep.get("menu", {})
    hits = mn.get("credit_hits") or []
    if mn.get("ok"):
        print(f"  통계 메뉴     {mn.get('menus')}개 중 신용거래융자 화면 "
              f"{len(hits)}건")
        for h in hits:
            print(f"                - {h}")
    else:
        print(f"  통계 메뉴     확인 실패 — {mn.get('status') or mn.get('note')}")

    for t in rep.get("bld_tested") or []:
        mark = "OK " if t["usable"] else "   "
        print(f"  {mark}bld       {t['bld']}  rows={t['rows']}")

    cov = store.credit_coverage()
    print(f"\n  수동 주입     {cov['with_credit']}/{cov['active']}종목")
    print(f"\n  판정: {rep.get('verdict')}\n")
    for i, s in enumerate(rep.get("next_steps") or [], 1):
        print(f"    {i}. {s}")
    print()

    return EXIT_OK if rep.get("verdict") == "가능" else EXIT_PRECOND


def mode_kiwoom_plan(store: Store, args) -> int:
    """키움 OpenAPI+ 수집 계획 + 환경 준비도.

    OCX 를 건드리지 않으므로 64비트 본체에서 그대로 돌아간다. 실제 조회는
    32비트 `kiwoom_bridge.py` 가 한다.

    전종목이 왜 안 되는지를 말이 아니라 수치로 낸다. 시간당 1,000건
    제한이 지배적이라 2,800종목은 세 시간이 걸린다.
    """
    env = kiwoom_env()
    plan = kiwoom_plan(store, limit=args.kw_limit, scan_days=args.kw_days)
    SUMMARY["kiwoom_env"] = {k: v for k, v in env.items() if k != "missing"}
    SUMMARY["kiwoom_plan"] = {k: v for k, v in plan.items() if k != "tickers"}

    bit_note = ""
    if env.get("ocx_wow64_only"):
        bit_note = "  (InprocServer32 가 WOW6432Node 에만 = 32비트 전용)"
    print(f"\n키움 OpenAPI+ 환경  (파이썬 {env['python_bits']}비트)")
    print(f"  OCX 파일       {env['ocx_inproc_path'] or '없음'}")
    print(f"  COM 등록       {env['ocx_registered']}{bit_note}")
    print(f"  TR 정의 파일   {env['tr_defs']}")
    print(f"  pywin32 {env['win32com']}   PyQt5 {env['PyQt5']}   "
          f"KOA Studio {env['koa_studio']}")
    print(f"  준비 완료      {env['ready']}")
    if env["missing"]:
        print("\n  남은 준비:")
        for i, m in enumerate(env["missing"], 1):
            print(f"    {i}. {m}")

    lim = plan["limits"]
    print(f"\n호출 제한  초당 {lim['per_second']} / 분당 {lim['per_minute']} "
          f"/ 시간당 {lim['per_hour']}  (안전마진 적용 "
          f"{plan['safe_limits']['per_hour']}/시간)")
    print("  분당·시간당 제한에 걸리면 프로그램을 재실행해야 하므로 "
          "여유를 둡니다.")

    print(f"\n수집 대상  {plan['targets']}종목 / 활성 {plan['active_tickers']}종목")
    for reason, n in plan["by_reason"].items():
        print(f"    {reason:12s} {n}종목")
    print(f"\n  예상 소요    {plan['eta_sec'] / 60:.1f}분  "
          f"({plan['requests']}건)")
    print(f"  전수조사면   {plan['full_scan_eta_sec'] / 3600:.2f}시간  "
          f"({plan['full_scan_requests']}건)  <- 매일 불가")
    if not plan["feasible_daily"]:
        print("  ! 대상이 많아 1시간을 넘습니다. --kw-limit 을 줄이십시오.")

    cov = plan.get("coverage_now")
    if cov:
        print(f"\n현재 신용잔고 커버리지  {cov['with_credit']}/{cov['active']}종목"
              f" · 기준일 {cov['asof'] or '-'}")

    if args.write_targets:
        p = Path(args.write_targets)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("\n".join(plan["tickers"]) + "\n", encoding="utf-8")
        print(f"\n대상 목록 기록 -> {p}  ({len(plan['tickers'])}종목)")
        print("  32비트 브릿지에서:")
        print(f"    venv32\\Scripts\\python kiwoom_bridge.py "
              f"--plan-file {p}")
    else:
        print("\n  목록을 파일로 뽑으려면: --write-targets "
              "data/kiwoom_targets.txt")
    print()

    if not plan["targets"]:
        log.warning("대상이 0종목입니다. 먼저 --mode daily 로 스캔 이력을 "
                    "만들거나 포지션을 등록하십시오.")
        return EXIT_PRECOND
    return EXIT_OK if env["ready"] else EXIT_PRECOND


def mode_credit(store: Store, args) -> int:
    """신용잔고 주입. 이 시스템 핵심 가설의 데이터 구멍을 메운다.

    값이 들어온 종목만 LPS credit_heat 가 프록시 캡(1.25점)을 벗고
    만점(4.0점)까지 열리며, 청산 계층 6의 재급증 규칙이 살아난다.

    순서: KRX 자동 수집 → 수동 CSV 로 덮어쓰기.
    수동이 나중이므로 항상 사람의 값이 우선한다.
    """
    if not args.no_krx:
        auto = refresh_credit_auto(store, trade_date=args.date, bld=args.bld)
        SUMMARY["krx"] = auto
        if auto.get("ok"):
            log.info("KRX 자동 수집: %d종목 적재 (bld=%s)",
                     auto.get("stored", 0), auto.get("bld"))
        else:
            log.warning("KRX 자동 수집 실패: %s — 수동 CSV 로 진행",
                        auto.get("note"))

    res = refresh_credit(store, args.credit_file)
    cov = res.get("coverage") or store.credit_coverage()
    log.info("파싱 %s · 적재 %s · 주식수 역산 %s",
             res.get("parsed"), res.get("stored"), res.get("derived"))
    print(f"\n신용잔고 커버리지: {cov['with_credit']}/{cov['active']}종목 "
          f"· 최신 기준일 {cov['asof'] or '-'}")
    if not cov["with_credit"]:
        print(f"  {args.credit_file} 이 비어 있습니다.")
        print("  템플릿: data/credit_manual.csv.example")
        print("  값이 없으면 매집 점수 상한이 8.49점으로 눌립니다.\n")
    else:
        print("  해당 종목은 실측으로 채점됩니다 (credit_heat 최대 4.0점).\n")
    return 0


def mode_backtest(store: Store, args) -> int:
    """워크포워드 백테스트. 운영 코드를 절단해 호출하므로 룩어헤드가 없다."""
    tickers = _scan_pool(store, args)
    if not tickers:
        return EXIT_PRECOND

    bt = BacktestConfig(step=args.bt_step, cost_pct=args.bt_cost,
                        max_hold=args.bt_max_hold)
    log.info("백테스트 시작: %d종목 · 간격 %d거래일 · 비용 %.2f%%",
             len(tickers), bt.step, bt.cost_pct)

    events = run_backtest(store, tickers, DEFAULT, bt)
    if events.empty:
        log.error("이벤트가 0건입니다. 시세 기간이나 --limit 을 확인하십시오.")
        SUMMARY["events"] = 0
        return EXIT_PRECOND

    rnd = control_random(store, tickers, len(events), bt) \
        if not args.bt_no_controls else pd.DataFrame()
    rsi = control_rsi(store, tickers, 30.0, bt) \
        if not args.bt_no_controls else pd.DataFrame()

    summary = summarize(events, bt, rnd, rsi)
    sweep = sweep_thresholds(events, bt)
    exits_sim = (simulate_exit_rules(store, events, DEFAULT, bt)
                 if not args.bt_no_exits else pd.DataFrame())
    exit_sum = summarize_exits(exits_sim)

    _print_backtest_report(summary, sweep, exit_sum, bt, args)

    outdir = Path(args.export_dir)
    outdir.mkdir(parents=True, exist_ok=True)
    stamp = now_kst().strftime("%Y%m%d_%H%M")
    events.to_csv(outdir / f"bt_events_{stamp}.csv", index=False,
                  encoding="utf-8-sig")
    if not sweep.empty:
        sweep.to_csv(outdir / f"bt_sweep_{stamp}.csv", index=False,
                     encoding="utf-8-sig")
    if not exits_sim.empty:
        exits_sim.to_csv(outdir / f"bt_exits_{stamp}.csv", index=False,
                         encoding="utf-8-sig")
    log.info("결과 CSV: %s", outdir)

    SUMMARY.update({"events": int(len(events)), "summary": summary,
                    "exits": exit_sum, "out_dir": str(outdir)})
    # 경고가 있으면 부분 실패로 알린다. 전략이 검증되지 않았다는 신호다.
    return EXIT_PARTIAL if summary.get("warnings") else EXIT_OK


def _print_backtest_report(s: dict, sweep, ex: dict, bt, args) -> None:
    out = sys.stderr if args.json else sys.stdout
    w = 66

    def p(*a):
        print(*a, file=out)

    p("\n" + "=" * w)
    p(" 백테스트 결과")
    p("=" * w)
    if not s.get("n"):
        p(f"  {s.get('note', '이벤트 없음')}")
        return
    p(f"  표본 {s['n']:,}건 · 종목 {s['tickers']}개 · "
      f"기간 {s['period'][0]} ~ {s['period'][1]}")
    p(f"  익일 시가 체결 · 왕복비용 {bt.cost_pct}% 차감")

    p("\n── 보유기간별 (비용 차감 후) ──")
    p(f"  {'구간':<6}{'표본':>7}{'승률':>8}{'평균':>9}{'중위':>9}"
      f"{'초과':>9}{'최대역행':>10}")
    for h, item in (s.get("by_horizon") or {}).items():
        n = item.get("net", {})
        a = item.get("alpha", {})
        m = item.get("mae", {})
        if not n:
            continue
        p(f"  {h:>2}일 {n['n']:>8,}{n['win']:>7.0f}%{n['mean']:>+9.2f}"
          f"{n['median']:>+9.2f}{a.get('mean', float('nan')):>+9.2f}"
          f"{m.get('mean', float('nan')):>+10.2f}")

    ref = s.get("ref_horizon")
    p(f"\n── 대조군 비교 ({ref}일 기준, 초과수익) ──")
    ctrl = s.get("controls") or {}
    base = (s.get("by_horizon", {}).get(ref, {}).get("alpha", {}) or {})
    p(f"  우리 전략      {base.get('mean', float('nan')):>+8.2f}%p  "
      f"(승률 {base.get('win', 0):.0f}%, n={base.get('n', 0):,})")
    for key, label in (("market", "시장 중위"), ("random", "무작위 진입"),
                       ("rsi30", "단순 RSI<30")):
        c = ctrl.get(key)
        if c:
            p(f"  {label:<14}{c.get('mean', float('nan')):>+8.2f}%   "
              f"(승률 {c.get('win', 0):.0f}%, n={c.get('n', 0):,})")

    for key, label in (("by_grade", "등급"), ("by_track", "트랙"),
                       ("by_regime", "국면")):
        d = s.get(key)
        if not d:
            continue
        p(f"\n── {label}별 초과수익 ──")
        for k, v in sorted(d.items(), key=lambda kv: -(kv[1].get("mean") or 0)):
            p(f"  {k:<10}{v.get('mean', float('nan')):>+8.2f}%p  "
              f"승률 {v.get('win', 0):>3.0f}%  n={v.get('n', 0):,}")

    if sweep is not None and len(sweep):
        p("\n── 점수 임계값 스윕 ──")
        p(f"  {'트랙':<6}{'임계':>6}{'표본':>8}{'승률':>7}{'초과평균':>10}")
        for _, r in sweep.iterrows():
            p(f"  {r['track']:<6}{r['threshold']:>6.1f}{int(r['n']):>8,}"
              f"{r['win']:>6.0f}%{r['mean']:>+10.2f}")

    if ex.get("n"):
        p("\n── 청산 규칙 시뮬레이션 (실제 엔진) ──")
        n = ex["net"]
        p(f"  표본 {ex['n']:,}건 · 평균 보유 {ex['held_days']['mean']}거래일")
        p(f"  순수익 평균 {n['mean']:+.2f}% · 승률 {n['win']:.0f}% · "
          f"중위 {n['median']:+.2f}%")
        p(f"  최대역행 평균 {ex['mae']['mean']:+.2f}%")
        p("\n  계층별 (어느 규칙이 실제로 일하는가):")
        names = {0: "무효화", 1: "손절", 2: "트레일링", 3: "목표3차",
                 4: "목표2차", 5: "목표1차", 6: "시간", 7: "순환매",
                 99: "최대보유"}
        for layer, v in sorted(ex["by_layer"].items()):
            p(f"    {layer} {names.get(layer, '?'):<8}n={v['n']:>6,}  "
              f"평균 {v['mean']:>+7.2f}%  승률 {v['win']:>3.0f}%  "
              f"보유 {v['held_mean']:>4.1f}일")

    if s.get("warnings"):
        p("\n" + "!" * w)
        p(" 경고")
        p("!" * w)
        for wr in s["warnings"]:
            p(f"  · {wr}")
        p("\n  실탄 규모를 키우기 전에 파라미터를 재검토하십시오.")
    else:
        p("\n  대조군 대비 우위가 확인되었습니다.")

    p("\n── 이 결과의 한계 (반드시 인지) ──")
    p("  · 배제 플래그의 과거 시점 값이 없어 당시 관리종목이 표본에 섞임")
    p("  · 신용잔고 실측 히스토리가 없어 credit_heat 가 프록시 캡 상태")
    p("  · 상장폐지 종목이 DB 에 없어 생존 편향 존재")
    p("  → 결과는 실제보다 낙관적입니다.\n")


def mode_export(store: Store, args) -> int:
    """스냅샷·추천·플래그를 CSV 로 내보낸다.

    김승곤 차장이 신용잔고 증감률과 대조할 수 있게 하는 협업 경로다.
    엑셀에서 한글이 깨지지 않도록 utf-8-sig(BOM) 로 쓴다.
    """
    written = store.export_csv(out_dir=args.export_dir, days=args.export_days)
    print(f"\nCSV 내보내기 ({args.export_dir}):")
    for name, info in written.items():
        print(f"  {name:<14} {info['rows']:>7,}행   {info['path']}")
    print("\n  인코딩 utf-8-sig — 한글 엑셀에서 바로 열립니다.")
    print("  scans 파일이 전종목 점수표입니다. 신용잔고 대조용으로 넘기십시오.\n")
    return 0


def mode_flags(store: Store, args) -> int:
    """배제 플래그 갱신. 청산 계층 0(무효화)의 데이터 공급원이다.

    자본잠식(DART)은 종목당 최대 8회 조회가 들어가므로 TTL 캐시로
    호출을 줄이고, --dart-limit 으로 하루 분량을 나눠 돌릴 수 있다.
    """
    stats = refresh_flags(
        store,
        use_fdr=not args.no_fdr,
        use_dart=not args.no_dart,
        use_local=not args.no_local,
        use_manual=not args.no_manual,
        dart_limit=args.dart_limit,
        dart_ttl_days=args.dart_ttl,
        offering_days=args.offering_days,
    )
    for k, v in stats.items():
        if not k.startswith("_"):
            log.info("  %-26s %s", k, v)

    disc = stats.get("_discontinuity_list") or []
    if disc:
        print("\n시세 불연속 의심 (재적재 권고):")
        for code, memo in disc:
            print(f"  {code}  {memo}")
        print("  조치: DELETE FROM prices WHERE ticker='코드' 후 backfill 재실행\n")

    summary = flag_summary(store)
    if summary is not None and len(summary):
        print(f"\n배제 대상 {len(summary)}종목 (상위 25):")
        for _, r in summary.head(25).iterrows():
            imp = r.get("capital_impair")
            it = f" (잠식 {float(imp):.1f}%)" if imp == imp and imp else ""
            print(f"  {r['ticker']} {r['name'][:14]:<14} {r['reasons']}{it}")
        print()
    else:
        log.warning("배제 대상 0건 — 공급원이 모두 실패했을 수 있다")
    return 0


# ────────────────────────── 포지션 / 청산 ──────────────────────────
def mode_exits(store: Store, args) -> int:
    """보유 포지션 청산 판정.

    무효화·손절(계층 0~1)은 단건으로 즉시 발송하고, 나머지는 요약 1건으로
    묶는다. 청산은 진입 알림 예산(하루 3건) 밖에서 나간다.
    """
    reason = _closed_guard(store, args, "exits")
    if reason:
        log.info("%s", reason)
        SUMMARY.update({"skipped": True, "reason": reason})
        return EXIT_OK

    td = store.last_price_date()
    if td and store.has_exit_signal(td) and not args.force:
        # 손절·트레일링은 매일 재발송하는 설계다. 휴장일에 돌면 같은
        # 거래일 신호가 반복되므로 거래일 단위로 한 번만 낸다.
        log.info("%s 청산 신호가 이미 기록돼 있습니다 — 생략 (--force)", td)
        SUMMARY.update({"skipped": True, "reason": "signals exist",
                        "trade_date": td})
        return EXIT_OK

    res = run_exits(store, cfg=DEFAULT, include_rotation=not args.no_rotation)
    asof = now_kst()
    decs = res.get("decisions") or []

    for d in decs:
        if d.layer <= 1:
            _emit(render_exit_alert(d, DEFAULT), args.dry_run)

    _emit(render_exit_digest(res, asof, DEFAULT), args.dry_run)

    errs = res.get("errors") or []
    SUMMARY.update({
        "positions": res.get("positions", 0),
        "decisions": len(decs),
        "urgent": sum(1 for d in decs if d.layer <= 1),
        "failed": len(errs),
        "exits": [{"ticker": d.ticker, "name": d.name, "layer": d.layer,
                   "rule": d.rule, "action": d.action, "qty": d.qty,
                   "ret_pct": d.ret_pct} for d in decs],
    })
    if errs:
        log.warning("청산 판정 오류 %d건: %s", len(errs), errs[:3])
    return EXIT_PARTIAL if _partial(res.get("positions", 0), len(errs)) else EXIT_OK


def mode_pos_open(store: Store, args) -> int:
    """포지션 개시. 진입 시점 밴드/피보 레벨을 스냅샷으로 고정한다."""
    if not args.ticker or not args.qty:
        log.error("--ticker 와 --qty 가 필요합니다")
        return 1

    code = str(args.ticker).zfill(6)
    ohlcv = store.load_ohlcv(code, days=420)
    if ohlcv is None or len(ohlcv) < DEFAULT.ma.long + 30:
        log.error("%s 시세가 부족합니다 (backfill/update 확인)", code)
        return 1

    names = store.active_tickers()
    name = args.name or names.get(code, code)
    entry_price = float(args.price) if args.price else float(ohlcv["종가"].iloc[-1])
    entry_date = args.date or ohlcv.index[-1].strftime("%Y-%m-%d")

    res = screen_one(code, name, ohlcv, cfg=DEFAULT)
    snap: dict = {}
    if res.liq is not None:
        snap.update({"p0": res.liq.cost_basis, "band_hi": res.liq.band_hi,
                     "band_mid": res.liq.band_mid, "band_lo": res.liq.band_lo,
                     "credit_ratio": res.liq.credit_ratio
                     if res.liq.credit_ratio == res.liq.credit_ratio else None})
    if res.fib is not None:
        snap["fib_0382"] = res.fib.levels.get(0.382)
        snap["fib_0618"] = res.fib.levels.get(0.618)
    if (res.trend is not None and res.trend.best_cross
            and res.trend.best_cross.kind == "GOLDEN"):
        try:
            snap["cross_low"] = float(ohlcv.loc[res.trend.best_cross.date, "저가"])
        except KeyError:
            snap["cross_low"] = None

    track = args.track or ("TREND" if res.trend_score >= res.value_score
                           else "VALUE")
    pos_id = store.open_position(code, name, track, entry_date, entry_price,
                                 int(args.qty), snapshot=snap,
                                 opened_by="cli", note=args.note or "")

    log.info("포지션 개시 #%d %s(%s) %s %d주 @ %.0f",
             pos_id, name, code, track, int(args.qty), entry_price)

    def _won(key: str) -> str:
        v = snap.get(key)
        return f"{float(v):,.0f}원" if v else "없음"

    print(f"\n포지션 #{pos_id} 개시: {name} ({code}) [{track}]")
    print(f"  진입 {entry_date} @ {entry_price:,.0f}원 x {int(args.qty):,}주")
    print(f"  손절선(밴드 하단 -44%) : {_won('band_lo')}")
    print(f"  대량청산 중심(-30%)    : {_won('band_mid')}")
    print(f"  목표선(밴드 상단 -16%) : {_won('band_hi')}")
    print(f"  피보 0.382 (2차 익절)  : {_won('fib_0382')}")
    print(f"  골든크로스 봉 저가     : {_won('cross_low')}")
    print("  ※ 위 값은 고정됩니다. 이후 재계산하지 않습니다.\n")
    return 0


def mode_pos_list(store: Store, args) -> int:
    """보유 현황 조회. 로컬 상태 확인용이므로 텔레그램으로 보내지 않는다."""
    positions = store.list_positions(None if args.all else "OPEN")
    price_map = {p.ticker: store.load_ohlcv(p.ticker, days=5)
                 for p in positions}
    # 상태 조회는 stdout(--json 이면 stderr)으로만 낸다. 토큰이 없어도
    # 실패하지 않아야 한다.
    print(render_positions(positions, price_map),
          file=sys.stderr if args.json else sys.stdout)

    SUMMARY["positions"] = len(positions)
    SUMMARY["open"] = [{"id": p.id, "ticker": p.ticker, "name": p.name,
                        "track": p.track, "entry_price": p.entry_price,
                        "remaining": p.remaining}
                       for p in positions if p.status == "OPEN"]

    pending = store.pending_exits(days=args.catchup)
    out = sys.stderr if args.json else sys.stdout
    if pending is not None and not pending.empty:
        print("\n미체결 청산 신호:", file=out)
        for _, r in pending.iterrows():
            print(f"  log#{int(r['id'])} {r['name']}({r['ticker']}) "
                  f"L{int(r['layer'])} {r['action']} {int(r['qty'])}주 "
                  f"@ {float(r['signal_price']):,.0f} · {r['d']}", file=out)
        print("  체결 반영: --mode fill --log-id N --fill-price 가격\n", file=out)
        SUMMARY["pending_exits"] = int(len(pending))
    else:
        SUMMARY["pending_exits"] = 0
    return EXIT_OK


def mode_fill(store: Store, args) -> int:
    """체결 확인. 여기서만 잔량이 줄어든다."""
    if not args.log_id or args.fill_price is None:
        log.error("--log-id 와 --fill-price 가 필요합니다")
        return 1
    out = store.confirm_exit(int(args.log_id), float(args.fill_price),
                             int(args.fill_qty) if args.fill_qty else None)
    log.info("체결 반영: %s %d주 · 잔량 %d · %s",
             out["ticker"], out["filled_qty"], out["remaining"], out["status"])
    return 0


def mode_pos_close(store: Store, args) -> int:
    if not args.id:
        log.error("--id 가 필요합니다")
        return 1
    store.close_position(int(args.id), args.note or "manual close")
    log.info("포지션 #%s 수동 종료", args.id)
    return 0


MODES = {
    "master": mode_master,
    "backfill": mode_backfill,
    "update": mode_update,
    "daily": mode_daily,
    "weekly": mode_weekly,
    "fib": mode_fib,
    "flash": mode_flash,
    "news": mode_news,
    "brief-morning": mode_brief_morning,
    "brief-evening": mode_brief_evening,
    "brief-weekly": mode_brief_weekly,
    "flags": mode_flags,
    "credit": mode_credit,
    "credit-probe": mode_credit_probe,
    "kiwoom-plan": mode_kiwoom_plan,
    "backtest": mode_backtest,
    "export": mode_export,
    "exits": mode_exits,
    "pos-open": mode_pos_open,
    "pos-list": mode_pos_list,
    "fill": mode_fill,
    "pos-close": mode_pos_close,
}


def main(argv=None) -> int:
    ap = _Parser()
    ap.add_argument("--mode", choices=tuple(MODES), default="daily")
    ap.add_argument("--db", default="data/quant.db")
    ap.add_argument("--dry-run", action="store_true", help="콘솔 출력만")
    ap.add_argument("--limit", type=int, default=0, help="상위 N종목만 (테스트)")
    ap.add_argument("--top", type=int, default=10, help="추천 종목 수")
    ap.add_argument("--min-amount", type=float, default=5e8,
                    help="20일 평균 거래대금 하한(원)")
    ap.add_argument("--with-fib", action="store_true",
                    help="daily 모드에서 피보 목록도 함께 발송")
    ap.add_argument("--include-preferred", action="store_true",
                    help="우선주 포함 (기본 제외)")
    ap.add_argument("--days", type=int, default=420, help="backfill 적재 봉 수")
    ap.add_argument("--throttle", type=float, default=0.35,
                    help="backfill 요청 간 대기(초)")
    ap.add_argument("--catchup", type=int, default=7,
                    help="update 시 소급 확인할 일수")
    ap.add_argument("--week-days", type=int, default=5, help="주간 집계 거래일수")
    ap.add_argument("--horizon", type=int, default=5,
                    help="자기검증 보유 가정 거래일수")
    ap.add_argument("--ignore-window", action="store_true",
                    help="flash 시간창 무시 (테스트)")
    ap.add_argument("--no-update", action="store_true",
                    help="flash 시 시세 갱신 생략")
    # ── 뉴스 관련 ──
    ap.add_argument("--news-hours", type=int, default=16,
                    help="브리핑에 포함할 최근 시간 범위")
    ap.add_argument("--no-collect", action="store_true",
                    help="브리핑 시 수집 생략 (이미 news 모드로 받았을 때)")
    ap.add_argument("--no-naver", action="store_true", help="네이버 금융 제외")
    ap.add_argument("--no-google", action="store_true", help="Google News 제외")
    ap.add_argument("--no-yahoo", action="store_true", help="Yahoo Finance 제외")
    ap.add_argument("--no-dart", action="store_true", help="DART 공시 제외")
    # ── 포지션 / 청산 ──
    ap.add_argument("--ticker", help="pos-open 종목코드")
    ap.add_argument("--name", help="pos-open 종목명 (생략 시 마스터에서 조회)")
    ap.add_argument("--qty", type=int, help="pos-open 수량")
    ap.add_argument("--price", type=float,
                    help="pos-open 진입가 (생략 시 최근 종가)")
    ap.add_argument("--date", help="pos-open 진입일 YYYY-MM-DD")
    ap.add_argument("--track", choices=("VALUE", "TREND"),
                    help="pos-open 트랙 지정 (생략 시 점수로 자동 판정)")
    ap.add_argument("--note", help="포지션 메모")
    ap.add_argument("--id", type=int, help="pos-close 포지션 id")
    ap.add_argument("--log-id", type=int, help="fill 대상 exit_log id")
    ap.add_argument("--fill-price", type=float, help="fill 실제 체결가")
    ap.add_argument("--fill-qty", type=int,
                    help="fill 실제 체결 수량 (생략 시 신호 수량 전부)")
    ap.add_argument("--all", action="store_true",
                    help="pos-list 에서 종료된 포지션까지 표시")
    ap.add_argument("--no-rotation", action="store_true",
                    help="exits 에서 순환매(계층 7) 판정 제외")
    # ── 배제 플래그 ──
    ap.add_argument("--no-fdr", action="store_true",
                    help="flags: 관리종목(FDR) 조회 제외")
    ap.add_argument("--no-local", action="store_true",
                    help="flags: 로컬 판정(거래정지·동전주) 제외")
    ap.add_argument("--no-manual", action="store_true",
                    help="flags: 수동 CSV 오버라이드 제외")
    ap.add_argument("--dart-limit", type=int, default=0,
                    help="flags: 자본잠식 조회 종목 수 상한 (0=제한없음)")
    ap.add_argument("--dart-ttl", type=int, default=30,
                    help="flags: 자본잠식 캐시 유효일수")
    ap.add_argument("--offering-days", type=int, default=60,
                    help="flags: 증자/감사의견 공시 소급 일수")
    # ── 신용잔고 / 내보내기 ──
    ap.add_argument("--credit-file", default="data/credit_manual.csv",
                    help="credit: 수동 신용잔고 CSV 경로")
    ap.add_argument("--export-dir", default="data/export",
                    help="export: 출력 디렉터리")
    ap.add_argument("--export-days", type=int, default=10,
                    help="export: 스냅샷 소급 일수")
    # ── 에이전트 연동 ──
    ap.add_argument("--json", action="store_true",
                    help="결과 요약을 stdout 에 JSON 한 줄로 출력 "
                         "(로그는 stderr 로 분리)")
    ap.add_argument("--no-lock", action="store_true",
                    help="잡 락 사용하지 않음 (동시 실행 위험, 디버그용)")
    ap.add_argument("--lock-wait", type=int, default=0,
                    help="락이 잡혀 있을 때 대기할 초 (0=즉시 exit 3)")
    ap.add_argument("--force-unlock", action="store_true",
                    help="남아 있는 락을 강제 해제하고 시작")
    # ── 휴장일 / 중복 실행 ──
    ap.add_argument("--force", action="store_true",
                    help="휴장일 가드와 중복 실행 가드를 무시하고 실행")
    ap.add_argument("--no-auto-window", action="store_true",
                    help="brief-morning 의 뉴스 창 자동 확장을 끔")
    # ── KRX 신용잔고 자동 수집 ──
    ap.add_argument("--bld", help="credit/credit-probe: KRX bld 코드 직접 지정")
    ap.add_argument("--no-krx", action="store_true",
                    help="credit: KRX 자동 수집 없이 수동 CSV 만 사용")
    # ── 키움 OpenAPI+ ──
    ap.add_argument("--kw-limit", type=int, default=300,
                    help="kiwoom-plan: 수집 대상 상한 (기본 300)")
    ap.add_argument("--kw-days", type=int, default=5,
                    help="kiwoom-plan: 스캔/추천 이력 조회 일수 (기본 5)")
    ap.add_argument("--write-targets",
                    help="kiwoom-plan: 대상 종목코드를 이 파일에 기록")
    # ── 백테스트 ──
    ap.add_argument("--bt-step", type=int, default=5,
                    help="backtest: 평가 간격(거래일). 1이면 전수")
    ap.add_argument("--bt-cost", type=float, default=0.5,
                    help="backtest: 왕복 비용(%%)")
    ap.add_argument("--bt-max-hold", type=int, default=40,
                    help="backtest: 청산 시뮬 최대 보유 거래일")
    ap.add_argument("--bt-no-controls", action="store_true",
                    help="backtest: 대조군 생략 (빠른 확인용)")
    ap.add_argument("--bt-no-exits", action="store_true",
                    help="backtest: 청산 규칙 시뮬 생략")
    args = ap.parse_args(argv)

    SUMMARY.clear()
    SUMMARY["_json"] = bool(args.json)
    SUMMARY["mode"] = args.mode
    SUMMARY["started"] = now_kst().isoformat(timespec="seconds")

    if args.force_unlock:
        removed = clear_locks()
        log.warning("락 강제 해제: %s", removed or "없음")
        SUMMARY["unlocked"] = removed

    store = Store(args.db)
    t0 = time.time()
    lock = None
    try:
        # DB 쓰기 모드는 직렬화한다. 겹치면 database is locked 로 죽는다.
        if args.mode in _WRITE_MODES and not args.no_lock:
            lock = JobLock(mode=args.mode, wait_seconds=args.lock_wait)
            if not lock.acquire():
                log.error("다른 잡이 실행 중입니다: %s", lock.holder)
                SUMMARY["locked_by"] = lock.holder
                rc = EXIT_LOCKED
            else:
                rc = MODES[args.mode](store, args)
        else:
            rc = MODES[args.mode](store, args)
    except TelegramNotConfigured as exc:
        # 설정 문제다. 코드나 데이터 문제가 아니므로 전제조건으로 분류한다.
        log.error("%s", exc)
        rc = EXIT_PRECOND
        SUMMARY["error"] = str(exc)
    except KeyboardInterrupt:
        log.warning("사용자 중단")
        rc = EXIT_FAIL
        SUMMARY["error"] = "KeyboardInterrupt"
    except Exception as exc:  # noqa: BLE001
        log.exception("배치 실패 (%s): %s", args.mode, exc)
        rc = EXIT_FAIL
        SUMMARY["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if lock is not None:
            lock.release()

    elapsed = time.time() - t0
    SUMMARY["exit_code"] = int(rc)
    SUMMARY["elapsed_sec"] = round(elapsed, 1)
    SUMMARY["finished"] = now_kst().isoformat(timespec="seconds")
    log.info("[%s] 종료 rc=%d · 소요 %.1f초", args.mode, rc, elapsed)

    if args.json:
        payload = {k: v for k, v in SUMMARY.items() if not k.startswith("_")}
        print(json.dumps(payload, ensure_ascii=False, default=str))
    return rc


if __name__ == "__main__":
    sys.exit(main())
