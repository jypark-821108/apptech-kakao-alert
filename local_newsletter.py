from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote_plus, urlparse
import html
import json
import os
import re
import traceback
import xml.etree.ElementTree as ET

import requests
from bs4 import BeautifulSoup

from kakao import send_kakao

KST = timezone(timedelta(hours=9))
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.7,en;q=0.6",
}

BASE_QUERIES = ["용인시", "용인특례시", "용인 처인구", "용인 모현", "모현읍", "처인구 모현읍"]
GOOGLE_QUERIES = [q + " when:1d" for q in BASE_QUERIES[:3]] + [q + " when:7d" for q in BASE_QUERIES[3:]]
REGION_WORDS = ["용인", "용인시", "용인특례시", "처인", "처인구", "모현", "모현읍"]
MOHYEON_WORDS = ["모현", "모현읍", "왕산리", "능원리", "동림리", "초부리", "갈담리", "매산리", "오산리", "일산리"]
PAGE_PATH = Path("docs/newsletter.html")
DAILY_DIR = Path("docs/news")
DATA_DIR = Path("docs/news-data")
DEFAULT_PAGE_URL = "https://jypark-821108.github.io/apptech-kakao-alert/newsletter.html"
BASE_PAGE_URL = "https://jypark-821108.github.io/apptech-kakao-alert"

OFFICIAL_SUBJECTS = [
    "용인시", "용인특례시", "처인구", "기흥구", "수지구", "용인시의회", "용인도시공사",
    "용인문화재단", "용인시청", "용인교육지원청", "경기도", "경기도교육청",
]
PR_VERBS = [
    "밝혔다", "전했다", "설명했다", "덧붙였다", "나선다", "추진", "개최", "운영", "실시",
    "모집", "선정", "지원", "체결", "완료", "제공", "열었다", "연다", "착수", "확대",
    "조성", "진행", "배포", "안내", "홍보", "당부", "기념", "참여자", "대상으로",
]
DIRECT_PR_HINTS = [
    "보도자료", "releasecopy", "사진=용인", "용인시 제공", "용인특례시 제공", "처인구 제공",
    "시 관계자", "구 관계자", "시는 ", "용인시는", "용인특례시는", "처인구는",
]
COMMON_TOPIC_WORDS = {
    "용인", "용인시", "용인특례시", "처인", "처인구", "기흥", "기흥구", "수지", "수지구",
    "모현", "모현읍", "경기", "경기도", "오늘", "뉴스", "기자", "종합", "단독", "포토",
    "관련", "추진", "개최", "운영", "실시", "지원", "모집", "선정", "사업", "행사",
}


def now() -> datetime:
    return datetime.now(KST)


def today() -> str:
    return now().strftime("%Y-%m-%d")


def compact(text: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(text or "")).strip()


def shorten(text: str, limit: int) -> str:
    text = compact(text)
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "..."


def esc(text: str) -> str:
    return html.escape(compact(text), quote=True)


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^0-9A-Za-z가-힣 ]", " ", compact(text))).strip().lower()


def clean_title(text: str) -> str:
    title = compact(text)
    title = re.sub(r"\s*-\s*(네이버뉴스|다음뉴스|줌뉴스)$", "", title)
    title = re.split(r"\s+(?:\d+분 전|\d+시간 전|오늘|어제|20\d{2}[.\-/]\d{1,2}[.\-/]\d{1,2})", title)[0]
    title = re.split(r"\s+(?:관련뉴스|뉴스홈|댓글|공유|기사입력|입력|수정)\b", title)[0]
    return compact(title)


def valid_title(title: str) -> bool:
    title = compact(title)
    if len(title) < 8 or len(title) > 95:
        return False
    bad = ["뉴스", "이미지", "동영상", "검색", "바로가기", "구독", "로그인", "전체뉴스", "많이 본 뉴스"]
    if title in bad or any(title.startswith(x) for x in ["관련뉴스", "뉴스홈", "구독", "랭킹"]):
        return False
    if title.count(".") > 8 or title.count("|") > 3:
        return False
    return True


def strip_html(text: str) -> str:
    return compact(BeautifulSoup(text or "", "html.parser").get_text(" ", strip=True))


def source_from_url(url: str) -> str:
    host = urlparse(url).netloc.replace("www.", "")
    return host or "출처 확인 필요"


def split_title_source(title: str) -> tuple[str, str]:
    title = compact(title)
    if " - " in title:
        head, source = title.rsplit(" - ", 1)
        return clean_title(head), compact(source)
    return clean_title(title), "출처 확인 필요"


