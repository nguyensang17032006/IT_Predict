"""VietnamWorks crawler. Install: pip install selenium pandas beautifulsoup4
Clicks .sc-dYOqWG.HmCra and reads the count beside the IT category: exact total -> ceil(total/50), exact final page target.
100+ is a lower bound, not 100 jobs: use ul.pagination li.active and > instead.
Scroll max 15s per page. Advance as soon as target reached, including short
final page with exact total. Click actual >, avoiding stripped page query URLs.
Reconcile missing IDs as expired only after completion checks. Preserve old
history on failure. Keep the same START_URL filters for the same history CSV.
Approximate totals cannot detect silent omissions of entire 50-job pages.
"""
from __future__ import annotations
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse
import pandas as pd
from bs4 import BeautifulSoup

BASE_URL = 'https://www.vietnamworks.com'
START_URL = BASE_URL + '/viec-lam?g=5'
BASE_DIR = Path(__file__).resolve().parent
LINKS_FILE = BASE_DIR / 'vnwork_job_links.csv'
MAX_PAGES = 1000
JOBS_PER_PAGE = 50
WAIT_SECONDS = 15
PAGE_DELAY = 1
SITE_TZ = timezone(timedelta(hours=7))
CARD_SELECTOR = 'div.search_list.view_job_item, .new-job-card, a.img_job_card[href]'
COLUMNS = ['job_id', 'url', 'status', 'first_seen', 'last_seen', 'expired_at',
           'status_reason', 'status_checked_at', 'search_scope']


def timestamp():
    return datetime.now(SITE_TZ).isoformat(timespec='seconds')


def search_scope():
    p = urlparse(START_URL)
    q = parse_qs(p.query)
    q.pop('page', None)
    return urlunparse(p._replace(query=urlencode(sorted(q.items()), doseq=True), fragment=''))


def page_url(number):
    p = urlparse(START_URL)
    q = parse_qs(p.query)
    q['page'] = [str(number)]
    return urlunparse(p._replace(query=urlencode(q, doseq=True)))


def normalize_url(href):
    if not isinstance(href, str) or not href.strip():
        return None
    p = urlparse(urljoin(BASE_URL, href.strip()))
    if p.scheme not in ('https', 'http') or p.hostname not in ('www.vietnamworks.com', 'vietnamworks.com'):
        return None
    path = p.path.rstrip('/')
    if not re.search(r'-\d+-(?:jv|jd)$', path, re.I):
        return None
    return urlunparse(('https', 'www.vietnamworks.com', path, '', '', ''))


def job_id(url):
    m = re.search(r'-(\d+)-(?:jv|jd)$', urlparse(url).path, re.I)
    return m[1] if m else ''


def extract(html):
    soup = BeautifulSoup(html, 'html.parser')
    links = {}
    # Restrict to job cards: page-wide recommended links are not search results.
    for card in soup.select(CARD_SELECTOR):
        if card.has_attr('hidden') or card.get('aria-hidden') == 'true':
            continue
        anchors = [card] if card.name == 'a' else card.select('a[href]')
        for anchor in anchors:
            url = normalize_url(anchor.get('href'))
            if url:
                links.setdefault(job_id(url), url)
    return set(links.values())


def make_driver():
    from selenium import webdriver
    options = webdriver.ChromeOptions()
    options.add_argument('--window-size=1500,1050')
    options.add_argument('--disable-notifications')
    options.page_load_strategy = 'eager'
    driver = webdriver.Chrome(options=options)
    driver.set_page_load_timeout(30)
    return driver


def scroll_incrementally(driver):
    driver.execute_script('''
        window.scrollBy(0, Math.max(450, window.innerHeight * .7));
        for (const el of document.querySelectorAll('main,section,div')) {
            if (el.clientHeight > 220 && el.scrollHeight > el.clientHeight + 250 && /auto|scroll/.test(getComputedStyle(el).overflowY))
                el.scrollTop += Math.max(350, el.clientHeight * .7);
        }
    ''')


