import os
import re
import sys
import difflib
import datetime
import urllib.parse
import requests
import feedparser
from bs4 import BeautifulSoup

try:
    from googlenewsdecoder import gnewsdecoder
except ImportError:
    gnewsdecoder = None

# ===== 환경 변수 =====
ALPHA_API_KEY = os.environ.get("ALPHA_API_KEY", "").strip()
NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "").strip()
NOTION_DB_RAW = os.environ.get("NOTION_PARENT_PAGE_ID", "").strip()  # 값은 DB ID
FALLBACK_WITHOUT_AI = os.environ.get("FALLBACK_WITHOUT_AI", "1") == "1"

ALPHA_URL = "https://workspace.kr.pwc.com/api/workspace/coding_agent/v1/chat/completions"
NOTION_API_URL = "https://api.notion.com/v1/pages"

# ===== 설정 =====
DAILY_LIMIT = 5            # 하루 적재 건수
POOL_LIMIT = 15            # AI 분석 후보 풀
SEA_MAX = 3                # 해운 절대 상한
SEA_SOFT = 2               # 해운 기본 상한 (3번째는 영향도 5만 허용)
MIN_IMPACT = {"해운": 4}   # 카테고리별 최소 영향도
DEFAULT_MIN_IMPACT = 3
FILL_MIN_IMPACT = 2        # 5건 미달 시 채움 기준 (해운 제외)
SIMILARITY = 0.6           # 제목 유사도 임계값
AI_SENT_LIMIT = 12         # AI에 전달할 기사당 문장 수
BODY_MAX_CHARS = 6000
DEFAULT_CATEGORY = "종합·공급망"
ALLOWED_CATEGORIES = ["해운", "항공", "육상", "철도", "창고·물류센터",
                      "규제·정책", "친환경·기술", "종합·공급망"]

GOOGLE_QUERIES = [
    "컨테이너선 OR 해운 운임 OR 선사 OR 항만 물동량",
    "항공화물 OR 국제특송 OR 포워더 OR 항공 운임",
    "물류센터 OR 창고 OR 풀필먼트 OR 3PL OR 라스트마일",
    "글로벌 공급망 OR 관세 물류 OR 물류 규제 OR 수출입 물류",
]
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.5",
}


# ===== 유틸 =====
def clip(text, limit=1900):
    text = text or ""
    return text if len(text) <= limit else text[:limit - 1] + "…"


def normalize_db_id(raw):
    m = re.findall(r"[0-9a-fA-F]{32}", raw.replace("-", ""))
    return m[0] if m else raw


def one_line(text, limit=110):
    text = (text or "").replace("\n", " ").strip()
    text = re.split(r"(?<=[.。!?])\s", text)[0]
    text = text.strip().rstrip(".。 ")
    return text if len(text) <= limit else text[:limit - 1] + "…"


def norm_title(t):
    return re.sub(r"[^0-9A-Za-z가-힣]", "", t or "")


def split_sentences(text):
    parts = re.split(r"(?<=[.!?])\s+", (text or "").strip())
    return [p.strip() for p in parts if p.strip()]


# ===== 원문 수집 =====
NOISE_RE = re.compile(
    r"(저작권|무단\s*전재|재배포|구독|Copyright|©|광고문의|관련기사|댓글|공유하기|좋아요|기사제보|@[\w.-]+\.[a-z]{2,})",
    re.I)
BODY_HINT = re.compile(r"(article|content|news|view|body|text|detail|cont)", re.I)


def resolve_url(url):
    """구글 뉴스 리다이렉트 → 원문 URL (2회 재시도)"""
    if "news.google.com" not in url:
        return url
    if not gnewsdecoder:
        print("   ⚠️ googlenewsdecoder 미설치 (requirements.txt 확인)")
        return url
    for _ in range(2):
        try:
            r = gnewsdecoder(url, interval=1)
            if r.get("status") and r.get("decoded_url"):
                return r["decoded_url"]
            print(f"   ⚠️ URL 해제 실패: {str(r.get('message'))[:80]}")
        except Exception as e:
            print(f"   ⚠️ URL 해제 예외: {type(e).__name__}: {str(e)[:80]}")
    return url


def _lines_of(node):
    out, seen = [], set()
    for l in node.get_text("\n").split("\n"):
        l = re.sub(r"\s+", " ", l).strip()
        if len(l) < 25 or l in seen:
            continue
        if NOISE_RE.search(l) and len(l) < 100:
            continue
        seen.add(l)
        out.append(l)
    return out


