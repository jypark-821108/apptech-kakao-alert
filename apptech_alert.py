import argparse
import html
import json
import os
import re
from datetime import datetime, timezone, timedelta
from io import BytesIO
from urllib.parse import quote_plus

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

try:
    from PIL import Image
    import pytesseract
except Exception:
    Image = None
    pytesseract = None

from kakao import send_kakao

KST = timezone(timedelta(hours=9))
UNKNOWN = "아직 확인 안 됨"
STATUS_FILE = "apptech-quiz-status.json"
FM_BOARD = "https://www.fmkorea.com/freedeal"
FM_POST = "https://www.fmkorea.com/{}"
PP_BOARD = "https://www.ppomppu.co.kr/zboard/zboard.php?id=coupon"
PP_SEARCH = "https://www.ppomppu.co.kr/zboard/zboard.php?id=coupon&search_type=sub_memo&keyword={}"
PP_POST = "https://www.ppomppu.co.kr/zboard/view.php?id=coupon&no={}"

ITEMS = [
    "신한퀴즈",
    "모니모 영어 퀴즈",
    "KB Pay 퀴즈",
    "KB 스타퀴즈",
    "올원뱅크 디깅퀴즈",
    "하나원큐 축구Play 퀴즈",
    "하나원큐 OX퀴즈",
]

FM_KEYS = {
    "신한퀴즈": [["신한퀴즈"]],
    "모니모 영어 퀴즈": [["모니모", "영어", "퀴즈"]],
    "KB Pay 퀴즈": [["KB Pay", "퀴즈"]],
    "KB 스타퀴즈": [["kb", "스타퀴즈"]],
    "올원뱅크 디깅퀴즈": [["올원뱅크", "디깅퀴즈"]],
    "하나원큐 축구Play 퀴즈": [["하나원큐", "축구"]],
    "하나원큐 OX퀴즈": [["하나원큐", "OX퀴즈"]],
}

PP_KEYS = {
    "신한퀴즈": [["신한쏠"], ["신한플레이"], ["신한슈퍼SOL"], ["신한", "정답"]],
    "모니모 영어 퀴즈": [["모니모", "영어"], ["모니모"]],
    "KB Pay 퀴즈": [["KB Pay", "오늘의 퀴즈"], ["KB Pay"]],
    "KB 스타퀴즈": [["KB스타뱅킹", "스타퀴즈"], ["KB", "스타퀴즈"]],
    "올원뱅크 디깅퀴즈": [["올원뱅크", "디깅퀴즈"], ["NH올원뱅크", "디깅퀴즈"]],
    "하나원큐 축구Play 퀴즈": [["하나원큐", "축구"], ["축구", "Play"]],
    "하나원큐 OX퀴즈": [["하나원큐", "OX퀴즈"], ["하나원큐", "OX"]],
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.7,en;q=0.6",
}


def now() -> datetime:
    return datetime.now(KST)


def today() -> str:
    return now().strftime("%Y-%m-%d")


def date_markers() -> list[str]:
    d = now()
    return [
        d.strftime("%Y-%m-%d"),
        f"{d.month}/{d.day}",
        f"{d.month}/{d.day}일",
        f"{d.month}월 {d.day}일",
        f"{d.month}월{d.day}일",
    ]


def compact(text: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(text or "")).strip(" -:：[]()'\".,")


def norm(text: str) -> str:
    return compact(text).lower().replace(" ", "")


def title_match(title: str, groups: list[list[str]]) -> bool:
    n = norm(title)
    return any(all(norm(k) in n for k in group) for group in groups)


def is_today_title(title: str) -> bool:
    n = norm(title)
    return any(norm(marker) in n for marker in date_markers())


