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
 - [업로드 안정화]
   · 실행 초기에 노션 DB 접근·속성 점검 (실패 원인 로그 출력)
   · DB ID 정규화 (URL/하이픈 형태 허용)
   · 제목(Title) 속성명 자동 감지, 분류 select/multi-select 모두 지원
   · AI 실패·응답 파싱 실패 시에도 규칙 기반 선별로 노션 적재 (FALLBACK_WITHOUT_AI)
   · AI 선별 결과가 5건 미만이면 미선택 후보로 보충
   · 노션 429/5xx 재시도
   · 적재 0건·설정 오류 시 exit 1 → Actions 실패(빨간불) 표시
   · 제목 이중 정제로 '아시아-유럽' 같은 제목이 잘리던 버그 수정
   · RSS를 requests로 받아 타임아웃·응답코드 로그 확보
 - [경제 뉴스 5건 추가] 물류 5건과 별도로 경제 기사 5건 선정·적재
   · 수집: 구글 뉴스 '비즈니스' 주요뉴스(구글 중요도 순) + 거시경제 키워드 6종
   · 같은 사건 기사 묶기(제목 2-gram) → 사건별 보도 언론사 수 = 화제성
   · 종합점수 = 경제 영향도(AI, 60) + 보도량(25) + 구글 주요뉴스 순위(15)
   · 영향도 3 미만·같은 주제·세부분류 3건 이상 제외, 노션 상세에 선정 근거 표시
   · AI 실패 시 키워드 기반 영향도로 대체
