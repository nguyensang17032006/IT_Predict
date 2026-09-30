import re
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

import pandas as pd
import requests
from bs4 import BeautifulSoup


BASE_URL = "https://itviec.com"
START_URL = "https://itviec.com/it-jobs"

BASE_DIR = Path(__file__).resolve().parent

LINKS_FILE = BASE_DIR / "itviet_job_links.csv"

MAX_PAGES = 1000
REQUEST_DELAY = 1.5

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/130.0.0.0 Safari/537.36"
    )
}

session = requests.Session()
session.headers.update(HEADERS)


def now_text():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def set_page(url, page):
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    query["page"] = [str(page)]
    return urlunparse(parsed._replace(query=urlencode(query, doseq=True)))


def clean_url(url):
    if not url:
        return ""
    parsed = urlparse(str(url).strip())
    return urlunparse(parsed._replace(query="", fragment=""))


def is_job_detail_url(url):
    path = urlparse(url).path.rstrip("/")
    return bool(re.search(r"/it-jobs/.+-\d+$", path))


def extract_job_id(url):
    path = urlparse(url).path.rstrip("/")
    match = re.search(r"-(\d+)$", path)
    return match.group(1) if match else None


def read_csv_if_exists(path):
    if not path.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(path, dtype={"job_id": str})
    except pd.errors.EmptyDataError:
        return pd.DataFrame()


def get_job_links_from_page(url):
    """Return list[str] nếu thành công, None nếu request/HTTP lỗi."""
    try:
        response = session.get(url, timeout=30)
        print("Status:", response.status_code)

        if response.status_code != 200:
            print(f"Lỗi page: {response.status_code} - {url}")
            return None

        soup = BeautifulSoup(response.text, "html.parser")
        job_urls = set()

        for a in soup.find_all("a", href=True):
            href = a.get("href")
            if not href:
                continue

            full_url = clean_url(urljoin(BASE_URL, href))
            if is_job_detail_url(full_url):
                job_urls.add(full_url)

        return sorted(job_urls)

    except Exception as e:
        print("Lỗi lấy danh sách:", e)
        return None


def collect_all_job_links():
    all_links = set()
    crawl_complete = True

    for page_number in range(1, MAX_PAGES + 1):
        page_url = set_page(START_URL, page_number)

        print()
        print("=" * 80)
        print(f"PAGE {page_number}")
        print(page_url)

        links = get_job_links_from_page(page_url)

        if links is None:
            crawl_complete = False
            print("Không đọc được page -> dừng và KHÔNG cập nhật expired.")
            break

        if not links:
            print("Page không còn job -> dừng.")
            break

        new_links = [link for link in links if link not in all_links]
        print("Job tìm thấy:", len(links))
        print("Job URL mới ở page này:", len(new_links))

        if not new_links:
            print("Không còn URL mới -> dừng.")
            break

        all_links.update(new_links)
        time.sleep(REQUEST_DELAY)

    return sorted(all_links), crawl_complete


def sync_job_links():

    old_df = read_csv_if_exists(LINKS_FILE)
    current_links, crawl_complete = collect_all_job_links()

    current_set = set(map(clean_url, current_links))
    now = now_text()

    if old_df.empty:
        old_map = {}
        old_active = set()
    else:
        if "url" not in old_df.columns:
            raise ValueError(f"{LINKS_FILE} phải có cột 'url'.")

        old_df = old_df.copy()
        old_df["url"] = old_df["url"].astype(str).map(clean_url)
        old_df = old_df.drop_duplicates(subset=["url"], keep="last")
        old_map = {row["url"]: row.to_dict() for _, row in old_df.iterrows()}

        if "status" in old_df.columns:
            old_active = set(old_df.loc[old_df["status"] == "active", "url"])
        else:
            old_active = set(old_df["url"])

    old_all = set(old_map)
    new_urls = current_set - old_all
    expired_urls = (old_active - current_set) if crawl_complete else set()
    reactivated_urls = {
        url
        for url in current_set & old_all
        if str(old_map[url].get("status", "active")) == "expired"
    }

    all_urls = old_all | current_set
    rows = []

    for url in sorted(all_urls):
        old = old_map.get(url, {})

        first_seen = old.get("first_seen")
        if first_seen is None or pd.isna(first_seen) or str(first_seen).strip() == "":
            first_seen = now

        last_seen = old.get("last_seen")
        if last_seen is None or pd.isna(last_seen) or str(last_seen).strip() == "":
            last_seen = first_seen

        expired_at = old.get("expired_at", "")
        if pd.isna(expired_at):
            expired_at = ""

        old_status = str(old.get("status", "active"))

        if url in current_set:
            status = "active"
            last_seen = now
            expired_at = ""
        elif crawl_complete:
            status = "expired"
            if old_status != "expired" or not expired_at:
                expired_at = now
        else:
            status = old_status

        rows.append(
            {
                "job_id": old.get("job_id") or extract_job_id(url),
                "url": url,
                "status": status,
                "first_seen": first_seen,
                "last_seen": last_seen,
                "expired_at": expired_at,
            }
        )

    links_df = pd.DataFrame(rows)
    if not links_df.empty:
        links_df["job_id"] = links_df["job_id"].astype("string")

    links_df.to_csv(LINKS_FILE, index=False, encoding="utf-8-sig")

    print()
    print("=" * 80)
    print("ĐỒNG BỘ LINK ITVIEC")
    print("Job active hiện tại :", len(current_set))
    print("Job mới             :", len(new_urls))
    print("Job vừa hết hạn     :", len(expired_urls))
    print("Job active trở lại  :", len(reactivated_urls))
    print("Tổng URL lịch sử    :", len(links_df))
    print("File                 :", LINKS_FILE)

    if not crawl_complete:
        print("CẢNH BÁO: listing crawl chưa hoàn chỉnh -> không đánh dấu expired.")

    print("=" * 80)


if __name__ == "__main__":
    sync_job_links()
