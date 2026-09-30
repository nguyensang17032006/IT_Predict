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

REQUEST_DELAY = 10


# =========================================================
# CHECK BLOCK
# =========================================================

def is_blocked(page, status):
    if status in (403, 429):
        return True

    try:
        title = page.title().lower()

        blocked_keywords = [
            "attention required",
            "blocked",
            "access denied",
            "too many requests",
            "sorry, you have been blocked",
        ]

        if any(keyword in title for keyword in blocked_keywords):
            return True

    except Exception:
        pass

    try:
        html = page.content().lower()

        blocked_keywords = [
            "sorry, you have been blocked",
            "attention required",
            "access denied",
            "too many requests",
        ]

        if any(keyword in html for keyword in blocked_keywords):
            return True

    except Exception:
        pass

    return False


# =========================================================
# CHECK JOB DETAIL
# =========================================================

def is_real_job_page(page):
    # 1. Ưu tiên JSON-LD JobPosting
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

    # 2. Một số selector TopCV thường gặp
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

    # 3. Fallback cho URL /tuyen-dung/
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
# DOWNLOAD ONE JOB
# =========================================================

def download_one_job(p, job_id, url, output_path):
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

        if status != 200:
            if is_blocked(page, status):
                print("✗ TopCV/Cloudflare block request.")
                return "blocked"

            print(f"✗ HTTP {status} -> không lưu")
            return "failed"

        page.wait_for_timeout(5000)

        if is_blocked(page, status):
            print("✗ Nội dung là Cloudflare/block page -> không lưu")
            return "blocked"

        if not is_real_job_page(page):
            print("✗ Không xác nhận được đây là trang job detail -> không lưu")
            return "failed"

        html = page.content()

        if not html or len(html) < 1000:
            print("✗ HTML quá ngắn/bất thường -> không lưu")
            return "failed"

        output_path.write_text(
            html,
            encoding="utf-8"
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
# MAIN
# =========================================================

def main():
    if not INPUT_FILE.exists():
        raise FileNotFoundError(
            f"Không tìm thấy: {INPUT_FILE}"
        )

    df = pd.read_csv(
        INPUT_FILE,
        dtype={"job_id": str}
    )

    if "status" not in df.columns:
        raise ValueError(
            f"{INPUT_FILE} phải có cột 'status'."
        )

    # Chỉ lấy job active
    active_df = (
        df[df["status"] == "active"]
        .copy()
        .reset_index(drop=True)
    )

    # =====================================================
    # QUAN TRỌNG: ĐẢO NGƯỢC -> CHẠY TỪ CUỐI FILE LÊN ĐẦU
    # =====================================================
    active_df = active_df.iloc[::-1].reset_index(drop=True)

    total = len(active_df)

    success = 0
    skipped = 0
    failed = 0
    blocked = False

    print("=" * 80)
    print("TOPCV DOWNLOAD DETAILS - CHẠY TỪ CUỐI FILE")
    print("=" * 80)
    print("Active jobs :", total)
    print("Input       :", INPUT_FILE)
    print("Details     :", DETAIL_FOLDER)
    print("Thứ tự      : TỪ CUỐI -> ĐẦU")
    print("=" * 80)

    with sync_playwright() as p:
        for index, row in enumerate(
            active_df.to_dict("records"),
            start=1
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

            if not job_id or not url:
                print("✗ Thiếu job_id/url -> skip")
                failed += 1
                continue

            output_path = DETAIL_FOLDER / f"{job_id}.html"

            if output_path.exists():
                print("✓ HTML đã tồn tại -> skip")
                skipped += 1
                continue

            result = download_one_job(
                p,
                job_id,
                url,
                output_path
            )

            if result == "success":
                success += 1

            elif result == "blocked":
                blocked = True

                print()
                print(
                    "✗ Dừng lượt crawl hiện tại "
                    "vì TopCV đang block."
                )

                break

            else:
                failed += 1

            print(f"Chờ {REQUEST_DELAY}s...")
            time.sleep(REQUEST_DELAY)

    print()
    print("=" * 80)
    print("KẾT THÚC TOPCV DOWNLOAD - CHẠY TỪ CUỐI")
    print("=" * 80)
    print("Active jobs        :", total)
    print("Tải mới thành công :", success)
    print("HTML đã tồn tại    :", skipped)
    print("Lỗi                :", failed)
    print("Bị block            :", blocked)
    print("Folder              :", DETAIL_FOLDER)
    print("=" * 80)


if __name__ == "__main__":
    main()
