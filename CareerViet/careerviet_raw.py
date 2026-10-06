import os
import json
import re
import time
from urllib.parse import urljoin, urlparse, parse_qs, urlencode, urlunparse

import pandas as pd
import requests
from bs4 import BeautifulSoup

try:
    from selenium import webdriver
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
except ImportError:
    webdriver = None
    By = None
    WebDriverWait = None


# =========================================================
# CONFIG
# =========================================================

BASE_URL = "https://careerviet.vn/"
START_URL = "https://careerviet.vn/viec-lam/cntt-phan-mem-cntt-phan-cung-mang-c1,63-vi.html"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/130.0.0.0 Safari/537.36"
    )
}

MAX_PAGES = 1000
REQUEST_DELAY = 1.5
USD_TO_VND = 25000

LINKS_FILE = "careerviet_job_links.csv"
RAW_FILE = "careerviet_raw.csv"
PROCESSED_ALL_FILE = "careerviet_processed_all.csv"
SALARY_DATASET_FILE = "careerviet_salary_dataset.csv"

session = requests.Session()
session.headers.update(HEADERS)


# =========================================================
# COMMON URL HELPERS
# =========================================================

def set_page(url, page):
    if page <= 1:
        return url
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    query["page"] = [str(page)]
    return urlunparse(parsed._replace(query=urlencode(query, doseq=True)))


def clean_url(url):
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")
    return urlunparse(parsed._replace(path=path, query="", fragment=""))


def is_job_detail_url(url):
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    path = parsed.path
    return (
        ("careerviet.vn" in host)
        and bool(re.search(r"/(?:vi/tim-viec-lam|en/search-job)/[^?#]+\.35[A-Z0-9]+\.html$", path, re.I))
    )


def extract_job_id(url):
    path = urlparse(url).path
    m = re.search(r"\.([0-9A-Z]{8})\.html$", path, re.I)
    return m.group(1).upper() if m else None


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
# CAREERVIET SEARCH - SELENIUM CLICK PAGINATION
# =========================================================

def _build_search_driver():
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options

    options = Options()
    options.binary_location = r"C:\\Program Files\\CocCoc\\Browser\\Application\\browser.exe"
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1920,1080")

    print("Đang khởi động Cốc Cốc...")
    driver = webdriver.Chrome(options=options)
    driver.set_page_load_timeout(25)
    print("Khởi động Cốc Cốc thành công.")
    return driver


def _extract_search_result_links(driver, log_rejected=False):
    """Chỉ lấy URL từ job cards thuộc Search Results chính của CareerViet.

    Không lọc title theo một nhánh CNTT cụ thể. START_URL/search của website
    quyết định tập job cần crawl. Nhờ vậy có thể dùng IT, Java, Python,
    Testing, Data, DevOps... mà không phải sửa bộ lọc hard-code.
    """
    links = []
    seen = set()

    for a in driver.find_elements(By.CSS_SELECTOR, "#jobs-side-list-content a.job_link[href]"):
        try:
            href = a.get_attribute("href")
        except Exception:
            continue
        if not href:
            continue

        full = clean_url(urljoin(BASE_URL, href))
        if is_job_detail_url(full) and full not in seen:
            seen.add(full)
            links.append(full)

    return links

def _active_page_number(driver):
    try:
        text = driver.find_element(By.CSS_SELECTOR, ".pagination li.active a").text.strip()
        return int(text)
    except Exception:
        return None


def _click_page(driver, target_page):
    # CareerViet pagination là React button, không có href page=...
    # Tìm nút số target_page đang hiển thị; nếu chưa có thì bấm Next để tiến tới.
    for _ in range(10):
        anchors = driver.find_elements(By.CSS_SELECTOR, ".pagination li:not(.active) > a[role='button']")
        for a in anchors:
            try:
                if a.text.strip() == str(target_page):
                    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", a)
                    old_first = None
                    old_links = _extract_search_result_links(driver)
                    if old_links:
                        old_first = old_links[0]
                    driver.execute_script("arguments[0].click();", a)
                    WebDriverWait(driver, 15).until(
                        lambda d: _active_page_number(d) == target_page
                        and (not old_first or (_extract_search_result_links(d) and _extract_search_result_links(d)[0] != old_first))
                    )
                    return True
            except Exception:
                continue

        # Nút số chưa hiện: bấm next-page để pagination dịch sang nhóm kế tiếp.
        try:
            nxt = driver.find_element(By.CSS_SELECTOR, ".pagination li.next-page > a[role='button']")
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", nxt)
            before = _active_page_number(driver)
            driver.execute_script("arguments[0].click();", nxt)
            WebDriverWait(driver, 15).until(lambda d: _active_page_number(d) != before)
            if _active_page_number(driver) == target_page:
                return True
        except Exception:
            return False
    return False


