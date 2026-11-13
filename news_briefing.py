import os
import re
import sys
import datetime
import urllib.parse
import requests
import feedparser
from bs4 import BeautifulSoup

# ===== 환경 변수 =====
ALPHA_API_KEY = os.environ.get("ALPHA_API_KEY", "").strip()
NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "").strip()
NOTION_DB_RAW = os.environ.get("NOTION_PARENT_PAGE_ID", "").strip()  # Secret 이름 유지 (값은 DB ID)
FALLBACK_WITHOUT_AI = os.environ.get("FALLBACK_WITHOUT_AI", "1") == "1"  # AI 실패 시 원문만 적재

ALPHA_URL = "https://workspace.kr.pwc.com/api/workspace/coding_agent/v1/chat/completions"
NOTION_API_URL = "https://api.notion.com/v1/pages"

ALLOWED_CATEGORIES = ["해운", "항공", "육상", "철도", "창고·물류센터",
                      "규제·정책", "친환경·기술", "종합·공급망"]
DEFAULT_CATEGORY = "종합·공급망"


def clip(text, limit=1900):
    """Notion rich_text 2,000자 제한 대응"""
    text = text or ""
    return text if len(text) <= limit else text[:limit - 1] + "…"


def normalize_db_id(raw):
    """URL/하이픈 포함 값이 들어와도 32자리 DB ID 추출"""
    m = re.findall(r"[0-9a-fA-F]{32}", raw.replace("-", ""))
    return m[0] if m else raw


# ===== 뉴스 수집 =====
def fetch_ksg_news():
    url = "https://ksg.co.kr/news/main_news.jsp"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    articles = []
    try:
        res = requests.get(url, headers=headers, timeout=10)
        res.encoding = res.apparent_encoding
        print(f"   KSG status: {res.status_code}")
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
                        articles.append({"title": title, "link": full_url, "published": "최신 (KSG)"})
                        if len(articles) >= 5:
                            break
    except Exception as e:
        print(f"⚠️ KSG 크롤링 실패: {e}")
    return articles


def fetch_google_logistics_news(count=5):
    query = "항공물류 OR 화물운송 OR 물류센터 OR 공급망 OR 라스트마일"
    rss_url = (f"https://news.google.com/rss/search?q={urllib.parse.quote(query)}"
               f"&hl=ko&gl=KR&ceid=KR:ko")
    articles = []
    try:
        feed = feedparser.parse(rss_url)
        for entry in feed.entries[:count]:
            articles.append({
                "title": entry.title,
                "link": entry.link,
                "published": getattr(entry, "published", "날짜 미상"),
            })
    except Exception as e:
        print(f"⚠️ Google RSS 실패: {e}")
    return articles


def fetch_all_news():
    ksg = fetch_ksg_news()
    google = fetch_google_logistics_news(count=10 - len(ksg))
    return (ksg + google)[:10]


