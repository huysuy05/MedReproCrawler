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
    "contraceptives": [
        "Noracycline",
        "Marvelon",
        "Mircette",
        "Cyclessa",
        "Mercilon",
        "Varnoline",
        "Kariva",
        "Ovidol",
        "Oviol",
        "Practil",
        "Caziant",
        "Cycleane",
        "Desolett",
        "Emoquette",
        "Gracial",
        "Laurina",
        "Lovelle",
        "Marvelone",
        "Novelon",
        "Reclipsen",
        "Relivon",
        "Securgin",
        "Velivet",
        "Cesia",
        "Desogen",
        "OrthoCept",
        "Apri",
        "Bekyree",
        "Kalliga",
        "Kimidess",
        "Drospirenone",
        "Yasmin",
        "yasminelle",
        "Yaz",
        "Gianvi",
        "Vestura",
        "Loryna",
        "Nikki",
        "Ocella",
        "Syeda",
        "Zarah",
        "Petibelle",
        "Estropipate",
        "Harmogen",
        "Harmonet",
        "Ogen",
        "Ortho-Est",
        "piperazine estrone sulfate",
        "Sulestrex",
        "Pipestrone",
        "Ethynodiol Diacetate",
        "Demulen",
        "Ovamin",
        "Ovulen",
        "Zovia",
        "Kelnor",
        "Etonogestrel",
        "NuvaRing",
        "Vaginal Ring",
        "Elifemme",
        "Eugynon",
        "Gravistat",
        "Alesse",
        "Lybrel",
        "Ovran",
        "Anna",
        "Low-ogestrel",
        "Lo-Femenal",
        "Tri-Regol",
        "LEVLITE",
        "Loseasonique",
        "Altavera",
        "Introvale",
        "Jolessa",
        "Levonest",
        "Marlissa",
        "Quartette",
        "Portia",
        "Sronyx",
        "CRYSELLE",
        "ENPRESSE",
        "NORDETTE",
        "LESSINA",
        "TRIVORA",
        "aless",
        "Twirla",
        "Vienva",
        "OGESTREL",
        "Norelgestromin",
        "Evra",
        "Ortho Evra",
        "Xulane",
        "Brevicon",
        "Estrostep",
        "Loestrin",
        "Modicon",
        "Ortho Novum",
        "Synphase",
        "Triella",
        "Lo Minastrin Fe",
        "Nortrel",
        "Binovum",
        "Norminest",
        "Trinovum",
        "Neocon",
        "Norimin",
        "Ovysmen",
        "Oestro-primolut",
        "LoDose",
        "Femcon Fe",
        "E-Con",
        "Ortho-Novum",
        "Ovcon",
        "Norinyl",
        "Aranelle",
        "Balziva",
        "Zenchent",
        "Kaitlib Fe",
        "Cyclafem",
        "Cyonanz",
        "Leena",
        "Necon",
        "Alyacen",
        "Nylia",
        "Norgestimate",
        "Cilest",
        "Ortho Tri-Cyclen",
        "Tri-Cyclen",
        "TRI-SPRINTEC",
        "Estarylla",
        "Mili",
        "TriNessa",
        "Tri-Estarylla",
        "Mono-Linyah",
        "Ortho Cylen",
        "Tri Cyclen",
        "Tri-Linyah",
        "Tri-Mili",
        "ORTHO Tri Cyclen",
        "Tri-Lo-Estarylla",
        "Ortho Tri Lo",
        "Ortho Cyclen",
        "ethinylestradiol",
        "MonoNessa",
        "Tri-lo-sprintec",
        "Microgynon",
        "Ovral",
        "Levonorgestrel",
        "Mirena",
        "Levonova",
        "Microval",
        "Postinor",
        "Jadelle",
        "Plan B",
        "NORPLANT",
        "Follistrel",
        "Levonelle",
        "Ovrette",
        "Neogest",
        "Ovranette",
        "Levlen",
        "Minivlar",
        "Trifeme",
        "Tri-Levlen",
        "NorLevo",
        "Triphasil",
        "Ovoplex",
        "Microluton",
        "Next choice",
        "Fallback Solo",
        "Kyleena",
        "Medroxyprogesterone",
        "Farlutal",
        "Nonoxynol",
        "Advantage S",
        "Delfen",
        "Intercept",
        "Semicid",
        "Staycept",
        "Emko",
        "Today Sponge",
        "Gynol II",
        "C-Film",
        "Norethisterone",
        "Micronor",
        "Norcolut",
        "Noriday",
        "Norlutin",
        "Primolut-N",
        "Conludag",
        "Micronovum",
        "Camila",
        "Utovlan",
        "Mini-pill",
        "Conludaf",
        "Noralutin",
        "Norgestin",
        "Nor-QD",
        "Minovlar",
        "Normapause",
        "Primolut",
        "Ciclovulan",
        "Microneth",
        "Estrinor",
        "Norethin",
        "Triella",
        "Genora",
        "Nelova",
        "Nodiol",
        "Norcept-E",
        "Synphasic",
        "Brevinor",
        "Errin",
        "Jenest",
        "Tri-Norinyl",
        "Noretisterone",
        "Ulipristal",
        "Ulipristal acetate",
        "Ella",
        "Bravelle",
        "Follicle stimulating hormone",
        "Follitrin",
        "Fertinorm",
        "Contraception",
        "Birth Control",
        "Norethindrone",
        "Norgestrel",
        "Segesterone",
        "Liletta",
        "Skyla",
        "Morning after pill",
        "Paragard",
        "Intrauterine device",
        "Norethindrone",
        "Norgestrel",
        "Segesterone",
        "Opill",
        "Progesterone",
        "Oestrogen",
        "Conjugated estrogen",
        "Estrogens",
        "Estrogen",
        "Estroplus",
        "Progestin",
        "Syngesterone",
        "Estradiol",
        "Ethinyl estradiol",
        "Provera",
        "LNG",
        "Levnorgestrel",
    ],
    "abortion_meds": [
        "Mifepristone",
        "Mifeprex",
        "Mifiprex",
        "RU-486",
        "Misoprostol",
        "Cytotec",
        "Dinoprostone",
        "Prostaglandin E2",
        "Prostin E2",
        "Prepidil",
        "Cervidil",
        "Propess",
        "Minprostin E2",
        "Prostin",
        "Prostarmon E",
        "Glandin",
        "Prostaglandin E",
        "Cerviprime",
        "Prostarmon E2",
        "Medroxyprogesterone Acetate",
        "Cyclofem",
        "Lunelle",
        "Dienogest",
        "Climodien",
        "Lafamme",
        "Segesterone acetate",
        "Elcometrine",
        "Nestorone",
        "Methotrexate",
        "Abortion",
    ],
}


def build_patterns() -> List[Tuple[str, str, re.Pattern[str]]]:
    """Create regex patterns that tolerate punctuation and spacing differences."""
    compiled: List[Tuple[str, str, re.Pattern[str]]] = []
    for category, terms in TERM_GROUPS.items():
        for term in terms:
            raw_tokens = re.split(r"[\\s\\-]+", term)
            tokens = [re.escape(token) for token in raw_tokens if token]
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


PREFERRED_HEADERS: List[str] = [
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


def determine_columns(products: Iterable[Dict[str, object]]) -> List[str]:
    preferred = list(PREFERRED_HEADERS)
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
    default_output = project_root / "data" / "filtered_medicines2.csv"
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
        # Write an empty file with standard headers to avoid stale data.
        headers = list(PREFERRED_HEADERS)
        write_csv([], headers, args.output)
        print("No products matched the supplied medicine terms. Wrote empty CSV with headers.")
        return
    headers = determine_columns(filtered)
    write_csv(filtered, headers, args.output)
    print(f"Wrote {len(filtered)} products to {args.output}")


if __name__ == "__main__":
    main()
