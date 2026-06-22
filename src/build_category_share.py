"""Chart the share of abortion + contraception listings among all products fetched.

Pipeline step that runs after evaluate_llm.py. It answers: of every product the
crawler has fetched and parsed, what fraction are confirmed abortion or
contraception listings?

  numerator   = LLM-approved listings (data/filtered/filtered_medicines_llm.json,
                llm_relevant == True), split into abortion vs contraception
  denominator = every distinct product parsed across the pipeline
                (data/parsed/parsed_merged.json + data/parsed/parsed-torzone.json,
                deduped by original_url)

Renders a donut with three segments -- Abortion, Contraception, Other products --
and the combined abortion+contraception share annotated in the center. A one-row
summary of the counts is written alongside it for reference.

This is a snapshot of the current data (overwritten each run); it is not a time
series.
"""
from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Sequence

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"

DEFAULT_NUMERATOR = DATA_DIR / "filtered" / "filtered_medicines_llm.json"
DEFAULT_DENOMINATOR = [
    DATA_DIR / "parsed" / "parsed_merged.json",
    DATA_DIR / "parsed" / "parsed-torzone.json",
]
ANALYTICS_DIR = DATA_DIR / "analytics"
DEFAULT_CHART = ANALYTICS_DIR / "category_share.png"
DEFAULT_SUMMARY = ANALYTICS_DIR / "category_share.csv"

# Canonical category set (keys of data/config/search_keywords.json).
CATEGORIES = ("abortion", "contraception")

# Wedge colors: the two categories pop, "other" stays muted.
COLORS = {
    "abortion": "#d62728",
    "contraception": "#1f77b4",
    "other": "#d9d9d9",
}


def load_listings(path: Path) -> List[Dict[str, object]]:
    """Load a JSON list of listing dicts (mirrors push_to_sheets.load_listings)."""
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, list):
        raise ValueError(f"{path} must contain a JSON list of listings")
    return [item for item in data if isinstance(item, dict)]


def resolve_category(item: Dict[str, object]) -> str | None:
    """Pick a canonical category, preferring the LLM verdict over keyword match."""
    for field in ("llm_category", "matched_categories"):
        raw = item.get(field)
        if not raw:
            continue
        # matched_categories can be "abortion; contraception"; take the first match.
        for token in str(raw).replace(",", ";").split(";"):
            token = token.strip().lower()
            if token in CATEGORIES:
                return token
    return None


def count_numerator(path: Path) -> Dict[str, int]:
    """Count LLM-approved listings per category (abortion / contraception)."""
    counts = {cat: 0 for cat in CATEGORIES}
    skipped = 0
    for item in load_listings(path):
        if item.get("llm_relevant") is not True:
            continue
        category = resolve_category(item)
        if category is None:
            skipped += 1
            continue
        counts[category] += 1
    if skipped:
        print(f"  note: {skipped} approved listing(s) had no resolvable category; skipped")
    return counts


def count_denominator(paths: Sequence[Path]) -> int:
    """Distinct products fetched, deduped by original_url across all parse files."""
    urls: set[str] = set()
    for path in paths:
        if not path.exists():
            print(f"  skip (missing): {path}")
            continue
        chunk = load_listings(path)
        for item in chunk:
            url = item.get("original_url")
            if url:
                urls.add(str(url))
        print(f"  loaded {len(chunk)} parsed product(s) from {path.name}")
    return len(urls)


