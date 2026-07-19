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
- 공식 OpenDART 공시검색·KRX 일별 시세·NXT 20분 지연 시세 및 세션 집계·연합뉴스 RSS 수집기와 공통 근거 데이터 계약
- CLI, pytest, GitHub Actions CI

OpenDART·KRX·NXT·연합뉴스 RSS·KRX KIND 투자유의 상태 수집기가 구현됐습니다. OpenDART와 KRX는 각 API 키를 연결하면 공식 공시 및 KOSPI·KOSDAQ 일별 시세 스냅샷을 생성하며, NXT·연합뉴스 RSS·KIND는 별도 인증 없이 공식 웹사이트에서 각각 시세·세션 집계, 경제·산업·마켓 기사, 관리종목·거래정지·투자경고/위험 상태를 수집합니다. 투자자별 수급 수집기는 아직 연결되지 않았습니다. 샘플 신호는 입력 구조와 테스트 검증용으로만 사용하며 실제 최신 후보로 게시하지 않습니다.

## 설치와 테스트

```bash
uv sync
uv run pytest
```

## 리포트 생성

`run`은 구조 예제만으로 실행되지 않습니다. 후보 신호의 `business_date`와 일치하는 완전한 KRX 양시장 스냅샷, 분석일과 일치하는 후보별 KIND 상태 스냅샷이 모두 필요합니다. 연간 휴장일 calendar만으로는 당일 실제 운영상태를 증명할 수 없으므로 `pre-market` 실행은 아직 fail-closed로 차단됩니다.

```bash
uv run kr-stock-wiki run \
  --input build/signals/post-market.json \
  --krx-snapshot build/evidence/krx-${BUSINESS_DATE}.json \
  --kind-status build/evidence/kind-${ANALYSIS_DATE}.json \
  --output build/wiki

uv run kr-stock-wiki lint --wiki build/wiki
```

`examples/post-market-signals.json`은 입력 구조 검증용 예제입니다. KIND의 관리종목·거래정지는 현재 상태만 공식적으로 재현할 수 있으므로, 과거 예제에 현재 KIND 상태를 붙여 최신 운영 리포트로 재생할 수 없습니다.

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

KOSPI·KOSDAQ의 종가, 등락률, 시가·고가·저가, 거래량·거래대금, 시가총액, 상장주식 수를 공식 KRX 응답에서 정규화합니다. 인증키는 결과의 출처 URL, 오류 메시지, 예외 traceback 및 스냅샷에 기록하지 않습니다. 성공 스냅샷은 요청 시장·완료 시장·시장별 실제 레코드 수를 함께 저장합니다. 메타데이터·레코드 일관성 및 ticker 유일성뿐 아니라 운영 기본값으로 KOSPI 500건·KOSDAQ 1,000건의 보수적 cardinality 하한을 요구합니다. 0건은 휴장으로 추정하지 않고 `unknown`, 1건 이상이지만 하한 미만이면 부분 응답으로 거부합니다. KRX 응답에 독립 total-count가 없어 이 하한은 절대적 전체성 증명이 아닌 gross truncation 방어선이며, 향후 공식 상장 유니버스 대조로 강화해야 합니다.

## KRX 공식 휴장일 캘린더 수집

Global KRX의 `Market Closing(Holiday)` 페이지와 페이지가 직접 사용하는 공식 OTP·JSON 요청을 통해 선택 연도의 KRX 휴장일을 수집합니다. 인증키는 필요하지 않습니다.

```bash
uv run kr-stock-wiki collect-calendar \
  --year 2026 \
  --output build/evidence/krx-calendar-2026.json
```

collector는 `https://global.krx.co.kr/contents/GLB/05/0501/0501110000/GLB0501110000.jsp` 세션을 연 뒤 공식 `GenerateOTP.jspx`와 `GLB99000001.jspx` 요청만 사용합니다. 자동 redirect를 차단하고 exact HTTPS URL, 2 MiB 응답 상한, 필수 날짜·요일 필드, 날짜 정렬·유일성·선택 연도 일치 여부를 검증합니다. 응답에 독립 total-count가 없으므로 연간 최소 10건과 1월 1일·12월 31일 anchor를 gross-truncation 방어선으로 사용합니다. `holdy_eng_nm`은 공식 응답에서 빈 문자열일 수 있으므로 휴장 여부와 분리해 그대로 허용합니다.

