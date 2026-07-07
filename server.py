#!/usr/bin/env python3
"""트렌드 뷰어 로컬 서버 — 유튜브/쇼츠/릴스 인기 영상과 AI 영상 소식을 제공합니다.
외부 패키지 없이 파이썬 표준 라이브러리만 사용합니다.
실행: python3 server.py  →  http://localhost:8778
"""
import base64
import email.utils
import json
import os
import re
import secrets
import threading
import time
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, quote

PORT = 8778
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_TTL = 3600  # 1시간 캐시 (새로고침 버튼으로 강제 갱신 가능)
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# 서버가 켜질 때마다 새로 만드는 CSRF 토큰. index.html에 심어져 내려가고,
# 계정 목록을 바꾸는 POST 요청은 이 토큰이 헤더에 있어야만 처리됩니다.
# (다른 웹사이트가 방문자 브라우저를 통해 localhost로 몰래 요청을 보내는 것을 차단)
CSRF_TOKEN = secrets.token_hex(16)
# DNS 리바인딩 방어: localhost 이외의 Host 헤더로 들어온 요청은 모두 거부합니다.
ALLOWED_HOSTS = {"localhost", "127.0.0.1", "[::1]", "::1"}

_cache = {}
_cache_lock = threading.Lock()
# 썸네일 프록시 메모리 캐시 (url -> (content_type, bytes))
_img_cache = {}
_img_lock = threading.Lock()
IMG_CACHE_MAX = 600

# ---------------------------------------------------------------- 유튜브
# 카테고리 → 유튜브 검색어 매핑
CATEGORIES = {
    "먹방": "먹방",
    "뷰티/패션": "뷰티 메이크업 패션",
    "브이로그": "브이로그",
    "예능/코미디": "예능 웃긴 영상",
    "영화/드라마": "영화 드라마 리뷰",
    "테크/IT": "테크 리뷰",
    "지식/교육": "지식 교양",
    "자기계발": "자기계발 동기부여",
    "여행": "여행",
    "동물": "강아지 고양이",
}
# 해외 지역 선택 시 같은 카테고리를 현지 언어 검색어로 바꿔 현지 콘텐츠를 가져옵니다.
CATEGORIES_I18N = {
    "US": {
        "먹방": "mukbang", "뷰티/패션": "beauty makeup fashion", "브이로그": "vlog",
        "예능/코미디": "funny comedy videos", "영화/드라마": "movie review",
        "테크/IT": "tech review", "지식/교육": "educational explained",
        "자기계발": "self improvement motivation productivity",
        "여행": "travel vlog", "동물": "dogs cats",
    },
    "JP": {
        "먹방": "モッパン 大食い", "뷰티/패션": "メイク 美容", "브이로그": "vlog 日常",
        "예능/코미디": "お笑い 面白い", "영화/드라마": "映画 レビュー",
        "테크/IT": "ガジェット レビュー", "지식/교육": "教養 解説",
        "자기계발": "自己啓発 モチベーション", "여행": "旅行", "동물": "犬 猫",
    },
}
REGIONS = ("KR", "US", "JP")
# "전체" 탭은 아래 카테고리들을 합쳐 조회수순으로 재정렬
ALL_MERGE = ["먹방", "브이로그", "예능/코미디", "뷰티/패션", "영화/드라마", "자기계발", "여행"]


def category_query(category: str, region: str) -> str:
    if region != "KR" and category in CATEGORIES_I18N.get(region, {}):
        return CATEGORIES_I18N[region][category]
    return CATEGORIES.get(category, category)

# 검색 필터 protobuf: 업로드 날짜 (2=오늘, 3=이번 주, 4=이번 달)
# "어제(yesterday)"는 유튜브에 전용 필터가 없어 '이번 주'로 받은 뒤
# 게시일 텍스트가 "1일 전"인 영상만 남기는 방식으로 처리합니다.
PERIOD_CODE = {"day": 2, "yesterday": 3, "week": 3, "month": 4}

# 검색 결과에 섞여 오는 추천 섹션 영상이 기간 필터를 우회하는 경우를 걸러내기 위한
# 기간별 제외 문구 ("N일 전" 형태의 게시일 텍스트 기준)
PERIOD_EXCLUDE = {
    "day": ("일 전", "주 전", "개월 전", "년 전"),
    "week": ("주 전", "개월 전", "년 전"),
    "month": ("개월 전", "년 전"),
}

# ---------------------------------------------------------------- 유튜브 채널 추적
YT_CHANNELS_FILE = os.path.join(BASE_DIR, "yt_channels.json")
DEFAULT_YT_CHANNELS = []  # 화면에서 벤치마킹할 채널 핸들(@이름)을 직접 추가

# ---------------------------------------------------------------- 인스타그램 릴스
IG_APP_ID = "936619743392459"  # instagram.com 웹이 쓰는 공개 앱 ID
ACCOUNTS_FILE = os.path.join(BASE_DIR, "reels_accounts.json")
DEFAULT_IG_ACCOUNTS = [
    "openai", "runwayapp", "pika_labs", "lumalabsai", "midjourney",
    "klingai_official", "heygen_official", "higgsfield.ai", "googledeepmind",
]

