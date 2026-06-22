# Quick Start - Simplified Scraper

This repository contains a lightweight scraper that loads category pages (from `pages_url.json`), extracts product links and saves each product page's raw HTML for later parsing. The scraper is built around Selenium (to capture cookies / solve CAPTCHAs manually) and requests (for faster subsequent fetches).

## Highlights / Recent changes

- Outputs are now saved with a timestamp suffix. Example: `products_html_20251024_235959.json`.
- `pages_url.json` is the single source of truth for which category/listing pages are visited. It has been expanded/cleaned in this workspace to include explicit page ranges (examples included in the repo):
  - prescription pages: `/prescription/2/` through `/prescription/38/`
  - steroids pages: `/steroids/` and `/steroids/2/` through `/steroids/17/`
  - one listing path was extended to `?page=0..155` for full coverage
- When using SOCKS mode (`--socks`), the Firefox instance launched for Selenium now sends DNS lookups through Tor (`network.proxy.socks_remote_dns = True`) so `.onion` hostnames resolve properly.

## Important files

- `scrape_simple.py` — main scraper. Use this instead of the older `scrape.py`/`scrape_old.py` if available.
- `discover_markets.py` — finds candidate marketplaces to crawl (see "Discovering marketplaces" below).
- `pages_url.json` — JSON array of category/listing URLs. Edit to add or remove pages; the scraper will NOT auto-advance beyond what's in this file.
- `search_keywords.json` — medicine keywords grouped by research category (`contraception`, `abortion`). Drives both discovery scoring and `filter_medicines.py`.
- `discovery_sources.json` — directories + search engine config for `discover_markets.py`.
- `run.sh` — helper script that checks for Tor and runs the scraper with recommended flags.
- `reqs.txt` — Python dependencies.

## Requirements

- Python 3.11+ (tested with 3.13 on macOS)
- GeckoDriver (compatible with your Firefox/Tor Browser)
- Tor (system Tor or Tor Browser)
- Python packages: `requests`, `beautifulsoup4`, `selenium`, `termcolor` (install with `pip install -r reqs.txt`)

## Quick run (system Tor + Privoxy)

1. Make sure Tor is running (system tor or Tor Browser). Example for Homebrew-managed Tor:

```bash
brew services start tor
```

2. (Optional) If you use Privoxy, ensure it forwards to Tor. In Privoxy config (`/opt/homebrew/etc/privoxy/config`) add:

```
forward-socks5t / 127.0.0.1:9050 .
```

3. Run the included script or call Python directly:

```bash
./run.sh
# or, directly:
python3 scrape_simple.py --socks --socks-port 9150 --manual --disable-js --insecure
```

Options explained:
- `--socks --socks-port 9150` — connect Firefox to Tor SOCKS5. Use `9050` for system Tor (Homebrew) or `9150` for Tor Browser's own Tor. See "Choosing the SOCKS port" below.
- `--manual` — opens the browser for manual CAPTCHA solving / login and waits for you to press Enter.
- `--disable-js` — disables JavaScript in Firefox (can speed up loads and reduce bot detection).
- `--insecure` — disable TLS certificate verification. **Usually required**: most markets redirect to HTTPS and serve self-signed certs, which otherwise fail with `CERTIFICATE_VERIFY_FAILED`. Safe on `.onion` because the onion address itself provides authentication.

## Choosing the SOCKS port (9050 vs 9150)

System Tor (Homebrew, port `9050`) and Tor Browser's bundled Tor (port `9150`) are **separate Tor clients** with independent circuits and hidden-service descriptor caches. A `.onion` that times out through one can be perfectly reachable through the other.

- If a site times out on `9050` but **loads fine in Tor Browser**, run the scraper with `--socks-port 9150` and keep Tor Browser open (port 9150 only exists while the Tor Browser app is running).
- To sanity-check which port reaches a host, compare them directly:

```bash
# 9050 = system Tor, 9150 = Tor Browser's Tor
curl -s -o /dev/null -w "HTTP %{http_code} in %{time_total}s\n" \
  --socks5-hostname 127.0.0.1:9050 --max-time 60 "http://<onion-host>/"
curl -s -o /dev/null -w "HTTP %{http_code} in %{time_total}s\n" \
  --socks5-hostname 127.0.0.1:9150 --max-time 60 "http://<onion-host>/"
```

A `HTTP 000` / timeout means that Tor client currently can't route to the service; a `2xx`/`3xx` means it can. Restarting system Tor (`brew services restart tor`) sometimes clears a stuck `9050`.
- `--page-timeout <seconds>` — increase if pages load slowly over Tor.
- `--session-wait <seconds>` — seconds to wait after opening a page before collecting cookies (default 60).

## Discovering marketplaces

Finding which markets to put in `pages_url.json` is otherwise manual. `discover_markets.py` automates the search and produces a **ranked candidate report** for you to review.

