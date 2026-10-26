"""Parse saved VietnamWorks HTML; no browser or network.
Install: pip install beautifulsoup4
Run: python 03_parse_raw.py
Inputs: details/*.html and vnwork_job_links.csv next to this script.
Output: vnwork_raw.csv (latest row per job_id), vnwork_raw_parse_report.csv.
User mapping: part-time -> PARTTIME, everything else -> FULLTIME;
online -> Remote, everything else -> Onsite (including missing labels).
"""
from __future__ import annotations
import csv
import json
import math
import re
import unicodedata
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlparse, urljoin
from bs4 import BeautifulSoup
BASE_DIR = Path(__file__).resolve().parent
BASE_URL = 'https://www.vietnamworks.com'
SITE_TZ = timezone(timedelta(hours=7))
DETAIL_DIR = BASE_DIR / 'details'
LINKS_FILE = BASE_DIR / 'vnwork_job_links.csv'
OUTPUT_FILE = BASE_DIR / 'vnwork_raw.csv'

COLUMNS = ['job_id', 'title', 'company', 'location', 'locations_raw', 'level',
           'experience_min', 'experience_max', 'salary_raw_min', 'salary_raw_max',
           'currency', 'salary_unit', 'salary_text', 'skills', 'work_type',
           'posted_date', 'valid_through', 'description', 'requirements', 'job_location_type',
           'source', 'url', 'status', 'first_seen', 'last_seen', 'expired_at', 'collected_at']


def text(value):
    if value is None:
        return ''
    if isinstance(value, (list, tuple)):
        return '; '.join(filter(None, (text(v) for v in value)))
    if isinstance(value, dict):
        return text(value.get('name') or value.get('text') or value.get('value'))
    # Preserve line breaks between paragraph/list elements.
    raw = str(value)
    if re.search(r'<[a-zA-Z!/][^>]*>', raw):
        raw = BeautifulSoup(raw, 'html.parser').get_text('\n', strip=True)
    return re.sub(r'[ \t\r\f\v]+', ' ', raw).strip()


def key(value):
    raw = unicodedata.normalize('NFD', text(value).lower()).replace('đ', 'd')
    return re.sub(r'[^a-z0-9]+', '', ''.join(c for c in raw if not unicodedata.combining(c)))


def pick(data, *names):
    if not isinstance(data, dict):
        return None
    # Exact aliases only: do not confuse jobLocation with jobLocationType.
    for name in names:
        if name in data and data[name] not in (None, '', [], {}):
            return data[name]
    return None


def walk(value):
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from walk(item)


def jobposting(obj):
    types = obj.get('@type', [])
    return 'JobPosting' in ([types] if isinstance(types, str) else types or [])


def meta(soup, *names):
    for name in names:
        el = soup.find('meta', attrs={'property': name}) or soup.find('meta', attrs={'name': name})
        if el and el.get('content'):
            return text(el['content'])
    return ''


def selector_text(soup, *selectors):
    for selector in selectors:
        el = soup.select_one(selector)
        if el and text(el.get_text('\n', strip=True)):
            return text(el.get_text('\n', strip=True))
    return ''


def number(value):
    # Structured JSON numbers only. Do not guess locale or units from prose.
    if isinstance(value, bool):
        return ''
    try:
        val = float(value)
        return val if math.isfinite(val) and val >= 0 else ''
    except (TypeError, ValueError):
        return ''


