"""
물류 뉴스 자동 수집 → AI 선별(5건) → 노션 DB 저장
수정 사항:
 - Google News RSS 기간 제한(when:) + 한국 로케일(hl/gl/ceid) 적용
 - 과도한 AND 쿼리 → OR 쿼리로 재구성 (해상 Source 문제 해결)
 - 해운 전문지 site: 쿼리 보강
 - 최신순 재정렬 + 최대 기사 연령 필터
 - 노션 기존 기사(URL/제목) 기반 중복 제거
 - googlenewsdecoder 신/구버전 호환
 - <p> 없는 언론사 본문 추출 보강
 - AI 응답 파싱 강화 (마크다운/구분자 누락 대응)
 - 해상 1~3건 조건 코드 레벨 강제
 - 날짜 KST 기준 저장
"""

import os
import re
import calendar
import urllib.parse
from datetime import datetime, timedelta, timezone

import requests
import feedparser
from bs4 import BeautifulSoup

# --- 구글 뉴스 디코더 (신/구버전 호환) ---
try:
    from googlenewsdecoder import gnewsdecoder as _gn_decode          # 최신 버전
except ImportError:
    try:
        from googlenewsdecoder import new_decoderv1 as _gn_decode     # 구버전
    except ImportError:
        _gn_decode = None

# --- 설정 (Environment Variables) ---
ALPHA_API_KEY = os.getenv("ALPHA_API_KEY")
NOTION_TOKEN = os.getenv("NOTION_TOKEN")
DATABASE_ID = os.getenv("NOTION_PARENT_PAGE_ID")   # 반드시 '데이터베이스 ID'여야 함

KST = timezone(timedelta(hours=9))

NOTION_HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Content-Type": "application/json",
    "Notion-Version": "2022-06-28",
}

LLM_URL = "https://workspace.kr.pwc.com/api/workspace/coding_agent/v1/chat/completions"
MODELS_TO_TRY = [
    "Default",
    "gemini-3.7-flash",
    "Gemini_3.1_Pro",
    "Processing",
    "Coding",
    "GPT_5.6_Sol",
]

# 노션 허용 카테고리
ALLOWED_CATEGORIES = [
    "해상", "항공", "육상", "철도", "창고", "글로벌 공급망", "친환경&기술", "규제&정책"
]

CATEGORY_MAPPER = {
    "해운": "해상", "항만": "해상", "해운·항만": "해상", "해상": "해상",
    "항공": "항공", "항공·물류": "항공",
    "육상": "육상", "육상·창고": "육상",
    "철도": "철도",
    "창고": "창고", "창고·물류센터": "창고",
    "글로벌 공급망": "글로벌 공급망", "종합·공급망": "글로벌 공급망", "공급망": "글로벌 공급망",
    "친환경&기술": "친환경&기술", "친환경·기술": "친환경&기술",
    "규제&정책": "규제&정책", "규제·정책": "규제&정책", "정책·규제": "규제&정책",
}

# --- RSS 소스 ---
def gnews_rss(query, days=1):
    """기간 제한 + 한국 로케일 적용 Google News 검색 RSS"""
    q = f"{query} when:{days}d"
    return ("https://news.google.com/rss/search?q=" + urllib.parse.quote(q)
            + "&hl=ko&gl=KR&ceid=KR:ko")

RSS_SOURCES = {
    "해상": [
        gnews_rss("(해운 OR 항만 OR 해상운임 OR 컨테이너선 OR 선사 OR 벌크선 OR SCFI OR HMM)"),
        # 해운 전문지 보강 (도메인은 실제 사이트에서 확인 후 조정)
        gnews_rss("site:ksg.co.kr OR site:shippingnewsnet.com OR site:maritimepress.co.kr", days=2),
    ],
    "항공": [gnews_rss("(항공화물 OR 항공물류 OR 화물기 OR 항공운임)")],
    "육상": [gnews_rss("(화물차 OR 화물운송 OR 화물연대 OR 택배물류 OR 육상운송)")],
    "철도": [gnews_rss("(철도물류 OR 화물열차 OR 철도화물 OR TSR OR 중국횡단철도)", days=3)],
    "창고": [gnews_rss("(물류창고 OR 풀필먼트 OR 물류센터)")],
    "글로벌 공급망": [gnews_rss("공급망 (관세 OR 수출 OR 수입 OR 리스크)")],
    "친환경&기술": [gnews_rss("물류 (친환경 OR 자율주행 OR 로봇 OR 스마트 OR AI)")],
    "규제&정책": [gnews_rss("물류 (정책 OR 규제 OR 국토부 OR 해수부 OR 관세청)")],
}

