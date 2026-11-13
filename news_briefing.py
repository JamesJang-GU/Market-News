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
NOTION_DB_RAW = os.environ.get("NOTION_PARENT_PAGE_ID", "").strip()  # 값은 DB ID
FALLBACK_WITHOUT_AI = os.environ.get("FALLBACK_WITHOUT_AI", "1") == "1"

ALPHA_URL = "https://workspace.kr.pwc.com/api/workspace/coding_agent/v1/chat/completions"
NOTION_API_URL = "https://api.notion.com/v1/pages"

# ===== 설정 =====
DAILY_LIMIT = 5          # 하루 적재 건수
POOL_LIMIT = 16          # AI 분석 후보 풀
DEFAULT_CATEGORY = "종합·공급망"

# 카테고리 티어 (숫자 작을수록 선순위)
CATEGORY_TIER = {
    "해운": 1, "항공": 1,
    "종합·공급망": 2,
    "육상": 3, "철도": 3,
    "창고·물류센터": 4,
    "규제·정책": 5, "친환경·기술": 5,
}
ALLOWED_CATEGORIES = list(CATEGORY_TIER.keys())

GOOGLE_QUERIES = [
    "해운 OR 항공화물 OR 컨테이너선 OR 선사 OR 운임",
    "글로벌 공급망 OR 물동량 OR 홍해 OR 관세 물류 OR 국제물류",
]


def clip(text, limit=1900):
    text = text or ""
    return text if len(text) <= limit else text[:limit - 1] + "…"


def normalize_db_id(raw):
    m = re.findall(r"[0-9a-fA-F]{32}", raw.replace("-", ""))
    return m[0] if m else raw


def one_line(text, limit=110):
    """첫 문장만 추출, 줄바꿈·마침표 제거 → 1줄 요약"""
    text = (text or "").replace("\n", " ").strip()
    text = re.split(r"(?<=[.。!?])\s", text)[0]
    text = text.strip().rstrip(".。 ")
    return text if len(text) <= limit else text[:limit - 1] + "…"


# ===== 뉴스 수집 =====
def fetch_ksg_news(limit=6):
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
                        if len(articles) >= limit:
                            break
    except Exception as e:
        print(f"⚠️ KSG 크롤링 실패: {e}")
    return articles


def fetch_google_news(query, count=6):
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
        print(f"⚠️ Google RSS 실패 ({query[:10]}…): {e}")
    return articles


def fetch_all_news():
    pool = fetch_ksg_news()
    for q in GOOGLE_QUERIES:
        pool += fetch_google_news(q)

    # 제목 기준 중복 제거
    seen, unique = set(), []
    for a in pool:
        key = re.sub(r"\s+", "", a["title"])[:30]
        if key not in seen:
            seen.add(key)
            unique.append(a)
    return unique[:POOL_LIMIT]