def parse_pubdate(value: str) -> datetime | None:
    try:
        dt = parsedate_to_datetime(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(KST)
    except Exception:
        return None


def parse_portal_date(text: str) -> datetime | None:
    text = compact(text)
    current = now()
    if re.search(r"\d+\s*(분|시간)\s*전", text) or "오늘" in text:
        return current
    if "어제" in text or re.search(r"\d+\s*일\s*전", text):
        return current - timedelta(days=1)
    m = re.search(r"(20\d{2})[.\-/](\d{1,2})[.\-/](\d{1,2})", text)
    if m:
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=KST)
    m = re.search(r"(\d{1,2})[.\-/](\d{1,2})", text)
    if m:
        return datetime(current.year, int(m.group(1)), int(m.group(2)), tzinfo=KST)
    return None


def fetch(url: str) -> str:
    res = requests.get(url, headers=HEADERS, timeout=12)
    res.raise_for_status()
    if not res.encoding or res.encoding.lower() in {"iso-8859-1", "ascii"}:
        res.encoding = res.apparent_encoding
    return res.text


def google_news(query: str) -> list[dict]:
    url = "https://news.google.com/rss/search?q=" + quote_plus(query) + "&hl=ko&gl=KR&ceid=KR:ko"
    try:
        res = requests.get(url, headers=HEADERS, timeout=12)
        res.raise_for_status()
        root = ET.fromstring(res.content)
    except Exception as exc:
        print("Google News failed:", query, repr(exc))
        return []
    rows = []
    for item in root.findall("./channel/item")[:12]:
        title, source = split_title_source(item.findtext("title", ""))
        link = compact(item.findtext("link", ""))
        if valid_title(title):
            rows.append({"title": title, "source": source, "snippet": strip_html(item.findtext("description", "")), "link": link, "published": parse_pubdate(item.findtext("pubDate", "")), "channel": "Google"})
    print(f"Google results for {query}: {len(rows)}")
    return rows


def naver_news(query: str) -> list[dict]:
    url = "https://search.naver.com/search.naver?where=news&sort=1&pd=4&query=" + quote_plus(query)
    try:
        soup = BeautifulSoup(fetch(url), "html.parser")
    except Exception as exc:
        print("Naver News failed:", query, repr(exc))
        return []
    rows = []
    for area in soup.select("div.news_area")[:12]:
        try:
            link = area.select_one("a.news_tit")
            if not link:
                continue
            title = clean_title(link.get("title") or link.get_text(" ", strip=True))
            href = link.get("href", "")
            source_el = area.select_one("a.info.press") or area.select_one("span.info.press")
            source = compact(source_el.get_text(" ", strip=True)) if source_el else source_from_url(href)
            if valid_title(title) and href:
                text = compact(area.get_text(" ", strip=True))
                rows.append({"title": title, "source": source, "snippet": text, "link": href, "published": parse_portal_date(text), "channel": "Naver"})
        except Exception as exc:
            print("Naver item skipped:", repr(exc))
    print(f"Naver results for {query}: {len(rows)}")
    return rows


def daum_news(query: str) -> list[dict]:
    url = "https://search.daum.net/search?w=news&sort=recency&q=" + quote_plus(query)
    try:
        soup = BeautifulSoup(fetch(url), "html.parser")
    except Exception as exc:
        print("Daum News failed:", query, repr(exc))
        return []
    rows = []
    selectors = "a.tit_main, a.tit-g, a.link_tit, strong.tit-g a, div.item-title a"
    for link in soup.select(selectors)[:20]:
        try:
            title = clean_title(link.get("title") or link.get_text(" ", strip=True))
            href = link.get("href", "")
            parent = link.find_parent(["li", "div", "article"]) or link
            text = compact(parent.get_text(" ", strip=True))
            if valid_title(title) and href:
                rows.append({"title": title, "source": source_from_url(href), "snippet": text, "link": href, "published": parse_portal_date(text), "channel": "Daum"})
        except Exception as exc:
            print("Daum item skipped:", repr(exc))
    print(f"Daum results for {query}: {len(rows)}")
    return rows