# ---------------------------------------------------------------- X (트위터)
X_ACCOUNTS_FILE = os.path.join(BASE_DIR, "x_accounts.json")
DEFAULT_X_ACCOUNTS = [
    "OpenAI", "runwayml", "Kling_ai", "GoogleDeepMind", "midjourney",
    "LumaLabsAI", "pika_labs", "heygen_com", "elevenlabsio", "AIatMeta",
]

# ---------------------------------------------------------------- 스레드(Threads)
THREADS_ACCOUNTS_FILE = os.path.join(BASE_DIR, "threads_accounts.json")
DEFAULT_THREADS_ACCOUNTS = [
    "openai", "runway", "google", "meta.ai", "zuck",
]
IG_APP_ID_THREADS = "238260118697367"  # threads.com 웹이 쓰는 공개 앱 ID

# ---------------------------------------------------------------- 틱톡(TikTok)
# tikwm 무료 공개 API가 서명(X-Bogus/msToken)을 대신 처리해 조회수·좋아요·댓글까지 반환합니다.
TIKTOK_ACCOUNTS_FILE = os.path.join(BASE_DIR, "tiktok_accounts.json")
DEFAULT_TIKTOK_ACCOUNTS = [
    "openai", "runwayapp", "krea.ai", "elevenlabs", "sora",
    "zachking", "khaby.lame", "google",
]
TIKWM_BASE = "https://www.tikwm.com/api"
TIKTOK_REGION = "KR"

# ---------------------------------------------------------------- AI 영상 탭
AI_YT_QUERIES = ["AI 영상 제작", "AI 영상 생성", "sora ai video", "runway kling veo"]
NEWS_FEEDS = [
    ("국내", "https://news.google.com/rss/search?q=" +
     quote('AI 영상 생성 OR "AI 비디오" OR 영상생성모델') + "&hl=ko&gl=KR&ceid=KR:ko"),
    ("해외", "https://news.google.com/rss/search?q=" +
     quote('"AI video" model OR Sora OR Runway OR Kling OR Veo') + "&hl=en-US&gl=US&ceid=US:en"),
]
HF_PIPELINES = ["text-to-video", "image-to-video"]
# 이미지 프록시로 가져올 수 있는 호스트 (핫링크/차단 우회용)
IMG_PROXY_ALLOW = (".cdninstagram.com", ".fbcdn.net", ".ytimg.com",
                   ".googleusercontent.com", ".twimg.com",
                   ".tiktokcdn.com", ".tiktokcdn-eu.com", ".tiktokcdn-us.com")


def within_period(published: str, period: str) -> bool:
    if period == "yesterday":
        # 24~48시간 전 업로드 영상은 "1일 전"으로 표시됩니다. (라이브 등 게시일 없는 항목은 제외)
        return (published or "").strip() == "1일 전"
    if not published:
        return True  # 게시일 정보가 없으면(라이브 등) 통과
    return not any(word in published for word in PERIOD_EXCLUDE.get(period, ()))


def build_search_params(period: str, shorts: bool = False) -> str:
    """정렬=조회수(3) + 필터(업로드날짜, 동영상 타입, 길이) protobuf를 base64로 만듭니다."""
    filters = bytes([0x08, PERIOD_CODE.get(period, 3), 0x10, 0x01])
    if shorts:
        filters += bytes([0x18, 0x01])  # 길이: 4분 미만
    raw = bytes([0x08, 0x03, 0x12, len(filters)]) + filters
    return base64.urlsafe_b64encode(raw).decode()


def http_get(url: str, payload=None, headers=None, timeout=15):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data)
    req.add_header("User-Agent", UA)
    if payload is not None:
        req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.headers.get("Content-Type", ""), resp.read()


def http_json(url: str, payload=None, headers=None, timeout=15):
    _, body = http_get(url, payload, headers, timeout)
    return json.loads(body.decode())


def parse_view_count(text: str) -> int:
    """'조회수 1,234,567회'와 채널 페이지의 축약형 '조회수 12만회' 모두 숫자로 변환합니다."""
    m = re.search(r"([0-9][0-9,.]*)\s*(억|만|천)?", text or "")
    if not m:
        return 0
    try:
        num = float(m.group(1).replace(",", ""))
    except ValueError:
        return 0
    return int(num * SUB_UNITS.get(m.group(2) or "", 1))


def cached(key, force, fetch_fn):
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and not force and now - hit[0] < CACHE_TTL:
            return hit[1], hit[0]
    result = fetch_fn()
    fetched_at = time.time()
    with _cache_lock:
        _cache[key] = (fetched_at, result)
    return result, fetched_at


# ================================================================ 조회 히스토리 (추이 분석)
# 날짜별로 각 조회 키(카테고리/검색어×기간)의 영상 순위·조회수를 기록해,
# 다음 날부터 순위 변동(▲▼)과 신규 진입(NEW)을 표시할 수 있게 합니다.
HISTORY_FILE = os.path.join(BASE_DIR, "history.json")
HISTORY_DAYS = 14  # 이 일수보다 오래된 기록은 자동 삭제
_hist_lock = threading.Lock()


