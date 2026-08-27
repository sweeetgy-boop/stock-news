# AGENTS.md

이 저장소를 자동으로 운용하는 에이전트를 위한 규약입니다.
호스트는 **Windows PC (타임존 KST)** 이고, 모든 실행은 아래 래퍼를 통합니다.

---

## 1. 실행 방법 — 반드시 래퍼를 쓰십시오

```
hermes\run.cmd --mode <모드> [옵션]
```

`python run_screen.py` 를 직접 호출하면 실패합니다. 이유가 셋입니다.

- PATH 의 `python` 이 Microsoft Store 스텁(`WindowsApps\python.exe`)을 가리킵니다.
  실행하면 Store 페이지가 열리거나 아무 일도 일어나지 않습니다.
- 콘솔 코드페이지가 949(cp949)라 한글 stdout 이 깨집니다.
- 상대경로(`data/quant.db`)를 쓰므로 작업 디렉터리가 저장소 루트여야 합니다.

래퍼가 이 셋을 처리합니다. PowerShell 이 편하면 `hermes\run.ps1` 도 동일합니다.

**결과는 `--json` 으로 받으십시오.** stdout 에 JSON 한 줄이 나오고 로그는
stderr 로 분리됩니다. 한글 로그를 스크레이핑하지 마십시오.

```
hermes\run.cmd --mode daily --json
{"mode":"daily","trade_date":"2026-08-24","scanned":1847,"failed":3,
 "picks":10,"top":[...],"exit_code":0,"elapsed_sec":214.3}
```

---

## 2. 종료 코드 — 이 값으로 판단하십시오

```
0   정상
1   실패 (예외 또는 치명적 오류)          → 알림 + 중단
2   부분 실패 (실패율 10% 초과)           → 알림, 다음 잡은 계속
3   락 충돌 (다른 잡 실행 중)             → 5~10분 후 재시도
4   전제조건 미충족                       → 선행 잡 실행 후 재시도
       · 마스터/시세 없음 (순서 오류)
       · .env 미설정 (TELEGRAM_* 없음)
64  인자 오류 (모드 오타 등)              → 재시도 무의미
90  저장소 이동 실패 (래퍼)
91  파이썬 없음 (래퍼)
```

**exit 3 은 실패가 아닙니다.** 재시도하십시오. exit 4 는 순서가 틀렸거나
설정이 빠졌다는 뜻입니다. exit 64 는 명령 자체가 틀렸으니 재시도하지 마십시오.

`argparse` 기본값이 2 라 EXIT_PARTIAL 과 겹치는 문제가 있어 인자 오류만
64(EX_USAGE)로 분리했습니다. 실측 확인된 값입니다.

```
--mode pos-list --json          -> 0
--mode daily (빈 DB)            -> 4
--mode update (락 점유 중)      -> 3   locked_by 에 점유자 정보 포함
--mode credit --force-unlock    -> 0   unlocked 목록 반환
--mode credit-kiwoom (앱키 없음) -> 4   reason=no_credentials
--mode runs (스케줄 공백 있음)   -> 2   stale 목록 반환
--mode __nope__                 -> 64
```

---

## 3. 잡 의존 순서 — 이 순서를 어기면 exit 4 가 납니다

```
[최초 1회]
  master     전종목 마스터 + 업종            2~3분
  backfill   시세 히스토리 적재              20~40분  ★ 재실행하면 이어서 받음
  flags      배제 플래그 초기 구축            5~10분
  credit-kiwoom  신용잔고 REST 실측 (선택)   3~10분   ★ 앱키 필요
  credit     신용잔고 CSV 적재 (선택)         수초

[매일]
  master  →  update  →  flags  →  daily  →  exits
```

`credit-kiwoom` 은 `daily` 가 만든 스캔 이력에서 대상을 고르므로 `daily`
뒤에 두는 것이 이상적입니다. 대상이 0종목이면 `exit 4` 가 납니다.

