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

BASE_URL = "https://www.vietnamworks.com/"

START_URL = "https://www.vietnamworks.com/viec-lam?q=ai-engineer&g=5"

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

LINKS_FILE = "vnwork_job_links.csv"

RAW_FILE = "vnwork_raw.csv"

PROCESSED_ALL_FILE = "vnwork_processed_all.csv"

SALARY_DATASET_FILE = "vnwork_salary_dataset.csv"

# Nếu đã có vnwork_job_links.csv thì dùng lại, không crawl danh sách từ đầu.

USE_EXISTING_LINKS_IF_AVAILABLE = False  # cache chỉ để lưu lịch sử, không chặn crawl START_URL

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

    path = parsed.path.rstrip("/")

    return urlunparse(

        parsed._replace(

            path=path,

            query="",

            fragment=""

        )

    )

def is_job_detail_url(url):

    path = urlparse(url).path.rstrip("/")

    return bool(re.search(r"-\d+-jv$", path, re.IGNORECASE))

def extract_job_id(url):

    path = urlparse(url).path.rstrip("/")

    match = re.search(r"-(\d+)-jv$", path, re.IGNORECASE)

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

def _build_search_driver():

    from selenium import webdriver

    from selenium.webdriver.chrome.options import Options

    options = Options()

    # Cốc Cốc

    options.binary_location = r"C:\Program Files\CocCoc\Browser\Application\browser.exe"

    #options.add_argument("--headless=new")

    options.add_argument("--disable-gpu")

    options.add_argument("--no-sandbox")

    options.add_argument("--disable-dev-shm-usage")

    options.add_argument("--window-size=1920,1080")

    print("Đang khởi động Cốc Cốc...")

    driver = webdriver.Chrome(options=options)

    print("Khởi động Cốc Cốc thành công.")

    driver.set_page_load_timeout(15)

    return driver

def _extract_job_links_from_html(html):

    """Fallback: tìm URL/canonical VietnamWorks trong HTML/Next.js payload."""

    links = set()

    if not html:

        return links

    # 1) href bình thường.

    soup = BeautifulSoup(html, "html.parser")

    for a in soup.find_all("a", href=True):

        full_url = clean_url(urljoin(BASE_URL, a.get("href")))

        if is_job_detail_url(full_url):

            links.add(full_url)

    # 2) canonical trong JSON / Next.js payload.

    # Ví dụ: "canonical":"ai-enablement-engineer-2107871-jv"

    decoded = html.replace(r"\\\\\\\\/", "/")

    for m in re.finditer(

        r'["\\\\\\\\\\\\]canonical["\\\\\\\\\\\\]\s*:\s*["\\\\\\\\\\\\]\\\\([^"\\\\\\\\\\\\]+-\d+-jv)',

        decoded,

        flags=re.IGNORECASE,

    ):

        links.add(clean_url(urljoin(BASE_URL, m.group(1))))

    # 3) URL/slug xuất hiện trực tiếp trong source.

    for m in re.finditer(

        r'(?:(?:https?://(?:www\\\\\.)?vietnamworks\\\\\.com)?/)?'

        r'([a-zA-Z0-9][a-zA-Z0-9._%+-]*(?:-[a-zA-Z0-9._%+]+)*-\d+-jv)',

        decoded,

        flags=re.IGNORECASE,

    ):

        links.add(clean_url(urljoin(BASE_URL, m.group(1))))

    return links

def _extract_vnw_search_result_links(driver):

    """

    Chỉ lấy link từ job cards của khu vực search results.

    KHÔNG fallback sang a[href] toàn trang vì có thể dính Recommended/Ads.

    VietnamWorks có thể đổi class theo build, nên thử một số selector có ý nghĩa

    job-card/search-result. Selector nào bắt được >= 1 URL job hợp lệ sẽ được dùng.

    """

    selectors = [

        "[data-testid='job-card'] a[href]",

        "[data-testid*='job-card'] a[href]",

        "[data-testid*='job-item'] a[href]",

        "[data-testid*='job-result'] a[href]",

        "[class*='job-card'] a[href]",

        "[class*='jobCard'] a[href]",

        "[class*='job-item'] a[href]",

        "[class*='jobItem'] a[href]",

    ]

    for selector in selectors:

        urls = []

        seen = set()

        try:

            anchors = driver.find_elements(By.CSS_SELECTOR, selector)

        except Exception:

            continue

        for a in anchors:

            try:

                href = a.get_attribute("href")

            except Exception:

                continue

            if not href:

                continue

            full = clean_url(urljoin(BASE_URL, href))

            if is_job_detail_url(full) and full not in seen:

                seen.add(full)

                urls.append(full)

        if urls:

            print(f"  -> Search-result selector: {selector}")

            return urls

    return []

