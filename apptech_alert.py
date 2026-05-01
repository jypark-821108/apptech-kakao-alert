import argparse
import html
import json
import os
import re
from datetime import datetime, timezone, timedelta

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

from kakao import send_kakao

KST = timezone(timedelta(hours=9))
STATUS_FILE = "apptech-quiz-status.json"
UNKNOWN = "아직 확인 안 됨"
FM_BOARD = "https://www.fmkorea.com/freedeal"
FM_POST = "https://www.fmkorea.com/{}"

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
    "신한퀴즈": ["신한퀴즈"],
    "모니모 영어 퀴즈": ["모니모", "영어", "퀴즈"],
    "KB Pay 퀴즈": ["KB Pay", "퀴즈"],
    "KB 스타퀴즈": ["kb", "스타퀴즈"],
    "올원뱅크 디깅퀴즈": ["올원뱅크", "디깅퀴즈"],
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


def compact(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(value)).strip(" -:：[]()'\".,")


def parse_board_posts(html_text: str) -> list[tuple[str, str]]:
    soup = BeautifulSoup(html_text, "html.parser")
    posts: list[tuple[str, str]] = []
    for row in soup.select("table.bd_lst tbody tr"):
        title_cell = row.select_one("td.title")
        if not title_cell:
            continue
        link = title_cell.select_one('a[href^="/"]')
        if not link:
            continue
        href = link.get("href", "")
        match = re.search(r"/(\d+)$", href.split("?")[0])
        title = compact(link.get_text(" ", strip=True))
        if match and title:
            posts.append((match.group(1), title))
    return posts


def title_matches(item: str, title: str) -> bool:
    normalized = title.lower().replace(" ", "")
    keywords = [k.lower().replace(" ", "") for k in TITLE_KEYWORDS[item]]
    return all(k in normalized for k in keywords)


def clean_answer(raw: str) -> str | None:
    value = compact(raw)
    value = re.split(r"(?:입니다|입니당|참고|출처|댓글|추천|조회|스크랩|\||/|<)", value)[0]
    value = compact(value)
    if not value or len(value) > 50:
        return None
    if any(bad in value for bad in ["확인", "퀴즈", "정답", "게시판", "링크", "댓글", "본문"]):
        return None
    return value


def extract_answer_from_text(text: str) -> str | None:
    lines = [compact(line) for line in re.split(r"[\r\n]+", text) if compact(line)]
    for line in lines:
        for pattern in [r"정답\s*[:：은는]?\s*(.+)$", r"답\s*[:：은는]?\s*(.+)$"]:
            match = re.search(pattern, line, flags=re.IGNORECASE)
            if match:
                answer = clean_answer(match.group(1))
                if answer:
                    return answer
    normalized = compact(text)
    for pattern in [r"정답\s*[:：은는]?\s*([^。.!?]{1,60})", r"답\s*[:：은는]?\s*([^。.!?]{1,60})"]:
        match = re.search(pattern, normalized, flags=re.IGNORECASE)
        if match:
            answer = clean_answer(match.group(1))
            if answer:
                return answer
    return None


def wait_past_security(page) -> None:
    for _ in range(4):
        text = page.locator("body").inner_text(timeout=10000)
        if "에펨코리아 보안 시스템" not in text:
            return
        page.wait_for_timeout(4000)
        try:
            page.reload(wait_until="domcontentloaded", timeout=30000)
        except Exception:
            pass


def fetch_board_and_posts() -> tuple[list[tuple[str, str]], dict[str, str]]:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            locale="ko-KR",
            user_agent=HEADERS["User-Agent"],
            extra_http_headers={"Accept-Language": HEADERS["Accept-Language"]},
        )
        page = context.new_page()
        page.goto(FM_BOARD, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(3500)
        wait_past_security(page)
        board_html = page.content()
        posts = parse_board_posts(board_html)

        post_ids: list[str] = []
        for item in ITEMS:
            for post_id, title in posts:
                if title_matches(item, title) and post_id not in post_ids:
                    post_ids.append(post_id)
                if len(post_ids) >= 20:
                    break

        texts: dict[str, str] = {}
        for post_id in post_ids:
            try:
                page.goto(FM_POST.format(post_id), wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(3500)
                wait_past_security(page)
                texts[post_id] = page.locator("body").inner_text(timeout=10000)
            except Exception as exc:
                texts[post_id] = f"FETCH_ERROR: {exc}"
        browser.close()
        return posts, texts


def collect_answers() -> dict[str, str]:
    posts, texts = fetch_board_and_posts()
    matched_by_item: dict[str, list[tuple[str, str]]] = {}
    for item in ITEMS:
        matched_by_item[item] = [(post_id, title) for post_id, title in posts if title_matches(item, title)][:4]

    answers = {item: UNKNOWN for item in ITEMS}
    for item, matches in matched_by_item.items():
        if item == "신한퀴즈":
            parts = []
            for post_id, title in matches:
                answer = extract_answer_from_text(texts.get(post_id, ""))
                if not answer:
                    continue
                label = "신한"
                if "쏠" in title:
                    label = "쏠"
                elif "팡팡" in title:
                    label = "팡팡"
                elif "출석" in title:
                    label = "출석"
                parts.append(f"{label} {answer}")
            if parts:
                answers[item] = " / ".join(parts)
            continue
        for post_id, _title in matches:
            answer = extract_answer_from_text(texts.get(post_id, ""))
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
