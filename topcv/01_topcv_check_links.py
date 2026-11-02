"""Step 2: read saved NEW links, verify missing links, update job history.

Place beside 01_topcv_link.py. Run: python 01_topcv_check_links.py

No listing crawl is performed here. 403/429/errors never prove expiry.

A matching scan metadata file is required; incomplete scans only update seen jobs.

"""

from pathlib import Path

from datetime import datetime, timezone

from urllib.parse import urlparse

import importlib

import hashlib

import json

import re

import time

import pandas as pd

from bs4 import BeautifulSoup

listing = importlib.import_module('01_topcv_link')

# Explicit bindings let editors and static analyzers resolve every shared name.

LINKS_FILE = listing.LINKS_FILE

REPORT_FILE = listing.REPORT_FILE

NEW_LINKS_FILE = listing.NEW_LINKS_FILE

SCAN_FILE = listing.SCAN_FILE

SITE_TZ = listing.SITE_TZ

NAVIGATION_TIMEOUT_MS = listing.NAVIGATION_TIMEOUT_MS

ACTION_TIMEOUT_MS = listing.ACTION_TIMEOUT_MS

BROWSER_TIMEOUT_MS = listing.BROWSER_TIMEOUT_MS

BLOCK_STATUS = listing.BLOCK_STATUS

BLOCK_RETRY_DELAY = listing.BLOCK_RETRY_DELAY

VERIFY_MISSING = listing.VERIFY_MISSING

VERIFY_DELAY = listing.VERIFY_DELAY

VERIFY_WAIT_SECONDS = listing.VERIFY_WAIT_SECONDS

VERIFY_ATTEMPTS = listing.VERIFY_ATTEMPTS

VERIFY_RECOVERY_RELOADS = listing.VERIFY_RECOVERY_RELOADS

VERIFY_RELOAD_DELAY = listing.VERIFY_RELOAD_DELAY

MAX_VERIFY_RETRY_WAIT = listing.MAX_VERIFY_RETRY_WAIT

EXPIRED_LANDING_TITLE = listing.EXPIRED_LANDING_TITLE

EXPIRED_LANDING_TITLES = {

    EXPIRED_LANDING_TITLE,

    'TopCV - CV xịn việc làm chất - Tạo CV & Tìm việc miễn phí',

}

TITLE_CONFIRM_WAIT_MS = listing.TITLE_CONFIRM_WAIT_MS

COLUMNS = listing.COLUMNS

now_text = listing.now_text

normalized_text = listing.normalized_text

clean_url = listing.clean_url

extract_job_id = listing.extract_job_id

blocked_page = listing.blocked_page

retry_after_seconds = listing.retry_after_seconds

wait_with_message = listing.wait_with_message

close_browser_resources = listing.close_browser_resources

deduplicate_collected_links = listing.deduplicate_collected_links

def read_csv_if_exists(path):

    if not path.is_file():

        return pd.DataFrame(columns=COLUMNS)

    try:

        frame = pd.read_csv(path, dtype=str, keep_default_na=False, encoding='utf-8-sig')

    except pd.errors.EmptyDataError:

        raise ValueError('CSV lịch sử trống bất thường; kiểm tra file trước khi chạy lại.') from None

    if 'url' not in frame.columns:

        raise ValueError('CSV lịch sử thiếu url; không ghi đè.')

    for field in COLUMNS:

        if field not in frame:

            frame[field] = ''

    return frame

def jobpostings(value):

    if isinstance(value, list):

        for child in value:

            yield from jobpostings(child)

    elif isinstance(value, dict):

        kind = value.get('@type', [])

        kinds = [kind] if isinstance(kind, str) else kind

        if 'JobPosting' in (kinds or []):

            yield value

        for child in value.values():

            if isinstance(child, (dict, list)):

                yield from jobpostings(child)

