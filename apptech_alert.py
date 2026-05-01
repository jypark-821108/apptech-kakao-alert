import argparse
import html
import json
import os
import re
from datetime import datetime, timezone, timedelta

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

from kakao import send_kakao

KST = timezone(timedelta(hours=9))
STATUS_FILE = "apptech-quiz-status.json"
UNKNOWN = "아직 확인 안 됨"
FM_BOARD = "https://www.fmkorea.com/freedeal"
FM_POST = "https://www.fmkorea.com/{}"
PPOMPPU_BOARD = "https://www.ppomppu.co.kr/zboard/zboard.php?id=coupon"
PPOMPPU_POST = "https://www.ppomppu.co.kr/zboard/view.php?id=coupon&no={}"

ITEMS = [
    "신한퀴즈",
    "모니모 영어 퀴즈",
    "KB Pay 퀴즈",
    "KB 스타퀴즈",
    "올원뱅크 디깅퀴즈",
    "하나원큐 축구Play 퀴즈",
    "하나원큐 OX퀴즈",
]

FM_TITLE_KEYWORDS = {
    "신한퀴즈": ["신한퀴즈"],
    "모니모 영어 퀴즈": ["모니모", "영어", "퀴즈"],
    "KB Pay 퀴즈": ["KB Pay", "퀴즈"],
    "KB 스타퀴즈": ["kb", "스타퀴즈"],
    "올원뱅크 디깅퀴즈": ["올원뱅크", "디깅퀴즈"],
    "하나원큐 축구Play 퀴즈": ["하나원큐", "축구"],
    "하나원큐 OX퀴즈": ["하나원큐", "OX퀴즈"],
}

