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
NEWS_WINDOW = os.environ.get("NEWS_WINDOW", "1d")      # Google News 검색 기간 (최근 24시간, 이후 당일 날짜로 재필터)
ALLOW_UNKNOWN_DATE = os.environ.get("ALLOW_UNKNOWN_DATE", "0") == "1"  # 1이면 발행일 확인 불가 기사도 허용
RECENT_DAYS = 7                                         # 이 기간 내 Notion 적재 기사와 중복 제외
FORCE_RUN = os.environ.get("FORCE_RUN", "0") == "1"     # 1이면 오늘 이미 적재돼 있어도 실행
BODY_FIRST = os.environ.get("BODY_FIRST", "1") == "1"   # 1이면 본문 확보 기사에 +3점 가산
KST = datetime.timezone(datetime.timedelta(hours=9))
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
def today_kst():
    """기준일(KST). 자정~02:00 사이 실행(GitHub cron 지연)은 전날로 간주. TARGET_DATE=YYYY-MM-DD로 강제 가능"""
    override = os.environ.get("TARGET_DATE", "").strip()
    if override:
        return override
    now = datetime.datetime.now(KST)
    if now.hour < 2:
        now -= datetime.timedelta(days=1)
    return now.strftime("%Y-%m-%d")


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


# ===== 발행일 판정 =====
LABEL_DATE_RE = re.compile(
    r"(?:입력|등록|기사입력|발행|승인|작성)\s*[:：]?\s*(20\d{2})[.\-/년]\s*(\d{1,2})[.\-/월]\s*(\d{1,2})")


def parse_date_str(v):
    """ISO/일반 날짜 문자열 → KST 기준 YYYY-MM-DD (실패 시 '')"""
    v = (v or "").strip()
    try:
        dt = datetime.datetime.fromisoformat(v.replace("Z", "+00:00"))
        if dt.tzinfo:
            dt = dt.astimezone(KST)
        return dt.strftime("%Y-%m-%d")
    except ValueError:
        pass
    m = re.search(r"(20\d{2})[-./](\d{1,2})[-./](\d{1,2})", v)
    return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}" if m else ""


def extract_pub_date(soup, html):
    """기사 발행일(KST). meta / JSON-LD / '입력·등록' 라벨 날짜만 사용. 불확실하면 ''"""
    cands = []
    for attr, val in [("property", "article:published_time"), ("property", "og:article:published_time"),
                      ("name", "article:published_time"), ("name", "pubdate"), ("name", "date"),
                      ("itemprop", "datePublished"), ("name", "DC.date.issued")]:
        tag = soup.find("meta", attrs={attr: val})
        if tag and tag.get("content"):
            cands.append(tag["content"])
    m = re.search(r'"datePublished"\s*:\s*"([^"]+)"', html or "")
    if m:
        cands.append(m.group(1))
    for c in cands:
        d = parse_date_str(c)
        if d:
            return d
    m = LABEL_DATE_RE.search(soup.get_text(" ", strip=True))
    if m:
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return ""


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
    try:  # 보조: 리다이렉트 추적
        r = requests.get(url, headers=HEADERS, timeout=10, allow_redirects=True)
        if "news.google.com" not in r.url:
            return r.url
    except Exception:
        pass
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
    """return (실제URL, 문단리스트[[문장…]…], 상태, 발행일)"""
    real = resolve_url(url)
    if "news.google.com" in real:
        return real, [], "google_unresolved", ""
    try:
        res = requests.get(real, headers=HEADERS, timeout=15)
        if res.status_code != 200:
            return real, [], f"http_{res.status_code}", ""
        res.encoding = res.apparent_encoding
        soup = BeautifulSoup(res.text, "html.parser")

        pub = extract_pub_date(soup, res.text)
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
            return real, paras, "ok", pub
        if og_text and len(og_text) >= 30:       # 전문 실패 시 요약문이라도
            return real, [split_sentences(og_text)], "partial", pub
        return real, paras, ("no_text" if not paras else "ok"), pub
    except Exception as e:
        return real, [], f"err_{type(e).__name__}", ""


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


