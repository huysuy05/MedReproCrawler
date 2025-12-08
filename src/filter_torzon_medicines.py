"""Filter Torzon medicines of interest from parsed-torzone.json and export to CSV."""
from __future__ import annotations

import argparse
from pathlib import Path

from filter_medicines import (
    build_patterns,
    determine_columns,
    filter_products,
    load_products,
    write_csv,
)


def parse_args() -> argparse.Namespace:
    project_root = Path(__file__).resolve().parents[1]
    default_input = project_root / "data" / "parsed-torzone.json"
    default_output = project_root / "data" / "filtered_torzon_medicines.csv"
    parser = argparse.ArgumentParser(
        description="Filter Torzon parsed drugs for specified medicines and export to CSV."
    )
    parser.add_argument("--input", "-i", type=Path, default=default_input, help="Path to parsed-torzone.json")
    parser.add_argument("--output", "-o", type=Path, default=default_output, help="Destination CSV path")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    products = load_products(args.input)
    for product in products:
        product["market_name"] = "TorZon Market"
    patterns = build_patterns()
    filtered = filter_products(products, patterns)
    if not filtered:
        print("No Torzon products matched the supplied medicine terms.")
        return
    for product in filtered:
        product.pop("matched_terms", None)
        product.pop("matched_categories", None)
    headers = [h for h in determine_columns(filtered) if h not in {"matched_terms", "matched_categories"}]
    write_csv(filtered, headers, args.output)
    print(f"Wrote {len(filtered)} Torzon products to {args.output}")


if __name__ == "__main__":
    main()
