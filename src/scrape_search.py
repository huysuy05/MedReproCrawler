#!/usr/bin/env python3
"""
Keyword-search crawler for dark web marketplaces.

Unlike scrape_simple.py (which walks pre-listed category pages from
data/config/pages_url.json), this script crawls a market by SEARCH TERM. For each
keyword in data/config/search_keywords.json, and for each selected market, it:

  1. Loads the search-results page in the live browser, pausing for manual CAPTCHA
     handling (--manual) on each search.
  2. Reads how many listings the search returned (per-market selector below).
  3. Only if the count > 0, walks every result page and fetches each product page.

Supported markets (see MARKETS registry):
  - drughub : /?search_terms=<term>&search=simple ; count in <h1 class="h2 m-0 mb-1">
  - xwave   : /?s=<term>&post_type=product        ; count in <p class="woocommerce-result-count">

Output is written in the SAME raw record format and `products_html_<timestamp>.json`
naming as scrape_simple.py, so the existing pipeline picks it up with no changes:

    python3 src/scrape_search.py --market all --manual --socks --socks-port 9150 --insecure
    python3 src/merge_html_sessions.py
    python3 src/parser.py
    python3 src/filter_medicines.py
    python3 src/push_to_sheets.py
"""

import argparse
import json
import os
import random
import re
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

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


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "data"
KEYWORDS_FILE = DATA_DIR / "config" / "search_keywords.json"
PRODUCTS_HTML_FILE = DATA_DIR / "raw" / "products_html.json"
# Resume checkpoint: which terms have already been fully searched, PER MARKET, so
# reruns pick up where the last run left off instead of re-searching everything.
PROGRESS_FILE = DATA_DIR / "search_progress.json"


# --------------------------------------------------------------------------- #
# Per-market result-count parsers
# --------------------------------------------------------------------------- #