# 카테고리별 후보 수 / 최대 기사 연령(시간)
PER_CATEGORY = {"해상": 5}
DEFAULT_PER_CATEGORY = 3
MAX_AGE_HOURS = {"철도": 72, "해상": 48}
DEFAULT_MAX_AGE_HOURS = 36
DEDUP_LOOKBACK_DAYS = 30
TARGET_COUNT = 5
MARITIME_MIN, MARITIME_MAX = 1, 3


# ============================================================
# 유틸
# ============================================================
def clean_title(title):
    """제목 정제: 출처 및 [태그] 삭제"""
    title = re.sub(r'\s*-\s*[^-]+$', '', title or '').strip()
    title = re.sub(r'^\[.*?\]\s*', '', title).strip()
    return title

def norm_title(t):
    """제목 비교용 정규화 (공백/특수문자 제거)"""
    return re.sub(r'[^0-9a-zA-Z가-힣]', '', t or '')

def entry_time(entry):
    t = entry.get("published_parsed") or entry.get("updated_parsed")
    return datetime.fromtimestamp(calendar.timegm(t), tz=timezone.utc) if t else None

def sanitize_category(raw_cat):
    """카테고리 규격 검증 및 8개 단일 키워드 매핑"""
    raw_cat = (raw_cat or "").strip().strip("[]()")
    if raw_cat in CATEGORY_MAPPER:
        return CATEGORY_MAPPER[raw_cat]
    for allowed in ALLOWED_CATEGORIES:
        if allowed in raw_cat or (raw_cat and raw_cat in allowed):
            return allowed
    return "글로벌 공급망"

def rt(text, limit=2000):
    """노션 rich_text 1개 객체 최대 2000자 제한 대응"""
    return [{"type": "text", "text": {"content": (text or "")[:limit]}}]


# ============================================================
# 원문 URL 복원 및 본문 추출
# ============================================================
def decode_google_news_url(url):
    """구글 뉴스 RSS 링크를 실제 언론사 원문 URL로 변환"""
    if _gn_decode and "news.google.com" in url:
        try:
            r = _gn_decode(url, interval=1)
            if isinstance(r, dict) and r.get("status") and r.get("decoded_url"):
                return r["decoded_url"]
            print(f"⚠️ 디코딩 실패: {r}", flush=True)
        except Exception as e:
            print(f"구글 URL 디코딩 오류: {e}", flush=True)
    return url

def extract_article_details(entry):
    """뉴스 원문 URL 추적 및 본문 텍스트 수집"""
    headers = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
    }
    rss_snippet = ""
    if hasattr(entry, "summary"):
        rss_snippet = BeautifulSoup(entry.summary, "html.parser").get_text().strip()

    real_url = decode_google_news_url(entry.link)
    article_text = ""

    try:
        res = requests.get(real_url, headers=headers, timeout=10, allow_redirects=True)
        if res.status_code != 200:
            print(f"⚠️ 원문 응답 {res.status_code}: {real_url[:80]}", flush=True)
        res.encoding = res.apparent_encoding or "utf-8"
        soup = BeautifulSoup(res.text, "html.parser")

        for tag in soup(["script", "style", "header", "footer", "nav", "aside", "form"]):
            tag.decompose()

        main_content = (
            soup.find("article")
            or soup.find("main")
            or soup.find(id=re.compile(r"(article|content|news|view|body)", re.I))
            or soup.find("div", class_=re.compile(r"(article|content|post|view|news_body)", re.I))
        )
        target = main_content if main_content else soup

        paragraphs = [p.get_text().strip() for p in target.find_all("p")
                      if len(p.get_text().strip()) > 30]
        article_text = "\n".join(paragraphs)

        # <p> 없이 <div>/<br>로 본문을 쓰는 언론사 대응
        if len(article_text) < 200:
            lines = [l.strip() for l in target.get_text("\n").split("\n") if len(l.strip()) > 30]
            article_text = "\n".join(lines)

        if "news.google.com" in res.url:
            print(f"⚠️ 원문 URL 미복원 상태로 크롤링됨: {res.url[:80]}", flush=True)
            article_text = ""
    except Exception as e:
        print(f"원문 크롤링 참고 ({real_url[:80]}): {e}", flush=True)

    if not article_text or len(article_text) < 50:
        article_text = rss_snippet if rss_snippet else "원문 링크 참조 필요"

    return real_url, article_text


