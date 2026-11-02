"""Step 1: collect listing pages, deduplicate after the scan, save NEW links.
Install: pip install playwright pandas beautifulsoup4
Then: python -m playwright install chromium
Run this file, then 01_topcv_check_links.py in the same folder.
Does not read or modify topcv_job_links.csv and never opens detail links.
An interrupted scan is saved with crawl_complete=false.
"""

from pathlib import Path

from datetime import datetime, timedelta, timezone

from urllib.parse import urljoin, urlparse, urlunparse, parse_qs, urlencode

import json
import hashlib

import re

import time

import unicodedata

from email.utils import parsedate_to_datetime

import pandas as pd

from bs4 import BeautifulSoup

BASE_URL = 'https://www.topcv.vn'

START_URL = BASE_URL + '/tim-viec-lam-cong-nghe-thong-tin-cr257'

BASE_DIR = Path(__file__).resolve().parent

LINKS_FILE = BASE_DIR / 'topcv_job_links.csv'

REPORT_FILE = BASE_DIR / 'topcv_link_report.json'
NEW_LINKS_FILE = BASE_DIR / 'topcv_job_links_new.csv'
SCAN_FILE = BASE_DIR / 'topcv_job_links_new_scan.json'

SITE_TZ = timezone(timedelta(hours=7))

MAX_PAGES = 100

PAGE_DELAY = 10

LOAD_SECONDS = 15

NAVIGATION_TIMEOUT_MS = 20000

ACTION_TIMEOUT_MS = 5000

BROWSER_TIMEOUT_MS = 20000

PAGE_LOAD_ATTEMPTS = 3

PAGE_RETRY_DELAY = 5

MAX_BROWSER_ROUNDS_PER_PAGE = None  # None: retry the same failed page until success/Ctrl+C.

BROWSER_RECOVERY_DELAY = 30

MAX_BROWSER_RECOVERY_DELAY = 300

JOBS_PER_PAGE = 50

TOTAL_SELECTOR = '.tcv-text-primary'

BLOCK_STATUS = {403, 419, 429}

BLOCK_RETRY_DELAY = 15

MIN_SCROLL_ROUNDS = 2

VERIFY_MISSING = True

VERIFY_DELAY = 5

VERIFY_WAIT_SECONDS = 10

VERIFY_ATTEMPTS = 3

VERIFY_RECOVERY_RELOADS = 1

VERIFY_RELOAD_DELAY = 30

MAX_VERIFY_RETRY_WAIT = 120

EXPIRED_LANDING_TITLE = ('Tìm việc làm chất lượng cao từ các nhà tuyển dụng uy tín, '

                         'tuyển dụng hiệu quả cùng TopCV.vn')

TITLE_CONFIRM_WAIT_MS = 2000

COLUMNS = ['job_id', 'url', 'status', 'first_seen', 'last_seen', 'expired_at', 'search_scope', 'status_reason', 'status_checked_at']

def now_text():

    return datetime.now(SITE_TZ).isoformat(timespec='seconds')

def normalized_text(value):

    value = unicodedata.normalize('NFKD', str(value or '').casefold()).replace('đ', 'd')

    return ' '.join(''.join(c for c in value if not unicodedata.combining(c)).split())

def clean_url(value):

    if not isinstance(value, str) or not value.strip():

        return ''

    parsed = urlparse(urljoin(BASE_URL, value.strip()))

    if parsed.scheme not in {'http', 'https'} or parsed.hostname not in {'topcv.vn', 'www.topcv.vn'}:

        return ''

    return urlunparse(('https', 'www.topcv.vn', parsed.path.rstrip('/'), '', '', ''))

def extract_job_id(url):

    # Supports /123.html and /brand/.../job-title-j123.html.

    match = re.search(r'(?:/|-j|-)(\d+)\.html$', urlparse(url).path, re.I)

    return match.group(1) if match else None

def search_scope():

    p = urlparse(START_URL)

    query = parse_qs(p.query)

    query.pop('page', None)

    return urlunparse(p._replace(query=urlencode(sorted(query.items()), doseq=True), fragment=''))

