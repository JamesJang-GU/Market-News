import os
import re
import datetime
import requests
import feedparser
from bs4 import BeautifulSoup

# 1. 환경 변수 불러오기 (GitHub Secrets)
ALPHA_API_KEY = os.environ.get("ALPHA_API_KEY")
NOTION_TOKEN = os.environ.get("NOTION_TOKEN")
NOTION_PARENT_PAGE_ID = os.environ.get("NOTION_PARENT_PAGE_ID")

ALPHA_URL = "https://workspace.kr.pwc.com/api/workspace/coding_agent/v1/chat/completions"
NOTION_API_URL = "https://api.notion.com/v1/pages"


# ──────────────────────────────────────────────
# 뉴스 수집
# ──────────────────────────────────────────────
def fetch_ksg_news():
    """코리아쉬핑가제트(KSG) 메인 뉴스 크롤링 (최대 5개)"""
    url = "https://ksg.co.kr/news/main_news.jsp"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    articles = []
    try:
        res = requests.get(url, headers=headers, timeout=10)
        res.encoding = res.apparent_encoding
        if res.status_code == 200:
            soup = BeautifulSoup(res.text, "html.parser")
            seen = set()
            for a in soup.find_all("a", href=True):
                href = a["href"]
                title = a.get_text(strip=True)
                if "news_view.jsp" in href and title and len(title) > 12:
                    if href.startswith("http"):
                        full_url = href
                    elif href.startswith("/"):
                        full_url = f"https://ksg.co.kr{href}"
                    else:
                        full_url = f"https://ksg.co.kr/news/{href}"
                    if full_url not in seen:
                        seen.add(full_url)
                        articles.append({"title": title, "link": full_url, "source": "KSG"})
                        if len(articles) >= 5:
                            break
    except Exception as e:
        print(f"⚠️ KSG 크롤링 실패 (구글 뉴스로 대체): {e}")
    return articles


def fetch_google_logistics_news(count=5):
    """구글 뉴스 RSS에서 항공/육상/창고 등 종합 물류 뉴스 수집"""
    query = "항공물류 OR 화물운송 OR 물류센터 OR 공급망 OR 라스트마일"
    rss_url = f"https://news.google.com/rss/search?q={query}&hl=ko&gl=KR&ceid=KR:ko"
    feed = feedparser.parse(rss_url)
    return [
        {"title": e.title, "link": e.link, "source": "Google News"}
        for e in feed.entries[:count]
    ]


def fetch_all_news():
    """KSG + 구글 뉴스 합쳐서 최대 10개"""
    ksg = fetch_ksg_news()
    google = fetch_google_logistics_news(count=10 - len(ksg))
    return (ksg + google)[:10]


