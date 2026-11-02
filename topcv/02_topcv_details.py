"""Refresh active TopCV jobs once per Vietnam calendar day.
Install: pip install playwright pandas
Then: python -m playwright install chromium
Run beside topcv_job_links.csv, before step 03.
Fetch and validate each page; atomically overwrite only if HTML bytes changed.
Failed requests never replace the old HTML. Identical HTML keeps its file mtime.
Writes topcv_download_report.json for the latest run (no extra CSV).
Successful check dates and HTML hashes live in the report, independent of file mtime.
Set FORCE_REFRESH=True to check again within the same day.
HTML changes can include dynamic markup, not just changes in job fields.
"""
from pathlib import Path
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
import time

import pandas as pd


# =========================================================
# CONFIG
# =========================================================

BASE_DIR = Path(__file__).resolve().parent

INPUT_FILE = BASE_DIR / "topcv_job_links.csv"
DETAIL_FOLDER = BASE_DIR / "details"
REPORT_FILE = BASE_DIR / "topcv_download_report.json"
SITE_TZ = timezone(timedelta(hours=7))
FORCE_REFRESH = False  # True: check every active job again, even if checked today.


REQUEST_DELAY = 2

# Một link bị block / không vào được tối đa 3 lần thì bỏ qua link đó.
MAX_RETRIES_PER_JOB = 3
RETRY_DELAY = 5

# Các HTTP status được xem là block/rate-limit.
BLOCK_STATUS = {403, 419, 429}


SUCCESS_RESULTS = {"created", "updated", "unchanged"}


def now_text():
    return datetime.now(SITE_TZ).isoformat(timespec="seconds")


def file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_success_cache():
    if not REPORT_FILE.is_file():
        return {}
    try:
        report = json.loads(REPORT_FILE.read_text(encoding="utf-8"))
        cache = report.get("success_cache", {})
        return cache if isinstance(cache, dict) else {}
    except (OSError, ValueError, AttributeError):
        print("Không đọc được lịch sử tải; sẽ kiểm tra lại các tin active.")
        return {}


def already_checked_today(path, url, previous):
    if FORCE_REFRESH or not path.is_file() or not isinstance(previous, dict):
        return False
    if previous.get("url") != url:
        return False
    try:
        success = datetime.fromisoformat(previous.get("last_success_at", ""))
        now = datetime.fromisoformat(now_text())
        if success.tzinfo is None or success > now:
            return False
        if success.astimezone(SITE_TZ).date() != now.astimezone(SITE_TZ).date():
            return False
        # A changed, replaced, deleted or truncated local file must be downloaded again.
        return previous.get("html_sha256") == file_hash(path)
    except (OSError, ValueError, TypeError):
        return False


