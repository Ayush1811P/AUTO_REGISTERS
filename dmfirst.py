"""Registration QA runner for websites you own or are authorized to test."""
import argparse
import csv
import json
import random
import re
import string
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent


def reversed_phone_password(phone_number):
    return random.choice(string.ascii_lowercase) + str(phone_number)[::-1] + random.choice(string.ascii_uppercase)


class FormError(RuntimeError):
    pass


def host(url):
    p = urlparse(url)
    if p.scheme not in ('http', 'https') or not p.hostname or p.username or p.password:
        raise ValueError('Use a complete http:// or https:// URL without embedded credentials.')
    return p.hostname.lower()


def metadata(el):
    return el.evaluate('''e => [e.type, e.name, e.id, e.placeholder,
        e.getAttribute('aria-label'), e.autocomplete,
        ...Array.from(e.labels || []).map(l => l.innerText)].filter(Boolean).join(' ')''')


def visible_candidates(page, selector):
    found = []
    for frame in page.frames:
        for el in frame.locator(selector).all():
            if el.is_visible() and el.is_enabled():
                found.append(el)
    return found


def explicit(page, selector):
    matches = visible_candidates(page, selector)
    if len(matches) != 1:
        raise FormError(f'Configured selector must match exactly one visible element: {selector}')
    return matches[0]


def challenge_present(page):
    for frame in page.frames:
        if re.search(r'recaptcha|hcaptcha|challenges\.cloudflare', frame.url, re.I):
            # Empty hidden challenge frames need not block; visible widgets do.
            element = frame.frame_element() if frame != page.main_frame else None
            if element and element.is_visible():
                return True
        for el in frame.locator('input, [class*="captcha"], [id*="captcha"], [name*="captcha"]').all():
            if el.is_visible() and re.search(r'captcha|\botp\b|one.time|verification.?code', metadata(el), re.I):
                return True
    return False


def choose_phone(page, selectors):
    if selectors.get('phone'):
        return explicit(page, selectors['phone'])
    ranked = []
    for el in visible_candidates(page, 'input:not([type="hidden"]):not([type="password"]):not([type="checkbox"]):not([type="radio"]):not([type="submit"]):not([type="button"])'):
        m = metadata(el).lower()
        score = 0
        if re.search(r'phone|mobile|\bmob\b|telephone|\btel\b|contact.?number', m):
            score = 10
        if el.get_attribute('type') == 'tel':
            score += 5
        if re.search(r'country|dial|otp|code|email', m):
            score -= 20
        if score > 0:
            ranked.append((score, el))
    ranked.sort(key=lambda pair: pair[0], reverse=True)
    if not ranked or (len(ranked) > 1 and ranked[0][0] == ranked[1][0]):
        raise FormError('Cannot identify a unique phone field. Set selectors.phone in config.json.')
    return ranked[0][1]


def choose_agreement(page, selectors, phone):
    if selectors.get('agreement'):
        return [explicit(page, selectors['agreement'])]
    candidates = visible_candidates(page, 'input[type="checkbox"], [role="checkbox"]')
    matches = []
    for el in candidates:
        text = el.evaluate('''e => [e.getAttribute('aria-label'),
          ...Array.from(e.labels || []).map(l=>l.innerText),
          e.parentElement ? e.parentElement.innerText : ''].join(' ')''')
        if re.search(r'agree|terms|conditions|user.?agreement', text, re.I):
            matches.append(el)
    if not matches:
        raise FormError('No recognizable user-agreement checkbox. Set selectors.agreement.')
    return matches


def choose_submit(page, selectors, phone):
    if selectors.get('submit'):
        return explicit(page, selectors['submit'])
    form = phone.locator('xpath=ancestor::form[1]')
    scope = form if form.count() else phone.page
    candidates = scope.locator('button, input[type="submit"], [role="button"]').all()
    matches = []
    for el in candidates:
        if el.is_visible() and el.is_enabled():
            text = (el.inner_text() + ' ' + (el.get_attribute('value') or '')).strip()
            if re.search(r'\bregister\b|sign\s*up|create\s*(an\s*)?account|join\s*now', text, re.I):
                matches.append(el)
    if len(matches) != 1:
        raise FormError('Cannot identify a unique registration button. Set selectors.submit.')
    return matches[0]


