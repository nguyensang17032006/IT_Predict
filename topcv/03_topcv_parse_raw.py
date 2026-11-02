"""Parse saved TopCV HTML; run after step 02.
Install: pip install pandas beautifulsoup4
Read: topcv_job_links.csv, details/<job_id>.html, previous topcv_raw.csv.
Write: topcv_raw.csv and topcv_parse_report.json (latest run, for a future dashboard).
Work-mode extraction: old layout -> new layout -> scoped exact-label fallback.
Failed work-mode extraction keeps the previous value, with stale status and the
last successful field timestamp in the JSON report. Never infer Onsite from absence.
Other raw columns keep their established schema. No browser/network calls.
"""
from pathlib import Path
from datetime import datetime, timedelta, timezone
import json
import re
import unicodedata
import pandas as pd
from bs4 import BeautifulSoup
# =========================================================
# CONFIG
# =========================================================
BASE_DIR = Path(__file__).resolve().parent
LINKS_FILE = BASE_DIR / "topcv_job_links.csv"
DETAIL_FOLDER = BASE_DIR / "details"
RAW_FILE = BASE_DIR / "topcv_raw.csv"
REPORT_FILE = BASE_DIR / "topcv_parse_report.json"
SITE_TZ = timezone(timedelta(hours=7))
RAW_COLUMNS = [
    "job_id", "title", "company", "location", "locations_raw", "level",
    "experience_min", "experience_max", "salary_raw_min", "salary_raw_max",
    "currency", "salary_unit", "salary_text", "skills", "work_type",
    "posted_date", "valid_through", "description", "requirements",
    "job_location_type", "source", "url", "status", "first_seen",
    "last_seen", "expired_at", "collected_at",
]
def now_text():
    return datetime.now(SITE_TZ).isoformat(timespec="seconds")
def read_csv_if_exists(path):
    if not path.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(path, dtype=str, keep_default_na=False, encoding="utf-8-sig")
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
GENERAL_LAYOUTS = [
    ("box_job_information",
     ".box-job-information-general-info-list__item, "
     ".box-job-information-general-info-list__item--content",
     ".box-job-information-general-info-list__item--content-title",
     ".box-job-information-general-info-list__item--content-desc"),
    ("general_information_data", ".general-information-data",
     ".general-information-data__label", ".general-information-data__value"),
]
GENERAL_LABELS = {"Hình thức làm việc", "Loại hình làm việc", "Cấp bậc", "Kinh nghiệm"}
# Label fallback is limited to job information containers, never the whole page.
INFORMATION_ROOTS = (
    ".box-job-information-general-info, .box-job-information-general-info-list, "
    ".general-information, .job-detail__information, .box-job-information, "
    "#job-detail, .job-detail__body"
)


def normalize_label(value):
    value = unicodedata.normalize("NFKD", str(value or "").casefold()).replace("đ", "d")
    value = "".join(c for c in value if not unicodedata.combining(c))
    return " ".join(value.split()).rstrip(" :：")


def is_missing(value):
    return value is None or str(value).strip().casefold() in {"", "unknown", "nan", "none", "null", "<na>"}


def excluded_information_node(node):
    for parent in [node, *node.parents]:
        if getattr(parent, "name", None) in {"aside", "nav", "footer", "script", "style", "template"}:
            return True
        attrs = getattr(parent, "attrs", {}) or {}
        style = re.sub(r"\s+", "", attrs.get("style", "")).lower()
        if "hidden" in attrs or attrs.get("aria-hidden") == "true" or "display:none" in style:
            return True
        classes = " ".join(attrs.get("class", []))
        if re.search(r"recommend|suggest|related|similar|job-card", classes, re.I):
            return True
    return False


def layout_pairs(soup, layout):
    _, block_selector, label_selector, value_selector = layout
    for block in soup.select(block_selector):
        if excluded_information_node(block):
            continue
        label = text_or_none(block.select_one(label_selector))
        value = text_or_none(block.select_one(value_selector))
        if label:
            yield label.rstrip(" :："), value


def general_information_pairs(soup):
    for layout in GENERAL_LAYOUTS:
        yield from layout_pairs(soup, layout)


