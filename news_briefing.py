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

RSS_URLS = {
    "해운·항만": "https://news.google.com/rss/search?q=" + urllib.parse.quote("해운 항만 물류 -쇼핑"),
    "항공·물류": "https://news.google.com/rss/search?q=" + urllib.parse.quote("항공 화물 물류"),
    "종합·공급망": "https://news.google.com/rss/search?q=" + urllib.parse.quote("공급망 리스크 물류")
}

def extract_article_details(entry):
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
    rss_snippet = ""
    if hasattr(entry, 'summary'):
        rss_snippet = BeautifulSoup(entry.summary, 'html.parser').get_text().strip()

    real_url = entry.link
    article_text = ""
    try:
        res = requests.get(real_url, headers=headers, timeout=10, allow_redirects=True)
        res.encoding = res.apparent_encoding or 'utf-8'
        soup = BeautifulSoup(res.text, 'html.parser')
        
        for tag in soup(["script", "style", "header", "footer", "nav", "aside", "form"]):
            tag.decompose()
            
        main_content = soup.find('article') or soup.find('main') or soup.find('div', class_=re.compile(r'(article|content|post|view)', re.I))
        target = main_content if main_content else soup
        paragraphs = [p.get_text().strip() for p in target.find_all('p') if len(p.get_text().strip()) > 30]
        article_text = "\n".join(paragraphs)
    except:
        pass

    if not article_text or len(article_text) < 100:
        article_text = rss_snippet if rss_snippet else "원문 확인 필요"

    return real_url, article_text

def get_ai_analysis(news_list):
    url = "https://workspace.kr.pwc.com/api/workspace/coding_agent/v1/chat/completions"
    headers = {"Authorization": f"Bearer {ALPHA_API_KEY}", "Content-Type": "application/json"}
    
    # 입력 데이터가 너무 크면 API 오류가 발생하므로 800자로 제한
    articles_payload = ""
    for i, news in enumerate(news_list):
        articles_payload += f"---기사 {i+1}---\n제목: {news['title']}\n본문: {news['content'][:800]}\n\n"

    prompt = f"""
당신은 물류 전문가입니다. 다음 기사들을 분석하여 '요약', '시사점', '하이라이트'를 추출하세요.

[기사 목록]
{articles_payload}

[응답 양식]
각 기사마다 반드시 아래 형식을 지키세요:
---ARTICLE---
번호: (숫자)
분류: (해운·항만, 항공·물류, 친환경·기술, 정책·규제, 종합·공급망 중 택1)
중요도: (★~★★★)
요약: (기사 내용을 2문장으로 상세히 요약)
시사점: (물류 실무에 주는 영향 1문장)
하이라이트:
- (핵심 요약 포인트 1)
- (핵심 요약 포인트 2)
- (핵심 요약 포인트 3)
---END---
"""

    payload = {
        "model": "gpt-4o",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3
    }
    
    response = requests.post(url, headers=headers, json=payload)
    
    # 에러 핸들링 강화
    if response.status_code != 200:
        print(f"API Error: {response.status_code} - {response.text}")
        return None

    result = response.json()
    if 'choices' not in result:
        print(f"Unexpected Response Format: {result}")
        return None

    return result['choices'][0]['message']['content']

def add_to_notion(title, url, category, summary, implication, highlights, stars, article_content):
    notion_url = "https://api.notion.com/v1/pages"
    headers = {"Authorization": f"Bearer {NOTION_TOKEN}", "Content-Type": "application/json", "Notion-Version": "2022-06-28"}
    
    children_blocks = [
        {"object": "block", "type": "callout", "callout": {"rich_text": [{"text": {"content": f"💡 AI 시사점\n{implication}"}}], "icon": {"emoji": "💡"}, "color": "blue_background"}},
        {"object": "block", "type": "heading_3", "heading_3": {"rich_text": [{"text": {"content": "📌 핵심 하이라이트"}}]}}
    ]
    for hl in highlights:
        children_blocks.append({"object": "block", "type": "bulleted_list_item", "bulleted_list_item": {"rich_text": [{"text": {"content": hl}}]}})

    children_blocks.extend([
        {"object": "block", "type": "divider", "divider": {}},
        {"object": "block", "type": "heading_3", "heading_3": {"rich_text": [{"text": {"content": "📄 수집된 본문내용"}}]}},
        {"object": "block", "type": "paragraph", "paragraph": {"rich_text": [{"text": {"content": article_content[:1500]}}]}}
    ])

    payload = {
        "parent": {"database_id": DATABASE_ID},
        "properties": {
            "제목": {"title": [{"text": {"content": f"[{stars}] {title}"}}]},
            "날짜": {"date": {"start": datetime.now().strftime("%Y-%m-%d")}},
            "분류": {"select": {"name": category}},
            "요약": {"rich_text": [{"text": {"content": summary}}]},
            "URL": {"url": url}
        },
        "children": children_blocks
    }
    requests.post(notion_url, headers=headers, json=payload)

def main():
    if not ALPHA_API_KEY or not NOTION_TOKEN:
        print("API 토큰 설정이 누락되었습니다.")
        return

    all_news = []
    for category, rss_url in RSS_URLS.items():
        feed = feedparser.parse(rss_url)
        for entry in feed.entries[:3]:
            real_url, content = extract_article_details(entry)
            all_news.append({"title": entry.title, "link": real_url, "category": category, "content": content})

    if not all_news:
        print("수집된 뉴스가 없습니다.")
        return

    analysis_result = get_ai_analysis(all_news)
    if not analysis_result:
        print("AI 분석 결과를 가져오지 못했습니다.")
        return
    
    articles = analysis_result.split("---ARTICLE---")
    for art in articles:
        if "번호:" not in art: continue
        try:
            lines = [l.strip() for l in art.split("\n") if l.strip()]
            data = {"하이라이트": []}
            is_hl = False
            for line in lines:
                if line.startswith("번호:"): data["번호"] = int(re.sub(r'[^0-9]', '', line))
                elif line.startswith("분류:"): data["분류"] = line.split(":", 1)[1].strip()
                elif line.startswith("중요도:"): data["중요도"] = line.split(":", 1)[1].strip()
                elif line.startswith("요약:"): data["요약"] = line.split(":", 1)[1].strip(); is_hl = False
                elif line.startswith("시사점:"): data["시사점"] = line.split(":", 1)[1].strip(); is_hl = False
                elif line.startswith("하이라이트:"): is_hl = True
                elif is_hl and line.startswith("-"): data["하이라이트"].append(line.lstrip("- ").strip())

            orig = all_news[data["번호"] - 1]
            add_to_notion(orig['title'], orig['link'], data.get("분류", "종합·공급망"), data.get("요약", ""), data.get("시사점", ""), data["하이라이트"], data.get("중요도", "★"), orig['content'])
        except Exception as e:
            print(f"처리 중 오류: {e}")

if __name__ == "__main__":
    main()
