#!/usr/bin/env python3
"""
Simple HTML Scraper for Dark Web Marketplaces

This scraper:
1. Reads category URLs from pages_url.json
2. Extracts all product listing URLs from each category page
3. Fetches the raw HTML for each product page
4. Saves everything to products_html.json (overwrites each time)

General-purpose: Works with any marketplace HTML structure.


# If the marketplace limits, create a new account

TODO for Torzon Crawling. 
- Add a check if the string "Not calling you a bot,..." so we can refresh the crawl later.
- If the string appears, save it in a txt file to recrawl later.
- Only saves the successful product HTML pages.
- Maybe reduce the crawl speed with a delay between requests.
- Use time.sleep(random.randrange(20, 60)) to add delay between requests.

Parser:
- Add the listing posted date to the parsed drugs JSON.
- If there is not a date, use the Python datetime library to get the current date.

"""

import json
import os
import random
import re
import time
import urllib.parse
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests
from selenium import webdriver
from selenium.webdriver.firefox.options import Options
from selenium.common.exceptions import TimeoutException, WebDriverException
from bs4 import BeautifulSoup
from termcolor import colored


# Configuration
PROXY_HOST = "127.0.0.1"
PROXY_PORT = 8118
TOR_SOCKS_PORT = 9050

# Browser/session behaviour
SESSION_WARMUP_SECONDS = 30

# Input/Output files
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "data"
PAGES_URL_FILE = DATA_DIR / "config" / "pages_url.json"
PRODUCTS_HTML_FILE = DATA_DIR / "raw" / "products_html.json"


def load_pages_urls():
    """Load category URLs from pages_url.json"""
    if not PAGES_URL_FILE.exists():
        print(colored(f"❌ {PAGES_URL_FILE} not found!", "red"))
        print(colored(f"   Create it with category URLs, example:", "yellow"))
        print(colored(f'   ["http://marketplace.onion/category1/", "http://marketplace.onion/category2/"]', "yellow"))
        return []
    
    try:
        with open(PAGES_URL_FILE, 'r', encoding='utf-8') as f:
            urls = json.load(f)
            print(colored(f"✅ Loaded {len(urls)} category URLs from {PAGES_URL_FILE}", "green"))
            return urls
    except Exception as e:
        print(colored(f"❌ Error loading {PAGES_URL_FILE}: {e}", "red"))
        return []


def save_products_html(products, output_path, overwrite=True):
    """Save product HTML data to the specified JSON file"""
    mode = 'w' if overwrite else 'a'

    try:
        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
        with open(output_path, mode, encoding='utf-8') as f:
            json.dump(products, f, ensure_ascii=False, indent=2)
        print(colored(f"✅ Saved {len(products)} products to {output_path}", "green"))
    except Exception as e:
        print(colored(f"❌ Error saving to {output_path}: {e}", "red"))


def extract_cookies(driver, do_quit=False):
    """Extract cookies from Selenium driver"""
    cookies = driver.get_cookies()
    cookie_dict = {cookie['name']: cookie['value'] for cookie in cookies}
    if do_quit:
        try:
            driver.quit()
        except Exception:
            pass
    return cookie_dict


def setup_requests_session(cookies, use_socks=False, socks_port=9050, verify_ssl=True):
    """Setup requests session with cookies, proxy, and TLS behaviour"""
    session = requests.Session()
    
    if use_socks:
        session.proxies = {
            'http': f'socks5h://{PROXY_HOST}:{socks_port}',
            'https': f'socks5h://{PROXY_HOST}:{socks_port}'
        }
    else:
        session.proxies = {
            'http': f'http://{PROXY_HOST}:{PROXY_PORT}',
            'https': f'http://{PROXY_HOST}:{PROXY_PORT}'
        }
    
    session.cookies.update(cookies)
    session.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; rv:102.0) Gecko/20100101 Firefox/102.0'
    })
    session.verify = verify_ssl

    return session