def register(page, url, phone_number, config):
    allowed = {h.lower() for h in config['allowed_hosts']}
    if host(url) not in allowed:
        raise FormError('URL host is not in configured authorized hosts.')
    # Block navigations to unapproved hosts, while permitting static assets.
    def guard(route):
        req = route.request
        if req.is_navigation_request() and host(req.url) not in allowed:
            route.abort()
        else:
            route.continue_()
    page.route('**/*', guard)
    page.goto(url, wait_until='domcontentloaded', timeout=config.get('timeout_ms', 15000))
    if host(page.url) not in allowed:
        raise FormError('Page redirected outside your authorized hosts.')
    selectors = config.get('selectors', {})
    page.wait_for_selector(selectors.get('phone') or 'input', state='visible')
    if challenge_present(page):
        raise FormError('OTP/CAPTCHA detected. No bypass is provided; use your staging test mode.')
    phone = choose_phone(page, selectors)
    phone.fill(phone_number)
    if selectors.get('password'):
        passwords = [explicit(page, selectors['password'])]
        if selectors.get('confirm_password'):
            passwords.append(explicit(page, selectors['confirm_password']))
    else:
        form = phone.locator('xpath=ancestor::form[1]')
        if form.count():
            passwords = [e for e in form.locator('input[type="password"]').all() if e.is_visible() and e.is_enabled()]
        else:
            passwords = visible_candidates(page, 'input[type="password"]')
        if not 1 <= len(passwords) <= 2:
            raise FormError('Expected one password field, or password plus confirmation. Configure selectors.')
    password_value = reversed_phone_password(phone_number)
    for field in passwords:
        field.fill(password_value)
    for checkbox in choose_agreement(page, selectors, phone):
        checkbox.check()
    if challenge_present(page):
        raise FormError('OTP/CAPTCHA detected after filling the form; stopping.')
    # Do not submit forms with additional unfilled required inputs. Some Vue apps wrap
    # fields in custom components without a stable native form, so this validation can
    # occasionally hang; in that case, let the button click proceed and rely on server-side
    # validation instead of aborting early.
    form = phone.locator('xpath=ancestor::form[1]')
    if form.count():
        try:
            missing = form.evaluate('''f => Array.from(f.elements).filter(e =>
              e.willValidate && !e.checkValidity()).map(e => e.name || e.id || e.type)''')
            if missing:
                raise FormError('Invalid or missing required fields: ' + ', '.join(missing))
        except Exception:
            pass
    submit = choose_submit(page, selectors, phone)
    submit.click(timeout=config.get('timeout_ms', 15000))
    deadline = time.monotonic() + config.get('result_timeout_seconds', 10)
    success_selector = selectors.get('success')
    success_url = config.get('success_url_regex')
    while time.monotonic() < deadline:
        if challenge_present(page):
            return 'verification_required', 'OTP/CAPTCHA appeared after submission; stopped.'
        if success_selector and visible_candidates(page, success_selector):
            return 'confirmed', 'Configured success element appeared.'
        if success_url and re.search(success_url, page.url):
            return 'confirmed', 'Configured success URL matched.'
        if not success_selector and not success_url:
            body = page.locator('body').inner_text()
            if re.search(r'registration\s+(?:was\s+)?successful|account\s+(?:has been\s+)?created\s+successfully|successfully\s+registered', body, re.I):
                return 'success_message_detected', 'Success text found; server-side creation not independently verified.'
        page.wait_for_timeout(250)
    return 'unconfirmed', 'Submitted, but no configured success signal appeared. Inspect screenshot; no automatic retry.'


def load_test_numbers(path):
    numbers = []
    with path.open(newline='', encoding='utf-8-sig') as f:
        for row_number, row in enumerate(csv.reader(f), start=1):
            if len(row) != 1 or not re.fullmatch(r'\d{10}', row[0].strip()):
                raise ValueError(f'Expected one 10-digit test number on CSV row {row_number}.')
            numbers.append(row[0].strip())
    if not numbers:
        raise ValueError('The test-number CSV is empty.')
    unique_numbers = list(dict.fromkeys(numbers))
    duplicate_count = len(numbers) - len(unique_numbers)
    if duplicate_count:
        print(f'Skipping {duplicate_count} duplicate CSV row(s).')
    return unique_numbers


