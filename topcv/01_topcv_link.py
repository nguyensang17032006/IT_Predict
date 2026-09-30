from pathlib import Path
from datetime import datetime
from urllib.parse import urljoin, urlparse
import re
import time

import pandas as pd
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright


# =========================================================
# CONFIG
# =========================================================

BASE_URL = "https://www.topcv.vn"
START_URL = "https://www.topcv.vn/tim-viec-lam-cong-nghe-thong-tin-cr257"

# File .py đang nằm trong thư mục topcv -> lưu CSV ngay cùng thư mục.
BASE_DIR = Path(__file__).resolve().parent
LINKS_FILE = BASE_DIR / "topcv_job_links.csv"

MAX_PAGES = 100
PAGE_DELAY = 10

BLOCK_STATUS = {419, 429}

BLOCK_RETRY_LIMIT = 3      # 3 lần liên tiếp
BLOCK_RETRY_DELAY = 15     # giữa mỗi lần thử: 15 giây

BROWSER_RESTART_WAIT = 60  # đóng browser rồi chờ 60 giây
MAX_BROWSER_RESTARTS = 2   # tối đa mở lại browser 2 lần

# =========================================================
# HELPER
# =========================================================

def now_text():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def clean_url(url):
    if not url:
        return ""

    parsed = urlparse(str(url).strip())
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"


def extract_job_id(url):
    path = urlparse(url).path.rstrip("/")
    match = re.search(r"/(\d+)\.html$", path)
    return match.group(1) if match else None


def read_csv_if_exists(path):
    if not path.exists():
        return pd.DataFrame()

    try:
        return pd.read_csv(
            path,
            dtype={"job_id": str},
        )
    except pd.errors.EmptyDataError:
        return pd.DataFrame()


# =========================================================
# PARSE LISTING
# =========================================================

def extract_links_from_html(html):
    soup = BeautifulSoup(html, "html.parser")
    jobs = {}

    for card in soup.select("div.job-item-search-result"):
        job_id = card.get("data-job-id")

        a = card.select_one("h3.title a[href]")
        if not a:
            continue

        href = a.get("href")
        if not href:
            continue

        url = clean_url(
            urljoin(BASE_URL, href)
        )

        job_id = str(
            job_id
            or extract_job_id(url)
            or ""
        ).strip()

        if not job_id:
            continue

        jobs[url] = {
            "job_id": job_id,
            "url": url,
        }

    return list(jobs.values())


# =========================================================
# CRAWL LISTING
# =========================================================