def build_firefox_options(use_socks=False, socks_port=TOR_SOCKS_PORT, tor_binary=None, disable_js=False):
    """Build Firefox options pointed at Tor (SOCKS5 or HTTP proxy).

    Shared by the scraper and other tools (e.g. target_categories.py) so the
    browser/proxy behaviour stays identical across the pipeline.
    """
    options = Options()
    if tor_binary:
        options.binary_location = tor_binary

    options.set_preference("network.proxy.type", 1)

    if use_socks:
        options.set_preference("network.proxy.socks", PROXY_HOST)
        options.set_preference("network.proxy.socks_port", socks_port)
        options.set_preference("network.proxy.socks_version", 5)
        # Let Tor handle DNS lookups so .onion hosts resolve correctly.
        options.set_preference("network.proxy.socks_remote_dns", True)
    else:
        options.set_preference("network.proxy.http", PROXY_HOST)
        options.set_preference("network.proxy.http_port", PROXY_PORT)
        options.set_preference("network.proxy.ssl", PROXY_HOST)
        options.set_preference("network.proxy.ssl_port", PROXY_PORT)

    options.set_preference("network.proxy.no_proxies_on", "")

    if disable_js:
        # Disabling JS can help avoid heavy pages or scripts that detect automation.
        options.set_preference("javascript.enabled", False)

    return options


def extract_product_links(html, base_url):
    """
    Extract product links from category page HTML.
    Only extracts actual product pages, NOT category/navigation links.
    """
    soup = BeautifulSoup(html, 'html.parser')
    product_links = set()
    
    # Strategy 1: WooCommerce specific selectors (most common on dark web markets)
    woocommerce_selectors = [
        'li.product a.woocommerce-LoopProduct-link',
        'li.product h2 a',
        'li.product a[href]',
        '.products li.product a',
        'ul.products li a'
    ]
    
    for selector in woocommerce_selectors:
        links = soup.select(selector)
        for link in links:
            href = link.get('href')
            if href:
                full_url = urllib.parse.urljoin(base_url, href)
                # Only add if it's NOT a category page and not the listing page itself
                if (
                    '/product-category/' not in full_url
                    and '/category/' not in full_url
                    and full_url.rstrip('/') != base_url.rstrip('/')
                ):
                    product_links.add(full_url)
    
    # If we found products via WooCommerce selectors, return them
    if product_links:
        return list(product_links)
    
    # Strategy 2: Generic product link detection (fallback)
    # Look for links that have product-like patterns but exclude categories
    all_links = soup.find_all('a', href=True)
    for link in all_links:
        href = link['href']
        full_url = urllib.parse.urljoin(base_url, href)

        # Detect BlackOps-style UUID product URLs explicitly
        if '/product/' in full_url.lower():
            path = urllib.parse.urlparse(full_url).path
            if re.search(r'/product/[0-9a-f-]{36}', path, re.IGNORECASE):
                product_links.add(full_url)
                continue

        # Torzon-style query endpoints
        if 'products.php' in full_url.lower() and 'action=view' in full_url.lower():
            product_links.add(full_url)
            continue
        
        # Must match product indicators
        product_indicators = ['/shop/', '/item/', '/listing/', '/p/', '/product/', 'products.php?action=view']
        
        # Must NOT match excluded patterns  
        excluded_patterns = [
            'cart', 'checkout', 'account', 'login',
            '/product-category/', '/category/', '/tag/',
            'page/', '/page-', 'author', 'search', 'filter'
        ]
        
        has_product_indicator = any(indicator in full_url.lower() for indicator in product_indicators)
        has_excluded = any(pattern in full_url.lower() for pattern in excluded_patterns)
        
        if (
            has_product_indicator
            and not has_excluded
            and full_url.rstrip('/') != base_url.rstrip('/')
        ):
            product_links.add(full_url)
    
    return list(product_links)


def fetch_page_html(session, url, retries=3):
    """Fetch HTML from a URL with retries"""
    for attempt in range(retries):
        try:
            response = session.get(url, timeout=30)
            if response.status_code == 200:
                return response.text
            else:
                print(colored(f"⚠️  HTTP {response.status_code} for {url}", "yellow"))
        except requests.exceptions.RequestException as e:
            print(colored(f"❌ Error fetching {url} (attempt {attempt+1}/{retries}): {e}", "red"))
            if attempt < retries - 1:
                time.sleep(5)

    return None


# A meta-refresh-to-homepage stub is how some markets (e.g. drughub) bounce a
# request they consider unauthenticated/expired. Detect it so the browser fetch
# can retry instead of accepting the empty page.
_HOMEPAGE_BOUNCE_RE = re.compile(r'http-equiv=["\']?refresh["\']?[^>]*url=/?["\']?\s*/?\s*["\']?', re.IGNORECASE)