def extract_lines(soup):
    """본문 후보 컨테이너 중 (텍스트량 × 링크 비율 보정) 최고 점수 선택"""
    for t in soup(["script", "style", "nav", "footer", "aside", "header", "form", "iframe", "noscript"]):
        t.decompose()
    for br in soup.find_all("br"):
        br.replace_with("\n")

    cands = []
    art = soup.find("article")
    if art:
        cands.append(art)
    cands += soup.find_all(["div", "section"], id=BODY_HINT)
    cands += soup.find_all(["div", "section"], class_=BODY_HINT)

    best, best_score = [], 0
    for c in cands:
        ls = _lines_of(c)
        total = sum(len(l) for l in ls)
        link_len = sum(len(a.get_text(strip=True)) for a in c.find_all("a"))
        density = link_len / max(len(c.get_text(strip=True)), 1)
        score = total * (1 - density)
        if score > best_score:
            best, best_score = ls, score

    if sum(len(l) for l in best) < 300:          # 후보 부실 시 body 전체
        body_ls = _lines_of(soup.body or soup)
        if sum(len(l) for l in body_ls) > sum(len(l) for l in best):
            best = body_ls
    return best


def fetch_body(url):
    """return (실제URL, 문단리스트[[문장…]…], 상태)"""
    real = resolve_url(url)
    if "news.google.com" in real:
        return real, [], "google_unresolved"
    try:
        res = requests.get(real, headers=HEADERS, timeout=15)
        if res.status_code != 200:
            return real, [], f"http_{res.status_code}"
        res.encoding = res.apparent_encoding
        soup = BeautifulSoup(res.text, "html.parser")

        og = soup.find("meta", property="og:description") or soup.find("meta", attrs={"name": "description"})
        og_text = (og.get("content") or "").strip() if og else ""

        lines = extract_lines(soup)
        paras, total = [], 0
        for l in lines:
            total += len(l)
            if total > BODY_MAX_CHARS:
                break
            sents = split_sentences(l)
            if sents:
                paras.append(sents)

        if paras and sum(len(s) for p in paras for s in p) >= 200:
            return real, paras, "ok"
        if og_text and len(og_text) >= 30:       # 전문 실패 시 요약문이라도
            return real, [split_sentences(og_text)], "partial"
        return real, paras, "no_text" if not paras else "ok"
    except Exception as e:
        return real, [], f"err_{type(e).__name__}"


# ===== 뉴스 수집 =====
def fetch_ksg_news(limit=6):
    url = "https://ksg.co.kr/news/main_news.jsp"
    articles = []
    try:
        res = requests.get(url, headers=HEADERS, timeout=10)
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
                        articles.append({"title": title, "link": full_url})
                        if len(articles) >= limit:
                            break
    except Exception as e:
        print(f"⚠️ KSG 크롤링 실패: {e}")
    return articles


def fetch_google_news(query, count=5):
    rss_url = (f"https://news.google.com/rss/search?q={urllib.parse.quote(query)}"
               f"&hl=ko&gl=KR&ceid=KR:ko")
    articles = []
    try:
        feed = feedparser.parse(rss_url)
        for entry in feed.entries[:count]:
            articles.append({"title": entry.title, "link": entry.link})
    except Exception as e:
        print(f"⚠️ Google RSS 실패 ({query[:10]}…): {e}")
    return articles


def fetch_all_news():
    sources = [fetch_ksg_news()] + [fetch_google_news(q) for q in GOOGLE_QUERIES]

    # 소스별 라운드로빈 → 특정 소스 쏠림 방지
    pool, i = [], 0
    while any(i < len(s) for s in sources):
        for s in sources:
            if i < len(s):
                pool.append(s[i])
        i += 1

    seen, unique = set(), []
    for a in pool:
        key = norm_title(a["title"])[:30]
        if key not in seen:
            seen.add(key)
            unique.append(a)
    unique = unique[:POOL_LIMIT]

    got = 0
    for n, a in enumerate(unique, 1):
        a["link"], a["paras"], st = fetch_body(a["link"])
        a["partial"] = (st == "partial")
        a["sents"] = [s for p in a["paras"] for s in p]
        got += 1 if a["sents"] else 0
        print(f"   [{n:>2}] {st:<18} {len(a['sents']):>3}문장 | {a['link'][:70]}")
    print(f"   본문 수집 {got}/{len(unique)}건")
    return unique