이 artifact는 **예정 휴장일 목록**일 뿐 실시간 `OPEN/CLOSED` 또는 세션 시작시각 자료가 아닙니다. 목록에 없는 평일은 `scheduled trading day` 후보일 뿐 실제 개장이 확인된 날로 승격하지 않습니다. 비상휴장·수능일 지연 개장·연초 변경 등 공식 당일 시장운영 공지가 별도로 검증되기 전에는 pre-market gate를 열지 않습니다. JSON artifact는 collector가 원자적으로 저장한 로컬 파일을 신뢰 경계로 삼으며 암호학적 서명 문서가 아니므로, 쓰기 권한이 없는 경로에서 운영해야 합니다.

## KRX 공식 시장운영 공지 수집

```bash
uv run kr-stock-wiki collect-market-notices \
  --begin 2026-07-01 \
  --end 2026-07-20 \
  --output build/evidence/krx-market-notices-2026-07-20.json
```

collector는 KRX Data Marketplace의 공식 화면 `https://data.krx.co.kr/contents/MMC/NOTI/noti/MMCNOTI001.cmd`가 사용하는 공개 JSON POST 계약을 사용합니다. `mktId=ALL`, 기간 검색, page size 100으로 조회하고 `TOTAL_COUNT`, `CUR_PAGE`, `ROW_NUMBER`, 공지 ID, 게시일을 대조해 모든 페이지를 완주한 경우에만 snapshot을 발행합니다. exact HTTPS URL, redirect 차단, 페이지당 2 MiB 상한, 최대 367일·100,000건·1,000페이지 안전 한계를 적용하며 raw 원문 필드를 보존합니다. 긴 범위는 요청 수가 많으므로 짧은 rolling window 수집을 권장합니다.

이 공지는 휴장·수능일 지연개장·연말/연초 거래시간 변경 같은 **예외를 탐지해 분석을 거부하거나 연기하는 veto evidence**입니다. 매일 정상 운영을 선언하는 heartbeat가 아니며, 공지 부재·검색 결과 없음은 정상개장 positive evidence가 아닙니다. 시장 메타데이터가 실제 적용 시장과 일치하지 않는 사례가 있고 NXT도 포함하지 않으므로, 이 snapshot만으로 pre-market gate를 열지 않습니다. 현재 collector는 목록과 provenance를 보존할 뿐 본문 적용일·정정 관계·적용 시장을 자동 판정하지 않습니다.

## KRX KIND 투자유의 상태 수집

분석일 당일의 각 후보를 대상으로 KIND 공식 관리종목·매매거래정지 현재 목록과 최근 3년 투자경고·투자위험 지정/해제 이력을 조회합니다. 별도 인증키가 필요하지 않습니다.

```bash
uv run kr-stock-wiki collect-kind \
  --date 2026-07-18 \
  --ticker 005930 \
  --ticker 312610 \
  --output build/evidence/kind-2026-07-18.json
```

현재 목록으로 제공되는 관리종목·거래정지의 과거 상태를 추정하지 않기 위해 `--date`는 실행 시점의 KST 날짜만 허용합니다. 후보마다 KIND 공식 회사검색 JSON에서 A-prefixed 종목코드·6자리 단축코드·상장 상태·회사명을 먼저 교차 검증하고, 상태 AJAX endpoint가 실제 인식하는 검증된 6자리 단축코드와 회사명을 함께 사용한 네 상태 조회가 모두 성공해야 레코드를 만듭니다. 양성 행의 회사명도 공식 회사검색 결과와 대조하며, 식별 불가능하거나 다른 회사인 행은 fail-closed 합니다. 성공 스냅샷에는 요청·완료 ticker와 `coverage_complete: true`를 기록합니다. 각 상태는 출처·ticker·기준일·수집시각·공식 검증 상태를 가진 `listing-risk-status` 근거로 저장됩니다. 하나라도 누락되거나 비공식/날짜 불일치이면 운영 필터가 후보를 통과시키지 않습니다.

## NXT 시세 및 세션별 거래 현황 수집

NXT 공식 웹사이트에서 종목별 20분 지연 시세와 세션별 일일 집계를 별도 인증키 없이 수집합니다.

```bash
uv run kr-stock-wiki collect-nxt \
  --date 2026-07-16 \
  --output build/evidence/nxt-2026-07-16.json
```

