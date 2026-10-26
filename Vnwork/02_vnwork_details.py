"""Download VietnamWorks HTML only; parsing belongs to 03_parse_raw.py.
Install: pip install selenium beautifulsoup4
Input: vnwork_job_links.csv (only active rows).
Output: details/<job_id>.html. No extra CSV or checkpoint.
Skip valid files downloaded today (Vietnam time); refresh on the next day.
"""
from pathlib import Path
from urllib.parse import urlparse, urljoin
from datetime import datetime, timezone, timedelta
import csv
import json
import re
import time
import random
from bs4 import BeautifulSoup
BASE_DIR = Path(__file__).resolve().parent
LINKS_FILE = BASE_DIR / 'vnwork_job_links.csv'
DETAIL_DIR = BASE_DIR / 'details'
BASE_URL = 'https://www.vietnamworks.com'
SITE_TZ = timezone(timedelta(hours=7))
HEADLESS = False
FORCE_REFRESH = False
WAIT_SECONDS = 35
MAX_RETRIES = 3
DELAY_SECONDS = 2

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


def page_has_job_details(html):
    soup = BeautifulSoup(html, 'html.parser')
    text = soup.get_text(' ', strip=True).lower()
    if any(term in text[:1500] for term in (
        'verify you are human', 'checking your browser', 'access denied',
        'captcha', 'xác minh bạn là con người',
    )):
        return False

    def has_posting(value):
        if isinstance(value, list):
            return any(has_posting(v) for v in value)
        if isinstance(value, dict):
            kind = value.get('@type', [])
            if isinstance(kind, str):
                kind = [kind]
            if 'JobPosting' in kind and (value.get('title') or value.get('description')):
                return True
            return any(has_posting(v) for v in value.values() if isinstance(v, (dict, list)))
        return False

    for script in soup.select('script[type="application/ld+json"]'):
        try:
            if has_posting(json.loads(script.get_text())):
                return True
        except (ValueError, TypeError):
            continue
    detail = soup.select_one(
        "[class*='job-detail'], [class*='jobDetail'], "
        "[class*='job-description'], [class*='jobDescription']"
    )
    return bool(soup.select_one('h1') and detail and len(text) > 400)


def keep_job_tab(driver, job_handle):
    """Only manage tabs belonging to this WebDriver session."""
    handles = list(driver.window_handles)
    if job_handle not in handles:
        raise RuntimeError('Tab crawler đã đóng; dừng thay vì mở thêm Chrome.')
    for handle in handles:
        if handle != job_handle:
            driver.switch_to.window(handle)
            driver.close()
    driver.switch_to.window(job_handle)


def safe_expand_control(element):
    if (element.get_attribute('target') or '').lower() == '_blank':
        return False
    if element.tag_name.lower() == 'a':
        # Do not follow links to other pages, even when labelled "Xem thêm".
        href = (element.get_dom_attribute('href') or '').strip().lower()
        return not href or href.startswith('#') or href.startswith('javascript:')
    return True


def expand_job_details(driver):
    from selenium.webdriver.common.by import By
    from selenium.common.exceptions import StaleElementReferenceException, ElementClickInterceptedException
    labels = {'xem thêm', 'xem đầy đủ mô tả công việc', 'xem đầy đủ', 'show more', 'read more'}
    job_handle = driver.current_window_handle
    # Scope to the actual description/requirements blocks in supplied HTML.
    selector = ('.sc-1671001a-4 button, .sc-1671001a-4 a, '
                '.sc-1671001a-4 [role="button"], '
                '[class*="job-description"] button, [class*="jobDescription"] button')
    for _ in range(5):
        clicked = False
        for element in driver.find_elements(By.CSS_SELECTOR, selector):
            try:
                if ' '.join(element.text.split()).casefold() not in labels:
                    continue
                if not element.is_displayed() or not element.is_enabled() or not safe_expand_control(element):
                    continue
                driver.execute_script('arguments[0].scrollIntoView({block:"center"})', element)
                time.sleep(0.3)
                try:
                    element.click()
                    time.sleep(1)
                finally:
                    keep_job_tab(driver, job_handle)
                clicked = True
                break
            except (StaleElementReferenceException, ElementClickInterceptedException):
                continue
        if not clicked:
            break


