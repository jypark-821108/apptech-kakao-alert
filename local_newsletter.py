from datetime import datetime, timezone, timedelta
from urllib.parse import quote_plus
import re

import requests
from bs4 import BeautifulSoup

from kakao import send_kakao

KST = timezone(timedelta(hours=9))
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36"
}
QUERIES = [
    "용인시 오늘 뉴스",
    "모현읍 오늘 뉴스",
    "처인구 모현읍 오늘 뉴스",
    "용인 모현 오늘 뉴스",
    "용인시 모현읍 오늘 기사",
]
PRESS_RELEASE_HINTS = [
    "보도자료",
    "용인시 제공",
    "밝혔다",
    "추진한다",
    "개최한다",
    "운영한다",
    "참여자를 모집",
]


def today() -> str:
    return datetime.now(KST).strftime("%Y-%m-%d")


def search_web(query: str) -> list[dict]:
    url = "https://duckduckgo.com/html/?q=" + quote_plus(query)
    try:
        res = requests.get(url, headers=HEADERS, timeout=20)
        res.raise_for_status()
    except Exception:
        return []
    soup = BeautifulSoup(res.text, "html.parser")
    rows = []
    for result in soup.select(".result")[:10]:
        title_el = result.select_one(".result__title")
        snippet_el = result.select_one(".result__snippet")
        url_el = result.select_one(".result__url")
        title = title_el.get_text(" ", strip=True) if title_el else ""
        snippet = snippet_el.get_text(" ", strip=True) if snippet_el else ""
        source = url_el.get_text(" ", strip=True) if url_el else ""
        if title:
            rows.append({"title": title, "snippet": snippet, "source": source})
    return rows


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^0-9A-Za-z가-힣 ]", " ", text)).strip()


def likely_press_release(article: dict, duplicate_count: int) -> bool:
    text = article["title"] + " " + article["snippet"]
    if duplicate_count >= 2:
        return True
    return any(hint in text for hint in PRESS_RELEASE_HINTS)


def collect_articles() -> tuple[list[dict], int]:
    raw = []
    for query in QUERIES:
        raw.extend(search_web(query))

    seen = {}
    for article in raw:
        key = normalize(article["title"])[:60]
        if not key:
            continue
        seen.setdefault(key, []).append(article)

    included = []
    excluded = 0
    used_titles = set()
    for group in seen.values():
        article = group[0]
        title_key = normalize(article["title"])
        if title_key in used_titles:
            continue
        duplicate_count = len(group)
        if likely_press_release(article, duplicate_count):
            excluded += duplicate_count
            continue
        if not any(word in (article["title"] + article["snippet"]) for word in ["용인", "모현", "처인"]):
            continue
        included.append(article)
        used_titles.add(title_key)
        if len(included) >= 5:
            break
    return included, excluded


def build_message(articles: list[dict], excluded: int) -> str:
    lines = ["오늘의 용인시·모현읍 뉴스레터", today(), ""]
    if not articles:
        lines.append("오늘 확인된 자체 취재 기사는 없습니다.")
    else:
        for i, article in enumerate(articles, 1):
            source = article["source"] or "출처 확인 필요"
            summary = article["snippet"] or "요약을 확인할 수 없습니다."
            lines.extend([
                f"{i}. {article['title']}",
                f"매체 : {source}",
                f"핵심 : {summary[:140]}",
                "왜 중요함 : 지역 생활과 행정 흐름을 확인할 수 있는 기사입니다.",
                "",
            ])
    lines.append(f"제외한 중복/보도자료성 기사: {excluded}건")
    return "\n".join(lines).strip()


def main() -> None:
    articles, excluded = collect_articles()
    send_kakao(build_message(articles, excluded))


if __name__ == "__main__":
    main()
