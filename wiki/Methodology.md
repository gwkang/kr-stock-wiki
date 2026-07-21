---
title: 분석 방법론
created: 2026-07-18
updated: 2026-07-21
type: concept
tags: [methodology, short-term]
sources: [https://data.krx.co.kr/, https://www.nextrade.co.kr/, https://kind.krx.co.kr/]
as_of: 2026-07-21T10:17:03+09:00
confidence: high
---
# 분석 방법론

## 범위

- 투자 시간 범위: 1~5거래일
- 활성 실행: 평일 07:30 KST pre-market, 평일 20:45 KST post-market
- 시장: KRX와 NXT 프리·메인·애프터마켓
- 관심종목: 최대 20개
- 심층 리포트: 최대 5개

## 공식 pre-market 근거

1. Global KRX 연간 calendar로 분석일이 예정 거래일인지 확인하고 exact 직전 거래일을 계산합니다. calendar는 당일 실제 정상 개장 근거로 승격하지 않습니다.
2. exact 직전 거래일의 KOSPI·KOSDAQ 완전 KRX 일별 snapshot과 NXT session-summary·종목별 20분 지연 canonical quote를 사용합니다.
3. 후보 신호는 직전 거래일 KRX `price-volume`과 NXT `cross-market`을 결합하며 ID·URL·시각·등락률·거래량·거래대금·score·reason을 공식 record에 결속합니다.
4. 모든 후보의 분석일 KIND 위험 상태와 candidate 전체 canonical 재계산을 통과해야 합니다.
5. 07:30에는 당일 KRX·NXT 거래가 시작됐거나 정상 개장했다고 주장하지 않습니다. 당일 live activity는 장전 후보 신호에 포함하지 않습니다.

## 공식 post-market 근거

1. Global KRX 연간 calendar는 예정 휴장일을 거부하는 veto로만 사용합니다.
2. 같은 기준일의 KOSPI·KOSDAQ 양 시장 endpoint가 모두 완료되고 ticker 유일성 및 cardinality 하한을 통과한 KRX 일별 snapshot이 있어야 실제 거래일로 인정합니다. 이 하한은 gross truncation 방어선이며 공식 상장 유니버스와의 절대적 완전성 대조는 아닙니다.
3. NXT는 공식 프리·메인·애프터마켓 세션 합계와 `source_as_of`가 20:20 KST 이후인 20분 지연 quote를 요구합니다.
4. KRX `price-volume`과 NXT `cross-market`을 서로 다른 신호 그룹으로 계산합니다. NXT 거래대상이 아니거나 quote가 없는 종목은 독립 근거 2개 조건을 통과하지 않습니다.
5. 모든 후보에 대해 KIND의 당일 관리종목·거래정지·투자경고·투자위험 상태를 완전 수집합니다.
6. 후보 artifact는 게시 직전 같은 watchlist·KRX·NXT snapshot에서 다시 계산해 전체 필드를 대조합니다.

어느 단계든 공식 응답, 기준일, 완전성, 시각 계보 또는 종목 identity 검증에 실패하면 리포트와 Wiki를 갱신하지 않습니다. 적격 종목이 없으면 억지로 채우지 않습니다.

## 복합 신호와 점수

일반 후보는 서로 다른 신호 그룹과 근거가 최소 2개 필요합니다. 동일 원인의 중복 자료는 독립 신호로 계산하지 않습니다. 현재 자동 점수는 공식 KRX·NXT 등락률 절댓값 기반의 결정론적 1차 점수이며, 운영 필터가 유동성과 KIND 위험을 별도로 적용합니다.

## 현재 역할 범위

시장 스캐너, 기업·재무, 산업·경쟁, 밸류에이션, 공시·이벤트, 리스크·반대 관점, 리서치 편집장의 7개 역할 템플릿은 존재합니다. 현재 예약 리포트는 공식 가격·거래량·거래대금과 운영 위험을 사용하는 결정론적 버전이며, OpenDART·공식 뉴스·투자자별 수급 adapter와 실제 LLM 역할 실행은 아직 예약 파이프라인에 연결되지 않았습니다.

검증된 `wiki/`가 main에 커밋된 뒤 exact main commit SHA가 단일 직렬 배포 workflow로 전달되어 GitHub Wiki에 게시됩니다.

관련 문서: [[Home]], [[Candidates]], [[Disclaimer]].
