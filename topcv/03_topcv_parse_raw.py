from pathlib import Path
from datetime import datetime
import json
import re

import pandas as pd
from bs4 import BeautifulSoup


# =========================================================
# CONFIG
# =========================================================

BASE_DIR = Path(__file__).resolve().parent
LINKS_FILE = BASE_DIR / "topcv_job_links.csv"
DETAIL_FOLDER = BASE_DIR / "details"
RAW_FILE = BASE_DIR / "topcv_raw.csv"

RAW_COLUMNS = [
    "job_id", "title", "company", "location", "locations_raw", "level",
    "experience_min", "experience_max", "salary_raw_min", "salary_raw_max",
    "currency", "salary_unit", "salary_text", "skills", "work_type",
    "posted_date", "valid_through", "description", "requirements",
    "job_location_type", "source", "url", "status", "first_seen",
    "last_seen", "expired_at", "collected_at",
]


def now_text():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def read_csv_if_exists(path):
    if not path.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(path, dtype={"job_id": str})
    except pd.errors.EmptyDataError:
        return pd.DataFrame()


def clean_text(value):
    if value is None:
        return None
    soup = BeautifulSoup(str(value), "html.parser")
    text = soup.get_text(" ", strip=True)
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


def text_or_none(element):
    if element is None:
        return None
    return clean_text(element.get_text(" ", strip=True))


# =========================================================
# JSON-LD
# =========================================================

def get_jobposting_from_soup(soup):
    for script in soup.find_all("script", type="application/ld+json"):
        raw = script.string or script.get_text()
        if not raw:
            continue

        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue

        candidates = []

        if isinstance(data, dict):
            candidates.append(data)
            if isinstance(data.get("@graph"), list):
                candidates.extend(data["@graph"])
        elif isinstance(data, list):
            candidates.extend(data)

        for item in candidates:
            if isinstance(item, dict) and item.get("@type") == "JobPosting":
                return item

    return None


# =========================================================
# DOM HELPERS
# =========================================================

def extract_header_info(soup):
    result = {}
    for item in soup.select(".box-header-job-list-info__item"):
        title = text_or_none(item.select_one(".list-info__content__title"))
        value = text_or_none(item.select_one(".list-info__content__desc"))
        if title:
            result[title] = value
    return result


def extract_general_info(soup):
    result = {}
    for item in soup.select(".box-job-information-general-info-list__item"):
        title = text_or_none(
            item.select_one(
                ".box-job-information-general-info-list__item--content-title"
            )
        )
        value = text_or_none(
            item.select_one(
                ".box-job-information-general-info-list__item--content-desc"
            )
        )
        if title:
            result[title] = value
    return result


def get_section(soup, wanted_title):
    wanted = wanted_title.strip().lower()

    for section in soup.select(".box-job-information-detail-item"):
        title_el = section.select_one(
            ".box-job-information-detail-item__title--title"
        )
        if not title_el:
            continue

        title = text_or_none(title_el)
        if not title or title.strip().lower() != wanted:
            continue

        content = section.select_one(".box-job-information-detail-item__text")
        return text_or_none(content)

    return None


def extract_address_detail(soup):
    for item in soup.select(".box-job-information-address-and-time-list__item"):
        title = text_or_none(
            item.select_one(
                ".box-job-information-address-and-time-list__item--title"
            )
        )
        if title and title.strip().lower() == "địa điểm làm việc":
            return text_or_none(
                item.select_one(
                    ".box-job-information-address-and-time-list__item--content"
                )
            )
    return None


# =========================================================
# FIELD PARSERS
# =========================================================

def parse_experience(text):
    if not text:
        return None, None

    low = text.lower().strip()

    if "không yêu cầu" in low:
        return 0, 0

    match = re.search(r"(\d+)\s*[-–—]\s*(\d+)\s*năm", low)
    if match:
        return int(match.group(1)), int(match.group(2))

    match = re.search(r"trên\s*(\d+)\s*năm", low)
    if match:
        return int(match.group(1)), None

    match = re.search(r"từ\s*(\d+)\s*năm", low)
    if match:
        return int(match.group(1)), None

    match = re.search(r"dưới\s*(\d+)\s*năm", low)
    if match:
        return 0, int(match.group(1))

    match = re.search(r"(\d+)\s*năm", low)
    if match:
        value = int(match.group(1))
        return value, value

    return None, None