def classify_missing_detail(html, http_status, requested_url, final_url, stamp):

    """No expiry inference from absence, blocking, login redirects, or arbitrary text."""

    result = {'state': 'unknown', 'reason': 'unrecognized_detail', 'checked_at': stamp, 'http_status': http_status}

    normalized_requested = clean_url(requested_url)

    normalized_final = clean_url(final_url)

    if http_status in BLOCK_STATUS or blocked_page(html):

        result['reason'] = 'blocked_or_rate_limited'

        return result

    if not extract_job_id(normalized_requested) or not normalized_final:

        result['reason'] = 'redirect_or_job_identity_changed'

        return result

    soup = BeautifulSoup(html, 'html.parser')

    page_title = soup.title.get_text(' ', strip=True) if soup.title else ''

    login_or_challenge = re.search(r'/(?:dang-nhap|login|sign[_-]?in|auth|captcha|challenge|verify)(?:/|$)',

                                   urlparse(normalized_final).path, re.I)

    # The expiry class must be checked before an ordinary redirect returns unknown.
    expired_button = soup.select_one('.apply-expired')
    if http_status == 200 and not login_or_challenge and expired_button is not None:
        result.update(state='expired', reason='apply_expired_class',
                      evidence=str(expired_button)[:500], final_url=normalized_final,
                      page_title=page_title)
        return result

    # User-confirmed: a detail URL redirected to this exact listing landing page.

    if http_status == 200 and normalized_final == 'https://www.topcv.vn/viec-lam':

        result.update(state='expired', reason='redirect_to_viec_lam',

                      evidence=normalized_final, final_url=normalized_final,

                      page_title=page_title)

        return result

    # User-confirmed TopCV signal, including redirects to the home/search page.

    if (http_status == 200 and not login_or_challenge and

            normalized_text(page_title) in {normalized_text(title) for title in EXPIRED_LANDING_TITLES}):

        result.update(state='expired', reason='generic_topcv_landing_title',

                      evidence=page_title, final_url=normalized_final)

        return result

    if normalized_final != normalized_requested:

        result.update(reason='redirect_or_job_identity_changed',
                      final_url=normalized_final, page_title=page_title)

        return result

    if http_status in {404, 410}:

        result.update(state='expired', reason=f'confirmed_http_{http_status}')

        return result

    if http_status != 200:

        result['reason'] = f'http_{http_status}'

        return result

    canonical = soup.select_one('link[rel="canonical"]')

    if canonical and clean_url(canonical.get('href')) != normalized_requested:

        result['reason'] = 'canonical_identity_mismatch'

        return result

    expired_messages = {

        'tin tuyen dung da het han', 'tin tuyen dung nay da het han',

        'viec lam nay da het han', 'tin da het han', 'het han ung tuyen',

        'tin tuyen dung da ngung tuyen', 'tin tuyen dung nay da ngung tuyen',

        'job expired', 'this job has expired', 'this job is no longer available',

    }

    for node in soup.select('div, p, span, button, h1, h2, h3'):

        if node.find_parent(['script', 'style', 'nav', 'aside', 'footer']):

            continue

        if re.search(r'job-item|job-card|recommend|related|suggest', ' '.join(node.get('class', []))) or node.find_parent(class_=re.compile(r'job-item|job-card|recommend|related|suggest')):

            continue

        if any('hidden' in parent.attrs or parent.get('aria-hidden') == 'true' or

               re.search(r'display\s*:\s*none|visibility\s*:\s*hidden', parent.get('style', ''), re.I)

               for parent in [node, *node.parents] if getattr(parent, 'attrs', None) is not None):

            continue

        message = normalized_text(node.get_text(' ', strip=True)).strip(' .!:')

        if message in expired_messages:

            result.update(state='expired', reason='explicit_expired_notice',

                          evidence=node.get_text(' ', strip=True))

            return result

    records = []

    for script in soup.select('script[type="application/ld+json"]'):

        try:

            records.extend(jobpostings(json.loads(script.string or script.get_text())))

        except (ValueError, TypeError):

            continue

    for job in records:

        if not job.get('title'):

            continue

        # Multiple JobPostings can include recommended jobs: require an exact URL.

        if not job.get('url') and len(records) != 1:

            continue

        if job.get('url') and clean_url(job['url']) != normalized_requested:

            continue

        if not canonical and not job.get('url'):

            continue

        deadline = job.get('validThrough')

        if deadline:

            parsed = pd.to_datetime(deadline, errors='coerce')

            if pd.isna(parsed):

                result['reason'] = 'invalid_valid_through'

                return result

            if parsed.tzinfo is None:

                parsed = parsed.tz_localize(SITE_TZ)

            if re.fullmatch(r'\d{4}-\d{2}-\d{2}', str(deadline)):

                parsed += pd.Timedelta(days=1) - pd.Timedelta(microseconds=1)

            if parsed < pd.Timestamp(stamp):

                result.update(state='expired', reason='structured_valid_through_passed', evidence=str(deadline))

                return result

        result.update(state='active', reason='same_job_posting_available')

        return result

    return result