def _load_history():
    try:
        with open(HISTORY_FILE) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def hist_key(category, period, shorts, query, region):
    return "|".join([query or category, period, "s" if shorts else "v", region])


def annotate_and_record(key, videos):
    """전날 스냅샷과 비교해 prevRank/prevViews/isNew를 붙이고, 오늘 스냅샷을 저장합니다."""
    today = time.strftime("%Y-%m-%d")
    with _hist_lock:
        hist = _load_history()
        entry = hist.setdefault(key, {})
        prev = None
        for d in sorted(entry.keys(), reverse=True):
            if d < today:
                prev = entry[d]
                break
        for v in videos:
            p = (prev or {}).get(v["id"])
            v.pop("prevRank", None)
            v.pop("prevViews", None)
            v.pop("isNew", None)
            if p:
                v["prevRank"] = p["rank"]
                v["prevViews"] = p["views"]
            elif prev is not None:
                v["isNew"] = True  # 비교할 전날 기록이 있는데 그 안에 없던 영상
        entry[today] = {v["id"]: {"views": v["views"], "rank": i + 1}
                        for i, v in enumerate(videos[:60])}
        cutoff = time.strftime("%Y-%m-%d", time.localtime(time.time() - HISTORY_DAYS * 86400))
        for d in [d for d in entry if d < cutoff]:
            del entry[d]
        try:
            with open(HISTORY_FILE, "w") as f:
                json.dump(hist, f, ensure_ascii=False)
        except OSError:
            pass


# ================================================================ 유튜브
def extract_videos(node, out):
    """응답 트리를 순회하며 videoRenderer를 수집합니다."""
    if isinstance(node, dict):
        if "videoRenderer" in node:
            v = node["videoRenderer"]
            title = "".join(r.get("text", "") for r in v.get("title", {}).get("runs", []))
            views_text = v.get("viewCountText", {}).get("simpleText", "")
            thumbs = v.get("thumbnail", {}).get("thumbnails", [])
            out.append({
                "id": v.get("videoId", ""),
                "title": title,
                "channel": "".join(r.get("text", "") for r in v.get("ownerText", {}).get("runs", [])),
                "views": parse_view_count(views_text),
                "viewsText": views_text,
                "length": v.get("lengthText", {}).get("simpleText", ""),
                "published": v.get("publishedTimeText", {}).get("simpleText", ""),
                "thumbnail": thumbs[-1]["url"] if thumbs else "",
            })
        for value in node.values():
            extract_videos(value, out)
    elif isinstance(node, list):
        for item in node:
            extract_videos(item, out)


def yt_search(query: str, period: str, shorts: bool, region: str = "KR"):
    # hl은 ko로 고정: 게시일("N일 전")·조회수 텍스트 파싱을 한국어 형식으로 유지하면서
    # gl(지역)만 바꿔 해당 국가의 인기 결과를 받습니다.
    payload = {
        "context": {"client": {
            "clientName": "WEB",
            "clientVersion": "2.20250624.01.00",
            "hl": "ko", "gl": region,
        }},
        "query": query,
        "params": build_search_params(period, shorts),
    }
    try:
        data = http_json("https://www.youtube.com/youtubei/v1/search", payload)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return []
    videos = []
    extract_videos(data, videos)
    seen, unique = set(), []
    for v in videos:
        if v["id"] and v["id"] not in seen and within_period(v["published"], period):
            seen.add(v["id"])
            unique.append(v)
    return unique


# 구독자 수 표기 단위 → 숫자 배수 (한국어/영어 UI 모두 대응)
SUB_UNITS = {"억": 100000000, "만": 10000, "천": 1000,
             "K": 1000, "M": 1000000, "B": 1000000000}


def parse_subscribers(s: str) -> int:
    """응답 본문에서 '구독자 123만명' / '1.2M subscribers' 형태를 숫자로 변환합니다."""
    m = (re.search(r"구독자\s*([0-9][0-9,.]*)\s*(억|만|천)?\s*명", s)
         or re.search(r"([0-9][0-9,.]*)\s*([KMB])?\s*subscribers", s))
    if not m:
        return 0
    try:
        num = float(m.group(1).replace(",", ""))
    except ValueError:
        return 0
    return int(num * SUB_UNITS.get(m.group(2) or "", 1))


def yt_video_stats(video_id: str):
    """youtubei/v1/next로 영상 1개의 좋아요 수와 채널 구독자 수를 가져옵니다(검색 API엔 없음)."""
    payload = {"context": {"client": {
        "clientName": "WEB", "clientVersion": "2.20250624.01.00", "hl": "ko", "gl": "KR"}},
        "videoId": video_id}
    try:
        _, body = http_get("https://www.youtube.com/youtubei/v1/next", payload=payload, timeout=10)
        s = body.decode("utf-8", "ignore")
        m = re.search(r"다른 사용자 ([0-9,]+)명", s) or re.search(r"along with ([0-9,]+) other", s)
        likes = int(m.group(1).replace(",", "")) + 1 if m else 0
        return likes, parse_subscribers(s)
    except Exception:
        return 0, 0