# ===== AI 분석 =====
def analyze_with_alpha_ai(articles):
    articles_text = ""
    for idx, item in enumerate(articles, 1):
        articles_text += f"[{idx}] 제목: {item['title']}\n링크: {item['link']}\n\n"

    system_prompt = f"""
    당신은 국제물류·글로벌 공급망(SCM) 전문 리서치 분석가입니다.
    제공된 뉴스 전체({len(articles)}개)를 빠짐없이 분류·평가해 주세요.

    [카테고리 분류 지침]
    아래 8개 중 가장 적합한 하나를 표기 그대로 선택합니다:
    해운 / 항공 / 육상 / 철도 / 창고·물류센터 / 규제·정책 / 친환경·기술 / 종합·공급망
    - 해운·항공 등 국제 운송, 글로벌 공급망 이슈는 해당 카테고리를 우선 적용합니다.

    [중요도 판단 기준]
    - 5점/4점: 국제 운임·항로·선복·항공 화물 이슈, 글로벌 공급망 차질, 대형 선사/화주 계약, 즉시 대응 필요 사안
    - 3점/2점: 업계 동향, 중견 기업 실적, 신기술 도입
    - 1점: 홍보성, 참고 수준 단신

    [작성 규칙 - 매우 중요]
    - 요약: 반드시 1줄. 개조식. 어미는 명사형(~함, ~증가, ~확대, ~전망, ~필요 등). 마침표 금지.
    - 시사점: 1줄. 개조식. 어미는 명사형. 마침표 금지.
    - 서술형 어미(~합니다, ~했다, ~이다) 사용 금지.
    - 마크다운 기호(**, # 등) 사용 금지.
    - 각 기사에 원본 기사 번호를 번호: N 형식으로 명시.
    - 실무 영향력이 큰 순서대로 순위 부여.

    [출력 형식 예시]
    ---ARTICLE_START---
    번호: 3
    순위: 1
    중요도: 5
    카테고리: 해운
    요약: 홍해 우회 장기화로 아시아-유럽 컨테이너 운임 15% 상승
    시사점: 수출 화주 운송 리드타임 확대 및 선복 확보 비용 부담 증가
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
        r = requests.post(ALPHA_URL, headers=headers, json=payload, timeout=(10, 180))
        print(f"   Alpha status: {r.status_code}")
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]
    except Exception as e:
        print(f"❌ Alpha AI 호출 실패: {type(e).__name__}: {e}")
        return None


# ===== AI 응답 파싱 + 우선순위 확정 =====
KEYS = ["번호", "순위", "중요도", "카테고리", "요약", "시사점"]
KEY_RE = re.compile(r"^[\s*#\-]*(" + "|".join(KEYS) + r")\s*\**\s*[:：]\s*\**\s*(.*)$")


def parse_ai_analysis(ai_analysis, n_articles):
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
            elif line and last_key in ("요약", "시사점"):
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
            "summary": one_line(parsed.get("요약")) or "요약 정보 없음",
            "insight": one_line(parsed.get("시사점")) or "시사점 정보 없음",
        })

    # 중복 기사 번호 제거 (먼저 나온 것 유지)
    seen, dedup = set(), []
    for r in results:
        if r["idx"] not in seen:
            seen.add(r["idx"])
            dedup.append(r)
    return dedup


def prioritize(items, limit=DAILY_LIMIT):
    """카테고리 티어(국제운송 우선) → 중요도 → AI 순위"""
    ranked = sorted(
        items,
        key=lambda x: (CATEGORY_TIER.get(x["category"], 9), -x["importance"], x["rank"]),
    )
    return ranked[:limit]


def fallback_items(articles, limit=DAILY_LIMIT):
    return [{"idx": i, "rank": i + 1, "importance": 3, "category": DEFAULT_CATEGORY,
             "summary": "AI 분석 미수행, 원문 링크 참조", "insight": "AI 분석 실패로 생략"}
            for i in range(min(limit, len(articles)))]


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
        title_text = clip(art["title"], 200)  # 별 제거

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
            print(f"✅ [{it['category']}] {title_text}")
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

    print("1. 뉴스 수집 중...")
    articles = fetch_all_news()
    print(f"   후보 {len(articles)}개 수집")
    if not articles:
        sys.exit("❌ 기사 0건")

    print("2. Alpha AI 분석 중...")
    ai_analysis = analyze_with_alpha_ai(articles)
    items = parse_ai_analysis(ai_analysis, len(articles)) if ai_analysis else []

    if items:
        items = prioritize(items)
    elif FALLBACK_WITHOUT_AI:
        print("⚠️ AI 결과 없음 → 원문만 적재 (fallback)")
        items = fallback_items(articles)
    else:
        sys.exit("❌ AI 분석 실패")
    print(f"   적재 대상 {len(items)}건: " + ", ".join(i["category"] for i in items))

    print("3. Notion 적재 중...")
    if save_to_notion(articles, items) == 0:
        sys.exit("❌ Notion 적재 0건")


if __name__ == "__main__":
    main()