`backfill` 은 종목별 체크포인트가 있습니다. 중간에 끊기면 **같은 명령을 다시
실행하면 남은 종목만** 받습니다. `--json` 의 `remaining` 값이 0 이 될 때까지
반복하십시오. 타임아웃이 걱정되면 `--limit 500` 으로 나눠 돌리십시오.

---

## 4. 권장 스케줄 (KST)

```
08:20  --mode news
08:30  --mode brief-morning --no-collect
09:00~15:35  --mode flash              5분 간격. 창 밖이면 수초에 끝남
15:30  --mode master
15:40  --mode update
15:50  --mode flags --dart-limit 400
16:05  --mode daily
16:20  --mode exits
16:35  --mode credit-kiwoom          앱키가 있으면. 없으면 exit 4 로 즉시 끝남
18:20  --mode brief-evening
18:40  --mode runs                   배치 이력 점검. 공백이 있으면 exit 2
금 16:30  --mode weekly
금 16:40  --mode brief-weekly
금 16:50  --mode export
월 15:55  --mode credit
```

`flash` 는 4대 시간창(09:00~09:35 / 10:00~10:25 / 14:00~14:25 / 15:20~15:35)
안에서만 실제로 스캔합니다. 창 밖 호출은 즉시 `{"skipped":true}` 로 끝나므로
5분 간격으로 걸어도 부담이 없습니다.

**10:00 창이 최우선입니다.** 창별 예산이 독립이라 09시에 소진해도 10시는
살아있습니다.

---

## 5. 락 — 동시 실행하지 마십시오

DB 를 쓰는 모드는 파일 락으로 직렬화됩니다. 겹치면 `exit 3` 이 납니다.

```
쓰기 모드: master backfill update flags credit credit-kiwoom daily exits
           news brief-morning brief-evening flash pos-open fill pos-close
읽기 모드: weekly fib export pos-list brief-weekly runs
           backtest credit-probe kiwoom-plan          (락 없음)
```

이 두 목록과 `stocknews/joblock.py` 의 `MODE_TIMEOUTS` 키 집합이 정확히
일치해야 합니다. 스모크 테스트가 강제합니다. 예전에는 락을 잡지 않는
모드(fib/weekly/brief-weekly/export)에 타임아웃이 있고, 정작 락을 잡는
`pos-open`/`fill`/`pos-close` 는 빠져 있었습니다.

- 대기하려면 `--lock-wait 300` (최대 5분 대기)
- 배치가 비정상 종료해 락이 남았으면 `--force-unlock`
- 락은 모드별 예상 시간이 지나면 자동 만료됩니다 (backfill 2시간, daily 30분)

**`--no-lock` 은 쓰지 마십시오.** 디버그용입니다.

---

## 6. 절대 하지 말 것

- `data/quant.db` 를 삭제하지 마십시오. 재구축에 20~40분이 걸립니다.
- `backfill` 실행 중에 다른 쓰기 잡을 강제로 돌리지 마십시오.
- `--force-unlock` 을 습관적으로 쓰지 마십시오. 정상 실행 중인 잡의 락을
  빼앗아 DB 손상 위험이 있습니다. `exit 3` 이 반복될 때만 쓰십시오.
- 텔레그램 토큰을 로그나 커밋에 남기지 마십시오. `.env` 에만 둡니다.
- 실패한 잡을 무한 재시도하지 마십시오. `exit 1` 은 3회까지만.

---

## 7. 상태 확인

```
hermes\run.cmd verify              환경 검증 (핀 버전 + 런타임 기능)
hermes\run.cmd smoke               스모크 테스트 218건
hermes\run.cmd --mode pos-list     보유 현황 + 미체결 청산 신호
hermes\run.cmd --mode runs         배치 이력 + 스케줄 공백 점검
```

코드를 수정했다면 **반드시 `smoke` 를 먼저 돌리십시오.** 218건 전부 통과해야
합니다. 실패가 있으면 커밋하지 마십시오.

DB 안의 `runs` 테이블에 모든 배치 이력(모드·성공·실패·소요)이 남습니다.
`main()` 이 한 곳에서 기록하므로 예외로 죽은 실행도 남습니다.