# ============================================================
# 노션 중복 방지
# ============================================================
def fetch_existing_from_notion(days=DEDUP_LOOKBACK_DAYS):
    """최근 N일 동안 노션에 저장된 URL/제목 집합 반환"""
    url = f"https://api.notion.com/v1/databases/{DATABASE_ID}/query"
    since = (datetime.now(KST) - timedelta(days=days)).strftime("%Y-%m-%d")
    body = {"filter": {"property": "날짜", "date": {"on_or_after": since}}, "page_size": 100}
    urls, titles = set(), set()

    while True:
        try:
            res = requests.post(url, headers=NOTION_HEADERS, json=body, timeout=30)
        except Exception as e:
            print(f"⚠️ 노션 기존 기사 조회 오류: {e}", flush=True)
            break
        if res.status_code != 200:
            print(f"⚠️ 노션 기존 기사 조회 실패({res.status_code}): {res.text[:200]}", flush=True)
            break
        data = res.json()
        for page in data.get("results", []):
            props = page.get("properties", {})
            u = (props.get("URL") or {}).get("url")
            if u:
                urls.add(u)
            t = (props.get("제목") or {}).get("title", [])
            if t:
                titles.add(norm_title("".join(x.get("plain_text", "") for x in t)))
        if not data.get("has_more"):
            break
        body["start_cursor"] = data["next_cursor"]

    print(f"노션 기존 기사 로드: URL {len(urls)}건 / 제목 {len(titles)}건", flush=True)
    return urls, titles


# ============================================================
# 후보 수집
# ============================================================
def collect_candidate_news(seen_urls, seen_titles):
    candidates = []
    used_titles = set(seen_titles)
    used_urls = set(seen_urls)
    now = datetime.now(timezone.utc)
    oldest = datetime.min.replace(tzinfo=timezone.utc)

    for cat, urls in RSS_SOURCES.items():
        entries, seen_links = [], set()
        for u in urls:
            feed = feedparser.parse(u)
            if getattr(feed, "bozo", 0) and not feed.entries:
                print(f"⚠️ [{cat}] RSS 파싱 실패: {u[:100]}", flush=True)
            for e in feed.entries:
                if e.link not in seen_links:
                    seen_links.add(e.link)
                    entries.append(e)

        # 관련도순 → 최신순 재정렬
        entries.sort(key=lambda e: entry_time(e) or oldest, reverse=True)

        limit = PER_CATEGORY.get(cat, DEFAULT_PER_CATEGORY)
        max_age = timedelta(hours=MAX_AGE_HOURS.get(cat, DEFAULT_MAX_AGE_HOURS))
        picked = skipped_old = skipped_dup = 0

        for e in entries:
            if picked >= limit:
                break
            pub = entry_time(e)
            if pub and now - pub > max_age:
                skipped_old += 1
                continue

            title = clean_title(e.title)
            key = norm_title(title)
            if not key or key in used_titles:
                skipped_dup += 1
                continue
            used_titles.add(key)

            real_url, content = extract_article_details(e)
            if real_url in used_urls:
                skipped_dup += 1
                continue
            used_urls.add(real_url)

            candidates.append({"title": title, "link": real_url, "category": cat, "content": content})
            picked += 1

        flag = "  ← ⚠️ Source 점검 필요" if len(entries) == 0 else ""
        print(f"[{cat}] RSS {len(entries)}건 → 선정 {picked} / 오래됨 {skipped_old} / 중복 {skipped_dup}{flag}",
              flush=True)

    return candidates


# ============================================================
# AI 호출 / 프롬프트 / 파싱
# ============================================================
def call_llm(prompt):
    headers = {"Authorization": f"Bearer {ALPHA_API_KEY}", "Content-Type": "application/json"}
    for model_name in MODELS_TO_TRY:
        payload = {"model": model_name, "messages": [{"role": "user", "content": prompt}], "temperature": 0.2}
        try:
            print(f"[{model_name}] 모델 요청 중...", flush=True)
            response = requests.post(LLM_URL, headers=headers, json=payload, timeout=90)
            if response.status_code == 200:
                result = response.json()
                if result.get("choices"):
                    content = result["choices"][0]["message"]["content"] or ""
                    content = re.sub(r"```[a-zA-Z]*", "", content).replace("```", "").strip()
                    if content:
                        print(f"SUCCESS: [{model_name}] 응답 수신", flush=True)
                        return content
            print(f"FAIL: [{model_name}] 응답 코드 {response.status_code}", flush=True)
        except Exception as e:
            print(f"ERROR: [{model_name}] 요청 오류 - {e}", flush=True)
    return None

