from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus, urlparse
import html
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


def now() -> datetime:
    return datetime.now(KST)


def today() -> str:
    return now().strftime("%Y-%m-%d")


def compact(text: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(text or "")).strip()


def shorten(text: str, limit: int) -> str:
    text = compact(text)
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


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
            snippet_el = area.select_one("div.news_dsc") or area.select_one(".api_txt_lines") or area
            source_el = area.select_one("a.info.press") or area.select_one("span.info.press")
            source = compact(source_el.get_text(" ", strip=True)) if source_el else source_from_url(href)
            if title and href:
                rows.append({"title": title, "source": source, "snippet": compact(snippet_el.get_text(" ", strip=True)), "link": href, "published": parse_portal_date(area.get_text(" ", strip=True)), "channel": "Naver"})
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
    return included[:4], excluded[:4]


def build_message(articles: list[dict], excluded: list[dict]) -> str:
    lines = ["오늘의 용인시·모현읍 뉴스레터", today(), ""]
    if articles:
        lines.append("자체 기사로 볼 만한 내용")
        for i, article in enumerate(articles, 1):
            pub = article.get("published")
            time_text = pub.strftime("%H:%M") if pub else "시간 확인 필요"
            lines.append(f"{i}. {shorten(article['title'], 62)}")
            lines.append(f"매체 : {shorten(article['source'], 22)} / {time_text} / {article['channel']}")
            lines.append(f"핵심 : {shorten(article['snippet'], 82)}")
            lines.append("")
    else:
        lines.append("오늘 확인된 자체 취재 추정 기사는 없습니다.")
        lines.append("")

    if excluded:
        lines.append("보도자료·중복성 제외")
        for article in excluded[:2]:
            lines.append(f"- {shorten(article['title'], 64)}")
        lines.append("")

    lines.append("검색원: Google·Naver·Daum·Zum")
    lines.append(f"제외 기사: {len(excluded)}건")
    return shorten("\n".join(lines).strip(), 900)


def main() -> None:
    try:
        articles, excluded = collect_articles()
        message = build_message(articles, excluded)
        print("Newsletter message length:", len(message))
        send_kakao(message)
    except Exception:
        traceback.print_exc()
        raise


if __name__ == "__main__":
    main()
