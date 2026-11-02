"""Download ITviec HTML after email/password form sign-in in Selenium Chrome.
Install: pip install selenium beautifulsoup4
Run after 01_itviec_link.py. Only active rows are downloaded.
Reads ITVIEC_EMAIL / ITVIEC_PASSWORD, or prompts for a hidden password.
Visits active URLs in the same session after successful form submission.
No credentials/cookies are exported. No checkpoint or extra CSV.
"""
from pathlib import Path
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse, urljoin, urlunparse
import os
import getpass
import csv
import json
import re
import time
from bs4 import BeautifulSoup

BASE_DIR = Path(__file__).resolve().parent
INPUT_FILE = BASE_DIR / 'itviet_job_links.csv'
DETAIL_FOLDER = BASE_DIR / 'details'
LOGIN_URL = 'https://itviec.com/sign_in'
SITE_TZ = timezone(timedelta(hours=7))
FORCE_REFRESH = False
WAIT_SECONDS = 35
MAX_RETRIES = 3
REQUEST_DELAY = 2


class LoginRequired(RuntimeError):
    pass


def normalize_url(value):
    p = urlparse(urljoin('https://itviec.com', str(value or '').strip()))
    if p.scheme not in ('http', 'https') or p.hostname not in ('itviec.com', 'www.itviec.com'):
        return None
    if not p.path.startswith('/it-jobs/'):
        return None
    return urlunparse(('https', 'itviec.com', p.path.rstrip('/'), '', '', ''))


def salary_login_required(html):
    soup = BeautifulSoup(html, 'html.parser')
    return soup.select_one('.job-header-info .sign-in-view-salary') is not None


def contains_jobposting(html):
    soup = BeautifulSoup(html, 'html.parser')
    def has_job(value):
        if isinstance(value, list):
            return any(has_job(v) for v in value)
        if isinstance(value, dict):
            kind = value.get('@type', [])
            kind = [kind] if isinstance(kind, str) else kind
            if 'JobPosting' in (kind or []) and value.get('title'):
                return True
            return any(has_job(v) for v in value.values() if isinstance(v, (dict, list)))
        return False
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            if has_job(json.loads(script.string or script.get_text())):
                return True
        except (ValueError, TypeError):
            continue
    return False


def skip_today(path):
    if FORCE_REFRESH or not path.is_file():
        return False
    try:
        if datetime.fromtimestamp(path.stat().st_mtime, SITE_TZ).date() != datetime.now(SITE_TZ).date():
            return False
        html = path.read_text(encoding='utf-8-sig')
        # Refresh files captured before login, even when saved today.
        return contains_jobposting(html) and not salary_login_required(html)
    except (OSError, UnicodeError):
        return False


def make_driver():
    from selenium import webdriver
    options = webdriver.ChromeOptions()
    options.add_argument('--window-size=1440,1000')
    driver = webdriver.Chrome(options=options)
    try:
        driver.set_page_load_timeout(45)
        return driver
    except BaseException:
        driver.quit()
        raise


def sign_in(driver):
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.common.exceptions import TimeoutException
    email = os.environ.get('ITVIEC_EMAIL') or 'khanhdz1703@gmail.com'
    password = os.environ.get('ITVIEC_PASSWORD') or getpass.getpass('Mật khẩu ITviec (nhập ẩn): ')
    if not password:
        raise LoginRequired('Chưa nhập mật khẩu ITviec.')
    driver.get(LOGIN_URL)
    wait = WebDriverWait(driver, WAIT_SECONDS)
    try:
        email_input = wait.until(EC.visibility_of_element_located((By.NAME, 'user[email]')))
        password_input = wait.until(EC.visibility_of_element_located((By.NAME, 'user[password]')))
        email_input.clear()
        email_input.send_keys(email)
        password_input.clear()
        password_input.send_keys(password)
        del password
        # Submit only the form containing the password field.
        form = password_input.find_element(By.XPATH, './ancestor::form[1]')
        def submit_control(_):
            for button in form.find_elements(By.CSS_SELECTOR, 'button[type="submit"], input[type="submit"]'):
                if button.is_displayed() and button.is_enabled():
                    return button
            return False
        wait.until(submit_control).click()
        def signed_in(d):
            p = urlparse(d.current_url)
            if p.hostname not in ('itviec.com', 'www.itviec.com'):
                return False
            if re.search(r'/sign_in|/sign_up|/auth/', p.path):
                return False
            # A redirect is required; a blank password field alone is not proof.
            return not any(el.is_displayed() for el in d.find_elements(By.NAME, 'user[password]'))
        wait.until(signed_in)
    except TimeoutException:
        raise LoginRequired('Không xác nhận được đăng nhập. Kiểm tra thông báo trong Chrome, tài khoản hoặc yêu cầu xác minh.') from None
    print('Đã gửi form và chuyển khỏi trang đăng nhập; bắt đầu tải từng tin.')
    return driver.current_window_handle


