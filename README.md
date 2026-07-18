# kr-stock-wiki

한국 주식의 **1~5거래일 초단기 관심종목**을 근거 중심으로 발굴·분석하고 GitHub Wiki에 축적하기 위한 멀티에이전트 리서치 하네스입니다.

## 현재 구현 범위

- 7개 역할의 결정론적 분석 하네스
- 독립 신호 2개 이상을 요구하는 균형형 복합 신호 점수
- 위험 감점 및 강제 제외
- 후보 최대 20개, 심층 분석 최대 5개
- KRX/NXT 신호 그룹 지원
- 1~5거래일 유효기간
- 출처와 반대 의견이 포함된 Markdown 리포트
- YAML frontmatter 및 깨진 Wikilink 검사
- 실제 GitHub Wiki 저장소에 복사할 동기화 엔진
- 공식 OpenDART 공시검색·KRX 일별 시세·NXT 20분 지연 시세 및 세션 집계 수집기와 공통 근거 데이터 계약
- CLI, pytest, GitHub Actions CI

OpenDART·KRX·NXT 수집기가 구현됐습니다. OpenDART와 KRX는 각 API 키를 연결하면 공식 공시 및 KOSPI·KOSDAQ 일별 시세 스냅샷을 생성하며, NXT는 별도 인증 없이 공식 웹사이트의 20분 지연 종목별 시세와 프리·메인·애프터 세션 집계를 수집합니다. 수급·뉴스 수집기는 아직 연결되지 않았습니다. 샘플 데이터는 실행 검증용으로만 사용합니다.

## 설치와 테스트

```bash
uv sync
uv run pytest
```

## 모의 리포트 생성

```bash
uv run kr-stock-wiki run \
  --input examples/post-market-signals.json \
  --output build/wiki

uv run kr-stock-wiki lint --wiki build/wiki
```

## OpenDART 공시 수집

OpenDART 인증키는 명령행 인자가 아니라 환경변수로만 전달합니다. 키와 수집 결과는 저장소에 커밋하지 않습니다.

```bash
export DART_API_KEY="발급받은-40자리-인증키"
uv run kr-stock-wiki collect-dart \
  --begin 2026-07-18 \
  --end 2026-07-18 \
  --output build/evidence/dart-2026-07-18.json
```

특정 회사만 장기간 조회할 때는 8자리 DART 고유번호를 지정합니다.

```bash
uv run kr-stock-wiki collect-dart \
  --begin 2026-01-01 \
  --end 2026-07-18 \
  --corp-code 00126380 \
  --output build/evidence/dart-00126380.json
```

수집기는 페이지당 100건 제한을 자동 순회하고 페이지 경계 중복을 제거합니다. 정정공시는 `is_correction`으로 표시하되, OpenDART 목록 API가 원공시 계보를 제공하지 않으므로 제목만으로 원공시를 추정 연결하지 않습니다. `canonical_event_id`는 조회 범위와 무관하게 접수번호 기반으로 안정적으로 유지됩니다. 공식 원문 URL·수집 시각·검증 상태·원본 응답을 보존합니다. 회사 고유번호가 없는 시장 전체 검색은 OpenDART 공식 제한에 따라 최대 3개월입니다.

## KRX 일별 시세 수집

KRX 인증키도 명령행 인자가 아닌 환경변수로 전달합니다.

```bash
export KRX_API_KEY="발급받은-KRX-인증키"
uv run kr-stock-wiki collect-krx \
  --date 2026-07-17 \
  --output build/evidence/krx-2026-07-17.json
```

KOSPI·KOSDAQ의 종가, 등락률, 시가·고가·저가, 거래량·거래대금, 시가총액, 상장주식 수를 공식 KRX 응답에서 정규화합니다. 인증키는 결과의 출처 URL, 오류 메시지, 예외 traceback 및 스냅샷에 기록하지 않습니다.

## NXT 시세 및 세션별 거래 현황 수집

NXT 공식 웹사이트에서 종목별 20분 지연 시세와 세션별 일일 집계를 별도 인증키 없이 수집합니다.

```bash
uv run kr-stock-wiki collect-nxt \
  --date 2026-07-16 \
  --output build/evidence/nxt-2026-07-16.json
```

종목별 현재가·등락률·OHLC·누적 거래량·거래대금·거래가능시장과 시장 전체의 프리마켓(`08:00~08:50`), 메인마켓(`09:00:30~15:20`), 애프터마켓(`15:40~20:00`) 거래 종목 수·거래량·거래대금을 함께 저장합니다. `curPrc`는 장중에도 변하는 현재가이므로 확정 종가로 간주하지 않고 `price-snapshot/current_price`로 보존합니다. 공식 `setTime`과 총 레코드 수를 모든 페이지에서 검증하며, 현재 시장의 영문 포함 6자리 종목단축코드도 보존합니다. 20분 지연 표기가 확인된 종목 시세 레코드에만 `delay_minutes: 20`을 적용하며, 세션 일별 집계에는 근거 없는 지연값을 부여하지 않습니다. 혼합 출력의 `quote_delay_minutes`도 종목 시세에만 적용됩니다.

## 입력 계약

각 후보는 종목코드, 이름, 위험 감점과 신호를 갖습니다. 일반 후보가 되려면 최소 두 개의 서로 다른 `group`이 필요합니다.

지원 그룹:

- `catalyst`
- `price-volume`
- `flow`
- `sector`
- `freshness`
- `cross-market`
- `provenance`

모든 신호에는 원문 `source_url`과 관측 시각이 필요합니다.

## 예정 운영 시각

- 07:30 KST: NXT 프리마켓 전
- 20:30 KST: NXT 애프터마켓 종료 후

실시간 수집기와 GitHub 인증이 연결되기 전에는 예약 배포를 활성화하지 않습니다.

## 면책

이 프로젝트의 결과는 자동화된 조사 자료이며 투자 권유가 아닙니다. 원문, 최신 가격, 거래 가능 시장과 위험을 직접 확인해야 합니다.
