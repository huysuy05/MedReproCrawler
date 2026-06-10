#!/usr/bin/env python3
"""
Marketplace discovery tool for the contraception/abortion research pipeline.

Automates the (otherwise manual) job of finding dark-web marketplaces worth
crawling. Four stages:

  A. Seed   - gather candidate .onion markets from curated directories
              (dark.fail / tor.taxi / daunt) and an onion search engine (Ahmia),
              configured in data/discovery_sources.json.
  B. Live   - probe each candidate's homepage through Tor; drop dead ones.
  C. Score  - generic crawl of each live market (homepage + a few category
              pages), keyword-matched against data/search_keywords.json using the
              same regex machinery as filter_medicines.py. Score = number of
              unique medicine terms found.
  D. Report - rank by score and write data/candidate_markets.json + .csv and a
              console table. Promotion to data/pages_url.json stays MANUAL
              (human-in-the-loop) - confirm a market's onion is authentic (not a
              phishing clone) before crawling it. --promote is an opt-in helper.

Reuses Tor/session plumbing from scrape_simple.py and pattern matching from
filter_medicines.py - no new dependencies.

Example:
  python src/discover_markets.py --socks --socks-port 9150 --insecure
  python src/discover_markets.py --socks --socks-port 9150 --insecure --seeds-only
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import time
import urllib.parse
from pathlib import Path
from typing import Dict, List, Optional, Sequence

import requests
from bs4 import BeautifulSoup
from termcolor import colored

# Sibling modules (same src/ dir is on sys.path when run as a script).
from scrape_simple import setup_requests_session, extract_product_links
from filter_medicines import build_patterns, load_term_groups

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "data"
SOURCES_FILE = DATA_DIR / "config" / "discovery_sources.json"
KEYWORDS_FILE = DATA_DIR / "config" / "search_keywords.json"
PAGES_URL_FILE = DATA_DIR / "config" / "pages_url.json"
OUT_JSON = DATA_DIR / "discovery" / "candidate_markets.json"
OUT_CSV = DATA_DIR / "discovery" / "candidate_markets.csv"

# v3 onions are 56 chars, legacy v2 are 16 (base32 alphabet a-z2-7).
ONION_RE = re.compile(r"\b([a-z2-7]{16}(?:[a-z2-7]{40})?\.onion)\b", re.IGNORECASE)
# Internal links that look like category/listing pages worth crawling/promoting.
CATEGORY_HINTS = (
    "category", "categories", "shop", "products", "product-category",
    "drugs", "medicine", "medicines", "pharmacy", "listing", "listings",
    "prescription", "rx", "store",
)
WALL_HINTS = ("captcha", "cloudflare", "ddos", "are you human", "not a bot", "i'm not a robot")


def clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def load_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def probe(session: requests.Session, url: str, timeout: int) -> tuple[Optional[int], Optional[str]]:
    """Single GET through Tor. Returns (status_code, html) or (None, None) on failure."""
    try:
        resp = session.get(url, timeout=timeout, allow_redirects=True)
        return resp.status_code, resp.text
    except requests.exceptions.RequestException:
        return None, None


# ---------------------------------------------------------------------------
# Stage A: seed candidates
# ---------------------------------------------------------------------------

def extract_onions(html: str) -> Dict[str, str]:
    """Map onion host -> a usable http URL, from every onion mention in the HTML."""
    found: Dict[str, str] = {}
    soup = BeautifulSoup(html, "html.parser")

    def record(host: str, name: str = "") -> None:
        host = host.lower()
        if host not in found:
            found[host] = name

    # Anchors first (so we can grab link text as the market name).
    for a in soup.find_all("a", href=True):
        for m in ONION_RE.finditer(a["href"]):
            record(m.group(1), clean(a.get_text()))
    # Then any onion mentioned anywhere in the raw text (directories sometimes
    # print the address as plain text next to a copy button).
    for m in ONION_RE.finditer(html):
        record(m.group(1))
    return found


def seed_candidates(session, sources: dict, timeout: int) -> Dict[str, dict]:
    """Gather candidate markets from directories + Ahmia. Keyed by onion host."""
    candidates: Dict[str, dict] = {}

    def add(host: str, name: str, source: str) -> None:
        host = host.lower()
        entry = candidates.setdefault(host, {
            "name": "", "onion_host": host, "onion_url": f"http://{host}/", "sources": set(),
        })
        entry["sources"].add(source)
        if name and not entry["name"]:
            entry["name"] = name

    # Directories (curated, verified market lists).
    for directory in sources.get("directories", []):
        print(colored(f"📂 Directory: {directory}", "cyan"))
        status, html = probe(session, directory, timeout)
        if not html:
            print(colored(f"   ⚠️  unreachable (status={status})", "yellow"))
            continue
        onions = extract_onions(html)
        print(colored(f"   found {len(onions)} onion(s)", "green"))
        for host, name in onions.items():
            add(host, name, f"directory:{urllib.parse.urlparse(directory).netloc}")

    # Search engines (keyword-driven). Each entry is a base URL; the URL-encoded
    # term is appended. Use server-rendered engines (e.g. Torch) - JS-only ones
    # like Ahmia's clearnet site return an empty shell to a plain HTTP fetch.
    engines = sources.get("search_engines") or ([sources["ahmia_base"]] if sources.get("ahmia_base") else [])
    terms = sources.get("search_query_terms") or sources.get("ahmia_query_terms", [])
    for base in engines:
        engine_name = urllib.parse.urlparse(base).netloc[:20]
        for term in terms:
            url = base + urllib.parse.quote(term)
            print(colored(f"🔎 {engine_name}: {term!r}", "cyan"))
            status, html = probe(session, url, timeout)
            if not html:
                print(colored(f"   ⚠️  no results (status={status})", "yellow"))
                continue
            onions = extract_onions(html)
            print(colored(f"   found {len(onions)} onion(s)", "green"))
            for host, name in onions.items():
                add(host, name, f"search:{term}")
            time.sleep(1)  # be polite to the engine

    return candidates


# ---------------------------------------------------------------------------
# Stage C: relevance score (generic crawl)
# ---------------------------------------------------------------------------

def find_category_links(html: str, base_url: str, limit: int) -> List[str]:
    """Same-host links that look like category/listing pages."""
    soup = BeautifulSoup(html, "html.parser")
    base_host = urllib.parse.urlparse(base_url).netloc
    links: List[str] = []
    seen = set()
    for a in soup.find_all("a", href=True):
        full = urllib.parse.urljoin(base_url, a["href"])
        parsed = urllib.parse.urlparse(full)
        if parsed.netloc != base_host:
            continue
        if any(hint in full.lower() for hint in CATEGORY_HINTS):
            if full not in seen:
                seen.add(full)
                links.append(full)
        if len(links) >= limit:
            break
    return links


def extract_sample_titles(html: str, base_url: str, limit: int = 5) -> List[str]:
    """A few human-readable listing titles for the report (sanity check)."""
    soup = BeautifulSoup(html, "html.parser")
    titles: List[str] = []
    selectors = [
        "li.product h2", "li.product h3", ".woocommerce-loop-product__title",
        "a.woocommerce-LoopProduct-link", "li.product a", ".product-title", "h2 a",
    ]
    for selector in selectors:
        for el in soup.select(selector):
            text = clean(el.get_text())
            if 3 < len(text) < 120 and text not in titles:
                titles.append(text)
            if len(titles) >= limit:
                return titles
    return titles


def is_walled(status: Optional[int], html: Optional[str]) -> bool:
    if status in (401, 403):
        return True
    if html and len(html) < 4000:
        low = html.lower()
        if any(h in low for h in WALL_HINTS):
            return True
    return False


def score_market(session, base_url: str, patterns, max_pages: int, timeout: int) -> dict:
    """Crawl homepage + a few category pages, count unique keyword matches."""
    status, home = probe(session, base_url, timeout)
    if home is None:
        return {"status": "dead", "http_status": status, "score": None}
    if is_walled(status, home):
        return {"status": "wall/manual", "http_status": status, "score": None,
                "candidate_category_urls": [], "sample_titles": []}

    texts = [home]
    sample_titles = extract_sample_titles(home, base_url)
    category_urls = find_category_links(home, base_url, limit=max(max_pages * 2, 10))

    for page_url in category_urls[: max(max_pages - 1, 0)]:
        status_p, html_p = probe(session, page_url, timeout)
        if html_p:
            texts.append(html_p)
            if not sample_titles:
                sample_titles = extract_sample_titles(html_p, base_url)
        time.sleep(0.5)

    haystack = "\n".join(BeautifulSoup(t, "html.parser").get_text(" ", strip=True) for t in texts)
    matched_terms = set()
    matched_categories = set()
    for category, term, pattern in patterns:
        if pattern.search(haystack):
            matched_terms.add(term)
            matched_categories.add(category)

    return {
        "status": "scored",
        "http_status": status,
        "score": len(matched_terms),
        "matched_terms": sorted(matched_terms),
        "matched_categories": sorted(matched_categories),
        "sample_titles": sample_titles,
        "candidate_category_urls": category_urls,
    }


# ---------------------------------------------------------------------------
# Stage D: report + optional promotion
# ---------------------------------------------------------------------------

def write_reports(rows: List[dict]) -> None:
    OUT_JSON.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    print(colored(f"✅ Wrote {OUT_JSON}", "green"))

    columns = [
        "name", "onion_url", "sources", "search_hits", "status", "http_status", "score",
        "matched_categories", "sample_titles", "candidate_category_urls",
    ]
    with OUT_CSV.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow({
                "name": row.get("name", ""),
                "onion_url": row.get("onion_url", ""),
                "sources": "; ".join(sorted(row.get("sources", []))),
                "search_hits": row.get("search_hits", 0),
                "status": row.get("status", ""),
                "http_status": row.get("http_status", ""),
                "score": "" if row.get("score") is None else row.get("score"),
                "matched_categories": "; ".join(row.get("matched_categories", [])),
                "sample_titles": " | ".join(row.get("sample_titles", [])),
                "candidate_category_urls": " | ".join(row.get("candidate_category_urls", [])),
            })
    print(colored(f"✅ Wrote {OUT_CSV}", "green"))


def print_table(rows: List[dict]) -> None:
    print(colored(f"\n{'='*100}", "cyan"))
    print(colored("RANKED CANDIDATES (review authenticity before promoting!)", "cyan", attrs=["bold"]))
    print(colored(f"{'='*100}", "cyan"))
    header = f"{'SCORE':>5}  {'HITS':>4}  {'STATUS':<11}  {'NAME':<28}  ONION"
    print(colored(header, "white", attrs=["bold"]))
    for row in rows:
        score = row.get("score")
        score_str = "  -  " if score is None else f"{score:>5}"
        hits = row.get("search_hits", 0)
        color = "green" if (score or 0) > 0 else ("yellow" if (row.get("status") == "wall/manual" or hits > 0) else "white")
        name = (row.get("name") or "")[:28]
        print(colored(f"{score_str}  {hits:>4}  {row.get('status',''):<11}  {name:<28}  {row.get('onion_url','')}", color))


def promote(hosts: Sequence[str], rows: List[dict]) -> None:
    """Append the chosen markets' category URLs to pages_url.json (deduped)."""
    by_host = {r["onion_host"]: r for r in rows}
    existing: List[str] = []
    if PAGES_URL_FILE.exists():
        try:
            existing = load_json(PAGES_URL_FILE)
        except Exception:
            existing = []
    existing_set = set(existing)
    added = 0
    for host in hosts:
        row = by_host.get(host.lower().replace("http://", "").replace("https://", "").strip("/"))
        if not row:
            print(colored(f"⚠️  {host} not in candidates; skipping", "yellow"))
            continue
        urls = row.get("candidate_category_urls") or [row["onion_url"]]
        for url in urls:
            if url not in existing_set:
                existing.append(url)
                existing_set.add(url)
                added += 1
    PAGES_URL_FILE.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
    print(colored(f"✅ Promoted {added} URL(s) into {PAGES_URL_FILE}", "green"))


