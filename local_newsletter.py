from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus
import html
import re
import xml.etree.ElementTree as ET

import requests
from bs4 import BeautifulSoup

from kakao import send_kakao

KST = timezone(timedelta(hours=9))
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.7,en;q=0.6",
}

RSS_QUERIES = [
    "용인시 when:1d",
    "용인특례시 when:1d",
    "용인 처인구 when:1d",
    "용인 모현 when:7d",
    "모현읍 when:7d",
    "처인구 모현읍 when:7d",
]

REGION_WORDS = ["용인", "용인시", "용인특례시", "처인", "모현", "모현읍"]
PRESS_RELEASE_HINTS = [
    "보도자료",
    "releasecopy",
    "용인시 제공",
    "용인특례시 제공",
    "용인시청 전경",
    "밝혔다",
    "추진한다",
    "개최한다",
    "운영한다",
    "모집한다",
]


def now() -> datetime:
    return datetime.now(KST)


def today() -> str:
    return now().strftime("%Y-%m-%d")


def compact(text: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(text or "")).strip()


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^0-9A-Za-z가-힣 ]", " ", compact(text))).strip().lower()


def split_title_source(title: str) -> tuple[str, str]:
    title = compact(title)
    if " - " in title:
        head, source = title.rsplit(" - ", 1)
        return compact(head), compact(source)
    return title, "출처 확인 필요"


def strip_html(text: str) -> str:
    soup = BeautifulSoup(text or "", "html.parser")
    return compact(soup.get_text(" ", strip=True))


def parse_pubdate(value: str) -> datetime | None:
    try:
        dt = parsedate_to_datetime(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(KST)
    except Exception:
        return None


def google_news_rss(query: str) -> list[dict]:
    url = "https://news.google.com/rss/search?q=" + quote_plus(query) + "&hl=ko&gl=KR&ceid=KR:ko"
    try:
        res = requests.get(url, headers=HEADERS, timeout=20)
        res.raise_for_status()
    except Exception as exc:
        print("Google News RSS failed:", query, repr(exc))
        return []

    rows: list[dict] = []
    root = ET.fromstring(res.content)
    for item in root.findall("./channel/item")[:20]:
        raw_title = item.findtext("title", "")
        title, source = split_title_source(raw_title)
        pub = parse_pubdate(item.findtext("pubDate", ""))
        snippet = strip_html(item.findtext("description", ""))
        link = compact(item.findtext("link", ""))
        if title:
            rows.append({"title": title, "source": source, "snippet": snippet, "link": link, "published": pub, "query": query})
    print(f"Google News results for {query}: {len(rows)}")
    return rows


def is_target_date(article: dict) -> bool:
    pub = article.get("published")
    if not pub:
        return False
    return pub.strftime("%Y-%m-%d") == today()


def is_region_article(article: dict) -> bool:
    text = article["title"] + " " + article["snippet"]
    return any(word in text for word in REGION_WORDS)


def similarity_key(title: str) -> str:
    words = [w for w in normalize(title).split() if len(w) > 1]
    return " ".join(words[:8])


def likely_press_release(article: dict, duplicate_sources: int) -> bool:
    text = article["title"] + " " + article["snippet"] + " " + article.get("link", "")
    if any(hint in text for hint in PRESS_RELEASE_HINTS):
        return True
    if duplicate_sources >= 3:
        return True
    return False


def collect_articles() -> tuple[list[dict], list[dict]]:
    raw: list[dict] = []
    for query in RSS_QUERIES:
        raw.extend(google_news_rss(query))

    candidates: list[dict] = []
    seen_links = set()
    for article in raw:
        if article["link"] in seen_links:
            continue
        seen_links.add(article["link"])
        if not is_target_date(article):
            continue
        if not is_region_article(article):
            continue
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
            continue
        included.append(article)
        used_titles.add(key)

    included.sort(key=lambda x: x.get("published") or datetime.min.replace(tzinfo=KST), reverse=True)
    excluded.sort(key=lambda x: x.get("published") or datetime.min.replace(tzinfo=KST), reverse=True)
    print("Included local news:", [(a["published"].isoformat() if a.get("published") else "", a["source"], a["title"]) for a in included])
    print("Excluded local news:", [(a["published"].isoformat() if a.get("published") else "", a["source"], a["title"]) for a in excluded[:10]])
    return included[:5], excluded[:5]


def article_line(article: dict, index: int) -> list[str]:
    pub = article.get("published")
    time_text = pub.strftime("%H:%M") if pub else "시간 확인 필요"
    summary = article["snippet"] or "요약을 확인할 수 없습니다."
    return [
        f"{index}. {article['title']}",
        f"매체 : {article['source']} / {time_text}",
        f"핵심 : {summary[:150]}",
        "",
    ]


def build_message(articles: list[dict], excluded: list[dict]) -> str:
    lines = ["오늘의 용인시·모현읍 뉴스레터", today(), ""]
    if articles:
        lines.append("자체 기사로 볼 만한 내용")
        for i, article in enumerate(articles, 1):
            lines.extend(article_line(article, i))
    else:
        lines.append("오늘 확인된 자체 취재 추정 기사는 없습니다.")
        lines.append("")

    if excluded:
        lines.append("보도자료·중복성으로 제외한 주요 기사")
        for article in excluded[:3]:
            pub = article.get("published")
            time_text = pub.strftime("%H:%M") if pub else "시간 확인 필요"
            lines.append(f"- {article['title']} ({article['source']} / {time_text})")
        lines.append("")

    lines.append(f"제외한 보도자료·중복성 기사: {len(excluded)}건")
    return "\n".join(lines).strip()


def main() -> None:
    articles, excluded = collect_articles()
    send_kakao(build_message(articles, excluded), link="https://news.google.com/search?q=%EC%9A%A9%EC%9D%B8%EC%8B%9C")


if __name__ == "__main__":
    main()
