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
- 평일 07:30 KST 장전 리포트와 20:45 KST post-market 리포트의 `wiki/` 커밋 및 GitHub Wiki 자동 배포

OpenDART·KRX·NXT·연합뉴스 RSS·KRX KIND 투자유의 상태 수집기가 구현됐습니다. OpenDART와 KRX는 각 API 키를 연결하면 공식 공시 및 KOSPI·KOSDAQ 일별 시세 스냅샷을 생성하며, NXT·연합뉴스 RSS·KIND는 별도 인증 없이 공식 웹사이트에서 각각 시세·세션 집계, 경제·산업·마켓 기사, 관리종목·거래정지·투자경고/위험 상태를 수집합니다. 투자자별 수급 수집기는 아직 연결되지 않았습니다. 샘플 신호는 입력 구조와 테스트 검증용으로만 사용하며 실제 최신 후보로 게시하지 않습니다.

## 설치와 테스트

```bash
uv sync
uv run pytest
```

## 리포트 생성

`run`은 구조 예제만으로 실행되지 않습니다. 모든 mode는 분석일과 5거래일 만료일을 계산할 수 있는 공식 KRX 연간 calendar artifact가 필요하며, 연말에는 다음 연도 artifact도 함께 전달해야 합니다. post-market 후보에는 같은 기준일의 완전한 KRX 양시장 스냅샷이 필요합니다. `pre-market` 후보에는 calendar가 계산한 정확한 직전 거래일의 완전한 KRX 양시장 스냅샷과 NXT 20분 지연 종가·세션 집계가 필요합니다. 두 mode 모두 분석일과 일치하는 후보별 KIND 상태가 필요합니다. 07:30 장전 리포트는 당일 KRX·NXT 거래가 시작됐다고 주장하지 않으며, 직전 공식 세션의 가격·거래량·거래대금과 분석일 예정 휴장 여부만 사용합니다.

```bash
uv run kr-stock-wiki run \
  --input build/signals/post-market.json \
  --krx-snapshot build/evidence/krx-${BUSINESS_DATE}.json \
  --calendar build/evidence/calendar-${ANALYSIS_YEAR}.json \
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

이 공지는 휴장·수능일 지연개장·연말/연초 거래시간 변경 같은 **예외를 탐지해 분석을 거부하거나 연기하는 veto evidence**입니다. 매일 정상 운영을 선언하는 heartbeat가 아니며, 공지 부재·검색 결과 없음은 정상개장 positive evidence가 아닙니다. 시장 메타데이터가 실제 적용 시장과 일치하지 않는 사례가 있고 NXT도 포함하지 않으므로, 이 snapshot만으로 pre-market gate를 열지 않습니다.

DRV 공지의 `BBS_SEQ`가 KIND 접수번호(`acptNo`)인 경우에는 상세문서를 별도 수집할 수 있습니다.

```bash
uv run kr-stock-wiki collect-kind-market-notice \
  --acceptance-number 20250520000110 \
  --output build/evidence/kind-market-notice-20250520000110.json
```

상세 collector는 KIND 공식 `searchInitInfo`에서 selected `docNo`와 이전 `|N` 문서번호를 확인하고, 같은 viewer의 `searchContents` POST를 거쳐 `https://kind.krx.co.kr/external/...` 원문 HTML을 가져옵니다. acptNo의 게시일·6자리 suffix, docNo, external 경로를 서로 결합 검증하고 exact HTTPS URL, `text/html` media type, redirect 차단, 단계별 2 MiB 상한을 적용합니다. init page, wrapper, external body 원문을 모두 artifact에 보존하며 load 시 원문에서 체인과 파생 필드를 다시 계산해 **경로·정규화 결과의 불일치**를 거부합니다. artifact 자체를 함께 수정할 수 있는 공격자에 대한 암호학적 변조 방지는 제공하지 않으므로, 쓰기 권한이 제한된 경로에서 운영해야 합니다. 보존된 HTML은 **증거 원문일 뿐 신뢰된 표시 콘텐츠가 아니므로** Wiki나 UI에 그대로 렌더링하지 않고, 향후 표시가 필요하면 별도 sanitization을 적용해야 합니다.

