import os
import requests
import feedparser
from bs4 import BeautifulSoup
from datetime import datetime
import json
import urllib.parse
import re

# 구글 뉴스 디코더 불러오기
try:
    from googlenewsdecoder import new_decoderv1
    HAS_GND = True
except ImportError:
    HAS_GND = False

# --- 설정 (Environment Variables) ---
ALPHA_API_KEY = os.getenv("ALPHA_API_KEY")
NOTION_TOKEN = os.getenv("NOTION_TOKEN")
DATABASE_ID = os.getenv("NOTION_PARENT_PAGE_ID")

RSS_URLS = {
    "해운·항만": "https://news.google.com/rss/search?q=" + urllib.parse.quote("해운 항만 물류 -쇼핑"),
    "항공·물류": "https://news.google.com/rss/search?q=" + urllib.parse.quote("항공 화물 물류"),
    "종합·공급망": "https://news.google.com/rss/search?q=" + urllib.parse.quote("공급망 리스크 물류")
}

def decode_google_news_url(url):
    """구글 뉴스 RSS 링크를 실제 언론사 원문 URL로 변환"""
    if HAS_GND and "news.google.com" in url:
        try:
            decoded = new_decoderv1(url, interval=1)
            if decoded and isinstance(decoded, dict) and decoded.get("decoded_url"):
                return decoded["decoded_url"]
        except Exception as e:
            print(f"구글 URL 디코딩 오류: {e}", flush=True)
    return url

def extract_article_details(entry):
    """뉴스 원문 URL 추적 및 본문 텍스트 수집"""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    rss_snippet = ""
    if hasattr(entry, 'summary'):
        rss_snippet = BeautifulSoup(entry.summary, 'html.parser').get_text().strip()

    real_url = decode_google_news_url(entry.link)
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
    except Exception as e:
        print(f"원문 본문 크롤링 실패 ({real_url}): {e}", flush=True)

    if not article_text or len(article_text) < 50:
        article_text = rss_snippet if rss_snippet else "원문 링크 참조 필요"

    return real_url, article_text

def get_ai_analysis(news_list):
    """PwC Alpha AI 모델을 이용하여 뉴스 요약/시사점/하이라이트 추출"""
    base_url = "https://workspace.kr.pwc.com/api/workspace/coding_agent/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {ALPHA_API_KEY}",
        "Content-Type": "application/json"
    }
    
    models_to_try = [
        "Gemini_3.1_Pro",
        "gemini-3.1-pro-preview",
        "Default",
        "gemini-3.7-flash",
        "Processing",
        "Coding",
        "GPT_5.6_Sol"
    ]

    articles_payload = ""
    for i, news in enumerate(news_list):
        articles_payload += f"---기사 {i+1}---\n제목: {news['title']}\n본문: {news['content'][:800]}\n\n"

    prompt = f"""
당신은 물류/공급망 전문가입니다. 다음 기사들을 분석하여 정확히 지침대로 출력하세요.

[기사 목록]
{articles_payload}

[응답 양식]
각 기사마다 반드시 아래 형식을 엄수하세요:
---ARTICLE---
번호: (숫자)
분류: (해운·항만, 항공·물류, 친환경·기술, 정책·규제, 종합·공급망 중 택1)
중요도: (★~★★★)
요약: (기사 내용을 2문장으로 상세히 요약)
시사점: (물류 실무자 관점 영향 1문장)
하이라이트:
- (핵심 요약 포인트 1)
- (핵심 요약 포인트 2)
- (핵심 요약 포인트 3)
---END---
"""

    for model_name in models_to_try:
        payload = {
            "model": model_name,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.3
        }
        
        try:
            print(f"[{model_name}] 모델로 AI 분석 시도 중...", flush=True)
            response = requests.post(base_url, headers=headers, json=payload, timeout=30)
            if response.status_code == 200:
                result = response.json()
                if 'choices' in result and len(result['choices']) > 0:
                    print(f"SUCCESS: [{model_name}] 모델로 AI 분석 성공!", flush=True)
                    content = result['choices'][0]['message']['content']
                    # 마크다운 코드 블록 제거
                    content = re.sub(r'```[a-zA-Z]*', '', content).replace('```', '').strip()
                    return content
            
            print(f"FAIL: [{model_name}] 응답 코드 {response.status_code}", flush=True)
        except Exception as e:
            print(f"ERROR: [{model_name}] 요청 오류 - {e}", flush=True)

    return None