종목별 현재가·등락률·OHLC·누적 거래량·거래대금·거래가능시장과 시장 전체의 프리마켓(`08:00~08:50`), 메인마켓(`09:00:30~15:20`), 애프터마켓(`15:40~20:00`) 거래 종목 수·거래량·거래대금을 함께 저장합니다. `curPrc`는 장중에도 변하는 현재가이므로 확정 종가로 간주하지 않고 `price-snapshot/current_price`로 보존합니다. 공식 `setTime`과 총 레코드 수를 모든 페이지에서 검증하며, 현재 시장의 영문 포함 6자리 종목단축코드도 보존합니다. 20분 지연 표기가 확인된 종목 시세 레코드에만 `delay_minutes: 20`을 적용하며, 세션 일별 집계에는 근거 없는 지연값을 부여하지 않습니다. 혼합 출력의 `quote_delay_minutes`도 종목 시세에만 적용됩니다.

## 연합뉴스 공식 RSS 수집

연합뉴스가 직접 발행하는 경제·산업·마켓 RSS의 GUID, 원문 URL, KST 발행시각, 기자, 요약을 수집합니다.

```bash
uv run kr-stock-wiki collect-news \
  --begin 2026-07-18 \
  --end 2026-07-18 \
  --output build/evidence/news-2026-07-18.json
```

같은 기사가 여러 피드에 나타나면 GUID로 병합하고 카테고리를 보존합니다. 모든 발행시각은 원본 문자열을 보존하면서 KST로 정규화합니다. 피드가 120건 한도에 도달하고 요청 시작일이 가장 오래된 항목 날짜 이전 또는 당일이면 과거 범위가 잘린 것으로 판단해 스냅샷을 쓰지 않고 실패합니다. 성공 출력에는 `coverage_complete: true`가 포함됩니다. `verification: official`은 RSS와 원문 링크가 연합뉴스의 1차 발행 경로임을 뜻하며, 기사 속 기업 주장이나 전망이 거래소·공시로 별도 확인됐다는 뜻은 아닙니다. RSS는 최신 기사 창만 제공하므로 장기 과거자료 API로 사용하지 않습니다.

## 거래일 및 운영 필터

`TradingDayGate`는 토·일요일을 휴장으로 처리하고, post-market 평일에는 같은 기준일의 공식 KRX 검증 스냅샷이 KOSPI·KOSDAQ 양 endpoint 완료, 시장별 cardinality 하한, ticker 유일성을 모두 충족할 때만 `open`으로 판정합니다. 임의 EvidenceRecord 목록은 입력으로 받지 않습니다. 평일에 스냅샷이 없거나 endpoint 완료가 빠지거나 어느 시장의 레코드가 0건이면 휴장으로 추정하지 않고 `unknown`, 1건 이상이지만 운영 하한 미만이면 부분 응답으로 처리합니다. 연간 calendar는 예정 휴장일을 판정하는 보조 근거로만 수집합니다. 평일이 목록에 없다는 음성 근거만으로 당일 실제 개장이나 정규 세션 시각을 확정하지 않으며, 별도의 공식 KRX 당일 운영상태 근거가 연결될 때까지 07:30 pre-market 실행은 CLI와 `ResearchHarness` 직접 호출 모두에서 fail-closed로 차단합니다.

`OperationalFilter`의 기본 유동성 하한은 종가 1,000원, 거래량 100,000주, 거래대금 50억원, 시가총액 1,000억원입니다. 기준은 생성자 인자로 명시적으로 변경할 수 있습니다. 관리종목·거래정지·투자경고는 KIND 공식 `listing-risk-status` 근거의 정수 `0/1` 값으로 확인돼야 하며 근거가 없거나 출처·ticker·기준일이 불일치하면 fail-closed로 후보에서 제외되거나 입력을 거부합니다. `as_of` 이후 수집된 근거는 금지하며 KIND는 최대 1시간, KRX 일별 스냅샷은 최대 12시간 이내 freshness를 요구합니다. `ResearchHarness.run`은 임의 운영 판정 map을 받지 않고 모든 후보와 정확히 일치하는 `OperationalEvidence` map의 KRX 가격·KIND 위험 근거를 자체 검증해 판정을 재계산하므로 직접 호출에서도 `eligible=True` 주입으로 필터를 우회할 수 없습니다.

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
