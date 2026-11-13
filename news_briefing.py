import os
import datetime
import requests
import feedparser
from bs4 import BeautifulSoup
import urllib.parse

# 1. 환경 변수 불러오기 (GitHub Secrets)
ALPHA_API_KEY = os.environ.get("ALPHA_API_KEY")
NOTION_TOKEN = os.environ.get("NOTION_TOKEN")
NOTION_PARENT_PAGE_ID = os.environ.get("NOTION_PARENT_PAGE_ID")  # 노션 DB ID

# 설정
ALPHA_URL = "https://workspace.kr.pwc.com/api/workspace/coding_agent/v1/chat/completions"
NOTION_API_URL = "https://api.notion.com/v1/pages"

def fetch_ksg_news():
    """코리아쉬핑가제트(KSG) 메인 뉴스 크롤링"""
    url = "https://ksg.co.kr/news/main_news.jsp"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
    articles = []
    try:
        res = requests.get(url, headers=headers, timeout=10)
        res.encoding = res.apparent_encoding
        
        if res.status_code == 200:
            soup = BeautifulSoup(res.text, 'html.parser')
            links = soup.find_all('a', href=True)
            seen_links = set()
            
            for a in links:
                href = a['href']
                title = a.get_text(strip=True)
                
                if 'news_view.jsp' in href and title and len(title) > 12:
                    if href.startswith('http'):
                        full_url = href
                    elif href.startswith('/'):
                        full_url = f"https://ksg.co.kr{href}"
                    else:
                        full_url = f"https://ksg.co.kr/news/{href}"
                        
                    if full_url not in seen_links:
                        seen_links.add(full_url)
                        articles.append({
                            "title": title,
                            "link": full_url,
                            "published": "최신 (KSG)"
                        })
                        if len(articles) >= 5:
                            break
    except Exception as e:
        print(f"⚠️ KSG 크롤링 실패: {e}")
        
    return articles

def fetch_google_logistics_news(count=5):
    """구글 뉴스 RSS에서 종합 물류 뉴스 수집"""
    query = "항공물류 OR 화물운송 OR 물류센터 OR 공급망 OR 라스트마일"
    encoded_query = urllib.parse.quote(query) 
    rss_url = f"https://news.google.com/rss/search?q={encoded_query}&hl=ko&gl=KR&ceid=KR:ko"
    
    feed = feedparser.parse(rss_url)
    articles = []
    for entry in feed.entries[:count]:
        articles.append({
            "title": entry.title,
            "link": entry.link,
            "published": getattr(entry, 'published', '날짜 미상')
        })
    return articles

def fetch_all_news():
    ksg_news = fetch_ksg_news()
    google_count = 10 - len(ksg_news)
    google_news = fetch_google_logistics_news(count=google_count)
    return (ksg_news + google_news)[:10]

def analyze_with_alpha_ai(articles):
    articles_text = ""
    for idx, item in enumerate(articles, 1):
        articles_text += f"[{idx}] 제목: {item['title']}\n링크: {item['link']}\n\n"

    system_prompt = """
    당신은 종합 물류 및 공급망(SCM) 산업 전문 리서치 분석가입니다.
    제공된 뉴스 목록(최대 10개)을 분석하여, 실무자 관점에서 영향력이 큰 순서대로 정렬해 주세요.

    [카테고리 분류 지침]
    아래 8개 카테고리 중 가장 적합한 하나를 선택합니다:
    - 해운 / 항공 / 육상 / 철도 / 창고·물류센터 / 규제·정책 / 친환경·기술 / 종합·공급망

    [중요도 판단 기준]
    - 5점/4점: 공급망 리스크, 규제 변경, 대형 선사/화주 계약, 즉시 대응 필요 사안
    - 3점/2점: 업계 동향, 신기술 도입, 중견 기업 실적 등
    - 1점: 일반 홍보성, 참고 수준의 단신

    [출력 정렬 규칙]
    - 실무 영향력이 가장 큰 기사를 1번으로 배치하여 내림차순 정렬합니다.
    - 각 기사에는 반드시 원본 기사 번호를 번호: N 형식으로 명시합니다.

    [출력 형식 예시]
    ---ARTICLE_START---
    번호: 3
    순위: 1
    중요도: 5
    카테고리: 해운
    요약: [2~3문장 핵심 요약]
    시사점: [업계/화주/공급망에 미칠 영향 1~2문장]
    ---ARTICLE_END---
    """

    headers = {
        "Authorization": f"Bearer {ALPHA_API_KEY}",
        "Content-Type": "application/json"
    }

    payload = {
        "model": "Default",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"다음 뉴스들을 분석해 주세요:\n\n{articles_text}"}
        ]
    }

    try:
        response = requests.post(ALPHA_URL, headers=headers, json=payload)
        response.raise_for_status()
        return response.json()['choices'][0]['message']['content']
    except Exception as e:
        print(f"Alpha AI 호출 실패: {e}")
        return None

def save_articles_to_notion_db(articles, ai_analysis):
    """기사 1개당 노션 DB의 1개 행(Row)으로 적재"""
    today_str = datetime.datetime.now().strftime("%Y-%m-%d")

    headers = {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Content-Type": "application/json",
        "Notion-Version": "2022-06-28"
    }

    parsed_articles = ai_analysis.split("---ARTICLE_START---")
    success_count = 0

    for art_text in parsed_articles[1:]:
        parsed = {}
        for line in art_text.split("\n"):
            line = line.strip()
            for key in ["번호", "순위", "중요도", "카테고리", "요약", "시사점"]:
                if line.startswith(f"{key}:"):
                    parsed[key] = line.replace(f"{key}:", "").strip()

        try:
            orig_idx = int(parsed.get("번호", "")) - 1
            if orig_idx < 0 or orig_idx >= len(articles):
                continue
            orig_article = articles[orig_idx]
        except (ValueError, TypeError):
            continue

        importance = parsed.get("중요도", "3")
        category = parsed.get("카테고리", "종합·공급망")
        summary = parsed.get("요약", "요약 정보 없음")
        insight = parsed.get("시사점", "시사점 정보 없음")

        # 별점 가공 (예: 중요도 4점 -> ★★★★)
        stars = "★" * int(importance) if importance.isdigit() else "★3"
        title_text = f"[{stars}] {orig_article['title']}"

        # 페이지 본문 (클릭해서 들어갔을 때 보일 내용)
        children_blocks = [
            {
                "object": "block",
                "type": "callout",
                "callout": {
                    "rich_text": [{"type": "text", "text": {"content": "💡 AI 시사점 분석"}}],
                    "icon": {"emoji": "💡"}
                }
            },
            {
                "object": "block",
                "type": "paragraph",
                "paragraph": {
                    "rich_text": [{"type": "text", "text": {"content": insight}}]
                }
            }
        ]

        # ★ 