"""

import os
import re
import sys
import math
import time
import calendar
import urllib.parse
from datetime import datetime, timedelta, timezone

import requests
import feedparser
from bs4 import BeautifulSoup

# --- 구글 뉴스 디코더 (신/구버전 호환) ---
# 0.2.x(최신)는 Python 3.11 이상 필요. 3.10에서는 구버전이 설치되어 원문 URL 복원이 실패함
_GN_IMPORT_ERROR = None
try:
    from googlenewsdecoder import gnewsdecoder as _gn_decode          # 최신 버전
except ImportError as _e1:
    try:
        from googlenewsdecoder import new_decoderv1 as _gn_decode     # 구버전
    except ImportError as _e2:
        _gn_decode = None
        _GN_IMPORT_ERROR = f"{_e1} / {_e2}"
try:
    from importlib.metadata import version as _pkg_version
    GN_VERSION = _pkg_version("googlenewsdecoder")
except Exception:
    GN_VERSION = "미설치"

DECODE_STATS = {"ok": 0, "fail": 0, "logged": 0}   # 원문 URL 복원 통계 (실패 로그는 앞 3건만)


def normalize_db_id(raw):
    """DB URL / 하이픈 포함 UUID / 32자리 ID → 32자리 ID"""
    s = (raw or "").strip().split("?")[0].split("#")[0].rstrip("/")
    s = s.rsplit("/", 1)[-1]
    m = re.search(r"([0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?"
                  r"[0-9a-fA-F]{4}-?[0-9a-fA-F]{12})$", s)
    return m.group(1).replace("-", "").lower() if m else s


# --- 설정 (Environment Variables) ---
ALPHA_API_KEY = os.getenv("ALPHA_API_KEY")
NOTION_TOKEN = os.getenv("NOTION_TOKEN")
DATABASE_ID = normalize_db_id(os.getenv("NOTION_PARENT_PAGE_ID"))   # 반드시 '데이터베이스 ID'여야 함
FALLBACK_WITHOUT_AI = os.getenv("FALLBACK_WITHOUT_AI", "1") != "0"  # AI 실패 시 규칙 기반 적재

KST = timezone(timedelta(hours=9))

NOTION_HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Content-Type": "application/json",
    "Notion-Version": "2022-06-28",
}
BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
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

# 노션 DB 속성 (제목 속성명은 실행 시 자동 감지)
PROPS = {"title": "제목", "date": "날짜", "category": "분류", "summary": "요약", "url": "URL"}
CATEGORY_TYPE = "multi_select"   # 실행 시 DB 스키마에 맞춰 select/multi_select 결정

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

# --- 경제 뉴스 (물류 5건과 별도 5건) ---
ECON_ENABLED = os.getenv("ECON_ENABLED", "1") != "0"
ECON_COUNT = 5
ECON_POOL = 18              # AI 평가 후보(사건) 수
ECON_MIN_IMPACT = 3         # 기본 최소 영향도
ECON_FILL_MIN_IMPACT = 2    # 5건 미달 시 보충 기준
ECON_SIM = 0.45             # 같은 사건 판정 임계값 (제목 2-gram 중첩도)
ECON_EXCLUDE_SIM = 0.7      # 노션 기존 기사·당일 물류 기사와 같은 기사 판정 임계값
ECON_MAX_PER_SUB = 2        # 세부분류별 최대 건수 (증시 기사만 5건 되는 것 방지)
ECON_MAX_AGE_HOURS = 36
ECON_WEIGHTS = {"impact": 60, "coverage": 25, "google_rank": 15}   # 종합점수 100점 만점
ECON_COVERAGE_CAP = 20      # 보도 언론사 20곳 이상이면 보도량 만점
ECON_RANK_CAP = 30          # 구글 주요뉴스 30위 이내만 순위 가산

# 구글 뉴스 '비즈니스' 주요뉴스 (한국판, 구글 중요도 순 정렬 + 관련 보도 묶음 포함)
ECON_TOPIC_FEED = ("https://news.google.com/rss/topics/"
                   "CAAqJggKIiBDQkFTRWdvSUwyMHZNRGx6TVdZU0FtdHZHZ0pMVWlnQVAB?hl=ko&gl=KR&ceid=KR:ko")
ECON_QUERY_FEEDS = [gnews_rss(q) for q in [
    "(금리 OR 기준금리 OR 한국은행 OR 연준 OR 환율)",
    "(코스피 OR 증시 OR 주가 OR 나스닥)",
    "(수출 OR 무역수지 OR 관세 OR 통상)",
    "(물가 OR 유가 OR 경기 OR 성장률 OR GDP)",
    "(부동산 OR 집값 OR 가계부채 OR 대출)",
    "(기획재정부 OR 재정 OR 세제 OR 금융위원회 OR 공정위)",
]]

ECON_SUBCATEGORIES = ["금융·통화", "증시·투자", "무역·통상", "물가·경기",
                      "부동산", "정책·재정", "산업·기업", "글로벌 경제"]
# AI 미사용 시 세부분류·영향도 추정용 키워드 (위에서부터 우선 매칭)
ECON_SUB_KEYWORDS = {
    "금융·통화": ["기준금리", "금리", "한국은행", "한은", "연준", "환율", "원·달러", "원달러", "국채", "채권", "통화"],
    "증시·투자": ["코스피", "코스닥", "증시", "주가", "나스닥", "S&P", "다우", "시총", "공매도", "ETF", "비트코인"],
    "무역·통상": ["수출", "수입", "무역", "관세", "통상", "301조", "FTA", "수출통제"],
    "물가·경기": ["물가", "CPI", "유가", "경기전망", "경기", "성장률", "GDP", "소비심리", "고용", "실업"],
    "부동산": ["부동산", "집값", "아파트", "전세", "분양", "주택", "가계부채", "공시가격"],
    "정책·재정": ["기획재정부", "기재부", "재정", "세제", "추경", "예산", "금융위", "공정위", "금감원"],
    "글로벌 경제": ["미국", "중국", "일본", "유럽", "트럼프", "글로벌"],
}
ECON_LOW_VALUE_KEYWORDS = ["교육감", "교육청", "시위", "간담회", "개최", "협약", "수상", "축제", "기부",
                           "봉사", "행사", "세미나", "포럼", "공모", "인사]", "부고", "운세", "날씨"]


# ============================================================
# 유틸
# ============================================================
def clean_title(title):
    """제목 정제: 끝의 ' - 언론사' 및 앞의 [태그] 삭제
    (하이픈 양옆 공백이 있을 때만 출처로 판단 → '아시아-유럽' 같은 제목 보존)"""
    title = re.sub(r'\s+-\s+[^-]+$', '', title or '').strip()
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

def first_sentence(text, limit=150):
    """규칙 기반 적재용 요약: 본문 첫 문장"""
    text = (text or "").strip().split("\n")[0]
    m = re.search(r"^(.+?[.!?])(\s|$)", text)
    s = m.group(1) if m else text
    return s[:limit]


# ============================================================
# 노션 API
# ============================================================
def notion_request(method, url, **kwargs):
    """노션 API 호출 (429/5xx/네트워크 오류 시 최대 3회 재시도). 실패 시 None 또는 마지막 응답"""
    res = None
    for attempt in range(1, 4):
        try:
            res = requests.request(method, url, headers=NOTION_HEADERS, timeout=30, **kwargs)
        except requests.RequestException as e:
            print(f"⚠️ 노션 요청 오류({attempt}/3): {e}", flush=True)
            res = None
            time.sleep(2 * attempt)
            continue
        if res.status_code == 429 or res.status_code >= 500:
            wait = float(res.headers.get("Retry-After") or 2 * attempt)
            print(f"⚠️ 노션 응답 {res.status_code} → {wait:.0f}초 후 재시도({attempt}/3)", flush=True)
            time.sleep(wait)
            continue
        return res
    return res

NOTION_ERROR_HINTS = {
    400: "DB ID 형식 오류이거나, 페이지 ID를 넣었을 가능성 (NOTION_PARENT_PAGE_ID에는 '데이터베이스 ID' 필요)",
    401: "NOTION_TOKEN 값 오류 (Integration 토큰 재확인)",
    403: "Integration 권한 부족 (Capabilities에서 Read/Insert content 허용 확인)",
    404: "DB를 찾을 수 없음 → DB 우측 상단 ⋯ → Connections에 Integration 추가했는지, "
         "DB ID가 맞는지(링크된 DB 뷰가 아닌 원본 DB인지) 확인",
}

def check_notion_db():
    """노션 DB 접근 및 속성 점검. 성공 시 PROPS/CATEGORY_TYPE 갱신 후 True"""
    global CATEGORY_TYPE
    print(f"노션 DB 점검: ID {DATABASE_ID[:6]}…{DATABASE_ID[-4:]} (길이 {len(DATABASE_ID)})", flush=True)
    if not re.fullmatch(r"[0-9a-f]{32}", DATABASE_ID):
        print("❌ NOTION_PARENT_PAGE_ID가 32자리 ID 형식이 아님. DB URL의 32자리 ID를 등록할 것.", flush=True)
        return False

    res = notion_request("GET", f"https://api.notion.com/v1/databases/{DATABASE_ID}")
    if res is None:
        print("❌ 노션 API 접속 실패 (네트워크)", flush=True)
        return False
    if res.status_code != 200:
        print(f"❌ 노션 DB 접근 실패 ({res.status_code}): {res.text[:300]}", flush=True)
        hint = NOTION_ERROR_HINTS.get(res.status_code)
        if hint:
            print(f"   → 조치: {hint}", flush=True)
        return False

    props = res.json().get("properties", {})
    types = {name: p.get("type") for name, p in props.items()}
    print(f"노션 DB 속성: {types}", flush=True)

    # 제목(Title) 속성은 DB마다 1개 → 이름이 달라도 자동 사용
    title_names = [n for n, t in types.items() if t == "title"]
    if title_names and title_names[0] != PROPS["title"]:
        print(f"ℹ️ 제목 속성명 '{title_names[0]}' 자동 사용", flush=True)
        PROPS["title"] = title_names[0]

    problems = []
    expected = {"date": ("date",), "category": ("multi_select", "select"),
                "summary": ("rich_text",), "url": ("url",)}
    for key, allowed in expected.items():
        name = PROPS[key]
        if name not in types:
            problems.append(f"'{name}' 속성 없음 (필요 타입: {'/'.join(allowed)})")
        elif types[name] not in allowed:
            problems.append(f"'{name}' 속성 타입이 {types[name]} → {'/'.join(allowed)} 필요")
    if problems:
        for p in problems:
            print(f"❌ 노션 DB 속성 불일치: {p}", flush=True)
        return False

    CATEGORY_TYPE = types[PROPS["category"]]
    print(f"✅ 노션 DB 점검 통과 (분류 타입: {CATEGORY_TYPE})", flush=True)
    return True


def fetch_existing_from_notion(days=DEDUP_LOOKBACK_DAYS):
    """최근 N일 동안 노션에 저장된 URL/제목 집합 반환"""
    url = f"https://api.notion.com/v1/databases/{DATABASE_ID}/query"
    since = (datetime.now(KST) - timedelta(days=days)).strftime("%Y-%m-%d")
    body = {"filter": {"property": PROPS["date"], "date": {"on_or_after": since}}, "page_size": 100}
    urls, titles = set(), set()

    while True:
        res = notion_request("POST", url, json=body)
        if res is None or res.status_code != 200:
            detail = f"({res.status_code}): {res.text[:200]}" if res is not None else "(네트워크)"
            print(f"⚠️ 노션 기존 기사 조회 실패{detail} → 중복 체크 없이 진행", flush=True)
            break
        data = res.json()
        for page in data.get("results", []):
            props = page.get("properties", {})
            u = (props.get(PROPS["url"]) or {}).get("url")
            if u:
                urls.add(u)
            t = (props.get(PROPS["title"]) or {}).get("title", [])
            if t:
                titles.add(norm_title("".join(x.get("plain_text", "") for x in t)))
        if not data.get("has_more"):
            break
        body["start_cursor"] = data["next_cursor"]

    print(f"노션 기존 기사 로드: URL {len(urls)}건 / 제목 {len(titles)}건", flush=True)
    return urls, titles


# ============================================================
# 원문 URL 복원 및 본문 추출
# ============================================================
def decode_google_news_url(url):
    """구글 뉴스 RSS 링크를 실제 언론사 원문 URL로 변환"""
    if "news.google.com" not in url:
        return url
    if _gn_decode:
        try:
            r = _gn_decode(url, interval=1)
            # 구버전 {"status": True, ...} / 최신 {"success": True, ...} 모두 지원
            if isinstance(r, dict) and (r.get("success") or r.get("status")) and r.get("decoded_url"):
                DECODE_STATS["ok"] += 1
                return r["decoded_url"]
            msg = f"⚠️ 원문 URL 복원 실패: {r}"
        except Exception as e:
            msg = f"⚠️ 원문 URL 복원 오류: {type(e).__name__}: {e}"
        DECODE_STATS["fail"] += 1
        if DECODE_STATS["logged"] < 3:
            DECODE_STATS["logged"] += 1
            print(msg, flush=True)
    else:
        DECODE_STATS["fail"] += 1
    return url

def extract_article_details(entry):
    """RSS 항목 → (원문 URL, 본문)"""
    return extract_from_link(entry.link, getattr(entry, "summary", ""))

def extract_from_link(link, summary_html=""):
    """뉴스 원문 URL 추적 및 본문 텍스트 수집"""
    rss_snippet = ""
    if summary_html:
        rss_snippet = BeautifulSoup(summary_html, "html.parser").get_text().strip()

    real_url = decode_google_news_url(link)
    article_text = ""

    try:
        res = requests.get(real_url, headers=BROWSER_HEADERS, timeout=10, allow_redirects=True)
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
            article_text = ""   # 구글 중간 페이지 → 본문 아님 (RSS 요약으로 대체)
    except Exception as e:
        print(f"원문 크롤링 참고 ({real_url[:80]}): {e}", flush=True)

    if not article_text or len(article_text) < 50:
        article_text = rss_snippet if rss_snippet else "원문 링크 참조 필요"

    return real_url, article_text


# ============================================================
# 후보 수집
# ============================================================
def fetch_feed(url, cat):
    """RSS 다운로드(타임아웃·응답코드 확인) 후 파싱"""
    try:
        res = requests.get(url, headers=BROWSER_HEADERS, timeout=20)
    except requests.RequestException as e:
        print(f"⚠️ [{cat}] RSS 요청 오류: {e}", flush=True)
        return []
    if res.status_code != 200:
        print(f"⚠️ [{cat}] RSS 응답 {res.status_code}: {url[:100]}", flush=True)
        return []
    feed = feedparser.parse(res.content)
    if getattr(feed, "bozo", 0) and not feed.entries:
        print(f"⚠️ [{cat}] RSS 파싱 실패: {feed.get('bozo_exception')}", flush=True)
    return feed.entries

def collect_candidate_news(seen_urls, seen_titles):
    candidates = []
    used_titles = set(seen_titles)
    used_urls = set(seen_urls)
    now = datetime.now(timezone.utc)
    oldest = datetime.min.replace(tzinfo=timezone.utc)

    for cat, urls in RSS_SOURCES.items():
        entries, seen_links = [], set()
        for u in urls:
            for e in fetch_feed(u, cat):
                if e.get("link") and e.link not in seen_links:
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

            title = clean_title(e.get("title", ""))
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
            response = requests.post(LLM_URL, headers=headers, json=payload, timeout=(10, 120))
            if response.status_code == 200:
                try:
                    result = response.json()
                except ValueError:
                    result = {}
                if result.get("choices"):
                    content = result["choices"][0]["message"]["content"] or ""
                    content = re.sub(r"```[a-zA-Z]*", "", content).replace("```", "").strip()
                    if content:
                        print(f"SUCCESS: [{model_name}] 응답 수신", flush=True)
                        return content
            # 실패 원인 파악용 응답 일부 출력 (방화벽 차단 페이지, 인증 오류 등)
            body = re.sub(r"\s+", " ", response.text or "")[:200]
            print(f"FAIL: [{model_name}] 응답 코드 {response.status_code} / 본문: {body}", flush=True)
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

FIELD_RE = re.compile(r"^(후보번호|분류|경제관련|영향도|세부분류|주제|요약|시사점|하이라이트)\s*[:：]\s*(.*)$")
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
def rule_based_item(idx, cand):
    """AI 없이 적재할 항목 (본문 첫 문장을 요약으로 사용)"""
    return {
        "후보번호": idx,
        "final_cat": cand["category"],
        "요약": first_sentence(cand["content"]) or cand["title"],
        "시사점": "AI 분석 미수행 – 원문 확인 필요",
        "하이라이트": [],
        "rule_based": True,
    }

def fill_rule_based(selected, candidates, target=TARGET_COUNT):
    """선별 결과가 target 미만이면 미선택 후보로 보충
    (카테고리 순환 → 다양성 확보, 해상 상한 준수, 해상 0건이면 해상 우선)"""
    used = {d["후보번호"] for d in selected}
    m_count = sum(1 for d in selected if d["final_cat"] == "해상")

    by_cat = {}
    for i, c in enumerate(candidates, start=1):
        if i not in used:
            by_cat.setdefault(c["category"], []).append(i)
    order = list(RSS_SOURCES.keys())
    if m_count >= MARITIME_MIN and "해상" in order:     # 해상 충족 시 다른 분야 먼저
        order.remove("해상")
        order.append("해상")

    added = 0
    while len(selected) < target and any(by_cat.values()):
        for cat in order:
            if len(selected) >= target:
                break
            if not by_cat.get(cat):
                continue
            if cat == "해상" and m_count >= MARITIME_MAX:
                by_cat[cat] = []
                continue
            idx = by_cat[cat].pop(0)
            selected.append(rule_based_item(idx, candidates[idx - 1]))
            added += 1
            if cat == "해상":
                m_count += 1
    if added:
        print(f"ℹ️ 규칙 기반 보충 {added}건", flush=True)
    return selected

def select_articles(candidates):
    """AI 선별. AI 호출/파싱 실패 시 빈 리스트"""
    indexed = list(enumerate(candidates, start=1))
    content = call_llm(build_prompt(indexed, TARGET_COUNT, maritime_rule=True))
    if not content:
        print("⚠️ 모든 AI 모델 호출 실패", flush=True)
        return []

    selected, used_idx = [], set()
    for d in parse_articles(content):
        idx = d["후보번호"]
        if not (1 <= idx <= len(candidates)) or idx in used_idx:
            continue
        used_idx.add(idx)
        d["final_cat"] = sanitize_category(d.get("분류") or candidates[idx - 1]["category"])
        selected.append(d)

    if not selected:
        print(f"⚠️ AI 응답 파싱 결과 0건. 응답 앞부분: {content[:300]!r}", flush=True)
        return []

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
            forced = parsed[0] if parsed else rule_based_item(idx, cand)
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
def add_to_notion(title, url, categories, summary, implication, highlights, article_content, basis=None):
    """categories: 분류 태그 리스트 (select 타입 DB면 첫 번째만 사용)
    basis: 선정 근거 문구 (경제 기사용, 상단 회색 callout)"""
    if isinstance(categories, str):
        categories = [categories]
    category = categories[0]
    children = []
    if basis:
        children.append({"object": "block", "type": "callout", "callout": {
            "rich_text": rt(basis), "icon": {"emoji": "📊"}, "color": "gray_background"}})
    children.append(
        {"object": "block", "type": "callout", "callout": {
            "rich_text": rt(f"AI 시사점\n{implication}"),
            "icon": {"emoji": "💡"}, "color": "yellow_background"}})
    highlights = [h for h in highlights if h.strip()]
    if highlights:
        children.append({"object": "block", "type": "heading_3",
                         "heading_3": {"rich_text": rt("📌 핵심 하이라이트")}})
        for hl in highlights:
            children.append({"object": "block", "type": "bulleted_list_item",
                             "bulleted_list_item": {"rich_text": rt(hl)}})
    children.extend([
        {"object": "block", "type": "divider", "divider": {}},
        {"object": "block", "type": "heading_3", "heading_3": {"rich_text": rt("📄 수집된 원문 내용")}},
        {"object": "block", "type": "paragraph", "paragraph": {"rich_text": rt(article_content, 1500)}},
    ])

    if CATEGORY_TYPE == "select":
        category_value = {"select": {"name": category}}
    else:
        category_value = {"multi_select": [{"name": c} for c in dict.fromkeys(categories) if c]}

    payload = {
        "parent": {"database_id": DATABASE_ID},
        "properties": {
            PROPS["title"]: {"title": [{"text": {"content": (title or "(제목 없음)")[:2000]}}]},
            PROPS["date"]: {"date": {"start": datetime.now(KST).strftime("%Y-%m-%d")}},
            PROPS["category"]: category_value,
            PROPS["summary"]: {"rich_text": [{"text": {"content": (summary or "")[:2000]}}]},
            PROPS["url"]: {"url": url or None},
        },
        "children": children,
    }

    res = notion_request("POST", "https://api.notion.com/v1/pages", json=payload)
    if res is None:
        print(f"❌ 노션 요청 오류(네트워크): {title[:25]}", flush=True)
        return False
    if res.status_code == 200:
        print(f"✅ 노션 등록 성공: [{'/'.join(categories)}] {title[:25]}...", flush=True)
        return True
    print(f"❌ 노션 등록 실패 ({res.status_code}): {res.text[:300]}", flush=True)
    return False

# ============================================================
# 경제 뉴스 (추가 5건): 경제 영향도 + 화제성 기반 순위
# ============================================================
def bigrams(text):
    t = norm_title(text)
    return {t[i:i + 2] for i in range(len(t) - 1)}

def overlap(a, b):
    """제목 2-gram 중첩도 (짧은 쪽 기준, 0~1)"""
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))

def entry_source(e):
    src = (e.get("source") or {}).get("title")
    if src:
        return src
    m = re.search(r"\s+-\s+([^-]+)$", e.get("title", ""))
    return m.group(1).strip() if m else ""

def keyword_sub(title):
    for sub, kws in ECON_SUB_KEYWORDS.items():
        if any(k in title for k in kws):
            return sub
    return "산업·기업"

def heuristic_impact(story):
    """AI 미사용 시 영향도 추정: 거시 키워드·보도량·주요뉴스 순위 기반 (1~5)"""
    title = story["rep"]["title"]
    macro = any(k in title for sub, kws in ECON_SUB_KEYWORDS.items()
                if sub != "글로벌 경제" for k in kws)
    score = 3 if macro else 2
    if story["coverage"] >= 10 or (story["g_rank"] and story["g_rank"] <= 5):
        score += 1
    if any(k in title for k in ECON_LOW_VALUE_KEYWORDS):
        score -= 1
    return max(1, min(5, score))

def econ_score(impact, coverage, g_rank):
    """종합점수(100점) = 영향도 60 + 보도량 25 + 구글 주요뉴스 순위 15"""
    w = ECON_WEIGHTS
    s = w["impact"] * impact / 5
    s += w["coverage"] * min(1.0, math.log1p(coverage) / math.log1p(ECON_COVERAGE_CAP))
    if g_rank and g_rank <= ECON_RANK_CAP:
        s += w["google_rank"] * (ECON_RANK_CAP - g_rank + 1) / ECON_RANK_CAP
    return round(s, 1)

def collect_econ_stories(exclude_urls, exclude_titles):
    """경제 기사 수집 → 같은 사건끼리 묶어 사건(story) 단위로 반환 (사전점수 순)"""
    now = datetime.now(timezone.utc)
    max_age = timedelta(hours=ECON_MAX_AGE_HOURS)
    items, seen_links = [], set()

    def add_item(raw_title, link, source, summary, g_rank, pub):
        title = clean_title(raw_title)
        if not link or link in seen_links or len(norm_title(title)) < 10:
            return
        seen_links.add(link)
        items.append({"title": title, "link": link, "source": source or "",
                      "summary": summary or "", "g_rank": g_rank, "pub": pub, "bg": bigrams(title)})

    # 1) 구글 '비즈니스' 주요뉴스: 순위 + 묶음 내 관련 보도(다른 언론사)
    topic_entries = fetch_feed(ECON_TOPIC_FEED, "경제·주요뉴스")
    for rank, e in enumerate(topic_entries, start=1):
        pub = entry_time(e)
        if pub and now - pub > max_age:
            continue
        add_item(e.get("title", ""), e.get("link"), entry_source(e), e.get("summary", ""), rank, pub)
        for li in BeautifulSoup(e.get("summary", ""), "html.parser").find_all("li"):
            a, f = li.find("a"), li.find("font")
            if a and a.get("href"):
                add_item(a.get_text(), a["href"], f.get_text(strip=True) if f else "", "", None, pub)
    # 2) 거시경제 키워드 검색
    n_query = 0
    for u in ECON_QUERY_FEEDS:
        for e in fetch_feed(u, "경제·키워드"):
            pub = entry_time(e)
            if pub and now - pub > max_age:
                continue
            add_item(e.get("title", ""), e.get("link"), entry_source(e), e.get("summary", ""), None, pub)
            n_query += 1
    print(f"[경제] 주요뉴스 {len(topic_entries)}건 / 키워드 {n_query}건 → 기사 {len(items)}건", flush=True)

    # 3) 같은 사건 묶기 (제목 2-gram 중첩도)
    stories = []
    for it in items:
        for st in stories:
            if any(overlap(it["bg"], m["bg"]) >= ECON_SIM for m in st["members"][:6]):
                st["members"].append(it)
                break
        else:
            stories.append({"members": [it]})

    # 4) 사건별 지표 + 기존 노션/당일 물류 기사 제외
    excl_bg = [bigrams(t) for t in exclude_titles if t]
    result, excluded = [], 0
    for st in stories:
        ms = st["members"]
        ranked = [m for m in ms if m["g_rank"]]
        st["rep"] = min(ranked, key=lambda m: m["g_rank"]) if ranked else ms[0]
        st["g_rank"] = st["rep"]["g_rank"]
        st["coverage"] = len({m["source"] for m in ms if m["source"]}) or 1
        if (any(m["link"] in exclude_urls for m in ms)
                or any(overlap(st["rep"]["bg"], b) >= ECON_EXCLUDE_SIM for b in excl_bg)):
            excluded += 1
            continue
        st["prescore"] = econ_score(heuristic_impact(st), st["coverage"], st["g_rank"])
        result.append(st)

    result.sort(key=lambda s: s["prescore"], reverse=True)
    print(f"[경제] 사건 {len(stories)}개 (기존/물류 중복 제외 {excluded}) → 평가 후보 {min(len(result), ECON_POOL)}개",
          flush=True)
    return result[:ECON_POOL]

def build_econ_prompt(pool):
    payload = ""
    for i, st in enumerate(pool, start=1):
        payload += (f"---후보 기사 {i}---\n제목: {st['rep']['title']}\n"
                    f"보도 언론사 수: {st['coverage']}\n본문: {st['content'][:500]}\n\n")
    subs = ", ".join(ECON_SUBCATEGORIES)
    return f"""
