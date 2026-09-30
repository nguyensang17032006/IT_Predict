from pathlib import Path
import time

import pandas as pd
from playwright.sync_api import sync_playwright


# =========================================================
# CONFIG
# =========================================================

BASE_DIR = Path(__file__).resolve().parent

INPUT_FILE = BASE_DIR / "topcv_job_links.csv"
DETAIL_FOLDER = BASE_DIR / "details"

DETAIL_FOLDER.mkdir(parents=True, exist_ok=True)

REQUEST_DELAY = 2

# Một link bị block / không vào được tối đa 3 lần thì bỏ qua link đó.
MAX_RETRIES_PER_JOB = 3
RETRY_DELAY = 5

# Các HTTP status được xem là block/rate-limit.
BLOCK_STATUS = {403, 419, 429}


# =========================================================
# CHECK BLOCK
# =========================================================

def is_blocked(page, status):
    if status in BLOCK_STATUS:
        return True

    blocked_keywords = [
        "attention required",
        "blocked",
        "access denied",
        "too many requests",
        "sorry, you have been blocked",
    ]

    try:
        title = page.title().lower()
        if any(keyword in title for keyword in blocked_keywords):
            return True
    except Exception:
        pass

    try:
        html = page.content().lower()
        if any(keyword in html for keyword in blocked_keywords):
            return True
    except Exception:
        pass

    return False


# =========================================================
# CHECK JOB DETAIL
# =========================================================

def is_real_job_page(page):
    """
    Kiểm tra trang hiện tại có thực sự là trang chi tiết việc làm hay không.
    """

    # Cách 1: JSON-LD JobPosting
    try:
        scripts = page.locator(
            'script[type="application/ld+json"]'
        ).all_text_contents()

        for script in scripts:
            if "JobPosting" in script:
                print("✓ Xác nhận job bằng JSON-LD JobPosting")
                return True
    except Exception as e:
        print("Không đọc được JSON-LD:", e)

    # Cách 2: selector thường gặp
    selectors = [
        "h1.box-header-job__title",
        ".box-header-job__title",
        "h1.job-detail__info--title",
        ".job-detail__info--title",
        "h1.job-detail-info__title",
        ".job-detail-info__title",
        "h1.job-detail__title",
        ".job-detail__title",
    ]

    for selector in selectors:
        try:
            locator = page.locator(selector)

            if locator.count() > 0:
                text = locator.first.text_content()

                if text and text.strip():
                    print(f"✓ Xác nhận job bằng selector: {selector}")
                    print("Job title:", text.strip())
                    return True
        except Exception:
            pass

    # Cách 3: fallback cho /brand/.../tuyen-dung/
    try:
        h1_locator = page.locator("h1")

        if h1_locator.count() > 0:
            h1_texts = []

            for i in range(min(h1_locator.count(), 10)):
                try:
                    text = h1_locator.nth(i).text_content()

                    if text and text.strip():
                        h1_texts.append(text.strip())
                except Exception:
                    pass

            if h1_texts:
                print("Các H1 tìm thấy:", h1_texts)

                current_url = page.url.lower()

                if "/tuyen-dung/" in current_url:
                    print("✓ Xác nhận job bằng H1 + URL /tuyen-dung/")
                    return True
    except Exception:
        pass

    return False


# =========================================================
# DOWNLOAD ONE JOB - ONE ATTEMPT
# =========================================================

def download_one_job_once(p, job_id, url, output_path):
    """
    Mỗi lần thử sẽ mở một browser mới.

    Trả về:
        success -> tải và lưu HTML thành công
        blocked -> bị block/rate-limit
        skip    -> URL không phải job detail
        failed  -> lỗi khác / không vào được
    """

    browser = None
    context = None

    try:
        print("Mở browser mới...")

        browser = p.chromium.launch(
            headless=False,
            args=[
                "--window-position=-32000,-32000",
                "--window-size=800,600",
            ],
        )

        context = browser.new_context(
            locale="vi-VN",
            viewport={
                "width": 1280,
                "height": 800,
            },
        )

        page = context.new_page()

        response = page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=60000,
        )

        status = response.status if response else None

        print("Status:", status)
        print("Final URL:", page.url)

        try:
            print("Page title:", page.title())
        except Exception:
            pass

        # HTTP không phải 200
        if status != 200:
            if is_blocked(page, status):
                print("✗ TopCV/Cloudflare block request.")
                return "blocked"

            print(f"✗ HTTP {status} -> không vào được")
            return "failed"

        # Chờ JS render
        page.wait_for_timeout(5000)

        # Kiểm tra block sau render
        if is_blocked(page, status):
            print("✗ Nội dung là Cloudflare/block page")
            return "blocked"

        # Không phải job detail -> skip ngay, không retry
        if not is_real_job_page(page):
            print("✗ Không phải trang job detail -> bỏ qua link này")

            try:
                print("Số thẻ H1:", page.locator("h1").count())
            except Exception:
                pass

            return "skip"

        html = page.content()

        if not html or len(html) < 1000:
            print("✗ HTML quá ngắn/bất thường")
            return "failed"

        output_path.write_text(
            html,
            encoding="utf-8",
        )

        print("✓ Saved:", output_path)
        return "success"

    except Exception as e:
        print("✗ Error:", e)
        return "failed"

    finally:
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

        print("Đã đóng browser.")