`--mode runs` 가 그 이력을 읽습니다.

```
hermes\run.cmd --mode runs --json
hermes\run.cmd --mode runs --runs-days 30
hermes\run.cmd --mode runs --runs-mode daily     특정 모드 상세
```

매일 돌아야 하는 모드가 36시간 넘게 안 돌았으면 `exit 2` 로 알립니다.
휴장일에도 각 모드는 `skipped` 로 exit 0 을 내고 이력을 남기므로,
**이력의 공백은 곧 cron 이나 절전 문제**입니다.

---

## 8. 사람의 승인이 필요한 것

이 시스템은 **신호만 냅니다. 주문을 내지 않습니다.**

- 진입/청산 신호는 텔레그램으로 갑니다. 실제 매매는 사람이 합니다.
- `--mode fill` 은 사람이 체결한 뒤 결과를 기록하는 명령입니다.
  에이전트가 임의로 호출하지 마십시오.
- `--mode pos-open` / `pos-close` 도 사람의 지시가 있을 때만 실행하십시오.

포지션과 청산 기록은 실제 자금과 연결됩니다. 자동으로 만들거나 닫지 마십시오.

---

## 8-2. 휴장일 처리 — 주말·공휴일에 돌려도 안전합니다

배치를 매일 걸어도 됩니다. 휴장일에는 각 모드가 스스로 판단해 생략하고
`exit 0` + `{"skipped":true,"reason":"..."}` 을 돌려줍니다. 실패가 아닙니다.

```
flash    주말/공휴일/장시간 밖 -> 시세 조회조차 하지 않고 즉시 종료
daily    휴장일이거나 그 거래일 스냅샷이 이미 있으면 생략
exits    휴장일이거나 그 거래일 청산 신호가 이미 있으면 생략
update   알려진 휴장일은 재요청하지 않음 (holidays_skipped 로 보고)
master   종목 목록이 비정상적으로 적으면 갱신하지 않음
         (조회 실패를 휴장일로 기록하지 않으므로 다음 실행에서 재시도)
```

**공휴일 목록은 하드코딩하지 않았습니다.** 새 공휴일을 만나면 한 번
헛조회하고 `non_trading_days` 에 기록해 다음부터 건너뜁니다.

**단, 0건을 곧바로 휴장일로 기록하지는 않습니다.** 소스 장애로도 0건이
나오기 때문입니다(실제로 2026-08 에 KRX 전종목 엔드포인트가 막혀 그렇게
됐습니다). 그 상태로 기록하면 실제 거래일이 영구히 휴장일로 박혀 그
날짜를 두 번 다시 받지 못합니다. 그래서 이렇게 판정합니다.

```
주말 / 기지정 공휴일        -> 조회하지 않고 생략        (근거 있음)
기준 종목도 데이터 없음     -> 휴장일로 기록             (근거 있음)
기준 종목은 데이터 있음     -> 소스 장애. 기록하지 않고 실패 반환
                              (다음 실행에서 다시 시도)
```

기준 종목(삼성전자·SK하이닉스·현대차)은 종목별 엔드포인트로 조회합니다.
전종목이 막혀도 이쪽은 살아 있으므로 '거래일인지'를 독립 판정할 수 있습니다.

`--force` 로 가드를 무시할 수 있지만, **중복 알림이 발송되므로 쓰지
마십시오.** 스냅샷을 다시 만들어야 할 때만 사용하십시오.

월요일과 연휴 뒤에는 `brief-morning` 의 뉴스 창이 자동으로 넓어집니다.
'최근 16시간' 고정이면 주말 뉴스가 통째로 빠집니다.

## 9. 알려진 제약