def get_job_links_from_page(url, driver=None):

    own_driver = driver is None

    if own_driver:

        driver = _build_search_driver()

    try:

        print("  -> Cốc Cốc đang mở search page...")

        try:

            driver.get(url)

        except Exception as e:

            print(f"  -> Page load chưa hoàn tất ({type(e).__name__}), tiếp tục đọc DOM...")

        try:

            WebDriverWait(driver, 12).until(

                lambda d: d.execute_script("return document.readyState") in ("interactive", "complete")

            )

        except Exception:

            pass

        # Scroll vừa đủ để các job card lazy-render.

        job_urls = []

        last_count = -1

        stable_rounds = 0

        for _ in range(8):

            job_urls = _extract_vnw_search_result_links(driver)

            if len(job_urls) == last_count:

                stable_rounds += 1

            else:

                stable_rounds = 0

                last_count = len(job_urls)

            if stable_rounds >= 2 and job_urls:

                break

            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")

            time.sleep(0.8)

        print(f"  -> URL hiện tại: {driver.current_url}")

        print(f"  -> Link trong search-result cards: {len(job_urls)}")

        if not job_urls:
            print("  ⚠ Không xác định được job-card search results.")
            print("  ⚠ Không quét toàn trang để tránh Recommended/Ads.")

        return sorted(job_urls)

    except Exception as e:

        print("Lỗi lấy danh sách bằng Selenium:", repr(e))

        return []

    finally:

        if own_driver and driver is not None:

            driver.quit()

def collect_all_job_links():

    all_links = set()

    driver = None

    try:

        driver = _build_search_driver()

        for page in range(1, MAX_PAGES + 1):

            page_url = set_page(START_URL, page)

            print(f"\nĐang đọc page {page}")

            print(page_url)

            links = get_job_links_from_page(page_url, driver=driver)

            new_links = [link for link in links if link not in all_links]

            print(f"Tìm thấy: {len(links)} job")

            print(f"Job mới: {len(new_links)}")

            if not new_links:

                print("Không còn job mới. Dừng crawl page.")

                break

            all_links.update(new_links)

            time.sleep(REQUEST_DELAY)

    finally:

        if driver is not None:

            driver.quit()

    return sorted(all_links)

def load_or_collect_links():

    """

    Luôn crawl START_URL hiện tại.

    LINKS_FILE chỉ là lịch sử link đã tìm được, KHÔNG dùng nó để bỏ qua START_URL.

    - Link cũ được giữ nguyên.

    - Link mới được append.

    - Nếu crawl search thất bại / trả 0 link: tuyệt đối không ghi đè cache cũ.

    """

    existing_links = []

    if os.path.exists(LINKS_FILE):

        try:

            df_links = pd.read_csv(LINKS_FILE)

            if "url" in df_links.columns:

                existing_links = [

                    clean_url(u)

                    for u in df_links["url"].dropna().astype(str).tolist()

                    if is_job_detail_url(clean_url(u))

                ]

                existing_links = unique_keep_order(existing_links)

                print(f"Đã có {len(existing_links)} URL lịch sử trong {LINKS_FILE}")

        except Exception as e:

            print(f"Không đọc được {LINKS_FILE}: {e}")

    print(f"Crawl START_URL hiện tại: {START_URL}")

    crawled_links = collect_all_job_links()

    # Search fail => keep old cache untouched.

    if not crawled_links:

        if existing_links:

            print(

                f"Không lấy được link mới từ START_URL. "

                f"GIỮ NGUYÊN {len(existing_links)} link cũ; không ghi đè file."

            )

            return []  # search lỗi thì không crawl nhầm cache lịch sử

        print("Không có link cũ và cũng không crawl được link mới.")

        return []

    old_set = set(existing_links)

    new_only = [u for u in crawled_links if u not in old_set]

    all_links = unique_keep_order(existing_links + crawled_links)

    pd.DataFrame({"url": all_links}).to_csv(

        LINKS_FILE,

        index=False,

        encoding="utf-8-sig"

    )

    print(f"Search hiện tại tìm thấy : {len(crawled_links)} URL")

    print(f"URL mới                 : {len(new_only)}")

    print(f"Tổng URL lịch sử        : {len(all_links)}")

    print(f"Đã cập nhật {LINKS_FILE}")

    # Return all links; main() will filter already-crawled jobs before detail requests.

    return crawled_links  # chỉ crawl kết quả START_URL hiện tại

