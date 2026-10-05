import os
import json
import re
import time
from urllib.parse import urljoin, urlparse, parse_qs, urlencode, urlunparse

import pandas as pd
import requests
from bs4 import BeautifulSoup


# =========================================================
# CONFIG
# =========================================================

BASE_URL = "https://www.vietnamworks.com/"
START_URL = "https://itviec.com/it-jobs"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/130.0.0.0 Safari/537.36"
    )
}

MAX_PAGES = 1000
REQUEST_DELAY = 1.5

# Tỷ giá dùng để tạo dữ liệu processed (triệu VND/tháng).
# Raw data vẫn giữ nguyên currency + salary gốc để có thể xử lý lại sau.
USD_TO_VND = 25000

LINKS_FILE = "itviec_job_links.csv"
RAW_FILE = "itviec_raw.csv"
PROCESSED_ALL_FILE = "itviec_processed_all.csv"
SALARY_DATASET_FILE = "itviec_salary_dataset.csv"

# Nếu đã có itviec_job_links.csv thì dùng lại, không crawl danh sách từ đầu.
USE_EXISTING_LINKS_IF_AVAILABLE = True


# =========================================================
# SESSION
# =========================================================

session = requests.Session()
session.headers.update(HEADERS)


# =========================================================
# HELPER
# =========================================================

def set_page(url, page):
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    query["page"] = [str(page)]
    new_query = urlencode(query, doseq=True)
    return urlunparse(parsed._replace(query=new_query))


def clean_url(url):
    parsed = urlparse(url)
    return urlunparse(parsed._replace(query="", fragment=""))


def is_job_detail_url(url):
    path = urlparse(url).path.rstrip("/")
    return bool(re.search(r"/it-jobs/.+-\d+$", path))


def extract_job_id(url):
    path = urlparse(url).path.rstrip("/")
    match = re.search(r"-(\d+)$", path)
    return match.group(1) if match else None


def clean_description(description):
    if not description:
        return ""
    soup = BeautifulSoup(description, "html.parser")
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


# =========================================================
# 1. CRAWL JOB LINKS
# =========================================================

def get_job_links_from_page(url):
    try:
        response = session.get(url, timeout=20)

        if response.status_code != 200:
            print(f"Lỗi page: {response.status_code} - {url}")
            return []

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
        return []


def collect_all_job_links():
    all_links = set()

    for page in range(1, MAX_PAGES + 1):
        page_url = set_page(START_URL, page)

        print(f"\nĐang đọc page {page}")
        print(page_url)

        links = get_job_links_from_page(page_url)
        new_links = [link for link in links if link not in all_links]

        print(f"Tìm thấy: {len(links)} job")
        print(f"Job mới: {len(new_links)}")

        if not new_links:
            print("Không còn job mới. Dừng crawl page.")
            break

        all_links.update(new_links)
        time.sleep(REQUEST_DELAY)

    return sorted(all_links)


def load_or_collect_links():
    if USE_EXISTING_LINKS_IF_AVAILABLE and os.path.exists(LINKS_FILE):
        df_links = pd.read_csv(LINKS_FILE)

        if "url" not in df_links.columns:
            raise ValueError(f"{LINKS_FILE} phải có cột 'url'.")

        links = (
            df_links["url"]
            .dropna()
            .astype(str)
            .map(clean_url)
            .drop_duplicates()
            .tolist()
        )

        print(f"Dùng lại {len(links)} URL từ {LINKS_FILE}")
        return links

    links = collect_all_job_links()

    pd.DataFrame({"url": links}).to_csv(
        LINKS_FILE,
        index=False,
        encoding="utf-8-sig"
    )

    return links


# =========================================================
# 2. JOBPOSTING JSON-LD
# =========================================================

def get_jobposting(url):
    try:
        response = session.get(url, timeout=20)

        if response.status_code != 200:
            print(f"Lỗi job {response.status_code}: {url}")
            return None

        soup = BeautifulSoup(response.text, "html.parser")

        for script in soup.find_all("script", type="application/ld+json"):
            try:
                if not script.string:
                    continue

                data = json.loads(script.string)

                # Trường hợp JSON-LD là dict trực tiếp
                if isinstance(data, dict) and data.get("@type") == "JobPosting":
                    return data

                # Phòng trường hợp website chuyển sang @graph
                if isinstance(data, dict) and isinstance(data.get("@graph"), list):
                    for item in data["@graph"]:
                        if isinstance(item, dict) and item.get("@type") == "JobPosting":
                            return item

                # Phòng trường hợp JSON-LD là list
                if isinstance(data, list):
                    for item in data:
                        if isinstance(item, dict) and item.get("@type") == "JobPosting":
                            return item

            except (json.JSONDecodeError, TypeError):
                continue

        return None

    except Exception as e:
        print("Lỗi get_jobposting:", e, url)
        return None


