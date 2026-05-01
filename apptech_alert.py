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
BOARD_URL = "https://www.fmkorea.com/freedeal"
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
    "신한퀴즈": ["신한퀴즈 정답", "신한 쏠퀴즈 정답", "신한플레이 퀴즈 정답"],
    "모니모 영어 퀴즈": ["모니모 영어 퀴즈 정답", "모니모 오늘의 영어 정답"],
    "KB Pay 퀴즈": ["KB Pay 퀴즈 정답", "KB Pay 오늘의 퀴즈 정답"],
    "KB 스타퀴즈": ["KB 스타퀴즈 정답", "KB스타뱅킹 스타퀴즈 정답"],
    "올원뱅크 디깅퀴즈": ["올원뱅크 디깅퀴즈 정답", "NH올원뱅크 디깅퀴즈 정답"],
    "하나원큐 축구Play 퀴즈": ["하나원큐 축구Play 퀴즈 정답", "하나원큐 축구플레이 퀴즈 정답"],
    "하나원큐 OX퀴즈": ["하나원큐 OX퀴즈 정답", "하나원큐 OX 퀴즈 정답"],
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36"
}


def today() -> str:
    return datetime.now(KST).strftime("%Y-%m-%d")


def search_web(query: str) -> list[str]:
    # DuckDuckGo HTML is lightweight and does not require an API key.
    url = "https://duckduckgo.com/html/?q=" + quote_plus(query)
    try:
        res = requests.get(url, headers=HEADERS, timeout=20)
        res.raise_for_status()
    except Exception:
        return []
    soup = BeautifulSoup(res.text, "html.parser")
    texts = []
    for result in soup.select(".result")[:8]:
        title = result.select_one(".result__title")
        snippet = result.select_one(".result__snippet")
        text = " ".join(x.get_text(" ", strip=True) for x in [title, snippet] if x)
        if text:
            texts.append(text)
    return texts


def extract_answer(item: str, texts: list[str]) -> str | None:
    joined = "\n".join(texts)
    candidates = []
    patterns = [
        r"정답\s*[:：은는]?\s*([^\n。.!?]{1,40})",
        r"답\s*[:：은는]?\s*([^\n。.!?]{1,40})",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, joined, flags=re.IGNORECASE):
            value = match.group(1).strip(" -:：[]()'")
            if value and not any(bad in value for bad in ["확인", "공개", "참여", "퀴즈"]):
                candidates.append(value)
    if candidates:
        return candidates[0]
    return None


def collect_answers() -> dict[str, str]:
    date_kr = datetime.now(KST).strftime("%Y년 %-m월 %-d일") if os.name != "nt" else datetime.now(KST).strftime("%Y년 %#m월 %#d일")
    date_dash = today()
    answers = {}
    for item in ITEMS:
        all_texts = []
        for query in QUERY_MAP[item]:
            all_texts.extend(search_web(f'{date_dash} {query}'))
            all_texts.extend(search_web(f'{date_kr} {query}'))
            all_texts.extend(search_web(f'site:fmkorea.com/freedeal {query}'))
        answer = extract_answer(item, all_texts)
        answers[item] = answer or "아직 확인 안 됨"
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
    missing = [name for name, answer in answers.items() if answer == "아직 확인 안 됨"]
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
