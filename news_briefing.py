import os
import requests
import feedparser
from bs4 import BeautifulSoup
from datetime import datetime
import json
import urllib.parse

# --- 설정 (Environment Variables) ---
ALPHA_API_KEY = os.getenv("ALPHA_API_KEY")
NOTION_TOKEN = os.getenv("NOTION_TOKEN")
DATABASE_ID = os.getenv("NOTION_PARENT_PAGE_ID")

# --- 뉴스 수집 대상 ---
RSS_URLS = {
    "해운/항만": "https://news.google.com/rss/search?q=" + urllib.parse.quote("해운 항만 물류 -쇼핑"),
    "항공/물류": "https://news.google.com/rss/search?q=" + urllib.parse.quote("항공 화물 물류"),
    "종합/공급망": "https://news.google.com/rss/search?q=" + urllib.parse.quote("공급망 리스크 물류")
}

def fetch_article_content(url):
    """뉴스 URL에서 본문 텍스트를 추출하는 강화된 함수"""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
    }
    try:
        response = requests.get(url, headers=headers, timeout=10)
        response.encoding = 'utf-8' # 인코딩 강제 설정
        
        if response.status_code != 200:
            return "본문 수집 불가 (접근 차단)"

        soup = BeautifulSoup(response.text, 'html.parser')
        
        # 기사 본문이 주로 위치하는 태그들 탐색
        article_body = ""
        # 1. 일반적인 기사 영역 클래스들 시도
        content_area = soup.find('div', class_=['article_body', 'view_content', 'article-body', 'entry-content', 'post-content'])
        
        if content_area:
            paragraphs = content_area.find_all('p')
        else:
            # 2. 클래스가 없을 경우 모든 p 태그 수집
            paragraphs = soup.find_all('p')
            
        text_list = [p.get_text().strip() for p in paragraphs if len(p.get_text().strip()) > 20]
        article_body = "\n".join(text_list)

        return article_body[:2000] if article_body else "본문 자동 수집 실패. 상단 URL 속성의 링크에서 원문 확인 필요"
    except Exception as e:
        return f"본문 수집 중 오류 발생: {str(e)}"

def get_ai_analysis(news_list):
    """PwC Alpha AI를 사용하여 뉴스 분석"""
    url = "https://workspace.kr.pwc.com/api/workspace/coding_agent/v1/chat/completions"
    headers = {"Authorization": f"Bearer {ALPHA_API_KEY}", "Content-Type": "application/json"}
    
    prompt = "당신은 물류/공급망 전문가입니다. 다음 뉴스들을 분석하여 형식에 맞춰 답변하세요.\n\n"
    for i, news in enumerate(news_list):
        prompt += f"번호: {i+1}\n제목: {news['title']}\n카테고리: {news['category']}\n본문내용: {news['content'][:500]}\n\n"
    
    prompt += """
    [응답 형식]
    각 뉴스별로 아래 항목을 작성하세요:
    번호: N
    분류: (해운, 항공, 육상, 공급망 중 택1)
    요약: (2문장 내외)
    시사점: (실무적 영향 1문장)
    중요도: (★~★★★)
    """

    payload = {
        "model": "gpt-4o",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.2
    }
    
    response = requests.post(url, headers=headers, json=payload)
    return response.json()['choices'][0]['message']['content']

def add_to_notion(title, url, category, summary, implication, stars, article_content):
    """노션 데이터베이스에 개별 항목 추가 (본문 포함)"""
    notion_url = "https://api.notion.com/v1/pages"
    headers = {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Content-Type": "application/json",
        "Notion-Version": "2022-06-28"
    }
    
    date_str = datetime.now().strftime("%Y-%m-%d")
    
    # 본문 텍스트가 너무 길면 노션 API에서 오류가 날 수 있으므로 분할 또는 제한
    clean_content = article_content[:1500] 

    payload = {
        "parent": {"database_id": DATABASE_ID},
        "properties": {
            "제목": {"title": [{"text": {"content": f"[{stars}] {title}"}}]},
            "날짜": {"date": {"start": date_str}},
            "분류": {"select": {"name": category}},
            "요약": {"rich_text": [{"text": {"content": summary}}]},
            "URL": {"url": url}
        },
        "children": [
            {
                "object": "block",
                "type": "callout",
                "callout": {
                    "rich_text": [{"text": {"content": f"💡 AI 시사점\n{implication}"}}],
                    "icon": {"emoji": "💡"},
                    "color": "light_gray_background"
                }
            },
            {
                "object": "block",
                "type": "heading_3",
                "heading_3": {"rich_text": [{"text": {"content": "원문 기사 본문"}}] }
            },
            {
                "object": "block",
                "type": "paragraph",
                "paragraph": {
                    "rich_text": [{"text": {"content": clean_content}}],
                    "color": "default"
                }
            }
        ]
    }
    requests.post(notion_url, headers=headers, json=payload)

def main():
    all_news = []
    for category, url in RSS_URLS.items():
        feed = feedparser.parse(url)
        for entry in feed.entries[:3]:
            content = fetch_article_content(entry.link)
            all_news.append({
                "title": entry.title,
                "link": entry.link,
                "category": category,
                "content": content
            })

    analysis_result = get_ai_analysis(all_news)
    
    # AI 응답 파싱 및 노션 저장
    articles = analysis_result.split("번호:")
    for art in articles:
        if not art.strip(): continue
        try:
            lines = art.strip().split("\n")
            data = {}
            for line in lines:
                if ":" in line:
                    key, val = line.split(":", 1)
                    data[key.strip()] = val.strip()
            
            idx = int(data.get("번호", 0)) - 1
            if idx >= 0:
                original = all_news[idx]
                add_to_notion(
                    original['title'], 
                    original['link'], 
                    data.get("분류", "종합/공급망"),
                    data.get("요약", ""),
                    data.get("시사점", ""),
                    data.get("중요도", "★"),
                    original['content']
                )
        except:
            continue

if __name__ == "__main__":
    main()
