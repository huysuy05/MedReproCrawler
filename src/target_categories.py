#!/usr/bin/env python3
"""
Category-targeting helper for Flow 1 (crawl -> filter).

Flow 1's only weakness is wasted fetches: crawling irrelevant categories over slow
Tor. Given a market you've confirmed authentic, this tool enumerates that market's
category pages, flags the research-relevant ones (pharmacy / prescription / health /
women's / ...), optionally expands pagination, and produces a reviewed pages_url.json
so scrape_simple.py crawls only what matters.

Complements discover_markets.py (breadth across markets) with depth inside one market.
Human-in-the-loop: writes a report by default; only touches pages_url.json with --write.

Examples:
  python src/target_categories.py http://<onion>/ --socks --socks-port 9150 --insecure
  python src/target_categories.py http://<onion>/ --socks --socks-port 9150 --insecure --expand-pages --write
  python src/target_categories.py http://<onion>/ --socks --socks-port 9150 --insecure --manual
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import time
import urllib.parse
from pathlib import Path
from typing import Dict, List, Optional

from bs4 import BeautifulSoup
from termcolor import colored

# Sibling modules (same src/ dir is on sys.path when run as a script).
from scrape_simple import setup_requests_session, extract_cookies, build_firefox_options
from discover_markets import CATEGORY_HINTS, is_walled, clean, probe

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "data"
PAGES_URL_FILE = DATA_DIR / "config" / "pages_url.json"
OUT_JSON = DATA_DIR / "discovery" / "category_candidates.json"
OUT_CSV = DATA_DIR / "discovery" / "category_candidates.csv"

# Tight, research-specific terms for flagging WHICH categories are worth crawling
# (narrower than discover_markets.CATEGORY_HINTS, which is for finding category links).
CATEGORY_RELEVANCE = (
    "pharmacy", "pharma", "prescription", "rx", "health", "pill", "medicine",
    "medication", "drug", "hormone", "fertility", "women", "contracept", "abortion",
    "birth control", "sexual", "steroid",
)

# Mirrors the pagination selectors used in scrape_simple.scrape_category_page().
PAGINATION_SELECTORS = (
    'a[rel="next"]', 'a.next', 'li.next a',
    '.pagination a', 'ul.pagination a', 'nav a', 'a[aria-label="Next"]',
)

# Anti-DDoS "access queue"/waiting-room and JS-gate phrases. These pages return 200
# and can be large (so is_walled misses them) but expose no real links.
GATE_HINTS = (
    "access queue", "you have been placed in a queue", "estimated wait",
    "please wait", "just a moment", "one moment", "redirected",
    "ddos", "captcha", "cloudflare", "enable javascript",
)


def looks_gated(status: Optional[int], html: Optional[str]) -> bool:
    """True if the page is a login/CAPTCHA wall or an anti-DDoS access queue."""
    if is_walled(status, html):
        return True
    if not html:
        return False
    soup = BeautifulSoup(html, "html.parser")
    anchors = soup.find_all("a", href=True)
    text = soup.get_text(" ", strip=True).lower()
    # A near-linkless page that talks about queues/waiting/redirects is a gate.
    return len(anchors) <= 2 and any(hint in text for hint in GATE_HINTS)


def capture_session_manual(market_url, args):
    """Launch Firefox via Tor, let the user solve login/CAPTCHA, return a cookie'd session."""
    from selenium import webdriver  # local import: only needed for --manual

    options = build_firefox_options(
        use_socks=args.socks, socks_port=args.socks_port,
        tor_binary=args.tor_binary, disable_js=args.disable_js,
    )
    print(colored(f"🌐 Opening {market_url} in Firefox for manual login/CAPTCHA...", "blue"))
    driver = webdriver.Firefox(options=options)
    driver.set_page_load_timeout(args.timeout)
    try:
        try:
            driver.get(market_url)
        except Exception:
            print(colored("⏱️  Page load slow/failed; continuing so you can still solve it.", "yellow"))
        input(colored("   Solve login/CAPTCHA in the browser, then press Enter here...", "yellow"))
        cookies = extract_cookies(driver, do_quit=True)
    finally:
        try:
            driver.quit()
        except Exception:
            pass
    print(colored(f"✅ Captured {len(cookies)} cookie(s)", "green"))
    return setup_requests_session(cookies, args.socks, args.socks_port, verify_ssl=not args.insecure)


# Navigation / account / meta path segments that are NOT product categories.
# Markets often use plain slugs (/electronics, /documents) for real categories, so
# we include shallow internal links by default and exclude these instead.
NON_CATEGORY_SEGMENTS = frozenset((
    "login", "register", "signup", "signin", "sign-in", "signout", "logout", "auth",
    "profile", "account", "myaccount", "dashboard", "settings", "setting",
    "seller", "sellers", "buyer", "buyers",
    "cart", "checkout", "basket", "order", "orders", "wishlist",
    "support", "help", "faq", "about", "about-us", "contact", "terms", "tos",
    "privacy", "rules", "forum", "forums", "wiki", "hiddenwiki", "thread", "post",
    "message", "messages", "inbox", "chat", "escrow", "dispute",
    "proof", "proof-reviews", "review", "reviews", "feedback",
    "freemoney", "free-money", "download", "downloads", "home", "index",
    "vendor", "vendors", "become-a-vendor", "ticket", "tickets", "news",
    "search", "filter", "tag", "tags", "captcha", "2fa", "pgp",
))