def main() -> None:
    ap = argparse.ArgumentParser(description="Discover candidate dark-web marketplaces to crawl.")
    ap.add_argument("--socks", action="store_true", help="Use Tor SOCKS5 (default uses HTTP proxy 8118)")
    ap.add_argument("--socks-port", type=int, default=9050, help="Tor SOCKS port (9050 system Tor, 9150 Tor Browser)")
    ap.add_argument("--insecure", action="store_true", help="Skip TLS verification (needed for self-signed market certs)")
    ap.add_argument("--timeout", type=int, default=60, help="Per-request timeout in seconds (default 60)")
    ap.add_argument("--max-candidates", type=int, default=40, help="Cap how many seeded onions to score (default 40)")
    ap.add_argument("--max-pages-per-market", type=int, default=5, help="Pages to crawl per market when scoring (default 5)")
    ap.add_argument("--seeds-only", action="store_true", help="Stop after Stage A (dry run; no liveness/scoring)")
    ap.add_argument("--promote", nargs="+", metavar="ONION", default=None,
                    help="After reporting, append these onion hosts' category URLs to pages_url.json")
    args = ap.parse_args()

    sources = load_json(SOURCES_FILE)
    term_groups = load_term_groups(KEYWORDS_FILE)
    patterns = build_patterns(term_groups)
    print(colored(f"🔑 Loaded {sum(len(v) for v in term_groups.values())} keywords "
                  f"across {len(term_groups)} categories", "green"))

    session = setup_requests_session({}, use_socks=args.socks, socks_port=args.socks_port,
                                     verify_ssl=not args.insecure)

    # Stage A
    print(colored("\n=== Stage A: seeding candidates ===", "cyan", attrs=["bold"]))
    candidates = seed_candidates(session, sources, args.timeout)
    print(colored(f"\n🌱 {len(candidates)} unique candidate market(s) seeded", "green", attrs=["bold"]))

    rows = list(candidates.values())
    for r in rows:
        r["sources"] = sorted(r["sources"])
        # How many distinct medicine searches surfaced this market - a relevance
        # signal that survives even when the market gates listings behind login
        # (so the homepage crawl scores 0).
        r["search_hits"] = sum(1 for s in r["sources"] if s.startswith("search:"))

    # Score keyword-search hits first: a market that a search engine returned for
    # "Misoprostol" is likelier relevant than a generic directory listing, so it
    # should win a slot within the --max-candidates budget.
    rows.sort(key=lambda r: any(s.startswith("search:") for s in r["sources"]), reverse=True)

    if args.seeds_only:
        write_reports(rows)
        print_table(rows)
        return

    rows = rows[: args.max_candidates]

    # Stages B + C
    print(colored(f"\n=== Stages B+C: liveness + scoring ({len(rows)} markets) ===", "cyan", attrs=["bold"]))
    for i, row in enumerate(rows, 1):
        print(colored(f"[{i}/{len(rows)}] {row['onion_url']}", "blue"))
        result = score_market(session, row["onion_url"], patterns,
                              args.max_pages_per_market, args.timeout)
        row.update(result)
        msg = f"   status={result['status']} score={result.get('score')}"
        print(colored(msg, "green" if (result.get("score") or 0) > 0 else "yellow"))

    # Stage D - rank by crawl score, then by how many medicine searches surfaced it.
    def sort_key(r: dict):
        return (r.get("score") if r.get("score") is not None else -1, r.get("search_hits", 0))
    rows.sort(key=sort_key, reverse=True)

    write_reports(rows)
    print_table(rows)

    if args.promote:
        promote(args.promote, rows)
    else:
        print(colored(f"\nℹ️  Review {OUT_CSV.name}, confirm onions are authentic, then either copy "
                      f"category URLs into pages_url.json or re-run with --promote <onion> ...", "yellow"))


if __name__ == "__main__":
    main()
