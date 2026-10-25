"""에버랜드 구글 지도 리뷰를 최신순으로 모아 CSV로 저장한다.

화면의 잘린 칩 대신, 리뷰 목록이 스크롤될 때 페이지가 받는 응답을 읽는다.
방문 시간은 구글 항목명이며 CSV의 체류시간 열에 넣는다.

필요 패키지: pip install selenium
Chrome과 맞는 ChromeDriver는 Selenium 4.6 이상이 자동으로 받는다.

예:
    python everland_reviews_selenium.py
    python everland_reviews_selenium.py --output "C:\\Users\\user\\Desktop\\lenny\\everland_reviews.csv"
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

KST = timezone(timedelta(hours=9))
PLACE_ID = "0x0:0x1fa18ab619238098"
MAPS_URL = "https://www.google.com/maps?cid=2279255401136619672&hl=ko"
COLUMNS = [
    "아이디",
    "날짜",
    "게시글_원문",
    "게시글",
    "평점",
    "방문일",
    "대기시간",
    "예약권장",
    "체류시간",
    "좋아요수",
]

# 페이지가 만들어지기 전에 fetch와 XHR을 감싼다.
HOOK = r"""
window.__blobs = window.__blobs || [];
if (!window.__reviewHook) {
  const keep = (text) => {
    if (!text) return;
    if (text.includes('TTD_DAY_OF_VISIT') || text.includes('PPSF_VISIT_DURATION')) {
      window.__blobs.push(text);
    }
  };
  const origFetch = window.fetch;
  window.fetch = async function(...args) {
    const res = await origFetch.apply(this, args);
    try { keep(await res.clone().text()); } catch (e) {}
    return res;
  };
  const origOpen = XMLHttpRequest.prototype.open;
  const origSend = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function(method, url, ...rest) {
    this.__url = url;
    return origOpen.call(this, method, url, ...rest);
  };
  XMLHttpRequest.prototype.send = function(...args) {
    this.addEventListener('load', function() {
      try { keep(String(this.responseText || '')); } catch (e) {}
    });
    return origSend.apply(this, args);
  };
  window.__reviewHook = true;
}
"""

SCROLL = r"""
const feeds = [...document.querySelectorAll('div.m6QErb.DxyBCb')];
const feed = feeds.sort((a, b) => b.querySelectorAll('div.jftiEf').length - a.querySelectorAll('div.jftiEf').length)[0];
if (!feed) return {ok: false};
const cards = [...feed.querySelectorAll('div.jftiEf')];
if (cards.length > 18) {
  for (let i = 0; i < cards.length - 10; i++) cards[i].remove();
}
feed.scrollTop = Math.max(0, feed.scrollHeight - 12000);
feed.scrollTop = feed.scrollHeight;
return {
  ok: true,
  height: feed.scrollHeight,
  top: feed.scrollTop,
  cards: feed.querySelectorAll('div.jftiEf').length,
  first: feed.querySelector('.d4r55') ? feed.querySelector('.d4r55').textContent.trim() : ''
};
"""


def cutoff_us(day: str) -> int:
    dt = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=KST)
    return int(dt.timestamp() * 1_000_000)


def format_kst(us: int) -> str:
    return datetime.fromtimestamp(us / 1_000_000, tz=KST).strftime("%Y.%m.%d")


def parse_inner(text: str):
    """XSSI 응답에서 리뷰 목록이 들어 있는 JSON을 꺼낸다."""
    start = text.find("[")
    if start < 0:
        return None
    match = re.search(r"(\d+)\s*$", text[:start])
    lengths = [int(match.group(1))] if match else []
    lengths += [lengths[0] - 1, lengths[0] - 2] if lengths else []
    for length in lengths:
        if length <= 0:
            continue
        try:
            data = json.loads(text[start : start + length])
        except json.JSONDecodeError:
            continue
        payload = data[0][2] if data and isinstance(data[0], list) and len(data[0]) > 2 else None
        if not isinstance(payload, str):
            continue
        try:
            return json.loads(payload)
        except json.JSONDecodeError:
            continue
    return None


def dig(value, *indexes):
    current = value
    for index in indexes:
        if not isinstance(current, list) or index >= len(current):
            return None
        current = current[index]
    return current


def answer_of(attr) -> str:
    text = dig(attr, 2, 0, 0, 1)
    return text if isinstance(text, str) else ""


def code_of(attr) -> str:
    code = dig(attr, 0, 0)
    return code if isinstance(code, str) else ""


def review_text(content) -> tuple[str, str]:
    original = dig(content, 15, 0, 0)
    translated = dig(content, 15, 1, 0)
    original = original if isinstance(original, str) else ""
    translated = translated if isinstance(translated, str) else ""
    lang = dig(content, 14, 0)
    lang = lang if isinstance(lang, str) else ""
    korean = original if lang == "ko" or not translated else translated
    return original, korean


def rating_of(item, content):
    rating = dig(content, 0, 0)
    if isinstance(rating, (int, float)) and rating <= 5:
        return rating
    for index in (12, 13):
        value = dig(item, 1, index, 4)
        if isinstance(value, (int, float)) and value <= 5:
            return value
    return ""


def likes_of(item) -> int:
    likes = dig(item, 4, 6, 1, 0, 1)
    return int(likes) if isinstance(likes, (int, float)) else 0


def attributes_of(content) -> dict[str, str]:
    found = {"방문일": "", "대기시간": "", "예약권장": "", "체류시간": ""}
    attrs = dig(content, 6) or []
    if not isinstance(attrs, list):
        return found
    for attr in attrs:
        if not isinstance(attr, list):
            continue
        code = code_of(attr)
        label = attr[5] if len(attr) > 5 and isinstance(attr[5], str) else ""
        answer = answer_of(attr)
        if code == "TTD_DAY_OF_VISIT" or label == "방문일":
            found["방문일"] = answer
        elif code == "TTD_WAIT_TIME" or label == "대기 시간":
            found["대기시간"] = answer
        elif code == "TTD_ADVANCE_RESERVATIONS" or label == "예약 권장":
            found["예약권장"] = answer
        elif code == "PPSF_VISIT_DURATION" or label == "방문 시간":
            found["체류시간"] = answer
    return found


def excel_safe(value) -> str:
    """엑셀이 식이나 이름으로 읽는 글자(-, =)를 그대로 보이게 한다."""
    text = "" if value is None else str(value)
    if text.startswith(("=", "+", "-", "@")):
        return "'" + text
    return text


def row_from_review(item) -> dict | None:
    if isinstance(item, list) and len(item) == 1 and isinstance(item[0], list):
        item = item[0]
    if not isinstance(item, list) or not item or not isinstance(item[0], str):
        return None
    if dig(item, 1, 0) != PLACE_ID:
        return None
    us = dig(item, 1, 2)
    name = dig(item, 1, 4, 5, 0)
    if not isinstance(us, (int, float)) or not isinstance(name, str):
        return None
    content = item[2] if len(item) > 2 else None
    original, korean = review_text(content)
    attrs = attributes_of(content)
    return {
        "review_id": item[0],
        "us": int(us),
        "아이디": name,
        "날짜": format_kst(int(us)),
        "게시글_원문": original,
        "게시글": korean,
        "평점": rating_of(item, content),
        "방문일": attrs["방문일"],
        "대기시간": attrs["대기시간"],
        "예약권장": attrs["예약권장"],
        "체류시간": attrs["체류시간"],
        "좋아요수": likes_of(item),
    }


def ingest(text: str, seen: set[str], rows: list[dict], cutoff: int) -> int | None:
    """응답 한 페이지를 넣고, 그 페이지에서 가장 최근 리뷰의 시각을 돌려준다."""
    inner = parse_inner(text)
    reviews = inner[2] if isinstance(inner, list) and len(inner) > 2 else None
    if not isinstance(reviews, list):
        return None
    page_max = 0
    for item in reviews:
        if isinstance(item, list) and len(item) == 1:
            item = item[0]
        parsed = row_from_review(item)
        if not parsed:
            continue
        page_max = max(page_max, parsed["us"])
        if parsed["review_id"] in seen or parsed["us"] < cutoff:
            seen.add(parsed["review_id"])
            continue
        seen.add(parsed["review_id"])
        rows.append(parsed)
    return page_max or None


def take_blobs(driver) -> list[str]:
    return driver.execute_script(
        "const blobs = window.__blobs || []; window.__blobs = []; return blobs;"
    )


def click_when_ready(driver, xpath: str, timeout: int = 25) -> None:
    element = WebDriverWait(driver, timeout).until(
        EC.element_to_be_clickable((By.XPATH, xpath))
    )
    driver.execute_script("arguments[0].click();", element)


def open_newest_reviews(driver) -> None:
    driver.get(MAPS_URL)
    click_when_ready(driver, "//button[@role='tab' and normalize-space()='리뷰']")
    time.sleep(1.5)
    click_when_ready(driver, "//button[@aria-label='리뷰 정렬']")
    time.sleep(0.4)
    click_when_ready(driver, "//*[@role='menuitemradio' and contains(., '최신순')]")
    time.sleep(2)


def collect(driver, cutoff: int, stall_limit: int) -> list[dict]:
    seen: set[str] = set()
    rows: list[dict] = []
    stall = 0
    reached_cutoff = False

    while not reached_cutoff and stall < stall_limit:
        for blob in take_blobs(driver):
            page_max = ingest(blob, seen, rows, cutoff)
            if page_max is not None and page_max < cutoff:
                reached_cutoff = True
        before = len(rows)
        state = driver.execute_script(SCROLL)
        time.sleep(0.85)
        if not state or not state.get("ok"):
            stall += 1
            continue
        if len(rows) == before:
            stall += 1
        else:
            stall = 0
        print(
            f"수집 {len(rows)}건 | 화면 {state.get('first', '')} | "
            f"체류시간 {sum(1 for row in rows if row['체류시간'])}건",
            flush=True,
        )

    for blob in take_blobs(driver):
        ingest(blob, seen, rows, cutoff)
    rows.sort(key=lambda row: row["us"], reverse=True)
    return rows


def write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow({column: excel_safe(row[column]) for column in COLUMNS})


def build_driver(headless: bool, user_data_dir: str | None) -> webdriver.Chrome:
    options = webdriver.ChromeOptions()
    options.add_argument("--lang=ko-KR")
    options.add_argument("--window-size=1400,1000")
    if headless:
        options.add_argument("--headless=new")
    if user_data_dir:
        options.add_argument(f"--user-data-dir={user_data_dir}")
    driver = webdriver.Chrome(options=options)
    driver.execute_cdp_cmd(
        "Page.addScriptToEvaluateOnNewDocument",
        {"source": HOOK},
    )
    return driver


def main() -> None:
    parser = argparse.ArgumentParser(description="에버랜드 구글 지도 리뷰를 CSV로 저장한다.")
    parser.add_argument(
        "--output",
        default="everland_reviews.csv",
        help="저장할 CSV 경로. 기본값은 이 스크립트와 같은 폴더의 everland_reviews.csv",
    )
    parser.add_argument("--cutoff", default="2024-01-01", help="이 날짜(KST)부터 수집한다.")
    parser.add_argument("--headless", action="store_true", help="브라우저 창을 띄우지 않는다.")
    parser.add_argument("--user-data-dir", default="", help="로그인된 크롬 프로필 폴더. 비우면 새 프로필을 쓴다.")
    parser.add_argument("--stall-limit", type=int, default=12, help="새 리뷰가 이 횟수만큼 안 늘어나면 멈춘다.")
    args = parser.parse_args()

    output = Path(args.output)
    if not output.is_absolute():
        output = Path(__file__).resolve().parent / output

    driver = build_driver(args.headless, args.user_data_dir or None)
    try:
        open_newest_reviews(driver)
        rows = collect(driver, cutoff_us(args.cutoff), args.stall_limit)
    except TimeoutException as exc:
        raise SystemExit("리뷰 화면을 열지 못했습니다. 지도가 한국어로 열렸는지 확인해 주세요.") from exc
    finally:
        driver.quit()

    write_csv(output, rows)
    with_stay = sum(1 for row in rows if row["체류시간"])
    oldest = rows[-1]["날짜"] if rows else "-"
    newest = rows[0]["날짜"] if rows else "-"
    print(f"저장 {output}")
    print(f"{len(rows)}건, {oldest} ~ {newest}, 체류시간 {with_stay}건")


if __name__ == "__main__":
    main()