def run(config, url, iterations, headless=False, bundled=False, test_numbers=None):
    if config.get('authorized_testing') is not True or config.get('numbers_reserved_for_testing') is not True:
        raise ValueError('Complete authorized-site and reserved-number setup first (python bot.py --setup).')
    if host(url) not in config.get('allowed_hosts', []):
        raise ValueError('URL host is not authorized in config.json. Run --setup for your site.')
    start = str(config['test_phone_start'])
    count = int(config['test_phone_count'])
    if not re.fullmatch(r'\d{10}', start) or count < 1 or int(start) + count - 1 > 9999999999:
        raise ValueError('Provide a valid ten-digit test range.')
    if iterations < 1:
        raise ValueError('Iterations must be positive.')
    state_path = ROOT / 'used_test_numbers.json'
    used = set(json.loads(state_path.read_text()) if state_path.exists() else [])
    if test_numbers is None:
        used_in_range = {n for n in used if int(start) <= int(n) < int(start) + count}
        if iterations > count - len(used_in_range):
            raise ValueError('Not enough unused reserved test numbers. Use a new authorized range.')
    else:
        previously_used = set(test_numbers) & used
        if previously_used:
            raise ValueError(f'{len(previously_used)} supplied test number(s) were already used; refusing to retry.')
        if iterations != len(test_numbers):
            raise ValueError('Iterations must equal the number of supplied test numbers.')
    logdir = ROOT / 'results' / datetime.now().strftime('%Y%m%d_%H%M%S_%f')
    logdir.mkdir(parents=True)
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(headless=headless, **({} if bundled else {'channel': 'chrome'}))
        except Exception as exc:
            raise RuntimeError('Chrome could not launch. Install Chrome, or run with --bundled after setup.') from exc
        try:
            with (logdir / 'results.csv').open('w', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                writer.writerow(['iteration', 'test_phone', 'status', 'detail', 'final_url'])
                for i in range(1, iterations + 1):
                    if test_numbers is not None:
                        number = test_numbers[i - 1]
                    else:
                        number = f'{int(start) + random.randrange(count):010d}'
                        while number in used:
                            number = f'{int(start) + random.randrange(count):010d}'
                    # Reserve before attempting: failed/ambiguous submissions are never retried automatically.
                    used.add(number)
                    state_path.write_text(json.dumps(sorted(used), indent=2))
                    context = browser.new_context()
                    page = context.new_page()
                    page.set_default_timeout(config.get('timeout_ms', 15000))
                    status, detail = 'error', ''
                    try:
                        status, detail = register(page, url, number, config)
                    except Exception as exc:
                        detail = str(exc).split('\n')[0]
                    try:
                        page.screenshot(path=str(logdir / f'{i:04d}_{status}.png'), full_page=True)
                    except Exception:
                        pass
                    writer.writerow([i, number, status, detail, page.url])
                    f.flush()
                    print(f'[{i}/{iterations}] {number}: {status} — {detail}')
                    context.close()
                    if status not in ('confirmed', 'success_message_detected'):
                        print('Stopped safely. Fix the form/config before another run.')
                        break
                    if i < iterations:
                        time.sleep(max(1, float(config.get('delay_seconds', 1))))
        finally:
            browser.close()
    print(f'Results: {logdir}')
    return logdir


def setup(path):
    print('Configure only your own site or a site you have permission to test.')
    if input('Type YES to confirm authorization: ').strip() != 'YES':
        raise ValueError('Authorization not confirmed.')
    url = input('Registration URL: ').strip()
    hostname = host(url)
    print('Numbers MUST be a range reserved by your website for tests, not arbitrary real mobile numbers.')
    if input('Type YES to confirm the test range is reserved: ').strip() != 'YES':
        raise ValueError('Reserved numbers not confirmed.')
    start = input('First 10-digit test number: ').strip()
    count = int(input('Number of test numbers in that range: ').strip())
    if not re.fullmatch(r'\d{10}', start) or count < 1 or int(start) + count - 1 > 9999999999:
        raise ValueError('Invalid test range.')
    cfg = {'authorized_testing': True, 'numbers_reserved_for_testing': True,
           'allowed_hosts': [hostname], 'test_phone_start': start, 'test_phone_count': count,
           'delay_seconds': 1, 'timeout_ms': 15000, 'result_timeout_seconds': 10,
           'selectors': {}, 'success_url_regex': ''}
    path.write_text(json.dumps(cfg, indent=2))
    print(f'Saved {path}. Future runs ask only for URL and iterations.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--setup', action='store_true')
    parser.add_argument('--url')
    parser.add_argument('--iterations', type=int)
    parser.add_argument('--config', default=str(ROOT / 'config.json'))
    parser.add_argument('--numbers-file', help='Use the configured reserved test-number CSV, once per entry')
    parser.add_argument('--headless', action='store_true', help='Hide browser window')
    parser.add_argument('--bundled', action='store_true', help='Use installed Playwright Chromium instead of Chrome')
    args = parser.parse_args()
    path = Path(args.config)
    try:
        if args.setup:
            setup(path)
            return
        if not path.exists():
            print('First-time configuration is required.')
            setup(path)
        config = json.loads(path.read_text())
        url = args.url or input('Registration URL: ').strip()
        test_numbers = None
        if args.numbers_file:
            approved_file = config.get('reserved_test_numbers_file')
            numbers_path = Path(args.numbers_file).resolve()
            if not approved_file or numbers_path != (ROOT / approved_file).resolve():
                raise ValueError('The CSV path must match reserved_test_numbers_file in config.json.')
            if args.iterations is not None:
                raise ValueError('Do not set --iterations when using --numbers-file.')
            test_numbers = load_test_numbers(numbers_path)
            iterations = len(test_numbers)
        else:
            iterations = args.iterations if args.iterations is not None else int(input('Iterations: ').strip())
        run(config, url, iterations, args.headless, args.bundled, test_numbers)
    except KeyboardInterrupt:
        print('\nStopped by user.')
    except Exception as exc:
        print(f'ERROR: {exc}')
        raise SystemExit(1)


if __name__ == '__main__':
    main()