def enrich_likes(videos, limit=45):
    """영상 리스트에 좋아요 수(likes)와 구독자 수(subs)를 병렬로 채웁니다. 이미 채워진 항목은 건너뜁니다."""
    todo = [v for v in videos[:limit] if not v.get("likes") and not v.get("subs")]
    if not todo:
        return videos
    with ThreadPoolExecutor(max_workers=12) as pool:
        stats = pool.map(lambda v: yt_video_stats(v["id"]), todo)
    for v, (likes, subs) in zip(todo, stats):
        v["likes"] = likes
        v["subs"] = subs
    return videos


def merge_yt_searches(queries, period, shorts, region="KR"):
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = pool.map(lambda q: yt_search(q, period, shorts, region), queries)
    merged, seen = [], set()
    for chunk in results:
        for v in chunk:
            if v["id"] not in seen:
                seen.add(v["id"])
                merged.append(v)
    merged.sort(key=lambda v: v["views"], reverse=True)
    return merged


def get_videos(category: str, period: str, shorts: bool, force: bool,
               enrich: bool = False, query: str = "", region: str = "KR"):
    def fetch():
        if query:
            queries = [query]
        elif category == "전체":
            queries = [category_query(c, region) for c in ALL_MERGE]
        elif category == "AI":
            queries = AI_YT_QUERIES
        else:
            queries = [category_query(category, region)]
        vids = merge_yt_searches(queries, period, shorts, region)
        if enrich:
            enrich_likes(vids)
        return vids
    return cached(("yt", query or category, period, shorts, enrich, region), force, fetch)


# ================================================================ 인스타그램 릴스
def load_accounts(path, defaults):
    try:
        with open(path) as f:
            accounts = json.load(f)
            if isinstance(accounts, list) and accounts:
                return accounts
    except (OSError, json.JSONDecodeError):
        pass
    return list(defaults)


def save_accounts(path, accounts):
    with open(path, "w") as f:
        json.dump(accounts, f, ensure_ascii=False, indent=2)


# 계정 목록을 쓰는 소스별 설정 (파일 경로, 기본 계정)
ACCOUNT_SOURCES = {
    "reels": (ACCOUNTS_FILE, DEFAULT_IG_ACCOUNTS),
    "x": (X_ACCOUNTS_FILE, DEFAULT_X_ACCOUNTS),
    "threads": (THREADS_ACCOUNTS_FILE, DEFAULT_THREADS_ACCOUNTS),
    "tiktok": (TIKTOK_ACCOUNTS_FILE, DEFAULT_TIKTOK_ACCOUNTS),
    "channels": (YT_CHANNELS_FILE, DEFAULT_YT_CHANNELS),
}


# ================================================================ 유튜브 채널 추적
def fetch_channel_videos(handle: str):
    """채널 페이지(@핸들/videos)의 ytInitialData에서 최신 영상을 추출합니다."""
    h = handle.lstrip("@")
    try:
        _, body = http_get("https://www.youtube.com/@%s/videos?hl=ko" % quote(h), timeout=12)
        s = body.decode("utf-8", "ignore")
        m = re.search(r"var ytInitialData = (\{.*?\});</script>", s, re.S)
        if not m:
            return []
        data = json.loads(m.group(1))
    except Exception:
        return []
    vids = []
    extract_videos(data, vids)
    seen, out = set(), []
    for v in vids:
        if v["id"] and v["id"] not in seen:
            seen.add(v["id"])
            v["channel"] = v["channel"] or "@" + h
            v["account"] = h
            out.append(v)
    return out[:15]


def get_channels(force: bool):
    accounts = load_accounts(YT_CHANNELS_FILE, DEFAULT_YT_CHANNELS)

    def fetch():
        if not accounts:
            return []
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = pool.map(fetch_channel_videos, accounts)
        return [v for chunk in results for v in chunk]
    vids, fetched_at = cached(("ytch", tuple(accounts)), force, fetch)
    return vids, accounts, fetched_at


# ================================================================ 유튜브 인기 댓글
def fetch_comments(video_id: str, limit: int = 6):
    """영상의 인기 댓글 상위 몇 개를 가져옵니다 (2단계: 댓글 섹션 토큰 → 댓글 조회)."""
    client = {"context": {"client": {
        "clientName": "WEB", "clientVersion": "2.20250624.01.00", "hl": "ko", "gl": "KR"}}}
    try:
        _, body = http_get("https://www.youtube.com/youtubei/v1/next",
                           payload={**client, "videoId": video_id}, timeout=10)
        s = body.decode("utf-8", "ignore")
        idx = s.find('"comment-item-section"')
        if idx == -1:
            return []
        tokens = re.findall(r'"token":\s*"([^"]+)"', s[max(0, idx - 6000):idx])
        if not tokens:
            return []
        data = http_json("https://www.youtube.com/youtubei/v1/next",
                         payload={**client, "continuation": tokens[-1]}, timeout=10)
    except Exception:
        return []
    out = []

    def walk(o):
        if isinstance(o, dict):
            c = o.get("commentEntityPayload")
            if isinstance(c, dict):
                text = (((c.get("properties") or {}).get("content") or {}).get("content") or "").strip()
                if text:
                    out.append({
                        "text": text[:300],
                        "author": (c.get("author") or {}).get("displayName", ""),
                        "likes": ((c.get("toolbar") or {}).get("likeCountNotliked") or "").strip(),
                    })
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(data)
    return out[:limit]