def page_url(number):

    p = urlparse(START_URL)

    query = parse_qs(p.query)

    query['page'] = [str(number)]

    return urlunparse(p._replace(query=urlencode(query, doseq=True)))

def extract_links_from_html(html):

    soup = BeautifulSoup(html, 'html.parser')

    jobs = []

    for card in soup.select('div.job-item-search-result'):

        anchor = card.select_one('h3.title a[href]')

        url = clean_url(anchor.get('href')) if anchor else ''

        job_id = extract_job_id(url) if url else None

        if not url or not job_id:

            raise ValueError('Không nhận diện được URL chi tiết việc làm; không lấy ID công ty làm job_id.')

        # URL is the identity. data-job-id is not trusted for reconciliation.

        jobs.append({'job_id': job_id, 'url': url})

    return jobs

def extract_total_jobs(html):

    """Read an unambiguous total, avoiding numbers in cards or navigation."""

    soup = BeautifulSoup(html, 'html.parser')

    candidates = []

    for node in soup.select(TOTAL_SELECTOR):

        if node.find_parent(['nav', 'aside', 'footer', 'header']):

            continue

        if node.find_parent(class_=re.compile(r'job-item|job-card|recommend|related|suggest')):

            continue

        chain = [node, *node.parents]

        if any(getattr(x, 'attrs', {}).get('aria-hidden') == 'true' or

               'hidden' in (getattr(x, 'attrs', {}) or {}) or

               'display:none' in re.sub(r'\s+', '', (getattr(x, 'attrs', {}) or {}).get('style', '')).lower()

               for x in chain):

            continue

        value = node.get_text(' ', strip=True)

        # Example: "2.698 việc làm Công nghệ Thông tin" -> 2698.

        counter = re.fullmatch(r'(\d+(?:[.,\s]\d{3})*)\s+viec lam(?:\s+.*)?', normalized_text(value))

        if counter:

            total = int(re.sub(r'[^0-9]', '', counter.group(1)))

            score = 3  # A labeled job total outranks unrelated numeric links.

        else:

            tokens = re.findall(r'\d+(?:[.,\s]\d{3})*', value)

            if len(tokens) != 1:

                continue

            # Do not interpret prices or percentages as job totals.

            remainder = normalized_text(value.replace(tokens[0], '', 1)).strip(' ()[]:.,')

            if remainder and remainder not in {'viec lam', 'cong viec', 'jobs', 'ket qua', 'viec lam phu hop'}:

                continue

            total = int(re.sub(r'[^0-9]', '', tokens[0]))

            score = 0

        parent = node

        for _ in range(3):

            if parent is None:

                break

            context = normalized_text(parent.get_text(' ', strip=True))

            if len(context) <= 400 and ('viec lam' in context or 'jobs' in context or 'ket qua' in context):

                score = max(score, 2 if any(x in context for x in ['tim thay', 'tong', 'ket qua', 'phu hop']) else 1)

            parent = parent.parent

        candidates.append((score, total))

    if not candidates:

        raise ValueError(f'Không đọc được tổng số tin từ {TOTAL_SELECTOR}.')

    best = max(score for score, _ in candidates)

    totals = {total for score, total in candidates if score == best}

    if len(totals) != 1:

        raise ValueError(f'{TOTAL_SELECTOR} có nhiều số khác nhau; cần kiểm tra đúng khối tổng tin.')

    return totals.pop()

def expected_jobs_on_page(total, number):

    return min(JOBS_PER_PAGE, max(0, total - (number - 1) * JOBS_PER_PAGE))

def blocked_page(html):

    soup = BeautifulSoup(html, 'html.parser')

    title = normalized_text(soup.title.get_text(' ', strip=True) if soup.title else '')

    return any(term in title for term in ['access denied', 'attention required', 'just a moment', 'blocked', 'too many requests'])

def scroll_listing(page, rounds):

    page.evaluate("""() => {

        window.scrollBy(0, Math.max(500, window.innerHeight * 0.8));

        const card = document.querySelector('div.job-item-search-result');

        for (let el = card && card.parentElement; el; el = el.parentElement) {

            const style = getComputedStyle(el);

            if (/auto|scroll/.test(style.overflowY) && el.scrollHeight > el.clientHeight + 100) {

                el.scrollTop += Math.max(400, el.clientHeight * 0.8);

            }

        }

    }""")

    if rounds and rounds % 7 == 0:

        page.evaluate('window.scrollTo(0, 0)')