def detail_retryable(result):

    return result.get('reason') in {

        'blocked_or_rate_limited', 'detail_request_failed', 'unrecognized_detail'

    } or result.get('http_status') in {408, 500, 502, 503, 504, None}

# Initial navigation plus at most three reloads; no extra recovery cycle.

MAX_DETAIL_RELOADS = 3

DETAIL_RELOAD_PAUSE = 30

def verify_one_missing(page, url):

    trace = []

    for attempt in range(MAX_DETAIL_RELOADS + 1):

        request_kind = 'goto' if attempt == 0 else 'reload'

        # A failed initial navigation may leave about:blank: reopen only the requested URL.

        if request_kind == 'reload' and not clean_url(page.url):

            request_kind = 'goto'

        print(f'    {"Mở link" if attempt == 0 else f"Reload {attempt}/{MAX_DETAIL_RELOADS}"}: {url}', flush=True)

        stamp = now_text()

        retry_after = 0

        try:

            if request_kind == 'reload':

                response = page.reload(wait_until='domcontentloaded', timeout=NAVIGATION_TIMEOUT_MS)

            else:

                response = page.goto(url, wait_until='domcontentloaded', timeout=NAVIGATION_TIMEOUT_MS)

            status = response.status if response else None

            if response:

                retry_after = retry_after_seconds(response.headers.get('retry-after'), stamp)

            start = time.monotonic()

            title_rechecked = False

            while True:

                result = classify_missing_detail(page.content(), status, url, page.url, stamp)

                if result['reason'] in {'generic_topcv_landing_title', 'redirect_to_viec_lam', 'apply_expired_class'} and not title_rechecked:

                    print('        Gặp trang đích hết hạn; đợi 2s kiểm tra lại URL/title trước khi kết luận.', flush=True)

                    page.wait_for_timeout(TITLE_CONFIRM_WAIT_MS)

                    title_rechecked = True

                    continue

                if result['state'] != 'unknown' or result['reason'] != 'unrecognized_detail':

                    break

                if time.monotonic() - start >= VERIFY_WAIT_SECONDS:

                    break

                page.wait_for_timeout(1000)

        except Exception as exc:

            result = {'state': 'unknown', 'reason': 'detail_request_failed', 'checked_at': stamp,

                      'message': f'{type(exc).__name__}: {exc}'}

        trace.append({'attempt': attempt + 1, 'reload_number': attempt,

                      'request': request_kind, 'state': result['state'],

                      'reason': result['reason'], 'http_status': result.get('http_status'),

                      'checked_at': stamp})

        result.update(attempts=trace.copy(), retry_exhausted=False)
        if result.get('reason') == 'blocked_or_rate_limited':
            result['retry_after_seconds'] = retry_after
            return result

        print(f"        {result['state']}: {result['reason']}", flush=True)
        if result['reason'] == 'redirect_or_job_identity_changed':
            print(f"        URL cuối: {result.get('final_url', page.url)}; title: {result.get('page_title', '')}", flush=True)

        if result['state'] != 'unknown' or not detail_retryable(result):

            return result

        if attempt == MAX_DETAIL_RELOADS:

            result['retry_exhausted'] = True

            return result

        delay = max(DETAIL_RELOAD_PAUSE, retry_after)

        if delay > MAX_VERIFY_RETRY_WAIT:

            result.update(retry_deferred=True, retry_after_seconds=delay)

            print(f'    Máy chủ yêu cầu chờ {delay:g}s; dừng để thử vào lượt sau.', flush=True)

            return result

        wait_with_message(delay, f'Chờ trước khi reload {attempt + 1}/{MAX_DETAIL_RELOADS}')

    return result