# =========================================================
# 3. LOCATION
# =========================================================

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

        locations.append({
            "street": address.get("streetAddress"),
            "city": address.get("addressLocality"),
            "region": address.get("addressRegion"),
            "country": address.get("addressCountry")
        })

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

    # Với ML, tránh tạo quá nhiều category kiểu "HCM, Đà Nẵng".
    if len(regions) > 1:
        return "Nhiều địa điểm"

    return regions[0]


# =========================================================
# 4. LEVEL
# =========================================================

def extract_level(title):
    if not title:
        return "Unknown"

    t = title.lower()

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


# =========================================================
# 5. EXPERIENCE
# =========================================================

def extract_experience_range(description):
    """
    Trả về (experience_min, experience_max).
    Không lấy midpoint nữa.

    Ví dụ:
      3-5 years -> (3, 5)
      at least 5 years -> (5, None)
      5+ years -> (5, None)
    """
    text = clean_description(description).lower()

    if not text:
        return None, None

    # 3 - 5 years / 3 to 5 years / from 3 to 5 years
    range_patterns = [
        r"(?:from\s+)?(\d+)\s*(?:-|–|—|to)\s*(\d+)\s*\+?\s*years?",
        r"(\d+)\s*(?:-|–|—)\s*(\d+)\s*yrs?"
    ]

    for pattern in range_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            exp_min = int(match.group(1))
            exp_max = int(match.group(2))

            # bỏ match vô lý kiểu 2024-2026 years nếu có
            if 0 <= exp_min <= 30 and 0 <= exp_max <= 30 and exp_min <= exp_max:
                return exp_min, exp_max

    # at least / minimum / min 5 years
    min_patterns = [
        r"at\s+least\s+(\d+)\+?\s*years?",
        r"minimum\s+(?:of\s+)?(\d+)\+?\s*years?",
        r"min\.?\s*(\d+)\+?\s*years?",
        r"more\s+than\s+(\d+)\s*years?",
        r"over\s+(\d+)\s*years?",
        r"(\d+)\+\s*years?"
    ]

    for pattern in min_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            exp = int(match.group(1))
            if 0 <= exp <= 30:
                return exp, None

    # 3 years of experience / 3 years experience
    match = re.search(
        r"(\d+)\s+years?\s+(?:of\s+)?(?:working\s+)?experience",
        text,
        re.IGNORECASE
    )

    if match:
        exp = int(match.group(1))
        if 0 <= exp <= 30:
            return exp, None

    return None, None


# =========================================================
# 6. JOB ROLE
# =========================================================

