from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote_plus, urlparse
import html
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
REGION_WORDS = ["용인", "용인시", "용인특례시", "처인", "모현", "모현읍"]
PRESS_RELEASE_HINTS = ["보도자료", "releasecopy", "용인시 제공", "용인특례시 제공", "용인시청 전경", "밝혔다", "추진한다", "개최한다", "운영한다", "모집한다"]
PAGE_PATH = Path("docs/newsletter.html")
DEFAULT_PAGE_URL = "https://jypark-821108.github.io/apptech-kakao-alert/newsletter.html"


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


def strip_html(text: str) -> str:
    return compact(BeautifulSoup(text or "", "html.parser").get_text(" ", strip=True))


def source_from_url(url: str) -> str:
    host = urlparse(url).netloc.replace("www.", "")
    return host or "출처 확인 필요"


def split_title_source(title: str) -> tuple[str, str]:
    title = compact(title)
    if " - " in title:
        head, source = title.rsplit(" - ", 1)
        return compact(head), compact(source)
    return title, "출처 확인 필요"


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
        if title:
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
    for area in soup.select("div.news_area, div.sds-comps-vertical-layout")[:12]:
        try:
            link = area.select_one("a.news_tit") or area.select_one('a[href*="http"]')
            if not link:
                continue
            title = compact(link.get("title") or link.get_text(" ", strip=True))
            href = link.get("href", "")
            source_el = area.select_one("a.info.press") or area.select_one("span.info.press")
            source = compact(source_el.get_text(" ", strip=True)) if source_el else source_from_url(href)
            if title and href:
                text = compact(area.get_text(" ", strip=True))
                rows.append({"title": title, "source": source, "snippet": text, "link": href, "published": parse_portal_date(text), "channel": "Naver"})
        except Exception as exc:
            print("Naver item skipped:", repr(exc))
    print(f"Naver results for {query}: {len(rows)}")
    return rows


def portal_news(query: str, channel: str, url: str) -> list[dict]:
    try:
        soup = BeautifulSoup(fetch(url), "html.parser")
    except Exception as exc:
        print(f"{channel} News failed:", query, repr(exc))
        return []
    rows = []
    for area in soup.select("div.item-bundle, div.c-item-content, div.news_wrap, li, div.wrap_cont, div.cont")[:30]:
        try:
            link = area.select_one('a[href*="http"]')
            if not link:
                continue
            title = compact(link.get("title") or link.get_text(" ", strip=True))
            href = link.get("href", "")
            text = compact(area.get_text(" ", strip=True))
            if len(title) < 6 or title == "뉴스":
                continue
            rows.append({"title": title, "source": source_from_url(href), "snippet": text, "link": href, "published": parse_portal_date(text), "channel": channel})
        except Exception as exc:
            print(f"{channel} item skipped:", repr(exc))
    print(f"{channel} results for {query}: {len(rows)}")
    return rows


def daum_news(query: str) -> list[dict]:
    return portal_news(query, "Daum", "https://search.daum.net/search?w=news&sort=recency&q=" + quote_plus(query))


def zum_news(query: str) -> list[dict]:
    return portal_news(query, "Zum", "https://search.zum.com/search.zum?method=news&option=date&query=" + quote_plus(query))


def is_target_date(article: dict) -> bool:
    pub = article.get("published")
    return bool(pub and pub.strftime("%Y-%m-%d") == today())


def is_region_article(article: dict) -> bool:
    text = article["title"] + " " + article["snippet"]
    return any(word in text for word in REGION_WORDS)


def similarity_key(title: str) -> str:
    words = [w for w in normalize(title).split() if len(w) > 1]
    return " ".join(words[:8])


def likely_press_release(article: dict, duplicate_sources: int) -> bool:
    text = article["title"] + " " + article["snippet"] + " " + article.get("link", "")
    return any(hint in text for hint in PRESS_RELEASE_HINTS) or duplicate_sources >= 3


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
        link_key = article.get("link") or normalize(article["title"])
        if link_key in seen_links:
            continue
        seen_links.add(link_key)
        if is_target_date(article) and is_region_article(article):
            candidates.append(article)

    groups: dict[str, list[dict]] = {}
    for article in candidates:
        groups.setdefault(similarity_key(article["title"]), []).append(article)

    included: list[dict] = []
    excluded: list[dict] = []
    used_titles = set()
    for group in groups.values():
        group_sources = {g["source"] for g in group}
        article = group[0]
        key = normalize(article["title"])
        if key in used_titles:
            continue
        if likely_press_release(article, len(group_sources)):
            excluded.extend(group)
        else:
            included.append(article)
            used_titles.add(key)

    included.sort(key=lambda x: x.get("published") or datetime(1970, 1, 1, tzinfo=KST), reverse=True)
    excluded.sort(key=lambda x: x.get("published") or datetime(1970, 1, 1, tzinfo=KST), reverse=True)
    print("Included local news:", [(a["channel"], a["published"].isoformat() if a.get("published") else "", a["source"], a["title"]) for a in included])
    print("Excluded local news:", [(a["channel"], a["published"].isoformat() if a.get("published") else "", a["source"], a["title"]) for a in excluded[:10]])
    return included[:12], excluded[:8]