당신은 거시경제·금융시장 전문 애널리스트입니다. 아래 후보 기사 {len(pool)}개를 '모두' 평가하세요.

[후보 기사 목록]
{payload}
[영향도 기준] 한국 경제·금융시장·기업 경영에 미치는 실질 영향
5: 금리·환율·증시·유가 급변, 국가 단위 정책·통상 조치, 주요 거시지표 충격 → 즉시 주목 필요
4: 업종 전반·주요 지표에 중기적으로 유의미한 영향
3: 참고할 경제 동향
2: 영향 제한적 (개별 기업 소식, 지역·행사·홍보성)
1: 경제와 무관 (연예·가십·사건사고 등)

[작성 규칙]
1. 경제관련: 예 / 아니오
2. 영향도: 1~5 숫자만
3. 세부분류: 다음 중 1개 [{subs}]
4. 주제: 같은 사건이면 같은 문구가 되도록 핵심 주제를 10자 내외로 (예: 엔비디아 CEO 복귀)
5. 요약: 핵심 내용 1문장 (명사형 어미)
6. 시사점: 한국 경제·시장 관점 영향 1문장 (명사형 어미)
7. 하이라이트: 핵심 포인트 3개 (명사형 어미)
8. 마크다운 굵게(**), 제목(#) 사용 금지. 평가 근거 설명 없이 결과 양식만 출력.

[응답 양식]
---ARTICLE---
후보번호: (숫자)
경제관련: (예/아니오)
영향도: (1~5)
세부분류: (택1)
주제: (10자 내외)
요약: (1문장)
시사점: (1문장)
하이라이트:
- (포인트 1)
- (포인트 2)
- (포인트 3)
"""

def evaluate_econ(pool):
    """AI 평가 (실패 시 키워드 기반) → 각 story에 impact/sub/topic/summary 등 기록"""
    ai = {}
    if ALPHA_API_KEY and pool:
        content = call_llm(build_econ_prompt(pool))
        if content:
            for d in parse_articles(content):
                idx = d.get("후보번호")
                if idx and 1 <= idx <= len(pool) and idx not in ai:
                    ai[idx] = d
        print(f"[경제] AI 평가 {len(ai)}/{len(pool)}건", flush=True)

    for i, st in enumerate(pool, start=1):
        d = ai.get(i)
        title = st["rep"]["title"]
        if d:
            m = re.search(r"[1-5]", d.get("영향도", ""))
            impact = int(m.group()) if m else heuristic_impact(st)
            if (d.get("경제관련") or "").strip().startswith("아니"):
                impact = 1
            sub = next((s for s in ECON_SUBCATEGORIES if s in (d.get("세부분류") or "")), keyword_sub(title))
            st.update(impact=impact, sub=sub, ai=True,
                      topic=d.get("주제") or title,
                      summary=d.get("요약") or first_sentence(st["content"]) or title,
                      implication=d.get("시사점") or "",
                      highlights=d.get("하이라이트") or [])
        else:
            st.update(impact=heuristic_impact(st), sub=keyword_sub(title), ai=False, topic=title,
                      summary=first_sentence(st["content"]) or title,
                      implication="AI 분석 미수행 – 원문 확인 필요", highlights=[])
        st["score"] = econ_score(st["impact"], st["coverage"], st["g_rank"])
    return pool

def rank_econ(pool):
    """종합점수 순 선정: 최소 영향도 → 주제 중복 제외 → 세부분류 상한. 미달 시 보충 기준으로 재시도"""
    ordered = sorted(pool, key=lambda s: (s["score"], s["coverage"]), reverse=True)
    selected = []

    def is_dup(st):
        tb, rb = bigrams(st["topic"]), st["rep"]["bg"]
        return any(norm_title(st["topic"]) == norm_title(x["topic"])
                   or overlap(tb, bigrams(x["topic"])) >= 0.6
                   or overlap(rb, x["rep"]["bg"]) >= ECON_SIM for x in selected)

    for min_impact in (ECON_MIN_IMPACT, ECON_FILL_MIN_IMPACT):
        for st in ordered:
            if len(selected) >= ECON_COUNT:
                break
            if st in selected or st["impact"] < min_impact or is_dup(st):
                continue
            if sum(1 for x in selected if x["sub"] == st["sub"]) >= ECON_MAX_PER_SUB:
                continue
            if min_impact < ECON_MIN_IMPACT:
                print(f"ℹ️ [경제] 영향도 {st['impact']} 기사로 보충: {st['rep']['title'][:30]}", flush=True)
            selected.append(st)
    return selected

def econ_basis(st):
    parts = [f"경제 영향도 {st['impact']}/5" + ("" if st["ai"] else "(키워드 추정)"),
             f"관련 보도 {st['coverage']}곳"]
    if st["g_rank"]:
        parts.append(f"구글 비즈니스 주요뉴스 {st['g_rank']}위")
    w = ECON_WEIGHTS
    return (f"선정 근거: {' · '.join(parts)} → 종합 {st['score']}점\n"
            f"(종합점수 = 영향도 {w['impact']} + 보도량 {w['coverage']} + 주요뉴스 순위 {w['google_rank']}, 100점 만점)")

def run_econ(exclude_urls, exclude_titles):
    """경제 기사 5건 선정·적재. 반환: (저장 건수, 시도 건수)"""
    print("\n===== 경제 뉴스 선정 =====", flush=True)
    pool = collect_econ_stories(exclude_urls, exclude_titles)
    if not pool:
        print("❌ [경제] 후보 0건. 경제 RSS 점검 필요.", flush=True)
        return 0, 0

    # 원문 URL 복원·본문 수집 (복원 후 기존 기사와 URL 중복이면 제외)
    kept = []
    for st in pool:
        st["url"], st["content"] = extract_from_link(st["rep"]["link"], st["rep"]["summary"])
        if st["url"] in exclude_urls:
            continue
        kept.append(st)

    selected = rank_econ(evaluate_econ(kept))
    print("[경제] 선정 순위:", flush=True)
    for i, st in enumerate(selected, start=1):
        print(f"  {i}. {st['score']}점 (영향도 {st['impact']}, 보도 {st['coverage']}곳, "
              f"주요뉴스 {st['g_rank'] or '-'}위, {st['sub']}) {st['rep']['title'][:40]}", flush=True)

    saved = 0
    for st in selected:
        ok = add_to_notion(
            st["rep"]["title"], st["url"], ["경제", st["sub"]],
            st["summary"], st["implication"], st["highlights"], st["content"],
            basis=econ_basis(st),
        )
        saved += int(ok)
    ai_cnt = sum(1 for st in selected if st["ai"])
    print(f"[경제] 선정 {len(selected)}건(AI 평가 {ai_cnt}) / 저장 {saved}건 / 실패 {len(selected) - saved}건",
          flush=True)
    return saved, len(selected)


# ============================================================
# main
# ============================================================
def run_logistics(seen_urls, seen_titles):
    """물류 기사 5건 선정·적재. 반환: (저장 건수, 시도 건수, 사용 후보 목록)"""
    print("\n===== 물류 뉴스 선정 =====", flush=True)
    candidates = collect_candidate_news(seen_urls, seen_titles)
    if not candidates:
        print("❌ 신규 후보 뉴스 0건. 소스 쿼리 점검 필요.", flush=True)
        return 0, 0, []
    if len(candidates) < TARGET_COUNT:
        print(f"⚠️ 신규 후보 {len(candidates)}건뿐. 소스 쿼리 점검 권고.", flush=True)

    print(f"총 {len(candidates)}개 신규 후보 수집 완료. AI 선별 시작.", flush=True)

    selected = select_articles(candidates) if ALPHA_API_KEY else []
    if not selected:
        if not FALLBACK_WITHOUT_AI:
            print("❌ AI 분석 결과 없음 (FALLBACK_WITHOUT_AI=0 → 적재 중단)", flush=True)
            return 0, 0, candidates
        print("⚠️ AI 분석 결과 없음 → 규칙 기반 선별로 적재", flush=True)
    if FALLBACK_WITHOUT_AI:
        selected = fill_rule_based(selected, candidates)

    saved = 0
    for d in selected:
        orig = candidates[d["후보번호"] - 1]
        ok = add_to_notion(
            orig["title"],
            orig["link"],
            [d["final_cat"]],
            d.get("요약", ""),
            d.get("시사점", ""),
            d["하이라이트"] or ([] if d.get("rule_based") else ["본문 내용 참조"]),
            orig["content"],
        )
        saved += int(ok)

    m_cnt = sum(1 for d in selected if d["final_cat"] == "해상")
    ai_cnt = sum(1 for d in selected if not d.get("rule_based"))
    print(f"선별 {len(selected)}건(AI {ai_cnt} / 규칙 {len(selected) - ai_cnt}) / "
          f"저장 {saved}건 / 실패 {len(selected) - saved}건 / 해상 {m_cnt}건 완료.", flush=True)
    return saved, len(selected), candidates


def main():
    """반환값: 프로세스 종료 코드 (0 정상 / 1 실패 → Actions 빨간불)"""
    missing = [k for k, v in {"ALPHA_API_KEY": ALPHA_API_KEY, "NOTION_TOKEN": NOTION_TOKEN,
                              "NOTION_PARENT_PAGE_ID": DATABASE_ID}.items() if not v]
    if "NOTION_TOKEN" in missing or "NOTION_PARENT_PAGE_ID" in missing:
        print(f"❌ 환경 변수 누락: {', '.join(missing)} (GitHub Secrets 확인)", flush=True)
        return 1
    if "ALPHA_API_KEY" in missing:
        print("⚠️ ALPHA_API_KEY 누락 → AI 없이 규칙 기반으로만 적재", flush=True)
    print(f"Python {sys.version.split()[0]} / googlenewsdecoder {GN_VERSION}", flush=True)
    if _gn_decode is None:
        print(f"⚠️ googlenewsdecoder 로드 실패({_GN_IMPORT_ERROR}): 원문 URL 복원 불가 → 본문 수집 품질 저하",
              flush=True)

    if not check_notion_db():
        return 1

    seen_urls, seen_titles = fetch_existing_from_notion()

    log_saved, log_tried, log_cands = run_logistics(seen_urls, seen_titles)

    econ_saved = econ_tried = 0
    if ECON_ENABLED:
        # 오늘 물류 후보로 쓴 기사는 경제 쪽에서 제외 (같은 기사 이중 적재 방지)
        excl_urls = set(seen_urls) | {c["link"] for c in log_cands}
        excl_titles = set(seen_titles) | {norm_title(c["title"]) for c in log_cands}
        econ_saved, econ_tried = run_econ(excl_urls, excl_titles)

    print(f"\n===== 결과: 물류 {log_saved}/{log_tried}건 · 경제 {econ_saved}/{econ_tried}건 저장 =====",
          flush=True)
    total = DECODE_STATS["ok"] + DECODE_STATS["fail"]
    if total:
        print(f"원문 URL 복원 {DECODE_STATS['ok']}/{total}건"
              + ("  ← ⚠️ 복원 실패 많음: 본문 대신 RSS 요약 저장됨" if DECODE_STATS["ok"] < total / 2 else ""),
              flush=True)
    failed = []
    if log_saved == 0:
        failed.append("물류")
    if ECON_ENABLED and econ_saved == 0:
        failed.append("경제")
    if failed:
        print(f"❌ 노션 적재 0건: {', '.join(failed)}", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