def open_job_total(driver):
    """Open the control supplied by the user before reading the result total."""
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.common.exceptions import StaleElementReferenceException

    def visible_trigger(d):
        for element in d.find_elements(By.CSS_SELECTOR, '.sc-dYOqWG.HmCra'):
            try:
                if element.is_displayed() and element.is_enabled():
                    return element
            except StaleElementReferenceException:
                continue
        return False

    trigger = WebDriverWait(driver, WAIT_SECONDS).until(visible_trigger)
    driver.execute_script('arguments[0].scrollIntoView({block:"center"})', trigger)
    trigger.click()

    def visible_total(d):
        for label in d.find_elements(By.CSS_SELECTOR, '.sc-bcSKrn.cwECVW'):
            try:
                if label.is_displayed() and ' '.join(label.text.split()) == 'Công Nghệ Thông Tin/Viễn Thông':
                    parent = label.find_element(By.XPATH, '..')
                    for count in parent.find_elements(By.CSS_SELECTOR, '.sc-huFNyZ.pJKTG'):
                        if count.is_displayed() and count.text.strip():
                            return count.text.strip()
            except StaleElementReferenceException:
                continue
        return False

    raw = WebDriverWait(driver, WAIT_SECONDS).until(visible_total)
    # Close the dropdown with the same trigger; do not change selected filters.
    trigger.click()
    print(f'Tong tin Cong Nghe Thong Tin/Vien Thong: {raw}')
    return parse_total(raw)


def parse_total(raw):
    raw = raw.strip()
    if raw.startswith('(') and raw.endswith(')'):
        raw = raw[1:-1].strip()
    if not re.fullmatch(r'(?:\d+|\d{1,3}(?:[.,\s]\d{3})+)', raw):
        return raw, None
    return raw, int(re.sub(r'\D', '', raw))


def read_total(html):
    """Read the count beside the exact IT/Telecommunications category only."""
    soup = BeautifulSoup(html, 'html.parser')
    labels = set()
    for label in soup.select('.sc-bcSKrn.cwECVW'):
        if label.get_text(' ', strip=True) == 'Công Nghệ Thông Tin/Viễn Thông':
            count = label.parent.select_one('.sc-huFNyZ.pJKTG')
            if count:
                labels.add(count.get_text(' ', strip=True))
    return parse_total(labels.pop()) if len(labels) == 1 else ('', None)


def read_pagination(html):
    soup = BeautifulSoup(html, 'html.parser')
    results = []
    for pager in soup.select('ul.pagination'):
        current = pager.select_one('li.active button, li.active a, [aria-current="page"]')
        label = current.get_text(' ', strip=True) if current else ''
        if not label.isdigit():
            continue
        numbers = [int(el.get_text(strip=True)) for el in pager.select('button,a') if el.get_text(strip=True).isdigit()]
        forward = [el for el in pager.select('button,a') if el.get_text(strip=True) in ('>', '›', '→') or el.get('rel') == ['next']]
        def disabled(el):
            return any(n and (n.has_attr('disabled') or n.get('aria-disabled') == 'true' or 'disabled' in n.get('class', [])) for n in (el, el.parent))
        has_next = any(not disabled(el) for el in forward)
        # Actual supplied markup uses literal > and li.active.
        last = (bool(forward) and all(disabled(el) for el in forward)) or (
            not forward and len(set(numbers)) >= 2 and int(label) == max(numbers)
            and not any('…' in el.get_text() or '...' in el.get_text() for el in pager.select('li')))
        results.append((int(label), has_next, last))
    if len(set(results)) == 1:
        current, has_next, last = results[0]
        return dict(current=current, has_next=has_next, last=last)
    return dict(current=None, has_next=False, last=False)


def expected_links(total, page):
    return min(JOBS_PER_PAGE, max(0, total - (page - 1) * JOBS_PER_PAGE))


def final_decision(total, initial_raw, latest_html, page, page_count, observed_count, checked_total=None):
    raw, latest_total = checked_total if checked_total is not None else read_total(latest_html)
    if total is not None:
        pages = (total + JOBS_PER_PAGE - 1) // JOBS_PER_PAGE
        return (total > 0 and page == pages and page_count == expected_links(total, page)
                and observed_count == total and latest_total == total)
    pager = read_pagination(latest_html)
    return (raw == initial_raw and latest_total is None and page_count > 0
            and pager['current'] == page and pager['last'] and not pager['has_next'])