def save_html_if_changed(output_path, html):
    """Called only after validating the fetched page. Never delete the old file."""
    incoming = html.encode("utf-8")
    existed = output_path.is_file()
    if existed:
        old_hash = hashlib.sha256(output_path.read_bytes()).digest()
        if old_hash == hashlib.sha256(incoming).digest():
            return "unchanged"
    temporary = output_path.with_name(output_path.name + ".tmp")
    try:
        temporary.write_bytes(incoming)
        temporary.replace(output_path)
    finally:
        if temporary.exists():
            temporary.unlink()
    return "updated" if existed else "created"


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
        created / updated / unchanged -> đã tải và kiểm tra HTML thành công
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

        result = save_html_if_changed(output_path, html)
        if result == "unchanged":
            print("✓ Đã tải và kiểm tra: HTML giống bản cũ, không ghi lại file.")
        else:
            print("✓", "Tải mới:" if result == "created" else "Cập nhật HTML:", output_path)
        return result

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

        if result in SUCCESS_RESULTS:
            return result

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
    from playwright.sync_api import sync_playwright
    if not INPUT_FILE.is_file():
        raise FileNotFoundError(f"Không tìm thấy: {INPUT_FILE}")
    frame = pd.read_csv(INPUT_FILE, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    if not {"job_id", "url", "status"}.issubset(frame.columns):
        raise ValueError("CSV phải có các cột job_id, url, status.")
    frame["status"] = frame["status"].str.strip().str.lower()
    active = frame[frame["status"] == "active"]
    if "expired_at" in active.columns:
        active = active[active["expired_at"].str.strip() == ""]
    active = active.drop_duplicates("job_id", keep="last").reset_index(drop=True)
    DETAIL_FOLDER.mkdir(parents=True, exist_ok=True)
    cache = read_success_cache()
    started = now_text()
    results = []
    run_finished = False
    print(f"TOPCV: {len(active)} tin active. Cập nhật theo ngày Việt Nam (UTC+7).")
    print("Tin đã tải và kiểm tra thành công hôm nay: bỏ qua. Sang ngày mới: kiểm tra lại.")
    if FORCE_REFRESH:
        print("FORCE_REFRESH=True: kiểm tra lại cả tin đã tải hôm nay.")
    try:
        with sync_playwright() as playwright:
            for index, row in enumerate(active.to_dict("records"), 1):
                job_id = row["job_id"].strip()
                url = row["url"].strip()
                print(f"\n[{index}/{len(active)}] {job_id}: {url}")
                previous = cache.get(job_id, {})
                if not isinstance(previous, dict):
                    previous = {}
                record = {"job_id": job_id, "url": url, "checked_at": None,
                          "last_success_at": previous.get("last_success_at")}
                if not re.fullmatch(r"[A-Za-z0-9_-]+", job_id) or not url:
                    record.update(status="invalid_input", message="job_id hoặc URL không hợp lệ.")
                else:
                    path = DETAIL_FOLDER / f"{job_id}.html"
                    record["html_path"] = str(path)
                    if already_checked_today(path, url, previous):
                        record.update(status="skipped_today", decision_at=now_text(),
                                      message="Đã tải và kiểm tra thành công hôm nay; không truy cập lại.")
                        print("✓ SKIP: đã kiểm tra thành công hôm nay.")
                        results.append(record)
                        continue
                    status = download_one_job(playwright, job_id, url, path)
                    record.update(status=status, checked_at=now_text(),
                                  old_html_kept=status not in SUCCESS_RESULTS and path.is_file())
                    if status in SUCCESS_RESULTS:
                        cache[job_id] = {"url": url, "last_success_at": record["checked_at"],
                                         "html_sha256": file_hash(path)}
                        record["last_success_at"] = record["checked_at"]
                    if status == "unchanged":
                        record["message"] = "HTML không thay đổi; đã ghi nhận kiểm tra thành công hôm nay."
                    elif status == "updated":
                        record["message"] = "HTML đã thay đổi; lưu đè thành công."
                    elif status == "created":
                        record["message"] = "Đã lưu HTML cho tin mới."
                    else:
                        record["message"] = "Không cập nhật được tin; giữ HTML cũ nếu có. Chạy lại sẽ thử lại."
                results.append(record)
                time.sleep(REQUEST_DELAY)
        run_finished = True
    except KeyboardInterrupt:
        print("Đã dừng thủ công; HTML và ngày kiểm tra của tin đã hoàn tất được giữ lại.")
    finally:
        summary = {key: sum(x["status"] == key for x in results)
                   for key in ["created", "updated", "unchanged", "skipped_today", "skip", "retry_exhausted", "invalid_input"]}
        summary.update(total_active=len(active), processed=len(results),
                       errors=sum(x["status"] not in SUCCESS_RESULTS | {"skipped_today"} for x in results))
        report = {"schema_version": 2, "source": "TopCV", "refresh_policy": "daily",
                  "force_refresh": FORCE_REFRESH, "started_at": started, "finished_at": now_text(),
                  "status": "incomplete" if not run_finished or len(results) < len(active)
                            else "completed_with_errors" if summary["errors"] else "completed",
                  "summary": summary, "jobs": results, "success_cache": cache,
                  "notes": "Tin được bỏ qua chỉ khi đã kiểm tra thành công trong ngày Việt Nam, URL "
                           "không đổi và file HTML còn đúng hash. Tin tải lỗi không được đánh dấu thành công. "
                           "HTML thay đổi có thể do nội dung động; updated không khẳng định lương đã đổi."}
        temporary = REPORT_FILE.with_name(REPORT_FILE.name + ".tmp")
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(REPORT_FILE)
        print(f"DONE: {summary}")
        print(f"Report: {REPORT_FILE}")
    return report


if __name__ == "__main__":
    main()
