import json
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import pandas as pd
from bs4 import BeautifulSoup


BASE_DIR = Path(__file__).resolve().parent

LINKS_FILE = BASE_DIR / "itviet_job_links.csv"
DETAIL_FOLDER = BASE_DIR / "details"
RAW_FILE = BASE_DIR / "itviet_raw.csv"

DETAIL_FOLDER.mkdir(parents=True, exist_ok=True)

RAW_COLUMNS = [
    "job_id",
    "title",
    "company",
    "location",
    "locations_raw",
    "level",
    "experience_min",
    "experience_max",
    "salary_raw_min",
    "salary_raw_max",
    "currency",
    "salary_unit",
    "salary_text",
    "skills",
    "work_type",
    "posted_date",
    "valid_through",
    "description",
    "job_location_type",
    "source",
    "url",
    "status",
    "first_seen",
    "last_seen",
    "expired_at",
    "collected_at",
]


def now_text():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def clean_url(url):
    if not url:
        return ""
    parsed = urlparse(str(url).strip())
    return parsed._replace(query="", fragment="").geturl()


def clean_description(description):
    if not description:
        return ""
    soup = BeautifulSoup(str(description), "html.parser")
    return soup.get_text(" ", strip=True)


def unique_keep_order(values):
    result = []
    seen = set()
    for value in values:
        if value is None:
            continue
        value = str(value).strip()
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def get_jobposting_from_html(file_path):
    html = file_path.read_text(encoding="utf-8", errors="ignore")
    soup = BeautifulSoup(html, "html.parser")

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


def extract_raw_locations(job_data):
    job_locations = job_data.get("jobLocation", [])
    if not isinstance(job_locations, list):
        job_locations = [job_locations]

    locations = []

    for loc in job_locations:
        if not isinstance(loc, dict):
            continue

        address = loc.get("address", {})
        if not isinstance(address, dict):
            continue

        country = address.get("addressCountry")
        if isinstance(country, dict):
            country = country.get("name") or country.get("@id")

        locations.append(
            {
                "street": address.get("streetAddress"),
                "city": address.get("addressLocality"),
                "region": address.get("addressRegion"),
                "country": country,
            }
        )

    return locations


def normalize_region_name(value):
    if not value:
        return None

    text = str(value).strip()
    low = text.lower()

    if any(k in low for k in ["hồ chí minh", "ho chi minh", "hcm"]):
        return "Hồ Chí Minh"
    if any(k in low for k in ["hà nội", "ha noi", "hanoi"]):
        return "Hà Nội"
    if any(k in low for k in ["đà nẵng", "da nang", "danang"]):
        return "Đà Nẵng"

    return text


def extract_location(job_data):
    locations = extract_raw_locations(job_data)
    regions = []

    for loc in locations:
        value = loc.get("region") or loc.get("city")
        value = normalize_region_name(value)
        if value:
            regions.append(value)

    regions = unique_keep_order(regions)

    if not regions:
        return None
    if len(regions) > 1:
        return "Nhiều địa điểm"
    return regions[0]


def extract_level(title):
    if not title:
        return "Unknown"

    t = str(title).lower()
    has_mid = bool(re.search(r"\b(mid|middle)\b", t))
    has_senior = bool(re.search(r"\b(senior|sr)\b", t))

    if has_mid and has_senior:
        return "Middle/Senior"
    if re.search(r"\bintern(ship)?\b", t):
        return "Intern"
    if "fresher" in t or "graduate" in t:
        return "Fresher"
    if re.search(r"\b(junior|jr)\b", t):
        return "Junior"
    if has_mid:
        return "Middle"
    if has_senior:
        return "Senior"
    if re.search(r"\bprincipal\b", t):
        return "Principal"
    if re.search(r"\blead\b", t) or "team leader" in t or "tech lead" in t:
        return "Lead"
    if "manager" in t or "head of" in t:
        return "Manager"

    return "Unknown"