def wait_for_detail(driver):
    from selenium.webdriver.support.ui import WebDriverWait
    WebDriverWait(driver, WAIT_SECONDS, poll_frequency=1).until(
        lambda d: page_has_job_details(d.page_source)
    )
    time.sleep(1.5)
    expand_job_details(driver)
    return driver.page_source


def make_driver():
    from selenium import webdriver
    options = webdriver.ChromeOptions()
    if HEADLESS:
        options.add_argument('--headless=new')
    options.add_argument('--window-size=1440,1000')
    driver = webdriver.Chrome(options=options)
    try:
        driver.set_page_load_timeout(45)
        return driver
    except BaseException:
        driver.quit()
        raise

def already_downloaded_today(path):
    if FORCE_REFRESH or not path.is_file():
        return False
    try:
        downloaded_date = datetime.fromtimestamp(path.stat().st_mtime, SITE_TZ).date()
        if downloaded_date != datetime.now(SITE_TZ).date():
            return False
        return page_has_job_details(path.read_text(encoding='utf-8-sig'))
    except (OSError, UnicodeError):
        return False


def save_html(destination, html, expected_id):
    if not page_has_job_details(html):
        raise ValueError('Trang không có nội dung tuyển dụng hợp lệ')
    soup = BeautifulSoup(html, 'html.parser')
    canonical = soup.select_one('link[rel="canonical"]')
    if canonical:
        url = clean_url(canonical.get('href'))
        if not url or get_job_id({}, url) != expected_id:
            raise ValueError('Trang bị chuyển hướng sang tin khác')
    temp = destination.with_name(destination.name + '.tmp')
    temp.write_text(html, encoding='utf-8')
    temp.replace(destination)


def main():
    if not LINKS_FILE.is_file():
        raise FileNotFoundError(f'Thiếu {LINKS_FILE}; chạy bước 01 trước.')
    with LINKS_FILE.open(encoding='utf-8-sig', newline='') as f:
        reader = csv.DictReader(f)
        if not {'url', 'status'}.issubset(reader.fieldnames or []):
            raise ValueError('CSV phải có cột url và status; không tự tải tất cả.')
        links = [row for row in reader if row['status'].strip().lower() == 'active']
    DETAIL_DIR.mkdir(parents=True, exist_ok=True)
    driver = None
    job_handle = None
    seen = set()
    saved = skipped = failed = 0
    try:
        for index, row in enumerate(links, 1):
            url = clean_url(row['url'])
            identity = get_job_id(row, url) if url else None
            if not identity:
                failed += 1
                print(f'[{index}/{len(links)}] INVALID URL: {row["url"]}')
                continue
            if identity in seen:
                skipped += 1
                continue
            seen.add(identity)
            destination = DETAIL_DIR / f'{identity}.html'
            if already_downloaded_today(destination):
                skipped += 1
                print(f'[{index}/{len(links)}] SKIP {identity}: đã tải hôm nay')
                continue
            print(f'[{index}/{len(links)}] GET {identity}: {url}')
            for attempt in range(1, MAX_RETRIES + 1):
                try:
                    if driver is None:
                        driver = make_driver()
                        job_handle = driver.current_window_handle
                    keep_job_tab(driver, job_handle)
                    driver.get(url)
                    html = wait_for_detail(driver)
                    keep_job_tab(driver, job_handle)
                    save_html(destination, html, identity)
                    saved += 1
                    print(f'    SAVED {destination.name}')
                    break
                except Exception as exc:
                    print(f'    Lần {attempt}/{MAX_RETRIES}: {type(exc).__name__}: {exc}')
                    if attempt == MAX_RETRIES:
                        failed += 1
                        print('    Giữ HTML cũ nếu có; chạy lại để thử lại.')
                    else:
                        time.sleep(3 * attempt)
            time.sleep(DELAY_SECONDS + random.uniform(0, 1))
    finally:
        if driver is not None:
            driver.quit()
    print(f'DONE: saved={saved}, skipped={skipped}, failed={failed}')
    print(f'HTML: {DETAIL_DIR}; chạy 03_parse_raw.py để tạo CSV dữ liệu.')


if __name__ == '__main__':
    main()