def _looks_like_bounce(html):
    return bool(html) and len(html) < 400 and bool(_HOMEPAGE_BOUNCE_RE.search(html))


def _looks_like_captcha(html):
    """True if the page is an anti-bot / CAPTCHA challenge rather than content."""
    if not html:
        return False
    low = html.lower()
    return ('<title' in low and 'captcha' in low.split('</title>')[0]) or \
        any(w in low for w in ('captcha', 'verify you are human', 'cloudflare', 'ddos-guard', 'are you human'))


def fetch_page_html_browser(driver, url, settle=3.0, retries=5, manual=False):
    """Fetch a page through the live Selenium browser (carries JS cookies/tokens),
    returning driver.page_source. Used for markets that reject deep pagination over
    the plain requests session and redirect to the homepage or throw a CAPTCHA.

    drughub's session goes stale after a handful of pages: it then either throws a
    fresh CAPTCHA or bounces the request to the homepage. When `manual` is set, both
    cases pause so the operator can re-solve / re-auth in the open browser; solving
    once unlocks the next run of pages.

    NOT thread-safe (one WebDriver), so only the serialized category-page path may
    call it -- never the product worker threads.
    """
    for attempt in range(retries):
        try:
            driver.get(url)
        except TimeoutException:
            try:
                driver.execute_script("window.stop();")
            except Exception:
                pass
        except WebDriverException as exc:
            print(colored(f"⚠️  Browser fetch failed for {url}: {exc}", "yellow"))
            return None
        if settle > 0:
            time.sleep(settle)
        html = driver.page_source

        if not _looks_like_captcha(html) and not _looks_like_bounce(html):
            return html  # got the real page

        # Stale session: CAPTCHA challenge or homepage bounce. Both need the operator
        # to re-establish the session in the open browser, then we re-fetch the page.
        if manual:
            kind = "CAPTCHA" if _looks_like_captcha(html) else "homepage bounce (session expired)"
            print(colored(f"\n🔐 {kind} on {url}", "yellow", attrs=['bold']))
            print(colored("   In the open browser: solve any CAPTCHA / let the homepage load so the "
                          "session is valid again, then press Enter — I'll re-fetch this page.", "yellow"))
            input(colored("   Press Enter to retry this page...", "yellow"))
            continue  # next loop iteration re-does driver.get(url)

        # Non-interactive: a couple of quiet retries for a transient bounce, else bail.
        print(colored(f"   ↩️  {url} blocked (attempt {attempt+1}/{retries}); "
                       f"{'retrying' if attempt < retries - 1 else 'giving up'}...", "yellow"))
        time.sleep(2)
    return html


def scrape_category_page(session, category_url, driver=None, use_browser=False, manual=False):
    """Scrape a category page and return all product links.

    When use_browser is set (and a driver is supplied), the listing page is loaded
    through the live browser instead of the requests session -- required for markets
    that bounce deep ?page=N requests to the homepage. `manual` lets the browser path
    pause for the operator to solve a mid-crawl CAPTCHA.
    """
    print(colored(f"\n📄 Scraping category: {category_url}", "cyan"))

    if use_browser and driver is not None:
        html = fetch_page_html_browser(driver, category_url, manual=manual)
    else:
        html = fetch_page_html(session, category_url)
    if not html:
        print(colored(f"❌ Failed to fetch category page", "red"))
        return None, None
    
    product_links = extract_product_links(html, category_url)
    print(colored(f"✅ Found {len(product_links)} product links", "green"))

    # Diagnostic: if a category page yields no products, dump its HTML so we can
    # see WHY (CAPTCHA / anti-bot block / empty list / JS-rendered listings).
    if not product_links:
        try:
            debug_dir = DATA_DIR / "raw" / "_debug"
            debug_dir.mkdir(parents=True, exist_ok=True)
            host = urllib.parse.urlparse(category_url).netloc[:16]
            stamp = time.strftime("%H%M%S")
            debug_path = debug_dir / f"empty_{host}_{stamp}.html"
            debug_path.write_text(html, encoding="utf-8")
            looks_blocked = any(w in html.lower() for w in
                                ('captcha', 'verify you', 'cloudflare', 'challenge', 'ddos', 'rate limit', 'too many'))
            note = " (looks like a CAPTCHA/anti-bot page)" if looks_blocked else ""
            print(colored(f"   🐞 0 products — saved page HTML to {debug_path}{note}", "magenta"))
        except Exception as exc:
            print(colored(f"   ⚠️  Could not write debug HTML: {exc}", "yellow"))

    # Also check for pagination
    soup = BeautifulSoup(html, 'html.parser')
    pagination_links = []
    
    pagination_selectors = [
        'a[rel="next"]', 'a.next', 'li.next a',
        '.pagination a', 'ul.pagination a',
        'nav a', 'a[aria-label="Next"]'
    ]
    
    for selector in pagination_selectors:
        links = soup.select(selector)
        for link in links:
            href = link.get('href')
            if href:
                full_url = urllib.parse.urljoin(category_url, href)
                pagination_links.append(full_url)
        if pagination_links:
            break
    
    if pagination_links:
        print(colored(f"📑 Found {len(pagination_links)} pagination links", "blue"))

    return product_links, pagination_links