def extract_experience_range(description):
    text = clean_description(description).lower()
    if not text:
        return None, None

    range_patterns = [
        r"(?:from\s+)?(\d+)\s*(?:-|–|—|to)\s*(\d+)\s*\+?\s*years?",
        r"(\d+)\s*(?:-|–|—)\s*(\d+)\s*yrs?",
        r"(\d+)\s*(?:-|–|—|đến|tới)\s*(\d+)\s*năm",
    ]

    for pattern in range_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            exp_min = int(match.group(1))
            exp_max = int(match.group(2))
            if 0 <= exp_min <= 30 and 0 <= exp_max <= 30 and exp_min <= exp_max:
                return exp_min, exp_max

    min_patterns = [
        r"at\s+least\s+(\d+)\+?\s*years?",
        r"minimum\s+(?:of\s+)?(\d+)\+?\s*years?",
        r"min\.?\s*(\d+)\+?\s*years?",
        r"more\s+than\s+(\d+)\s*years?",
        r"over\s+(\d+)\s*years?",
        r"(\d+)\+\s*years?",
        r"ít nhất\s+(\d+)\s*năm",
        r"tối thiểu\s+(\d+)\s*năm",
        r"trên\s+(\d+)\s*năm",
    ]

    for pattern in min_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            exp = int(match.group(1))
            if 0 <= exp <= 30:
                return exp, None

    single_patterns = [
        r"(\d+)\s+years?\s+(?:of\s+)?(?:working\s+)?experience",
        r"(\d+)\s*năm\s*kinh nghiệm",
    ]

    for pattern in single_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            exp = int(match.group(1))
            if 0 <= exp <= 30:
                return exp, None

    return None, None


def extract_raw_salary(job_data):
    salary = job_data.get("baseSalary")

    result = {
        "salary_raw_min": None,
        "salary_raw_max": None,
        "salary_currency": None,
        "salary_unit": None,
        "salary_text": None,
    }

    if not isinstance(salary, dict):
        return result

    result["salary_currency"] = salary.get("currency")
    value = salary.get("value", {})

    if not isinstance(value, dict):
        return result

    result["salary_raw_min"] = value.get("minValue")
    result["salary_raw_max"] = value.get("maxValue")
    result["salary_unit"] = value.get("unitText")
    result["salary_text"] = value.get("value")

    return result


def stringify_skills(skills):
    if skills is None:
        return None
    if isinstance(skills, str):
        return skills.strip() or None
    if isinstance(skills, (list, tuple, set)):
        values = [str(x).strip() for x in skills if str(x).strip()]
        return ", ".join(values) if values else None
    return str(skills)


def parse_raw_job(file_path, link_info):
    job_data = get_jobposting_from_html(file_path)
    if not job_data:
        return None

    title = job_data.get("title")
    description_html = job_data.get("description") or ""
    description_text = clean_description(description_html)

    organization = job_data.get("hiringOrganization", {})
    company = organization.get("name") if isinstance(organization, dict) else None

    raw_locations = extract_raw_locations(job_data)
    raw_salary = extract_raw_salary(job_data)
    exp_min, exp_max = extract_experience_range(description_html)

    return {
        "job_id": str(link_info.get("job_id") or file_path.stem),
        "title": title,
        "company": company,
        "location": extract_location(job_data),
        "locations_raw": json.dumps(raw_locations, ensure_ascii=False),
        "level": extract_level(title),
        "experience_min": exp_min,
        "experience_max": exp_max,
        "salary_raw_min": raw_salary["salary_raw_min"],
        "salary_raw_max": raw_salary["salary_raw_max"],
        "currency": raw_salary["salary_currency"],
        "salary_unit": raw_salary["salary_unit"],
        "salary_text": raw_salary["salary_text"],
        "skills": stringify_skills(job_data.get("skills")),
        "work_type": job_data.get("employmentType"),
        "posted_date": job_data.get("datePosted"),
        "valid_through": job_data.get("validThrough"),
        "description": description_text,
        "job_location_type": job_data.get("jobLocationType"),
        "source": "ITviec",
        "url": clean_url(link_info.get("url")),
        "status": link_info.get("status", "active"),
        "first_seen": link_info.get("first_seen"),
        "last_seen": link_info.get("last_seen"),
        "expired_at": link_info.get("expired_at"),
        "collected_at": now_text(),
    }