def add_to_notion(title, url, category, summary, implication, highlights, stars, article_content):
    """노션 데이터베이스 행 및 페이지 본문 생성 (Notion API 정규 포맷 준수)"""
    notion_url = "https://api.notion.com/v1/pages"
    headers = {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Content-Type": "application/json",
        "Notion-Version": "2022-06-28"
    }
    
    # 노션 API 규격에 맞게 rich_text에 "type": "text" 명시
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
    
    for hl in highlights:
        if hl.strip():
            children_blocks.append({
                "object": "block",
                "type": "bulleted_list_item",
                "bulleted_list_item": {"rich_text": [{"type": "text", "text": {"content": hl}}]}
            })

    children_blocks.extend([
        {"object": "block", "type": "divider", "divider": {}},
        {"object": "block", "type": "heading_3", "heading_3": {"rich_text": [{"type": "text", "text": {"content": "📄 수집된 원문 내용"}}]} },
        {"object": "block", "type": "paragraph", "paragraph": {"rich_text": [{"type": "text", "text": {"content": article_content[:1500]}}]}}
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
    
    res = requests.post(notion_url, headers=headers, json=payload)
    if res.status_code == 200:
        print(f"✅ 노션 등록 성공: {title[:25]}...", flush=True)
    else:
        print(f"❌ 노션 등록 실패 ({res.status_code}): {res.text}", flush=True)

def main():
    if not ALPHA_API_KEY or not NOTION_TOKEN or not DATABASE_ID:
        print("환경 변수(ALPHA_API_KEY, NOTION_TOKEN, NOTION_PARENT_PAGE_ID) 설정을 확인하세요.", flush=True)
        return

    all_news = []
    for category, rss_url in RSS_URLS.items():
        feed = feedparser.parse(rss_url)
        for entry in feed.entries[:3]:
            real_url, content = extract_article_details(entry)
            all_news.append({
                "title": entry.title,
                "link": real_url,
                "category": category,
                "content": content
            })

    if not all_news:
        print("수집된 뉴스가 없습니다.", flush=True)
        return

    analysis_result = get_ai_analysis(all_news)
    if not analysis_result:
        print("AI 분석 결과를 가져오지 못했습니다.", flush=True)
        return
    
    articles = analysis_result.split("---ARTICLE---")
    saved_count = 0
    for art in articles:
        if "번호:" not in art: continue
        try:
            lines = [l.strip() for l in art.split("\n") if l.strip()]
            data = {"하이라이트": []}
            is_hl = False
            for line in lines:
                if line.startswith("번호:"):
                    data["번호"] = int(re.sub(r'[^0-9]', '', line))
                elif line.startswith("분류:"):
                    data["분류"] = line.split(":", 1)[1].strip()
                elif line.startswith("중요도:"):
                    data["중요도"] = line.split(":", 1)[1].strip()
                elif line.startswith("요약:"):
                    data["요약"] = line.split(":", 1)[1].strip()
                    is_hl = False
                elif line.startswith("시사점:"):
                    data["시사점"] = line.split(":", 1)[1].strip()
                    is_hl = False
                elif line.startswith("하이라이트:"):
                    is_hl = True
                elif is_hl and line.startswith("-"):
                    data["하이라이트"].append(line.lstrip("- ").strip())

            if "번호" in data and 1 <= data["번호"] <= len(all_news):
                orig = all_news[data["번호"] - 1]
                add_to_notion(
                    orig['title'],
                    orig['link'],
                    data.get("분류", "종합·공급망"),
                    data.get("요약", ""),
                    data.get("시사점", ""),
                    data["하이라이트"] if data["하이라이트"] else ["본문 내용 참조"],
                    data.get("중요도", "★"),
                    orig['content']
                )
                saved_count += 1
        except Exception as e:
            print(f"파싱/저장 중 오류: {e}", flush=True)

    print(f"총 {saved_count}개 기사 처리 완료.", flush=True)

if __name__ == "__main__":
    main()