def launch_detail_browser(playwright):

    options = dict(headless=False, timeout=BROWSER_TIMEOUT_MS,

                   args=['--window-size=1280,800'])

    try:

        return playwright.chromium.launch(channel='chrome', **options)

    except Exception as exc:

        message = str(exc)

        print(f'    Lỗi mở Google Chrome: {type(exc).__name__}: {message}', flush=True)

        missing_browser = any(term in message.lower() for term in (

            "executable doesn't exist", 'executable does not exist',

            'distribution', 'not found', 'cannot find'))

        if not missing_browser:

            raise

        print('    Không tìm thấy Chrome; thử Chromium của Playwright...', flush=True)

        try:

            return playwright.chromium.launch(**options)

        except Exception as fallback_exc:

            print('    Nếu thiếu trình duyệt, dùng đúng Python đang chạy file để cài: '

                  'python -m playwright install chromium', flush=True)

            raise RuntimeError(f'Chrome: {message}; Chromium: {fallback_exc}') from fallback_exc

BLOCK_REOPEN_DELAY = 10
MAX_BLOCK_REOPENS = 3
DAILY_CHECKS_FILE = LINKS_FILE.with_name('topcv_detail_daily_checks.json')


def local_day(stamp):
    parsed = pd.to_datetime(stamp, errors='coerce', utc=True)
    return None if pd.isna(parsed) else parsed.tz_convert(SITE_TZ).date().isoformat()


def load_daily_checks():
    if not DAILY_CHECKS_FILE.is_file():
        return {}
    data = json.loads(DAILY_CHECKS_FILE.read_text(encoding='utf-8'))
    if not isinstance(data, dict):
        raise ValueError('File lịch kiểm tra không hợp lệ; không tự ghi đè.')
    return data


def save_daily_check(cache, url, result):
    # Inconclusive errors/blocks may be retried; they do not count as completed.
    if result.get('state') not in {'active', 'expired'}:
        return
    cache[url] = result.copy()
    temporary = DAILY_CHECKS_FILE.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(DAILY_CHECKS_FILE)


def checked_today(row, cache):
    today = local_day(now_text())
    url = clean_url(row['url'])
    previous = cache.get(url, {})
    if previous.get('state') in {'active', 'expired'} and local_day(previous.get('checked_at')) == today:
        return {**previous, 'skipped_checked_today': True}
    # Recognize conclusive checks made by earlier script versions as well.
    reason = row.get('status_reason', '')
    detail_reasons = {'active_confirmed_by_detail', 'generic_topcv_landing_title',
                      'redirect_to_viec_lam', 'apply_expired_class',
                      'explicit_expired_notice', 'structured_valid_through_passed',
                      'confirmed_http_404', 'confirmed_http_410', 'detail_confirmed_expired'}
    if reason in detail_reasons and local_day(row.get('status_checked_at')) == today:
        state = str(row.get('status', '')).strip().lower()
        if state in {'active', 'expired'}:
            return {'state': state, 'reason': reason, 'checked_at': row['status_checked_at'],
                    'skipped_checked_today': True}
    return None


