"""에버랜드 네이버 플레이스 방문자 리뷰 수집.

리뷰 목록은 페이지를 스크롤해도 전부 나오지 않고, 플레이스 화면이
호출하는 리뷰 API로 페이지를 넘기며 내려옵니다. 셀레니움은 리뷰 페이지를
연 뒤 그 브라우저 안에서 API를 호출하는 용도로 씁니다.

수집 조건
- 장소: 에버랜드 (place id 11574457)
- 기간: 방문일 2024-01-01 ~ 실행일
- 정렬: 최신순
- 한 페이지: 50건 (100건을 요청하면 결과가 비어 옵니다)

실행
    pip install selenium
    python crawl_everland_reviews.py
"""

from __future__ import annotations

import csv
import json
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from selenium import webdriver
from selenium.webdriver.chrome.options import Options

PLACE_ID = "11574457"
START_DATE = "2024.01.01"
PAGE_SIZE = 50
REQUEST_PAUSE_SEC = 0.4
OUTPUT_PATH = Path(__file__).with_name(
    "everland_naver_visitor_reviews_20240101_today.csv"
)

REVIEW_URL = (
    "https://pcmap.place.naver.com/place/11574457/review/visitor"
)
GRAPHQL_URL = "https://pcmap-api.place.naver.com/graphql"
CID_LIST = ["222117", "222121", "1000209", "229273"]

REVIEW_QUERY = """
query getVisitorReviews($input: VisitorReviewsInput) {
  visitorReviews(input: $input) {
    total
    items {
      id
      cursor
      rating
      body
      visitCount
      bookingItemName
      representativeVisitDateTime
      author { nickname }
      item { name }
      votedKeywords { name }
      visitCategories {
        code
        name
        keywords { code name }
      }
    }
  }
}
""".strip()

COLUMNS = [
    "아이디",
    "날짜",
    "게시글",
    "평점",
    "결제유형",
    "방문유형",
    "동행자",
    "재방문",
    "키워드 리뷰",
]

KST = timezone(timedelta(hours=9))
COMPANION_WORDS = {
    "연인・배우자",
    "친구",
    "지인・동료",
    "혼자",
    "아이",
    "부모님",
    "친척・형제자매",
    "반려동물",
}
VISIT_WORDS = {
    "데이트",
    "친목",
    "여행",
    "나들이",
    "일상",
    "기념일",
    "회식",
    "가족모임",
    "비즈니스",
    "업무",
    "기타",
}
SKIP_CODE = re.compile(
    r"^(v_no_reservation|v_with_reservation|v_enter_immediately|"
    r"v_within_10minutes|v_within_30minutes|v_within_1hour|v_within_2hours|"
    r"v_more_than_30minutes|v_more_than_1hour|v_more_than_2hours|v_waiting)$"
)
COMPANION_CODE = re.compile(
    r"^(v_child|v_friend|v_significant_other|v_solo|v_parents|v_relative|"
    r"v_sibling|v_pet|v_colleague|v_coworker|v_acquaintance|v_alone|"
    r"v_kids|v_family|v_partner)$"
)
TICKET_NAME = re.compile(r"이용권|티켓|입장|종일권|야간권|오후권")


def today_kst() -> str:
    return datetime.now(KST).strftime("%Y.%m.%d")


def to_kst_date(iso_text: str | None) -> str:
    """방문 시각을 한국 날짜(YYYY.MM.DD)로 바꿉니다.

    네이버는 날짜만 있는 값을 전날 15:00 UTC로 내려줍니다.
    예: 2026-10-21T15:00:00Z -> 2026.10.22
    """
    if not iso_text:
        return ""
    parsed = datetime.fromisoformat(iso_text.replace("Z", "+00:00"))
    return parsed.astimezone(KST).strftime("%Y.%m.%d")


def normalize_label(text: str) -> str:
    return re.sub(r"[·･∙・]", "・", text or "").strip()


def unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            ordered.append(value)
    return ordered


def payment_type(item: dict) -> str:
    booked = (item.get("bookingItemName") or "").strip()
    if booked:
        return booked
    receipt_item = item.get("item") or {}
    name = (receipt_item.get("name") or "").strip()
    if name and TICKET_NAME.search(name):
        return name
    return ""


def split_visit_tags(item: dict) -> tuple[str, str]:
    visit_types: list[str] = []
    companions: list[str] = []
    for category in item.get("visitCategories") or []:
        code = category.get("code") or ""
        if category.get("name") == "대기 시간" or SKIP_CODE.match(code):
            continue
        companion_bucket = bool(COMPANION_CODE.match(code))
        for keyword in category.get("keywords") or []:
            label = normalize_label(keyword.get("name") or keyword.get("code") or "")
            if not label:
                continue
            if label in COMPANION_WORDS or (companion_bucket and label not in VISIT_WORDS):
                companions.append(label)
            else:
                visit_types.append(label)
    return ", ".join(unique(visit_types)), ", ".join(unique(companions))