def extract_job_role(title):
    """
    Ưu tiên title. Không dùng skill để ép role vì dễ gán sai.
    """
    if not title:
        return "Other"

    t = title.lower().strip()

    # Management / product / analyst trước để tránh bị bắt bởi từ "software"
    role_rules = [
        ("Project Manager", [r"\bproject manager\b", r"\bproject management\b", r"\bdelivery manager\b"]),
        ("Product Manager", [r"\bproduct manager\b", r"\bproduct owner\b"]),
        ("Business Analyst", [r"\bbusiness analyst\b", r"\bsystem analyst\b", r"\bsystems analyst\b"]),
        ("IT Consultant", [r"\bapplication consultant\b", r"\bit consultant\b", r"\btechnical consultant\b", r"\bsolution consultant\b"]),
        ("Software Architect", [r"\bsolution architect\b", r"\bsoftware architect\b", r"\btechnical architect\b", r"\bcloud architect\b"]),

        # Security / infra
        ("Cybersecurity", [r"\bcyber\s*security\b", r"\bcybersecurity\b", r"\bsecurity engineer\b", r"\bsecurity analyst\b", r"\bsoc analyst\b", r"\bpenetration tester\b", r"\bpentest\b"]),
        ("Cloud Engineer", [r"\bcloud engineer\b", r"\bcloud infrastructure\b", r"\bcloud platform\b"]),
        ("DevOps Engineer", [r"\bdevops\b", r"\bsite reliability\b", r"\bsre\b", r"\bplatform engineer\b"]),
        ("Network/System Engineer", [r"\bnetwork engineer\b", r"\bsystem engineer\b", r"\bsystems engineer\b", r"\bsystem administrator\b", r"\bsysadmin\b"]),
        ("Database Administrator", [r"\bdatabase administrator\b", r"\bdba\b"]),
        ("IT Support", [r"\bit support\b", r"\btechnical support\b", r"\bhelp\s*desk\b", r"\bservice desk\b"]),

        # Data / AI
        ("Data Scientist", [r"\bdata scientist\b"]),
        ("Data Engineer", [r"\bdata engineer\b", r"\bbig data engineer\b"]),
        ("Data Analyst", [r"\bdata analyst\b", r"\bbi analyst\b", r"\bbusiness intelligence analyst\b"]),
        ("Machine Learning Engineer", [r"\bmachine learning\b", r"\bml engineer\b", r"\bai engineer\b", r"\bartificial intelligence\b", r"\bgenai\b"]),

        # Testing
        ("QA/Tester", [r"\bqa\b", r"\bqaqc\b", r"\btester\b", r"\btest engineer\b", r"\btesting engineer\b", r"\bautomation test\b", r"\bmanual test\b", r"\bquality assurance\b", r"\bquality control\b"]),

        # Mobile
        ("Mobile Developer", [r"\bandroid\b", r"\bios developer\b", r"\bmobile developer\b", r"\bflutter\b", r"\breact native\b"]),

        # Web / app role
        ("Fullstack Developer", [r"\bfull\s*-?\s*stack\b", r"\bfullstack\b"]),
        ("Backend Developer", [r"\bback\s*-?\s*end\b", r"\bbackend\b"]),
        ("Frontend Developer", [r"\bfront\s*-?\s*end\b", r"\bfrontend\b"]),

        # Embedded / game / ERP
        ("Embedded Engineer", [r"\bembedded\b", r"\bfirmware\b"]),
        ("Game Developer", [r"\bgame developer\b", r"\bunity developer\b", r"\bunreal developer\b"]),
        ("ERP/SAP", [r"\bsap\b", r"\berp\b", r"\bodoo\b"]),

        # Language-specific developer
        (".NET Developer", [r"\.net\b", r"\bc#\b", r"\bdotnet\b"]),
        ("PHP Developer", [r"\bphp\b", r"\blaravel\b"]),
        ("Python Developer", [r"\bpython developer\b", r"\bpython engineer\b"]),
        ("Java Developer", [r"\bjava developer\b", r"\bjava engineer\b", r"\bjava lead\b"]),
        ("JavaScript Developer", [r"\bjavascript developer\b", r"\bnode\.?js developer\b", r"\bnodejs developer\b"]),

        # Generic application/software
        ("Application Developer", [r"\bapplication developer\b", r"\bapplication engineer\b"]),
        ("Software Engineer", [r"\bsoftware engineer\b", r"\bsoftware developer\b", r"\bdeveloper\b"]),
    ]

    for role, patterns in role_rules:
        for pattern in patterns:
            if re.search(pattern, t, re.IGNORECASE):
                return role

    return "Other"


# =========================================================
# 7. SKILLS
# =========================================================

def contains_skill(text, patterns):
    text = (text or "").lower()

    for pattern in patterns:
        if re.search(pattern, text, re.IGNORECASE):
            return 1

    return 0


def build_skill_text(title, skills, description):
    """
    Ưu tiên field skills của JSON-LD.
    Chỉ fallback title + description khi skills trống để giảm nhiễu.
    """
    if skills and str(skills).strip():
        return str(skills)

    return f"{title or ''} {clean_description(description)}"


# =========================================================
# 8. REMOTE
# =========================================================

def extract_remote(job_data, title, description):
    location_type = job_data.get("jobLocationType")

    if location_type and "telecommute" in str(location_type).lower():
        return 1

    text = f"{title or ''} {clean_description(description)}".lower()

    remote_patterns = [
        r"\bremote\b",
        r"work\s+from\s+home",
        r"\bwfh\b",
        r"fully\s+remote",
        r"hybrid\s+working",
        r"hybrid\s+work"
    ]

    return int(any(re.search(p, text, re.IGNORECASE) for p in remote_patterns))


# =========================================================
# 9. SALARY
# =========================================================

