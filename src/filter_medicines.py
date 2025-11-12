"""Filter medicines of interest from parsed_drugs.json and export to CSV."""
from __future__ import annotations

import argparse
import csv
import json
import re
from pathlib import Path
from typing import Dict, Iterable, List, Sequence, Set, Tuple

# Terms grouped by category so the resulting CSV can show which families matched.
TERM_GROUPS: Dict[str, Sequence[str]] = {
    "abortion_meds": [
        "Abortion", "Methotrexate", "Mifepristone", "Misoprostol",
        "Mifeprex", "Mifiprex", "Cytotec", "Dinoprostone",
    ],
    "contraceptives": [
        "Noracycline", "Marvelon", "Mircette", "Cyclessa",
        "Mercilon", "Varnoline", "Kariva",
    ],
    "sildenafil": [
        "Sildenafil", "Sildenafil Citrate", "Tadalafil", "Clomiphene",
    ],
    "hormonal": [
        "Testosterone", "Dihydrotestosterone", "Estrace", "Premarin",
    ],
}


def build_patterns() -> List[Tuple[str, str, re.Pattern[str]]]:
    """Create regex patterns that tolerate punctuation and spacing differences."""
    compiled: List[Tuple[str, str, re.Pattern[str]]] = []
    for category, terms in TERM_GROUPS.items():
        for term in terms:
            tokens = [re.escape(token) for token in term.split() if token]
            if not tokens:
                continue
            pattern_body = r"\W*".join(tokens)
            pattern = re.compile(rf"\b{pattern_body}\b", re.IGNORECASE)
            compiled.append((category, term, pattern))
    return compiled


def load_products(path: Path) -> List[Dict[str, object]]:
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, list):
        raise ValueError("Expected the JSON file to contain a list of products")
    return [item for item in data if isinstance(item, dict)]


def normalise_cell(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def determine_columns(products: Iterable[Dict[str, object]]) -> List[str]:
    preferred = [
        "market_name",
        "listing_title",
        "price",
        "dosage",
        "rating",
        "review",
        "description",
        "number_in_stocks",
        "original_url",
        "category_page",
        "fetched_at",
    ]
    seen: Set[str] = set(preferred)
    extras: Set[str] = set()
    for product in products:
        extras.update(product.keys())
    ordered = [key for key in preferred if key in extras]
    ordered.extend(sorted(extras - seen))
    ordered.extend(["matched_terms", "matched_categories"])
    return ordered


def filter_products(products: Sequence[Dict[str, object]], patterns: Sequence[Tuple[str, str, re.Pattern[str]]]) -> List[Dict[str, object]]:
    filtered: List[Dict[str, object]] = []
    for product in products:
        haystack_parts = [
            normalise_cell(product.get("listing_title", "")),
            normalise_cell(product.get("description", "")),
            normalise_cell(product.get("review", "")),
        ]
        haystack = " \n ".join(haystack_parts)
        matched_terms: Set[str] = set()
        matched_categories: Set[str] = set()
        for category, term, pattern in patterns:
            if pattern.search(haystack):
                matched_terms.add(term)
                matched_categories.add(category)
        if matched_terms:
            product_copy = dict(product)
            product_copy["matched_terms"] = "; ".join(sorted(matched_terms))
            product_copy["matched_categories"] = "; ".join(sorted(matched_categories))
            filtered.append(product_copy)
    return filtered


def write_csv(products: Sequence[Dict[str, object]], headers: Sequence[str], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=headers)
        writer.writeheader()
        for product in products:
            row = {column: normalise_cell(product.get(column, "")) for column in headers}
            writer.writerow(row)


def parse_args() -> argparse.Namespace:
    project_root = Path(__file__).resolve().parents[1]
    default_input = project_root / "data" / "parsed_drugs.json"
    default_output = project_root / "data" / "filtered_medicines.csv"
    parser = argparse.ArgumentParser(description="Filter parsed drugs for specified medicines and export to CSV.")
    parser.add_argument("--input", "-i", type=Path, default=default_input, help="Path to parsed_drugs.json")
    parser.add_argument("--output", "-o", type=Path, default=default_output, help="Destination CSV path")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    products = load_products(args.input)
    patterns = build_patterns()
    filtered = filter_products(products, patterns)
    if not filtered:
        print("No products matched the supplied medicine terms.")
        return
    headers = determine_columns(filtered)
    write_csv(filtered, headers, args.output)
    print(f"Wrote {len(filtered)} products to {args.output}")


if __name__ == "__main__":
    main()