# ──────────────────────────────────────────────
# AI 분석 (분류 + 요약 + 시사점 + 중요도 평가 + 재배치)
# ──────────────────────────────────────────────
def analyze_with_alpha_ai(articles):
    articles_text = ""
    for idx, item in enumerate(articles, 1):
        articles_text += f"[{idx}] 제목: {item['title']}\n출처: {item['source']}\n링크: {item['link']}\n\n"

    system_prompt = """
당신은 종합 물류 및 공급망(SCM) 산업 전문 리서치 분석가입니다.
제공된 뉴스 목록을 분석하고, 실무 영향력이 큰 순서대로 재배치하여 출력하세요.

[1단계: 카테고리 분류]
아래 8개 중 가장 적합한 하나를 선택:
- 해운 (선사, 항만, 해운운임, 컨테이너 등)
- 항공 (항공화물, 항공운임, 공항물류 등)
- 육상 (트럭킹, 화물차, 택배, 라스트마일, 도로운송 등)
- 철도 (철도화물, 복합운송, 대륙철도 등)
- 창고·물류센터 (풀필먼트, 콜드체인, 물류부동산, 보관 등)
- 규제·정책 (정부정책, 관세, 노사관계, 파업, 무역규제 등)
- 친환경·기술 (탈탄소, AI/로봇 자동화, 스마트물류, 친환경연료 등)
- 종합·공급망 (SCM, 글로벌 공급망 이슈, 종합물류기업 소식 등)

[2단계: 중요도 평가 (1~5점)]
아래 4가지 기준으로 종합 평가:
① 운임·비용 영향: 물류비, 운임, 유가 등 비용 구조에 직접 영향을 주는가
② 공급망 리스크: 파업, 사고, 항만 적체, 지정학 등 물류 흐름 차질 가능성
③ 규제·정책 파급력: 법/제도/관세 변화로 업계 전반에 적용되는가
④ 시급성: 즉시 대응이 필요한가, 아니면 중장기 참고 정보인가

점수 가이드:
- 5점: 업계 전반에 즉각적·광범위한 영향 (대규모 파업, 주요 운임 급변, 핵심 규제 시행)
- 4점: 특정 영역에 상당한 영향 또는 중요한 선행 신호
- 3점: 참고할 가치가 있는 업계 동향
- 2점: 개별 기업 소식, 제한적 영향
- 1점: 홍보성·단신·실무 영향 거의 없음

[3단계: 재배치]
중요도 점수가 높은 순서대로 출력하세요. 동점이면 시급성이 높은 것을 먼저 배치하세요.
모든 기사를 빠짐없이 출력해야 하며, 번호는 원본 번호를 그대로 유지하세요.

[출력 형식] 아래 구분자를 정확히 지키세요:
---ARTICLE_START---
번호: [원본 번호 숫자만]
카테고리: [카테고리 명칭만]
중요도: [1~5 숫자만]
선정이유: [이 순위로 배치한 이유 1문장]
요약: [핵심 내용 2~3문장]
시사점: [물류 업계·화주·공급망 관점 영향 1~2문장]
---ARTICLE_END---
"""

    headers = {"Authorization": f"Bearer {ALPHA_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": "Default",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"다음 뉴스들을 분석하고 중요도순으로 재배치해 주세요:\n\n{articles_text}"},
        ],
    }
    try:
        response = requests.post(ALPHA_URL, headers=headers, json=payload, timeout=120)
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"]
    except Exception as e:
        print(f"Alpha AI 호출 실패: {e}")
        return None


def parse_ai_result(ai_analysis, articles):
    """AI 출력을 파싱해 원본 기사와 '번호' 기준으로 매칭 (순서가 바뀌므로 필수)"""
    results = []
    used_numbers = set()

    for block in ai_analysis.split("---ARTICLE_START---")[1:]:
        block = block.split("---ARTICLE_END---")[0]
        fields = {"번호": "", "카테고리": "종합·공급망", "중요도": "3",
                  "선정이유": "", "요약": "요약 정보 없음", "시사점": "시사점 정보 없음"}
        for line in block.split("\n"):
            line = line.strip()
            for key in fields:
                if line.startswith(f"{key}:"):
                    fields[key] = line[len(key) + 1:].strip()

        num_match = re.search(r"\d+", fields["번호"])
        if not num_match:
            continue
        num = int(num_match.group())
        if num < 1 or num > len(articles) or num in used_numbers:
            continue
        used_numbers.add(num)

        score_match = re.search(r"\d", fields["중요도"])
        score = int(score_match.group()) if score_match else 3
        score = max(1, min(5, score))

        orig = articles[num - 1]
        results.append({
            "title": orig["title"], "link": orig["link"], "source": orig["source"],
            "category": fields["카테고리"], "score": score,
            "reason": fields["선정이유"], "summary": fields["요약"], "insight": fields["시사점"],
        })

    # AI가 누락한 기사가 있으면 하위에 추가 (안전장치)
    for i, orig in enumerate(articles, 1):
        if i not in used_numbers:
            results.append({
                "title": orig["title"], "link": orig["link"], "source": orig["source"],
                "category": "종합·공급망", "score": 1, "reason": "AI 분석 누락",
                "summary": "요약 정보 없음", "insight": "시사점 정보 없음",
            })

    # AI 출력 순서를 신뢰하되, 점수 기준으로 한 번 더 안정 정렬
    results.sort(key=lambda x: -x["score"])
    return results