def fetch_ig_reels(username: str):
    """인스타그램 웹 내부 API(무인증)로 계정의 최근 릴스를 가져옵니다."""
    url = ("https://www.instagram.com/api/v1/users/web_profile_info/?username="
           + quote(username))
    try:
        data = http_json(url, headers={"x-ig-app-id": IG_APP_ID}, timeout=12)
    except Exception:
        return []
    user = (data.get("data") or {}).get("user") or {}
    reels = []
    for edge in (user.get("edge_owner_to_timeline_media") or {}).get("edges", []):
        n = edge.get("node", {})
        if not n.get("is_video"):
            continue
        caps = (n.get("edge_media_to_caption") or {}).get("edges") or []
        title = caps[0]["node"]["text"].split("\n")[0][:120] if caps else ""
        reels.append({
            "account": username,
            "title": title or "(설명 없음)",
            "views": n.get("video_view_count") or 0,
            "likes": (n.get("edge_liked_by") or {}).get("count", 0),
            "comments": (n.get("edge_media_to_comment") or {}).get("count", 0),
            "thumbnail": n.get("thumbnail_src") or "",
            "url": "https://www.instagram.com/reel/%s/" % n.get("shortcode", ""),
            "takenAt": n.get("taken_at_timestamp") or 0,
        })
    return reels


def get_reels(force: bool):
    accounts = load_accounts(ACCOUNTS_FILE, DEFAULT_IG_ACCOUNTS)

    def fetch():
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = pool.map(fetch_ig_reels, accounts)
        merged = [r for chunk in results for r in chunk]
        merged.sort(key=lambda r: r["views"], reverse=True)
        return merged
    reels, fetched_at = cached(("reels", tuple(accounts)), force, fetch)
    return reels, accounts, fetched_at


# ================================================================ X (트위터)
def _find_timeline_entries(node):
    """syndication __NEXT_DATA__에서 timeline entries 리스트를 찾습니다."""
    if isinstance(node, dict):
        tl = node.get("timeline")
        if isinstance(tl, dict) and isinstance(tl.get("entries"), list):
            return tl["entries"]
        for v in node.values():
            r = _find_timeline_entries(v)
            if r:
                return r
    elif isinstance(node, list):
        for v in node:
            r = _find_timeline_entries(v)
            if r:
                return r
    return None


def fetch_x_posts(username: str):
    """트위터 syndication(임베드용, 무인증) API로 계정의 최근 트윗을 참여수와 함께 가져옵니다."""
    url = "https://syndication.twitter.com/srv/timeline-profile/screen-name/" + quote(username)
    try:
        _, body = http_get(url, headers={"Accept": "text/html"}, timeout=12)
        html = body.decode("utf-8", "ignore")
        m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
        if not m:
            return []
        data = json.loads(m.group(1))
    except Exception:
        return []
    entries = _find_timeline_entries(data) or []
    posts = []
    for e in entries:
        content = e.get("content", {}) if isinstance(e, dict) else {}
        t = content.get("tweet")
        if not isinstance(t, dict):
            tr = content.get("tweetResult") or {}
            t = tr.get("result") if isinstance(tr, dict) else None
        if not isinstance(t, dict) or t.get("favorite_count") is None:
            continue
        user = t.get("user", {}) if isinstance(t.get("user"), dict) else {}
        media = ""
        for mm in (t.get("mediaDetails") or []):
            if mm.get("media_url_https"):
                media = mm["media_url_https"]
                break
        posts.append({
            "account": username,
            "name": user.get("name", username),
            "text": (t.get("full_text") or t.get("text") or "").strip(),
            "likes": t.get("favorite_count") or 0,
            "replies": t.get("reply_count") or 0,
            "retweets": t.get("retweet_count") or 0,
            "views": int(t.get("views", {}).get("count", 0)) if isinstance(t.get("views"), dict) else 0,
            "media": media,
            "url": "https://x.com/%s/status/%s" % (username, t.get("id_str", "")),
            "createdAt": t.get("created_at", ""),
        })
    return posts


def get_x_posts(force: bool):
    accounts = load_accounts(X_ACCOUNTS_FILE, DEFAULT_X_ACCOUNTS)

    def fetch():
        # syndication은 동시 요청이 많으면 빈 응답을 주므로 동시성을 낮춥니다.
        with ThreadPoolExecutor(max_workers=3) as pool:
            results = pool.map(fetch_x_posts, accounts)
        return [p for chunk in results for p in chunk]
    posts, fetched_at = cached(("x", tuple(accounts)), force, fetch)
    return posts, accounts, fetched_at