def clean_answer(raw: str) -> str | None:
    value = compact(raw)
    value = re.split(r"(?:입니다|입니당|참고|출처|댓글|추천|조회|스크랩|정답 입력 전|모든 분들|즐거운|감사|\||<)", value)[0]
    value = compact(value)
    if not value or len(value) > 120:
        return None
    bad_words = ["확인", "퀴즈", "정답", "게시판", "링크", "댓글", "본문", "쿠폰", "참여", "이미지"]
    if any(bad in value for bad in bad_words):
        return None
    return value


def extract_answer(text: str) -> str | None:
    lines = [compact(x) for x in re.split(r"[\r\n]+", text or "") if compact(x)]
    patterns = [
        r"정답\s*[:：은는]?\s*(.+)$",
        r"정답은\s*(.+)$",
        r"답\s*[:：은는]?\s*(.+)$",
    ]
    for line in lines:
        for pattern in patterns:
            m = re.search(pattern, line, flags=re.I)
            if m:
                ans = clean_answer(m.group(1))
                if ans:
                    return ans
    for i, line in enumerate(lines):
        if re.fullmatch(r"(?:정답|답)\s*[:：]?", line, flags=re.I):
            pieces: list[str] = []
            for nxt in lines[i + 1:i + 4]:
                if any(stop in nxt for stop in ["참고", "댓글", "추천", "조회", "스크랩", "안녕하세요"]):
                    break
                cand = clean_answer(nxt)
                if cand:
                    pieces.append(cand)
                if len(" ".join(pieces)) >= 2:
                    ans = clean_answer(" ".join(pieces))
                    if ans:
                        return ans
    one = compact(text)
    for pattern in [r"정답\s*[:：은는]?\s*([^。!?]{1,100})", r"정답은\s*([^。!?]{1,100})"]:
        m = re.search(pattern, one, flags=re.I)
        if m:
            ans = clean_answer(m.group(1))
            if ans:
                return ans
    return None


def extract_english_sentences(text: str) -> str | None:
    fixed = text.replace("|", "I").replace("\n", " ")
    fixed = re.sub(r"\s+", " ", fixed)
    candidates = re.findall(r"\b(?:I|There|This|That|You|We|They|He|She)[A-Za-z0-9' ,;-]{3,90}[.!?]", fixed)
    seen: list[str] = []
    for sent in candidates:
        sent = compact(sent)
        if any(noise in sent.lower() for noise in ["google", "cookie", "script", "ppomppu"]):
            continue
        if sent not in seen:
            seen.append(sent)
    if seen:
        return " / ".join(seen[:3])
    return None


def parse_fm_posts(page_html: str) -> list[tuple[str, str]]:
    soup = BeautifulSoup(page_html, "html.parser")
    posts: list[tuple[str, str]] = []
    for row in soup.select("table.bd_lst tbody tr"):
        cell = row.select_one("td.title")
        if not cell:
            continue
        link = cell.select_one('a[href^="/"]')
        if not link:
            continue
        href = link.get("href", "")
        m = re.search(r"/(\d+)$", href.split("?")[0])
        title = compact(link.get_text(" ", strip=True))
        if m and title:
            posts.append((m.group(1), title))
    return posts


def wait_fm(page) -> str:
    last = ""
    for i in range(8):
        page.wait_for_timeout(3000)
        try:
            text = page.locator("body").inner_text(timeout=10000)
        except Exception:
            text = ""
        last = text
        if "에펨코리아 보안 시스템" not in text and "사람인지 확인" not in text:
            return text
        print(f"FMKorea security page still visible, wait round {i + 1}")
    return last