# Product-detail patterns (these are items, not categories).
PRODUCT_DETAIL_RE = re.compile(r"/(product|item|listing)/|action=view|[0-9a-f-]{36}", re.IGNORECASE)


def enumerate_categories(html: str, base_url: str) -> List[Dict[str, str]]:
    """Candidate category links: shallow same-host internal links that aren't nav,
    account, or product-detail pages. Markets use varied URL schemes (plain slugs
    like /electronics as well as /product-category/...), so we include broadly and
    let relevance flagging + human review narrow it down."""
    soup = BeautifulSoup(html, "html.parser")
    base_host = urllib.parse.urlparse(base_url).netloc
    out: List[Dict[str, str]] = []
    seen = set()
    for a in soup.find_all("a", href=True):
        full = urllib.parse.urljoin(base_url, a["href"])
        parsed = urllib.parse.urlparse(full)
        if parsed.netloc != base_host:
            continue
        path = parsed.path.strip("/")
        if not path:  # homepage
            continue
        low = full.lower()
        if PRODUCT_DETAIL_RE.search(low):
            continue
        segments = path.split("/")
        if any(seg.lower() in NON_CATEGORY_SEGMENTS for seg in segments):
            continue
        is_hint = any(hint in low for hint in CATEGORY_HINTS)
        # Deep paths are usually products/sub-content unless they look category-ish.
        if len(segments) > 2 and not is_hint:
            continue
        key = full.split("#")[0]
        if key in seen:
            continue
        seen.add(key)
        out.append({"url": key, "name": clean(a.get_text())})
    return out


def is_relevant(category: Dict[str, str]) -> bool:
    hay = f"{category['url']} {category['name']}".lower()
    return any(term in hay for term in CATEGORY_RELEVANCE)


def collect_page_urls(html: str, category_url: str) -> List[str]:
    """All same-host pagination URLs linked from a category page (plus the page itself)."""
    soup = BeautifulSoup(html, "html.parser")
    host = urllib.parse.urlparse(category_url).netloc
    urls = {category_url}
    for selector in PAGINATION_SELECTORS:
        for a in soup.select(selector):
            href = a.get("href")
            if not href:
                continue
            full = urllib.parse.urljoin(category_url, href)
            if urllib.parse.urlparse(full).netloc == host:
                urls.add(full)
    return sorted(urls)


def expand_numeric_range(urls: List[str], cap: int) -> List[str]:
    """Detect the dominant '...<int>...' pagination template and fill its min..max range.

    Handles both /prescription/2/ ... /38/ and ?page=2 ... styles. Falls back to the
    raw collected URLs when no clear numeric template is present.
    """
    templates: Dict[str, set] = {}
    for url in urls:
        matches = list(re.finditer(r"\d+", url))
        if not matches:
            continue
        last = matches[-1]
        key = url[: last.start()] + "{n}" + url[last.end():]
        templates.setdefault(key, set()).add(int(last.group()))
    if not templates:
        return sorted(urls)
    key = max(templates, key=lambda k: len(templates[k]))
    ints = templates[key]
    if len(ints) < 2:
        return sorted(urls)
    lo, hi = min(ints), min(max(ints), min(ints) + cap - 1)
    return [key.replace("{n}", str(i)) for i in range(lo, hi + 1)]


def write_reports(rows: List[dict]) -> None:
    OUT_JSON.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    print(colored(f"✅ Wrote {OUT_JSON}", "green"))
    columns = ["relevant", "name", "url", "pages_detected", "page_urls"]
    with OUT_CSV.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow({
                "relevant": row.get("relevant", False),
                "name": row.get("name", ""),
                "url": row.get("url", ""),
                "pages_detected": row.get("pages_detected", 1),
                "page_urls": " | ".join(row.get("page_urls", [])),
            })
    print(colored(f"✅ Wrote {OUT_CSV}", "green"))


def print_table(rows: List[dict]) -> None:
    print(colored(f"\n{'='*90}", "cyan"))
    print(colored("CATEGORIES (✔ = flagged relevant; review before crawling)", "cyan", attrs=["bold"]))
    print(colored(f"{'='*90}", "cyan"))
    print(colored(f"{'REL':<4} {'PAGES':>5}  {'NAME':<30}  URL", "white", attrs=["bold"]))
    for row in rows:
        mark = "✔" if row.get("relevant") else " "
        color = "green" if row.get("relevant") else "white"
        name = (row.get("name") or "")[:30]
        print(colored(f"{mark:<4} {row.get('pages_detected', 1):>5}  {name:<30}  {row.get('url','')}", color))


