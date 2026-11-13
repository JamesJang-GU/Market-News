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

# 노션 허용 카테고리 (8개로 엄격 제한)
ALLOWED_CATEGORIES = [
    "해상", "항공", "육상", "철도", "창고", "글로벌 공급망", "친환경&기술", "규제&정책"
]

# AI가 다른 단어를 출력하더라도 지정된 8개 카테고리로 자동 매핑
CATEGORY_MAPPER = {
    "해운": "해상", "항만": "해상", "해운·항만": "해상", "해상": "해상",
    "항공": "항공", "항공·물류": "항공",
    "육상": "육상", "육상·창고": "육상",
    "철도": "철도",
    "창고": "창고", "창고·물류센터": "창고",
    "글로벌 공급망": "글로벌 공급망", "종합·공급망": "글로벌 공급망", "공급망": "글로벌 공급망",
    "친환경&기술": "친환경&기술", "친환경·기술": "친환경&기술",
    "규제&정책": "규제&정책", "규제·정책": "규제&정책", "정책·규제": "규제&정책"
}

# 우선순위별 RSS 수집 대상
RSS_GROUPS = {
    "group1": {  # 1순위: 해상/항공 (합산 최대 3개)
        "해상": "https://news.google.com/rss/search?q=" + urllib.parse.quote("해운 항만 해상물류 -쇼핑"),
        "항공": "https://news.google.com/rss/search?q=" + urllib.parse.quote("항공 화물 항공사 -쇼핑")
    },
    "group2": {  # 2순위: 육상/철도/창고
        "육상": "https://news.google.com/rss/search?q=" + urllib.parse.quote("육상 운송 화물차 트럭"),
        "철도": "https://news.google.com/rss/search?q=" + urllib.parse.quote("철도 물류 화물열차"),
        "창고": "https://news.google.com/rss/search?q=" + urllib.parse.quote("물류창고 창고 풀필먼트")
    },
    "group3": {  # 3순위: 공급망/기술/정책
        "글로벌 공급망": "https://news.google.com/rss/search?q=" + urllib.parse.quote("글로벌 공급망 리스크 수입 수출"),
        "친환경&기술": "https://news.google.com/rss/search?q=" + urllib.parse.quote("친환경 물류 자율주행 로봇 스마트물류"),
        "규제&정책": "https://news.google.com/rss/search?q=" + urllib.parse.quote("물류 정책 규제 국토부 관세청")
    }
}

def clean_title(title):
    """기사 제목에서 별표 및 출처(- 언론사) 삭제하여 순수 제목만 추출"""
    title = re.sub(r'\s*-\s*[^-]+$', '', title).strip()
    title = re.sub(r'^\[.*?\]\s*', '', title).strip()
    return title

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
        print(f"원문 크롤링 참고 ({real_url}): {e}", flush=True)

    if not article_text or len(article_text) < 50:
        article_text = rss_snippet if rss_snippet else "원문 링크 참조 필요"

    return real_url, article_text

def collect_top_5_news():
    """1순위(해상/항공) 최대 3개 + 2, 3순위 조합으로 정확히 5개 기사 수집"""
    selected_news = []
    
    # 1. Group 1 (해상/항공): 최대 3개 수집
    g1_items = []
    for cat, url in RSS_GROUPS["group1"].items():
        feed = feedparser.parse(url)
        for entry in feed.entries[:2]:
            real_url, content = extract_article_details(entry)
            g1_items.append({
                "title": clean_title(entry.title),
                "link": real_url,
                "category": cat,
                "content": content
            })
    selected_news.extend(g1_items[:3])

    # 2. Group 2 (육상/철도/창고) 수집
    g2_items = []
    for cat, url in RSS_GROUPS["group2"].items():
        feed = feedparser.parse(url)
        for entry in feed.entries[:2]:
            real_url, content = extract_article_details(entry)
            g2_items.append({
                "title": clean_title(entry.title),
                "link": real_url,
                "category": cat,
                "content": content
            })

    # 3. Group 3 (공급망/기술/정책) 수집
    g3_items = []
    for cat, url in RSS_GROUPS["group3"].items():
        feed = feedparser.parse(url)
        for entry in feed.entries[:2]:
            real_url, content = extract_article_details(entry)
            g3_items.append({
                "title": clean_title(entry.title),
                "link": real_url,
                "category": cat,
                "content": content
            })

    for item in g2_items + g3_items:
        if len(selected_news) >= 5:
            break
        selected_news.append(item)

    return selected_news[:5]

def sanitize_category(raw_cat):
    """입력받은 분류명을 허용된 8개 카테고리 중 하나로 정리"""
    raw_cat = raw_cat.strip()
    if raw_cat in CATEGORY_MAPPER:
        return CATEGORY_MAPPER[raw_cat]
    for allowed in ALLOWED_CATEGORIES:
        if allowed in raw_cat or raw_cat in allowed:
            return allowed
    return "글로벌 공급망"  # 기본값

