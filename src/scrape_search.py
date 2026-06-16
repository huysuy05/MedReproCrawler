#!/usr/bin/env python3
"""
DrugHub keyword-search crawler.

Unlike scrape_simple.py (which walks pre-listed category pages from
data/config/pages_url.json), this script crawls DrugHub by SEARCH TERM. For each
keyword in data/config/search_keywords.json it:

  1. Loads the search-results page in the live browser, pausing for manual CAPTCHA
     handling (--manual) on each search.
  2. Reads the result count from the page header
     `<h1 class="h2 m-0 mb-1"><strong>N</strong> Listings</h1>`.
  3. Only if N > 0, walks every result page (&page=N) and fetches each /listing/
     product page.

Output is written in the SAME raw record format and `products_html_<timestamp>.json`
naming as scrape_simple.py, so the existing pipeline picks it up with no changes:

    python3 src/scrape_search.py --manual --socks --socks-port 9150 --insecure
    python3 src/merge_html_sessions.py
    python3 src/parser.py
    python3 src/filter_medicines.py
    python3 src/push_to_sheets.py

DrugHub-only for now.
"""

import argparse
import json
import os
import random
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from bs4 import BeautifulSoup
from termcolor import colored

# Reuse the existing crawler's primitives instead of reimplementing them. The
# heavy crawl in scrape_simple.py only runs under its __main__ guard, so importing
# from it is side-effect free.
from scrape_simple import (
    TOR_SOCKS_PORT,
    SESSION_WARMUP_SECONDS,
    WALK_PAGE_CAP,
    build_firefox_options,
    extract_cookies,
    setup_requests_session,
    fetch_page_html_browser,
    extract_product_links,
    scrape_product_page,
    save_products_html,
)


# Configuration
DRUGHUB_BASE = "http://drughubdzlrwp2pyserbkmc2sxbxzrjvutirthcyqn6c2p46qcc7mlyd.onion"
SEARCH_URL_TEMPLATE = DRUGHUB_BASE + "/?search_terms={term}&search=simple"
DRUGHUB_HOST = urllib.parse.urlparse(DRUGHUB_BASE).netloc

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "data"
KEYWORDS_FILE = DATA_DIR / "config" / "search_keywords.json"
PRODUCTS_HTML_FILE = DATA_DIR / "raw" / "products_html.json"
# Resume checkpoint: which terms have already been fully searched, so reruns pick
# up where the last run left off instead of re-searching everything.
PROGRESS_FILE = DATA_DIR / "search_progress.json"


def load_search_terms(path):
    """Load every keyword from search_keywords.json into one flat, deduped list.

    The file is shaped like {category: [terms]} (e.g. "contraception",
    "abortion"). All groups are flattened; first occurrence wins and dedup is
    case-insensitive so the same term across groups is searched once.
    """
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError("Expected the keywords JSON to be an object of {category: [terms]}")

    terms = []
    seen = set()
    for group_terms in data.values():
        for term in group_terms:
            term = str(term).strip()
            key = term.lower()
            if term and key not in seen:
                seen.add(key)
                terms.append(term)
    return terms


def load_progress(path):
    """Load the set of already-completed search terms (lowercased keys).

    Returns an empty set if the file is missing or unreadable -- a bad checkpoint
    should never block a crawl, it just means we re-search.
    """
    if not path.exists():
        return set()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        terms = data.get("completed_terms", []) if isinstance(data, dict) else []
        return {str(t).strip().lower() for t in terms if str(t).strip()}
    except Exception as exc:
        print(colored(f"⚠️  Could not read progress file {path}: {exc} (starting fresh)", "yellow"))
        return set()