def collect_all_job_links():
    """
    Mỗi page:
        - Nếu bình thường -> crawl rồi sang page tiếp theo.
        - Nếu 419/429:
            + thử lại cùng page tối đa 3 lần.
            + đủ 3 lần liên tiếp -> đóng browser.
            + chờ -> mở browser mới.
            + tải lại chính page đó.
        - Nếu vẫn block sau MAX_BROWSER_RESTARTS -> dừng.
    """

    all_jobs = {}
    crawl_complete = True

    with sync_playwright() as p:

        for page_no in range(1, MAX_PAGES + 1):

            page_url = f"{START_URL}?page={page_no}"

            print()
            print("=" * 80)
            print(f"PAGE {page_no}")
            print(page_url)

            browser = None
            context = None
            page = None

            browser_restart_count = 0

            try:

                # =================================================
                # PAGE HIỆN TẠI
                # =================================================

                while True:

                    # =============================================
                    # MỞ BROWSER NẾU CHƯA CÓ
                    # =============================================

                    if browser is None:

                        print("Mở browser...")

                        browser = p.chromium.launch(
                            headless=False,
                            args=[
                                "--window-position=-32000,-32000",
                                "--window-size=800,600",
                            ],
                        )

                        context = browser.new_context(
                            locale="vi-VN"
                        )

                        page = context.new_page()

                    # =============================================
                    # RETRY 419 / 429 TRÊN CÙNG BROWSER
                    # =============================================

                    blocked_count = 0
                    response = None
                    status = None

                    while blocked_count < BLOCK_RETRY_LIMIT:

                        try:

                            response = page.goto(
                                page_url,
                                wait_until="domcontentloaded",
                                timeout=60000,
                            )

                            status = (
                                response.status
                                if response
                                else None
                            )

                            print("Status:", status)

                        except Exception as e:

                            print(
                                "Lỗi mở listing:",
                                e
                            )

                            crawl_complete = False

                            return (
                                list(all_jobs.values()),
                                crawl_complete
                            )

                        # =========================================
                        # 419 / 429
                        # =========================================

                        if status in BLOCK_STATUS:

                            blocked_count += 1

                            print(
                                f"⚠ HTTP {status} "
                                f"({blocked_count}/{BLOCK_RETRY_LIMIT})"
                            )

                            # Chưa đủ 3 lần
                            if blocked_count < BLOCK_RETRY_LIMIT:

                                print(
                                    f"Chờ {BLOCK_RETRY_DELAY}s "
                                    "rồi tải lại chính page này..."
                                )

                                time.sleep(
                                    BLOCK_RETRY_DELAY
                                )

                                continue

                            # Đủ 3 lần
                            print(
                                f"✗ Bị {status} "
                                f"{BLOCK_RETRY_LIMIT} lần liên tiếp."
                            )

                            break

                        # =========================================
                        # STATUS KHÁC 200
                        # =========================================

                        if status != 200:

                            print(
                                f"HTTP {status} -> "
                                "dừng crawl và KHÔNG "
                                "cập nhật expired."
                            )

                            crawl_complete = False

                            return (
                                list(all_jobs.values()),
                                crawl_complete
                            )

                        # =========================================
                        # STATUS 200
                        # =========================================

                        break

                    # =============================================
                    # NẾU 419/429 ĐỦ 3 LẦN
                    # =============================================

                    if status in BLOCK_STATUS:

                        browser_restart_count += 1

                        print(
                            "Đóng browser do bị block..."
                        )

                        if context:
                            try:
                                context.close()
                            except Exception:
                                pass

                        if browser:
                            try:
                                browser.close()
                            except Exception:
                                pass

                        context = None
                        browser = None
                        page = None

                        # -----------------------------------------
                        # Quá số lần restart cho phép
                        # -----------------------------------------

                        if (
                            browser_restart_count
                            > MAX_BROWSER_RESTARTS
                        ):

                            print(
                                "✗ Đã mở lại browser "
                                f"{MAX_BROWSER_RESTARTS} lần "
                                "nhưng vẫn bị block."
                            )

                            print(
                                "Dừng crawl và KHÔNG "
                                "cập nhật expired."
                            )

                            crawl_complete = False

                            return (
                                list(all_jobs.values()),
                                crawl_complete
                            )

                        # -----------------------------------------
                        # Chờ rồi mở lại
                        # -----------------------------------------

                        print(
                            f"Chờ {BROWSER_RESTART_WAIT}s "
                            "trước khi mở browser mới..."
                        )

                        time.sleep(
                            BROWSER_RESTART_WAIT
                        )

                        print(
                            "Mở browser mới và thử lại "
                            f"PAGE {page_no}..."
                        )

                        # Quay lên đầu while True
                        # → mở browser mới
                        # → vẫn dùng page_url hiện tại

                        continue

                    # =============================================
                    # ĐÃ LOAD PAGE THÀNH CÔNG
                    # =============================================

                    break

                # =================================================
                # PAGE LOAD OK
                # =================================================

                page.wait_for_timeout(4000)

                html = page.content()

                jobs = extract_links_from_html(
                    html
                )

                print(
                    "Job trên page:",
                    len(jobs)
                )

                # =================================================
                # HẾT LISTING
                # =================================================

                if not jobs:

                    print(
                        "Không còn job -> dừng crawl."
                    )

                    break

                # =================================================
                # ADD JOB
                # =================================================

                new_count = 0

                for item in jobs:

                    url = item["url"]

                    if url not in all_jobs:

                        all_jobs[url] = item

                        new_count += 1

                print(
                    "Job URL mới:",
                    new_count
                )

                # =================================================
                # PAGE LẶP
                # =================================================

                if new_count == 0:

                    print(
                        "Page không có URL mới "
                        "-> dừng."
                    )

                    # An toàn: không expire nếu pagination bất thường
                    crawl_complete = False

                    break

            except Exception as e:

                print(
                    "Lỗi crawler:",
                    e
                )

                crawl_complete = False

                break

            finally:

                # =================================================
                # PAGE XONG -> ĐÓNG BROWSER
                # =================================================

                if context:

                    try:
                        context.close()

                    except Exception:
                        pass

                if browser:

                    try:
                        browser.close()

                    except Exception:
                        pass

                print(
                    f"Đã đóng browser PAGE {page_no}"
                )

            # =====================================================
            # DELAY TRƯỚC PAGE TIẾP
            # =====================================================

            if page_no < MAX_PAGES:

                print(
                    f"Chờ {PAGE_DELAY}s..."
                )

                time.sleep(PAGE_DELAY)

    return (
        list(all_jobs.values()),
        crawl_complete
    )

# =========================================================
# SYNC LIFECYCLE
# =========================================================