- **종목별 신용잔고는 무료 공개 소스로는 자동 수집이 불가능합니다.** bld
  코드를 못 찾은 게 아니라, 데이터 자체가 공개되지 않습니다. 2026-08-24
  실측: KRX 통계 메뉴 464개에 신용거래융자 잔고 화면이 없고('신용'은 전부
  채권 발행사 신용등급), 네이버 금융에도 없습니다. `--mode credit-probe`
  가 이 판정을 실행 시점에 다시 확인합니다.

  **자동 경로는 키움 REST(`--mode credit-kiwoom`) 하나입니다.** 앱키가
  있으면 에이전트가 직접 돌릴 수 있습니다(11장). 없으면 수동
  CSV(`--mode credit`)를 쓰십시오. 값이 들어온 종목만 실측으로 채점되고
  나머지는 매물대 POC 프록시(`credit_heat` 1.25점 캡)입니다.

  적재 순서는 코드가 고정합니다. **뒤가 앞을 덮으므로 사람의 값이 항상
  이깁니다.**

  ```
  KRX 자동  →  data/credit_kiwoom.csv (source=kiwoom)
            →  data/credit_manual.csv (source=manual)
  ```

  `--mode credit` 한 번으로 셋을 순서대로 병합합니다. `--credit-file` 로
  수동 파일 경로를 바꿔도 그 항목은 **체인 마지막**에 남습니다.
  어떤 출처가 몇 종목을 채웠는지는 `--json` 의 `by_source` 로 나옵니다.
- **키움 OCX(OpenAPI+) 경로는 REST 로 대체됐습니다.** OCX 는 32비트
  프로세스 · pywin32 · PyQt5 · 로그인 창이 전부 필요해서 무인 실행이
  불가능합니다. REST 는 앱키/시크릿만 있으면 64비트 본체에서 그대로
  돕니다. 11장을 보십시오.
- **키움 REST 유량 제한 수치는 공개돼 있지 않습니다.** 가이드에 없습니다.
  대신 초과를 `return_code` 1700/1701/1702 로 알려줍니다. 그래서 보수적
  기본값(초당 3 / 분당 60 / 시간당 900)으로 시작해, 그 코드나 HTTP 429 를
  받으면 **스스로 속도를 절반으로 줄이고** 백오프합니다. 실제 제한을
  알게 되면 `.env` 의 `KIWOOM_REST_PER_SECOND` / `_PER_MINUTE` /
  `_PER_HOUR` 만 고치십시오. 코드에 박힌 '공식값'은 없습니다.
- **KRX 전종목 계열 엔드포인트가 막혔습니다** (2026-08 실측).
  `getJsonData.cmd` 가 알려진 bld 에도 `HTTP 400 / LOGOUT` 을 돌려줘,
  pykrx 의 전종목·투자자별·공매도잔고 함수가 빈 결과를 줍니다.
  전종목 경로는 FinanceDataReader 스냅샷으로 대체했습니다. 종목별
  시계열(`get_market_ohlcv(start, end, ticker)`)은 정상입니다.
  스냅샷에 날짜 파라미터가 없어 장중에 부르면 현재가가 섞이므로,
  기준 종목 종가를 대조해 **확인된 경우에만** 적재합니다. 확인이 안 되면
  종목별 폴백으로 내려가고, 이때는 2,800종목 × 0.3초 ≈ 15분 걸립니다.
- 청산 파라미터는 `--mode backtest` 로 검증할 수 있으나, 백테스트 자체가
  생존 편향과 과거 시점 플래그 부재로 **낙관적**입니다. 실탄 규모를 키우기
  전에 반드시 리포트 하단의 한계 항목을 읽으십시오.
- 이 시스템은 투자 조언이 아닙니다. `DISCLAIMER.md` 를 참조하십시오.
- PC 가 절전/최대 절전으로 들어가면 cron 이 돌지 않습니다. 전원 옵션에서
  절전을 끄거나, 깨우기 타이머를 허용해야 스케줄이 유지됩니다.
- 네이버 금융은 마크업을 자주 바꿉니다. 로그에 `셀렉터 미매칭` 이 보이면
  `stocknews/news_sources.py` 의 셀렉터를 갱신해야 합니다.

---

## 10. 코드 수정 시

- 파라미터는 `stocknews/config.py` 한 곳에 모여 있습니다. 숫자를 코드에
  박지 마십시오.
