import os
import requests
import feedparser
from bs4 import BeautifulSoup
from datetime import datetime
import json
import urllib.parse
import re

# --- 설정 (Environment Variables) ---
ALPHA_API_KEY = os.getenv("ALPHA_API_KEY")
NOTION_TOKEN = os.getenv("NOTION_TOKEN")
DATABASE_ID = os.getenv("NOTION_PARENT_PAGE_ID")

# --- 뉴스 수집 대상 ---
RSS_URLS = {
    "해운·항만": "https://news.google.com/rss/search?q=" + urllib.parse.quote("해운 항만 물류 -쇼핑"),
    "항공·물류": "https://news.google.com/rss/search?q=" + urllib.parse.quote("항공 화물 물류"),
    "종합·공급망": "https://news.google.com/rss/search?q=" + urllib.parse.quote("공급망 리스크 물류")
}

def extract_article_details(entry):
    """구글 RSS 링크에서 실제 언론사 원문 URL 추출 및 본문 수집"""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    }
    
    # 1. RSS 요약본 텍스트 미리 확보 (최후의 백업용)
    rss_snippet = ""
    if hasattr(entry, 'summary'):
        rss_soup = BeautifulSoup(entry.summary, 'html.parser')
        rss_snippet = rss_soup.get_text().strip()

    # 2. 구글 RSS 요약 내 실제 언론사 링크 추출 시도
    real_url = entry.link
    if hasattr(entry, 'summary'):
        rss_soup = BeautifulSoup(entry.summary, 'html.parser')
        a_tag = rss_soup.find('a')
        if a_tag and a_tag.get('href') and 'news.google.com' not in a_tag['href']:
            real_url = a_tag['href']

    article_text = ""
    try:
        res = requests.get(real_url, headers=headers, timeout=8, allow_redirects=True)
        res.encoding = res.apparent_encoding or 'utf-8'
        
        soup = BeautifulSoup(res.text, 'html.parser')
        
        # 불필요한 태그 제거
        for tag in soup(["script", "style", "header", "footer", "nav", "aside", "form"]):
            tag.decompose()
            
        # Meta description 수집
        meta_desc = ""
        meta_tag = soup.find('meta', property='og:description') or soup.find('meta', attrs={'name': 'description'})
        if meta_tag and meta_tag.get('content'):
            meta_desc = meta_tag['content'].strip()

        # 본문 영역 탐색
        main_content = soup.find('article') or soup.find('main') or soup.find('div', class_=re.compile(r'(article|content|post|view)', re.I))
        target_area = main_content if main_content else soup
        
        paragraphs = [p.get_text().strip() for p in target_area.find_all('p') if len(p.get_text().strip()) > 20]
        article_text = "\n\n".join(paragraphs)

        if not article_text and meta_desc:
            article_text = meta_desc
    except Exception:
        pass

    # 본문 추출 실패 시 RSS snippet으로 대체하여 '수집 실패' 방지
    if not article_text or len(article_text) < 50:
        article_text = rss_snippet if rss_snippet else "원문 텍스트 수집이 제한된 사이트입니다. 상단 URL 속성의 링크에서 직접 확인 가능합니다."

    return real_url, article_text

def get_ai_analysis(news_list):
    """PwC Alpha AI를 사용하여 요약, 시사점, 하이라이트 생성"""
    url = "https://workspace.kr.pwc.com/api/workspace/coding_agent/v1/chat/completions"
    headers = {"Authorization": f"Bearer {ALPHA_API_KEY}", "Content-Type": "application/json"}
    
    articles_payload = ""
    for i, news in enumerate(news_list):
        articles_payload += f"---기사 {i+1}---\n제목: {news['title']}\n분류: {news['category']}\n본문: {news['content'][:1200]}\n\n"

    prompt = f"""
당신은 물류/해운/공급망 전문가입니다. 아래 수집된 기사들을 분석하여 정확히 요청된 양식대로 응답하세요.

[수집된 기사]
{articles_payload}

[응답 작성 지침]
각 기사마다 아래 구조를 완벽히 준수하여 출력하세요:

---ARTICLE---
번호: (1부터 시작하는 숫자)
분류: (해운·항만, 항공·물류, 친환경·기술, 정책·규제, 종합·공급망 중 가장 적합한 것 1개 선택)
중요도: (★, ★★, ★★★ 중 선택)
노션요약: (데이터베이스 속성용 1문장 핵심 요약)
시사점: (물류 실무자 입장의 시사점/영향 1~2문장)
하이라이트:
- (핵심 하이라이트 내용 1)
- (핵심 하이라이트 내용 2)
- (핵심 하이라이트 내용 3)
---END---
"""

    payload = {
        "model": "gpt-4o",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.2
    }
    
    response = requests.post(url, headers=headers, json=payload)
    return response.json()['choices'][0]['message']['content']