SKILL_ALIASES = {
    'React': ['React', 'ReactJS', 'React.js'], 'JavaScript': ['JavaScript', 'Javascript', 'JS'],
    'TypeScript': ['TypeScript'], 'Node.js': ['Node.js', 'NodeJS'],
    'Vue.js': ['Vue', 'VueJS', 'Vue.js'], 'Angular': ['Angular', 'AngularJS'],
    'Python': ['Python'], 'Java': ['Java'], 'C#': ['C#', 'C Sharp'],
    'C++': ['C++'], '.NET': ['.NET', 'dotnet'], 'ASP.NET': ['ASP.NET'],
    'PHP': ['PHP'], 'Laravel': ['Laravel'], 'Spring Boot': ['Spring Boot', 'SpringBoot'],
    'HTML': ['HTML', 'HTML5'], 'CSS': ['CSS', 'CSS3'],
    'SQL': ['SQL'], 'MySQL': ['MySQL'], 'PostgreSQL': ['PostgreSQL', 'Postgres'],
    'SQL Server': ['SQL Server', 'MSSQL'], 'MongoDB': ['MongoDB'], 'Redis': ['Redis'],
    'Git': ['Git'], 'Docker': ['Docker'], 'Kubernetes': ['Kubernetes', 'K8s'],
    'AWS': ['AWS', 'Amazon Web Services'], 'Azure': ['Azure'], 'GCP': ['GCP', 'Google Cloud'],
    'Linux': ['Linux'], 'CI/CD': ['CI/CD', 'CI CD'], 'REST API': ['REST API', 'RESTful API'],
    'Flutter': ['Flutter'], 'Dart': ['Dart'], 'Kotlin': ['Kotlin'], 'Swift': ['Swift'],
    'Selenium': ['Selenium'], 'JUnit': ['JUnit'], 'TestNG': ['TestNG'],
    'Agile': ['Agile'], 'Scrum': ['Scrum'], 'Waterfall': ['Waterfall'],
    'SAP': ['SAP'], 'SAP ABAP': ['ABAP', 'SAP ABAP'], 'SAP S/4HANA': ['SAP S/4HANA', 'S/4HANA'],
    'SAP ECC': ['SAP ECC'], 'SAP SuccessFactors': ['SAP SuccessFactors', 'SuccessFactors'],
    'ITIL': ['ITIL'], 'PMP': ['PMP'], 'PRINCE2': ['PRINCE2'], 'DevOps': ['DevOps'],
    'Machine Learning': ['Machine Learning'], 'Deep Learning': ['Deep Learning'],
    'TensorFlow': ['TensorFlow'], 'PyTorch': ['PyTorch'], 'Power BI': ['Power BI'],
    'Analytical Thinking': ['Analytical Thinking'],
}


def compact(value):
    return ' '.join(str(value or '').split())


def info_value(soup, *labels):
    wanted = {key(v) for v in labels}
    for block in soup.select('.sc-7bf5461f-2.JtIju'):
        label = block.find('label')
        if label and key(label.get_text(' ', strip=True)) in wanted:
            value = block.find('p')
            return compact(value.get_text(' ', strip=True)) if value else ''
    return ''


def job_section(soup, title):
    for heading in soup.select('h2.sc-1671001a-5.cjuZti'):
        if key(heading.get_text(' ', strip=True)) == key(title):
            content = heading.parent.select_one('.sc-1671001a-6.dVvinc')
            return content.get_text('\n', strip=True) if content else ''
    return ''


def flight_job(soup, identity):
    """Decode embedded Next.js strings as JSON, without executing JavaScript."""
    for script in soup.find_all('script'):
        raw = (script.string or script.get_text()).strip()
        match = re.fullmatch(r'self\.__next_f\.push\((.*)\);?', raw, re.S)
        if not match:
            continue
        try:
            payload = json.loads(match[1])
            stream = payload[1] if len(payload) > 1 else ''
        except (ValueError, TypeError):
            continue
        if not isinstance(stream, str):
            continue
        for line in stream.splitlines():
            record = re.match(r'^[0-9a-f]+:(\{.*\})$', line)
            if not record:
                continue
            try:
                obj = json.loads(record[1])
            except ValueError:
                continue
            if str(obj.get('jobId', '')) == identity:
                return obj
    return {}