def fetch_google_news(query, count=10):
    rss_url = (f"https://news.google.com/rss/search?q={urllib.parse.quote(query + f' when:{NEWS_WINDOW}')}"
               f"&hl=ko&gl=KR&ceid=KR:ko")
    articles = []
    try:
        feed = feedparser.parse(rss_url)
        for entry in feed.entries[:count]:
            pub = ""
            pp = getattr(entry, "published_parsed", None)
            if pp:
                pub = (datetime.datetime(*pp[:6], tzinfo=datetime.timezone.utc)
                       .astimezone(KST).strftime("%Y-%m-%d"))
            articles.append({"title": entry.title, "link": entry.link, "date": pub})
    except Exception as e:
        print(f"⚠️ Google RSS 실패 ({query[:10]}…): {e}")
    return articles


def fetch_all_news(recent_titles=(), recent_urls=()):
    sources = [fetch_ksg_news()] + [fetch_google_news(q) for q in GOOGLE_QUERIES]

    # 소스별 라운드로빈 → 특정 소스 쏠림 방지
    pool, i = [], 0
    while any(i < len(s) for s in sources):
        for s in sources:
            if i < len(s):
                pool.append(s[i])
        i += 1

    seen, unique, skipped, date_skipped = set(), [], 0, 0
    today = today_kst()
    recent_norm = [norm_title(t) for t in recent_titles]
    for a in pool:
        nt = norm_title(a["title"])
        key = nt[:30]
        if key in seen:
            continue
        if a.get("date") and a["date"] != today:      # 당일 기사만
            date_skipped += 1
            continue
        if a["link"] in recent_urls or any(
                difflib.SequenceMatcher(None, nt, r).ratio() >= SIMILARITY for r in recent_norm):
            skipped += 1
            continue
        seen.add(key)
        unique.append(a)
    print(f"   기준일 {today} 외 기사 제외: {date_skipped}건 / 최근 {RECENT_DAYS}일 적재 기사와 중복 제외: {skipped}건")
    unique = unique[:POOL_LIMIT]

    got = 0
    for n, a in enumerate(unique, 1):
        a["link"], a["paras"], st, pub = fetch_body(a["link"])
        if not a.get("date"):
            a["date"] = pub
        a["partial"] = (st == "partial")
        a["sents"] = [s for p in a["paras"] for s in p]
        got += 1 if a["sents"] else 0
        print(f"   [{n:>2}] {st:<18} {len(a['sents']):>3}문장 | {a['date'] or '날짜불명':<10} | {a['link'][:60]}")
    print(f"   본문 수집 {got}/{len(unique)}건")

    kept = [a for a in unique if a["date"] == today or (not a["date"] and ALLOW_UNKNOWN_DATE)]
    unknown = sum(1 for a in unique if not a["date"])
    print(f"   당일({today}) 기사 {len(kept)}/{len(unique)}건 (발행일 불명 {unknown}건, "
          f"{'포함' if ALLOW_UNKNOWN_DATE else '제외'})")
    return kept


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
        "temperature": 0,   # 실행마다 결과가 바뀌는 현상 최소화
    }
    try:
        r = requests.post(ALPHA_URL, headers=headers, json=payload, timeout=(10, 240))
        if r.status_code == 400 and "temperature" in r.text.lower():
            payload.pop("temperature", None)
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


def prioritize(items, articles, limit=DAILY_LIMIT):
    def has_body(it):
        return bool(articles[it["idx"]]["sents"])

    # 점수 = 영향도×10 (+2 해운·항공) (+3 본문 확보). 본문 가산은 동일 영향도 내 타이브레이크 수준
    def total(it):
        return score(it) + (3 if BODY_FIRST and has_body(it) else 0)

    ranked = sorted(items, key=lambda x: (-total(x), x["idx"]))
    chosen = []

    def meets_min(it):
        return it["impact"] >= MIN_IMPACT.get(it["category"], DEFAULT_MIN_IMPACT)

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
        if len(chosen) >= limit:
            break
        if meets_min(it):
            try_add(it, SEA_MAX if it["impact"] >= 5 else SEA_SOFT)

    # 2차: 미달 시 해운 제외 영향도 2 이상으로 채움
    for it in ranked:
        if len(chosen) >= limit:
            break
        if it in chosen or it["category"] == "해운" or it["impact"] < FILL_MIN_IMPACT:
            continue
        try_add(it, SEA_SOFT)
    return chosen