def verify_missing_links(candidates):
    results = {}
    if not candidates or not VERIFY_MISSING:
        return results
    cache = load_daily_checks()
    pending = []
    for row in candidates:
        url = clean_url(row['url'])
        cached = checked_today(row, cache)
        if cached is not None:
            results[url] = cached
        else:
            pending.append(row)
    print(f'Bỏ qua {len(results)} tin đã kiểm hôm nay; còn {len(pending)} tin cần kiểm.', flush=True)
    if not pending:
        return results
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as playwright:
            for index, row in enumerate(pending, 1):
                url = clean_url(row['url'])
                print(f'Kiểm tra tin vắng mặt {index}/{len(pending)}: {url}', flush=True)
                browser_trace = []
                for reopen in range(MAX_BLOCK_REOPENS + 1):
                    browser = context = None
                    stage = 'launch_browser'
                    try:
                        print(f'    Mở Chrome cho link này (vòng {reopen + 1})...', flush=True)
                        browser = launch_detail_browser(playwright)
                        stage = 'create_context'
                        context = browser.new_context(locale='vi-VN')
                        context.set_default_timeout(ACTION_TIMEOUT_MS)
                        context.set_default_navigation_timeout(NAVIGATION_TIMEOUT_MS)
                        stage = 'create_page'
                        page = context.new_page()
                        stage = 'verify_detail'
                        result = verify_one_missing(page, url)
                    except Exception as exc:
                        result = {'state': 'unknown', 'reason': 'detail_request_failed',
                                  'checked_at': now_text(), 'stage': stage,
                                  'message': f'{type(exc).__name__}: {exc}'}
                        print(f'    LỖI tại {stage}: {result["message"]}', flush=True)
                    finally:
                        print('    Đóng Chrome của link này...', flush=True)
                        close_browser_resources(context, browser)
                    browser_trace.append({'round': reopen + 1, 'reason': result['reason'],
                                          'attempts': result.get('attempts', [])})
                    result['browser_rounds'] = browser_trace.copy()
                    if result.get('reason') != 'blocked_or_rate_limited':
                        break
                    if reopen == MAX_BLOCK_REOPENS:
                        result['retry_exhausted'] = True
                        break
                    delay = max(BLOCK_REOPEN_DELAY, result.get('retry_after_seconds', 0))
                    if delay > MAX_VERIFY_RETRY_WAIT:
                        result.update(retry_deferred=True, retry_after_seconds=delay)
                        break
                    wait_with_message(delay, f'Bị block; chờ mở lại ĐÚNG LINK (lần {reopen + 1}/{MAX_BLOCK_REOPENS})')
                results[url] = result
                save_daily_check(cache, url, result)
                if result.get('retry_deferred') or (result['state'] == 'unknown' and detail_retryable(result)):
                    print('Dừng xác minh; giữ trạng thái cũ của link lỗi và các link chưa kiểm tra.', flush=True)
                    break
                if index < len(pending):
                    wait_with_message(VERIFY_DELAY, 'Chờ mở Chrome cho link kế tiếp')
    except KeyboardInterrupt:
        print('Dừng thủ công; giữ kết quả đã kiểm tra và trạng thái cũ của tin còn lại.', flush=True)
    except Exception as exc:
        print(f'Không thể xác minh: {type(exc).__name__}: {exc}', flush=True)
    return results


def time_key(value):

    parsed = pd.to_datetime(value, errors='coerce', utc=True)

    return parsed.value if pd.notna(parsed) else -1