# 2. VIETNAMWORKS NEXT.JS JOB DATA

# =========================================================

def _extract_next_f_stream(html_text):
    """Ghép payload self.__next_f.push([1, "..."]) theo đúng thứ tự."""
    marker = 'self.__next_f.push([1,'
    chunks = []
    pos = 0

    while True:
        idx = html_text.find(marker, pos)
        if idx == -1:
            break

        i = idx + len(marker)
        while i < len(html_text) and html_text[i].isspace():
            i += 1

        if i >= len(html_text) or html_text[i] != '"':
            pos = i
            continue

        # Parse đúng một JSON string, kể cả khi bên trong có escape.
        j = i + 1
        escaped = False
        while j < len(html_text):
            ch = html_text[j]
            if escaped:
                escaped = False
            elif ch == '\\':
                escaped = True
            elif ch == '"':
                break
            j += 1

        if j >= len(html_text):
            break

        literal = html_text[i:j + 1]
        try:
            chunks.append(json.loads(literal))
        except json.JSONDecodeError:
            pass

        pos = j + 1

    return "".join(chunks)

def _parse_rsc_records(stream):

    """Parse record Next.js Flight: JSON record và text record dạng id:T<hex_length>,text."""

    records = {}

    i = 0

    n = len(stream)

    decoder = json.JSONDecoder()

    while i < n:

        # Record id nằm ở đầu dòng: 26:, 2a:, 34: ...

        m = re.match(r'([0-9a-f]+):', stream[i:], re.IGNORECASE)

        if not m:

            nl = stream.find('\n', i)

            i = n if nl == -1 else nl + 1

            continue

        rid = m.group(1).lower()

        pos = i + m.end()

        # Text record: 27:Tf25,\\\\\\\\\\\\<f25 ký tự>

        tm = re.match(r'T([0-9a-f]+),', stream[pos:], re.IGNORECASE)

        if tm:

            length = int(tm.group(1), 16)

            text_start = pos + tm.end()

            records[rid] = stream[text_start:text_start + length]

            i = text_start + length

            if i < n and stream[i] == '\n':

                i += 1

            continue

        try:

            value, consumed = decoder.raw_decode(stream[pos:])

            records[rid] = value

            i = pos + consumed

            if i < n and stream[i] == '\n':

                i += 1

        except json.JSONDecodeError:

            nl = stream.find('\n', pos)

            i = n if nl == -1 else nl + 1

    # Một số text record của React Flight nối sát record trước, không có \n.

    # Quét bổ sung toàn stream để không mất $27/$28 hoặc record ngay sau text.

    for tm in re.finditer(r'([0-9a-f]+):T([0-9a-f]+),', stream, re.IGNORECASE):

        rid = tm.group(1).lower()

        length = int(tm.group(2), 16)

        records[rid] = stream[tm.end():tm.end() + length]

    # Quét bổ sung các JSON object/list record chưa bắt được.

    for jm in re.finditer(r'([0-9a-f]+):(?=[\\\\\\\\[{])', stream, re.IGNORECASE):

        rid = jm.group(1).lower()

        if rid in records:

            continue

        try:

            value, _ = decoder.raw_decode(stream[jm.end():])

            records[rid] = value

        except json.JSONDecodeError:

            pass

    return records

def _resolve_rsc(value, records, seen=None):

    """Resolve $27, $29, $34... thành dữ liệu thật; chống vòng lặp."""

    if seen is None:

        seen = set()

    if isinstance(value, str) and re.fullmatch(r'\\\\\\\\$[0-9a-f]+', value, re.IGNORECASE):

        rid = value[1:].lower()

        if rid in seen or rid not in records:

            return value

        return _resolve_rsc(records[rid], records, seen | {rid})

    if isinstance(value, list):

        return [_resolve_rsc(v, records, seen) for v in value]

    if isinstance(value, dict):

        return {k: _resolve_rsc(v, records, seen) for k, v in value.items()}

    return value

