"""TorZon-specific HTML parser.

Reads TorZon product HTML blobs from data/torzone-html.json and writes
normalized records to data/parsed-torzone.json. Extracts shipping info that the
generic parser misses.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Dict, Iterable, List, Tuple

from bs4 import BeautifulSoup


def clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def load_products_data(path: Path) -> List[Dict[str, str]]:
    return json.loads(path.read_text(encoding="utf-8"))


def extract_title(soup: BeautifulSoup) -> str:
    selectors = [
        "center font[style*='font-size']",
        "center h1",
        "h1",
        "title",
    ]
    for selector in selectors:
        for elem in soup.select(selector):
            text = clean(elem.get_text())
            if text and len(text) > 5:
                return text
    return ""


def extract_price(soup: BeautifulSoup) -> str:
    text = soup.get_text(" ", strip=True)
    match = re.search(r"USD\s*([\d.,]+)", text, re.IGNORECASE)
    if match:
        return f"USD {match.group(1)}"
    return ""


def extract_table_value(soup: BeautifulSoup, label: str) -> str:
    target = label.lower()
    best = None
    for row in soup.find_all("tr"):
        cells = row.find_all(["td", "th"])
        texts = [clean(c.get_text(" ", strip=True)) for c in cells]
        for idx, text in enumerate(texts):
            lower = text.lower()
            # Only accept reasonably short heading matches to avoid giant blobs that contain the word.
            if lower == target or lower.startswith(f"{target} "):
                if idx + 1 < len(texts):
                    candidate = texts[idx + 1]
                    if candidate:
                        if best is None or len(candidate) < len(best):
                            best = candidate
                        continue
                continue
            # Handle cases where the label and value are jammed together (e.g., "Shipping India -> WorldWide")
            if lower.startswith(target) and len(lower) <= len(target) + 30:
                remainder = text[len(label):].strip(" :")
                # Trim at common following labels to avoid swallowing multiple fields
                for stop_word in ("Price", "Shipping", "Payment", "Close", "All "):
                    if stop_word in remainder:
                        remainder = remainder.split(stop_word, 1)[0]
                        break
                if remainder:
                    if best is None or len(remainder) < len(best):
                        best = remainder
    return best or ""


def extract_shipping(soup: BeautifulSoup) -> Tuple[str, str]:
    # Prefer rows where the label is a standalone "Shipping" cell.
    for row in soup.find_all("tr"):
        cells = [clean(c.get_text(" ", strip=True)) for c in row.find_all(["td", "th"])]
        for idx, text in enumerate(cells):
            if text.lower() == "shipping" and idx + 1 < len(cells):
                raw = cells[idx + 1]
                parts = [part.strip(" -") for part in raw.split("->") if part.strip(" -")]
                if parts:
                    ship_from = parts[0]
                    ship_to = parts[1] if len(parts) > 1 else ""
                    return ship_from, ship_to
    # Fallback to generic extraction if no clean pair found.
    raw = extract_table_value(soup, "shipping")
    if raw:
        parts = [part.strip(" -") for part in raw.split("->") if part.strip(" -")]
        ship_from = parts[0] if parts else raw
        ship_to = parts[1] if len(parts) > 1 else ""
        return ship_from, ship_to
    return "", ""


def extract_rating(soup: BeautifulSoup) -> str:
    # Prefer product rating
    rating_text = extract_table_value(soup, "Product Rating")
    match = re.search(r"(\d+\.?\d*)", rating_text)
    if match:
        return match.group(1)

    # Fallback to vendor rating (e.g., "krybaby (114) (4.89 ★)")
    for center in soup.find_all("center"):
        text = clean(center.get_text())
        m = re.search(r"\((\d+\.?\d*)\s*★", text)
        if m:
            return m.group(1)
    return ""


def extract_description(soup: BeautifulSoup) -> str:
    section = soup.select_one("#description")
    if section:
        text = clean(section.get_text())
        if text:
            return text
    return ""


def extract_category(soup: BeautifulSoup) -> str:
    return extract_table_value(soup, "Category")


def extract_ship_from_to(soup: BeautifulSoup) -> Tuple[str, str]:
    ship_from, ship_to = extract_shipping(soup)
    return ship_from, ship_to


def parse_product(entry: Dict[str, str]) -> Dict[str, object]:
    soup = BeautifulSoup(entry.get("html", ""), "html.parser")

    ship_from, ship_to = extract_ship_from_to(soup)

    return {
        "market_name": "TorZon Market",
        "listing_title": extract_title(soup),
        "price": extract_price(soup),
        "dosage": "",
        "rating": extract_rating(soup),
        "review": "",
        "description": extract_description(soup),
        "number_in_stocks": "",
        "original_url": entry.get("product_url", ""),
        "category_page": entry.get("category_page", ""),
        "category": extract_category(soup),
        "ship_from": ship_from,
        "ship_to": ship_to,
        "fetched_at": entry.get("fetched_at", ""),
    }


def parse_all(entries: Iterable[Dict[str, str]]) -> List[Dict[str, object]]:
    parsed: List[Dict[str, object]] = []
    for entry in entries:
        parsed.append(parse_product(entry))
    return parsed


def main() -> None:
    project_root = Path(__file__).resolve().parents[1]
    input_path = project_root / "data" / "torzone-html.json"
    output_path = project_root / "data" / "parsed-torzone.json"

    entries = load_products_data(input_path)
    parsed = parse_all(entries)
    output_path.write_text(json.dumps(parsed, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Parsed {len(parsed)} TorZon products -> {output_path}")


if __name__ == "__main__":
    main()