def build_prompt(indexed_items, select_n, maritime_rule=True):
    """indexed_items: [(후보번호, candidate), ...]"""
    payload = ""
    for idx, news in indexed_items:
        payload += (f"---후보 기사 {idx}---\n제목: {news['title']}\n"
                    f"기본분류: {news['category']}\n본문: {news['content'][:600]}\n\n")

    if maritime_rule:
        rules = f"""1. 전체 선별 수량: 정확히 {select_n}개 기사 선택.
2. '해상' 카테고리 조건: 선별된 {select_n}개 중 '해상' 기사는 최소 {MARITIME_MIN}개 이상, 최대 {MARITIME_MAX}개 이하.
3. 나머지 기사: 글로벌 공급망 및 물류 실무 영향도가 높은 순으로 채울 것.
4. 평가 과정·판단 근거 문구는 일절 출력 금지. 결과 양식만 출력."""
    else:
        rules = f"""1. 제시된 기사 {select_n}개를 모두 분석할 것.
2. 평가 과정·판단 근거 문구는 일절 출력 금지. 결과 양식만 출력."""

    return f"""
당신은 물류/공급망 전문 분석가입니다. 제시된 후보 기사를 지정 양식으로 분석하세요.

[후보 기사 목록]
{payload}
[선별 지침]
{rules}

[작성 규칙]
1. 분류: 반드시 다음 8개 중 1개 [해상, 항공, 육상, 철도, 창고, 글로벌 공급망, 친환경&기술, 규제&정책]
2. 요약: 핵심 내용을 명사형 어미로 끝나는 단 1문장 (~함, ~음, ~임, ~확대, ~추진, ~전망 등)
3. 시사점: 물류 실무 관점 영향 단 1문장 (명사형 어미)
4. 하이라이트: 핵심 포인트 3개 (명사형 어미)
5. 마크다운 굵게(**), 제목(#) 사용 금지.

[응답 양식]
---ARTICLE---
후보번호: (숫자)
분류: (8개 중 택1)
요약: (1문장)
시사점: (1문장)
하이라이트:
- (포인트 1)
- (포인트 2)
- (포인트 3)
"""

FIELD_RE = re.compile(r"^(후보번호|분류|요약|시사점|하이라이트)\s*[:：]\s*(.*)$")
BULLET_RE = re.compile(r"^[-•·*]\s*(.+)$")

def parse_articles(text):
    """AI 응답 파싱 (마크다운·구분자 누락에 강건하게)"""
    text = text.replace("**", "").replace("---ARTICLE---", "\n")
    blocks = re.split(r"(?=^\s*#*\s*후보번호\s*[:：])", text, flags=re.M)
    results = []
    for block in blocks:
        data = {"하이라이트": []}
        is_hl = False
        for raw in block.split("\n"):
            line = raw.strip().lstrip("#").strip()
            if not line:
                continue
            m = FIELD_RE.match(line)
            if m:
                key, val = m.group(1), m.group(2).strip()
                is_hl = (key == "하이라이트")
                if key == "후보번호":
                    num = re.search(r"\d+", val)
                    if num:
                        data["후보번호"] = int(num.group())
                elif key != "하이라이트":
                    data[key] = val
                continue
            if is_hl:
                b = BULLET_RE.match(line)
                if b:
                    data["하이라이트"].append(b.group(1).strip())
        if "후보번호" in data:
            results.append(data)
    return results


# ============================================================
# 선별 + 해상 조건 강제
# ============================================================
def select_articles(candidates):
    indexed = list(enumerate(candidates, start=1))
    content = call_llm(build_prompt(indexed, TARGET_COUNT, maritime_rule=True))
    if not content:
        return []

    selected, used_idx = [], set()
    for d in parse_articles(content):
        idx = d["후보번호"]
        if not (1 <= idx <= len(candidates)) or idx in used_idx:
            continue
        used_idx.add(idx)
        d["final_cat"] = sanitize_category(d.get("분류") or candidates[idx - 1]["category"])
        selected.append(d)

    # 해상 최대 3건 초과 시 초과분 제거
    kept, m_count = [], 0
    for d in selected:
        if d["final_cat"] == "해상":
            m_count += 1
            if m_count > MARITIME_MAX:
                continue
        kept.append(d)
    selected = kept[:TARGET_COUNT]

    # 해상 0건 시 해상 후보 1건 강제 편입
    if not any(d["final_cat"] == "해상" for d in selected):
        pool = [(i, c) for i, c in indexed if c["category"] == "해상" and i not in used_idx]
        if not pool:
            print("⚠️ 해상 후보 0건 → Source(쿼리) 문제. 해상 RSS 점검 필요.", flush=True)
        else:
            print("⚠️ AI가 해상 조건 미준수 → 해상 후보 1건 강제 편입", flush=True)
            idx, cand = pool[0]
            single = call_llm(build_prompt([(idx, cand)], 1, maritime_rule=False))
            parsed = parse_articles(single) if single else []
            forced = parsed[0] if parsed else {"후보번호": idx, "요약": "", "시사점": "", "하이라이트": []}
            forced["후보번호"] = idx
            forced["final_cat"] = "해상"
            if len(selected) >= TARGET_COUNT:
                selected[-1] = forced
            else:
                selected.append(forced)

    return selected