def go_next(driver, expected_page, previous):
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    for el in driver.find_elements(By.CSS_SELECTOR, 'ul.pagination button, ul.pagination a'):
        if el.text.strip() in ('>', '›', '→') and el.is_displayed() and el.is_enabled():
            if el.get_attribute('aria-disabled') == 'true':
                continue
            driver.execute_script('arguments[0].scrollIntoView({block:"center"})', el)
            el.click()
            def changed(d):
                html = d.page_source
                links = extract(html)
                return read_pagination(html)['current'] == expected_page and bool(links) and links != previous
            WebDriverWait(driver, WAIT_SECONDS).until(changed)
            driver.execute_script('window.scrollTo(0,0)')
            return
    raise RuntimeError('Khong tim thay nut > de chuyen trang; khong doi soat het han.')


def collect_page(driver, page, target=JOBS_PER_PAGE):
    # The 15-second budget starts after navigation returns.
    start = time.monotonic()
    seen = {}
    previous_count = -1
    while True:
        for url in extract(driver.page_source):
            seen.setdefault(job_id(url), url)
        if len(seen) != previous_count:
            print(f'    page={page}, links={len(seen)}')
            previous_count = len(seen)
        if len(seen) >= target:
            return set(seen.values())
        remaining = WAIT_SECONDS - (time.monotonic() - start)
        if remaining <= 0:
            return set(seen.values())
        scroll_incrementally(driver)
        time.sleep(min(1, remaining))


def read_history():
    if not LINKS_FILE.exists():
        return pd.DataFrame(columns=COLUMNS)
    df = pd.read_csv(LINKS_FILE, dtype=str, keep_default_na=False, encoding='utf-8-sig')
    if 'url' not in df.columns:
        raise ValueError('CSV cu thieu cot url; khong ghi de.')
    for col in COLUMNS:
        if col not in df.columns:
            df[col] = ''
    return df


def atomic_csv(df, path):
    tmp = path.with_name(path.name + '.tmp')
    df.to_csv(tmp, index=False, encoding='utf-8-sig')
    tmp.replace(path)


def reconcile(df, observed, complete, stamp, scope):
    """Pure reconciliation; incomplete runs may update seen jobs only."""
    columns = list(dict.fromkeys(COLUMNS + list(df.columns)))
    rows = {}
    for old in df.to_dict('records'):
        normalized = normalize_url(old.get('url'))
        identity = job_id(normalized) if normalized else old.get('job_id') or old['url']
        row = dict.fromkeys(columns, '')
        row.update(old)
        if normalized:
            row['url'], row['job_id'] = normalized, identity
        rows[identity] = row
    added = expired = reactivated = 0
    observed_ids = {job_id(url) for url in observed}
    for url in sorted(observed):
        identity = job_id(url)
        if identity not in rows:
            rows[identity] = dict.fromkeys(columns, '')
            added += 1
        row = rows[identity]
        reactivated += row.get('status') == 'expired'
        row.update(job_id=identity, url=url, status='active',
                   first_seen=row.get('first_seen') or stamp, last_seen=stamp,
                   expired_at='', status_reason='present_in_search',
                   status_checked_at=stamp, search_scope=scope)
    if complete:
        for identity, row in rows.items():
            # Legacy CSV with no search_scope is assumed to belong to START_URL,
            # matching the supplied old crawler. Do not reuse another search's CSV.
            same_scope = not row.get('search_scope') or row['search_scope'] == scope
            if identity not in observed_ids and same_scope and normalize_url(row.get('url')):
                if row.get('status') != 'expired':
                    expired += 1
                    row['expired_at'] = stamp
                row.update(status='expired', status_reason='absent_from_complete_search',
                           status_checked_at=stamp, search_scope=scope)
                # Never change last_seen for a job absent from this run.
    result = pd.DataFrame(rows.values(), columns=columns).fillna('')
    queue = result[result['job_id'].isin(observed_ids) & (result['status'] == 'active')].copy()
    return result, queue, dict(added=added, expired=expired, reactivated=reactivated)