def experience_bounds(raw):
    raw = compact(raw).lower().replace(',', '.')
    pair = re.search(r'(\d+(?:\.\d+)?)\s*(?:[-–—]|đến|to)\s*(\d+(?:\.\d+)?)', raw)
    scale = 12 if re.search(r'tháng|months?', raw) else 1
    if pair:
        lo, hi = float(pair[1])/scale, float(pair[2])/scale
        if lo > hi:
            raise ValueError('Khoảng kinh nghiệm bị đảo min/max')
        return lo, hi
    single = re.search(r'\d+(?:\.\d+)?', raw)
    if not single:
        return '', ''
    value = float(single[0])/scale
    if re.search(r'tối đa|không quá|up to|at most|dưới|less than', raw):
        return '', value
    return value, ''


def normalized_skills(listed, requirements):
    result = []
    lookup = {compact(alias).casefold(): canonical for canonical, aliases in SKILL_ALIASES.items() for alias in aliases}
    for item in re.split(r'[,;\n|]+', listed):
        item = compact(item)
        if item:
            canonical = lookup.get(compact(item).casefold(), item)
            if canonical not in result:
                result.append(canonical)
    # Dictionary matching: no invented skills; Java does not match JavaScript.
    for canonical, aliases in SKILL_ALIASES.items():
        if any(re.search(r'(?<![\w+#.])' + re.escape(alias) + r'(?![\w+#])', requirements, re.I) for alias in aliases):
            if canonical not in result:
                result.append(canonical)
    return result


def visible_salary(raw):
    """Fallback only for clear visible salary numbers and explicitly stated units."""
    currency = 'USD' if re.search(r'USD|\$', raw, re.I) else 'VND' if re.search(r'VND|VNĐ|triệu|trieu|₫', raw, re.I) else ''
    unit = next((value for pattern, value in [(r'tháng|/month|monthly', 'MONTH'), (r'/năm|/year|yearly', 'YEAR'), (r'/giờ|/hour|hourly', 'HOUR')] if re.search(pattern, raw, re.I)), '')
    numbers = re.findall(r'\d+(?:[.,]\d+)*', raw)
    def convert(v):
        if re.fullmatch(r'\d{1,3}(?:[.,]\d{3})+', v):
            n = float(re.sub(r'[.,]', '', v))
        else:
            n = float(v.replace(',', '.'))
        return n * (1_000_000 if re.search(r'triệu|trieu', raw, re.I) else 1)
    if not numbers:
        return '', '', currency, unit
    vals = [convert(v) for v in numbers]
    if len(vals) == 2:
        return vals[0], vals[1], currency, unit
    if len(vals) == 1:
        if re.search(r'up to|tối đa|đến|dưới', raw, re.I):
            return '', vals[0], currency, unit
        if re.search(r'from|từ|trên|\+', raw, re.I):
            return vals[0], '', currency, unit
        return vals[0], vals[0], currency, unit
    return '', '', currency, unit