def merge_history_rows(previous, row):

    if previous.get('search_scope') and row.get('search_scope') and previous['search_scope'] != row['search_scope']:

        raise ValueError('Một URL thuộc nhiều search_scope; không tự gộp lịch sử khác phạm vi.')

    def recent(item):

        return max(time_key(item.get('last_seen')), time_key(item.get('expired_at')))

    winner = dict(row if recent(row) >= recent(previous) else previous)

    firsts = [x for x in [previous.get('first_seen'), row.get('first_seen')] if x]

    valid_firsts = [x for x in firsts if time_key(x) != -1]

    if firsts:

        winner['first_seen'] = min(valid_firsts, key=time_key) if valid_firsts else firsts[0]

    lasts = [x for x in [previous.get('last_seen'), row.get('last_seen')] if x]

    if lasts:

        winner['last_seen'] = max(lasts, key=time_key)

    winner['search_scope'] = row.get('search_scope') or previous.get('search_scope') or ''

    return winner

def reconcile_links(old_df, current_jobs, complete, stamp, scope, verifications=None):

    """Compare full normalized URLs. A DOM/company ID can never merge two links."""

    verifications = {clean_url(url): check for url, check in (verifications or {}).items()}

    columns = list(dict.fromkeys(COLUMNS + list(old_df.columns)))

    history = {}

    duplicates_merged = 0

    repaired_ids = 0

    for original in old_df.to_dict('records'):

        row = dict.fromkeys(columns, '')

        row.update(original)

        url = clean_url(str(row.get('url') or ''))

        identity = extract_job_id(url) if url else None

        if not url or not identity:

            raise ValueError('Lịch sử có URL không phải trang chi tiết việc làm; không ghi đè CSV.')

        if str(row.get('job_id') or '').strip() != identity:

            repaired_ids += 1

        row.update(job_id=identity, url=url)

        if url in history:

            row = merge_history_rows(history[url], row)

            duplicates_merged += 1

        history[url] = row

    observed = {}

    for item in current_jobs:

        url = clean_url(item.get('url'))

        identity = extract_job_id(url) if url else None

        if not url or not identity:

            raise ValueError('Danh sách mới có URL không hợp lệ; không đối soát.')

        observed[url] = {'job_id': identity, 'url': url}

    counts = {'added': 0, 'expired': 0, 'reactivated': 0, 'observed': len(observed),

              'history_duplicates_merged': duplicates_merged, 'history_ids_repaired_from_url': repaired_ids,

              'missing_unverified': 0, 'verified_active': 0}

    for url, current in observed.items():

        old = history.get(url)

        if old is None:

            old = dict.fromkeys(columns, '')

            history[url] = old

            counts['added'] += 1

        elif str(old.get('status', '')).strip().lower() == 'expired':

            counts['reactivated'] += 1

        old.update(current)

        old.update(status='active', first_seen=old.get('first_seen') or stamp,

                   last_seen=stamp, expired_at='', search_scope=scope,

                   status_reason='present_in_listing', status_checked_at=stamp)

    if complete:

        for url, row in history.items():

            if url in observed or (row.get('search_scope') and row['search_scope'] != scope):

                continue

            check = verifications.get(url, {})

            state = check.get('state', 'unknown')

            if state == 'expired':

                if str(row.get('status', '')).strip().lower() != 'expired':

                    counts['expired'] += 1

                    row['expired_at'] = check.get('checked_at') or stamp

                elif not row.get('expired_at'):

                    row['expired_at'] = check.get('checked_at') or stamp

                row.update(status='expired', search_scope=scope,

                           status_reason=check.get('reason', 'detail_confirmed_expired'),

                           status_checked_at=check.get('checked_at') or stamp)

            elif state == 'active':

                if str(row.get('status', '')).strip().lower() == 'expired':

                    counts['reactivated'] += 1

                row.update(status='active', expired_at='', search_scope=scope,

                           status_reason='active_confirmed_by_detail',

                           status_checked_at=check.get('checked_at') or stamp)

                counts['verified_active'] += 1

            elif str(row.get('status', '')).strip().lower() != 'expired':

                counts['missing_unverified'] += 1

                row['status_reason'] = 'missing_from_listing_unverified'

                if check.get('checked_at'):

                    row['status_checked_at'] = check['checked_at']

    # Existing steps 02/03 store one HTML/raw row per URL-derived job_id.

    # Put current active aliases last so their deduplication keeps the latest sighting.

    ordered = sorted(history.values(), key=lambda row: (

        str(row.get('status', '')).strip().lower() == 'active',

        time_key(row.get('last_seen')), row['url']))

    return pd.DataFrame(ordered, columns=columns), counts