def label_fallback_values(soup, wanted):
    """Exact label + adjacent value in a bounded job-information block."""
    wanted = normalize_label(wanted)
    known_labels = {normalize_label(x) for x in GENERAL_LABELS}
    visited = set()
    for root in soup.select(INFORMATION_ROOTS):
        if excluded_information_node(root):
            continue
        for label in root.find_all(["div", "span", "p", "dt", "label", "strong", "b"]):
            if id(label) in visited or excluded_information_node(label):
                continue
            visited.add(id(label))
            if normalize_label(label.get_text(" ", strip=True)) != wanted:
                continue
            # Try siblings of the label, then its immediate wrapper only.
            branch = label
            for _ in range(2):
                parent = branch.parent
                if parent is None or branch is root:
                    break
                if len(parent.get_text(" ", strip=True)) > 700:
                    break
                for sibling in branch.next_siblings:
                    if not getattr(sibling, "name", None):
                        continue
                    if excluded_information_node(sibling):
                        continue
                    value = text_or_none(sibling)
                    if not value:
                        continue
                    # Never cross another field's label to find a value.
                    if normalize_label(value) in known_labels:
                        break
                    nested_labels = sibling.find_all(["div", "span", "p", "dt", "label"])
                    if any(normalize_label(n.get_text(" ", strip=True)) in known_labels for n in nested_labels):
                        break
                    if sibling.name not in {"a", "button", "input"} and len(value) <= 300:
                        yield value
                    break
                if parent is root:
                    break
                branch = parent


def extract_general_info(soup):
    result = {}
    for label, value in general_information_pairs(soup):
        if value and is_missing(result.get(label)):
            result[label] = value
    for wanted in GENERAL_LABELS:
        if not any(normalize_label(k) == normalize_label(wanted) and not is_missing(v)
                   for k, v in result.items()):
            value = next(label_fallback_values(soup, wanted), None)
            if value:
                result[wanted] = value
    return result


def general_value(general, wanted):
    return next((v for k, v in general.items()
                 if normalize_label(k) == normalize_label(wanted) and not is_missing(v)), None)


def valid_location_type(value):
    return not is_missing(value) and bool(re.search(
        r"\bonsite\b|on[ -]site|van phong|\bhybrid\b|\bremote\b|"
        r"tu xa|lam tai nha|work from home|telecommut|viec lam online",
        normalize_label(value)))


def extract_job_location_type(soup, general=None, diagnostics=None):
    attempted, rejected = [], []
    for method in [layout[0] for layout in GENERAL_LAYOUTS] + ["label_fallback"]:
        attempted.append(method)
        if method == "label_fallback":
            values = label_fallback_values(soup, "Hình thức làm việc")
        else:
            layout = next(x for x in GENERAL_LAYOUTS if x[0] == method)
            values = (v for k, v in layout_pairs(soup, layout)
                      if normalize_label(k) == "hinh thuc lam viec")
        for value in values:
            if not valid_location_type(value):
                if not is_missing(value):
                    rejected.append(value)
                continue
            if diagnostics is not None:
                diagnostics.append({
                    "field": "job_location_type", "severity": "info" if method == GENERAL_LAYOUTS[0][0] else "warning",
                    "code": "extracted" if method == GENERAL_LAYOUTS[0][0] else "fallback_used",
                    "method": method, "attempted_methods": attempted.copy(),
                    "extracted_value": value, "rejected_values": rejected,
                    "message": "Đã đọc hình thức làm việc." if method == GENERAL_LAYOUTS[0][0]
                               else "Đã đọc hình thức làm việc bằng cách dự phòng.",
                })
            return value
    if diagnostics is not None:
        diagnostics.append({
            "field": "job_location_type", "severity": "error", "code": "field_extraction_failed",
            "method": None, "attempted_methods": attempted, "extracted_value": None,
            "rejected_values": rejected,
            "message": "Không trích xuất được hình thức làm việc; chưa xác định nguyên nhân.",
        })
    return None


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
    # Only interpret the dedicated experience field; missing stays missing.
    text = clean_text(text)
    if not text:
        return None, None
    normalized = unicodedata.normalize("NFKC", text)
    low = " ".join(normalized.casefold().split())
    unaccented = unicodedata.normalize("NFKD", low).replace("đ", "d")
    unaccented = "".join(c for c in unaccented if not unicodedata.combining(c))
    if re.search(r"\bkhong (?:yeu cau|can)(?: kinh nghiem)?\b", unaccented):
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
        numbers = re.findall(r"\d+(?:[.,]\d+)*", low)
        numbers = [parse_salary_number(x) for x in numbers]
        numbers = [x for x in numbers if x is not None]
        if numbers:
            salary_min = numbers[0]
            salary_max = numbers[1] if len(numbers) >= 2 else numbers[0]
            # Đồng nhất với JSON-LD: VND lưu theo đơn vị đồng.
            if result["currency"] == "VND" and "triệu" in low:
                salary_min = 1_000_000
                salary_max = 1_000_000
            result["salary_raw_min"] = salary_min
            result["salary_raw_max"] = salary_max
            result["salary_unit"] = result["salary_unit"] or "MONTH"
    return result