def save_progress(path, completed_terms):
    """Persist the completed terms (original casing) so a rerun can resume."""
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        payload = {
            "completed_terms": sorted(completed_terms, key=str.lower),
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
    except Exception as exc:
        print(colored(f"⚠️  Could not write progress file {path}: {exc}", "yellow"))


def build_search_url(term, page=1):
    """Search-results URL for `term` (URL-encoded), with &page=N for page > 1."""
    url = SEARCH_URL_TEMPLATE.format(term=urllib.parse.quote(term))
    if page > 1:
        url += f"&page={page}"
    return url


def parse_listing_count(html):
    """Return the integer result count from DrugHub's search header, else None.

    The count lives in `<h1 class="h2 m-0 mb-1"><strong>N</strong> Listings</h1>`.
    That h1 class is ALSO used for the product-page title, so we only accept the
    element when its text actually mentions "Listing" -- otherwise we'd mis-read a
    product title as a count. None means the header wasn't found (unknown).
    """
    if not html:
        return None
    soup = BeautifulSoup(html, "html.parser")
    for h1 in soup.select("h1.h2.m-0.mb-1"):
        if "listing" not in h1.get_text().lower():
            continue
        strong = h1.find("strong")
        if not strong:
            continue
        digits = strong.get_text().strip().replace(",", "")
        if digits.isdigit():
            return int(digits)
    return None


def crawl_search_term(driver, session, term, args, scraped_urls, all_products):
    """Crawl every listing returned for a single search `term`.

    Returns True if the term was fully searched (so it can be checkpointed as
    done), False if it stopped early for a reason worth retrying next run -- a
    fetch/CAPTCHA failure, an unreadable count, the global --max-products cap, or
    a count>0 page that yielded no links (likely a selector issue).
    """
    print(colored(f"\n{'='*80}", "cyan"))
    print(colored(f"SEARCH: {term!r}", "cyan", attrs=["bold"]))
    print(colored(f"{'='*80}", "cyan"))

    walk_prev_sig = None
    page = 1
    count = None

    while page <= WALK_PAGE_CAP:
        search_page_url = build_search_url(term, page)
        # Browser fetch carries JS cookies/tokens and is where the operator solves
        # any CAPTCHA for THIS search (manual pause inside fetch_page_html_browser).
        html = fetch_page_html_browser(driver, search_page_url, manual=args.manual)
        if not html:
            print(colored(f"❌ Failed to fetch search page {page} for {term!r}", "red"))
            return False  # transient → retry next run

        # First page: gate the whole term on the reported result count.
        if page == 1:
            count = parse_listing_count(html)
            if count is None:
                print(colored(f"   ⚠️  Could not read a result count for {term!r} — will retry next run.", "yellow"))
                return False
            print(colored(f"   🔢 {count} listing(s) reported for {term!r}", "green", attrs=["bold"]))
            if count == 0:
                print(colored("   ⏭️  0 listings → nothing to crawl (done).", "yellow"))
                return True  # genuinely no results → don't re-search

        product_links = extract_product_links(html, search_page_url)
        print(colored(f"   📄 Page {page}: found {len(product_links)} product link(s)", "blue"))

        # End-of-pagination detection (mirrors scrape_simple's forward walk):
        # stop on an empty page or one that repeats the previous page's link set.
        sig = frozenset(product_links)
        if not product_links:
            if page == 1 and count:
                # Count said there are results but we extracted none — almost
                # certainly a selector mismatch. Don't checkpoint; surface it.
                print(colored(f"   ⚠️  {count} listed but 0 links extracted — not marking done.", "yellow"))
                return False
            print(colored("   ⛔ Empty page → end of results.", "yellow"))
            return True
        if sig == walk_prev_sig:
            print(colored("   ⛔ Page repeats the previous one → end of results.", "yellow"))
            return True
        walk_prev_sig = sig

        # Fetch the new product pages over the (fast) requests session.
        pending = []
        for product_url in product_links:
            if product_url in scraped_urls:
                continue
            if args.max_products and len(all_products) >= args.max_products:
                break
            pending.append(product_url)

        def fetch_one(p_url):
            data = scrape_product_page(session, p_url, search_page_url, DRUGHUB_HOST)
            time.sleep(args.delay + random.uniform(0, 1))
            return p_url, data

        def record(p_url, data):
            if data:
                all_products.append(data)
                scraped_urls.add(p_url)
                print(colored(f"    ✅ Saved {p_url} (total: {len(all_products)})", "green"))
            else:
                print(colored(f"    ❌ Failed {p_url}", "red"))

        workers = max(1, args.workers)
        if workers > 1 and len(pending) > 1:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {pool.submit(fetch_one, p): p for p in pending}
                for fut in as_completed(futures):
                    p_url, data = fut.result()
                    record(p_url, data)
        else:
            for p_url in pending:
                _, data = fetch_one(p_url)
                record(p_url, data)

        if args.max_products and len(all_products) >= args.max_products:
            # Cap hit mid-term: don't checkpoint, so this term resumes next run.
            print(colored("   🛑 Reached --max-products cap (term not marked done).", "yellow"))
            return False

        page += 1

    # Walked the full safety cap of pages without a natural end → treat as done.
    return True


def open_drughub_session(driver, args):
    """Open DrugHub once in the browser, let the operator solve the initial
    CAPTCHA, then capture cookies into a requests session for product fetches."""
    try:
        driver.delete_all_cookies()
    except Exception:
        pass

    print(colored(f"\n🌐 Opening DrugHub for session setup: {DRUGHUB_BASE}", "blue"))
    try:
        driver.get(DRUGHUB_BASE)
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
        print(colored(f"⚠️  Browser navigation failed: {exc}", "yellow"))
        print(colored("   Continuing so the session can still be captured.", "yellow"))

    if args.session_wait > 0:
        print(colored(f"⏳ Waiting {args.session_wait}s before collecting cookies...", "yellow"))
        time.sleep(args.session_wait)

    if args.manual:
        print(colored("\n🔐 Manual step: solve any CAPTCHA in the browser.", "yellow", attrs=["bold"]))
        print(colored("   When DrugHub loads normally, press Enter here.", "yellow"))
        input(colored("   Press Enter to continue...", "yellow"))

    cookies = extract_cookies(driver, do_quit=False)
    session = setup_requests_session(cookies, args.socks, args.socks_port, verify_ssl=not args.insecure)
    print(colored(f"✅ Session ready for {DRUGHUB_HOST} (cookies captured: {len(cookies)})", "green"))
    return session


def parse_args():
    parser = argparse.ArgumentParser(description="DrugHub keyword-search crawler")
    # Shared with scrape_simple.py
    parser.add_argument("--manual", action="store_true",
                        help="Open browser and pause for manual CAPTCHA solving on each search")
    parser.add_argument("--socks", action="store_true",
                        help="Use Tor SOCKS5 (default uses HTTP proxy on 8118)")
    parser.add_argument("--socks-port", type=int, default=TOR_SOCKS_PORT,
                        help="Tor SOCKS port (default: 9050; use 9150 for Tor Browser)")
    parser.add_argument("--page-timeout", type=int, default=300,
                        help="Selenium page load timeout in seconds")
    parser.add_argument("--tor-binary", type=str, default=None,
                        help="Path to Tor Browser firefox binary")
    parser.add_argument("--delay", type=float, default=2.0,
                        help="Delay between product requests in seconds (default: 2)")
    parser.add_argument("--workers", type=int, default=2,
                        help="Concurrent product fetchers (default: 2)")
    parser.add_argument("--session-wait", type=int, default=SESSION_WARMUP_SECONDS,
                        help="Seconds to wait after opening before collecting cookies (default: 30)")
    parser.add_argument("--disable-js", action="store_true",
                        help="Disable JavaScript in the browser (Firefox preference)")
    parser.add_argument("--insecure", action="store_true",
                        help="Skip TLS certificate verification (self-signed onion certs)")
    parser.add_argument("--keep-browser-open", action="store_true",
                        help="Leave Firefox open at the end")
    parser.add_argument("--max-products", type=int, default=None,
                        help="Global cap on products scraped (default: unlimited)")
    # Search-specific
    parser.add_argument("--keywords", type=Path, default=KEYWORDS_FILE,
                        help=f"Keywords JSON (default: {KEYWORDS_FILE})")
    parser.add_argument("--terms", type=str, default=None,
                        help="Comma-separated terms to search instead of the keywords file")
    parser.add_argument("--limit-terms", type=int, default=None,
                        help="Only search the first N not-yet-done terms (smoke testing / batching)")
    parser.add_argument("--restart", action="store_true",
                        help="Ignore and clear the progress checkpoint, re-searching every term")
    parser.add_argument("--progress-file", type=Path, default=PROGRESS_FILE,
                        help=f"Resume checkpoint of completed terms (default: {PROGRESS_FILE})")
    return parser.parse_args()


def main():
    args = parse_args()

    # Resolve the term list.
    if args.terms:
        terms = [t.strip() for t in args.terms.split(",") if t.strip()]
        print(colored(f"✅ Using {len(terms)} term(s) from --terms", "green"))
    else:
        if not args.keywords.exists():
            print(colored(f"❌ {args.keywords} not found!", "red"))
            return
        terms = load_search_terms(args.keywords)
        print(colored(f"✅ Loaded {len(terms)} search term(s) from {args.keywords}", "green"))
    # Resume: drop terms already fully searched in a previous run so a rerun
    # picks up where it left off instead of starting over.
    if args.restart and args.progress_file.exists():
        try:
            args.progress_file.unlink()
            print(colored(f"   --restart → cleared progress file {args.progress_file}", "yellow"))
        except Exception as exc:
            print(colored(f"   ⚠️  Could not clear progress file: {exc}", "yellow"))
    completed_keys = set() if args.restart else load_progress(args.progress_file)
    completed_terms = sorted({t for t in terms if t.lower() in completed_keys}, key=str.lower)
    if completed_keys:
        remaining = [t for t in terms if t.lower() not in completed_keys]
        print(colored(f"   ⏭️  Resuming: {len(terms) - len(remaining)} done, {len(remaining)} remaining", "cyan"))
        terms = remaining

    if args.limit_terms:
        terms = terms[: args.limit_terms]
        print(colored(f"   --limit-terms → searching next {len(terms)} term(s)", "yellow"))
    if not terms:
        print(colored("✅ All terms already searched — nothing to do (use --restart to redo).", "green"))
        return

    run_timestamp = time.strftime("%Y%m%d_%H%M%S")
    base_name, ext = os.path.splitext(PRODUCTS_HTML_FILE)
    output_file = f"{base_name}_{run_timestamp}{ext or '.json'}"

    print(colored("\n🚀 Starting DrugHub search crawl...", "cyan", attrs=["bold"]))
    print(colored(f"   Terms: {len(terms)} | Output: {output_file}", "white"))
    if args.insecure:
        print(colored("   WARNING: TLS verification disabled (--insecure)", "yellow"))

    driver = None
    all_products = []
    scraped_urls = set()

    try:
        options = build_firefox_options(
            use_socks=args.socks,
            socks_port=args.socks_port,
            tor_binary=args.tor_binary,
            disable_js=args.disable_js,
        )
        driver = webdriver.Firefox(options=options)
        driver.set_page_load_timeout(args.page_timeout)

        session = open_drughub_session(driver, args)

        for i, term in enumerate(terms, 1):
            print(colored(f"\n[{i}/{len(terms)}]", "white", attrs=["bold"]), end=" ")
            completed = crawl_search_term(driver, session, term, args, scraped_urls, all_products)
            if completed:
                # Checkpoint immediately so an interrupt/crash keeps this term done.
                completed_terms.append(term)
                completed_keys.add(term.lower())
                save_progress(args.progress_file, completed_terms)
            if args.max_products and len(all_products) >= args.max_products:
                print(colored("\n🛑 Global --max-products cap reached; stopping.", "yellow"))
                break

        print(colored(f"\n{'='*80}", "cyan"))
        print(colored("💾 SAVING RESULTS", "cyan", attrs=["bold"]))
        print(colored(f"{'='*80}", "cyan"))
        save_products_html(all_products, output_file, overwrite=True)
        print(colored("\n✅ Search crawl complete!", "green", attrs=["bold"]))
        print(colored(f"   Total products scraped: {len(all_products)}", "green"))
        print(colored(f"   Saved to: {output_file}", "green"))

    except KeyboardInterrupt:
        print(colored("\n\n⚠️  Interrupted by user", "yellow"))
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
                    input(colored("\n👋 Press Enter once you're ready to close Firefox...", "yellow"))
                except EOFError:
                    pass
            try:
                driver.quit()
            except Exception:
                pass
        elif driver and args.keep_browser_open:
            print(colored("\nℹ️  Leaving Firefox open (--keep-browser-open).", "yellow"))


if __name__ == "__main__":
    main()