def fallback_items(articles, limit=DAILY_LIMIT):
    return [{"idx": i, "impact": 3, "category": DEFAULT_CATEGORY, "group": "",
             "summary": "AI 분석 미수행, 원문 링크 참조", "insight": "AI 분석 실패로 생략",
             "highlights": []}
            for i in range(min(limit, len(articles)))]


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
    today_str = today_kst()
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


# ===== 최근 적재 이력 (중복·재실행 방지) =====
def fetch_recent_notion():
    """return (최근 제목 리스트, URL 집합, 오늘 적재 건수)"""
    db_id = normalize_db_id(NOTION_DB_RAW)
    headers = {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Content-Type": "application/json",
        "Notion-Version": "2022-06-28",
    }
    since = (datetime.datetime.now(KST) - datetime.timedelta(days=RECENT_DAYS)).strftime("%Y-%m-%d")
    body = {"filter": {"property": "날짜", "date": {"on_or_after": since}}, "page_size": 100}
    titles, urls, today_cnt = [], set(), 0
    try:
        res = requests.post(f"https://api.notion.com/v1/databases/{db_id}/query",
                            headers=headers, json=body, timeout=30)
        if res.status_code != 200:
            print(f"⚠️ 최근 이력 조회 실패 ({res.status_code}): {res.text[:300]}")
            return titles, urls, today_cnt
        today = today_kst()
        for row in res.json().get("results", []):
            props = row.get("properties", {})
            t = "".join(x.get("plain_text", "") for x in props.get("제목", {}).get("title", []))
            u = props.get("URL", {}).get("url")
            d = ((props.get("날짜", {}) or {}).get("date") or {}).get("start", "")
            if t:
                titles.append(t)
            if u:
                urls.add(u)
            if d[:10] == today:
                today_cnt += 1
    except Exception as e:
        print(f"⚠️ 최근 이력 조회 예외: {type(e).__name__}: {e}")
    print(f"   최근 {RECENT_DAYS}일 적재 {len(titles)}건 / 오늘 {today_cnt}건")
    return titles, urls, today_cnt


# ===== 실행 =====
def main():
    missing = [k for k, v in {"ALPHA_API_KEY": ALPHA_API_KEY, "NOTION_TOKEN": NOTION_TOKEN,
                              "NOTION_PARENT_PAGE_ID": NOTION_DB_RAW}.items() if not v]
    if missing:
        sys.exit(f"❌ 환경변수 누락: {missing}")
    print(f"   googlenewsdecoder: {'사용 가능' if gnewsdecoder else '미설치 (requirements.txt 확인)'}")

    print("0. 최근 적재 이력 확인 중...")
    recent_titles, recent_urls, today_cnt = fetch_recent_notion()
    remaining = DAILY_LIMIT - today_cnt
    if remaining <= 0 and not FORCE_RUN:
        print(f"ℹ️ 오늘({today_kst()}) 이미 {today_cnt}건 적재됨 → 종료 (재실행하려면 FORCE_RUN=1)")
        return
    if remaining <= 0:
        remaining = DAILY_LIMIT
    print(f"   오늘 추가 적재 목표: {remaining}건")

    print("1. 뉴스·본문 수집 중...")
    articles = fetch_all_news(recent_titles, recent_urls)
    print(f"   후보 {len(articles)}개")
    if not articles:
        sys.exit("❌ 신규 기사 0건")

    print("2. Alpha AI 분석 중...")
    ai_analysis = analyze_with_alpha_ai(articles)
    items = parse_ai_analysis(ai_analysis, articles) if ai_analysis else []

    if items:
        print("   AI 평가: " + ", ".join(f"{i['idx']+1}:{i['category']}/{i['impact']}" for i in items))
        items = prioritize(items, articles, remaining)
    elif FALLBACK_WITHOUT_AI:
        print("⚠️ AI 결과 없음 → 원문만 적재 (fallback)")
        items = fallback_items(articles, remaining)
    else:
        sys.exit("❌ AI 분석 실패")
    print(f"   적재 대상 {len(items)}건: " + ", ".join(i["category"] for i in items))

    print("3. Notion 적재 중...")
    if save_to_notion(articles, items) == 0:
        sys.exit("❌ Notion 적재 0건")


if __name__ == "__main__":
    main()