# ================================================================ 스레드(Threads)
def _threads_lsd_and_userid(username: str):
    """스레드 프로필 페이지에서 LSD 토큰을, 인스타 API에서 user_id를 얻습니다."""
    lsd = None
    try:
        _, body = http_get("https://www.threads.com/@" + quote(username), timeout=12)
        m = re.search(r'"LSD",\[\],\{"token":"([^"]+)"', body.decode("utf-8", "ignore"))
        lsd = m.group(1) if m else None
    except Exception:
        pass
    user_id = None
    try:
        info = http_json(
            "https://www.instagram.com/api/v1/users/web_profile_info/?username=" + quote(username),
            headers={"x-ig-app-id": IG_APP_ID}, timeout=12)
        user_id = (info.get("data") or {}).get("user", {}).get("id")
    except Exception:
        pass
    return lsd, user_id


# 스레드 프로필 탭 쿼리의 doc_id는 수시로 바뀌므로, 알려진 후보를 순서대로 시도합니다.
THREADS_DOC_IDS = [
    "25073444226023094", "7451607104958938", "23996318550159868",
    "9925907010825989", "26286467210919721",
]


def fetch_threads_posts(username: str):
    lsd, user_id = _threads_lsd_and_userid(username)
    if not lsd or not user_id:
        return []
    from urllib.parse import urlencode
    headers = {
        "X-FB-LSD": lsd, "X-IG-App-ID": IG_APP_ID_THREADS,
        "Sec-Fetch-Site": "same-origin",
        "X-FB-Friendly-Name": "BarcelonaProfileThreadsTabQuery",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    for doc_id in THREADS_DOC_IDS:
        payload = urlencode({
            "lsd": lsd, "doc_id": doc_id,
            "variables": json.dumps({"userID": str(user_id), "__relay_internal__pv__BarcelonaIsLoggedInrelayprovider": False}),
        }).encode()
        req = urllib.request.Request("https://www.threads.com/api/graphql", data=payload)
        req.add_header("User-Agent", UA)
        for k, v in headers.items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=12) as resp:
                data = json.loads(resp.read().decode())
        except Exception:
            continue
        if data.get("errors"):
            continue
        posts = _parse_threads(data, username)
        if posts:
            return posts
    return []


def _parse_threads(data, username):
    posts = []

    def walk(o):
        if isinstance(o, dict):
            if "post" in o and isinstance(o["post"], dict) and o["post"].get("caption") is not None:
                p = o["post"]
                caption = (p.get("caption") or {}).get("text", "") if isinstance(p.get("caption"), dict) else ""
                info = p.get("text_post_app_info", {}) or {}
                imgs = (p.get("image_versions2") or {}).get("candidates") or []
                posts.append({
                    "account": username,
                    "text": caption[:280],
                    "likes": p.get("like_count") or 0,
                    "replies": info.get("direct_reply_count") or 0,
                    "reposts": info.get("repost_count") or 0,
                    "views": 0,
                    "media": imgs[0]["url"] if imgs else "",
                    "url": "https://www.threads.com/@%s/post/%s" % (username, p.get("code", "")),
                    "createdAt": p.get("taken_at") or 0,
                })
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(data)
    return posts


def get_threads_posts(force: bool):
    accounts = load_accounts(THREADS_ACCOUNTS_FILE, DEFAULT_THREADS_ACCOUNTS)

    def fetch():
        with ThreadPoolExecutor(max_workers=5) as pool:
            results = pool.map(fetch_threads_posts, accounts)
        return [p for chunk in results for p in chunk]
    posts, fetched_at = cached(("threads", tuple(accounts)), force, fetch)
    return posts, accounts, fetched_at


# ================================================================ 틱톡(TikTok)
def _tiktok_item(v):
    author = v.get("author", {}) if isinstance(v.get("author"), dict) else {}
    handle = author.get("unique_id", "")
    vid = v.get("video_id", "")
    return {
        "account": handle,
        "name": author.get("nickname", handle),
        "title": (v.get("title") or "").strip() or "(설명 없음)",
        "views": v.get("play_count") or 0,
        "likes": v.get("digg_count") or 0,
        "comments": v.get("comment_count") or 0,
        "shares": v.get("share_count") or 0,
        "thumbnail": v.get("cover") or v.get("origin_cover") or "",
        "url": "https://www.tiktok.com/@%s/video/%s" % (handle, vid),
        "id": vid,
        "createdAt": v.get("create_time") or 0,
    }


def fetch_tiktok_user(handle: str):
    url = "%s/user/posts?unique_id=%s&count=12" % (TIKWM_BASE, quote(handle))
    try:
        d = http_json(url, timeout=15)
    except Exception:
        return []
    vids = (d.get("data") or {}).get("videos") or []
    return [_tiktok_item(v) for v in vids]


def fetch_tiktok_trending():
    url = "%s/feed/list?region=%s&count=20" % (TIKWM_BASE, TIKTOK_REGION)
    try:
        d = http_json(url, timeout=15)
    except Exception:
        return []
    vids = d.get("data") or []
    return [_tiktok_item(v) for v in vids]