def write_pages_url(rows: List[dict]) -> None:
    """Append the relevant categories' page URLs to pages_url.json (deduped)."""
    existing: List[str] = []
    if PAGES_URL_FILE.exists():
        try:
            existing = json.loads(PAGES_URL_FILE.read_text(encoding="utf-8"))
        except Exception:
            existing = []
    existing_set = set(existing)
    added = 0
    for row in rows:
        if not row.get("relevant"):
            continue
        for url in row.get("page_urls") or [row["url"]]:
            if url not in existing_set:
                existing.append(url)
                existing_set.add(url)
                added += 1
    PAGES_URL_FILE.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
    print(colored(f"✅ Added {added} URL(s) to {PAGES_URL_FILE}", "green"))


def main() -> None:
    ap = argparse.ArgumentParser(description="Enumerate and flag a market's relevant categories for Flow 1.")
    ap.add_argument("market_url", help="Market base URL (an onion you've confirmed authentic)")
    ap.add_argument("--socks", action="store_true", help="Use Tor SOCKS5 (default uses HTTP proxy 8118)")
    ap.add_argument("--socks-port", type=int, default=9050, help="Tor SOCKS port (9050 system Tor, 9150 Tor Browser)")
    ap.add_argument("--insecure", action="store_true", help="Skip TLS verification (self-signed market certs)")
    ap.add_argument("--timeout", type=int, default=60, help="Per-request timeout in seconds (default 60)")
    ap.add_argument("--manual", action="store_true", help="Launch Firefox to solve login/CAPTCHA and capture a session")
    ap.add_argument("--disable-js", action="store_true", help="Disable JavaScript in the --manual browser")
    ap.add_argument("--tor-binary", type=str, default=None, help="Path to Tor Browser firefox binary (for --manual)")
    ap.add_argument("--depth", type=int, default=1, help="1 = homepage only; 2 = also follow each category once for sub-categories")
    ap.add_argument("--expand-pages", action="store_true", help="Expand each relevant category's pagination into explicit page URLs")
    ap.add_argument("--page-cap", type=int, default=200, help="Max pages to expand per category (safety cap, default 200)")
    ap.add_argument("--write", action="store_true", help="Append relevant categories' URLs to pages_url.json")
    args = ap.parse_args()

    if args.manual:
        session = capture_session_manual(args.market_url, args)
    else:
        session = setup_requests_session({}, use_socks=args.socks, socks_port=args.socks_port,
                                         verify_ssl=not args.insecure)

    print(colored(f"\n📄 Fetching homepage: {args.market_url}", "cyan"))
    status, home = probe(session, args.market_url, args.timeout)
    if home is None:
        print(colored(f"❌ Could not reach {args.market_url} (status={status}). "
                      f"Check the onion / try --socks-port 9150.", "red"))
        return
    if looks_gated(status, home):
        if args.manual:
            print(colored("🚧 Still gated after manual session — the access queue may need more "
                          "time. Wait for the browser to pass the queue, then re-run.", "yellow", attrs=["bold"]))
        else:
            print(colored("🚧 This market is gated (login / CAPTCHA / anti-DDoS access queue). "
                          "Re-run with --manual to pass it in a browser and capture a session.",
                          "yellow", attrs=["bold"]))
        return

    categories = enumerate_categories(home, args.market_url)

    # Optional one level deeper: follow each found category once to catch sub-categories.
    if args.depth >= 2:
        seen = {c["url"] for c in categories}
        for cat in list(categories):
            _, html = probe(session, cat["url"], args.timeout)
            if not html:
                continue
            for sub in enumerate_categories(html, args.market_url):
                if sub["url"] not in seen:
                    seen.add(sub["url"])
                    categories.append(sub)
            time.sleep(0.5)

    print(colored(f"🗂️  Found {len(categories)} category link(s)", "green"))

    rows: List[dict] = []
    for cat in categories:
        relevant = is_relevant(cat)
        row = {"url": cat["url"], "name": cat["name"], "relevant": relevant,
               "pages_detected": 1, "page_urls": [cat["url"]]}
        if relevant and args.expand_pages:
            _, html = probe(session, cat["url"], args.timeout)
            if html:
                page_urls = expand_numeric_range(collect_page_urls(html, cat["url"]), args.page_cap)
                # Always keep the category index page (page 1) even if pagination
                # links only exposed pages 2..N.
                if cat["url"] not in page_urls:
                    page_urls = [cat["url"]] + page_urls
                row["page_urls"] = page_urls
                row["pages_detected"] = len(page_urls)
            time.sleep(0.5)
        rows.append(row)

    # Relevant first, then by detected page count.
    rows.sort(key=lambda r: (r["relevant"], r["pages_detected"]), reverse=True)

    write_reports(rows)
    print_table(rows)

    relevant_count = sum(1 for r in rows if r["relevant"])
    if args.write:
        write_pages_url(rows)
    else:
        print(colored(f"\nℹ️  {relevant_count} categories flagged relevant. Review {OUT_CSV.name}, "
                      f"then re-run with --write to add them to pages_url.json.", "yellow"))


if __name__ == "__main__":
    main()