def render_chart(
    cat_counts: Dict[str, int],
    other: int,
    total: int,
    chart_path: Path,
) -> None:
    import matplotlib
    matplotlib.use("Agg")  # headless: write a file, no display needed
    import matplotlib.pyplot as plt

    segments = [
        ("Abortion", cat_counts["abortion"], COLORS["abortion"]),
        ("Contraception", cat_counts["contraception"], COLORS["contraception"]),
        ("Other products", other, COLORS["other"]),
    ]
    sizes = [s[1] for s in segments]
    colors = [s[2] for s in segments]
    labels = [
        f"{name}\n{count:,} ({count / total:.2%})"
        for name, count, _ in segments
    ]

    fig, ax = plt.subplots(figsize=(8, 8))
    wedges, _ = ax.pie(
        sizes,
        colors=colors,
        startangle=90,
        counterclock=False,
        wedgeprops={"width": 0.42, "edgecolor": "white", "linewidth": 1},
    )

    combined = cat_counts["abortion"] + cat_counts["contraception"]
    ax.text(0, 0.08, f"{combined / total:.2%}", ha="center", va="center",
            fontsize=26, fontweight="bold")
    ax.text(0, -0.12, "abortion +\ncontraception", ha="center", va="center",
            fontsize=11, color="#555555")

    ax.set_title(
        f"Abortion & contraception share of all products fetched\n"
        f"{combined:,} of {total:,} products (LLM-approved)",
        fontsize=13,
    )
    ax.legend(wedges, labels, loc="lower center", bbox_to_anchor=(0.5, -0.08),
              ncol=3, frameon=False, fontsize=10)
    ax.set_aspect("equal")
    fig.tight_layout()

    chart_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(chart_path, dpi=120, bbox_inches="tight")
    plt.close(fig)


def write_summary(path: Path, row: Dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    header = ["generated_at", "abortion", "contraception",
              "abortion_contraception", "other", "total", "share_pct"]
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=header)
        writer.writeheader()
        writer.writerow(row)


def parse_args(argv: List[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--numerator", "-n", type=Path, default=DEFAULT_NUMERATOR,
                        help=f"LLM-evaluated listings JSON (default: {DEFAULT_NUMERATOR})")
    parser.add_argument("--denominator", "-d", type=Path, nargs="+",
                        default=DEFAULT_DENOMINATOR,
                        help="Parsed product JSON file(s) for the 'total fetched' "
                             "denominator (deduped by original_url)")
    parser.add_argument("--chart-output", type=Path, default=DEFAULT_CHART,
                        help=f"Donut chart PNG path (default: {DEFAULT_CHART})")
    parser.add_argument("--summary-output", type=Path, default=DEFAULT_SUMMARY,
                        help=f"One-row counts summary CSV (default: {DEFAULT_SUMMARY})")
    parser.add_argument("--no-chart", action="store_true",
                        help="Print/write the summary only; do not render the PNG.")
    return parser.parse_args(argv)


def main(argv: List[str] | None = None) -> None:
    args = parse_args(argv)

    if not args.numerator.exists():
        raise SystemExit(f"Numerator not found at {args.numerator}. Run evaluate_llm.py first.")

    cat_counts = count_numerator(args.numerator)
    total = count_denominator(args.denominator)
    if total == 0:
        raise SystemExit("Denominator is 0 (no parsed products found); cannot compute a share.")

    combined = cat_counts["abortion"] + cat_counts["contraception"]
    if combined > total:
        raise SystemExit(
            f"Numerator ({combined}) exceeds denominator ({total}); check that the "
            "parse files cover the same products as the LLM input."
        )
    other = total - combined

    print(f"  abortion={cat_counts['abortion']}  contraception={cat_counts['contraception']}  "
          f"other={other:,}  total={total:,}")
    print(f"  abortion+contraception share: {combined}/{total} = {combined / total:.2%}")

    write_summary(args.summary_output, {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "abortion": cat_counts["abortion"],
        "contraception": cat_counts["contraception"],
        "abortion_contraception": combined,
        "other": other,
        "total": total,
        "share_pct": round(100 * combined / total, 4),
    })
    print(f"  wrote summary -> {args.summary_output}")

    if args.no_chart:
        print("  --no-chart: skipped rendering")
        return

    render_chart(cat_counts, other, total, args.chart_output)
    print(f"  rendered chart -> {args.chart_output}")


if __name__ == "__main__":
    main()