def collect_all_job_links():
    all_links = []
    seen = set()
    driver = None

    try:
        driver = _build_search_driver()
        print(f"Mở search: {START_URL}")
        driver.get(START_URL)
        WebDriverWait(driver, 20).until(
            lambda d: len(d.find_elements(By.CSS_SELECTOR, "#jobs-side-list-content a.job_link[href]")) > 0
        )

        page = _active_page_number(driver) or 1
        while page <= MAX_PAGES:
            links = _extract_search_result_links(driver, log_rejected=True)
            new_links = [x for x in links if x not in seen]

            print(f"\nĐang đọc UI page {page}")
            print(f"Tìm thấy: {len(links)} job")
            print(f"Job mới: {len(new_links)}")

            for url in new_links:
                seen.add(url)
                all_links.append(url)

            if not links:
                print("Không còn job trong danh sách kết quả. Dừng.")
                break

            target = page + 1
            if target > MAX_PAGES:
                break

            if not _click_page(driver, target):
                print("Không tìm thấy/bấm được trang kế tiếp. Đã tới cuối hoặc UI thay đổi.")
                break

            page = _active_page_number(driver) or target
            time.sleep(REQUEST_DELAY)

    finally:
        if driver is not None:
            driver.quit()

    return sorted(all_links)


def load_or_collect_links():
    existing_links = []

    if os.path.exists(LINKS_FILE):
        try:
            df = pd.read_csv(LINKS_FILE)
            if "url" in df.columns:
                existing_links = unique_keep_order(
                    clean_url(x)
                    for x in df["url"].dropna().astype(str)
                    if is_job_detail_url(clean_url(x))
                )
                print(f"Đã có {len(existing_links)} URL lịch sử trong {LINKS_FILE}")
        except Exception as e:
            print(f"Không đọc được {LINKS_FILE}: {e}")

    print(f"Crawl START_URL hiện tại: {START_URL}")
    crawled = collect_all_job_links()

    if not crawled:
        if existing_links:
            print(
                f"Không lấy được link mới từ START_URL. "
                f"GIỮ NGUYÊN {len(existing_links)} link cũ."
            )
            return []  # không crawl cache lịch sử nếu search hiện tại lỗi
        return []

    old_set = set(existing_links)
    new_only = [x for x in crawled if x not in old_set]
    all_links = unique_keep_order(existing_links + crawled)

    pd.DataFrame({"url": all_links}).to_csv(
        LINKS_FILE, index=False, encoding="utf-8-sig"
    )

    print(f"Search hiện tại tìm thấy : {len(crawled)} URL")
    print(f"URL mới                 : {len(new_only)}")
    print(f"Tổng URL lịch sử        : {len(all_links)}")

    return crawled  # chỉ crawl kết quả START_URL hiện tại


# =========================================================
# CAREERVIET JOBPOSTING JSON-LD
# =========================================================

def get_jobposting(url):
    try:
        response = session.get(url, timeout=25)
        if response.status_code != 200:
            print(f"Lỗi job {response.status_code}: {url}")
            return None

        soup = BeautifulSoup(response.text, "html.parser")

        candidates = []
        for script in soup.find_all("script", type="application/ld+json"):
            raw = script.string or script.get_text()
            if not raw or not raw.strip():
                continue
            try:
                data = json.loads(raw)
            except Exception:
                continue

            if isinstance(data, dict):
                candidates.append(data)
            elif isinstance(data, list):
                candidates.extend(x for x in data if isinstance(x, dict))

        for data in candidates:
            if data.get("@type") == "JobPosting":
                return data

            graph = data.get("@graph")
            if isinstance(graph, list):
                for item in graph:
                    if isinstance(item, dict) and item.get("@type") == "JobPosting":
                        return item

        print(f"Không tìm thấy JobPosting JSON-LD: {url}")
        return None

    except Exception as e:
        print("Lỗi get_jobposting:", e, url)
        return None


# =========================================================
# CAREERVIET FIELD MAPPING
# =========================================================

def extract_raw_locations(job_data):
    raw = job_data.get("jobLocation") or []
    if not isinstance(raw, list):
        raw = [raw]

    result = []
    for loc in raw:
        if not isinstance(loc, dict):
            continue
        address = loc.get("address") or {}
        if not isinstance(address, dict):
            address = {}

        result.append({
            "street_address": address.get("streetAddress"),
            "region": address.get("addressRegion"),
            "locality": address.get("addressLocality"),
            "country": address.get("addressCountry"),
        })
    return result


def extract_location(job_data):
    regions = []

    for loc in extract_raw_locations(job_data):
        value = loc.get("locality") or loc.get("region") or loc.get("street_address")
        value = normalize_region_name(value)
        if value:
            regions.append(value)

    regions = unique_keep_order(regions)
    if not regions:
        return "Unknown"
    return "Nhiều địa điểm" if len(regions) > 1 else regions[0]


