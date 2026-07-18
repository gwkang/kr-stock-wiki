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
- CLI, pytest, GitHub Actions CI

실시간 DART·KRX·NXT·뉴스 수집기는 아직 연결되지 않았습니다. 하네스는 검증된 JSON 입력을 받으며, 샘플 데이터는 실행 검증용으로만 사용합니다.

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
