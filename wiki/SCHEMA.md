---
title: Wiki Schema
created: 2026-07-18
updated: 2026-07-18
type: policy
tags: [methodology, policy]
sources: [https://github.com/gwkang/kr-stock-wiki]
as_of: 2026-07-18T20:30:00+09:00
confidence: high
---
# Wiki Schema

## 필수 Frontmatter

모든 게시 문서는 다음 필드를 포함합니다.

```yaml
title:
created:
updated:
type:
tags:
sources:
as_of:
confidence: high | medium | low
```

## 문서 규칙

- 핵심 주장에는 원문 URL을 연결합니다.
- 사실, 해석, 반대 의견을 구분합니다.
- 초단기 리포트에는 최대 5거래일의 유효기간과 무효화 조건을 기록합니다.
- KRX와 NXT를 구분하고 지연 데이터 여부를 표시합니다.
- 모의 데이터는 실제 데이터처럼 게시하지 않습니다.
- 새 문서는 [[Home]] 또는 색인 문서에서 연결합니다.

## 태그

허용 태그: `market`, `stock`, `short-term`, `methodology`, `policy`, `risk`, `pre-market`, `post-market`.

관련 문서: [[Home]], [[Methodology]], [[Disclaimer]].