def save_history(frame):

    temporary = LINKS_FILE.with_name(LINKS_FILE.name + '.tmp')

    frame.to_csv(temporary, index=False, encoding='utf-8-sig')

    temporary.replace(LINKS_FILE)

def load_new_snapshot():

    if not NEW_LINKS_FILE.is_file() or not SCAN_FILE.is_file():

        raise ValueError('Chạy 01_topcv_link.py trước; cần cả CSV new và JSON scan cùng lượt.')

    meta = json.loads(SCAN_FILE.read_text(encoding='utf-8'))

    if hashlib.sha256(NEW_LINKS_FILE.read_bytes()).hexdigest() != meta.get('csv_sha256'):

        raise ValueError('CSV new không khớp JSON scan; không cập nhật lịch sử. Chạy lại bước quét.')

    if not isinstance(meta.get('crawl_complete'), bool):

        raise ValueError('Thiếu trạng thái hoàn tất của lượt quét.')

    frame = pd.read_csv(NEW_LINKS_FILE, dtype=str, keep_default_na=False, encoding='utf-8-sig')

    required = {'job_id', 'url', 'collected_at', 'search_scope'}

    if not required.issubset(frame.columns) or len(frame) != meta.get('rows'):

        raise ValueError('CSV new thiếu cột hoặc sai số dòng; không cập nhật lịch sử.')

    stamp, scope = meta.get('collected_at'), meta.get('search_scope')

    if not stamp or pd.isna(pd.to_datetime(stamp, errors='coerce', utc=True)) or not scope:

        raise ValueError('Thiếu thời gian hoặc phạm vi quét.')

    if not frame['collected_at'].eq(stamp).all() or not frame['search_scope'].eq(scope).all():

        raise ValueError('CSV new chứa nhiều lượt/phạm vi quét; không cập nhật lịch sử.')

    return deduplicate_collected_links(frame.to_dict('records')), meta

def sync_job_links():

    jobs, meta = load_new_snapshot()

    old = read_csv_if_exists(LINKS_FILE)

    stamp, scope = meta['collected_at'], meta['search_scope']

    complete = meta['crawl_complete']

    # Refuse an older scan overwriting newer sightings/verification results.

    latest_scan = max((time_key(x) for x in old.get('last_seen', [])), default=-1)

    if latest_scan > time_key(stamp):

        raise ValueError('Lượt quét new cũ hơn lịch sử hiện tại; chạy lại bước quét.')

    provisional, _ = reconcile_links(old, jobs, False, stamp, scope)

    save_history(provisional)

    observed = {clean_url(x['url']) for x in jobs}

    candidates = [row for row in provisional.to_dict('records')

                  if clean_url(row['url']) not in observed

                  and (not row.get('search_scope') or row['search_scope'] == scope)] if complete else []

    verifications = verify_missing_links(candidates)

    frame, counts = reconcile_links(old, jobs, complete, stamp, scope, verifications)

    save_history(frame)

    report = {'source': 'TopCV', 'checked_at': now_text(), 'collected_at': stamp,

              'search_scope': scope, 'crawl_complete': complete, 'counts': counts,

              'scan': meta['scan'], 'missing_candidates': len(candidates),

              'detail_verifications': verifications}

    temporary = REPORT_FILE.with_suffix('.json.tmp')

    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')

    temporary.replace(REPORT_FILE)

    print(f'DONE: {counts}')

    if not complete:

        print('INCOMPLETE: chỉ cập nhật link đã thấy; không xác minh hết hạn tin vắng.')

    print(f'History: {LINKS_FILE}')

    return frame, complete

if __name__ == '__main__':

    sync_job_links()