# ===== AI 분석 =====
def analyze_with_alpha_ai(articles):
    articles_text = ""
    for idx, item in enumerate(articles, 1):
        articles_text += f"[{idx}] 제목: {item['title']}\n링크: {item['link']}\n\n"

    system_prompt = """
    당신은 종합 물류 및 공급망(SCM) 산업 전문 리서치 분석가입니다.
    제공된 뉴스 목록(최대 10개)을 분석하여, 실무자 관점에서 영향력이 큰 순서대로 정렬해 주세요.

    [카테고리 분류 지침]
    아래 8개 카테고리 중 가장 적합한 하나를 선택합니다 (표기 그대로 사용):
    - 해운 / 항공 / 육상 / 철도 / 창고·물류센터 / 규제·정책 / 친환경·기술 / 종합·공급망

    [중요도 판단 기준]
    - 5점/4점: 공급망 리스크, 규제 변경, 대형 선사/화주 계약, 즉시 대응 필요 사안
    - 3점/2점: 업계 동향, 신기술 도입, 중견 기업 실적 등
    - 1점: 일반 홍보성, 참고 수준의 단신

    [출력 정렬 규칙]
    - 실무 영향력이 가장 큰 기사를 1번으로 배치하여 내림차순 정렬합니다.
    - 각 기사에는 반드시 원본 기사 번호를 번호: N 형식으로 명시합니다.
    - 마크다운 기호(**, # 등) 없이 아래 형식만 출력합니다.

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

    headers = {"Authorization": f"Bearer {ALPHA_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": "Default",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"다음 뉴스들을 분석해 주세요:\n\n{articles_text}"},
        ],
    }

    try:
        r = requests.post(ALPHA_URL, headers=headers, json=payload, timeout=(10, 120))
        print(f"   Alpha status: {r.status_code}")
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]
    except Exception as e:
        print(f"❌ Alpha AI 호출 실패: {type(e).__name__}: {e}")
        return None


# ===== AI 응답 파싱 =====
KEYS = ["번호", "순위", "중요도", "카테고리", "요약", "시사점"]
KEY_RE = re.compile(r"^[\s*#\-]*(" + "|".join(KEYS) + r")\s*\**\s*[:：]\s*\**\s*(.*)$")


def parse_ai_analysis(ai_analysis, n_articles):
    """AI 텍스트 → [{idx, rank, importance, category, summary, insight}] (순위순)"""
    results = []
    for block in ai_analysis.split("---ARTICLE_START---")[1:]:
        block = block.split("---ARTICLE_END---")[0]
        parsed, last_key = {}, None
        for line in block.split("\n"):
            line = line.strip()
            m = KEY_RE.match(line)
            if m:
                last_key = m.group(1)
                parsed[last_key] = m.group(2).strip().strip("*")
            elif line and last_key in ("요약", "시사점"):  # 여러 줄 이어붙임
                parsed[last_key] += " " + line.strip("*")

        num = re.search(r"\d+", parsed.get("번호", ""))
        if not num:
            continue
        idx = int(num.group()) - 1
        if not (0 <= idx < n_articles):
            continue

        imp = re.search(r"[1-5]", parsed.get("중요도", ""))
        rank = re.search(r"\d+", parsed.get("순위", ""))
        category = parsed.get("카테고리", "").strip()
        if category not in ALLOWED_CATEGORIES:
            category = DEFAULT_CATEGORY

        results.append({
            "idx": idx,
            "rank": int(rank.group()) if rank else 99,
            "importance": int(imp.group()) if imp else 3,
            "category": category,
            "summary": parsed.get("요약") or "요약 정보 없음",
            "insight": parsed.get("시사점") or "시사점 정보 없음",
        })
    return sorted(results, key=lambda x: x["rank"])


def fallback_items(articles):
    return [{"idx": i, "rank": i + 1, "importance": 3, "category": DEFAULT_CATEGORY,
             "summary": "(AI 분석 미수행 - 원문 링크 참조)", "insight": "AI 분석 실패로 생략"}
            for i in range(len(articles))]


# ===== Notion 적재 =====
def save_to_notion(articles, items):
    today_str = datetime.datetime.now().strftime("%Y-%m-%d")
    db_id = normalize_db_id(NOTION_DB_RAW)
    headers = {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Content-Type": "application/json",
        "Notion-Version": "2022-06-28",
    }
    ok, fail = 0, 0

    for it in items:
        art = articles[it["idx"]]
        title_text = clip(f"[{'★' * it['importance']}] {art['title']}", 200)

        payload = {
            "parent": {"type": "database_id", "database_id": db_id},
            "properties": {
                "제목": {"title": [{"type": "text", "text": {"content": title_text}}]},
                "날짜": {"date": {"start": today_str}},
                "분류": {"multi_select": [{"name": it["category"]}]},
                "요약": {"rich_text": [{"type": "text", "text": {"content": clip(it["summary"])}}]},
                "URL": {"url": art["link"]},
            },
            "children": [
                {"object": "block", "type": "callout",
                 "callout": {"rich_text": [{"type": "text", "text": {"content": "AI 시사점 분석"}}],
                             "icon": {"emoji": "💡"}}},
                {"object": "block", "type": "paragraph",
                 "paragraph": {"rich_text": [{"type": "text", "text": {"content": clip(it["insight"])}}]}},
            ],
        }

        try:
            res = requests.post(NOTION_API_URL, headers=headers, json=payload, timeout=30)
        except Exception as e:
            fail += 1
            print(f"❌ 요청 예외: {e}")
            continue

        if res.status_code == 200:
            ok += 1
            print(f"✅ {title_text}")
        else:
            fail += 1
            print(f"❌ ({res.status_code}) {res.text[:500]}")

    print(f"\n결과: 성공 {ok} / 실패 {fail}")
    return ok


# ===== 실행 =====
def main():
    missing = [k for k, v in {"ALPHA_API_KEY": ALPHA_API_KEY, "NOTION_TOKEN": NOTION_TOKEN,
                              "NOTION_PARENT_PAGE_ID": NOTION_DB_RAW}.items() if not v]
    if missing:
        sys.exit(f"❌ 환경변수 누락: {missing}")
    print(f"   DB ID 길이: {len(normalize_db_id(NOTION_DB_RAW))} (32여야 정상)")

    print("1. 뉴스 수집 중...")
    articles = fetch_all_news()
    print(f"   총 {len(articles)}개 수집")
    if not articles:
        sys.exit("❌ 기사 0건")

    print("2. Alpha AI 분석 중...")
    ai_analysis = analyze_with_alpha_ai(articles)
    items = parse_ai_analysis(ai_analysis, len(articles)) if ai_analysis else []

    if not items:
        if FALLBACK_WITHOUT_AI:
            print("⚠️ AI 결과 없음 → 원문만 적재 (fallback)")
            items = fallback_items(articles)
        else:
            sys.exit("❌ AI 분석 실패")
    print(f"   적재 대상 {len(items)}건")

    print("3. Notion 적재 중...")
    if save_to_notion(articles, items) == 0:
        sys.exit("❌ Notion 적재 0건")  # exit 1 → Action 빨간불


if __name__ == "__main__":
    main()