def parse_html(html, job_id, path, link=''):
    soup = BeautifulSoup(html, 'html.parser')
    canonical = soup.select_one('link[rel="canonical"]')
    url = clean_url(canonical.get('href')) if canonical else clean_url(link)
    if not url or get_job_id({}, url) != job_id:
        raise ValueError('Canonical URL không khớp job_id đang tải')
    obj = flight_job(soup, job_id)
    ld = {}
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            candidates = list(walk(json.loads(script.string or script.get_text())))
        except (ValueError, TypeError):
            continue
        # identifier.value may be company ID; match title instead.
        for item in candidates:
            if jobposting(item) and compact(item.get('title')) == compact(selector_text(soup, 'h1.sc-ab270149-0.hAejeW')):
                ld = item
                break
    row = dict.fromkeys(COLUMNS, '')
    row.update(job_id=job_id, source='VietnamWorks', url=url)
    row['title'] = compact(selector_text(soup, 'h1.sc-ab270149-0.hAejeW'))
    row['company'] = compact(selector_text(soup, 'a.sc-ab270149-0.egZKeY.sc-f0821106-0.gWSkfE'))
    row['location'] = compact(selector_text(soup, '.sc-4ab41082-1.gVpPKv span.sc-ab270149-0.ePOHWr'))
    locations = list(dict.fromkeys(compact(e.get_text(' ', strip=True)) for e in soup.select('.sc-a137b890-1.joxJgK p')))
    row['locations_raw'] = json.dumps(locations, ensure_ascii=False)
    row['level'] = info_value(soup, 'CẤP BẬC')
    row['experience_min'], row['experience_max'] = experience_bounds(info_value(soup, 'SỐ NĂM KINH NGHIỆM TỐI THIỂU', 'KINH NGHIỆM'))
    row['salary_text'] = compact(selector_text(soup, 'span.sc-ab270149-0.cVbwLK'))
    negotiated = bool(re.search(r'thương lượng|thỏa thuận|thoả thuận|negotiable', row['salary_text'], re.I))
    warnings = []
    if not negotiated and obj.get('isSalaryVisible') is not False:
        lo, hi, currency, unit = visible_salary(row['salary_text'])
        for field, fallback in [('salary_raw_min', lo), ('salary_raw_max', hi)]:
            structured = number(obj.get('salaryMin' if field.endswith('min') else 'salaryMax'))
            row[field] = structured if structured != '' and structured > 0 else fallback
        row['currency'] = text(obj.get('salaryCurrency')) or currency
        row['salary_unit'] = unit  # Never guess salaryPeriodId or adopt contradictory JSON-LD HOUR.
        if row['salary_raw_min'] != '' and row['salary_raw_max'] != '' and row['salary_raw_min'] > row['salary_raw_max']:
            raise ValueError('Lương min lớn hơn max')
    row['description'] = job_section(soup, 'Mô tả công việc')
    requirements = job_section(soup, 'Yêu cầu công việc')
    row['requirements'] = requirements
    row['skills'] = json.dumps(normalized_skills(info_value(soup, 'KỸ NĂNG'), requirements), ensure_ascii=False)
    work = key(info_value(soup, 'LOẠI HÌNH LÀM VIỆC'))
    # User-requested mapping, including fallback for unknown/missing values.
    row['work_type'] = 'PARTTIME' if 'banthoigian' in work else 'FULLTIME'
    row['job_location_type'] = 'Remote' if 'vieclamonline' in work else 'Onsite'
    posted = info_value(soup, 'NGÀY ĐĂNG')
    try:
        row['posted_date'] = datetime.strptime(posted, '%d/%m/%Y').date().isoformat() if posted else ''
    except ValueError:
        row['posted_date'] = posted
        warnings.append('unparsed_posted_date')
    row['valid_through'] = text(obj.get('expiredOn') or ld.get('validThrough'))
    if not row['title'] or not row['description']:
        raise ValueError('Không tìm được tiêu đề/mô tả theo selector đã chốt')
    return row, warnings


def clean_url(value):
    if not isinstance(value, str) or not value.strip():
        return None
    p = urlparse(urljoin(BASE_URL, value.strip()))
    if p.scheme not in ('http', 'https') or p.hostname not in ('vietnamworks.com', 'www.vietnamworks.com'):
        return None
    if not re.search(r'-\d+-(?:jv|jd)$', p.path.rstrip('/'), re.I):
        return None
    return f'https://www.vietnamworks.com{p.path.rstrip("/")}'


def get_job_id(row, url):
    # URL ID is authoritative and safe as a filename.
    match = re.search(r'-(\d+)-(?:jv|jd)$', urlparse(url).path, re.I)
    return match.group(1) if match else None