def get_tiktok(force: bool):
    accounts = load_accounts(TIKTOK_ACCOUNTS_FILE, DEFAULT_TIKTOK_ACCOUNTS)

    def fetch():
        # 트렌딩(전체 인기) + 구독 계정 최신 영상을 합쳐 중복 제거.
        # tikwm 무료 티어의 레이트리밋을 피하려 동시성을 낮춥니다.
        posts = fetch_tiktok_trending()
        with ThreadPoolExecutor(max_workers=3) as pool:
            for chunk in pool.map(fetch_tiktok_user, accounts):
                posts.extend(chunk)
        seen, unique = set(), []
        for p in posts:
            if p["id"] and p["id"] not in seen:
                seen.add(p["id"])
                unique.append(p)
        return unique
    posts, fetched_at = cached(("tiktok", tuple(accounts)), force, fetch)
    return posts, accounts, fetched_at


# ================================================================ AI 영상 탭
def fetch_news():
    def one(feed):
        label, url = feed
        try:
            _, body = http_get(url, timeout=12)
            root = ET.fromstring(body)
        except Exception:
            return []
        items = []
        for item in root.iter("item"):
            title = item.findtext("title") or ""
            source = item.findtext("source") or ""
            pub = item.findtext("pubDate") or ""
            try:
                ts = email.utils.parsedate_to_datetime(pub).timestamp()
            except (TypeError, ValueError):
                ts = 0
            items.append({"region": label, "title": title, "source": source,
                          "link": item.findtext("link") or "", "ts": ts})
        return items[:25]

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = pool.map(one, NEWS_FEEDS)
    merged = [n for chunk in results for n in chunk]
    merged.sort(key=lambda n: n["ts"], reverse=True)
    return merged[:40]


def fetch_hf_models():
    def one(args):
        pipeline, sort = args
        url = ("https://huggingface.co/api/models?pipeline_tag=%s&sort=%s"
               "&direction=-1&limit=12" % (pipeline, sort))
        try:
            data = http_json(url, timeout=12)
        except Exception:
            return []
        return [{"id": m.get("id", ""), "likes": m.get("likes", 0),
                 "downloads": m.get("downloads", 0), "pipeline": pipeline,
                 "createdAt": m.get("createdAt", "")} for m in data]

    jobs = [(p, s) for p in HF_PIPELINES for s in ("createdAt", "trendingScore")]
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(one, jobs))

    def dedupe(lists):
        seen, out = set(), []
        for chunk in lists:
            for m in chunk:
                if m["id"] not in seen:
                    seen.add(m["id"])
                    out.append(m)
        return out
    latest = dedupe(results[0::2])
    latest.sort(key=lambda m: m["createdAt"], reverse=True)
    trending = dedupe(results[1::2])
    return {"latest": latest[:12], "trending": trending[:12]}


def get_ai_data(force: bool):
    # AI 탭은 '글'(모델·뉴스)만 제공합니다. AI 영상은 유튜브 탭의 'AI' 카테고리로 통합됨.
    def fetch():
        with ThreadPoolExecutor(max_workers=2) as pool:
            news_f = pool.submit(fetch_news)
            models_f = pool.submit(fetch_hf_models)
            return {"news": news_f.result(), "models": models_f.result()}
    return cached(("ai",), force, fetch)


