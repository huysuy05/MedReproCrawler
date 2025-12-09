"""Merge filtered_medicines2.csv and filtered_torzon_medicines.csv into final_filtered_medcinde.csv.

- Preserves all columns from both sources. Any missing column values in a row are written as blank.
- Handles duplicate header names by suffixing later occurrences with `__2`, `__3`, etc.
- Defaults to data/*.csv relative to this script; override via CLI args if needed.
"""
from __future__ import annotations

import argparse
import csv
from pathlib import Path
from typing import Dict, List, Sequence, Tuple


# Project root (src/ is one level below the repo root).
BASE_DIR = Path(__file__).resolve().parent.parent


def _read_csv_with_unique_columns(path: Path) -> Tuple[List[str], List[Dict[str, str]]]:
    """Read a CSV and ensure duplicate column names become unique with suffixes."""
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        try:
            raw_header = next(reader)
        except StopIteration:
            return [], []

        seen: Dict[str, int] = {}
        header: List[str] = []
        for name in raw_header:
            seen[name] = seen.get(name, 0) + 1
            if seen[name] == 1:
                header.append(name)
            else:
                header.append(f"{name}__{seen[name]}")

        rows: List[Dict[str, str]] = []
        for row in reader:
            rows.append({col: val for col, val in zip(header, row)})
    return header, rows


def merge_csvs(file_a: Path, file_b: Path, output: Path) -> None:
    header_a, rows_a = _read_csv_with_unique_columns(file_a)
    header_b, rows_b = _read_csv_with_unique_columns(file_b)

    columns: List[str] = list(header_a)
    for col in header_b:
        if col not in columns:
            columns.append(col)

    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        for row in rows_a + rows_b:
            writer.writerow({col: row.get(col, "") for col in columns})



def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Merge two filtered medicine CSV files.")
    parser.add_argument(
        "--file-a",
        type=Path,
        default=BASE_DIR / "data" / "filtered_medicines2.csv",
        help="First CSV to merge (defaults to data/filtered_medicines2.csv)",
    )
    parser.add_argument(
        "--file-b",
        type=Path,
        default=BASE_DIR / "data" / "filtered_torzon_medicines.csv",
        help="Second CSV to merge (defaults to data/filtered_torzon_medicines.csv)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=BASE_DIR / "data" / "final_filtered_medcinde.csv",
        help="Output CSV path (defaults to data/final_filtered_medcinde.csv)",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> None:
    args = parse_args(argv)
    merge_csvs(args.file_a, args.file_b, args.output)
    print(f"Merged {args.file_a.name} and {args.file_b.name} -> {args.output}")


if __name__ == "__main__":
    main()