def collect_fmkorea() -> dict[str, str]:
    answers = {item: UNKNOWN for item in ITEMS}
    headed = os.environ.get("PLAYWRIGHT_HEADED") == "1"
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=not headed,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--no-sandbox",
            ],
        )
        context = browser.new_context(
            locale="ko-KR",
            timezone_id="Asia/Seoul",
            user_agent=HEADERS["User-Agent"],
            viewport={"width": 1365, "height": 900},
            extra_http_headers={"Accept-Language": HEADERS["Accept-Language"]},
        )
        context.add_init_script("""
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['ko-KR', 'ko', 'en-US', 'en']});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
window.chrome = window.chrome || { runtime: {} };
""")
        page = context.new_page()
        try:
            page.goto(FM_BOARD, wait_until="domcontentloaded", timeout=45000)
            board_text = wait_fm(page)
            if "에펨코리아 보안 시스템" in board_text or "사람인지 확인" in board_text:
                print("FMKorea board remained blocked. body snippet:", compact(board_text[:500]))
                return answers
            posts = parse_fm_posts(page.content())
            matched = [(pid, title) for pid, title in posts if any(title_match(title, FM_KEYS[item]) for item in ITEMS)]
            print("FMKorea matched posts:", matched[:30])
            for item in ITEMS:
                matches = [(pid, title) for pid, title in posts if title_match(title, FM_KEYS[item])][:4]
                if item == "신한퀴즈":
                    parts = []
                    for pid, title in matches:
                        ans = fm_post_answer(page, pid)
                        if ans:
                            label = "쏠" if "쏠" in title else "팡팡" if "팡팡" in title else "출석" if "출석" in title else "신한"
                            parts.append(f"{label} {ans}")
                    if parts:
                        answers[item] = " / ".join(parts)
                    continue
                for pid, _ in matches:
                    ans = fm_post_answer(page, pid)
                    if ans:
                        answers[item] = ans
                        break
        except Exception as exc:
            print("FMKorea collection failed:", repr(exc))
        finally:
            browser.close()
    return answers


def fm_post_answer(page, pid: str) -> str | None:
    try:
        page.goto(FM_POST.format(pid), wait_until="domcontentloaded", timeout=45000)
        text = wait_fm(page)
        if "에펨코리아 보안 시스템" in text or "사람인지 확인" in text:
            print(f"FMKorea post {pid} blocked")
            return None
        ans = extract_answer(text)
        print(f"FMKorea post {pid} answer: {ans}")
        return ans
    except Exception as exc:
        print(f"FMKorea post {pid} failed: {exc!r}")
        return None


def req(url: str) -> str:
    r = requests.get(url, headers=HEADERS, timeout=20)
    r.raise_for_status()
    if not r.encoding or r.encoding.lower() in {"iso-8859-1", "ascii"}:
        r.encoding = r.apparent_encoding
    return r.text


def parse_ppomppu_links(html_text: str) -> list[tuple[str, str]]:
    soup = BeautifulSoup(html_text, "html.parser")
    posts: list[tuple[str, str]] = []
    for link in soup.find_all("a", href=True):
        href = link.get("href", "")
        if "view.php?id=coupon" not in href or "no=" not in href:
            continue
        m = re.search(r"(?:\?|&)no=(\d+)", href)
        title = compact(link.get_text(" ", strip=True))
        if not m or not title or len(title) <= 2:
            continue
        pair = (m.group(1), title)
        if pair not in posts:
            posts.append(pair)
    return posts


def ppomppu_candidates(item: str) -> list[tuple[str, str]]:
    posts: list[tuple[str, str]] = []
    keywords = [" ".join(group) for group in PP_KEYS[item]]
    urls = [PP_BOARD] + [PP_SEARCH.format(quote_plus(k)) for k in keywords]
    for url in urls:
        try:
            found = parse_ppomppu_links(req(url))
            print(f"Ppomppu links from {url}: {len(found)}")
            posts.extend(found)
        except Exception as exc:
            print("Ppomppu fetch failed:", url, repr(exc))
    dedup: list[tuple[str, str]] = []
    for pair in posts:
        if pair not in dedup:
            dedup.append(pair)
    matched = [(pid, title) for pid, title in dedup if title_match(title, PP_KEYS[item]) and "정답" in title]
    today_matches = [(pid, title) for pid, title in matched if is_today_title(title)]
    filtered = today_matches or matched
    print(f"Ppomppu candidates for {item}:", filtered[:10])
    return filtered[:8]