def to_row(item: dict) -> list[str]:
    visit_type, companion = split_visit_tags(item)
    keywords = " ".join(
        name
        for keyword in (item.get("votedKeywords") or [])
        if (name := (keyword.get("name") or "").strip())
    )
    visit_count = item.get("visitCount")
    revisit = f"{visit_count}번째 방문" if visit_count is not None else ""
    rating = item.get("rating")
    author = item.get("author") or {}
    return [
        author.get("nickname") or "",
        to_kst_date(item.get("representativeVisitDateTime")),
        item.get("body") or "",
        "" if rating is None else str(rating),
        payment_type(item),
        visit_type,
        companion,
        revisit,
        keywords,
    ]


def make_driver() -> webdriver.Chrome:
    options = Options()
    options.add_argument("--window-size=1400,1000")
    options.add_argument("--lang=ko-KR")
    options.page_load_strategy = "eager"
    driver = webdriver.Chrome(options=options)
    driver.set_script_timeout(60)
    return driver


def fetch_page(driver: webdriver.Chrome, cursor: str | None) -> dict:
    payload = {
        "businessId": PLACE_ID,
        "businessType": "place",
        "item": "0",
        "size": PAGE_SIZE,
        "sort": "recent",
        "isPhotoUsed": False,
        "includeContent": True,
        "getUserStats": True,
        "includeReceiptPhotos": True,
        "cidList": CID_LIST,
        "getReactions": True,
        "getTrailer": True,
    }
    if cursor:
        payload["after"] = cursor

    script = """
        const callback = arguments[arguments.length - 1];
        const url = arguments[0];
        const query = arguments[1];
        const input = arguments[2];
        fetch(url, {
            method: "POST",
            headers: { "content-type": "application/json" },
            body: JSON.stringify([{
                operationName: "getVisitorReviews",
                variables: { input },
                query
            }])
        })
        .then((response) => response.text())
        .then((text) => callback(text))
        .catch((error) => callback("ERROR " + String(error)));
    """
    last_error = "unknown"
    for attempt in range(5):
        raw = driver.execute_async_script(script, GRAPHQL_URL, REVIEW_QUERY, payload)
        if not isinstance(raw, str) or raw.startswith("ERROR") or raw.lstrip().startswith("<"):
            last_error = (raw or "")[:180]
            time.sleep(2.5 * (attempt + 1))
            continue
        data = json.loads(raw)
        block = (
            data
            and data[0].get("data")
            and data[0]["data"].get("visitorReviews")
        )
        if not block or not isinstance(block.get("items"), list):
            last_error = json.dumps(data, ensure_ascii=False)[:180]
            time.sleep(2.5 * (attempt + 1))
            continue
        return block
    raise RuntimeError(f"리뷰 페이지를 가져오지 못했습니다: {last_error}")


def collect_reviews(driver: webdriver.Chrome) -> list[list[str]]:
    end_date = today_kst()
    rows: list[list[str]] = []
    seen: set[str] = set()
    cursor = None
    page_no = 0

    while True:
        block = fetch_page(driver, cursor)
        items = block["items"]
        page_no += 1
        if not items:
            break

        page_dates = []
        for item in items:
            review_id = item.get("id")
            if not review_id or review_id in seen:
                continue
            seen.add(review_id)
            visit_date = to_kst_date(item.get("representativeVisitDateTime"))
            if visit_date:
                page_dates.append(visit_date)
            if visit_date and (visit_date < START_DATE or visit_date > end_date):
                continue
            rows.append(to_row(item))

        newest = max(page_dates) if page_dates else ""
        oldest = min(page_dates) if page_dates else ""
        print(
            f"{page_no}페이지 | 누적 {len(rows)}건 | "
            f"이 페이지 {oldest or '-'} ~ {newest or '-'}"
        )

        next_cursor = items[-1].get("cursor")
        if not next_cursor or next_cursor == cursor:
            break
        if newest and newest < START_DATE:
            break
        cursor = next_cursor
        time.sleep(REQUEST_PAUSE_SEC)

    return rows


def save_csv(rows: list[list[str]], path: Path) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(COLUMNS)
        writer.writerows(rows)


def main() -> None:
    driver = make_driver()
    try:
        driver.get(REVIEW_URL)
        time.sleep(2)
        rows = collect_reviews(driver)
    finally:
        driver.quit()

    save_csv(rows, OUTPUT_PATH)
    print(f"저장 완료: {OUTPUT_PATH}")
    print(f"총 {len(rows)}건 ({START_DATE} ~ {today_kst()})")


if __name__ == "__main__":
    main()