# Explicit pagination markers. Each lives in the query string or path, never in
# the netloc -- important because v3 onion hosts are full of digits (2-7) that
# must not be mistaken for page numbers.
PAGE_PATTERNS = (
    r'([?&]page=)(\d+)',
    r'([?&]paged=)(\d+)',
    r'([?&]p=)(\d+)',
    r'(/page/)(\d+)',
)


def find_page_pattern(url):
    """Return (template, page_number) if `url` carries an explicit pagination
    marker (?page=N, &paged=N, /page/N, ...), else None.

    `template` is the URL with '{n}' in place of the page number, so callers can
    rebuild any page. Only the query/path is inspected (the patterns are anchored
    to '?'/'&'/'/page/'), so digits inside the onion hostname are never treated
    as page numbers.
    """
    for pat in PAGE_PATTERNS:
        m = re.search(pat, url)
        if m:
            template = url[:m.start(2)] + "{n}" + url[m.end(2):]
            return template, int(m.group(2))
    return None


# Hard ceiling on the forward page-walk so a misbehaving (or clamping) market
# can never spin forever. Reaching it prints a warning.
WALK_PAGE_CAP = 1000


def page_url(template, n):
    """Build the URL for page `n` from a '{n}' template (see find_page_pattern)."""
    return template.replace("{n}", str(n))


def scrape_product_page(session, product_url, category_url, market_name):
    """Scrape a single product page and return HTML data"""
    print(colored(f"  📦 Fetching: {product_url}", "blue"))
    
    html = fetch_page_html(session, product_url)
    if not html:
        return None
    
    return {
        "market": market_name,
        "category_page": category_url,
        "product_url": product_url,
        "fetched_at": int(time.time()),
        "html": html
    }