현재 구조화 parser는 본문에 명시된 `휴장일자`와 **단일 지원 시장명**이 함께 있고 날짜 label·후보가 각각 하나뿐인 휴장, 또는 유일한 `시행일`과 비부정형 `거래시간 변경` 표현 및 **단일 지원 시장명**이 함께 있는 세션 변경만 event로 승격합니다. NXT·대체거래소 같은 미지원 시장이 함께 있거나 복수 날짜·부정형·알 수 없는 양식이면 본문을 보존하되 `structured_complete: false`로 남겨 자동 정상 판정에 사용하지 않습니다. selected 문서 제목에 `정정`이 있으면 `is_correction: true`로 표시하고, 같은 viewer의 이전 `|N` docNo는 `prior_document_numbers`로 보존합니다. 이는 한 접수건 안의 공식 문서 버전 lineage일 뿐 별도 원 공지의 접수번호를 뜻하지 않으므로 `replaces_acceptance_number` 같은 관계는 추정하지 않습니다. 이 상세 artifact 역시 예외 veto를 강화할 뿐 정상개장 positive evidence가 아닙니다.

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

`TradingDayGate`는 토·일요일을 휴장으로 처리하고, post-market 평일에는 같은 기준일의 공식 KRX 검증 스냅샷이 KOSPI·KOSDAQ 양 endpoint 완료, 시장별 cardinality 하한, ticker 유일성을 모두 충족할 때만 `open`으로 판정합니다. pre-market은 분석일 calendar가 예정 거래일인지 확인하고, 같은 calendar가 계산한 exact 직전 거래일의 완전한 KRX snapshot으로 전일 세션을 검증합니다. calendar는 당일 실제 개장이나 세션 상태를 증명하지 않으므로 장전 리포트는 당일 거래가 시작됐다는 주장이나 당일 live signal을 만들지 않습니다.

`OperationalFilter`의 기본 유동성 하한은 종가 1,000원, 거래량 100,000주, 거래대금 50억원, 시가총액 1,000억원입니다. 기준은 생성자 인자로 명시적으로 변경할 수 있습니다. 관리종목·거래정지·투자경고는 KIND 공식 `listing-risk-status` 근거의 정수 `0/1` 값으로 확인돼야 하며 근거가 없거나 출처·ticker·기준일이 불일치하면 fail-closed로 후보에서 제외되거나 입력을 거부합니다. `as_of` 이후 수집된 근거는 금지하며 KIND는 최대 1시간 이내 freshness를 요구합니다. post-market KRX 일별 스냅샷은 최대 12시간으로 제한합니다. pre-market은 exact 직전 거래일의 KRX·NXT 완전 snapshot을 요구하며 긴 연휴에도 calendar가 계산한 직전 거래일이면 임의 wall-clock age 상한을 적용하지 않습니다. `ResearchHarness.run`은 모든 후보와 정확히 일치하는 `OperationalEvidence` 및 canonical NXT evidence를 자체 검증해 판정을 재계산합니다.

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

## 07:30 장전 자동 리포트

`.github/workflows/pre-market-report.yml`은 한국 평일 07:30 KST에 실행됩니다. UTC cron은 한국의 월–금과 정확히 맞도록 일–목 `22:30 UTC`(`30 22 * * 0-4`)입니다. 분석일이 official calendar의 예정 거래일이 아니면 정상 종료하고 Wiki를 변경하지 않습니다.

장전 후보는 calendar가 계산한 exact 직전 거래일의 완전한 KRX `price-volume`과 NXT 20분 지연 `cross-market`을 독립 근거로 사용합니다. NXT session-summary와 종목별 canonical quote가 직전 거래일에 결속돼야 하며, 모든 후보의 분석일 KIND 위험 상태를 다시 확인합니다. candidate artifact는 게시 직전 동일 watchlist·KRX·NXT snapshot에서 전체 재계산해 deep-equality로 대조합니다. 07:30에는 당일 KRX·NXT 실거래를 요구하거나 정상 개장을 주장하지 않습니다.