def article_url(article: dict) -> str:
    return article.get("link") or "#"


def render_articles(articles: list[dict]) -> str:
    if not articles:
        return '<p class="empty">오늘 확인된 자체 취재 추정 기사는 없습니다.</p>'
    cards = []
    for article in articles:
        pub = article.get("published")
        time_text = pub.strftime("%H:%M") if pub else "시간 확인 필요"
        cards.append(f"""
        <article class="news-card">
          <a href="{esc(article_url(article))}" target="_blank" rel="noopener noreferrer">
            <h2>{esc(article['title'])}</h2>
            <p class="meta"><span>{esc(article['source'])}</span><span>{esc(time_text)}</span></p>
          </a>
        </article>
        """)
    return "\n".join(cards)


def render_excluded(excluded: list[dict]) -> str:
    if not excluded:
        return ""
    items = []
    for article in excluded[:6]:
        items.append(f"<li>{esc(article['title'])} <span>{esc(article['source'])}</span></li>")
    return "<section class='excluded'><h2>보도자료·중복으로 제외</h2><ul>" + "".join(items) + "</ul></section>"


def write_page(articles: list[dict], excluded: list[dict]) -> None:
    PAGE_PATH.parent.mkdir(parents=True, exist_ok=True)
    generated_at = now().strftime("%Y-%m-%d %H:%M")
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
    header {{ background:#0f172a; color:white; padding:28px 18px 24px; }}
    .wrap {{ max-width:860px; margin:0 auto; }}
    h1 {{ margin:0 0 8px; font-size:28px; letter-spacing:0; }}
    .sub {{ margin:0; color:#cbd5e1; font-size:14px; }}
    main {{ padding:18px; }}
    .summary {{ display:flex; gap:8px; flex-wrap:wrap; margin:0 0 14px; }}
    .pill {{ background:#e6f4ef; color:#0b5d4d; border:1px solid #cbe7dd; border-radius:999px; padding:7px 10px; font-size:13px; font-weight:700; }}
    .news-card {{ background:var(--card); border:1px solid var(--line); border-radius:10px; margin:10px 0; overflow:hidden; }}
    .news-card a {{ display:block; color:inherit; text-decoration:none; padding:16px; }}
    .news-card h2 {{ margin:0 0 10px; font-size:18px; line-height:1.35; letter-spacing:0; }}
    .meta {{ display:flex; gap:8px; flex-wrap:wrap; margin:0; color:var(--muted); font-size:13px; }}
    .meta span {{ border-right:1px solid var(--line); padding-right:8px; }}
    .meta span:last-child {{ border-right:0; }}
    .excluded {{ margin-top:24px; padding:16px; border:1px solid var(--line); border-radius:10px; background:white; }}
    .excluded h2 {{ margin:0 0 10px; font-size:16px; }}
    .excluded ul {{ margin:0; padding-left:20px; color:#4b5563; }}
    .excluded li {{ margin:8px 0; }}
    .excluded span {{ color:#8a94a3; font-size:12px; }}
    .empty {{ padding:18px; background:white; border:1px solid var(--line); border-radius:10px; }}
    footer {{ color:#8a94a3; font-size:12px; padding:10px 0 28px; }}
  </style>
</head>
<body>
  <header><div class="wrap"><h1>용인·모현 오늘 뉴스</h1><p class="sub">{esc(today())} · 생성 {esc(generated_at)}</p></div></header>
  <main><div class="wrap">
    <div class="summary"><span class="pill">자체 기사 {len(articles)}건</span><span class="pill">제외 {len(excluded)}건</span></div>
    {render_articles(articles)}
    {render_excluded(excluded)}
    <footer>검색원: Google News, Naver, Daum, Zum</footer>
  </div></main>
</body>
</html>
"""
    PAGE_PATH.write_text(page, encoding="utf-8")


def build_message(articles: list[dict], page_url: str) -> str:
    return f"용인·모현 오늘 뉴스\n{today()}\n새 기사 {len(articles)}건 정리 완료\n자세히 보기: {page_url}"


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
