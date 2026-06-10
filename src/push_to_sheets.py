"""Push filtered medicine listings to a Google Sheet.

Reads data/filtered_medicines.json (produced by filter_medicines.py) and writes
one row per matched listing to a worksheet, using a Google Cloud service account
for auth.

Setup (one-time):
  1. Create a service account in Google Cloud and enable the Google Sheets API.
  2. Download its JSON key to credentials/service_account.json (gitignored).
  3. Share the target sheet with the service account's client_email (Editor).

The whole sheet is rewritten in a single batched update so we stay well under
the Sheets API rate limits.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Dict, List, Sequence

import gspread

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"

DEFAULT_JSON = DATA_DIR / "filtered" / "filtered_medicines.json"
DEFAULT_CREDENTIALS = BASE_DIR / "credentials" / "service_account.json"
DEFAULT_SHEET_ID = "1mZp58VNB1qR2A5SsKApvuOMAtZV7tUF1EOOHBqKSsUM"
DEFAULT_WORKSHEET = "Listings"

# Column order written to the sheet. Mirrors filter_medicines.PREFERRED_HEADERS
# plus the match metadata the filter appends.
COLUMNS: List[str] = [
    "market_name",
    "listing_title",
    "price",
    "dosage",
    "rating",
    "review",
    "description",
    "number_in_stocks",
    "matched_terms",
    "matched_categories",
    "original_url",
    "category_page",
    "fetched_at",
]


def load_listings(path: Path) -> List[Dict[str, object]]:
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, list):
        raise ValueError(f"{path} must contain a JSON list of listings")
    return [item for item in data if isinstance(item, dict)]


def _cell(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def build_rows(listings: Sequence[Dict
[str, object]]) -> List[List[str]]:
    rows: List[List[str]] = [COLUMNS]
    for item in listings:
        rows.append([_cell(item.get(col, "")) for col in COLUMNS])
    return rows


def push(rows: List[List[str]], credentials: Path, sheet_id: str, worksheet_name: str) -> str:
    client = gspread.service_account(filename=str(credentials))
    spreadsheet = client.open_by_key(sheet_id)
    try:
        worksheet = spreadsheet.worksheet(worksheet_name)
    except gspread.WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(
            title=worksheet_name,
            rows=max(len(rows) + 10, 100),
            cols=len(COLUMNS),
        )

    worksheet.clear()
    # A single batched write: header + all data rows.
    worksheet.update(rows, value_input_option="RAW")
    return spreadsheet.url


def parse_args(argv: List[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json-input", "-j", type=Path, default=DEFAULT_JSON,
                        help=f"Filtered listings JSON (default: {DEFAULT_JSON})")
    parser.add_argument("--credentials", "-c", type=Path, default=DEFAULT_CREDENTIALS,
                        help=f"Service account JSON key (default: {DEFAULT_CREDENTIALS})")
    parser.add_argument("--sheet-id", default=DEFAULT_SHEET_ID,
                        help="Target spreadsheet ID (the long token in its URL)")
    parser.add_argument("--worksheet", "-w", default=DEFAULT_WORKSHEET,
                        help=f"Worksheet/tab name (default: {DEFAULT_WORKSHEET})")
    return parser.parse_args(argv)


def main(argv: List[str] | None = None) -> None:
    args = parse_args(argv)
    if not args.credentials.exists():
        raise SystemExit(
            f"Service account key not found at {args.credentials}. "
            "See the setup notes at the top of this file."
        )

    listings = load_listings(args.json_input)
    rows = build_rows(listings)
    url = push(rows, args.credentials, args.sheet_id, args.worksheet)
    print(f"Wrote {len(listings)} listing(s) to worksheet '{args.worksheet}' in {url}")


if __name__ == "__main__":
    main()