# ============================================================
# 노션 저장
# ============================================================
def add_to_notion(title, url, category, summary, implication, highlights, article_content):
    notion_url = "https://api.notion.com/v1/pages"
    pure_title = clean_title(title)

    children = [
        {"object": "block", "type": "callout", "callout": {
            "rich_text": rt(f"AI 시사점\n{implication}"),
            "icon": {"emoji": "💡"}, "color": "yellow_background"}},
        {"object": "block", "type": "heading_3", "heading_3": {"rich_text": rt("📌 핵심 하이라이트")}},
    ]
    for hl in highlights:
        if hl.strip():
            children.append({"object": "block", "type": "bulleted_list_item",
                             "bulleted_list_item": {"rich_text": rt(hl)}})
    children.extend([
        {"object": "block", "type": "divider", "divider": {}},
        {"object": "block", "type": "heading_3", "heading_3": {"rich_text": rt("📄 수집된 원문 내용")}},
        {"object": "block", "type": "paragraph", "paragraph": {"rich_text": rt(article_content, 1500)}},
    ])

    payload = {
        "parent": {"database_id": DATABASE_ID},
        "properties": {
            "제목": {"title": [{"text": {"content": pure_title[:2000]}}]},
            "날짜": {"date": {"start": datetime.now(KST).strftime("%Y-%m-%d")}},
            "분류": {"multi_select": [{"name": category}]},
            "요약": {"rich_text": [{"text": {"content": (summary or "")[:2000]}}]},
            "URL": {"url": url},
        },
        "children": children,
    }

    try:
        res = requests.post(notion_url, headers=NOTION_HEADERS, json=payload, timeout=30)
    except Exception as e:
        print(f"❌ 노션 요청 오류: {e}", flush=True)
        return False
    if res.status_code == 200:
        print(f"✅ 노션 등록 성공: [{category}] {pure_title[:25]}...", flush=True)
        return True
    print(f"❌ 노션 등록 실패 ({res.status_code}): {res.text[:300]}", flush=True)
    return False


# ============================================================
# main
# ============================================================
def main():
    if not ALPHA_API_KEY or not NOTION_TOKEN or not DATABASE_ID:
        print("환경 변수(ALPHA_API_KEY, NOTION_TOKEN, NOTION_PARENT_PAGE_ID) 설정을 확인하세요.", flush=True)
        return
    if _gn_decode is None:
        print("⚠️ googlenewsdecoder 미설치: 원문 URL 복원 불가 → 본문 수집 품질 저하", flush=True)

    seen_urls, seen_titles = fetch_existing_from_notion()
    candidates = collect_candidate_news(seen_urls, seen_titles)

    if not candidates:
        print("신규 후보 뉴스 0건. 소스 쿼리 점검 필요.", flush=True)
        return
    if len(candidates) < TARGET_COUNT:
        print(f"⚠️ 신규 후보 {len(candidates)}건뿐. 소스 쿼리 점검 권고.", flush=True)

    print(f"총 {len(candidates)}개 신규 후보 수집 완료. AI 선별 시작.", flush=True)

    selected = select_articles(candidates)
    if not selected:
        print("AI 분석 결과를 가져오지 못했습니다.", flush=True)
        return

    saved = 0
    for d in selected:
        orig = candidates[d["후보번호"] - 1]
        ok = add_to_notion(
            orig["title"],
            orig["link"],
            d["final_cat"],
            d.get("요약", ""),
            d.get("시사점", ""),
            d["하이라이트"] or ["본문 내용 참조"],
            orig["content"],
        )
        saved += int(ok)

    m_cnt = sum(1 for d in selected if d["final_cat"] == "해상")
    print(f"선별 {len(selected)}건 / 저장 {saved}건 / 해상 {m_cnt}건 완료.", flush=True)


if __name__ == "__main__":
    main()