def save_snapshot(observed, complete=False):
    result, queue, counts = reconcile(read_history(), observed, complete, timestamp(), search_scope())
    atomic_csv(result, LINKS_FILE)
    return counts


def main(run_attempt=1):
    from selenium.webdriver.support.ui import WebDriverWait
    print('Start page 1. Read total jobs; use real > pagination button.')
    read_history()
    driver = make_driver()
    observed, fingerprints = set(), set()
    complete = False
    retry_scan = False
    id_pages = {}
    try:
        driver.get(START_URL)
        initial_raw, total = open_job_total(driver)
        if total == 0:
            print('Tong bang 0: can kiem tra lai, khong tu danh dau het han toan bo.')
            return
        print(f'Tong hien thi: {initial_raw}')
        if total is not None:
            print(f'Tong chinh xac: {total}; so trang: {(total + JOBS_PER_PAGE - 1) // JOBS_PER_PAGE}')
        else:
            print('So dang 100+ khong phai tong chinh xac; dung phan trang >.')
        for page in range(1, MAX_PAGES + 1):
            target = expected_links(total, page) if total is not None else JOBS_PER_PAGE
            if target <= 0:
                break
            print(f'\nPAGE {page}, can lay: {target}')
            links = collect_page(driver, page, target)
            fingerprint = tuple(sorted(job_id(url) for url in links))
            if not links or fingerprint in fingerprints:
                print('STOP: trang rong/lap. Khong danh dau het han.')
                break
            pager = read_pagination(driver.page_source)
            if pager['current'] is not None and pager['current'] != page:
                print('STOP: sai trang dang hien thi. Khong danh dau het han.')
                break
            for url in links:
                identity = job_id(url)
                if identity in id_pages and id_pages[identity] != page:
                    print(f'    DUPLICATE ID={identity}: trang {id_pages[identity]} va {page}: {url}')
                else:
                    id_pages[identity] = page
            fingerprints.add(fingerprint)
            observed.update(links)
            save_snapshot(observed, complete=False)
            latest_raw, latest_total = open_job_total(driver)
            if latest_total != total or latest_raw != initial_raw:
                print('STOP: tong so tin thay doi trong luc crawl. Chay lai de doi soat.')
                break
            if final_decision(total, initial_raw, driver.page_source, page, len(links), len({job_id(u) for u in observed}), checked_total=(latest_raw, latest_total)):
                complete = True
                break
            if total is not None and page >= (total + JOBS_PER_PAGE - 1) // JOBS_PER_PAGE:
                unique_count = len({job_id(u) for u in observed})
                print(f'STOP: da den trang cuoi {page}; trang nay {len(links)}/{target} link; '
                      f'tong ID duy nhat {unique_count}/{total}.')
                retry_scan = True
                print('Chua dat dieu kien doi soat (co the trung tin giua cac trang/ket qua thay doi). '
                      'Khong bam sang trang tiep va khong danh dau het han.')
                break
            if len(links) != target:
                retry_scan = True
                print('STOP: chua lay du so tin can thiet/khong xac nhan trang cuoi. Khong doi soat het han.')
                break
            if page == MAX_PAGES:
                print('STOP: cham MAX_PAGES. Khong doi soat het han.')
                break
            time.sleep(PAGE_DELAY)
            go_next(driver, page + 1, extract(driver.page_source))
        if complete:
            print('COMPLETE - doi soat cu/moi:', save_snapshot(observed, complete=True))
        else:
            print('INCOMPLETE - chi luu link da thay, khong danh dau het han link vang mat.')
    except KeyboardInterrupt:
        print('Da dung thu cong. Khong danh dau het han.')
    except Exception as exc:
        print(f'ERROR: {type(exc).__name__}: {exc}')
        print('Khong danh dau het han link chua thay.')
    finally:
        driver.quit()
    print(f'History: {LINKS_FILE}')
    if retry_scan and run_attempt < 2:
        print('Quet lai tu trang 1 mot lan de xu ly tin trung/ket qua dich chuyen.')
        return main(run_attempt + 1)
    if retry_scan:
        print('Van lech tong sau 2 luot: xem DUPLICATE ID; khong tu bo qua chenh lech.')


if __name__ == '__main__':
    main()