def _extract_text_field(description, label):
    text = clean_description(description)
    m = re.search(
        rf"{re.escape(label)}\s*:\s*(.+?)(?=\s+(?:Ngành nghề|Kinh nghiệm|Cấp bậc|Hình thức|Địa điểm)\s*:|$)",
        text,
        re.I,
    )
    return m.group(1).strip() if m else None


def _extract_level_careerviet(job_data, title):
    title_level = extract_level(title)
    if title_level != "Unknown":
        return title_level

    raw_level = _extract_text_field(job_data.get("description") or "", "Cấp bậc")
    low = (raw_level or "").lower()

    if "thực tập" in low or "intern" in low:
        return "Intern"
    if "mới tốt nghiệp" in low or "fresher" in low:
        return "Fresher"
    if "quản lý" in low or "manager" in low or "trưởng" in low:
        return "Manager"
    if "nhân viên" in low or "chuyên viên" in low:
        return "Experienced"

    return "Unknown"


def _extract_skills(job_data):
    raw = job_data.get("skills")
    if isinstance(raw, list):
        return ", ".join(unique_keep_order(str(x) for x in raw))
    return str(raw).strip() if raw else ""


def _experience_from_careerviet(job_data):
    exp = job_data.get("experienceRequirements")

    if isinstance(exp, dict):
        months = exp.get("monthsOfExperience")
        try:
            months = float(months)
            if 0 <= months <= 360:
                years = months / 12
                years = int(years) if years.is_integer() else round(years, 1)
                return years, None
        except (TypeError, ValueError):
            pass

        desc = exp.get("description")
        if desc:
            a, b = extract_experience_range(desc)
            if a is not None:
                return a, b

    description = job_data.get("description") or ""
    return extract_experience_range(description)


def _number(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).replace(",", "").strip()
    m = re.search(r"-?\d+(?:\.\d+)?", text)
    return float(m.group()) if m else None


def extract_raw_salary(job_data):
    result = {
        "salary_raw_min": None,
        "salary_raw_max": None,
        "salary_currency": None,
        "salary_unit": None,
        "salary_text": None,
    }

    base = job_data.get("baseSalary")
    if not isinstance(base, dict):
        return result

    currency = base.get("currency")
    value = base.get("value")

    result["salary_currency"] = currency

    if isinstance(value, dict):
        result["salary_unit"] = value.get("unitText")
        result["salary_text"] = value.get("value")

        min_v = _number(value.get("minValue"))
        max_v = _number(value.get("maxValue"))

        # Một số JobPosting có value số duy nhất.
        if min_v is None and max_v is None:
            scalar = value.get("value")
            if isinstance(scalar, (int, float)):
                min_v = max_v = float(scalar)

        result["salary_raw_min"] = min_v
        result["salary_raw_max"] = max_v

    elif isinstance(value, (int, float)):
        result["salary_raw_min"] = float(value)
        result["salary_raw_max"] = float(value)

    # CareerViet dùng "Cạnh tranh" khi không công khai lương.
    salary_text = str(result["salary_text"] or "").lower()
    if any(x in salary_text for x in ["cạnh tranh", "thỏa thuận", "thoả thuận", "negotiable"]):
        result["salary_raw_min"] = None
        result["salary_raw_max"] = None

    return result


def parse_raw_job(url):
    job_data = get_jobposting(url)
    if not job_data:
        return None

    title = job_data.get("title")
    org = job_data.get("hiringOrganization") or {}
    company = org.get("name") if isinstance(org, dict) else None

    description_html = job_data.get("description") or ""
    description = clean_description(description_html)

    exp_min, exp_max = _experience_from_careerviet(job_data)
    raw_salary = extract_raw_salary(job_data)
    raw_locations = extract_raw_locations(job_data)

    identifier = job_data.get("identifier") or {}
    job_id = identifier.get("value") if isinstance(identifier, dict) else None
    job_id = str(job_id or extract_job_id(url) or "")

    employment = job_data.get("employmentType")
    if isinstance(employment, list):
        employment = ", ".join(str(x).strip('"') for x in employment)

    return {
        "job_id": job_id,
        "title": title,
        "company": company,
        "location": extract_location(job_data),
        "locations_raw": json.dumps(raw_locations, ensure_ascii=False),
        "level": _extract_level_careerviet(job_data, title),
        "experience_min": exp_min,
        "experience_max": exp_max,
        "salary_raw_min": raw_salary["salary_raw_min"],
        "salary_raw_max": raw_salary["salary_raw_max"],
        "currency": raw_salary["salary_currency"],
        "salary_unit": raw_salary["salary_unit"],
        "salary_text": raw_salary["salary_text"],
        "skills": _extract_skills(job_data),
        "work_type": employment,
        "posted_date": job_data.get("datePosted"),
        "valid_through": job_data.get("validThrough"),
        "description": description,
        "job_location_type": job_data.get("jobLocationType"),
        "industry": job_data.get("industry"),
        "education": (
            (job_data.get("educationRequirements") or {}).get("alternateName")
            if isinstance(job_data.get("educationRequirements"), dict)
            else None
        ),
        "job_benefits": job_data.get("jobBenefits"),
        "source": "CareerViet",
        "url": clean_url(job_data.get("url") or url),
    }