def zum_news(query: str) -> list[dict]:
    url = "https://search.zum.com/search.zum?method=news&option=date&query=" + quote_plus(query)
    try:
        soup = BeautifulSoup(fetch(url), "html.parser")
    except Exception as exc:
        print("Zum News failed:", query, repr(exc))
        return []
    rows = []
    selectors = "a.title, a.tit, a.news_tit, div.news_wrap a[href*='http']"
    for link in soup.select(selectors)[:20]:
        try:
            title = clean_title(link.get("title") or link.get_text(" ", strip=True))
            href = link.get("href", "")
            parent = link.find_parent(["li", "div", "article"]) or link
            text = compact(parent.get_text(" ", strip=True))
            if valid_title(title) and href:
                rows.append({"title": title, "source": source_from_url(href), "snippet": text, "link": href, "published": parse_portal_date(text), "channel": "Zum"})
        except Exception as exc:
            print("Zum item skipped:", repr(exc))
    print(f"Zum results for {query}: {len(rows)}")
    return rows


def is_target_date(article: dict) -> bool:
    pub = article.get("published")
    return bool(pub and pub.strftime("%Y-%m-%d") == today())


def is_region_article(article: dict) -> bool:
    text = article["title"] + " " + article["snippet"]
    return any(word in text for word in REGION_WORDS)


def title_tokens(text: str) -> set[str]:
    tokens = set()
    for word in normalize(text).split():
        if len(word) <= 1 or word in COMMON_TOPIC_WORDS:
            continue
        tokens.add(word)
    return tokens


def same_topic(left: dict, right: dict) -> bool:
    a = title_tokens(left["title"] + " " + left.get("snippet", "")[:120])
    b = title_tokens(right["title"] + " " + right.get("snippet", "")[:120])
    if not a or not b:
        return False
    overlap = len(a & b)
    smaller = min(len(a), len(b))
    union = len(a | b)
    return overlap >= 3 and (overlap / smaller >= 0.55 or overlap / union >= 0.42)


def group_candidates(candidates: list[dict]) -> list[list[dict]]:
    groups: list[list[dict]] = []
    for article in candidates:
        placed = False
        for group in groups:
            if any(same_topic(article, existing) for existing in group):
                group.append(article)
                placed = True
                break
        if not placed:
            groups.append([article])
    return groups


def official_press_release_score(article: dict) -> int:
    text = article["title"] + " " + article.get("snippet", "") + " " + article.get("link", "")
    score = 0
    if any(hint in text for hint in DIRECT_PR_HINTS):
        score += 2
    if any(subject in text for subject in OFFICIAL_SUBJECTS):
        score += 1
    verb_hits = sum(1 for verb in PR_VERBS if verb in text)
    score += min(verb_hits, 3)
    if re.search(r"(업무협약|MOU|간담회|캠페인|교육|공모|참여자|수강생|대상자|착공식|준공식)", text):
        score += 1
    return score


def likely_press_release(article: dict, group: list[dict]) -> bool:
    sources = {g["source"] for g in group}
    score = official_press_release_score(article)
    if score >= 4:
        return True
    if len(sources) >= 2 and score >= 2:
        return True
    if len(sources) >= 3:
        return True
    return False


def collect_articles() -> tuple[list[dict], list[dict]]:
    raw: list[dict] = []
    for query in GOOGLE_QUERIES:
        raw.extend(google_news(query))
    for query in BASE_QUERIES:
        raw.extend(naver_news(query))
        raw.extend(daum_news(query))
        raw.extend(zum_news(query))

    candidates: list[dict] = []
    seen_links = set()
    for article in raw:
        if not valid_title(article["title"]):
            continue
        link_key = article.get("link") or normalize(article["title"])
        if link_key in seen_links:
            continue
        seen_links.add(link_key)
        if is_target_date(article) and is_region_article(article):
            candidates.append(article)

    included: list[dict] = []
    excluded: list[dict] = []
    used_topics: list[dict] = []
    for group in group_candidates(candidates):
        group.sort(key=lambda x: x.get("published") or datetime(1970, 1, 1, tzinfo=KST), reverse=True)
        article = group[0]
        if any(same_topic(article, used) for used in used_topics):
            continue
        if likely_press_release(article, group):
            excluded.extend(group)
        else:
            included.append(article)
            used_topics.append(article)

    included.sort(key=lambda x: x.get("published") or datetime(1970, 1, 1, tzinfo=KST), reverse=True)
    excluded.sort(key=lambda x: x.get("published") or datetime(1970, 1, 1, tzinfo=KST), reverse=True)
    print("Included local news:", [(a["channel"], a["published"].isoformat() if a.get("published") else "", a["source"], a["title"]) for a in included])
    print("Excluded local news:", [(a["channel"], a["published"].isoformat() if a.get("published") else "", a["source"], a["title"]) for a in excluded[:20]])
    return included[:12], excluded[:12]


def article_url(article: dict) -> str:
    return article.get("link") or "#"