# ──────────────────────────────────────────────
# 노션 페이지 생성
# ──────────────────────────────────────────────
def create_notion_page(ranked_articles):
    today_str = datetime.datetime.now().strftime("%Y-%m-%d")
    page_title = f"📦 [{today_str}] 해운·물류 통합 브리핑 (중요도순 TOP {len(ranked_articles)})"

    headers = {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Content-Type": "application/json",
        "Notion-Version": "2022-06-28",
    }

    children = [
        {
            "object": "block", "type": "callout",
            "callout": {
                "rich_text": [{"type": "text", "text": {"content":
                    f"AI가 실무 영향력(운임·비용 / 공급망 리스크 / 규제 파급력 / 시급성)을 평가해 중요도순으로 배치했습니다. (작성일: {today_str})"}}],
                "icon": {"emoji": "🚢"},
            },
        },
        {"object": "block", "type": "divider", "divider": {}},
    ]

    for rank, art in enumerate(ranked_articles, 1):
        stars = "★" * art["score"] + "☆" * (5 - art["score"])

        children.append({
            "object": "block", "type": "heading_3",
            "heading_3": {"rich_text": [
                {"type": "text", "text": {"content": f"{rank}위 {stars} [{art['category']}] "}},
                {"type": "text", "text": {"content": art["title"], "link": {"url": art["link"]}}},
            ]},
        })
        if art["reason"]:
            children.append({
                "object": "block", "type": "paragraph",
                "paragraph": {"rich_text": [
                    {"type": "text", "text": {"content": f"🔎 선정 이유: {art['reason']}"},
                     "annotations": {"italic": True, "color": "gray"}},
                ]},
            })
        children.append({
            "object": "block", "type": "bulleted_list_item",
            "bulleted_list_item": {"rich_text": [
                {"type": "text", "text": {"content": "📌 요약: "}, "annotations": {"bold": True}},
                {"type": "text", "text": {"content": art["summary"]}},
            ]},
        })
        children.append({
            "object": "block", "type": "bulleted_list_item",
            "bulleted_list_item": {"rich_text": [
                {"type": "text", "text": {"content": "💡 시사점: "}, "annotations": {"bold": True}},
                {"type": "text", "text": {"content": art["insight"]}},
            ]},
        })

    payload = {
        "parent": {"type": "page_id", "page_id": NOTION_PARENT_PAGE_ID},
        "properties": {"title": {"title": [{"type": "text", "text": {"content": page_title}}]}},
        "children": children,
    }

    res = requests.post(NOTION_API_URL, headers=headers, json=payload)
    if res.status_code == 200:
        print("✅ 성공: 노션 페이지 생성 완료!")
    else:
        print(f"❌ 실패: 노션 페이지 생성 오류 -> {res.text}")


# ──────────────────────────────────────────────
if __name__ == "__main__":
    print("1. KSG 및 물류 뉴스 수집 중...")
    articles = fetch_all_news()
    print(f"총 {len(articles)}개 기사 수집 완료.")

    if not articles:
        print("수집된 기사가 없습니다.")
        raise SystemExit(0)

    print("2. Alpha AI로 분류·요약·중요도 평가 중...")
    ai_analysis = analyze_with_alpha_ai(articles)
    if not ai_analysis:
        print("AI 분석 실패")
        raise SystemExit(1)

    print("3. 중요도순 재배치 중...")
    ranked = parse_ai_result(ai_analysis, articles)
    for r in ranked:
        print(f"  ★{r['score']} [{r['category']}] {r['title'][:40]}")

    print("4. 노션 페이지 생성 중...")
    create_notion_page(ranked)
