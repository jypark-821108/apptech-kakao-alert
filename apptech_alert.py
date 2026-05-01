import argparse
import html
import json
import os
import re
from datetime import datetime, timezone, timedelta
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from kakao import send_kakao

KST = timezone(timedelta(hours=9))
STATUS_FILE = "apptech-quiz-status.json"
UNKNOWN = "아직 확인 안 됨"
PPOMPPU_BOARD = "https://www.ppomppu.co.kr/zboard/zboard.php?id=coupon"
PPOMPPU_VIEW = "https://www.ppomppu.co.kr/zboard/view.php?id=coupon&no={}"

ITEMS = [
    "신한퀴즈",
    "모니모 영어 퀴즈",
    "KB Pay 퀴즈",
    "KB 스타퀴즈",
    "올원뱅크 디깅퀴즈",
    "하나원큐 축구Play 퀴즈",
    "하나원큐 OX퀴즈",
]

TITLE_KEYWORDS = {
    "신한퀴즈": ["신한슈퍼SOL", "신한플레이", "신한쏠"],
    "모니모 영어 퀴즈": ["모니모", "영어"],
    "KB Pay 퀴즈": ["KB Pay", "오늘의 퀴즈"],
    "KB 스타퀴즈": ["KB스타뱅킹", "스타퀴즈"],
    "올원뱅크 디깅퀴즈": ["NH올원뱅크", "디깅퀴즈"],
    "하나원큐 축구Play 퀴즈": ["하나원큐", "축구"],
    "하나원큐 OX퀴즈": ["하나원큐", "OX퀴즈"],
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.7,en;q=0.6",
}


def now_kst() -> datetime:
    return datetime.now(KST)


def today() -> str:
    return now_kst().strftime("%Y-%m-%d")


def today_tokens() -> list[str]:
    d = now_kst()
    return [f"{d.month}/{d.day}", f"{d.month}월 {d.day}일", d.strftime("%y%m%d")]


def request_text(url: str) -> str:
    res = requests.get(url, headers=HEADERS, timeout=12)
    res.raise_for_status()
    # Ppomppu is EUC-KR. requests usually detects it, but force apparent encoding when needed.
    if not res.encoding or res.encoding.lower() in {"iso-8859-1", "ascii"}:
        res.encoding = res.apparent_encoding
    return res.text


def compact(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(value)).strip(" -:：[]()'\".,")


def clean_answer(raw: str) -> str | None:
    value = compact(raw)
    value = re.split(r"(?:PS\.|ps\.|참고|정답 입력 전|모든 분들|즐거운|감사|<|\||\n)", value)[0]
    value = compact(value)
    if not value or len(value) > 40:
        return None
    if any(bad in value for bad in ["확인", "퀴즈", "정답", "링크", "게시판", "쿠폰"]):
        return None
    return value


def extract_answer_from_text(text: str) -> str | None:
    normalized = compact(text)
    patterns = [
        r"정답\s*[:：은는]?\s*([^\r\n。.!?]{1,60})",
        r"정답은\s*([^\r\n。.!?]{1,60})",
    ]
    for pattern in patterns:
        match = re.search(pattern, normalized, flags=re.IGNORECASE)
        if match:
            answer = clean_answer(match.group(1))
            if answer:
                return answer
    return None


def post_text(no: str) -> tuple[str, str, str]:
    page = request_text(PPOMPPU_VIEW.format(no))
    soup = BeautifulSoup(page, "html.parser")
    title = ""
    title_meta = soup.select_one('meta[property="og:title"]')
    if title_meta:
        title = title_meta.get("content", "")
    desc = ""
    desc_meta = soup.select_one('meta[name="description"]') or soup.select_one('meta[property="og:description"]')
    if desc_meta:
        desc = desc_meta.get("content", "")
    body = soup.get_text("\n", strip=True)
    return compact(title), compact(desc), body


def board_posts() -> list[tuple[str, str]]:
    page = request_text(PPOMPPU_BOARD)
    soup = BeautifulSoup(page, "html.parser")
    posts: list[tuple[str, str]] = []
    for link in soup.select('a[href*="view.php?id=coupon&no="]'):
        href = link.get("href", "")
        match = re.search(r"no=(\d+)", href)
        title = compact(link.get_text(" ", strip=True))
        if match and title:
            pair = (match.group(1), title)
            if pair not in posts:
                posts.append(pair)
    return posts[:80]


def title_matches(item: str, title: str) -> bool:
    keywords = TITLE_KEYWORDS[item]
    if item == "신한퀴즈":
        return any(k in title for k in keywords) and "정답" in title
    return all(k in title for k in keywords) and "정답" in title


def collect_answers() -> dict[str, str]:
    posts = board_posts()
    answers = {item: UNKNOWN for item in ITEMS}

    for item in ITEMS:
        matched = [(no, title) for no, title in posts if title_matches(item, title)]
        if item == "신한퀴즈":
            parts = []
            for no, title in matched[:4]:
                post_title, desc, body = post_text(no)
                answer = extract_answer_from_text("\n".join([desc, body]))
                if not answer:
                    continue
                label = "신한"
                if "쏠" in post_title:
                    label = "쏠"
                elif "팡팡" in post_title:
                    label = "팡팡"
                elif "출석" in post_title or "슈퍼SOL" in post_title:
                    label = "출석"
                parts.append(f"{label} {answer}")
            if parts:
                answers[item] = " / ".join(parts)
            continue

        for no, _title in matched[:3]:
            post_title, desc, body = post_text(no)
            answer = extract_answer_from_text("\n".join([desc, body]))
            if answer:
                answers[item] = answer
                break

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