- 앵커 검증이 있습니다. 피보 0.618 = 454,710원(고점 705,000/파동시작
  300,000), 청산 밴드 중심 = P0 × 0.70. 스모크 테스트가 이를 확인합니다.
- 진입 시점의 밴드/피보 레벨은 `positions` 테이블 스냅샷으로 고정됩니다.
  절대 재계산하지 마십시오. 손절선이 주가를 따라 내려갑니다.
- 신용잔고는 익영업일, 공매도는 T+2 공시입니다. 백테스트에서 shift 를
  빼면 승률이 실제보다 높게 나옵니다.
- **시각은 항상 KST 로 뽑으십시오.** `datetime.now()` 를 쓰면 UTC 호스트
  에서 날짜가 최대 9시간 뒤처집니다. 에러가 나지 않고 하루가 조용히
  비므로 알아채기 어렵습니다. `stocknews.trading_day.now_kst()` 또는
  `stocknews.notify.now_kst()` 를 쓰십시오. DB 저장은 naive KST 문자열
  규격입니다.
- **사람이 읽는 출력은 `_say()` 로 내십시오.** 맨 `print` 는 `--json`
  계약(stdout 은 JSON 한 줄)을 깹니다. 스모크 테스트가 회귀를 막습니다.
  한글이 섞인 표는 `_pad()` 로 맞추십시오. `{:<16}` 은 글자 수로 채워서
  전각 문자에서 어긋납니다.
- **배치 이력은 `main()` 이 한 곳에서 기록합니다.** 모드 안에서
  `store.log_run()` 을 부르지 마십시오. 이력이 두 줄씩 생깁니다.

---

## 11. 키움 (신용잔고 실측 경로)

종목별 신용잔고를 실제로 주는 유일한 자동 경로입니다. **REST 를 쓰십시오.**

```
권장   REST      --mode credit-kiwoom     에이전트가 직접 실행 가능
대체   OCX       kiwoom_bridge.py         사람이 실행 (로그인 창)
```

### 왜 REST 인가

OCX(OpenAPI+) 는 **32비트 COM** 입니다. 본체는 64비트 파이썬이고,
64비트 프로세스는 32비트 OCX 를 인프로세스로 로드할 수 없습니다.
레지스트리 실측으로도 `InprocServer32` 가 `WOW6432Node` 아래에만
있습니다(32비트 전용 등록). 그래서 별도 32비트 venv · pywin32 · PyQt5 ·
**로그인 창**이 필요합니다. 사람이 앉아 있어야 돌아갑니다.

REST 는 앱키/시크릿만 있으면 그 전부가 필요 없습니다.

### REST 사용법

```
1. https://openapi.kiwoom.com 에서 앱 등록 -> 앱키 / 시크릿
2. .env 에 KIWOOM_APP_KEY / KIWOOM_APP_SECRET 입력
3. hermes\run.cmd --mode kiwoom-plan          준비 상태 + 대상 계획 확인
4. hermes\run.cmd --mode credit-kiwoom --json 실측 수집 + DB 적재
```

```
--targets-file <파일>   대상 목록 지정 (없으면 계획기가 직접 고름)
--kw-limit 300          계획기가 고를 대상 상한
--kiwoom-out <경로>     결과 CSV (기본 data/credit_kiwoom.csv)
--kiwoom-mock           모의 도메인 (KIWOOM_API_BASE 가 있으면 그쪽 우선)
--date 2026-08-27       기준일 (기본: DB 의 최신 거래일)
```

수집이 끝나면 CSV 를 쓰고 **그 자리에서 DB 에 적재**합니다
(`source=kiwoom`). `--mode credit` 을 따로 부를 필요는 없지만, 불러도
같은 파일을 다시 읽으므로 안전합니다.

### 규격 (추측이 아니라 확정)

키움증권 공식 예제 저장소와 공개 가이드가 글자 단위로 일치합니다.