def rendered_page_number(html):

    soup = BeautifulSoup(html, 'html.parser')

    active = soup.select_one('.pagination [aria-current="page"], .pagination .active')

    value = active.get_text(' ', strip=True) if active else ''

    return int(value) if value.isdigit() else None

def load_listing(page, number, response_meta=None):

    """Wait for card count/rendering, not for 50 globally new IDs."""

    response = page.goto(page_url(number), wait_until='domcontentloaded', timeout=NAVIGATION_TIMEOUT_MS)

    status = response.status if response else None

    if response_meta is not None:

        response_meta.update(http_status=status, retry_after_seconds=retry_after_seconds(

            response.headers.get('retry-after') if response else None, now_text()))

    if status != 200:

        return [], '', status

    start = time.monotonic()

    previous_log = None

    last_heartbeat = -5

    rounds = 0

    while True:

        html = page.content()

        if blocked_page(html):

            return [], html, 403

        jobs = extract_links_from_html(html)

        try:

            total = extract_total_jobs(html)

            expected = expected_jobs_on_page(total, number)

        except ValueError:

            expected = None

        rendered = rendered_page_number(html)

        state = (len(jobs), expected, rendered)

        elapsed = time.monotonic() - start

        if state != previous_log or elapsed - last_heartbeat >= 5:

            print(f'    page={number}, links={len(jobs)}/{expected if expected is not None else "?"}, '

                  f'trang hiển thị={rendered}, '

                  f'đã chờ={elapsed:.0f}/{LOAD_SECONDS}s', flush=True)

            previous_log = state

            last_heartbeat = elapsed

        if rounds >= MIN_SCROLL_ROUNDS and listing_ready(jobs, html, number):

            return jobs, html, status

        if elapsed >= LOAD_SECONDS:

            return jobs, html, status

        rounds += 1

        scroll_listing(page, rounds)

        page.wait_for_timeout(1000)

def listing_ready(jobs, html, number):

    try:

        total = extract_total_jobs(html)

    except ValueError:

        return False

    expected = expected_jobs_on_page(total, number)

    rendered = rendered_page_number(html)

    return expected > 0 and expected <= len(jobs) <= JOBS_PER_PAGE and rendered in {None, number}

def wait_with_message(seconds, reason):

    remaining = seconds

    while remaining > 0:

        print(f'    {reason}: còn {remaining:g}s...', flush=True)

        interval = min(5, remaining)

        time.sleep(interval)

        remaining -= interval

def close_browser_resources(context, browser):

    # One cleanup failure must not prevent closing the remaining resource.

    for name, resource in [('context', context), ('browser', browser)]:

        if resource is not None:

            try:

                resource.close()

            except Exception as exc:

                print(f'Không đóng được {name}: {type(exc).__name__}: {exc}', flush=True)

def deduplicate_collected_links(jobs):
    unique = {}
    for item in jobs:
        url = clean_url(item.get('url'))
        job_id = extract_job_id(url) if url else None
        if not job_id:
            raise ValueError('URL thu thập không hợp lệ; không đối soát.')
        unique[url] = {'url': url, 'job_id': job_id}
    return list(unique.values())