def add_to_notion(title, url, category, summary, implication, highlights, stars, article_content):
    """노션 데이터베이스에 항목 추가 및 상세 본문(하이라이트 포함) 구성"""
    notion_url = "https://api.notion.com/v1/pages"
    headers = {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Content-Type": "application/json",
        "Notion-Version": "2022-06-28"
    }
    
    date_str = datetime.now().strftime("%Y-%m-%d")
    clean_content = article_content[:1500]

    # 본문 블록 구성 (콜아웃 + 하이라이트 + 원문요약)
    children_blocks = [
        {
            "object": "block",
            "type": "callout",
            "callout": {
                "rich_text": [{"type": "text", "text": {"content": f"💡 AI 시사점\n{implication}"}}],
                "icon": {"emoji": "💡"},
                "color": "yellow_background"
            }
        },
        {
            "object": "block",
            "type": "heading_3",
            "heading_3": {"rich_text": [{"type": "text", "text": {"content": "📌 핵심 하이라이트"}}]}
        }
    ]

    # 하이라이트 불렛 포인트 추가
    for hl in highlights:
        children_blocks.append({
            "object": "block",
            "type": "bulleted_list_item",
            "bulleted_list_item": {
                "rich_text": [{"type": "text", "text": {"content": hl}}]
            }
        })

    # 구분선 및 원문 본문 추가
    children_blocks.extend([
        {
            "object": "block",
            "type": "divider",
            "divider": {}
        },
        {
            "object": "block",
            "type": "heading_3",
            "heading_3": {"rich_text": [{"type": "text", "text": {"content": "📄 기사 본문 내용"}}]}
        },
        {
            "object": "block",
            "type": "paragraph",
            "paragraph": {
                "rich_text": [{"type": "text", "text": {"content": clean_content}}]
            }
        }
    ])

    payload = {
        "parent": {"database_id": DATABASE_ID},
        "properties": {
            "제목": {"title": [{"text": {"content": f"[{stars}] {title}"}}]},
            "날짜": {"date": {"start": date_str}},
            "분류": {"select": {"name": category}},
            "요약": {"rich_text": [{"text": {"content": summary}}]},
            "URL": {"url": url}
        },
        "children": children_blocks
    }
    
    requests.post(notion_url, headers=headers, json=payload)

def main():
    all_news = []
    for category, rss_url in RSS_URLS.items():
        feed = feedparser.parse(rss_url)
        for entry in feed.entries[:3]: # 각 분야별 상위 3개 수집
            real_url, content = extract_article_details(entry)
            all_news.append({
                "title": entry.title,
                "link": real_url,
                "category": category,
                "content": content
            })

    analysis_result = get_ai_analysis(all_news)
    
    # AI 결과 파싱
    articles = analysis_result.split("---ARTICLE---")
    for art in articles:
        if not art.strip(): continue
        try:
            lines = [line.strip() for line in art.split("\n") if line.strip()]
            num = 0
            category = "종합·공급망"
            stars = "★"
            summary = ""
            implication = ""
            highlights = []
            
            is_hl = False
            for line in lines:
                if line.startswith("번호:"):
                    num = int(re.sub(r'[^0-9]', '', line))
                elif line.startswith("분류:"):
                    category = line.replace("분류:", "").strip()
                elif line.startswith("중요도:"):
                    stars = line.replace("중요도:", "").strip()
                elif line.startswith("노션요약:"):
                    summary = line.replace("노션요약:", "").strip()
                    is_hl = False
                elif line.startswith("시사점:"):
                    implication = line.replace("시사점:", "").strip()
                    is_hl = False
                elif line.startswith("하이라이트:"):
                    is_hl = True
                elif is_hl and line.startswith("-"):
                    highlights.append(line.lstrip("- ").strip())

            if num > 0 and num <= len(all_news):
                original = all_news[num - 1]
                add_to_notion(
                    original['title'],
                    original['link'],
                    category,
                    summary,
                    implication,
                    highlights if highlights else ["본문 주요 내용 요약 참조"],
                    stars,
                    original['content']
                )
        except Exception as e:
            print(f"파싱 및 저장 중 오류 발생: {e}")

if __name__ == "__main__":
    main()