```
POST /oauth2/token                      au10001
     {grant_type:"client_credentials", appkey, secretkey}
  -> {token, token_type, expires_dt, return_code, return_msg}
     expires_dt = YYYYMMDDHHMMSS, KST      <- naive 로 두면 9시간 어긋남

POST /api/dostk/stkinfo                 header api-id: ka10013
     {stk_cd, dt:YYYYMMDD, qry_tp}          qry_tp 1:융자 2:대주
  -> {crd_trde_trend:[{dt, cur_prc, ..., remn, remn_rt, ...}], ...}
     remn    잔고        <- 단위가 문서에 없다. 런타임에 역산해 판정한다
     remn_rt 잔고율(%)   <- 단위 모호성이 없다. 이게 1순위다
```

토큰은 `data/kiwoom_token.json` 에 캐시됩니다. **앱키와 시크릿은
저장하지 않습니다** — sha256 지문 앞 16자만 남겨 '키가 바뀌었는지'만
판정합니다.

`remn` 단위(주 vs 천주)는 `remn_rt × 상장주식수 ÷ remn` 배율을 여러
종목에서 재서 중위값으로 정합니다. 후보(1 / 1000)에서 벗어나면
**추측하지 않고** 주식수를 비우고 비율만 씁니다.

### 호출 제한 — 수치가 공개되지 않았습니다

REST 가이드에 유량 제한 수치가 없습니다. OCX 문서에는 있습니다.

```
OCX (문서에 있음)    초당 5 / 분당 100 / 시간당 1,000
REST (공개 안 됨)    보수적 기본 초당 3 / 분당 60 / 시간당 900
```

REST 는 초과를 `return_code` 1700/1701/1702 로 알려줍니다. 그 코드나
HTTP 429 를 받으면 속도를 **절반으로 줄이고** 백오프합니다. 실제 제한을
알게 되면 `.env` 만 고치십시오.

대상을 좁히는 이유는 그대로입니다. 시간당 900 기준 실측 계산:

```
  300종목  ->    3.1분     <- 이걸 씁니다
  900종목  ->    9.4분
1,000종목  ->   61.0분     <- 시간 절벽
2,874종목  ->  3.02시간    <- 전종목. 매일 불가
```

그래서 대상은 보유 포지션 + 최근 추천 + 밴드 근처 종목입니다.

### 에이전트가 할 일과 하지 말 일

```
해도 됨   --mode kiwoom-plan                    환경·계획 확인 (조회 없음)
해도 됨   --mode kiwoom-plan --write-targets ... 대상 목록 파일 생성
해도 됨   --mode credit-kiwoom                   REST 실측 수집 + 적재
해도 됨   --mode credit                          CSV 체인 적재
하지 말 것 kiwoom_bridge.py 직접 실행            (로그인 창이 뜹니다)
```

키움 규정상 OpenAPI 사용 계좌는 한국거래소에 **알고리즘 계좌로 등록될
수 있습니다.** 조회만 해도 해당됩니다.

조회 TR 만 씁니다. 주문 엔드포인트와 주문 TR 은 코드에 등장하지 않으며,
스모크 테스트가 그 문자열의 부재를 회귀 가드로 검사합니다. 이 시스템은
주문을 내지 않습니다(8장).

**실서버로 쓰십시오.** 모의투자만 3개월 접속하면 서비스가 자동
해지됩니다. 모의 도메인은 KRX 만 지원합니다.

### OCX 경로 (대체 · 사람이 실행)

```
py -3.12-32 -m venv venv32
venv32\Scripts\pip install pywin32 PyQt5
venv32\Scripts\python kiwoom_bridge.py --check
venv32\Scripts\python kiwoom_bridge.py --discover
```

`--discover` 가 필요한 이유: OPT10013 의 **출력** 필드명이
`C:\OpenAPI\data\opt10013.enc` 로 암호화돼 있어 오프라인으로 읽을 수
없습니다. 후보를 실제 응답에 대보고 맞는 것만
`data/kiwoom_fields.json` 에 고정합니다. 입력 필드는 확정입니다
(`koatrinputlegend.ini`: 종목코드 / 일자 / 조회구분 1:융자 2:대주).

REST 에는 이 문제가 없습니다. 응답 필드명이 공개돼 있습니다.