def collect_all_job_links(diagnostics=None):

    from playwright.sync_api import sync_playwright

    all_jobs = []

    complete = False

    info = {'initial_total': None, 'latest_total': None, 'expected_pages': None,

            'pages_completed': 0, 'unique_jobs': 0, 'overlap_count': 0,

            'total_changes': [], 'stop_reason': None, 'snapshot_consistent': False,

            'page_attempts': [], 'browser_recoveries': []}

    try:

        with sync_playwright() as playwright:

            for number in range(1, MAX_PAGES + 1):

                print(f'\nPAGE {number}: {page_url(number)}', flush=True)

                jobs, html, status = [], '', None

                page_ready = False

                browser_round = 0

                while not page_ready:

                    browser_round += 1

                    browser = context = None

                    error_message = ''

                    requested_pause = 0

                    try:

                        print(f'  Mở Chrome cho trang {number}, vòng {browser_round}...', flush=True)

                        browser = playwright.chromium.launch(headless=False, timeout=BROWSER_TIMEOUT_MS,

                                                             args=['--window-size=1280,800'])

                        context = browser.new_context(locale='vi-VN')

                        context.set_default_timeout(ACTION_TIMEOUT_MS)

                        context.set_default_navigation_timeout(NAVIGATION_TIMEOUT_MS)

                        page = context.new_page()

                        for attempt in range(1, PAGE_LOAD_ATTEMPTS + 1):

                            print(f'  Trang {number}, vòng Chrome {browser_round}, lượt {attempt}/{PAGE_LOAD_ATTEMPTS}: '

                                  f'mở tối đa {NAVIGATION_TIMEOUT_MS // 1000}s, chờ/cuộn tối đa {LOAD_SECONDS}s.', flush=True)

                            attempt_info = {'page': number, 'browser_round': browser_round,

                                            'attempt': attempt, 'started_at': now_text()}

                            status = None

                            meta = {}

                            try:

                                jobs, html, status = load_listing(page, number, meta)

                                requested_pause = max(0, meta.get('retry_after_seconds', 0))

                                attempt_info.update(http_status=status, links=len(jobs), retry_after_seconds=requested_pause)

                                if status == 200:

                                    all_jobs.extend(jobs)

                                page_ready = status == 200 and listing_ready(jobs, html, number)

                                if not page_ready:

                                    error_message = (f'HTTP {status}' if status != 200 else

                                                     f'trang thiếu nội dung hoặc sai số trang ({len(jobs)} link)')

                            except Exception as exc:

                                page_ready = False

                                error_message = f'{type(exc).__name__}: {exc}'

                                attempt_info['error'] = error_message

                            attempt_info.update(finished_at=now_text(), ready=page_ready)

                            info['page_attempts'].append(attempt_info)

                            if page_ready:

                                break

                            print(f'  Lỗi lượt {attempt}/{PAGE_LOAD_ATTEMPTS}: {error_message}', flush=True)

                            if attempt < PAGE_LOAD_ATTEMPTS:

                                delay = BLOCK_RETRY_DELAY if status in BLOCK_STATUS else PAGE_RETRY_DELAY

                                wait_with_message(max(delay, requested_pause), 'Chờ trước khi thử lại chính trang này')

                    except Exception as exc:

                        error_message = f'{type(exc).__name__}: {exc}'

                        print(f'  Lỗi Chrome: {error_message}', flush=True)

                    finally:

                        print(f'  Đóng Chrome của trang {number}, vòng {browser_round}...', flush=True)

                        close_browser_resources(context, browser)

                    if page_ready:

                        break

                    if MAX_BROWSER_ROUNDS_PER_PAGE is not None and browser_round >= MAX_BROWSER_ROUNDS_PER_PAGE:

                        info['stop_reason'] = (f'Trang {number} vẫn lỗi sau {browser_round} vòng Chrome '

                                               f'(tối đa {PAGE_LOAD_ATTEMPTS} lượt/vòng): {error_message}. '

                                               'Đã chạm giới hạn cấu hình; giữ các link đã lấy.')

                        break

                    # Increasing pauses reduce pressure; a longer Retry-After wins.

                    cooldown = min(MAX_BROWSER_RECOVERY_DELAY, BROWSER_RECOVERY_DELAY * 2 ** min(browser_round - 1, 12))

                    cooldown = max(cooldown, requested_pause)

                    info['browser_recoveries'].append({'page': number, 'completed_round': browser_round,

                                                       'error': error_message, 'wait_seconds': cooldown})

                    print(f'  Trang {number} vẫn lỗi. Giữ {len(all_jobs)} link đã lấy; '

                          'chờ rồi mở Chrome mới để thử LẠI TRANG NÀY. Ctrl+C để dừng và lưu.', flush=True)

                    wait_with_message(cooldown, f'Chờ mở lại Chrome cho trang {number}')

                if not page_ready:

                    break

                total = extract_total_jobs(html)

                if info['initial_total'] is None:

                    info['initial_total'] = total

                if info['latest_total'] is not None and info['latest_total'] != total:

                    info['total_changes'].append({'page': number, 'before': info['latest_total'], 'after': total})

                    print(f"Tổng tin thay đổi {info['latest_total']} -> {total}; tiếp tục thu thập.", flush=True)

                info['latest_total'] = total

                info['expected_pages'] = (total + JOBS_PER_PAGE - 1) // JOBS_PER_PAGE

                rendered = rendered_page_number(html)

                print(f'Trang {number}/{info["expected_pages"]}: {len(jobs)} link; '
                      f'tổng thu thập={len(all_jobs)} (chưa loại trùng); tổng hiển thị={total}.', flush=True)

                info['pages_completed'] += 1

                if number >= info['expected_pages']:

                    complete = True

                    info['stop_reason'] = 'Đã quét đến trang cuối theo tổng tin hiện tại.'

                    break

                if number < MAX_PAGES:

                    wait_with_message(PAGE_DELAY, 'Chờ sang trang tiếp theo')

            else:

                info['stop_reason'] = 'Chạm MAX_PAGES; quét trang chưa hoàn tất.'

    except KeyboardInterrupt:

        info['stop_reason'] = 'Dừng thủ công; lưu các link đã thấy.'

    except Exception as exc:

        info['stop_reason'] = f'{type(exc).__name__}: {exc}'

    finally:

        raw_count = len(all_jobs)
        all_jobs = deduplicate_collected_links(all_jobs)
        info['collected_rows'] = raw_count
        info['duplicates_removed'] = raw_count - len(all_jobs)
        info['overlap_count'] = info['duplicates_removed']
        info['unique_jobs'] = len(all_jobs)
        print(f'Gom xong: {raw_count} link -> {len(all_jobs)} duy nhất; '
              f'loại {info["duplicates_removed"]} bản trùng.', flush=True)

        info['crawl_complete'] = complete

        info['snapshot_consistent'] = bool(complete and not info['total_changes'] and

                                           info['overlap_count'] == 0 and len(all_jobs) == info['latest_total'])

        if diagnostics is not None:

            diagnostics.update(info)

        print(f"STOP: {info['stop_reason']}", flush=True)

        if complete:

            print('Tin vắng mặt sẽ được kiểm tra tại link chi tiết trước khi đánh expired.', flush=True)

        else:

            print('INCOMPLETE: lưu các link đã thấy, giữ trạng thái tin vắng mặt.', flush=True)

    return all_jobs, complete