PPOMPPU_TITLE_KEYWORDS = {
    "신한퀴즈": ["신한"],
    "모니모 영어 퀴즈": ["모니모", "영어"],
    "KB Pay 퀴즈": ["KB Pay", "오늘의 퀴즈"],
    "KB 스타퀴즈": ["KB스타뱅킹", "스타퀴즈"],
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


def normalized(value: str) -> str:
    return compact(value).lower().replace(" ", "")


def title_matches(item: str, title: str, keyword_map: dict[str, list[str]]) -> bool:
    title_norm = normalized(title)
    return all(normalized(k) in title_norm for k in keyword_map[item])


def clean_answer(raw: str) -> str | None:
    value = compact(raw)
    value = re.split(r"(?:입니다|입니당|참고|출처|댓글|추천|조회|스크랩|정답 입력 전|모든 분들|즐거운|감사|\||<)", value)[0]
    value = compact(value)
    if not value or len(value) > 50:
        return None
    if any(bad in value for bad in ["확인", "퀴즈", "정답", "게시판", "링크", "댓글", "본문", "쿠폰"]):
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
    text_one_line = compact(text)
    for pattern in [r"정답\s*[:：은는]?\s*([^。.!?]{1,60})", r"정답은\s*([^。.!?]{1,60})", r"답\s*[:：은는]?\s*([^。.!?]{1,60})"]:
        match = re.search(pattern, text_one_line, flags=re.IGNORECASE)
        if match:
            answer = clean_answer(match.group(1))
            if answer:
                return answer
    return None


def parse_fm_posts(html_text: str) -> list[tuple[str, str]]:
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


def collect_from_fmkorea() -> dict[str, str]:
    answers = {item: UNKNOWN for item in ITEMS}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            locale="ko-KR",
            user_agent=HEADERS["User-Agent"],
            extra_http_headers={"Accept-Language": HEADERS["Accept-Language"]},
        )
        page = context.new_page()
        try:
            page.goto(FM_BOARD, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(3500)
            wait_past_security(page)
            board_text = page.locator("body").inner_text(timeout=10000)
            if "에펨코리아 보안 시스템" in board_text:
                print("FMKorea board is still blocked by security page.")
                browser.close()
                return answers
            posts = parse_fm_posts(page.content())
            print("FMKorea matched board posts:", [(pid, title) for pid, title in posts if any(title_matches(item, title, FM_TITLE_KEYWORDS) for item in ITEMS)][:20])
            for item in ITEMS:
                matches = [(pid, title) for pid, title in posts if title_matches(item, title, FM_TITLE_KEYWORDS)][:4]
                if item == "신한퀴즈":
                    parts = []
                    for post_id, title in matches:
                        answer = fetch_fm_answer(page, post_id)
                        if answer:
                            label = "쏠" if "쏠" in title else "팡팡" if "팡팡" in title else "출석" if "출석" in title else "신한"
                            parts.append(f"{label} {answer}")
                    if parts:
                        answers[item] = " / ".join(parts)
                    continue
                for post_id, _title in matches:
                    answer = fetch_fm_answer(page, post_id)
                    if answer:
                        answers[item] = answer
                        break
        except Exception as exc:
            print(f"FMKorea fetch failed: {exc}")
        finally:
            browser.close()
    return answers


def fetch_fm_answer(page, post_id: str) -> str | None:
    try:
        page.goto(FM_POST.format(post_id), wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(3500)
        wait_past_security(page)
        text = page.locator("body").inner_text(timeout=10000)
        if "에펨코리아 보안 시스템" in text:
            return None
        return extract_answer_from_text(text)
    except Exception as exc:
        print(f"FMKorea post fetch failed {post_id}: {exc}")
        return None


def request_text(url: str) -> str:
    res = requests.get(url, headers=HEADERS, timeout=15)
    res.raise_for_status()
    if not res.encoding or res.encoding.lower() in {"iso-8859-1", "ascii"}:
        res.encoding = res.apparent_encoding
    return res.text


def ppomppu_posts() -> list[tuple[str, str]]:
    try:
        soup = BeautifulSoup(request_text(PPOMPPU_BOARD), "html.parser")
    except Exception as exc:
        print(f"Ppomppu board fetch failed: {exc}")
        return []
    posts: list[tuple[str, str]] = []
    for link in soup.select('a[href*="view.php?id=coupon&no="]'):
        href = link.get("href", "")
        match = re.search(r"no=(\d+)", href)
        title = compact(link.get_text(" ", strip=True))
        if match and title:
            pair = (match.group(1), title)
            if pair not in posts:
                posts.append(pair)
    return posts[:100]


def ppomppu_post_text(no: str) -> tuple[str, str]:
    soup = BeautifulSoup(request_text(PPOMPPU_POST.format(no)), "html.parser")
    desc = ""
    desc_meta = soup.select_one('meta[name="description"]') or soup.select_one('meta[property="og:description"]')
    if desc_meta:
        desc = desc_meta.get("content", "")
    body = soup.get_text("\n", strip=True)
    return desc, body


def collect_from_ppomppu(existing: dict[str, str]) -> dict[str, str]:
    answers = dict(existing)
    posts = ppomppu_posts()
    print("Ppomppu fallback candidates:", posts[:20])
    for item in ITEMS:
        if answers[item] != UNKNOWN:
            continue
        matches = [(pid, title) for pid, title in posts if title_matches(item, title, PPOMPPU_TITLE_KEYWORDS) and "정답" in title][:5]
        if item == "신한퀴즈":
            parts = []
            for post_id, title in matches:
                desc, body = ppomppu_post_text(post_id)
                answer = extract_answer_from_text("\n".join([desc, body]))
                if answer:
                    label = "쏠" if "쏠" in title else "팡팡" if "팡팡" in title else "출석" if "출석" in title or "슈퍼SOL" in title else "신한"
                    parts.append(f"{label} {answer}")
            if parts:
                answers[item] = " / ".join(parts)
            continue
        for post_id, _title in matches:
            desc, body = ppomppu_post_text(post_id)
            answer = extract_answer_from_text("\n".join([desc, body]))
            if answer:
                answers[item] = answer
                break
    return answers


def collect_answers() -> dict[str, str]:
    answers = collect_from_fmkorea()
    if any(answer == UNKNOWN for answer in answers.values()):
        answers = collect_from_ppomppu(answers)
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
