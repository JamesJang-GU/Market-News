import os
import datetime
import requests
import feedparser

# 1. 환경 변수 불러오기 (GitHub Secrets)
ALPHA_API_KEY = os.environ.get("ALPHA_API_KEY")
NOTION_TOKEN = os.environ.get("NOTION_TOKEN")
NOTION_PARENT_PAGE_ID = os.environ.get("NOTION_PARENT_PAGE_ID")

# 설정
ALPHA_URL = "https://workspace.kr.pwc.com/api/workspace/coding_agent/v1/chat/completions"
NOTION_API_URL = "https://api.notion.com/v1/pages"

def fetch_logistics_news():
    """구글 뉴스 RSS에서 해운/물류 관련 최신 뉴스 10개 수집"""
    # 검색어: 해운 OR 물류 OR 항만
    rss_url = "https://news.google.com/rss/search?q=%ED%95%B4%EC%9A%B4%20OR%20%EB%AC%BC%EB%A5%98%20OR%20%ED%95%AD%EB%A7%8C&hl=ko&gl=KR&ceid=KR:ko"
    feed = feedparser.parse(rss_url)
    
    articles = []
    for entry in feed.entries[:10]: # 최대 10개
        articles.append({
            "title": entry.title,
            "link": entry.link,
            "published": getattr(entry, 'published', '날짜 미상')
        })
    return articles

def analyze_with_alpha_ai(articles):
    """PwC Alpha AI API를 이용해 기사 요약, 시사점, 카테고리 추출"""
    articles_text = ""
    for idx, item in enumerate(articles, 1):
        articles_text += f"[{idx}] 제목: {item['title']}\n링크: {item['link']}\n\n"

    system_prompt = """
    당신은 해운·물류 산업 전문 리서치 분석가입니다.
    제공된 뉴스 목록(최대 10개)을 바탕으로 각 기사별로 다음 양식으로 분석해 주세요.

    [카테고리] (해운 운임 / 항만·터미널 / 물류·공급망 / 선사·기업 / 규제·정책 / 친환경·기술 / 기타 중 하나 선택)
    [요약] 기사 제목과 배경을 기반으로 핵심 내용 2~3문장 요약
    [시사점] 해당 뉴스가 해운/물류 업계 또는 화주에 미칠 영향이나 시사점 1~2문장

    응답은 아래 구분자를 명확히 지켜서 출력해 주세요:
    ---ARTICLE_START---
    번호: [번호]
    카테고리: [카테고리]
    요약: [요약 내용]
    시사점: [시사점 내용]
    ---ARTICLE_END---
    """

    headers = {
        "Authorization": f"Bearer {ALPHA_API_KEY}",
        "Content-Type": "application/json"
    }

    payload = {
        "model": "Default", # 이미지의 추천 기본 모델 (Gemini 3.7 Flash 호환)
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"다음 뉴스들을 분석해 주세요:\n\n{articles_text}"}
        ]
    }

    try:
        response = requests.post(ALPHA_URL, headers=headers, json=payload)
        response.raise_for_status()
        result = response.json()
        return result['choices'][0]['message']['content']
    except Exception as e:
        print(f"Alpha AI 호출 실패: {e}")
        return None

def create_notion_page(articles, ai_analysis):
    """노션 상위 페이지 하위에 오늘 날짜의 브리핑 페이지 생성"""
    today_str = datetime.datetime.now().strftime("%Y-%m-%d")
    page_title = f"🚢 [{today_str}] 해운·물류 뉴스 브리핑 (TOP 10)"

    headers = {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Content-Type": "application/json",
        "Notion-Version": "2022-06-28"
    }

    # 노션 블록(본문) 데이터 만들기
    children_blocks = [
        {
            "object": "block",
            "type": "callout",
            "callout": {
                "rich_text": [{"type": "text", "text": {"content": f"본 페이지는 매일 자동으로 생성되는 해운·물류 뉴스 요약본입니다. (작성일: {today_str})"}}],
                "icon": {"emoji": "💡"}
            }
        },
        {"object": "block", "type": "divider", "divider": {}}
    ]

    # AI 분석 결과를 파싱하여 노션 블록 생성
    parsed_articles = ai_analysis.split("---ARTICLE_START---")
    
    for idx, art_text in enumerate(parsed_articles[1:], 0):
        if idx >= len(articles): break
        orig_article = articles[idx]
        
        # 기본값 설정
        category = "기타"
        summary = "요약 정보 없음"
        insight = "시사점 정보 없음"

        for line in art_text.split("\n"):
            line = line.strip()
            if line.startswith("카테고리:"):
                category = line.replace("카테고리:", "").strip()
            elif line.startswith("요약:"):
                summary = line.replace("요약:", "").strip()
            elif line.startswith("시사점:"):
                insight = line.replace("시사점:", "").strip()

        # 기사 제목 + 링크 블록
        children_blocks.append({
            "object": "block",
            "type": "heading_3",
            "heading_3": {
                "rich_text": [
                    {"type": "text", "text": {"content": f"[{category}] "}},
                    {"type": "text", "text": {"content": orig_article['title'], "link": {"url": orig_article['link']}}}
                ]
            }
        })

        # 요약 및 시사점 리스트 블록
        children_blocks.append({
            "object": "block",
            "type": "bulleted_list_item",
            "bulleted_list_item": {
                "rich_text": [
                    {"type": "text", "text": {"content": "📌 요약: ", "annotations": {"bold": True}}},
                    {"type": "text", "text": {"content": summary}}
                ]
            }
        })
        children_blocks.append({
            "object": "block",
            "type": "bulleted_list_item",
            "bulleted_list_item": {
                "rich_text": [
                    {"type": "text", "text": {"content": "💡 시사점: ", "annotations": {"bold": True}}},
                    {"type": "text", "text": {"content": insight}}
                ]
            }
        })

    # 노션 API 요청 데이터
    payload = {
        "parent": {"type": "page_id", "page_id": NOTION_PARENT_PAGE_ID},
        "properties": {
            "title": {
                "title": [{"type": "text", "text": {"content": page_title}}]
            }
        },
        "children": children_blocks
    }

    res = requests.post(NOTION_API_URL, headers=headers, json=payload)
    if res.status_code == 200:
        print("✅ 성공: 노션 페이지 생성 완료!")
    else:
        print(f"❌ 실패: 노션 페이지 생성 중 오류 발생 -> {res.text}")

if __name__ == "__main__":
    print("1. 기사 수집 중...")
    articles = fetch_logistics_news()
    print(f"총 {len(articles)}개 기사 수집 완료.")

    if articles:
        print("2. Alpha AI로 요약 및 분석 중...")
        ai_analysis = analyze_with_alpha_ai(articles)
        
        if ai_analysis:
            print("3. 노션 페이지 생성 중...")
            create_notion_page(articles, ai_analysis)
        else:
            print("AI 분석 결과를 가져오지 못했습니다.")