def get_ai_analysis(news_list):
    """PwC Alpha AI를 사용하여 뉴스 분석 (지정 카테고리 및 명사형 어미 강제)"""
    base_url = "https://workspace.kr.pwc.com/api/workspace/coding_agent/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {ALPHA_API_KEY}",
        "Content-Type": "application/json"
    }
    
    models_to_try = [
        "Default",
        "gemini-3.7-flash",
        "Gemini_3.1_Pro",
        "Processing",
        "Coding",
        "GPT_5.6_Sol"
    ]

    articles_payload = ""
    for i, news in enumerate(news_list):
        articles_payload += f"---기사 {i+1}---\n제목: {news['title']}\n본문: {news['content'][:800]}\n\n"

    prompt = f"""
당신은 물류/공급망 전문 분석가입니다. 다음 5개 기사를 분석하여 지침에 따라 작성하세요.

[기사 목록]
{articles_payload}

[작성 지침 - 엄격 준수]
1. 분류: 반드시 다음 8개 중 가장 적합한 1개만 선택하세요. (다른 조합/복수 키워드 절대 금지!)
   - 선택 가능 카테고리: [해상, 항공, 육상, 철도, 창고, 글로벌 공급망, 친환경&기술, 규제&정책]
2. 요약: 기사 핵심 내용을 명사형 어미로 끝나는 ★단 1문장★으로 압축 작성하세요. (2문장 절대 금지)
   - 어미 예시: ~함, ~음, ~임, ~확대, ~추진, ~전망, ~증가, ~지속 등
3. 시사점: 물류 실무 관점 영향 단 1문장 (개조식 명사형 어미 필수).
4. 하이라이트: 핵심 요약 포인트 3개 (개조식 명사형 어미 필수).

[응답 양식]
---ARTICLE---
번호: (숫자)
분류: (해상, 항공, 육상, 철도, 창고, 글로벌 공급망, 친환경&기술, 규제&정책 중 택1)
요약: (명사형 어미로 끝나는 단 1문장 요약)
시사점: (명사형 어미로 끝나는 1문장 시사점)
하이라이트:
- (명사형 어미 포인트 1)
- (명사형 어미 포인트 2)
- (명사형 어미 포인트 3)
---END---
"""

    for model_name in models_to_try:
        payload = {
            "model": model_name,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2
        }
        
        try:
            print(f"[{model_name}] 모델로 AI 분석 요청 중...", flush=True)
            response = requests.post(base_url, headers=headers, json=payload, timeout=60)
            if response.status_code == 200:
                result = response.json()
                if 'choices' in result and len(result['choices']) > 0:
                    print(f"SUCCESS: [{model_name}] 모델 AI 분석 완료!", flush=True)
                    content = result['choices'][0]['message']['content']
                    content = re.sub(r'```[a-zA-Z]*', '', content).replace('```', '').strip()
                    return content
            
            print(f"FAIL: [{model_name}] 응답 코드 {response.status_code}", flush=True)
        except Exception as e:
            print(f"ERROR: [{model_name}] 요청 오류 - {e}", flush=True)

    return None

def add_to_notion(title, url, category, summary, implication, highlights, article_content):
    """노션 DB 행 및 본문 등록"""
    notion_url = "https://api.notion.com/v1/pages"
    headers = {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Content-Type": "application/json",
        "Notion-Version": "2022-06-28"
    }
    
    pure_title = clean_title(title)
    final_category = sanitize_category(category)  # 카테고리 검증 및 매핑

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
            "제목": {"title": [{"text": {"content": pure_title}}]},
            "날짜": {"date": {"start": datetime.now().strftime("%Y-%m-%d")}},
            "분류": {"multi_select": [{"name": final_category}]},
            "요약": {"rich_text": [{"text": {"content": summary}}]},
            "URL": {"url": url}
        },
        "children": children_blocks
    }
    
    res = requests.post(notion_url, headers=headers, json=payload)
    if res.status_code == 200:
        print(f"✅ 노션 등록 성공: [{final_category}] {pure_title[:25]}...", flush=True)
    else:
        print(f"❌ 노션 등록 실패 ({res.status_code}): {res.text}", flush=True)

def main():
    if not ALPHA_API_KEY or not NOTION_TOKEN or not DATABASE_ID:
        print("환경 변수(ALPHA_API_KEY, NOTION_TOKEN, NOTION_PARENT_PAGE_ID) 설정을 확인하세요.", flush=True)
        return

    all_news = collect_top_5_news()

    if not all_news:
        print("수집된 뉴스가 없습니다.", flush=True)
        return

    print(f"총 {len(all_news)}개 기사 수집 완료. AI 분석을 시작합니다.", flush=True)

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
                    data.get("분류", orig['category']),
                    data.get("요약", ""),
                    data.get("시사점", ""),
                    data["하이라이트"] if data["하이라이트"] else ["본문 내용 참조"],
                    orig['content']
                )
                saved_count += 1
        except Exception as e:
            print(f"파싱/저장 중 오류: {e}", flush=True)

    print(f"총 {saved_count}개 기사 처리 및 노션 저장 완료.", flush=True)

if __name__ == "__main__":
    main()