def article_record(article: dict) -> dict:
    pub = article.get("published")
    return {
        "title": article.get("title", ""),
        "source": article.get("source", ""),
        "link": article.get("link", ""),
        "channel": article.get("channel", ""),
        "snippet": article.get("snippet", ""),
        "published": pub.isoformat() if pub else "",
    }


def article_from_record(record: dict) -> dict:
    article = dict(record)
    pub = article.get("published")
    if isinstance(pub, str) and pub:
        try:
            article["published"] = datetime.fromisoformat(pub)
        except ValueError:
            article["published"] = None
    else:
        article["published"] = None
    return article


def is_mohyeon_article(article: dict) -> bool:
    text = article.get("title", "") + " " + article.get("snippet", "")
    return any(word in text for word in MOHYEON_WORDS)


def split_sections(articles: list[dict]) -> tuple[list[dict], list[dict]]:
    mohyeon = [article for article in articles if is_mohyeon_article(article)]
    yongin = [article for article in articles if not is_mohyeon_article(article)]
    return yongin, mohyeon


def archive_dates() -> list[str]:
    if not DATA_DIR.exists():
        return []
    return sorted([path.stem for path in DATA_DIR.glob("*.json")], reverse=True)


def archive_nav(active_date: str) -> str:
    dates = archive_dates()
    if active_date not in dates:
        dates = [active_date] + dates
    options = []
    for date in dates:
        selected = " selected" if date == active_date else ""
        options.append(f'<option value="{esc(BASE_PAGE_URL + "/news/" + date + ".html")}"{selected}>{esc(date)}</option>')
    return f"""
    <div class="archive-bar">
      <label for="date-select">날짜별 뉴스</label>
      <select id="date-select" onchange="if(this.value) location.href=this.value">
        {''.join(options)}
      </select>
      <a class="latest" href="{esc(BASE_PAGE_URL + '/newsletter.html')}">최신</a>
    </div>
    """


