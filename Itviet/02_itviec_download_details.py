import time
from pathlib import Path

import pandas as pd
import requests
from bs4 import BeautifulSoup


# =========================================================
# PATH
# =========================================================

# Folder chứa file 02_itviec_download_details.py
BASE_DIR = Path(__file__).resolve().parent

# CareerPredict/Itviet/itviet_job_links.csv
INPUT_FILE = BASE_DIR / "itviet_job_links.csv"

# CareerPredict/Itviet/details/
DETAIL_FOLDER = BASE_DIR / "details"


# =========================================================
# CONFIG
# =========================================================

REQUEST_DELAY = 2

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/130.0.0.0 Safari/537.36"
    )
}

session = requests.Session()
session.headers.update(HEADERS)


# =========================================================
# CHECK JOB POSTING
# =========================================================

def contains_jobposting(html):
    soup = BeautifulSoup(html, "html.parser")

    for script in soup.find_all("script", type="application/ld+json"):
        text = script.string or script.get_text()

        if text and "JobPosting" in text:
            return True

    return False


# =========================================================
# MAIN
# =========================================================

def main():

    # -----------------------------------------------------
    # Kiểm tra file input
    # -----------------------------------------------------

    if not INPUT_FILE.exists():
        raise FileNotFoundError(
            f"Không tìm thấy file:\n{INPUT_FILE}\n"
            "Hãy chạy 01_itviec_link.py trước."
        )

    # Tự động tạo folder details nếu chưa có
    DETAIL_FOLDER.mkdir(parents=True, exist_ok=True)

    print("=" * 80)
    print("ITVIEC - DOWNLOAD JOB DETAILS")
    print("Input :", INPUT_FILE)
    print("Output:", DETAIL_FOLDER)
    print("=" * 80)

    # -----------------------------------------------------
    # Đọc danh sách job
    # -----------------------------------------------------

    df = pd.read_csv(
        INPUT_FILE,
        dtype={"job_id": str}
    )

    if "status" not in df.columns:
        raise ValueError(
            "File itviet_job_links.csv phải có cột 'status'."
        )

    # Chỉ tải những job còn active
    active_df = df[df["status"] == "active"].copy()

    total = len(active_df)

    success = 0
    skipped = 0
    failed = 0

    # -----------------------------------------------------
    # Download từng job
    # -----------------------------------------------------

    for index, row in enumerate(
        active_df.to_dict("records"),
        start=1
    ):

        job_id = str(row.get("job_id") or "").strip()
        url = str(row.get("url") or "").strip()

        # -------------------------------------------------
        # Check dữ liệu
        # -------------------------------------------------

        if not job_id or not url:
            print(f"[{index}/{total}] ✗ Thiếu job_id hoặc url")
            failed += 1
            continue

        output_path = DETAIL_FOLDER / f"{job_id}.html"

        print()
        print("=" * 80)
        print(f"[{index}/{total}] JOB {job_id}")
        print(url)

        # -------------------------------------------------
        # Nếu HTML đã tồn tại thì không tải lại
        # -------------------------------------------------

        if output_path.exists():
            print("✓ HTML đã tồn tại -> skip")
            skipped += 1
            continue

        # -------------------------------------------------
        # Download
        # -------------------------------------------------

        try:
            response = session.get(
                url,
                timeout=30
            )

            print("Status:", response.status_code)

            if response.status_code != 200:
                print(
                    f"✗ HTTP {response.status_code} "
                    "-> không lưu HTML"
                )

                failed += 1
                time.sleep(REQUEST_DELAY)
                continue

            html = response.text

            # -------------------------------------------------
            # Check xem đây có còn là trang JobPosting không
            # -------------------------------------------------

            if not contains_jobposting(html):
                print(
                    "✗ Trang trả về 200 nhưng không tìm thấy "
                    "JobPosting JSON-LD -> không lưu"
                )

                failed += 1
                time.sleep(REQUEST_DELAY)
                continue

            # -------------------------------------------------
            # Save HTML
            # -------------------------------------------------

            output_path.write_text(
                html,
                encoding="utf-8"
            )

            success += 1

            print("✓ Saved:", output_path)

        except requests.RequestException as e:
            failed += 1
            print("✗ Request error:", e)

        except Exception as e:
            failed += 1
            print("✗ Error:", e)

        time.sleep(REQUEST_DELAY)

    # -----------------------------------------------------
    # Summary
    # -----------------------------------------------------

    print()
    print("=" * 80)
    print("KẾT QUẢ DOWNLOAD DETAIL ITVIEC")
    print("Active hiện tại    :", total)
    print("Tải mới thành công :", success)
    print("HTML đã tồn tại    :", skipped)
    print("Lỗi                :", failed)
    print("Folder             :", DETAIL_FOLDER)
    print("=" * 80)


if __name__ == "__main__":
    main()