# ===== AI 분석 =====
def analyze_with_alpha_ai(articles):
    articles_text = ""
    for idx, a in enumerate(articles, 1):
        articles_text += f"[{idx}] 제목: {a['title']}\n"
        sents = a["sents"][:AI_SENT_LIMIT]
        if sents:
            articles_text += "본문: " + " ".join(f"({n}) {clip(s, 200)}" for n, s in enumerate(sents, 1)) + "\n"
        else:
            articles_text += "본문: (수집 실패, 제목만으로 판단)\n"
        articles_text += "\n"

    system_prompt = f"""
    당신은 국제물류·글로벌 공급망(SCM) 전문 리서치 분석가입니다.
    제공된 뉴스 전체({len(articles)}개)를 빠짐없이 평가해 주세요.

    [카테고리] 아래 8개 중 하나를 표기 그대로 선택
    해운 / 항공 / 육상 / 철도 / 창고·물류센터 / 규제·정책 / 친환경·기술 / 종합·공급망

    [영향도 1~5] 기준 = 국내 화주·물류사의 비용, 리드타임, 운영, 계약에 미치는 실질 영향
    - 5: 운임·선복·항로·관세·창고 수급 등 급변, 공급망 차질, 즉시 대응 필요
    - 4: 중기적으로 비용·운영에 유의미한 영향이 전망되는 이슈
    - 3: 참고할 업계 동향
    - 2: 영향 제한적 (개별 기업 소식, 행사, 홍보성)
    - 1: 영향 없음
    - 해운 기사라도 선박 인도·명명식, 선사 홍보, 항만 행사, 지자체 사업 등 화주·물류사 영향이 없으면 1~2점.
    - 창고·물류센터·기타 공급망 이슈도 화주·물류사에 큰 영향이 전망되면 4~5점 부여.

    [중복그룹] 같은 사건·이슈를 다룬 기사는 같은 알파벳(A, B…)으로 묶음. 단독 기사는 '-'.

    [작성 규칙]
    - 요약: 반드시 1줄. 개조식. 어미는 명사형(~함, ~증가, ~확대, ~전망, ~필요). 마침표 금지.
    - 시사점: 1~2줄. 화주·물류사 관점. 개조식. 명사형 어미. 마침표 금지.
    - 서술형 어미(~합니다, ~했다, ~이다) 금지.
    - 핵심문장: 본문 문장 번호 중 가장 중요한 1~3개를 쉼표로 나열. 본문이 없으면 '-'.
    - 마크다운 기호(**, # 등) 금지.
    - 모든 기사에 원본 기사 번호를 번호: N 형식으로 명시.

    [출력 형식 예시]
    ---ARTICLE_START---
    번호: 3
    영향도: 5
    카테고리: 창고·물류센터
    중복그룹: -
    요약: 수도권 냉동창고 공실률 1%대로 하락, 임대료 급등
    시사점: 냉동·냉장 화주 보관 단가 인상 및 신규 계약 확보 난이도 상승 전망
    핵심문장: 2, 5
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
        r = requests.post(ALPHA_URL, headers=headers, json=payload, timeout=(10, 240))
        print(f"   Alpha status: {r.status_code}")
        if r.status_code != 200:
            print(f"   Alpha 응답 본문: {r.text[:500]}")
        r.raise_for_status()
        data = r.json()
        if "choices" not in data:
            print(f"   Alpha 응답에 choices 없음: {str(data)[:500]}")
            return None
        return data["choices"][0]["message"]["content"]
    except Exception as e:
        print(f"❌ Alpha AI 호출 실패: {type(e).__name__}: {e}")
        return None


# ===== 파싱 =====
KEYS = ["번호", "영향도", "카테고리", "중복그룹", "요약", "시사점", "핵심문장"]
KEY_RE = re.compile(r"^[\s*#\-]*(" + "|".join(KEYS) + r")\s*\**\s*[:：]\s*\**\s*(.*)$")


def parse_ai_analysis(ai_analysis, articles):
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
        if not (0 <= idx < len(articles)):
            continue

        imp = re.search(r"[1-5]", parsed.get("영향도", ""))
        category = parsed.get("카테고리", "").strip()
        if category not in ALLOWED_CATEGORIES:
            category = DEFAULT_CATEGORY

        n_sent = min(len(articles[idx]["sents"]), AI_SENT_LIMIT)
        hl = [int(x) for x in re.findall(r"\d+", parsed.get("핵심문장", ""))]
        hl = sorted({h for h in hl if 1 <= h <= n_sent})[:3]

        group = parsed.get("중복그룹", "-").strip()
        results.append({
            "idx": idx,
            "impact": int(imp.group()) if imp else 3,
            "category": category,
            "group": "" if group in ("", "-", "없음") else group,
            "summary": one_line(parsed.get("요약")) or "요약 정보 없음",
            "insight": one_line(parsed.get("시사점"), 160) or "시사점 정보 없음",
            "highlights": hl,
        })

    seen, dedup = set(), []
    for r in results:
        if r["idx"] not in seen:
            seen.add(r["idx"])
            dedup.append(r)
    return dedup


# ===== 우선순위 / 중복 / 해운 상한 =====
def score(it):
    return it["impact"] * 10 + (2 if it["category"] in ("해운", "항공") else 0)


def is_dup(a, b, articles):
    if a["group"] and a["group"] == b["group"]:
        return True
    ta = norm_title(articles[a["idx"]]["title"])
    tb = norm_title(articles[b["idx"]]["title"])
    return difflib.SequenceMatcher(None, ta, tb).ratio() >= SIMILARITY


def prioritize(items, articles):
    ranked = sorted(items, key=lambda x: (-score(x), x["idx"]))
    chosen = []

    def try_add(it, sea_cap):
        if any(is_dup(it, c, articles) for c in chosen):
            return False
        sea = sum(1 for c in chosen if c["category"] == "해운")
        if it["category"] == "해운" and sea >= sea_cap:
            return False
        chosen.append(it)
        return True

    # 1차: 최소 영향도 충족 기사 (해운 3번째는 영향도 5만)
    for it in ranked:
        if len(chosen) >= DAILY_LIMIT:
            break
        if it["impact"] < MIN_IMPACT.get(it["category"], DEFAULT_MIN_IMPACT):
            continue
        try_add(it, SEA_MAX if it["impact"] >= 5 else SEA_SOFT)

    # 2차: 미달 시 해운 제외 영향도 2 이상으로 채움
    if len(chosen) < DAILY_LIMIT:
        for it in ranked:
            if len(chosen) >= DAILY_LIMIT:
                break
            if it in chosen or it["category"] == "해운" or it["impact"] < FILL_MIN_IMPACT:
                continue
            try_add(it, SEA_SOFT)
    return chosen


def fallback_items(articles):
    return [{"idx": i, "impact": 3, "category": DEFAULT_CATEGORY, "group": "",
             "summary": "AI 분석 미수행, 원문 링크 참조", "insight": "AI 분석 실패로 생략",
             "highlights": []}
            for i in range(min(DAILY_LIMIT, len(articles)))]


# ===== Notion 본문 구성 =====
def build_children(it, art):
    children = [
        {"object": "block", "type": "callout",
         "callout": {
             "rich_text": [
                 {"type": "text", "text": {"content": "AI 시사점\n"}, "annotations": {"bold": True}},
                 {"type": "text", "text": {"content": clip(it["insight"])}},
             ],
             "icon": {"emoji": "💡"}, "color": "gray_background"}},
        {"object": "block", "type": "divider", "divider": {}},
        {"object": "block", "type": "heading_3",
         "heading_3": {"rich_text": [{"type": "text", "text": {"content":
             "원문 기사 (요약문만 수집됨, 전문은 URL 참조)" if art.get("partial") else "원문 기사"}}]}},
    ]

    if not art["paras"]:
        children.append({"object": "block", "type": "paragraph", "paragraph": {"rich_text": [
            {"type": "text", "text": {"content": "본문 자동 수집 실패. 상단 URL 속성의 링크에서 원문 확인 필요"}}]}})
        return children

    hl, n = set(it["highlights"]), 0
    for para in art["paras"]:
        rich = []
        for s in para:
            n += 1
            obj = {"type": "text", "text": {"content": clip(s, 1900)}}
            if n in hl:
                obj["annotations"] = {"color": "yellow_background"}
            rich.append(obj)
            rich.append({"type": "text", "text": {"content": " "}})
        for i in range(0, len(rich), 80):  # rich_text 100개 제한 대응
            children.append({"object": "block", "type": "paragraph",
                             "paragraph": {"rich_text": rich[i:i + 80]}})
    return children[:95]  # children 100개 제한 대응


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
        title_text = clip(art["title"], 200)
        payload = {
            "parent": {"type": "database_id", "database_id": db_id},
            "properties": {
                "제목": {"title": [{"type": "text", "text": {"content": title_text}}]},
                "날짜": {"date": {"start": today_str}},
                "분류": {"multi_select": [{"name": it["category"]}]},
                "요약": {"rich_text": [{"type": "text", "text": {"content": clip(it["summary"])}}]},
                "URL": {"url": art["link"]},
            },
            "children": build_children(it, art),
        }
        try:
            res = requests.post(NOTION_API_URL, headers=headers, json=payload, timeout=30)
        except Exception as e:
            fail += 1
            print(f"❌ 요청 예외: {e}")
            continue

        if res.status_code == 200:
            ok += 1
            print(f"✅ [{it['category']}/영향도{it['impact']}] {title_text}")
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

    print("1. 뉴스·본문 수집 중...")
    articles = fetch_all_news()
    print(f"   후보 {len(articles)}개")
    if not articles:
        sys.exit("❌ 기사 0건")

    print("2. Alpha AI 분석 중...")
    ai_analysis = analyze_with_alpha_ai(articles)
    items = parse_ai_analysis(ai_analysis, articles) if ai_analysis else []

    if items:
        print("   AI 평가: " + ", ".join(f"{i['idx']+1}:{i['category']}/{i['impact']}" for i in items))
        items = prioritize(items, articles)
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