# =========================================================
# MAIN - APPEND SAFE
# =========================================================

def main():
    print("=" * 70)
    print("CAREERVIET SALARY DATA PIPELINE - APPEND SAFE")
    print("=" * 70)

    # Search result collector đã được scope vào:
    # #jobs-side-list-content a.job_link[href]
    job_links = load_or_collect_links()
    print(f"\nTổng URL job trong lịch sử: {len(job_links)}")

    if os.path.exists(RAW_FILE):
        try:
            old_raw_df = pd.read_csv(RAW_FILE)
        except Exception as e:
            print(f"Không đọc được {RAW_FILE}: {e}")
            old_raw_df = pd.DataFrame()
    else:
        old_raw_df = pd.DataFrame()

    existing_job_ids = set()
    existing_urls = set()
    if not old_raw_df.empty:
        if "job_id" in old_raw_df.columns:
            existing_job_ids = {
                str(v).strip().upper() for v in old_raw_df["job_id"].dropna() if str(v).strip()
            }
        if "url" in old_raw_df.columns:
            existing_urls = {
                clean_url(str(v)) for v in old_raw_df["url"].dropna() if str(v).strip()
            }

    links_to_crawl = []
    for url in job_links:
        jid = extract_job_id(url)
        if clean_url(url) in existing_urls:
            continue
        if jid and jid.upper() in existing_job_ids:
            continue
        links_to_crawl.append(url)

    print(f"Job đã có trong RAW       : {len(job_links) - len(links_to_crawl)}")
    print(f"Job detail cần crawl mới  : {len(links_to_crawl)}")

    new_rows = []
    total = len(links_to_crawl)
    for index, url in enumerate(links_to_crawl, 1):
        print(f"[{index}/{total}] {url}")
        try:
            row = parse_raw_job(url)
            if row:
                new_rows.append(row)
                has_salary = row.get("salary_raw_min") is not None or row.get("salary_raw_max") is not None
                print("   ✓ Đã lưu raw" + (" + salary" if has_salary else " (không có salary)"))
            else:
                print("   ✗ Không đọc được JobPosting")
        except Exception as e:
            print("   ✗ Lỗi:", e)

        if new_rows and index % 20 == 0:
            temp = pd.concat([old_raw_df, pd.DataFrame(new_rows)], ignore_index=True)
            temp = temp.drop_duplicates(subset=["source", "job_id", "url"], keep="last")
            temp.to_csv(RAW_FILE, index=False, encoding="utf-8-sig")
            print(f"   💾 Autosave {RAW_FILE}: {len(temp)} job")
        time.sleep(REQUEST_DELAY)

    raw_df = pd.concat([old_raw_df, pd.DataFrame(new_rows)], ignore_index=True) if new_rows else old_raw_df.copy()
    if raw_df.empty:
        print("Không có dữ liệu RAW. Dừng.")
        return

    raw_df = raw_df.drop_duplicates(subset=["source", "job_id", "url"], keep="last")
    raw_df.to_csv(RAW_FILE, index=False, encoding="utf-8-sig")

    processed_rows = [process_raw_row(row) for row in raw_df.to_dict("records")]
    processed_df = pd.DataFrame(processed_rows)
    processed_columns = [
        "job_id", "job_role", "location", "level", "experience",
        "salary_min", "salary_max", "salary_mid", "python", "java",
        "javascript", "sql", "aws", "remote", "posted_date", "source",
        "url", "raw_title", "raw_skills",
    ]
    processed_df = processed_df[processed_columns]
    processed_df.to_csv(PROCESSED_ALL_FILE, index=False, encoding="utf-8-sig")

    salary_df = processed_df[processed_df["salary_mid"].notna()].copy()
    salary_df.to_csv(SALARY_DATASET_FILE, index=False, encoding="utf-8-sig")
    print_quality_report(raw_df, processed_df, salary_df)

    print("\nĐã cập nhật:")
    print(f"1. {LINKS_FILE} -> lịch sử URL từ search-result cards")
    print(f"2. {RAW_FILE} -> RAW cũ + RAW mới, append + dedup")
    print(f"3. {PROCESSED_ALL_FILE} -> rebuild từ toàn bộ RAW")
    print(f"4. {SALARY_DATASET_FILE} -> job có salary_mid")


if __name__ == "__main__":
    main()