# =========================================================
# PARSE 1 HTML
# =========================================================
def parse_detail_html(file_path, link_info, diagnostics=None):
    html = file_path.read_text(encoding="utf-8", errors="ignore")
    soup = BeautifulSoup(html, "html.parser")
    if not (get_jobposting_from_soup(soup) or soup.select_one("h1.box-header-job__title")):
        raise ValueError("HTML không có dấu hiệu nhận diện trang chi tiết tuyển dụng.")
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
    experience_text = header.get("Kinh nghiệm") or general_value(general, "Kinh nghiệm")
    exp_min, exp_max = parse_experience(experience_text)
    salary_text = text_or_none(
        soup.select_one(".box-header-job__salary--title")
    )
    salary = extract_salary(job, salary_text)
    level = general_value(general, "Cấp bậc")
    if not level and job:
        level = clean_text(job.get("occupationalCategory"))
    work_type = general_value(general, "Loại hình làm việc")
    if not work_type and job:
        employment_type = job.get("employmentType")
        if isinstance(employment_type, list):
            work_type = ", ".join(map(str, employment_type))
        else:
            work_type = clean_text(employment_type)
    job_location_type = extract_job_location_type(soup, general, diagnostics)
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
def atomic_write(path, writer):
    temporary = path.with_name(path.name + ".tmp")
    writer(temporary)
    temporary.replace(path)


def read_previous_report():
    if not REPORT_FILE.exists():
        return {}
    # A corrupt report is an input error: do not silently lose field timestamps.
    data = json.loads(REPORT_FILE.read_text(encoding="utf-8"))
    return {str(x["job_id"]): x for x in data.get("jobs", [])}


def previous_success(previous, old, field):
    for item in previous.get("fields", []):
        if item.get("field") == field:
            return item.get("last_success_at")
    return old.get("collected_at") if old and not is_missing(old.get(field)) else None


def apply_field_diagnostics(row, old, diagnostics, previous, stamp):
    for item in diagnostics:
        field = item["field"]
        success_at = previous_success(previous, old, field)
        if item["severity"] != "error":
            item.update(value=row.get(field), freshness="current", preserved=False, last_success_at=stamp)
        elif old and not is_missing(old.get(field)):
            row[field] = old[field]
            item.update(value=old[field], freshness="stale", preserved=True, last_success_at=success_at)
        else:
            row[field] = None
            item.update(value=None, freshness="unavailable", preserved=False, last_success_at=None)
        item["checked_at"] = stamp
    return row


def update_lifecycle(row, link):
    row = dict(row)
    for field in ["status", "first_seen", "last_seen", "expired_at"]:
        if field in link:
            # Preserve first detection if the link history lacks that timestamp.
            if field == "first_seen" and is_missing(link[field]) and not is_missing(row.get(field)):
                continue
            row[field] = link[field]
    return row