def extract_raw_locations(job):
    if not job:
        return []

    job_locations = job.get("jobLocation", [])
    if not isinstance(job_locations, list):
        job_locations = [job_locations]

    locations = []

    for loc in job_locations:
        if not isinstance(loc, dict):
            continue

        address = loc.get("address", {})
        if not isinstance(address, dict):
            continue

        locations.append({
            "street": address.get("streetAddress"),
            "city": address.get("addressLocality"),
            "region": address.get("addressRegion"),
            "country": address.get("addressCountry"),
        })

    return locations


def extract_location_from_raw(locations):
    values = []

    for loc in locations:
        value = loc.get("region") or loc.get("city")
        if not value:
            continue

        value = str(value).strip()
        if value and value not in values:
            values.append(value)

    if not values:
        return None
    if len(values) == 1:
        return values[0]
    return " & ".join(values)


def parse_salary_number(value):
    text = str(value).strip()
    if not text:
        return None

    if re.fullmatch(r"\d+[.,]\d{3}", text):
        return float(text.replace(".", "").replace(",", ""))

    try:
        return float(text.replace(",", "."))
    except ValueError:
        return None


def extract_salary(job, salary_text):
    result = {
        "salary_raw_min": None,
        "salary_raw_max": None,
        "currency": None,
        "salary_unit": None,
        "salary_text": salary_text,
    }

    # Ưu tiên JSON-LD.
    if job:
        base_salary = job.get("baseSalary")
        if isinstance(base_salary, dict):
            result["currency"] = base_salary.get("currency")
            value = base_salary.get("value", {})

            if isinstance(value, dict):
                result["salary_raw_min"] = value.get("minValue")
                result["salary_raw_max"] = value.get("maxValue")
                result["salary_unit"] = value.get("unitText")

    # Fallback DOM salary text.
    if (
        salary_text
        and result["salary_raw_min"] is None
        and result["salary_raw_max"] is None
    ):
        low = salary_text.lower().strip()

        if "thoả thuận" in low or "thỏa thuận" in low or "cạnh tranh" in low:
            return result

        if "usd" in low:
            result["currency"] = result["currency"] or "USD"
        elif "triệu" in low or "vnd" in low:
            result["currency"] = result["currency"] or "VND"

        numbers = re.findall(r"\d+(?:[.,]\d+)?", low)
        numbers = [parse_salary_number(x) for x in numbers]
        numbers = [x for x in numbers if x is not None]

        if numbers:
            salary_min = numbers[0]
            salary_max = numbers[1] if len(numbers) >= 2 else numbers[0]

            # Đồng nhất với JSON-LD: VND lưu theo đơn vị đồng.
            if result["currency"] == "VND" and "triệu" in low:
                salary_min *= 1_000_000
                salary_max *= 1_000_000

            result["salary_raw_min"] = salary_min
            result["salary_raw_max"] = salary_max
            result["salary_unit"] = result["salary_unit"] or "MONTH"

    return result


# =========================================================
# PARSE 1 HTML
# =========================================================