## 일일 post-market 자동 리포트

`.github/workflows/daily-report.yml`은 평일 `11:45 UTC`(20:45 KST)에 실행됩니다. GitHub Actions schedule은 부하에 따라 지연될 수 있습니다. `config/watchlist.json`에 명시된 1~20개 종목만 분석하며, 종목명은 공식 KRX 응답과 정확히 일치해야 합니다. 현재 기본 관심종목은 삼성전자(`005930`) 1개입니다.

운영 전 저장소의 **Settings → Secrets and variables → Actions**에 `KRX_API_KEY` secret을 등록해야 합니다. 키는 명령행·snapshot·로그·Wiki에 기록하지 않습니다. secret이 없거나 공식 수집·검증 단계가 하나라도 실패하면 리포트와 Wiki를 갱신하지 않습니다.

실행 순서:

1. Global KRX 연간 calendar를 검증하고 예정 휴장일·주말이면 정상 종료
2. 같은 KST 기준일의 KOSPI·KOSDAQ 전체 KRX snapshot 수집
3. NXT 프리·메인·애프터마켓 세션 합계와 `source_as_of ≥ 20:20 KST`인 종목별 20분 지연 snapshot 수집
4. KRX `price-volume`과 NXT `cross-market`을 독립 공식 근거로 변환하고, 리포트 직전에 동일 watchlist·KRX·NXT snapshot에서 다시 계산해 후보 artifact 전체와 정확히 대조
5. 모든 관심종목의 KIND 관리·정지·투자경고 상태를 당일 조회
6. 운영 필터와 후보 ranker를 통과한 최대 5개 리포트를 `wiki/`에 생성
7. Wiki lint 성공 후에만 main의 `wiki/`를 커밋하고, exact main commit SHA를 단일 직렬 Wiki 배포 workflow에 전달해 GitHub Wiki 탭에 동기화
8. raw official snapshot은 저장소에 커밋하지 않고 Actions artifact로 30일 보존

신호 점수는 각 시장의 공식 등락률 절댓값에 10을 곱해 0~100으로 제한하는 결정론적 값입니다. ranker에서 KRX 그룹은 최대 20점, NXT 그룹은 최대 10점으로 제한됩니다. NXT 거래대상이 아니거나 quote가 없는 종목은 KRX 신호만 남으므로 독립 그룹·근거 2개 조건을 통과하지 않습니다. 거래일은 최종 KRX 양시장 완전 snapshot으로 다시 확인하고, 유동성 하한과 KIND 위험 상태를 모두 통과한 경우에만 게시합니다. 적격 종목이 없으면 억지로 채우지 않고 후보 없음으로 게시합니다.

현재 자동 리포트는 **공식 가격·거래량·거래대금과 운영 위험에 기반한 결정론적 1차 버전**입니다. OpenDART 공시·연합뉴스 기사·공식 투자자별 수급을 자동 후보 신호로 결합하는 adapter와 실제 LLM 역할 실행은 아직 연결되지 않았습니다.

수동 재실행은 Actions의 `Pre-Market Research`와 `Daily Post-Market Research`에서 가능합니다. pre-market builder는 07:00~08:00 KST 밖의 실행을, post-market builder는 20:20 KST 이전 실행을 거부합니다.

## 예정 운영 시각

- 07:30 KST: **활성** — exact 직전 거래일 KRX·NXT와 분석일 KIND 상태 기반 장전 리포트
- 20:45 KST: **활성** — NXT 20분 지연 애프터마켓 snapshot 반영

## 면책

이 프로젝트의 결과는 자동화된 조사 자료이며 투자 권유가 아닙니다. 원문, 최신 가격, 거래 가능 시장과 위험을 직접 확인해야 합니다.
