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
- `pages_url.json` — JSON array of category/listing URLs. Edit to add or remove pages; the scraper will NOT auto-advance beyond what's in this file.
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
python3 scrape_simple.py --socks --socks-port 9050 --manual --disable-js
```

Options explained:
- `--socks --socks-port 9050` — connect Firefox to Tor SOCKS5 (default tor port).
- `--manual` — opens the browser for manual CAPTCHA solving / login and waits for you to press Enter.
- `--disable-js` — disables JavaScript in Firefox (can speed up loads and reduce bot detection).
- `--page-timeout <seconds>` — increase if pages load slowly over Tor.
- `--session-wait <seconds>` — seconds to wait after opening a page before collecting cookies (default 60).

## Output

- The scraper writes a timestamped JSON file derived from `products_html.json` (e.g. `products_html_YYYYMMDD_HHMMSS.json`). Each item contains:
  - market (hostname)
  - category_page (the listing page URL)
  - product_url
  - fetched_at (unix timestamp)
  - html (full HTML string)

## Editing `pages_url.json`

- `pages_url.json` must be a valid JSON array of ASCII URLs. Keep the list explicit — the scraper will only visit the URLs listed. To add numeric ranges programmatically, generate the URLs and overwrite `pages_url.json`.

Example (partial):

```json
["http://<onion-host>/category/prescription/2/",
 "http://<onion-host>/category/prescription/3/",
 ...]
```

## Troubleshooting

- If Firefox (Selenium) times out on `.onion` but Tor Browser opens the page, confirm a Tor SOCKS listener is running (9050 typically). Check:

```bash
pgrep -a tor || ps aux | grep -i tor | grep -v grep
lsof -nP -iTCP -sTCP:LISTEN | grep -E '9050|9150'
```

- If ports 9050/9051 are in use, that usually means Tor is already running (system tor or Tor Browser). Point the scraper at the running Tor instance (do not start a second Tor using the same ports).
- If using Privoxy, verify the `forward-socks5t` line is present so Privoxy forwards to Tor.
- To sanity-check connectivity via Tor from the same shell:

```bash
curl --socks5-hostname 127.0.0.1:9050 "http://<onion-host>/"
```

## Notes on safety and legality

Use this tool only for legitimate research or with appropriate authorization. Scraping marketplaces may violate terms of service or local law.

---