def parse_detail_html(file_path, link_info):
    html = file_path.read_text(encoding="utf-8", errors="ignore")
    soup = BeautifulSoup(html, "html.parser")

    job = get_jobposting_from_soup(soup)
    header = extract_header_info(soup)
    general = extract_general_info(soup)

    title = clean_text(job.get("title")) if job else None
    if not title:
        title = text_or_none(soup.select_one("h1.box-header-job__title"))

    company = None
    if job:
        org = job.get("hiringOrganization", {})
        if isinstance(org, dict):
            company = clean_text(org.get("name"))

    if not company:
        company = text_or_none(
            soup.select_one(".box-company-info__detail a.name")
        )

    raw_locations = extract_raw_locations(job)
    location = extract_location_from_raw(raw_locations)

    if not location:
        location = header.get("Địa điểm")

    if not raw_locations:
        address_detail = extract_address_detail(soup)
        if address_detail:
            raw_locations = [{"raw": address_detail}]

    experience_text = header.get("Kinh nghiệm")
    exp_min, exp_max = parse_experience(experience_text)

    salary_text = text_or_none(
        soup.select_one(".box-header-job__salary--title")
    )
    salary = extract_salary(job, salary_text)

    level = general.get("Cấp bậc")
    if not level and job:
        level = clean_text(job.get("occupationalCategory"))

    work_type = general.get("Loại hình làm việc")
    if not work_type and job:
        employment_type = job.get("employmentType")
        if isinstance(employment_type, list):
            work_type = ", ".join(map(str, employment_type))
        else:
            work_type = clean_text(employment_type)

    job_location_type = general.get("Hình thức làm việc")
    if not job_location_type and job:
        job_location_type = clean_text(job.get("jobLocationType"))

    posted_date = job.get("datePosted") if job else None
    valid_through = job.get("validThrough") if job else None
    if not valid_through:
        valid_through = header.get("Hạn ứng tuyển")

    description = clean_text(job.get("description")) if job else None
    if not description:
        description = get_section(soup, "Mô tả công việc")

    requirements = get_section(soup, "Yêu cầu ứng viên")

    skills = None
    if job:
        raw_skills = job.get("skills")
        if isinstance(raw_skills, list):
            skills = json.dumps(raw_skills, ensure_ascii=False)
        else:
            skills = clean_text(raw_skills)

    canonical = soup.select_one('link[rel="canonical"]')
    canonical_url = canonical.get("href") if canonical else None

    return {
        "job_id": str(link_info["job_id"]),
        "title": title,
        "company": company,
        "location": location,
        "locations_raw": json.dumps(raw_locations, ensure_ascii=False),
        "level": level,
        "experience_min": exp_min,
        "experience_max": exp_max,
        "salary_raw_min": salary["salary_raw_min"],
        "salary_raw_max": salary["salary_raw_max"],
        "currency": salary["currency"],
        "salary_unit": salary["salary_unit"],
        "salary_text": salary["salary_text"],
        "skills": skills,
        "work_type": work_type,
        "posted_date": posted_date,
        "valid_through": valid_through,
        "description": description,
        "requirements": requirements,
        "job_location_type": job_location_type,
        "source": "TopCV",
        "url": canonical_url or link_info.get("url"),
        "status": link_info.get("status"),
        "first_seen": link_info.get("first_seen"),
        "last_seen": link_info.get("last_seen"),
        "expired_at": link_info.get("expired_at"),
        "collected_at": now_text(),
    }


# =========================================================
# MAIN / MERGE RAW HISTORY
# =========================================================

def main():
    if not LINKS_FILE.exists():
        raise FileNotFoundError(f"Không tìm thấy: {LINKS_FILE}")

    links_df = pd.read_csv(LINKS_FILE, dtype={"job_id": str})
    old_raw_df = read_csv_if_exists(RAW_FILE)

    old_map = {}
    if not old_raw_df.empty:
        old_raw_df = old_raw_df.copy()
        old_raw_df["job_id"] = old_raw_df["job_id"].astype(str)
        old_map = {
            str(row["job_id"]): row
            for row in old_raw_df.to_dict("records")
        }

    rows = []
    total = len(links_df)

    for index, link_info in enumerate(links_df.to_dict("records"), start=1):
        job_id = str(link_info["job_id"]).strip()
        link_info["job_id"] = job_id
        file_path = DETAIL_FOLDER / f"{job_id}.html"

        print(f"[{index}/{total}] {job_id} {link_info.get('status')}")

        # Có HTML -> parse offline, kể cả expired.
        if file_path.exists():
            try:
                row = parse_detail_html(file_path, link_info)
                rows.append(row)
                print("   ✓ Parsed HTML")
                continue
            except Exception as e:
                print("   ✗ Parse error:", e)

        # Không có HTML nhưng raw cũ có -> giữ dữ liệu và chỉ cập nhật lifecycle.
        old = old_map.get(job_id)
        if old:
            old = dict(old)
            old["status"] = link_info.get("status")
            old["first_seen"] = link_info.get("first_seen")
            old["last_seen"] = link_info.get("last_seen")
            old["expired_at"] = link_info.get("expired_at")
            rows.append(old)
            print("   ✓ Giữ raw cũ + cập nhật status")
        else:
            print("   ⚠ Chưa có HTML/raw")

    raw_df = pd.DataFrame(rows)

    for col in RAW_COLUMNS:
        if col not in raw_df.columns:
            raw_df[col] = None

    raw_df = raw_df[RAW_COLUMNS]

    if not raw_df.empty:
        raw_df = raw_df.drop_duplicates(
            subset=["source", "job_id"],
            keep="last",
        )

    raw_df.to_csv(RAW_FILE, index=False, encoding="utf-8-sig")

    print()
    print("=" * 80)
    print("ĐÃ CẬP NHẬT TOPCV RAW")
    print("Raw file:", RAW_FILE)
    print("Tổng raw:", len(raw_df))

    if not raw_df.empty:
        print("Active :", int((raw_df["status"] == "active").sum()))
        print("Expired:", int((raw_df["status"] == "expired").sum()))

    print("=" * 80)


if __name__ == "__main__":
    main()