def image_url_from_soup(soup: BeautifulSoup) -> str | None:
    meta = soup.select_one('meta[property="og:image"]')
    if not meta:
        return None
    url = meta.get("content", "").strip()
    if url.startswith("//"):
        url = "https:" + url
    return url or None


def ocr_image_answer(url: str | None) -> str | None:
    if not url or Image is None or pytesseract is None:
        return None
    try:
        res = requests.get(url, headers={**HEADERS, "Referer": "https://www.ppomppu.co.kr/"}, timeout=20)
        res.raise_for_status()
        img = Image.open(BytesIO(res.content))
        text = pytesseract.image_to_string(img, lang="eng")
        print("OCR text:", compact(text[:500]))
        return extract_answer(text) or extract_english_sentences(text)
    except Exception as exc:
        print("OCR failed:", repr(exc))
        return None


def ppomppu_answer(pid: str, item: str | None = None) -> str | None:
    try:
        soup = BeautifulSoup(req(PP_POST.format(pid)), "html.parser")
        desc = ""
        meta = soup.select_one('meta[name="description"]') or soup.select_one('meta[property="og:description"]')
        if meta:
            desc = meta.get("content", "")
        text = soup.get_text("\n", strip=True)
        ans = extract_answer(desc + "\n" + text)
        if not ans and item == "모니모 영어 퀴즈":
            ans = ocr_image_answer(image_url_from_soup(soup))
        print(f"Ppomppu post {pid} answer: {ans}")
        return ans
    except Exception as exc:
        print(f"Ppomppu post {pid} failed: {exc!r}")
        return None


def collect_ppomppu(existing: dict[str, str]) -> dict[str, str]:
    answers = dict(existing)
    for item in ITEMS:
        if answers[item] != UNKNOWN:
            continue
        candidates = ppomppu_candidates(item)
        if item == "신한퀴즈":
            parts = []
            seen_labels = set()
            for pid, title in candidates:
                ans = ppomppu_answer(pid, item)
                if not ans:
                    continue
                label = "쏠" if "쏠" in title else "팡팡" if "팡팡" in title else "출석" if "출석" in title or "슈퍼SOL" in title else "신한"
                if label in seen_labels:
                    continue
                seen_labels.add(label)
                parts.append(f"{label} {ans}")
            if parts:
                answers[item] = " / ".join(parts)
            continue
        for pid, _ in candidates:
            ans = ppomppu_answer(pid, item)
            if ans:
                answers[item] = ans
                break
    return answers


def collect_answers() -> dict[str, str]:
    answers = collect_fmkorea()
    if any(v == UNKNOWN for v in answers.values()):
        answers = collect_ppomppu(answers)
    print("Final answers:", answers)
    return answers


def save_status(answers: dict[str, str], checked_at: str) -> None:
    missing = [k for k, v in answers.items() if v == UNKNOWN]
    with open(STATUS_FILE, "w", encoding="utf-8") as f:
        json.dump({"date": today(), "all_found": not missing, "missing_items": missing, "checked_at": checked_at}, f, ensure_ascii=False, indent=2)


def build_message(answers: dict[str, str], mode: str) -> str:
    title = "오늘의 앱테크 퀴즈 정답" + (" 오후 6시 재확인" if mode == "retry" else "")
    return "\n".join([title, today(), ""] + [f"{item} 정답 : {answers[item]}" for item in ITEMS])


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["noon", "retry"], required=True)
    args = parser.parse_args()
    if args.mode == "retry" and os.path.exists(STATUS_FILE):
        try:
            status = json.load(open(STATUS_FILE, encoding="utf-8"))
            if status.get("date") == today() and status.get("all_found") is True:
                print("No retry needed: all answers were found at noon.")
                return
        except Exception:
            pass
    answers = collect_answers()
    save_status(answers, "18:00" if args.mode == "retry" else "12:00")
    send_kakao(build_message(answers, args.mode))


if __name__ == "__main__":
    main()
