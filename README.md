# Market-News: 물류·공급망 일일 뉴스 브리핑 자동화

국내외 물류·공급망 뉴스를 매일 수집 → AI로 화주·물류사 영향도를 평가 → 상위 5건을 Notion 데이터베이스에 자동 적재하는 파이프라인.

```
[KSG / Google News RSS] → 원문 수집 → [Alpha AI 분석] → 우선순위·중복 필터 → [Notion DB 적재]
        (GitHub Actions, 매일 1회 실행)
```

---

## 1. 주요 기능

| 기능 | 내용 |
|---|---|
| 뉴스 수집 | 코리아쉬핑가제트(KSG) 메인 + Google News RSS 4종 검색어 (해운 / 항공·포워더 / 창고·3PL / 공급망·관세) |
| 원문 수집 | 기사 URL에서 본문 추출. Google News 링크는 `googlenewsdecoder`로 원문 URL 변환 |
| AI 분석 | 카테고리 분류, 화주·물류사 영향도(1~5), 1줄 요약, 시사점, 핵심문장, 중복그룹 판정 |
| 우선순위 선정 | 영향도 기반 점수 + 해운 편중 방지 + 유사 기사 제거 → **하루 5건** |
| Notion 적재 | 기사 1건 = DB 1행. 상세 페이지 상단에 AI 시사점, 하단에 원문 전체, 핵심문장은 노란색 하이라이트 |

---

## 2. 기사 선정 로직

### 2-1. 영향도 기준 (AI 평가)

국내 화주·물류사의 비용, 리드타임, 운영, 계약에 미치는 실질 영향 기준.

| 점수 | 의미 |
|---|---|
| 5 | 운임·선복·항로·관세·창고 수급 급변, 공급망 차질, 즉시 대응 필요 |
| 4 | 중기적으로 비용·운영에 유의미한 영향 전망 |
| 3 | 참고할 업계 동향 |
| 2 | 영향 제한적 (개별 기업 소식, 행사, 홍보성) |
| 1 | 영향 없음 |

### 2-2. 선정 규칙

1. **점수** = 영향도 × 10 (+2 가산: 해운·항공). 영향도 5인 창고·공급망 기사 > 영향도 4인 해운 기사.
2. **최소 영향도**: 해운 4 이상 / 그 외 3 이상. 해운 기사라도 화주·물류사 영향이 없으면 제외.
3. **해운 상한**: 기본 2건, 3번째는 영향도 5일 때만 허용 (절대 상한 3건).
4. **중복 제거**: AI 중복그룹 판정 + 제목 유사도(0.6 이상) 이중 필터.
5. **채움 규칙**: 5건 미달 시 해운 제외, 영향도 2 이상으로 보충.
6. 영향도 3 이상 기사가 부족하면 5건 미만으로 적재될 수 있음 (정상 동작).

### 2-3. 작성 형식

- 요약: 1줄, 개조식, 명사형 어미 (예: `홍해 우회 장기화로 아시아-유럽 운임 15% 상승`)
- 시사점: 1~2줄, 화주·물류사 관점, 명사형 어미
- 제목: 원문 제목 그대로 (별점 없음)

---

## 3. Notion 페이지 구조

```
[DB 행]
 ├─ 제목 / 날짜 / 분류 / 요약 / URL   ← 속성
 └─ 본문
     ├─ 💡 AI 시사점                  ← 상단 callout
     ├─ ───────────
     └─ 원문 기사 (핵심문장 노란 하이라이트)
```

본문 수집에 실패한 기사는 "본문 자동 수집 실패" 안내가 표시되며, URL 속성의 링크에서 원문 확인.

---

## 4. 사전 준비

### 4-1. Notion 데이터베이스

아래 속성명·타입이 **정확히 일치**해야 함.

| 속성명 | 타입 |
|---|---|
| 제목 | Title |
| 날짜 | Date |
| 분류 | **Multi-select** |
| 요약 | Text (Rich text) |
| URL | URL |

Integration 연결 필수: 해당 DB → 우측 상단 `⋯` → `Connections` → 생성한 Integration 추가.

### 4-2. GitHub Secrets

`Settings → Secrets and variables → Actions`에 등록.

| Secret | 값 |
|---|---|
| `ALPHA_API_KEY` | Alpha AI 게이트웨이 API 키 |
| `NOTION_TOKEN` | Notion Integration 토큰 |
| `NOTION_PARENT_PAGE_ID` | 대상 **데이터베이스 ID** (32자리. 이름은 PAGE이나 값은 DB ID) |

DB ID는 DB URL의 `notion.so/{workspace}/{32자리 ID}?v=...` 부분. 하이픈·URL 형태로 넣어도 코드가 32자리로 정규화함.

### 4-3. requirements.txt

```
requests==2.31.0
feedparser==6.0.11
beautifulsoup4==4.12.3
googlenewsdecoder
```

---

## 5. 실행

### 5-1. GitHub Actions 예시 (`.github/workflows/*.yml`)

```yaml
name: daily-briefing
on:
  schedule:
    - cron: "0 22 * * *"   # UTC 22:00 = KST 07:00
  workflow_dispatch:        # 수동 실행

jobs:
  run-briefing:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.10"
      - run: pip install -r requirements.txt
      - run: python news_briefing.py
        env:
          ALPHA_API_KEY: ${{ secrets.ALPHA_API_KEY }}
          NOTION_TOKEN: ${{ secrets.NOTION_TOKEN }}
          NOTION_PARENT_PAGE_ID: ${{ secrets.NOTION_PARENT_PAGE_ID }}
```