def parse_count_drughub(html):
    """Result count from DrugHub's header `<h1 class="h2 m-0 mb-1"><strong>N</strong> Listings</h1>`.

    That h1 class is ALSO used for the product-page title, so we only accept the
    element when its text mentions "Listing" -- otherwise we'd mis-read a product
    title as a count. None means the header wasn't found (unknown).
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


def parse_count_woocommerce(html):
    """Result count from WooCommerce's `<p class="woocommerce-result-count">` text.

    Handles the three standard phrasings (X Wave Market and any WooCommerce shop):
      - "Showing all 10 results"            -> 10
      - "Showing the single result"         -> 1
      - "Showing 1–12 of 30 results"        -> 30 (the total, not the page slice)
    None means the element wasn't found (unknown).
    """
    if not html:
        return None
    soup = BeautifulSoup(html, "html.parser")
    el = soup.select_one("p.woocommerce-result-count")
    if not el:
        return None
    text = " ".join(el.get_text().split())
    low = text.lower()
    if "single result" in low:
        return 1
    # "1–12 of 30 results" → the total after "of"
    m = re.search(r"of\s+([\d,]+)\s+results?", low)
    if m:
        return int(m.group(1).replace(",", ""))
    # "all 10 results"
    m = re.search(r"all\s+([\d,]+)\s+results?", low)
    if m:
        return int(m.group(1).replace(",", ""))
    # Fallback: any "<number> result(s)"
    m = re.search(r"([\d,]+)\s+results?", low)
    if m:
        return int(m.group(1).replace(",", ""))
    return None


# --------------------------------------------------------------------------- #
# Per-market search-URL builders
# --------------------------------------------------------------------------- #

def _drughub_search_url(base, term, page):
    url = f"{base}/?search_terms={urllib.parse.quote(term)}&search=simple"
    if page > 1:
        url += f"&page={page}"
    return url


def _xwave_search_url(base, term, page):
    # WooCommerce query-string search paginates via /page/N/.
    q = f"?s={urllib.parse.quote(term)}&post_type=product"
    if page > 1:
        return f"{base}/page/{page}/{q}"
    return f"{base}/{q}"


# --------------------------------------------------------------------------- #
# Market registry
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Market:
    key: str                                  # CLI/progress identifier
    name: str                                 # human label
    base: str                                 # onion root URL (no trailing slash)
    search_url: Callable[[str, str, int], str]  # (base, term, page) -> URL
    count_parser: Callable[[str], Optional[int]]  # (html) -> int | None

    @property
    def host(self):
        return urllib.parse.urlparse(self.base).netloc


MARKETS = {
    "drughub": Market(
        key="drughub",
        name="Drug Hub",
        base="http://drughubdzlrwp2pyserbkmc2sxbxzrjvutirthcyqn6c2p46qcc7mlyd.onion",
        search_url=_drughub_search_url,
        count_parser=parse_count_drughub,
    ),
    "xwave": Market(
        key="xwave",
        name="The X Wave Market",
        base="http://hs7mhjhab5tpowkgmk5hrholfcdmgedp73hr6czrsrbr2kopzbrv3byd.onion",
        search_url=_xwave_search_url,
        count_parser=parse_count_woocommerce,
    ),
}


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
    """Load completed search terms PER MARKET as {market_key: [terms]}.

    Tolerates a missing/unreadable file (returns {}) and migrates the old flat
    {"completed_terms": [...]} format (assumed to be DrugHub's).
    """
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception as exc:
        print(colored(f"⚠️  Could not read progress file {path}: {exc} (starting fresh)", "yellow"))
        return {}
    if not isinstance(data, dict):
        return {}
    if isinstance(data.get("markets"), dict):
        return {str(k): list(v) for k, v in data["markets"].items()}
    # Legacy flat format → treat as DrugHub progress.
    if isinstance(data.get("completed_terms"), list):
        return {"drughub": list(data["completed_terms"])}
    return {}


def save_progress(path, progress):
    """Persist {market_key: [terms]} (original casing) so a rerun can resume."""
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        payload = {
            "markets": {k: sorted(v, key=str.lower) for k, v in progress.items()},
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
    except Exception as exc:
        print(colored(f"⚠️  Could not write progress file {path}: {exc}", "yellow"))


def crawl_search_term(driver, session, market, term, args, scraped_urls, all_products):
    """Crawl every listing returned for a single search `term` on `market`.

    Returns True if the term was fully searched (so it can be checkpointed as
    done), False if it stopped early for a reason worth retrying next run -- a
    fetch/CAPTCHA failure, an unreadable count, the global --max-products cap, or
    a count>0 page that yielded no links (likely a selector issue).
    """
    print(colored(f"\n{'='*80}", "cyan"))
    print(colored(f"[{market.name}] SEARCH: {term!r}", "cyan", attrs=["bold"]))
    print(colored(f"{'='*80}", "cyan"))

    walk_prev_sig = None
    page = 1
    count = None

    while page <= WALK_PAGE_CAP:
        search_page_url = market.search_url(market.base, term, page)
        # Browser fetch carries JS cookies/tokens and is where the operator solves
        # any CAPTCHA for THIS search (manual pause inside fetch_page_html_browser).
        html = fetch_page_html_browser(driver, search_page_url, manual=args.manual)
        if not html:
            print(colored(f"❌ Failed to fetch search page {page} for {term!r}", "red"))
            return False  # transient → retry next run

        # First page: gate the whole term on the reported result count.
        if page == 1:
            count = market.count_parser(html)
            if count is None:
                print(colored(f"   ⚠️  Could not read a result count for {term!r} — will retry next run.", "yellow"))
                return False
            print(colored(f"   🔢 {count} result(s) reported for {term!r}", "green", attrs=["bold"]))
            if count == 0:
                print(colored("   ⏭️  0 results → nothing to crawl (done).", "yellow"))
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
            data = scrape_product_page(session, p_url, search_page_url, market.host)
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


def _set_client_timeout(driver, seconds):
    """Raise the Selenium client→geckodriver HTTP timeout (best-effort).

    The attribute path has changed across Selenium versions, so try the known
    locations and stay silent if none apply -- it's a hardening tweak, not a hard
    requirement.
    """
    executor = getattr(driver, "command_executor", None)
    if executor is None:
        return
    # Selenium 4.15+: command_executor._client_config.timeout
    client_config = getattr(executor, "_client_config", None)
    if client_config is not None:
        try:
            client_config.timeout = seconds
            return
        except Exception:
            pass
    # Older Selenium: command_executor.set_timeout(seconds)
    setter = getattr(executor, "set_timeout", None)
    if callable(setter):
        try:
            setter(seconds)
            return
        except Exception:
            pass
    # Fallback: a plain timeout attribute on the executor.
    try:
        executor.timeout = seconds
    except Exception:
        pass


def open_market_session(driver, market, args):
    """Open `market` once in the browser, let the operator solve the initial
    CAPTCHA, then capture cookies into a requests session for product fetches."""
    try:
        driver.delete_all_cookies()
    except Exception:
        pass

    print(colored(f"\n🌐 Opening {market.name} for session setup: {market.base}", "blue"))
    try:
        driver.get(market.base)
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
        print(colored(f"\n🔐 Manual step: solve any CAPTCHA for {market.name} in the browser.", "yellow", attrs=["bold"]))
        print(colored("   When the site loads normally, press Enter here.", "yellow"))
        input(colored("   Press Enter to continue...", "yellow"))

    cookies = extract_cookies(driver, do_quit=False)
    session = setup_requests_session(cookies, args.socks, args.socks_port, verify_ssl=not args.insecure)
    print(colored(f"✅ Session ready for {market.host} (cookies captured: {len(cookies)})", "green"))
    return session


def parse_args():
    parser = argparse.ArgumentParser(description="Keyword-search crawler for dark web marketplaces")
    # Market selection
    parser.add_argument("--market", choices=list(MARKETS) + ["all"], default="all",
                        help="Which market(s) to search (default: all)")
    # Shared with scrape_simple.py
    parser.add_argument("--manual", action="store_true",
                        help="Open browser and pause for manual CAPTCHA solving on each search")
    parser.add_argument("--socks", action="store_true",
                        help="Use Tor SOCKS5 (default uses HTTP proxy on 8118)")
    parser.add_argument("--socks-port", type=int, default=TOR_SOCKS_PORT,
                        help="Tor SOCKS port (default: 9050; use 9150 for Tor Browser)")
    parser.add_argument("--page-timeout", type=int, default=600,
                        help="Selenium page load timeout in seconds (default: 600). The "
                             "client timeout auto-tracks this at +60s.")
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
                        help="Only search the next N not-yet-done terms per market (smoke testing / batching)")
    parser.add_argument("--restart", action="store_true",
                        help="Clear the progress checkpoint for the selected market(s) and re-search every term")
    parser.add_argument("--progress-file", type=Path, default=PROGRESS_FILE,
                        help=f"Resume checkpoint of completed terms (default: {PROGRESS_FILE})")
    return parser.parse_args()


def main():
    args = parse_args()
    selected = list(MARKETS) if args.market == "all" else [args.market]

    # Resolve the term list (shared across markets).
    if args.terms:
        all_terms = [t.strip() for t in args.terms.split(",") if t.strip()]
        print(colored(f"✅ Using {len(all_terms)} term(s) from --terms", "green"))
    else:
        if not args.keywords.exists():
            print(colored(f"❌ {args.keywords} not found!", "red"))
            return
        all_terms = load_search_terms(args.keywords)
        print(colored(f"✅ Loaded {len(all_terms)} search term(s) from {args.keywords}", "green"))
    if not all_terms:
        print(colored("❌ No terms to search. Exiting.", "red"))
        return

    # Resume checkpoint (per market). --restart clears only the selected markets.
    progress = load_progress(args.progress_file)
    if args.restart:
        for mk in selected:
            progress.pop(mk, None)
        save_progress(args.progress_file, progress)
        print(colored(f"   --restart → cleared progress for: {', '.join(selected)}", "yellow"))

    run_timestamp = time.strftime("%Y%m%d_%H%M%S")
    base_name, ext = os.path.splitext(PRODUCTS_HTML_FILE)
    output_file = f"{base_name}_{run_timestamp}{ext or '.json'}"

    print(colored("\n🚀 Starting search crawl...", "cyan", attrs=["bold"]))
    print(colored(f"   Markets: {', '.join(MARKETS[m].name for m in selected)}", "white"))
    print(colored(f"   Output: {output_file}", "white"))
    if args.insecure:
        print(colored("   WARNING: TLS verification disabled (--insecure)", "yellow"))

    driver = None
    all_products = []
    scraped_urls = set()  # shared dedup across markets (hosts differ, so no collisions)
    capped = False

    try:
        options = build_firefox_options(
            use_socks=args.socks,
            socks_port=args.socks_port,
            tor_binary=args.tor_binary,
            disable_js=args.disable_js,
        )
        driver = webdriver.Firefox(options=options)
        driver.set_page_load_timeout(args.page_timeout)
        # The Selenium client→geckodriver HTTP call has its OWN timeout (defaults
        # to ~120s) that is separate from the page-load timeout. On slow Tor pages
        # it would otherwise fire first and raise a raw urllib3 ReadTimeoutError.
        # Push it comfortably above the page-load timeout so the page-load timeout
        # (handled gracefully) is what governs.
        _set_client_timeout(driver, args.page_timeout + 60)

        for market_key in selected:
            market = MARKETS[market_key]
            done_list = list(progress.get(market_key, []))
            done_keys = {t.lower() for t in done_list}
            remaining = [t for t in all_terms if t.lower() not in done_keys]

            print(colored(f"\n{'#'*80}", "magenta"))
            print(colored(f"MARKET: {market.name}", "magenta", attrs=["bold"]))
            if done_keys:
                print(colored(f"   ⏭️  Resuming: {len(all_terms) - len(remaining)} done, {len(remaining)} remaining", "cyan"))
            if args.limit_terms:
                remaining = remaining[: args.limit_terms]
                print(colored(f"   --limit-terms → searching next {len(remaining)} term(s)", "yellow"))
            print(colored(f"{'#'*80}", "magenta"))

            if not remaining:
                print(colored(f"✅ All terms already searched for {market.name} (use --restart to redo).", "green"))
                continue

            session = open_market_session(driver, market, args)

            for i, term in enumerate(remaining, 1):
                print(colored(f"\n[{market.key} {i}/{len(remaining)}]", "white", attrs=["bold"]), end=" ")
                completed = crawl_search_term(driver, session, market, term, args, scraped_urls, all_products)
                if completed:
                    # Checkpoint immediately so an interrupt/crash keeps this term done.
                    done_list.append(term)
                    progress[market_key] = done_list
                    save_progress(args.progress_file, progress)
                if args.max_products and len(all_products) >= args.max_products:
                    print(colored("\n🛑 Global --max-products cap reached; stopping.", "yellow"))
                    capped = True
                    break

            if capped:
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