def read_old_raw():
    if not RAW_FILE.exists():
        return pd.DataFrame(columns=RAW_COLUMNS)

    try:
        df = pd.read_csv(RAW_FILE, dtype={"job_id": str})
    except pd.errors.EmptyDataError:
        return pd.DataFrame(columns=RAW_COLUMNS)

    return df


def main():
    if not LINKS_FILE.exists():
        raise FileNotFoundError(
            f"Không thấy {LINKS_FILE}. Hãy chạy 01_sync_links.py trước."
        )

    links_df = pd.read_csv(LINKS_FILE, dtype={"job_id": str})
    old_raw_df = read_old_raw()

    old_map = {}
    if not old_raw_df.empty:
        for row in old_raw_df.to_dict("records"):
            key = str(row.get("job_id") or "").strip()
            if key:
                old_map[key] = row

    rows = []
    total = len(links_df)
    parsed_count = 0
    reused_count = 0
    missing_count = 0

    for index, link_info in enumerate(links_df.to_dict("records"), start=1):
        job_id = str(link_info.get("job_id") or "").strip()
        status = str(link_info.get("status") or "active")
        file_path = DETAIL_FOLDER / f"{job_id}.html"

        print(f"[{index}/{total}] {job_id} | {status}")

        # Có snapshot HTML -> parse lại từ file offline.
        if file_path.exists():
            try:
                row = parse_raw_job(file_path, link_info)
                if row:
                    rows.append(row)
                    parsed_count += 1
                    print("   ✓ Parsed HTML")
                    continue
            except Exception as e:
                print("   ✗ Parse error:", e)

        # Không có HTML / parse lỗi nhưng raw cũ có -> giữ dữ liệu cũ,
        # chỉ đồng bộ lifecycle mới nhất.
        old = old_map.get(job_id)
        if old:
            old = dict(old)
            old["job_id"] = job_id
            old["url"] = clean_url(link_info.get("url") or old.get("url"))
            old["status"] = status
            old["first_seen"] = link_info.get("first_seen", old.get("first_seen"))
            old["last_seen"] = link_info.get("last_seen", old.get("last_seen"))
            old["expired_at"] = link_info.get("expired_at", old.get("expired_at", ""))
            rows.append(old)
            reused_count += 1
            print("   ✓ Giữ raw cũ + cập nhật lifecycle")
        else:
            missing_count += 1
            print("   ⚠ Chưa có HTML và cũng chưa có raw cũ")

    raw_df = pd.DataFrame(rows)

    for col in RAW_COLUMNS:
        if col not in raw_df.columns:
            raw_df[col] = None

    raw_df = raw_df[RAW_COLUMNS]

    if not raw_df.empty:
        raw_df = raw_df.drop_duplicates(subset=["source", "job_id"], keep="last")

    RAW_FILE.parent.mkdir(parents=True, exist_ok=True)
    raw_df.to_csv(RAW_FILE, index=False, encoding="utf-8-sig")

    print()
    print("=" * 80)
    print("KẾT QUẢ PARSE RAW ITVIEC")
    print("Tổng link lịch sử        :", total)
    print("Parse từ HTML            :", parsed_count)
    print("Giữ raw cũ               :", reused_count)
    print("Chưa có dữ liệu           :", missing_count)

    if not raw_df.empty:
        print("Raw active                :", int((raw_df["status"] == "active").sum()))
        print("Raw expired               :", int((raw_df["status"] == "expired").sum()))

    print("Output                    :", RAW_FILE)
    print("=" * 80)


if __name__ == "__main__":
    main()