def save_daily_data(date_text: str, generated_at: str, articles: list[dict], excluded: list[dict]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    data = {
        "date": date_text,
        "generated_at": generated_at,
        "articles": [article_record(article) for article in articles],
        "excluded": [article_record(article) for article in excluded],
    }
    (DATA_DIR / f"{date_text}.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def render_articles(articles: list[dict], empty_text: str = "확인된 자체 취재 추정 기사는 없습니다.") -> str:
    if not articles:
        return f'<p class="empty">{esc(empty_text)}</p>'
    cards = []
    for article in articles:
        pub = article.get("published")
        time_text = pub.strftime("%H:%M") if pub else "시간 확인 필요"
        title = shorten(article["title"], 82)
        cards.append(f"""
        <article class="news-card">
          <a href="{esc(article_url(article))}" target="_blank" rel="noopener noreferrer">
            <h2>{esc(title)}</h2>
            <p class="meta"><span>{esc(article['source'])}</span><span>{esc(time_text)}</span></p>
          </a>
        </article>
        """)
    return "\n".join(cards)


def render_section(title: str, articles: list[dict], empty_text: str) -> str:
    return f"""
    <section class="news-section">
      <div class="section-head"><h2>{esc(title)}</h2><span>{len(articles)}건</span></div>
      {render_articles(articles, empty_text)}
    </section>
    """


def render_excluded(excluded: list[dict]) -> str:
    if not excluded:
        return ""
    items = []
    for article in excluded[:8]:
        items.append(f"<li>{esc(shorten(article['title'], 80))} <span>{esc(article['source'])}</span></li>")
    return "<section class='excluded'><h2>보도자료·중복으로 제외</h2><ul>" + "".join(items) + "</ul></section>"


def render_page(date_text: str, generated_at: str, articles: list[dict], excluded: list[dict]) -> str:
    yongin_articles, mohyeon_articles = split_sections(articles)
    page = f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>용인·모현 오늘 뉴스</title>
  <style>
    :root {{ color-scheme: light; --ink:#171717; --muted:#6b7280; --line:#e5e7eb; --bg:#f7f8fa; --card:#ffffff; --accent:#126d5b; }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif; background:var(--bg); color:var(--ink); }}
    header {{ background:#0f172a; color:white; padding:26px 18px 22px; }}
    .wrap {{ max-width:780px; margin:0 auto; }}
    h1 {{ margin:0 0 8px; font-size:26px; letter-spacing:0; }}
    .sub {{ margin:0; color:#cbd5e1; font-size:14px; }}
    main {{ padding:16px; }}
    .archive-bar {{ display:flex; align-items:center; gap:8px; margin:0 0 14px; padding:12px; background:white; border:1px solid var(--line); border-radius:8px; }}
    .archive-bar label {{ font-size:13px; font-weight:800; color:#374151; }}
    .archive-bar select {{ flex:1; min-width:0; height:38px; border:1px solid var(--line); border-radius:6px; padding:0 10px; background:white; font-size:14px; }}
    .archive-bar .latest {{ color:var(--accent); font-size:13px; font-weight:800; text-decoration:none; }}
    .summary {{ display:flex; gap:8px; flex-wrap:wrap; margin:0 0 12px; }}
    .pill {{ background:#e6f4ef; color:#0b5d4d; border:1px solid #cbe7dd; border-radius:999px; padding:7px 10px; font-size:13px; font-weight:700; }}
    .news-section {{ margin:16px 0 20px; }}
    .section-head {{ display:flex; justify-content:space-between; align-items:center; gap:10px; margin:0 0 8px; }}
    .section-head h2 {{ margin:0; font-size:18px; letter-spacing:0; }}
    .section-head span {{ color:var(--muted); font-size:13px; font-weight:800; }}
    .news-card {{ background:var(--card); border:1px solid var(--line); border-radius:8px; margin:8px 0; overflow:hidden; }}
    .news-card a {{ display:block; color:inherit; text-decoration:none; padding:14px; }}
    .news-card h2 {{ margin:0 0 8px; font-size:17px; line-height:1.35; letter-spacing:0; display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; overflow:hidden; }}
    .meta {{ display:flex; gap:8px; flex-wrap:wrap; margin:0; color:var(--muted); font-size:13px; }}
    .meta span {{ border-right:1px solid var(--line); padding-right:8px; }}
    .meta span:last-child {{ border-right:0; }}
    .excluded {{ margin-top:20px; padding:14px; border:1px solid var(--line); border-radius:8px; background:white; }}
    .excluded h2 {{ margin:0 0 10px; font-size:15px; }}
    .excluded ul {{ margin:0; padding-left:20px; color:#4b5563; }}
    .excluded li {{ margin:7px 0; }}
    .excluded span {{ color:#8a94a3; font-size:12px; }}
    .empty {{ padding:18px; background:white; border:1px solid var(--line); border-radius:8px; }}
    footer {{ color:#8a94a3; font-size:12px; padding:10px 0 28px; }}
  </style>
</head>
<body>
  <header><div class="wrap"><h1>용인·모현 오늘 뉴스</h1><p class="sub">{esc(date_text)} · 생성 {esc(generated_at)}</p></div></header>
  <main><div class="wrap">
    {archive_nav(date_text)}
    <div class="summary"><span class="pill">용인 {len(yongin_articles)}건</span><span class="pill">모현 {len(mohyeon_articles)}건</span><span class="pill">제외 {len(excluded)}건</span></div>
    {render_section("용인 소식", yongin_articles, "오늘 확인된 용인 자체 취재 추정 기사는 없습니다.")}
    {render_section("모현 소식", mohyeon_articles, "오늘 확인된 모현 자체 취재 추정 기사는 없습니다.")}
    {render_excluded(excluded)}
    <footer>검색원: Google News, Naver, Daum, Zum</footer>
  </div></main>
</body>
</html>
"""
    return page


def write_page(articles: list[dict], excluded: list[dict]) -> None:
    PAGE_PATH.parent.mkdir(parents=True, exist_ok=True)
    DAILY_DIR.mkdir(parents=True, exist_ok=True)
    generated_at = now().strftime("%Y-%m-%d %H:%M")
    date_text = today()
    save_daily_data(date_text, generated_at, articles, excluded)
    page = render_page(date_text, generated_at, articles, excluded)
    PAGE_PATH.write_text(page, encoding="utf-8")
    (DAILY_DIR / f"{date_text}.html").write_text(page, encoding="utf-8")


def build_message(articles: list[dict], page_url: str) -> str:
    yongin_articles, mohyeon_articles = split_sections(articles)
    return f"용인·모현 오늘 뉴스\n{today()}\n용인 {len(yongin_articles)}건 · 모현 {len(mohyeon_articles)}건 정리 완료\n자세히 보기: {page_url}"


def main() -> None:
    try:
        articles, excluded = collect_articles()
        write_page(articles, excluded)
        page_url = os.environ.get("NEWSLETTER_URL", DEFAULT_PAGE_URL)
        message = build_message(articles, page_url)
        print("Newsletter message length:", len(message))
        send_kakao(message, link=page_url)
    except Exception:
        traceback.print_exc()
        raise


if __name__ == "__main__":
    main()