How it works (four stages):
1. **Seed** — collects candidate `.onion` markets from curated directories (dark.fail, tor.taxi, daunt) and a server-rendered onion search engine (Torch by default), configured in `data/discovery_sources.json`. (Ahmia's clearnet site is JavaScript-only and returns an empty page to a plain fetch, so it isn't used.)
2. **Liveness** — probes each candidate through Tor and drops dead ones.
3. **Score** — generically crawls each live market (homepage + a few category pages) and counts how many of your `search_keywords.json` medicines appear. Markets behind a login/CAPTCHA wall are marked `wall/manual` (not scored) so they aren't silently dropped.
4. **Report** — ranks by score and writes `data/candidate_markets.json` and `data/candidate_markets.csv`, plus a console table.

Run it with the same Tor flags as the scraper:

```bash
python src/discover_markets.py --socks --socks-port 9150 --insecure
# dry run (seeding only, no crawling):
python src/discover_markets.py --socks --socks-port 9150 --insecure --seeds-only
```

Then **review `data/candidate_markets.csv`** and confirm each onion is authentic before crawling — dark-web markets are heavily impersonated by phishing clones, which is why this step is deliberately manual. Promote chosen markets either by copying their `candidate_category_urls` into `pages_url.json`, or with the opt-in helper:

```bash
python src/discover_markets.py --socks --socks-port 9150 --insecure --promote <onion-host> [<onion-host> ...]
```

Useful flags: `--max-candidates N` (cap how many seeds get scored), `--max-pages-per-market N` (pages crawled per market), `--timeout S` (per-request timeout).

## Targeting categories within a market

Once you've picked an authentic market, `target_categories.py` enumerates *that market's* category pages and flags the research-relevant ones (pharmacy / prescription / health / women's / hormones / ...), so the crawler only fetches what matters instead of the whole catalog. It's the depth complement to `discover_markets.py`'s breadth.

```bash
# report only (writes data/category_candidates.csv + .json):
python src/target_categories.py http://<onion>/ --socks --socks-port 9150 --insecure

# expand pagination + append the relevant categories to pages_url.json:
python src/target_categories.py http://<onion>/ --socks --socks-port 9150 --insecure --expand-pages --write

# for login / CAPTCHA / anti-DDoS "access queue" markets, capture a session in a browser first:
python src/target_categories.py http://<onion>/ --socks --socks-port 9150 --insecure --manual
```

Review `data/category_candidates.csv` (✔ = flagged relevant); only relevant categories are written to `pages_url.json`, and writes are deduped. If a market is gated, the tool detects it and tells you to re-run with `--manual`. Other flags: `--depth 2` (also follow each category once for sub-categories), `--page-cap N` (max pages to expand per category).

## Output

- The scraper writes a timestamped JSON file derived from `products_html.json` (e.g. `products_html_YYYYMMDD_HHMMSS.json`). Each item contains:
  - market (hostname)
  - category_page (the listing page URL)
  - product_url
  - fetched_at (unix timestamp)
  - html (full HTML string)

## Category-share chart (after `evaluate_llm.py`)

`src/build_category_share.py` renders a donut showing what fraction of **all products
fetched** are confirmed abortion or contraception listings. Run it after
`evaluate_llm.py` (order relative to `push_to_sheets.py` doesn't matter).

```bash
python src/build_category_share.py
```

- **Numerator:** LLM-approved listings (`data/filtered/filtered_medicines_llm.json`,
  `llm_relevant == true`), split into abortion vs. contraception.
- **Denominator:** every distinct product parsed (`data/parsed/parsed_merged.json` +
  `data/parsed/parsed-torzone.json`, deduped by `original_url`).
- **Outputs (overwritten each run, it's a snapshot not a time series):**
  - `data/analytics/category_share.png` — donut: Abortion / Contraception / Other, with
    the combined share % in the center.
  - `data/analytics/category_share.csv` — one-row counts summary.
- `--no-chart` writes the summary only; `--denominator <files...>` overrides the total set.

## Editing `pages_url.json`

- `pages_url.json` must be a valid JSON array of ASCII URLs. Keep the list explicit — the scraper will only visit the URLs listed. To add numeric ranges programmatically, generate the URLs and overwrite `pages_url.json`.

Example (partial):

```json
["http://<onion-host>/category/prescription/2/",
 "http://<onion-host>/category/prescription/3/",
 ...]
```

## Troubleshooting

- If Firefox (Selenium) times out on `.onion` but Tor Browser opens the page, you are almost certainly on the wrong SOCKS port. Tor Browser uses its own Tor on `9150`, not system Tor on `9050`. Switch the scraper to `--socks-port 9150` (and keep Tor Browser open). See "Choosing the SOCKS port" above. First confirm a Tor SOCKS listener is running:

```bash
pgrep -a tor || ps aux | grep -i tor | grep -v grep
lsof -nP -iTCP -sTCP:LISTEN | grep -E '9050|9150'
```

- `SSLError: CERTIFICATE_VERIFY_FAILED (self-signed certificate)` — the market redirected to HTTPS and serves a self-signed cert. Add `--insecure` to skip cert verification. This is expected and safe on `.onion` addresses.

- If ports 9050/9051 are in use, that usually means Tor is already running (system tor or Tor Browser). Point the scraper at the running Tor instance (do not start a second Tor using the same ports).
- If using Privoxy, verify the `forward-socks5t` line is present so Privoxy forwards to Tor.
- To sanity-check connectivity via Tor from the same shell:

```bash
curl --socks5-hostname 127.0.0.1:9050 "http://<onion-host>/"
```

---