def main():
    if not LINKS_FILE.exists():
        raise FileNotFoundError(f"Không tìm thấy: {LINKS_FILE}")
    links_df = read_csv_if_exists(LINKS_FILE)
    if "job_id" not in links_df.columns:
        raise ValueError("Lịch sử link thiếu cột job_id; không ghi đè raw.")
    old_raw_df = read_csv_if_exists(RAW_FILE)
    if not old_raw_df.empty and "job_id" not in old_raw_df.columns:
        raise ValueError("Raw cũ thiếu cột job_id; không ghi đè.")
    old_map = {str(r["job_id"]): r for r in old_raw_df.to_dict("records")}
    previous_reports = read_previous_report()
    # Preserve historical rows not present in the current link CSV as well.
    rows = dict(old_map)
    reports = []
    started = now_text()
    for index, link in enumerate(links_df.drop_duplicates("job_id", keep="last").to_dict("records"), 1):
        job_id = str(link["job_id"]).strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]+", job_id):
            raise ValueError(f"job_id không hợp lệ: {job_id!r}; không ghi đè raw.")
        link["job_id"] = job_id
        path = DETAIL_FOLDER / f"{job_id}.html"
        old = old_map.get(job_id)
        previous = previous_reports.get(job_id, {})
        stamp = now_text()
        report = {"job_id": job_id, "source": "TopCV", "url": link.get("url"),
                  "html_path": str(path), "checked_at": stamp, "fields": [], "errors": []}
        print(f"[{index}/{len(links_df)}] {job_id}")
        try:
            if not path.is_file():
                raise FileNotFoundError("Chưa có HTML đã lưu của tin.")
            row = parse_detail_html(path, link, report["fields"])
            row = apply_field_diagnostics(row, old, report["fields"], previous, stamp)
            rows[job_id] = row
            severities = {x["severity"] for x in report["fields"]}
            report["status"] = "error" if "error" in severities else "warning" if "warning" in severities else "success"
        except Exception as exc:
            if old:
                rows[job_id] = update_lifecycle(old, link)
            # Discard any partial diagnostics when the entire record failed.
            report["fields"] = [{
                "field": "job_location_type", "severity": "error", "code": "not_checked",
                "method": None, "attempted_methods": [], "extracted_value": None,
                "value": old.get("job_location_type") if old else None,
                "freshness": "stale" if old and not is_missing(old.get("job_location_type")) else "unavailable",
                "preserved": bool(old and not is_missing(old.get("job_location_type"))),
                "last_success_at": previous_success(previous, old, "job_location_type"),
                "checked_at": stamp,
                "message": "Không kiểm tra được trường vì HTML bị thiếu hoặc parse lỗi.",
            }]
            report.update(status="error", record_preserved=bool(old))
            report["errors"].append({"code": "html_missing" if not path.is_file() else "parse_failed",
                                     "message": f"{type(exc).__name__}: {exc}"})
        report["record_collected_at"] = rows.get(job_id, {}).get("collected_at")
        for field in report["fields"]:
            if field["severity"] != "info":
                suffix = " Giữ giá trị cũ." if field["preserved"] else ""
                print(f"   {field['severity'].upper()}: {field['message']}{suffix}")
        for error in report["errors"]:
            print(f"   ERROR: {error['message']}")
        reports.append(report)

    frame = pd.DataFrame(rows.values()).reindex(columns=RAW_COLUMNS)
    # No extra diagnostic columns in raw: the existing preprocessing still works.
    atomic_write(RAW_FILE, lambda path: frame.to_csv(path, index=False, encoding="utf-8-sig"))
    summary = {key: sum(r["status"] == key for r in reports) for key in ["success", "warning", "error"]}
    summary.update(total_checked=len(reports), raw_rows=len(frame),
                   stale_fields=sum(f["freshness"] == "stale" for r in reports for f in r["fields"]))
    result = {"schema_version": 1, "source": "TopCV", "started_at": started, "finished_at": now_text(),
              "status": "completed_with_errors" if summary["error"] else "completed_with_warnings" if summary["warning"] else "completed",
              "monitored_fields": ["job_location_type"], "summary": summary, "jobs": reports,
              "notes": "Báo cáo lần chạy gần nhất. collected_at là thời điểm parse bản ghi; "
                       "xem fields[].last_success_at để biết thời điểm đọc thành công của trường đang giữ lại."}
    atomic_write(REPORT_FILE, lambda path: path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"))
    print(f"DONE: {summary}")
    print(f"Raw: {RAW_FILE}")
    print(f"Report: {REPORT_FILE}")
    # A future worker can inspect this structured result without parsing stdout.
    return result


if __name__ == "__main__":
    main()