def sync_job_links():
    old_df = read_csv_if_exists(LINKS_FILE)

    current_jobs, crawl_complete = (
        collect_all_job_links()
    )

    current_map = {
        clean_url(item["url"]): item
        for item in current_jobs
        if item.get("url")
    }

    current_set = set(current_map)
    now = now_text()

    # -----------------------------------------
    # DỮ LIỆU CŨ
    # -----------------------------------------

    if old_df.empty:
        old_map = {}
        old_active = set()

    else:
        if "url" not in old_df.columns:
            raise ValueError(
                f"{LINKS_FILE} phải có cột 'url'."
            )

        old_df = old_df.copy()

        old_df["url"] = (
            old_df["url"]
            .astype(str)
            .map(clean_url)
        )

        old_df = old_df.drop_duplicates(
            subset=["url"],
            keep="last",
        )

        old_map = {
            row["url"]: row.to_dict()
            for _, row in old_df.iterrows()
        }

        if "status" in old_df.columns:
            old_active = set(
                old_df.loc[
                    old_df["status"] == "active",
                    "url",
                ]
            )
        else:
            old_active = set(
                old_df["url"]
            )

    # -----------------------------------------
    # SO SÁNH LIFECYCLE
    # -----------------------------------------

    old_all = set(old_map)

    new_urls = (
        current_set
        - old_all
    )

    expired_urls = (
        old_active
        - current_set
    ) if crawl_complete else set()

    reactivated_urls = {
        url
        for url in current_set & old_all
        if str(
            old_map[url].get(
                "status",
                "active",
            )
        ) == "expired"
    }

    rows = []

    for url in sorted(
        old_all | current_set
    ):
        old = old_map.get(
            url,
            {},
        )

        current = current_map.get(
            url,
            {},
        )

        first_seen = old.get(
            "first_seen"
        )

        if (
            first_seen is None
            or pd.isna(first_seen)
            or str(first_seen).strip() == ""
        ):
            first_seen = now

        last_seen = old.get(
            "last_seen"
        )

        if (
            last_seen is None
            or pd.isna(last_seen)
            or str(last_seen).strip() == ""
        ):
            last_seen = first_seen

        expired_at = old.get(
            "expired_at",
            "",
        )

        if (
            expired_at is None
            or pd.isna(expired_at)
        ):
            expired_at = ""

        old_status = str(
            old.get(
                "status",
                "active",
            )
        )

        # -------------------------------------
        # JOB ĐANG CÒN TRÊN LISTING
        # -------------------------------------

        if url in current_set:
            status = "active"
            last_seen = now
            expired_at = ""

        # -------------------------------------
        # JOB BIẾN MẤT
        # chỉ expire nếu listing crawl hoàn chỉnh
        # -------------------------------------

        elif crawl_complete:
            status = "expired"

            if (
                old_status != "expired"
                or not str(expired_at).strip()
            ):
                expired_at = now

        # -------------------------------------
        # LISTING CRAWL LỖI
        # giữ nguyên status cũ
        # -------------------------------------

        else:
            status = old_status

        rows.append(
            {
                "job_id": str(
                    old.get("job_id")
                    or current.get("job_id")
                    or extract_job_id(url)
                    or ""
                ),
                "url": url,
                "status": status,
                "first_seen": first_seen,
                "last_seen": last_seen,
                "expired_at": expired_at,
            }
        )

    links_df = pd.DataFrame(rows)

    if not links_df.empty:
        links_df["job_id"] = (
            links_df["job_id"]
            .astype("string")
        )

    links_df.to_csv(
        LINKS_FILE,
        index=False,
        encoding="utf-8-sig",
    )

    # -----------------------------------------
    # REPORT
    # -----------------------------------------

    print()
    print("=" * 80)
    print("ĐỒNG BỘ LINK TOPCV")
    print("=" * 80)

    print(
        "Job active hiện tại :",
        len(current_set),
    )

    print(
        "Job mới             :",
        len(new_urls),
    )

    print(
        "Job vừa hết hạn     :",
        len(expired_urls),
    )

    print(
        "Job active trở lại  :",
        len(reactivated_urls),
    )

    print(
        "Tổng URL lịch sử    :",
        len(links_df),
    )

    print(
        "File                :",
        LINKS_FILE,
    )

    if not crawl_complete:
        print(
            "CẢNH BÁO: crawl listing chưa hoàn chỉnh "
            "-> không cập nhật expired."
        )

    print("=" * 80)

    return links_df, crawl_complete


# =========================================================
# MAIN
# =========================================================

def main():
    sync_job_links()


if __name__ == "__main__":
    main()