def main():
    parser = argparse.ArgumentParser(description='Simple HTML scraper for dark web marketplaces')
    parser.add_argument('--manual', action='store_true', 
                       help='Open browser and wait for manual CAPTCHA solving')
    parser.add_argument('--socks', action='store_true', 
                       help='Use Tor SOCKS5 (default uses HTTP proxy on 8118)')
    parser.add_argument('--socks-port', type=int, default=TOR_SOCKS_PORT,
                       help='Tor SOCKS port (default: 9050)')
    parser.add_argument('--page-timeout', type=int, default=300,
                       help='Selenium page load timeout in seconds')
    parser.add_argument('--tor-binary', type=str, default=None,
                       help='Path to Tor Browser firefox binary')
    parser.add_argument('--delay', type=float, default=2.0,
                       help='Delay between requests in seconds (default: 2)')
    parser.add_argument('--max-products', type=int, default=None,
                       help='Maximum number of products to scrape (default: unlimited)')
    parser.add_argument('--session-wait', type=int, default=SESSION_WARMUP_SECONDS,
                       help='Seconds to wait after opening a page before collecting cookies (default: 30)')
    parser.add_argument('--disable-js', action='store_true',
                       help='Disable JavaScript execution in the browser (Firefox preference)')
    parser.add_argument('--insecure', action='store_true',
                       help='Skip TLS certificate verification (useful for self-signed certs)')
    parser.add_argument('--keep-browser-open', action='store_true',
                       help='Leave Firefox open at the end so you can see/solve CAPTCHAs if sessions refresh')
    parser.add_argument('--max-pages-per-category', type=int, default=3,
                       help='Follow pagination up to this many pages per category (0 = unlimited, 1 = first page only)')
    parser.add_argument('--enumerate-pages', action='store_true',
                       help='(Legacy) URLs with a pagination marker (?page=N, /page/N, ...) are '
                            'already page-walked automatically from page 1 until an empty page, '
                            'so this flag is rarely needed; for a marker-less URL it just forces a '
                            'single-page crawl instead of following pagination links')
    parser.add_argument('--single-page', action='store_true',
                       help='Crawl each seed URL as exactly one page (no forward-walk, no '
                            'pagination-following). Use with an explicit list of page URLs, e.g. '
                            'drughub ?page=1 .. ?page=15.')
    parser.add_argument('--workers', type=int, default=2,
                       help='Number of product pages to fetch concurrently (default: 2). '
                            'drughub allows ~2 simultaneous requests; raise for more lenient '
                            'markets. 1 = fully sequential.')
    parser.add_argument('--browser-categories', action='store_true',
                       help='Fetch category/listing pages through the Selenium browser instead of '
                            'the requests session. Needed for markets (e.g. drughub) that bounce '
                            'deep ?page=N requests to the homepage over a plain requests session.')

    args = parser.parse_args()
    
    # Load category URLs
    category_urls = load_pages_urls()
    if not category_urls:
        print(colored("\n❌ No URLs to scrape. Exiting.", "red"))
        return
    
    print(colored(f"\n🚀 Starting scraper...", "cyan", attrs=['bold']))
    print(colored(f"   Categories to scrape: {len(category_urls)}", "white"))
    print(colored(f"   Delay between requests: {args.delay}s", "white"))
    if args.insecure:
        print(colored("   WARNING: TLS verification disabled (--insecure)", "yellow"))

    run_timestamp = time.strftime("%Y%m%d_%H%M%S")
    base_name, ext = os.path.splitext(PRODUCTS_HTML_FILE)
    output_file = f"{base_name}_{run_timestamp}{ext or '.json'}"
    print(colored(f"   Output file: {output_file}", "white"))
    
    # Initialize browser for CAPTCHA solving
    driver = None
    host_sessions = {}
    all_products = []
    scraped_urls = set()
    last_host = None

    def ensure_driver():
        nonlocal driver
        if driver is not None:
            return driver

        options = build_firefox_options(
            use_socks=args.socks,
            socks_port=args.socks_port,
            tor_binary=args.tor_binary,
            disable_js=args.disable_js,
        )

        driver = webdriver.Firefox(options=options)
        driver.set_page_load_timeout(args.page_timeout)
        return driver

    def establish_session(target_url, reason):
        nonlocal driver
        parsed_url = urllib.parse.urlparse(target_url)
        host = parsed_url.netloc
        if host in host_sessions:
            return host_sessions[host]

        driver = ensure_driver()

        try:
            driver.delete_all_cookies()
        except Exception:
            pass

        print(colored(f"\n🌐 Opening ({reason}): {target_url}", "blue"))

        try:
            driver.get(target_url)
        except TimeoutException:
            try:
                driver.execute_script("window.stop();")
            except Exception:
                pass
            print(colored("⏱️  Page load timed out, continuing...", "yellow"))
        except WebDriverException as exc:
            try:
                driver.execute_script("window.stop();")
            except Exception:
                pass
            print(colored(f"⚠️  Browser navigation failed for {target_url}", "yellow"))
            print(colored(f"   {exc}", "yellow"))
            print(colored("   Continuing so the session can still be captured.", "yellow"))

        if args.session_wait > 0:
            print(colored(f"⏳ Waiting {args.session_wait} seconds before collecting cookies...", "yellow"))
            time.sleep(args.session_wait)

        if args.manual:
            print(colored("\n🔐 Manual step: Please solve any CAPTCHA in the browser.", "yellow", attrs=['bold']))
            print(colored("   Leave the browser open. When the page works in Requests too, press Enter here.", "yellow"))
            input(colored("   Press Enter to continue...", "yellow"))

        cookies = extract_cookies(driver, do_quit=False)
        session = setup_requests_session(cookies, args.socks, args.socks_port, verify_ssl=not args.insecure)
        host_sessions[host] = session

        print(colored(f"✅ Session ready for {host} (cookies captured: {len(cookies)})", "green"))
        return session

    def refresh_session(target_url, reason):
        parsed_url = urllib.parse.urlparse(target_url)
        host = parsed_url.netloc
        if host in host_sessions:
            del host_sessions[host]
        return establish_session(target_url, reason)

    try:
        for category_url in category_urls:
            print(colored(f"\n{'='*80}", "cyan"))
            print(colored(f"CATEGORY: {category_url}", "cyan", attrs=['bold']))
            print(colored(f"{'='*80}", "cyan"))
            
            # Extract market name from URL
            parsed = urllib.parse.urlparse(category_url)
            market_name = parsed.netloc
            
            if market_name not in host_sessions:
                reason = "new marketplace detected" if host_sessions else "initial session setup"
                session = establish_session(category_url, reason)
            else:
                if market_name != last_host:
                    print(colored(f"🔁 Switching back to existing session for {market_name}", "blue"))
                session = host_sessions[market_name]
            last_host = market_name
            
            # Crawl paginated category pages
            pages_seen = set()
            pages_scraped = 0

            # Decide the crawl strategy for THIS url.
            #  * URL with a pagination marker (?page=N, /page/N, ...): walk forward
            #    from page 1, requesting page+1 as long as each page yields products,
            #    until an empty (or repeated) page. This does NOT rely on the seed
            #    page exposing every page link -- many markets only render a small
            #    pagination window or build it with JS, so a one-shot scan under-counts.
            #  * URL without a marker: normal crawl (single page, plus any pagination
            #    links the market happens to expose).
            page_pat = find_page_pattern(category_url)
            walk_template = None
            walk_prev_sig = None
            if args.single_page:
                # Temporary mode: crawl each seed URL as exactly one page -- no
                # forward-walk, no pagination-following. Lets you list explicit page
                # URLs (e.g. ?page=1 .. ?page=15) and fetch each one singularly.
                page_queue = [category_url]
                follow_pagination = False
                page_limit = None
                print(colored("   --single-page → fetching this URL as one page only.", "yellow"))
            elif page_pat is not None:
                walk_template = page_pat[0]
                page_queue = [page_url(walk_template, 1)]  # always start from page 1
                follow_pagination = False
                page_limit = None
                print(colored(f"🔎 Pagination detected → walking pages from 1 until empty "
                              f"(pattern: {walk_template.replace('{n}', 'N')})", "cyan", attrs=['bold']))
            elif args.enumerate_pages:
                # Forced enumerate but the URL has no marker → just the single page.
                page_queue = [category_url]
                follow_pagination = False
                page_limit = None
                print(colored("   No pagination logic in URL → single-page crawl.", "yellow"))
            else:
                # Normal crawl: single page, but still follow pagination links if the
                # market exposes them.
                page_queue = [category_url]
                follow_pagination = True
                page_limit = None if args.max_pages_per_category == 0 else args.max_pages_per_category

            while page_queue:
                if page_limit is not None and pages_scraped >= page_limit:
                    break

                current_page = page_queue.pop(0)
                if current_page in pages_seen:
                    continue
                pages_seen.add(current_page)

                # Reuse the same session for the host
                page_session = host_sessions.get(market_name) or session

                product_links, pagination_links = scrape_category_page(
                    page_session, current_page, driver=driver,
                    use_browser=args.browser_categories, manual=args.manual)
                if product_links is None:
                    print(colored("⚠️  Retrying after refreshing session...", "yellow"))
                    page_session = refresh_session(current_page, "retry after failed fetch")
                    product_links, pagination_links = scrape_category_page(
                        page_session, current_page, driver=driver,
                        use_browser=args.browser_categories, manual=args.manual)
                    if product_links is None:
                        print(colored("❌ Skipping page due to repeated fetch failures", "red", attrs=['bold']))
                        continue

                # Add new pagination links to queue (normal crawl only).
                if follow_pagination:
                    for link in pagination_links or []:
                        if link not in pages_seen:
                            page_queue.append(link)

                # Forward page-walk: enqueue the next page number as long as this
                # page returned products and didn't just repeat the previous page
                # (some markets clamp out-of-range page numbers to the last page).
                if walk_template:
                    sig = frozenset(product_links)
                    cur = find_page_pattern(current_page)
                    if not product_links:
                        print(colored("   ⛔ Empty page → reached the end of pagination.", "yellow"))
                    elif sig == walk_prev_sig:
                        print(colored("   ⛔ Page repeats the previous one → reached the end of pagination.", "yellow"))
                    elif cur and cur[1] >= WALK_PAGE_CAP:
                        print(colored(f"   ⚠️  Hit the {WALK_PAGE_CAP}-page safety cap; stopping walk.", "yellow"))
                    elif cur:
                        nxt = page_url(walk_template, cur[1] + 1)
                        if nxt not in pages_seen:
                            page_queue.append(nxt)
                    walk_prev_sig = sig

                pages_scraped += 1

                all_product_links = set(product_links)
                print(colored(f"\n📊 Products found on this page: {len(all_product_links)} (page {pages_scraped})", "green", attrs=['bold']))

                # Build the work list: drop duplicates and ensure a session exists for
                # each product host UP FRONT. Session setup uses Selenium (not thread
                # safe), so it must happen here, serially, before any worker runs.
                pending = []
                for product_url in all_product_links:
                    if product_url in scraped_urls:
                        print(colored(f"  ⏭️  Skipping duplicate: {product_url}", "yellow"))
                        continue
                    product_host = urllib.parse.urlparse(product_url).netloc or market_name
                    if product_host not in host_sessions:
                        print(colored(f"\n🔄 New host detected for product ({product_host}). Opening browser...", "yellow"))
                        establish_session(product_url, "product host session setup")
                    pending.append((product_url, product_host))

                # Respect the global max-products cap before fetching this batch.
                if args.max_products:
                    room = args.max_products - len(all_products)
                    if room <= 0:
                        break
                    pending = pending[:room]

                def fetch_one(item):
                    """Fetch one product page (runs in a worker thread)."""
                    p_url, p_host = item
                    data = scrape_product_page(host_sessions[p_host], p_url, current_page, p_host)
                    # Pace each worker so `--workers` concurrent streams stay polite.
                    time.sleep(args.delay + random.uniform(0, 1))
                    return p_url, data

                def record(p_url, data):
                    """Store a finished fetch (called only from the main thread)."""
                    if data:
                        all_products.append(data)
                        scraped_urls.add(p_url)
                        print(colored(f"    ✅ Saved {p_url} (total: {len(all_products)})", "green"))
                    else:
                        print(colored(f"    ❌ Failed {p_url}", "red"))

                workers = max(1, args.workers)
                if workers > 1 and len(pending) > 1:
                    print(colored(f"  ⚙️  Fetching {len(pending)} products, {workers} at a time...", "blue"))
                    with ThreadPoolExecutor(max_workers=workers) as pool:
                        futures = {pool.submit(fetch_one, item): item for item in pending}
                        for fut in as_completed(futures):
                            p_url, data = fut.result()
                            record(p_url, data)  # main thread → no lock needed
                else:
                    for item in pending:
                        p_url, data = fetch_one(item)
                        record(p_url, data)

            if args.max_products and len(all_products) >= args.max_products:
                break
        
        # Save all products to JSON (overwrite mode)
        print(colored(f"\n{'='*80}", "cyan"))
        print(colored(f"💾 SAVING RESULTS", "cyan", attrs=['bold']))
        print(colored(f"{'='*80}", "cyan"))
        
        save_products_html(all_products, output_file, overwrite=True)
        
        print(colored(f"\n✅ Scraping complete!", "green", attrs=['bold']))
        print(colored(f"   Total products scraped: {len(all_products)}", "green"))
        print(colored(f"   Saved to: {output_file}", "green"))
        
    except KeyboardInterrupt:
        print(colored("\n\n⚠️  Scraping interrupted by user", "yellow"))
        if all_products:
            print(colored(f"💾 Saving {len(all_products)} products collected so far...", "yellow"))
            save_products_html(all_products, output_file, overwrite=True)
    
    except Exception as e:
        print(colored(f"\n❌ Error: {e}", "red"))
        import traceback
        traceback.print_exc()
    
    finally:
        if driver and not args.keep_browser_open:
            if args.manual:
                try:
                    input(colored("\n👋 Press Enter once you're ready for the scraper to close Firefox...", "yellow"))
                except EOFError:
                    pass
            try:
                driver.quit()
            except Exception:
                pass
        elif driver and args.keep_browser_open:
            print(colored("\nℹ️  Leaving Firefox open (--keep-browser-open). Manually close it when done.", "yellow"))


if __name__ == "__main__":
    main()