※ 기존 워크플로 파일의 스케줄·스텝이 있으면 그대로 사용. 위 내용은 참고용.

### 5-2. 로컬 실행

```bash
pip install -r requirements.txt
export ALPHA_API_KEY=...
export NOTION_TOKEN=...
export NOTION_PARENT_PAGE_ID=...
python news_briefing.py
```

---

## 6. 설정값 (`news_briefing.py` 상단)

| 상수 | 기본값 | 설명 |
|---|---|---|
| `DAILY_LIMIT` | 5 | 하루 적재 건수 |
| `POOL_LIMIT` | 15 | AI 분석 후보 풀 크기 |
| `SEA_SOFT` / `SEA_MAX` | 2 / 3 | 해운 기본 / 절대 상한 (3번째는 영향도 5만) |
| `MIN_IMPACT` | `{"해운": 4}` | 카테고리별 최소 영향도 |
| `DEFAULT_MIN_IMPACT` | 3 | 그 외 카테고리 최소 영향도 |
| `FILL_MIN_IMPACT` | 2 | 5건 미달 시 보충 기준 |
| `SIMILARITY` | 0.6 | 제목 유사도 중복 임계값 |
| `AI_SENT_LIMIT` | 12 | AI에 전달하는 기사당 최대 문장 수 (하이라이트 후보 범위) |
| `GOOGLE_QUERIES` | 4종 | Google News 검색어 |
| `FALLBACK_WITHOUT_AI` (환경변수) | `1` | AI 실패 시 원문만 적재. `0`이면 실패 처리 |

해운 편중이 다시 발생하면 `MIN_IMPACT["해운"] = 5`로 상향.

---

## 7. 로그 읽는 법

| 로그 | 정상 기준 |
|---|---|
| `KSG status: 200` | KSG 접근 정상 (실패해도 Google RSS로 대체) |
| `본문 수집 N/M건` | 50% 이상 권고. 30% 미만이면 수집 방식 재검토 |
| `Alpha status: 200` | AI 호출 정상 |
| `AI 평가: 1:해운/4, ...` | 기사번호:카테고리/영향도 |
| `적재 대상 5건: ...` | 해운 2~3건 이하 |
| `결과: 성공 5 / 실패 0` | 적재 정상 |

적재 0건이면 `exit 1`로 종료되어 Actions가 빨간불로 표시됨.

---

## 8. 트러블슈팅

| 증상 | 원인 | 조치 |
|---|---|---|
| 로그 출력 없이 0.2초 만에 종료 | 레포의 `news_briefing.py`가 로컬 수정본과 불일치 | 최신 코드 푸시 여부, 커밋 SHA 확인 |
| `환경변수 누락` | Secret 이름 오타 / 미등록 | Secrets 이름 3종 확인 |
| `Alpha AI 호출 실패: Connect...` | 사내 게이트웨이가 외부망(GitHub 러너)에서 접근 불가 | 사내망 전용 여부 확인. 불가 시 self-hosted runner 또는 사내 PC 스케줄러 |
| `(400) 분류 is expected to be multi_select` | `분류` 속성 타입 불일치 | 코드는 multi-select 기준. DB 속성 타입 확인 |
| `(400) ... is not a property` | 속성명 불일치 | `제목/날짜/분류/요약/URL` 철자 확인 |
| `(404) Could not find database` | Integration 미연결 또는 DB ID 오류 | Connections 추가, ID 32자리 확인 |
| `본문 수집 0/15건` | Google News URL 해제 실패 | `googlenewsdecoder` 설치·버전 확인. 비공식 방식이라 구글 측 변경 시 깨질 수 있음 |
| 노란 하이라이트 없음 | 본문 수집 실패 또는 AI가 핵심문장 미반환 | 본문 수집률 확인 |
| 적재 5건 미만 | 영향도 기준 충족 기사 부족 | 정상 동작. 필요 시 검색어 추가 또는 기준 완화 |

---

## 9. 한계 및 주의사항

- **AI 판단 변동성**: 영향도는 실행마다 ±1 변동 가능. 제목만 있는 기사는 정확도 하락.
- **원문 수집 불안정**: 유료·차단 사이트, 구조가 특이한 사이트는 본문 추출 실패 가능.
- **하이라이트 범위**: 기사당 앞 12문장 내에서만 선택.
- **중복 적재**: 현재 전일 기사와의 URL 중복 체크 없음. 매일 실행 시 동일 기사가 재적재될 수 있음 (개선 후보).
- **저작권**: 원문 전체를 Notion에 저장하므로 개인·사내 열람용으로 한정하고 외부 공유 금지.
- **보안**: 사내 AI 게이트웨이 키를 GitHub Secrets에 보관하는 경우 사내 정보보안·컴플라이언스 정책 확인 필요. 레포는 Private 권고.
- **Notion API 제한**: rich_text 2,000자, 블록 children 100개 제한을 코드에서 분할·절단 처리.

---

## 10. 파일 구성

```
Market-News/
├─ news_briefing.py        # 메인 스크립트
├─ requirements.txt        # 의존성
├─ README.md
└─ .github/workflows/      # GitHub Actions 워크플로
```

---

## 11. 향후 개선 후보

- 전일 URL 중복 적재 방지 (Notion DB URL 조회 후 skip)
- 물류 전문지 직접 RSS 추가로 Google News 의존도 축소
- 영향도 평가 일관성 개선 (본문 확보율 향상, 평가 기준 예시 추가)
- 요약 어미(명사형) 후처리 검증