def keep_job_tab(driver, handle):
    if handle not in driver.window_handles:
        raise RuntimeError('Tab ITviec đã bị đóng; dừng script.')
    for other in list(driver.window_handles):
        if other != handle:
            driver.switch_to.window(other)
            driver.close()
    driver.switch_to.window(handle)


def get_html(driver, url):
    from selenium.webdriver.support.ui import WebDriverWait
    driver.get(url)
    if '/sign_in' in urlparse(driver.current_url).path:
        raise LoginRequired('Phiên ITviec chưa đăng nhập hoặc đã hết hạn.')
    WebDriverWait(driver, WAIT_SECONDS, poll_frequency=1).until(
        lambda d: contains_jobposting(d.page_source)
    )
    time.sleep(1.5)
    html = driver.page_source
    if salary_login_required(html):
        raise LoginRequired('Trang vẫn hiện Sign in to view salary; dừng để tránh lưu hàng loạt HTML chưa đăng nhập.')
    soup = BeautifulSoup(html, 'html.parser')
    canonical = soup.select_one('link[rel="canonical"]')
    if canonical and normalize_url(canonical.get('href')) != url:
        raise ValueError('Canonical không khớp tin đang tải; giữ file cũ.')
    return html


def main():
    if not INPUT_FILE.is_file():
        raise FileNotFoundError(f'Thiếu {INPUT_FILE}; chạy bước 01 trước.')
    with INPUT_FILE.open(encoding='utf-8-sig', newline='') as f:
        reader = csv.DictReader(f)
        if not {'job_id', 'url', 'status'}.issubset(reader.fieldnames or []):
            raise ValueError('CSV cần có job_id, url, status.')
        rows = [r for r in reader if r['status'].strip().lower() == 'active']
    DETAIL_FOLDER.mkdir(parents=True, exist_ok=True)
    driver = None
    handle = None
    seen = set()
    saved = skipped = failed = 0
    try:
        for index, row in enumerate(rows, 1):
            identity = row['job_id'].strip()
            url = normalize_url(row['url'])
            if not re.fullmatch(r'[A-Za-z0-9_-]+', identity) or not url:
                failed += 1
                print(f'[{index}/{len(rows)}] Invalid ID/URL: {identity}')
                continue
            if identity in seen:
                skipped += 1
                continue
            seen.add(identity)
            destination = DETAIL_FOLDER / f'{identity}.html'
            if skip_today(destination):
                skipped += 1
                print(f'[{index}/{len(rows)}] SKIP {identity}: đã tải hôm nay')
                continue
            if driver is None:
                driver = make_driver()
                handle = sign_in(driver)
            print(f'[{index}/{len(rows)}] GET {identity}: {url}')
            for attempt in range(1, MAX_RETRIES + 1):
                try:
                    keep_job_tab(driver, handle)
                    html = get_html(driver, url)
                    temp = destination.with_name(destination.name + '.tmp')
                    temp.write_text(html, encoding='utf-8')
                    temp.replace(destination)
                    saved += 1
                    print(f'    SAVED {destination.name}')
                    break
                except LoginRequired:
                    raise
                except Exception as exc:
                    print(f'    Attempt {attempt}/{MAX_RETRIES}: {type(exc).__name__}: {exc}')
                    if attempt == MAX_RETRIES:
                        failed += 1
                    else:
                        time.sleep(3 * attempt)
            time.sleep(REQUEST_DELAY)
    except LoginRequired as exc:
        print(f'STOP LOGIN: {exc}')
    except KeyboardInterrupt:
        print('Đã dừng; những HTML tải thành công được giữ lại.')
    finally:
        if driver is not None:
            driver.quit()
    print(f'DONE: saved={saved}, skipped={skipped}, failed={failed}')
    print(f'HTML: {DETAIL_FOLDER}. Chạy bước 03 để cập nhật raw.')


if __name__ == '__main__':
    main()