# ================================================================ 기타
def fetch_oembed(url: str):
    """틱톡/유튜브 URL의 oEmbed 메타데이터를 가져옵니다 (CORS 우회용 프록시)."""
    host = urlparse(url).netloc.lower()
    if "tiktok.com" in host:
        endpoint = "https://www.tiktok.com/oembed?url=" + quote(url, safe="")
    elif "youtube.com" in host or "youtu.be" in host:
        endpoint = "https://www.youtube.com/oembed?format=json&url=" + quote(url, safe="")
    else:
        return {"ok": False, "reason": "unsupported"}
    try:
        data = http_json(endpoint, timeout=10)
        return {"ok": True, "title": data.get("title", ""),
                "author": data.get("author_name", ""),
                "thumbnail": data.get("thumbnail_url", "")}
    except Exception:
        return {"ok": False, "reason": "fetch_failed"}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print("[%s] %s" % (time.strftime("%H:%M:%S"), fmt % args))

    def _send(self, code, body, content_type="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _host_ok(self) -> bool:
        """DNS 리바인딩 방어: Host 헤더가 localhost 계열이 아니면 거부합니다."""
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip().lower()
        if host in ALLOWED_HOSTS:
            return True
        self._send(403, {"error": "forbidden host"})
        return False

    def do_GET(self):
        if not self._host_ok():
            return
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        force = qs.get("force", ["0"])[0] == "1"

        if parsed.path in ("/", "/index.html"):
            with open(os.path.join(BASE_DIR, "index.html"), "rb") as f:
                html = f.read().replace(b"__CSRF_TOKEN__", CSRF_TOKEN.encode())
                self._send(200, html, "text/html; charset=utf-8")
            return

        if parsed.path == "/api/videos":
            category = qs.get("category", ["전체"])[0]
            period = qs.get("period", ["week"])[0]
            shorts = qs.get("shorts", ["0"])[0] == "1"
            enrich = qs.get("enrich", ["0"])[0] == "1"
            query = qs.get("q", [""])[0].strip()
            region = qs.get("region", ["KR"])[0]
            if region not in REGIONS:
                region = "KR"
            if not query and category not in ("전체", "AI") and category not in CATEGORIES:
                self._send(400, {"error": "unknown category"})
                return
            videos, fetched_at = get_videos(category, period, shorts, force, enrich, query, region)
            annotate_and_record(hist_key(category, period, shorts, query, region), videos)
            self._send(200, {"videos": videos[:60], "fetchedAt": fetched_at})
            return

        if parsed.path == "/api/channels":
            vids, accounts, fetched_at = get_channels(force)
            self._send(200, {"videos": vids, "accounts": accounts, "fetchedAt": fetched_at})
            return

        if parsed.path == "/api/comments":
            vid = qs.get("id", [""])[0]
            if not re.fullmatch(r"[A-Za-z0-9_-]{5,20}", vid):
                self._send(400, {"error": "bad video id"})
                return
            comments, _ = cached(("cmt", vid), False, lambda: fetch_comments(vid))
            self._send(200, {"comments": comments})
            return

        if parsed.path == "/api/categories":
            self._send(200, {"categories": ["전체", "AI"] + list(CATEGORIES.keys())})
            return

        if parsed.path == "/api/reels":
            reels, accounts, fetched_at = get_reels(force)
            self._send(200, {"reels": reels[:80], "accounts": accounts, "fetchedAt": fetched_at})
            return

        if parsed.path == "/api/x":
            posts, accounts, fetched_at = get_x_posts(force)
            self._send(200, {"posts": posts, "accounts": accounts, "fetchedAt": fetched_at})
            return

        if parsed.path == "/api/threads":
            posts, accounts, fetched_at = get_threads_posts(force)
            self._send(200, {"posts": posts, "accounts": accounts, "fetchedAt": fetched_at})
            return

        if parsed.path == "/api/tiktok":
            posts, accounts, fetched_at = get_tiktok(force)
            self._send(200, {"posts": posts[:100], "accounts": accounts, "fetchedAt": fetched_at})
            return

        if parsed.path == "/api/ai":
            data, fetched_at = get_ai_data(force)
            self._send(200, {**data, "fetchedAt": fetched_at})
            return

        if parsed.path == "/api/oembed":
            self._send(200, fetch_oembed(qs.get("url", [""])[0]))
            return

        if parsed.path == "/api/img":
            # 인스타/틱톡 CDN 등 핫링크가 막힌 썸네일을 서버가 대신 받아 전달(메모리 캐시)
            url = qs.get("u", [""])[0]
            host = urlparse(url).netloc.lower()
            if not url.startswith("https://") or not host.endswith(IMG_PROXY_ALLOW):
                self._send(400, {"error": "host not allowed"})
                return
            hit = _img_cache.get(url)
            if hit:
                self._send(200, hit[1], hit[0])
                return
            try:
                ctype, body = http_get(url, timeout=12)
                ctype = ctype or "image/jpeg"
                with _img_lock:
                    if len(_img_cache) > IMG_CACHE_MAX:
                        _img_cache.clear()
                    _img_cache[url] = (ctype, body)
                self._send(200, body, ctype)
            except Exception:
                self._send(502, {"error": "fetch failed"})
            return

        self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._host_ok():
            return
        # CSRF 방어: 페이지에 심어준 토큰이 헤더로 오지 않으면 거부합니다.
        if not secrets.compare_digest(self.headers.get("X-Csrf-Token") or "", CSRF_TOKEN):
            self._send(403, {"error": "invalid csrf token"})
            return
        parsed = urlparse(self.path)
        # /api/{reels|x|threads|tiktok|channels}/accounts — 구독 계정/채널 추가/삭제
        m = re.match(r"^/api/(reels|x|threads|tiktok|channels)/accounts$", parsed.path)
        if m:
            source = m.group(1)
            path, defaults = ACCOUNT_SOURCES[source]
            length = int(self.headers.get("Content-Length", 0))
            try:
                req = json.loads(self.rfile.read(length).decode())
            except json.JSONDecodeError:
                self._send(400, {"error": "invalid json"})
                return
            action = req.get("action")
            raw = (req.get("username") or "").strip().lstrip("@")
            # X는 대소문자 보존, 인스타/스레드는 소문자
            username = raw if source == "x" else raw.lower()
            accounts = load_accounts(path, defaults)
            if action == "add" and username and username not in accounts:
                accounts.append(username)
            elif action == "remove" and username in accounts:
                accounts.remove(username)
            save_accounts(path, accounts)
            self._send(200, {"accounts": accounts})
            return
        self._send(404, {"error": "not found"})


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"트렌드 뷰어 실행 중: http://localhost:{PORT}")
    server.serve_forever()
