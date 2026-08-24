# 면책 조항 / Disclaimer

## 이 소프트웨어는 투자 조언이 아닙니다

이 저장소는 **주식 스크리닝 연구 도구**입니다. 다음이 아닙니다.

- 투자 조언, 투자 권유, 매매 추천이 아닙니다
- 금융투자업 인가를 받은 서비스가 아닙니다
- 수익을 보장하거나 암시하지 않습니다
- 특정 종목의 매수·매도를 권유하지 않습니다

출력물(추천 10선, 진입·청산 신호, 점수, 리포트)은 **기계적 계산 결과**이며
사람의 판단을 대신하지 않습니다. 모든 투자 결정과 그 결과는 이용자 본인의
책임입니다.

## 이 시스템은 주문을 내지 않습니다

설계상 **신호만 생성합니다.** 증권사 API 와 연결되어 있지 않고, 자동으로
주문을 전송하지 않습니다. `--mode pos-open` / `fill` / `pos-close` 는 사람이
실제로 체결한 내역을 사후 기록하는 명령입니다.

## 검증되지 않은 가설이 포함되어 있습니다

다음 파라미터는 **아직 실증 검증이 끝나지 않은 가설**입니다.

```
신용 강제청산 밴드    담보유지비율 140% × 융자비율 가정
피보나치 0.618 기준   되돌림 깊이 배점
익절폭 +15%           절반 기계적 청산
트레일링 -8%          최고 종가 대비
시간 스톱 15/20거래일
밴드 이탈 확인 3일
```

`--mode backtest` 로 과거 데이터 검증을 돌릴 수 있으나, 그 결과 자체도
아래 한계를 갖습니다.

- 배제 플래그(관리종목·자본잠식)의 과거 시점 값이 없어 결과가 낙관적입니다
- 상장폐지 종목이 데이터베이스에 없어 생존 편향이 있습니다
- 신용잔고 실측 히스토리가 없어 핵심 지표가 프록시로 계산됩니다

**과거 성과는 미래 수익을 보장하지 않습니다.**

## 데이터 출처와 정확성

시세·공시·뉴스 데이터는 제3자 공개 소스(KRX, DART, 네이버 금융, Google News,
Yahoo Finance)에서 수집합니다. 다음을 보장하지 않습니다.

- 데이터의 정확성, 완전성, 적시성
- 수집 대상 사이트의 구조 변경에 대한 지속적 대응
- 액면분할·감자 등으로 인한 시세 불연속의 완전한 보정

각 데이터 제공처의 이용약관을 준수하는 것은 이용자의 책임입니다. 수집
빈도를 과도하게 높이면 해당 서비스의 이용약관을 위반하거나 접근이 차단될
수 있습니다.

## 레버리지와 신용거래 경고

이 시스템은 신용거래 강제청산(반대매매) 구간을 분석 대상으로 삼습니다.
이는 **신용거래를 권유하는 것이 아닙니다.**

신용거래와 레버리지 상품은 원금을 초과하는 손실이 발생할 수 있습니다.
담보유지비율이 붕괴되면 의사와 무관하게 반대매매로 강제 청산됩니다.
본인의 위험 감수 능력을 넘는 규모로 사용하지 마십시오.

## 책임 제한

이 소프트웨어의 저작자와 기여자는 다음에 대해 어떠한 책임도 지지 않습니다.

- 이 소프트웨어의 사용 또는 사용 불능으로 인한 직접·간접·부수적 손해
- 계산 오류, 데이터 오류, 신호 누락, 오탐으로 인한 손실
- 투자 손실, 기회 손실, 세무상 불이익

---

# Disclaimer (English)

This software is a **stock screening research tool**. It is not investment
advice, not a recommendation to buy or sell any security, and not a
solicitation. It does not place orders and is not connected to any brokerage.

Parameters in this system (credit liquidation bands, Fibonacci thresholds,
take-profit and stop-loss levels) are **unvalidated hypotheses**. Backtest
results carry survivorship bias and lack point-in-time exclusion flags,
making them optimistic.

Past performance does not guarantee future results. Margin trading and
leveraged instruments can produce losses exceeding your principal.

The authors and contributors accept no liability for any loss arising from
the use of this software. All investment decisions and their consequences
are solely the user's responsibility.