def _find_job_object(records):

    # Không phụ thuộc record id = 26 vì id có thể đổi giữa các request/build.

    candidates = []

    for rid, value in records.items():

        if isinstance(value, dict) and value.get("jobId") and value.get("jobTitle"):

            candidates.append(value)

    if not candidates:

        return None

    # Ưu tiên object đầy đủ nhất.

    return max(candidates, key=lambda x: len(x))

def get_jobposting(url):

    try:

        response = session.get(url, timeout=25)

        if response.status_code != 200:

            print(f"Lỗi job {response.status_code}: {url}")

            return None

        stream = _extract_next_f_stream(response.text)

        if not stream:

            print(f"Không tìm thấy Next.js payload: {url}")

            return None

        records = _parse_rsc_records(stream)

        job_data = _find_job_object(records)

        if not job_data:

            print(f"Không tìm thấy job object VietnamWorks: {url}")

            return None

        return _resolve_rsc(job_data, records)

    except Exception as e:

        print("Lỗi get_jobposting:", e, url)

        return None

# =========================================================

# 3. LOCATION

# =========================================================

def extract_raw_locations(job_data):

    job_locations = job_data.get("workingLocations") or []

    if not isinstance(job_locations, list):

        job_locations = [job_locations]

    locations = []

    for loc in job_locations:

        if not isinstance(loc, dict):

            continue

        locations.append({

            "address": loc.get("address"),

            "city": loc.get("cityName"),

            "city_vi": loc.get("cityNameVI"),

            "city_id": loc.get("cityId"),

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

    regions = []

    for loc in extract_raw_locations(job_data):

        value = loc.get("city_vi") or loc.get("city") or loc.get("address")

        value = normalize_region_name(value)

        if value:

            regions.append(value)

    regions = unique_keep_order(regions)

    if not regions:

        return normalize_region_name(job_data.get("address"))

    return "Nhiều địa điểm" if len(regions) > 1 else regions[0]

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
    """Trả về (experience_min, experience_max)."""
    text = clean_description(description).lower()
    if not text:
        return None, None

    range_patterns = [
        r"(?:from\s+)?(\d+)\s*(?:-|–|—|to)\s*(\d+)\s*\+?\s*years?",
        r"(\d+)\s*(?:-|–|—)\s*(\d+)\s*yrs?",
    ]
    for pattern in range_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            exp_min, exp_max = int(match.group(1)), int(match.group(2))
            if 0 <= exp_min <= 30 and 0 <= exp_max <= 30 and exp_min <= exp_max:
                return exp_min, exp_max

    min_patterns = [
        r"at\s+least\s+(\d+)\+?\s*years?",
        r"minimum\s+(?:of\s+)?(\d+)\+?\s*years?",
        r"min\.?\s*(\d+)\+?\s*years?",
        r"more\s+than\s+(\d+)\s*years?",
        r"over\s+(\d+)\s*years?",
        r"(\d+)\+\s*years?",
    ]
    for pattern in min_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            exp = int(match.group(1))
            if 0 <= exp <= 30:
                return exp, None

    match = re.search(r"(\d+)\s+years?\s+(?:of\s+)?(?:working\s+)?experience", text, re.IGNORECASE)
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

        (".NET Developer", [r"\\\\\.net\b", r"\bc#\b", r"\bdotnet\b"]),

        ("PHP Developer", [r"\bphp\b", r"\blaravel\b"]),

        ("Python Developer", [r"\bpython developer\b", r"\bpython engineer\b"]),

        ("Java Developer", [r"\bjava developer\b", r"\bjava engineer\b", r"\bjava lead\b"]),

        ("JavaScript Developer", [r"\bjavascript developer\b", r"\bnode\\\\\.?js developer\b", r"\bnodejs developer\b"]),

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

    result = {

        "salary_raw_min": None,

        "salary_raw_max": None,

        "salary_currency": None,

        "salary_unit": None,

        "salary_text": job_data.get("prettySalary") or job_data.get("prettySalaryVI")

    }

    # VietnamWorks dùng 0 khi lương bị ẩn/thương lượng -> phải coi là missing.

    if job_data.get("isSalaryVisible") is False:

        return result

    raw_min = job_data.get("salaryMin")

    raw_max = job_data.get("salaryMax")

    try:

        raw_min = float(raw_min) if raw_min not in (None, "", 0, "0") else None

    except (TypeError, ValueError):

        raw_min = None

    try:

        raw_max = float(raw_max) if raw_max not in (None, "", 0, "0") else None

    except (TypeError, ValueError):

        raw_max = None

    result["salary_raw_min"] = raw_min

    result["salary_raw_max"] = raw_max

    result["salary_currency"] = (job_data.get("salaryCurrency") or "").upper() or None

    # salaryPeriodId=0 ở job thương lượng không mang nghĩa MONTH. Chỉ set MONTH khi có salary thật.

    if raw_min is not None or raw_max is not None:

        result["salary_unit"] = "MONTH"

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

def _extract_skills(job_data):

    raw = job_data.get("skills") or []

    if isinstance(raw, list):

        names = []

        for item in raw:

            if isinstance(item, dict) and item.get("skillName"):

                names.append(str(item["skillName"]).strip())

            elif isinstance(item, str) and not item.startswith("$"):

                names.append(item.strip())

        return ", ".join(unique_keep_order(names))

    return str(raw) if raw else ""

def _extract_level_vnw(job_data, title):

    title_level = extract_level(title)

    if title_level != "Unknown":

        return title_level

    raw = " ".join(filter(None, [job_data.get("jobLevel"), job_data.get("jobLevelVI")])).lower()

    if "intern" in raw or "thực tập" in raw:

        return "Intern"

    if "fresher" in raw or "mới tốt nghiệp" in raw:

        return "Fresher"

    if "non-manager" in raw or "experienced" in raw or "nhân viên" in raw:

        return "Experienced"

    if "manager" in raw or "quản lý" in raw or "trưởng phòng" in raw:

        return "Manager"

    return "Unknown"

def parse_raw_job(url):

    job_data = get_jobposting(url)

    if not job_data:

        return None

    title = job_data.get("jobTitle")

    company = job_data.get("companyName")

    description_html = job_data.get("jobDescription") or ""

    requirement_html = job_data.get("jobRequirement") or ""

    description_text = clean_description(description_html)

    requirement_text = clean_description(requirement_html)

    combined_text = " ".join(x for x in [description_text, requirement_text] if x).strip()

    skills = _extract_skills(job_data)

    raw_locations = extract_raw_locations(job_data)

    raw_salary = extract_raw_salary(job_data)

    years = job_data.get("yearsOfExperience")

    try:

        years = int(years) if years not in (None, "") else 0

    except (TypeError, ValueError):

        years = 0

    if 0 < years <= 30:

        exp_min, exp_max = years, None

    else:

        # Requirement đáng tin hơn description cho số năm kinh nghiệm.

        exp_min, exp_max = extract_experience_range(requirement_html or description_html)

    return {

        "job_id": str(job_data.get("jobId") or extract_job_id(url) or ""),

        "title": title,

        "company": company,

        "location": extract_location(job_data),

        "locations_raw": json.dumps(raw_locations, ensure_ascii=False),

        "level": _extract_level_vnw(job_data, title),

        "experience_min": exp_min,

        "experience_max": exp_max,

        "salary_raw_min": raw_salary["salary_raw_min"],

        "salary_raw_max": raw_salary["salary_raw_max"],

        "currency": raw_salary["salary_currency"],

        "salary_unit": raw_salary["salary_unit"],

        "salary_text": raw_salary["salary_text"],

        "skills": skills,

        "work_type": job_data.get("typeWorkingId"),

        "posted_date": job_data.get("approvedOn") or job_data.get("createdOn"),

        "valid_through": job_data.get("expiredOn"),

        "description": combined_text,

        "job_location_type": None,

        "vnw_job_level": job_data.get("jobLevel"),

        "vnw_job_level_vi": job_data.get("jobLevelVI"),

        "vnw_pretty_salary": job_data.get("prettySalary") or job_data.get("prettySalaryVI"),

        "source": "VietnamWorks",

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

            [r"\bjavascript\b", r"\bjava\s*script\b", r"\bjs\b", r"\bnode\\\\\.?js\b", r"\bnodejs\b"]

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

    print("VNWORK SALARY DATA PIPELINE - APPEND SAFE")

    print("=" * 70)

    # 1) Luôn crawl START_URL rồi merge vào lịch sử job links.

    job_links = load_or_collect_links()

    print(f"\nTổng URL job trong lịch sử: {len(job_links)}")

    # 2) Load RAW cũ để không crawl detail lại.

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

                str(v).strip()

                for v in old_raw_df["job_id"].dropna().tolist()

                if str(v).strip()

            }

        if "url" in old_raw_df.columns:

            existing_urls = {

                clean_url(str(v))

                for v in old_raw_df["url"].dropna().tolist()

                if str(v).strip()

            }

    # Chỉ crawl detail job chưa có trong RAW.

    # Không tự lọc theo keyword sau khi website đã trả job trong search results.

    links_to_crawl = []

    for url in job_links:

        jid = extract_job_id(url)

        if clean_url(url) in existing_urls:

            continue

        if jid and jid in existing_job_ids:

            continue

        links_to_crawl.append(url)

    print(f"Job đã có trong RAW       : {len(job_links) - len(links_to_crawl)}")

    print(f"Job detail cần crawl mới  : {len(links_to_crawl)}")

    new_rows = []

    total = len(links_to_crawl)

    for index, url in enumerate(links_to_crawl, start=1):

        print(f"[{index}/{total}] {url}")

        try:

            row = parse_raw_job(url)

            if row:

                new_rows.append(row)

                has_salary = (

                    row.get("salary_raw_min") is not None

                    or row.get("salary_raw_max") is not None

                )

                print("   ✓ Đã lưu raw" + (" + salary" if has_salary else " (không có salary)"))

            else:

                print("   ✗ Không đọc được JobPosting")

        except Exception as e:

            print("   ✗ Lỗi:", e)

        # Autosave theo kiểu MERGE, không bao giờ overwrite RAW cũ chỉ bằng batch mới.

        if new_rows and index % 20 == 0:

            batch_df = pd.DataFrame(new_rows)

            temp_raw = pd.concat([old_raw_df, batch_df], ignore_index=True)

            temp_raw = temp_raw.drop_duplicates(

                subset=["source", "job_id", "url"], keep="last"

            )

            temp_raw.to_csv(RAW_FILE, index=False, encoding="utf-8-sig")

            print(f"   💾 Autosave merge {RAW_FILE}: {len(temp_raw)} job")

        time.sleep(REQUEST_DELAY)

    # 3) Merge RAW cũ + job mới.

    if new_rows:

        new_raw_df = pd.DataFrame(new_rows)

        raw_df = pd.concat([old_raw_df, new_raw_df], ignore_index=True)

    else:

        raw_df = old_raw_df.copy()

    if raw_df.empty:

        print("Không có dữ liệu RAW. Dừng.")

        return

    raw_df = raw_df.drop_duplicates(

        subset=["source", "job_id", "url"], keep="last"

    )

    raw_df.to_csv(

        RAW_FILE,

        index=False,

        encoding="utf-8-sig"

    )

    # 4) Luôn rebuild processed từ TOÀN BỘ RAW.

    processed_rows = [

        process_raw_row(row)

        for row in raw_df.to_dict("records")

    ]

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

    # 5) ML dataset = subset có salary_mid.

    salary_df = processed_df[

        processed_df["salary_mid"].notna()

    ].copy()

    salary_df.to_csv(

        SALARY_DATASET_FILE,

        index=False,

        encoding="utf-8-sig"

    )

    print_quality_report(raw_df, processed_df, salary_df)

    print("\nĐã cập nhật:")

    print(f"1. {LINKS_FILE}            -> lịch sử URL, append + dedup")

    print(f"2. {RAW_FILE}              -> RAW cũ + RAW mới, append + dedup")

    print(f"3. {PROCESSED_ALL_FILE}    -> rebuild từ toàn bộ RAW")

    print(f"4. {SALARY_DATASET_FILE}   -> job có salary_mid")

    print("\nLƯU Ý KHI TRAIN MODEL:")

    print("X = job_role, location, level, experience, python, java, javascript, sql, aws, remote")

    print("y = salary_mid")

    print("KHÔNG đưa salary_min hoặc salary_max vào X vì sẽ bị data leakage.")

if __name__ == "__main__":

    main()