def load_rows(path, required):
    if not path.exists():
        return [], []
    with path.open(encoding='utf-8-sig', newline='') as f:
        reader = csv.DictReader(f)
        columns = reader.fieldnames or []
        if required not in columns:
            raise ValueError(f'{path.name} thiếu cột {required}; không ghi đè.')
        return columns, list(reader)


def atomic_csv(path, columns, rows):
    temp = path.with_name(path.name + '.tmp')
    with temp.open('w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)
    temp.replace(path)


def parse_detail_html(file_path, link_info):
    identity = str(link_info['job_id']).strip().removesuffix('.0')
    row, warnings = parse_html(file_path.read_text(encoding='utf-8-sig'), identity,
                               file_path, link_info.get('url', ''))
    for col in ('status', 'first_seen', 'last_seen', 'expired_at'):
        row[col] = link_info.get(col, '')
    # Match TopCV: this is the time of successful offline extraction.
    row['collected_at'] = datetime.now(SITE_TZ).isoformat(timespec='seconds')
    for warning in warnings:
        print(f'    WARNING: {warning}')
    return row


def main():
    if not LINKS_FILE.is_file():
        raise FileNotFoundError(f'Thiếu {LINKS_FILE}; chạy bước 01 trước.')
    _, links = load_rows(LINKS_FILE, 'job_id')
    fields, old = load_rows(OUTPUT_FILE, 'job_id')
    old_map = {}
    for item in old:
        identity = item['job_id'].strip().removesuffix('.0')
        if not identity.isdigit():
            raise ValueError('CSV cũ có job_id không hợp lệ; không ghi đè.')
        old_map[identity] = {col: item.get(col, '') for col in COLUMNS}
        old_map[identity]['job_id'] = identity
    # Keep old rows even if a history CSV was truncated; never silently lose them.
    records = dict(old_map)
    parsed = kept = missing = failed = 0
    for index, item in enumerate(links, 1):
        identity = str(item.get('job_id', '')).strip().removesuffix('.0')
        if not identity.isdigit():
            raise ValueError(f'job_id không hợp lệ ở dòng {index}; không ghi đè CSV.')
        item['job_id'] = identity
        path = DETAIL_DIR / f'{identity}.html'
        print(f'[{index}/{len(links)}] {identity} {item.get("status", "")}')
        if path.is_file():
            try:
                records[identity] = parse_detail_html(path, item)
                parsed += 1
                print('    Parsed HTML')
                continue
            except Exception as exc:
                failed += 1
                print(f'    Parse error: {type(exc).__name__}: {exc}')
        old_row = old_map.get(identity)
        if old_row is not None:
            row = dict(old_row)
            for col in ('status', 'first_seen', 'last_seen', 'expired_at'):
                row[col] = item.get(col, '')
            # Preserve collected_at: old content has not been extracted anew.
            records[identity] = row
            kept += 1
            print('    Giữ raw cũ + cập nhật lifecycle')
        else:
            missing += 1
            print('    Chưa có HTML/raw hợp lệ')
    if records:
        if fields and fields != COLUMNS:
            backup = OUTPUT_FILE.with_name(OUTPUT_FILE.stem + '_before_schema_change.csv')
            if not backup.exists():
                backup.write_bytes(OUTPUT_FILE.read_bytes())
        atomic_csv(OUTPUT_FILE, COLUMNS, records.values())
    elif not old:
        atomic_csv(OUTPUT_FILE, COLUMNS, [])
    active = sum(row.get('status') == 'active' for row in records.values())
    expired = sum(row.get('status') == 'expired' for row in records.values())
    print(f'ĐÃ CẬP NHẬT VIETNAMWORKS RAW: {OUTPUT_FILE}')
    print(f'Tổng raw: {len(records)} | Active: {active} | Expired: {expired}')
    print(f'Parsed: {parsed} | Giữ cũ: {kept} | Chưa có dữ liệu: {missing} | Parse lỗi: {failed}')
    return 1 if failed else 0


if __name__ == '__main__':
    raise SystemExit(main())
