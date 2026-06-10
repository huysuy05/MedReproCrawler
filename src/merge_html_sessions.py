"""Merge per-session raw HTML crawl files and drop duplicate products.

Each crawl session writes its own data/products_html_<timestamp>.json (see
scrape_simple.py). This tool unions all of those, keeping a single record per
product_url -- the one with the newest fetched_at -- so the downstream parse +
filter stages only see each listing once.

Files are loaded one at a time and only the surviving (deduped) records are held
in memory, so peak memory is roughly "unique records" rather than "all files at
once". Individual session files can still be hundreds of MB; if a single file is
too large to json.load, install ijson and the loader will stream it instead.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
from pathlib import Path
from typing import Dict, Iterator, List

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"

# Match the timestamped session files (products_html_20260604_133729.json, ...)
# without picking up the legacy un-timestamped products_html.json.
DEFAULT_GLOB = str(DATA_DIR / "raw" / "products_html_20*.json")
DEFAULT_OUTPUT = DATA_DIR / "merged" / "products_html_merged.json"

DEDUP_KEY = "product_url"
RECENCY_KEY = "fetched_at"

# Files larger than this are streamed with ijson rather than json.load, to keep
# peak memory bounded on machines without much free RAM.
STREAM_THRESHOLD_BYTES = 150 * 1024 * 1024  # 150 MB

# Truncated files (interrupted crawls) raise these while parsing; ijson has its
# own error type, referenced here without making ijson a hard import.
_PARSE_ERRORS: tuple = (json.JSONDecodeError,)
try:
    from ijson.common import IncompleteJSONError as _StreamError
    _PARSE_ERRORS = (json.JSONDecodeError, _StreamError)
except ImportError:  # pragma: no cover
    pass


def _coerce_recency(value: object) -> float:
    """Best-effort numeric ordering for fetched_at (epoch int/str)."""
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return -1.0


def _iter_streaming(path: Path) -> Iterator[dict]:
    """Stream product dicts from a large file without loading it all at once."""
    try:
        import ijson  # type: ignore
    except ImportError as exc:  # pragma: no cover - surfaced to the user
        raise RuntimeError(
            f"{path.name} is large; install 'ijson' to stream it "
            "(pip install ijson)."
        ) from exc
    with path.open("rb") as fh:
        for item in ijson.items(fh, "item"):
            if isinstance(item, dict):
                yield item


def iter_records(path: Path) -> Iterator[dict]:
    """Yield product dicts from one session file.

    Large files (> STREAM_THRESHOLD_BYTES) are streamed with ijson to keep peak
    memory bounded; small ones use a plain json.load.
    """
    if path.stat().st_size > STREAM_THRESHOLD_BYTES:
        yield from _iter_streaming(path)
        return

    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, list):
        raise ValueError(f"{path.name} is not a JSON list of products")
    for item in data:
        if isinstance(item, dict):
            yield item


def merge(paths: List[Path]) -> Dict[str, dict]:
    """Union records across files, keeping the newest per product_url."""
    survivors: Dict[str, dict] = {}
    no_url = 0
    for path in paths:
        seen_in_file = 0
        kept_before = len(survivors)
        truncated = False
        try:
            for record in iter_records(path):
                seen_in_file += 1
                url = record.get(DEDUP_KEY)
                if not url:
                    # No dedup key -- keep it under a synthetic unique key so it
                    # isn't silently dropped.
                    no_url += 1
                    survivors[f"__no_url__{no_url}"] = record
                    continue
                existing = survivors.get(url)
                if existing is None or _coerce_recency(record.get(RECENCY_KEY)) >= _coerce_recency(
                    existing.get(RECENCY_KEY)
                ):
                    survivors[url] = record
        except _PARSE_ERRORS as exc:
            # A crawl interrupted mid-write leaves a truncated file. Keep the
            # records salvaged before the break and move on.
            truncated = True
            print(f"  WARNING: {path.name} is truncated ({exc}); kept {seen_in_file} salvaged record(s)")
        added = len(survivors) - kept_before
        suffix = " [TRUNCATED]" if truncated else ""
        print(
            f"  {path.name}: read {seen_in_file}, "
            f"net-new unique {added} (running total {len(survivors)}){suffix}"
        )
    if no_url:
        print(f"  note: {no_url} record(s) had no {DEDUP_KEY} and were kept as-is")
    return survivors


def parse_args(argv: List[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--glob",
        default=DEFAULT_GLOB,
        help=f"Glob for session files (default: {DEFAULT_GLOB})",
    )
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"Merged output path (default: {DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--include-legacy",
        action="store_true",
        help="Also include the un-timestamped data/products_html.json",
    )
    return parser.parse_args(argv)


def main(argv: List[str] | None = None) -> None:
    args = parse_args(argv)
    paths = [Path(p) for p in sorted(glob.glob(args.glob))]
    if args.include_legacy:
        legacy = DATA_DIR / "raw" / "products_html.json"
        if legacy.exists():
            paths.append(legacy)
    if not paths:
        raise SystemExit(f"No session files matched {args.glob!r}")

    print(f"Merging {len(paths)} session file(s):")
    survivors = merge(paths)
    records = list(survivors.values())

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as fh:
        json.dump(records, fh, ensure_ascii=False, indent=2)

    print(
        f"\nWrote {len(records)} unique products to {args.output} "
        f"({os.path.getsize(args.output) / 1e6:.1f} MB)"
    )


if __name__ == "__main__":
    main()
