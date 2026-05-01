import argparse
import json
import os
import re
from datetime import datetime, timezone, timedelta
from urllib.parse import quote_plus

import requests
from bs4 import BeautifulSoup

from kakao import send_kakao

KST = timezone(timedelta(hours=9))
STATUS_FILE = "apptech-quiz-status.json"
UNKNOWN = "아직 확인 안 됨"
ITEMS = [
    "신한퀴즈",
    "모니모 영어 퀴즈",
    "KB Pay 퀴즈",
    "KB 스타퀴즈",
    "올원뱅크 디깅퀴즈",
    "하나원큐 축구Play 퀴즈",
    "하나원큐 OX퀴즈",
]

QUERY_MAP = {
    "신한퀴즈": ["신한퀴즈", "신한 쏠퀴즈", "신한플레이 퀴즈", "신한 슈퍼SOL 출석퀴즈"],
    "모니모 영어 퀴즈": ["모니모 영어 퀴즈", "모니모 오늘의 영어", "모니모 영어공부"],
    "KB Pay 퀴즈": ["KB Pay 퀴즈", "KB Pay 오늘의 퀴즈", "KB페이 오늘의퀴즈"],
    "KB 스타퀴즈": ["KB 스타퀴즈", "KB스타뱅킹 스타퀴즈"],
    "올원뱅크 디깅퀴즈": ["올원뱅크 디깅퀴즈", "NH올원뱅크 디깅퀴즈"],
    "하나원큐 축구Play 퀴즈": ["하나원큐 축구Play 퀴즈", "하나원큐 축구플레이 퀴즈", "하나원큐 축구 Play"],
    "하나원큐 OX퀴즈": ["하나원큐 OX퀴즈", "하나원큐 OX 퀴즈", "하나원큐 슬기로운 금융생활 OX"],
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.7,en;q=0.6",
}

BAD_VALUES = ["확인", "공개", "참여", "퀴즈", "정답", "오늘", "포인트", "바로가기", "보기"]


def now_kst() -> datetime:
    return datetime.now(KST)


def today() -> str:
    return now_kst().strftime("%Y-%m-%d")


def today_kr() -> str:
    d = now_kst()
    return f"{d.year}년 {d.month}월 {d.day}일"


def compact(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip(" -:：[]()'\".,")


def request_text(url: str, **kwargs) -> str:
    res = requests.get(url, headers=HEADERS, timeout=20, **kwargs)
    res.raise_for_status()
    return res.text


def search_duckduckgo(query: str) -> list[str]:
    url = "https://duckduckgo.com/html/?q=" + quote_plus(query)
    try:
        html = request_text(url)
    except Exception:
        return []
    soup = BeautifulSoup(html, "html.parser")
    texts = []
    for result in soup.select(".result")[:10]:
        title = result.select_one(".result__title")
        snippet = result.select_one(".result__snippet")
        text = " ".join(x.get_text(" ", strip=True) for x in [title, snippet] if x)
        if text:
            texts.append(text)
    return texts


def search_naver_web(query: str) -> list[str]:
    # Public search-result HTML only; no login or API key.
    url = "https://search.naver.com/search.naver?where=webkr&query=" + quote_plus(query)
    try:
        html = request_text(url)
    except Exception:
        return []
    soup = BeautifulSoup(html, "html.parser")
    texts = []
    selectors = [".total_wrap", ".api_subject_bx", ".fds-collection-root"]
    for selector in selectors:
        for result in soup.select(selector)[:8]:
            text = result.get_text(" ", strip=True)
            if text:
                texts.append(text)
    return texts


def fetch_fmkorea_texts(keyword: str) -> list[str]:
    urls = [
        f"https://www.fmkorea.com/search.php?mid=freedeal&search_keyword={quote_plus(keyword)}&search_target=title_content",
        f"https://www.fmkorea.com/index.php?mid=freedeal&act=IS&is_keyword={quote_plus(keyword)}",
    ]
    texts = []
    for url in urls:
        try:
            html = request_text(url)
        except Exception:
            continue
        soup = BeautifulSoup(html, "html.parser")
        text = soup.get_text(" ", strip=True)
        if text:
            texts.append(text[:5000])
    return texts


def candidate_queries(item: str) -> list[str]:
    base = QUERY_MAP[item]
    dates = [today(), today_kr(), f"{now_kst().month}월 {now_kst().day}일"]
    queries = []
    for keyword in base:
        for date in dates:
            queries.extend([
                f"{date} {keyword} 정답",
                f"{keyword} {date} 정답",
                f"{keyword} 정답",
            ])
    return list(dict.fromkeys(queries))


def clean_candidate(raw: str) -> str | None:
    value = compact(raw)
    value = re.split(r"(?:입니다|이다|이며|라고|\||/ 출처| 출처| 관련| 참여| 바로)", value)[0]
    value = compact(value)
    if not value or len(value) > 60:
        return None
    if any(bad in value for bad in BAD_VALUES):
        return None
    if re.match(r"^[가-힣A-Za-z0-9 /+\-().%]+$", value):
        return value
    return None


def extract_answer(item: str, texts: list[str]) -> str | None:
    joined = "\n".join(texts)
    patterns = [
        rf"{re.escape(item)}[^\n]{{0,80}}?정답\s*[:：은는]?\s*([^\n。.!?]{{1,60}})",
        r"정답\s*[:：은는]?\s*([^\n。.!?]{1,60})",
        r"답\s*[:：은는]?\s*([^\n。.!?]{1,60})",
    ]
    candidates: list[str] = []
    for pattern in patterns:
        for match in re.finditer(pattern, joined, flags=re.IGNORECASE):
            value = clean_candidate(match.group(1))
            if value:
                candidates.append(value)
    if not candidates:
        return None
    # Prefer the shortest plausible value; snippets often append unrelated text after the answer.
    candidates.sort(key=lambda x: (len(x), x))
    return candidates[0]


def collect_answers() -> dict[str, str]:
    answers = {}
    for item in ITEMS:
        all_texts = []
        for query in candidate_queries(item)[:12]:
            all_texts.extend(search_naver_web(query))
            all_texts.extend(search_duckduckgo(query))
        for keyword in QUERY_MAP[item][:2]:
            all_texts.extend(fetch_fmkorea_texts(keyword))
        answer = extract_answer(item, all_texts)
        answers[item] = answer or UNKNOWN
    return answers


def load_status() -> dict | None:
    if not os.path.exists(STATUS_FILE):
        return None
    try:
        with open(STATUS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def save_status(answers: dict[str, str], checked_at: str) -> None:
    missing = [name for name, answer in answers.items() if answer == UNKNOWN]
    payload = {
        "date": today(),
        "all_found": not missing,
        "missing_items": missing,
        "checked_at": checked_at,
    }
    with open(STATUS_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def build_message(answers: dict[str, str], mode: str) -> str:
    title = "오늘의 앱테크 퀴즈 정답"
    if mode == "retry":
        title += " 오후 6시 재확인"
    lines = [title, today(), ""]
    for item in ITEMS:
        lines.append(f"{item} 정답 : {answers[item]}")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["noon", "retry"], required=True)
    args = parser.parse_args()

    if args.mode == "retry":
        status = load_status()
        if status and status.get("date") == today() and status.get("all_found") is True:
            print("No retry needed: all answers were found at noon.")
            return

    answers = collect_answers()
    save_status(answers, "18:00" if args.mode == "retry" else "12:00")
    send_kakao(build_message(answers, args.mode))


if __name__ == "__main__":
    main()