# =========================================================
# DOWNLOAD ONE JOB - RETRY MAX 3
# =========================================================

def download_one_job(p, job_id, url, output_path):
    """
    Nếu link bị block hoặc không vào được:
        thử lại tối đa MAX_RETRIES_PER_JOB lần.

    Sau 3 lần vẫn không vào được:
        skip link đó và tiếp tục job kế tiếp.

    Nếu không phải job_detail:
        skip ngay, không thử lại.
    """

    for attempt in range(1, MAX_RETRIES_PER_JOB + 1):

        print(
            f"Thử link lần {attempt}/{MAX_RETRIES_PER_JOB}"
        )

        result = download_one_job_once(
            p,
            job_id,
            url,
            output_path,
        )

        if result == "success":
            return "success"

        if result == "skip":
            return "skip"

        # blocked hoặc failed -> retry
        if attempt < MAX_RETRIES_PER_JOB:
            print(
                f"⚠ Chưa vào được link "
                f"({attempt}/{MAX_RETRIES_PER_JOB})"
            )
            print(
                f"Chờ {RETRY_DELAY}s rồi thử lại chính link này..."
            )
            time.sleep(RETRY_DELAY)

    print(
        f"↪ Đã thử {MAX_RETRIES_PER_JOB} lần vẫn không vào được "
        "-> SKIP link này và tiếp tục."
    )

    return "retry_exhausted"


# =========================================================
# MAIN
# =========================================================

def main():
    if not INPUT_FILE.exists():
        raise FileNotFoundError(
            f"Không tìm thấy: {INPUT_FILE}"
        )

    df = pd.read_csv(
        INPUT_FILE,
        dtype={"job_id": str},
    )

    if "status" not in df.columns:
        raise ValueError(
            f"{INPUT_FILE} phải có cột 'status'."
        )

    # Chuẩn hóa status
    df["status"] = (
        df["status"]
        .fillna("")
        .astype(str)
        .str.strip()
        .str.lower()
    )

    # =====================================================
    # CHỈ CRAWL JOB CHƯA HẾT HẠN
    # =====================================================

    if "expired_at" in df.columns:
        expired_at_empty = (
            df["expired_at"].isna()
            | (df["expired_at"].astype(str).str.strip() == "")
        )

        active_df = (
            df[
                (df["status"] == "active")
                & expired_at_empty
            ]
            .copy()
            .reset_index(drop=True)
        )
    else:
        active_df = (
            df[df["status"] == "active"]
            .copy()
            .reset_index(drop=True)
        )

    total_all = len(df)
    total = len(active_df)
    total_not_active = total_all - total

    success = 0
    skipped_existing = 0
    skipped_invalid = 0
    skipped_after_retry = 0

    print("=" * 80)
    print("TOPCV DOWNLOAD DETAILS")
    print("=" * 80)
    print("Tổng link trong CSV :", total_all)
    print("Job chưa hết hạn    :", total)
    print("Job không crawl     :", total_not_active)
    print("Input               :", INPUT_FILE)
    print("Details             :", DETAIL_FOLDER)
    print("Retry mỗi link      :", MAX_RETRIES_PER_JOB)
    print("=" * 80)

    with sync_playwright() as p:

        for index, row in enumerate(
            active_df.to_dict("records"),
            start=1,
        ):

            job_id = str(
                row.get("job_id", "")
            ).strip()

            url = str(
                row.get("url", "")
            ).strip()

            print()
            print("=" * 80)
            print(f"[{index}/{total}] JOB {job_id}")
            print(url)

            # Thiếu dữ liệu -> skip
            if not job_id or not url:
                print("✗ Thiếu job_id/url -> skip")
                skipped_invalid += 1
                continue

            output_path = (
                DETAIL_FOLDER /
                f"{job_id}.html"
            )

            # HTML đã tồn tại -> skip
            if output_path.exists():
                print("✓ HTML đã tồn tại -> skip")
                skipped_existing += 1
                continue

            result = download_one_job(
                p,
                job_id,
                url,
                output_path,
            )

            if result == "success":
                success += 1

            elif result == "skip":
                skipped_invalid += 1

            elif result == "retry_exhausted":
                skipped_after_retry += 1

            # Không bao giờ break vì block.
            # Luôn tiếp tục cho đến hết active_df.

            print(f"Chờ {REQUEST_DELAY}s...")
            time.sleep(REQUEST_DELAY)

    print()
    print("=" * 80)
    print("KẾT THÚC TOPCV DOWNLOAD")
    print("=" * 80)
    print("Job chưa hết hạn      :", total)
    print("Tải mới thành công    :", success)
    print("HTML đã tồn tại       :", skipped_existing)
    print("Không phải job detail :", skipped_invalid)
    print("Skip sau 3 lần thử    :", skipped_after_retry)
    print("Folder                :", DETAIL_FOLDER)
    print("=" * 80)


if __name__ == "__main__":
    main()