def extract_raw_salary(job_data):
    salary = job_data.get("baseSalary")

    result = {
        "salary_raw_min": None,
        "salary_raw_max": None,
        "salary_currency": None,
        "salary_unit": None,
        "salary_text": None
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


def normalize_salary_from_raw(raw_min, raw_max, currency, unit):
    """
    Trả về salary_min, salary_max, salary_mid theo TRIỆU VND / THÁNG.
    Nếu không đủ dữ liệu để tính midpoint thì trả None tương ứng.
    """
    if unit and str(unit).upper() != "MONTH":
        return None, None, None

    try:
        salary_min = float(raw_min) if raw_min is not None else None
    except (TypeError, ValueError):
        salary_min = None

    try:
        salary_max = float(raw_max) if raw_max is not None else None
    except (TypeError, ValueError):
        salary_max = None

    if salary_min is None and salary_max is None:
        return None, None, None

    currency = (currency or "").upper().strip()

    def convert(value):
        if value is None:
            return None

        if currency == "USD":
            return value * USD_TO_VND / 1_000_000

        if currency == "VND":
            # ITviec có thể trả 30 hoặc 30,000,000.
            return value / 1_000_000 if value > 100000 else value

        return None

    salary_min = convert(salary_min)
    salary_max = convert(salary_max)

    if salary_min is None and salary_max is None:
        return None, None, None

    salary_mid = None
    if salary_min is not None and salary_max is not None:
        salary_mid = (salary_min + salary_max) / 2

    return (
        round(salary_min, 2) if salary_min is not None else None,
        round(salary_max, 2) if salary_max is not None else None,
        round(salary_mid, 2) if salary_mid is not None else None,
    )


# =========================================================
# 10. PARSE RAW JOB
# =========================================================

def parse_raw_job(url):
    job_data = get_jobposting(url)

    if not job_data:
        return None

    title = job_data.get("title")
    description_html = job_data.get("description") or ""
    description_text = clean_description(description_html)
    skills = job_data.get("skills")

    organization = job_data.get("hiringOrganization", {})
    company = organization.get("name") if isinstance(organization, dict) else None

    raw_locations = extract_raw_locations(job_data)
    raw_salary = extract_raw_salary(job_data)

    exp_min, exp_max = extract_experience_range(description_html)

    return {
        "job_id": extract_job_id(url),
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
        "skills": skills,
        "work_type": job_data.get("employmentType"),
        "posted_date": job_data.get("datePosted"),
        "valid_through": job_data.get("validThrough"),
        "description": description_text,
        "job_location_type": job_data.get("jobLocationType"),
        "source": "ITviec",
        "url": clean_url(url),
    }


# =========================================================
# 11. RAW -> PROCESSED
# =========================================================

def process_raw_row(row):
    title = row.get("title")
    skills = row.get("skills")
    description = row.get("description")

    skill_text = build_skill_text(title, skills, description)

    salary_min, salary_max, salary_mid = normalize_salary_from_raw(
        row.get("salary_raw_min"),
        row.get("salary_raw_max"),
        row.get("currency"),
        row.get("salary_unit")
    )

    # experience dùng MINIMUM requirement, không dùng midpoint.
    experience = row.get("experience_min")

    # job_data giả lập tối thiểu để extract_remote đọc được jobLocationType
    job_data_for_remote = {
        "jobLocationType": row.get("job_location_type")
    }

    return {
        "job_id": row.get("job_id"),
        "job_role": extract_job_role(title),
        "location": row.get("location") or "Unknown",
        "level": row.get("level") or "Unknown",
        "experience": experience,
        "salary_min": salary_min,
        "salary_max": salary_max,
        "salary_mid": salary_mid,
        "python": contains_skill(skill_text, [r"\bpython\b"]),
        "java": contains_skill(skill_text, [r"\bjava\b"]),
        "javascript": contains_skill(
            skill_text,
            [r"\bjavascript\b", r"\bjava\s*script\b", r"\bjs\b", r"\bnode\.?js\b", r"\bnodejs\b"]
        ),
        "sql": contains_skill(
            skill_text,
            [r"\bsql\b", r"\bmysql\b", r"\bpostgresql\b", r"\bpostgres\b", r"\boracle\b", r"\bmssql\b"]
        ),
        "aws": contains_skill(skill_text, [r"\baws\b", r"amazon web services"]),
        "remote": extract_remote(job_data_for_remote, title, description),
        "posted_date": row.get("posted_date"),
        "source": row.get("source"),
        "url": row.get("url"),
        "raw_title": title,
        "raw_skills": skills,
    }


# =========================================================
# 12. REPORT
# =========================================================

def print_quality_report(raw_df, processed_df, salary_df):
    print("\n" + "=" * 70)
    print("BÁO CÁO CHẤT LƯỢNG DỮ LIỆU")
    print("=" * 70)

    print(f"Tổng job raw                  : {len(raw_df)}")
    print(f"Tổng job processed            : {len(processed_df)}")
    print(f"Job có salary_mid để train    : {len(salary_df)}")

    if len(processed_df) > 0:
        print(f"Thiếu experience              : {processed_df['experience'].isna().sum()}")
        print(f"Level Unknown                 : {(processed_df['level'] == 'Unknown').sum()}")
        print(f"Job role Other                : {(processed_df['job_role'] == 'Other').sum()}")
        print(f"Location Unknown              : {(processed_df['location'] == 'Unknown').sum()}")
        print(f"Nhiều địa điểm                : {(processed_df['location'] == 'Nhiều địa điểm').sum()}")

    print("=" * 70)


# =========================================================
# 13. MAIN
# =========================================================

def main():
    print("=" * 70)
    print("ITVIEC SALARY DATA PIPELINE - FIXED")
    print("=" * 70)

    # 1) Lấy hoặc dùng lại URL đã có
    job_links = load_or_collect_links()
    print(f"\nTổng URL job: {len(job_links)}")

    # 2) Crawl RAW: giữ cả job thiếu salary
    raw_rows = []
    total = len(job_links)

    for index, url in enumerate(job_links, start=1):
        print(f"[{index}/{total}] {url}")

        try:
            row = parse_raw_job(url)

            if row:
                raw_rows.append(row)
                has_salary = row.get("salary_raw_min") is not None or row.get("salary_raw_max") is not None
                print("   ✓ Đã lưu raw" + (" + salary" if has_salary else " (không có salary)"))
            else:
                print("   ✗ Không đọc được JobPosting")

        except Exception as e:
            print("   ✗ Lỗi:", e)

        # autosave raw mỗi 20 URL
        if raw_rows and index % 20 == 0:
            pd.DataFrame(raw_rows).to_csv(
                RAW_FILE,
                index=False,
                encoding="utf-8-sig"
            )
            print(f"   💾 Autosave {RAW_FILE}")

        time.sleep(REQUEST_DELAY)

    # 3) RAW dataframe + dedup theo source/job_id/url
    raw_df = pd.DataFrame(raw_rows)

    if raw_df.empty:
        print("Không thu được dữ liệu. Dừng.")
        return

    # job_id có thể null ở case lạ, nên fallback url vẫn được giữ
    raw_df = raw_df.drop_duplicates(subset=["source", "job_id", "url"], keep="first")

    raw_df.to_csv(
        RAW_FILE,
        index=False,
        encoding="utf-8-sig"
    )

    # 4) Process toàn bộ raw
    processed_rows = [process_raw_row(row) for row in raw_df.to_dict("records")]
    processed_df = pd.DataFrame(processed_rows)

    processed_columns = [
        "job_id",
        "job_role",
        "location",
        "level",
        "experience",
        "salary_min",
        "salary_max",
        "salary_mid",
        "python",
        "java",
        "javascript",
        "sql",
        "aws",
        "remote",
        "posted_date",
        "source",
        "url",
        "raw_title",
        "raw_skills",
    ]

    processed_df = processed_df[processed_columns]

    processed_df.to_csv(
        PROCESSED_ALL_FILE,
        index=False,
        encoding="utf-8-sig"
    )

    # 5) Dataset ML: chỉ giữ job có salary_mid hợp lệ
    salary_df = processed_df[
        processed_df["salary_mid"].notna()
    ].copy()

    # Dataset này vẫn giữ salary_min/max để kiểm tra dữ liệu.
    # KHI TRAIN MODEL: KHÔNG đưa salary_min và salary_max vào X.
    salary_df.to_csv(
        SALARY_DATASET_FILE,
        index=False,
        encoding="utf-8-sig"
    )

    # 6) Report
    print_quality_report(raw_df, processed_df, salary_df)

    print("\nĐã tạo:")
    print(f"1. {RAW_FILE}               -> dữ liệu raw, giữ cả job thiếu salary")
    print(f"2. {PROCESSED_ALL_FILE}     -> toàn bộ job sau feature engineering")
    print(f"3. {SALARY_DATASET_FILE}    -> chỉ job có salary_mid để dùng ML")

    print("\nLƯU Ý KHI TRAIN MODEL:")
    print("X = job_role, location, level, experience, python, java, javascript, sql, aws, remote")
    print("y = salary_mid")
    print("KHÔNG đưa salary_min hoặc salary_max vào X vì sẽ bị data leakage.")


if __name__ == "__main__":
    main()