def retry_after_seconds(value, stamp):

    if not value:

        return 0

    value = str(value).strip()

    if value.isdigit():

        return int(value)

    try:

        deadline = parsedate_to_datetime(value)

        if deadline.tzinfo is None:

            deadline = deadline.replace(tzinfo=timezone.utc)

        return max(0, (deadline - datetime.fromisoformat(stamp)).total_seconds())

    except (TypeError, ValueError, OverflowError):

        return 0

def save_new_links():
    """Save this scan only; history is updated by 01_topcv_check_links.py."""
    scan = {}
    jobs, complete = collect_all_job_links(scan)
    stamp = now_text()
    scope = search_scope()
    frame = pd.DataFrame(jobs, columns=['job_id', 'url'])
    frame['collected_at'] = stamp
    frame['search_scope'] = scope
    temporary = NEW_LINKS_FILE.with_suffix('.csv.tmp')
    frame.to_csv(temporary, index=False, encoding='utf-8-sig')
    digest = hashlib.sha256(temporary.read_bytes()).hexdigest()
    temporary.replace(NEW_LINKS_FILE)
    metadata = {'source': 'TopCV', 'collected_at': stamp, 'search_scope': scope,
                'crawl_complete': complete, 'rows': len(frame),
                'csv_sha256': digest, 'scan': scan}
    temporary = SCAN_FILE.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(SCAN_FILE)
    print(f'Saved: {NEW_LINKS_FILE} ({len(frame)} link duy nhất)')
    print('Chạy python 01_topcv_check_links.py để đối chiếu và cập nhật lịch sử.')
    return frame, complete

if __name__ == '__main__':
    save_new_links()
