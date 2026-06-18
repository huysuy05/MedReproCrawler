#!/usr/bin/env python3
"""
LLM evaluation layer for the medicine pipeline.

The keyword filter (filter_medicines.py) is high-recall but low-precision: generic
terms match unrelated listings (e.g. an anabolic steroid whose description says
"estrogen blocker"). This stage runs each keyword-matched candidate past a LOCAL
LLM (served over an OpenAI-compatible API, e.g. LM Studio) that judges whether the
listing is genuinely a contraception/abortion product, and why.

Pipeline position (second-stage judge):

    parser.py → filter_medicines.py → evaluate_llm.py ★ → push_to_sheets.py

Each record is annotated with:
    llm_relevant      true | false | null(=undecided/error)
    llm_category      contraception | abortion | none
    llm_product_type  short label, e.g. "combined oral contraceptive"
    llm_confidence    0.0 - 1.0
    llm_reason        one-line justification

Results are cached (data/llm_cache.json) keyed by model + content, so reruns are
free and resumable. Default output keeps every record with its verdict; use
--relevant-only to drop the rejects.

Usage:
    python3 src/evaluate_llm.py                       # eval filtered_medicines.json
    python3 src/evaluate_llm.py --limit 5             # smoke test
    python3 src/evaluate_llm.py --relevant-only       # only keep llm_relevant=true
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
from pathlib import Path
from typing import Dict, List, Optional

import requests
from termcolor import colored

# Reuse the filter's CSV/JSON writers so output columns stay consistent with the
# rest of the pipeline (these modules are import-safe -- main() is __main__-gated).
from filter_medicines import determine_columns, write_csv, write_json


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "data"
# Both filtered sources, mirroring push_to_sheets.py: the merged markets plus the
# separately-filtered TorZon listings. Missing files are skipped with a warning.
DEFAULT_INPUTS = [
    DATA_DIR / "filtered" / "filtered_medicines.json",
    DATA_DIR / "filtered" / "filtered_torzon_medicines.json",
]
DEFAULT_OUTPUT_JSON = DATA_DIR / "filtered" / "filtered_medicines_llm.json"
DEFAULT_OUTPUT_CSV = DATA_DIR / "filtered" / "filtered_medicines_llm.csv"
DEFAULT_CACHE = DATA_DIR / "llm_cache.json"

DEFAULT_BASE_URL = "http://localhost:1234/v1"   # LM Studio default
DEFAULT_MODEL = "qwen2.5-7b-instruct-1m"

LLM_FIELDS = ["llm_relevant", "llm_category", "llm_product_type", "llm_confidence", "llm_reason"]

SYSTEM_PROMPT = (
    "You are a research assistant screening darknet-market drug listings for a public-health "
    "study on access to CONTRACEPTION and ABORTION medicines.\n"
    "Decide whether a listing is genuinely such a medicine.\n"
    "- contraception: birth-control pills, patches, rings, IUDs, emergency contraception, and "
    "their hormones used FOR contraception (e.g. ethinylestradiol + a progestin, levonorgestrel).\n"
    "- abortion: medical-abortion drugs (mifepristone, misoprostol/cytotec) and their kits.\n"
    "- none: anything else. In particular, ANABOLIC STEROIDS (e.g. drostanolone/masteron, "
    "testosterone, trenbolone) are NOT contraceptives even though their descriptions often "
    "mention 'estrogen', 'estrogen blocker', or 'estrogenic' as a side-effect. Vitamins, "
    "erectile-dysfunction drugs, recreational drugs, and general HRT are also 'none'.\n"
    "Judge by what the product IS, not by an incidentally mentioned hormone word.\n"
    "Respond ONLY with a JSON object matching the requested schema."
)

# Few-shot exemplars drawn from real false/true positives, fed as prior turns.
FEWSHOT = [
    ("Market: THE X WAVE MARKET\nTitle: Drostanolone-P 100 mg 10 ml – 1 vial\n"
     "Keyword hits: Estrogen\nDescription: Drostanolone-P is also noted as being an effective "
     "estrogen blocker, and also binds to SHBG...",
     {"relevant": False, "category": "none", "product_type": "anabolic steroid",
      "confidence": 0.97, "reason": "Injectable anabolic steroid; 'estrogen' is only a side-effect mention."}),
    ("Market: THE X WAVE MARKET\nTitle: Yasmin (Drospirenone/Ethinylestradiol) 28 tabs\n"
     "Keyword hits: Yasmin, Ethinyl estradiol\nDescription: Combined oral contraceptive pill.",
     {"relevant": True, "category": "contraception", "product_type": "combined oral contraceptive",
      "confidence": 0.98, "reason": "Drospirenone + ethinylestradiol is a combined birth-control pill."}),
    ("Market: Drug Hub\nTitle: Mifepristone + Misoprostol abortion kit\n"
     "Keyword hits: Mifepristone, Misoprostol\nDescription: Complete medical abortion kit.",
     {"relevant": True, "category": "abortion", "product_type": "medical abortion kit",
      "confidence": 0.99, "reason": "Mifepristone/misoprostol is the standard medical-abortion regimen."}),
]

JSON_SCHEMA = {
    "name": "product_eval",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "relevant": {"type": "boolean"},
            "category": {"type": "string", "enum": ["contraception", "abortion", "none"]},
            "product_type": {"type": "string"},
            "confidence": {"type": "number"},
            "reason": {"type": "string"},
        },
        "required": ["relevant", "category", "product_type", "confidence", "reason"],
    },
}


def load_records(path: Path) -> List[Dict[str, object]]:
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, list):
        raise ValueError(f"{path} must contain a JSON list of records")
    return [r for r in data if isinstance(r, dict)]


def load_cache(path: Path) -> Dict[str, dict]:
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        print(colored(f"⚠️  Could not read cache {path}: {exc} (ignoring)", "yellow"))
        return {}


def save_cache(path: Path, cache: Dict[str, dict]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as fh:
            json.dump(cache, fh, ensure_ascii=False, indent=2)
    except Exception as exc:
        print(colored(f"⚠️  Could not write cache {path}: {exc}", "yellow"))


def cache_key(model: str, record: Dict[str, object], max_desc: int) -> str:
    payload = "\n".join([
        model,
        str(record.get("listing_title", "")),
        str(record.get("market_name", "")),
        str(record.get("description", ""))[:max_desc],
    ])
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def build_user_message(record: Dict[str, object], max_desc: int) -> str:
    desc = str(record.get("description", ""))
    if len(desc) > max_desc:
        desc = desc[:max_desc] + "..."
    return (
        f"Market: {record.get('market_name', '')}\n"
        f"Title: {record.get('listing_title', '')}\n"
        f"Keyword hits: {record.get('matched_terms', '')}\n"
        f"Description: {desc}"
    )


def _parse_json_lenient(content: str) -> Optional[dict]:
    """Parse a JSON object from a model response, tolerating code fences / prose."""
    if not content:
        return None
    content = content.strip()
    # Strip ```json ... ``` fences if present.
    if content.startswith("```"):
        content = re.sub(r"^```[a-zA-Z]*\n?", "", content)
        content = re.sub(r"\n?```$", "", content).strip()
    try:
        return json.loads(content)
    except Exception:
        pass
    # Fall back to the first {...} block.
    m = re.search(r"\{.*\}", content, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            return None
    return None


def call_llm(base_url: str, model: str, user_msg: str, timeout: int) -> Optional[dict]:
    """One chat-completion call returning the parsed verdict dict, or None on failure.

    Tries strict json_schema structured output first, then falls back to json_object,
    then plain text -- so it works across OpenAI-compatible servers of varying support.
    """
    url = f"{base_url.rstrip('/')}/chat/completions"
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    for ex_user, ex_json in FEWSHOT:
        messages.append({"role": "user", "content": ex_user})
        messages.append({"role": "assistant", "content": json.dumps(ex_json)})
    messages.append({"role": "user", "content": user_msg})

    base_payload = {"model": model, "messages": messages, "temperature": 0, "max_tokens": 300}
    response_formats = [
        {"type": "json_schema", "json_schema": JSON_SCHEMA},
        {"type": "json_object"},
        None,
    ]

    for rf in response_formats:
        payload = dict(base_payload)
        if rf is not None:
            payload["response_format"] = rf
        try:
            resp = requests.post(url, json=payload, timeout=timeout)
        except requests.RequestException as exc:
            print(colored(f"    ⚠️  request failed: {exc}", "yellow"))
            return None
        if resp.status_code == 400 and rf is not None:
            # Server rejected this response_format; try the next, simpler one.
            continue
        if resp.status_code != 200:
            print(colored(f"    ⚠️  HTTP {resp.status_code}: {resp.text[:200]}", "yellow"))
            return None
        try:
            content = resp.json()["choices"][0]["message"]["content"]
        except Exception as exc:
            print(colored(f"    ⚠️  unexpected response shape: {exc}", "yellow"))
            return None
        parsed = _parse_json_lenient(content)
        if parsed is not None:
            return parsed
        # Valid HTTP but unparsable body → try the next response_format.
    return None


def _normalise_verdict(raw: Optional[dict]) -> Dict[str, object]:
    """Coerce a raw model dict into the five llm_* fields (null verdict on failure)."""
    if not isinstance(raw, dict):
        return {"llm_relevant": None, "llm_category": "", "llm_product_type": "",
                "llm_confidence": "", "llm_reason": "evaluation failed / unparsable"}
    cat = str(raw.get("category", "")).strip().lower()
    if cat not in ("contraception", "abortion", "none"):
        cat = "none"
    rel = raw.get("relevant")
    rel = bool(rel) if isinstance(rel, (bool, int)) else None
    try:
        conf = round(float(raw.get("confidence")), 3)
    except (TypeError, ValueError):
        conf = ""
    return {
        "llm_relevant": rel,
        "llm_category": cat,
        "llm_product_type": str(raw.get("product_type", "")).strip(),
        "llm_confidence": conf,
        "llm_reason": str(raw.get("reason", "")).strip(),
    }


def check_server(base_url: str, model: str) -> None:
    """Best-effort connectivity/model check; warns but never blocks the run."""
    try:
        resp = requests.get(f"{base_url.rstrip('/')}/models", timeout=10)
        names = [m.get("id") for m in resp.json().get("data", [])]
        if model not in names:
            print(colored(f"⚠️  Model {model!r} not in server list {names}. Proceeding anyway "
                          "(use --model to match).", "yellow"))
        else:
            print(colored(f"✅ Server reachable; model {model!r} loaded.", "green"))
    except Exception as exc:
        print(colored(f"⚠️  Could not reach {base_url}/models: {exc}\n"
                      "   Make sure the local server is running (LM Studio → Start Server).", "yellow"))


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Local-LLM relevance evaluation for filtered listings")
    p.add_argument("--input", "-i", type=Path, nargs="+", default=DEFAULT_INPUTS,
                   help="One or more filtered listings JSON files to evaluate; their records are "
                        "concatenated (default: merged markets + TorZon)")
    p.add_argument("--output", "-o", type=Path, default=DEFAULT_OUTPUT_JSON,
                   help=f"Annotated JSON output (default: {DEFAULT_OUTPUT_JSON})")
    p.add_argument("--csv-output", type=Path, default=DEFAULT_OUTPUT_CSV,
                   help=f"Annotated CSV output (default: {DEFAULT_OUTPUT_CSV})")
    p.add_argument("--base-url", default=DEFAULT_BASE_URL,
                   help=f"OpenAI-compatible API base (default: {DEFAULT_BASE_URL})")
    p.add_argument("--model", default=DEFAULT_MODEL, help=f"Model id (default: {DEFAULT_MODEL})")
    p.add_argument("--cache", type=Path, default=DEFAULT_CACHE,
                   help=f"Verdict cache for resumable reruns (default: {DEFAULT_CACHE})")
    p.add_argument("--no-cache", action="store_true", help="Ignore and do not write the cache")
    p.add_argument("--max-desc-chars", type=int, default=800, help="Truncate description to N chars")
    p.add_argument("--timeout", type=int, default=120, help="Per-request timeout in seconds")
    p.add_argument("--limit", type=int, default=None, help="Only evaluate the first N records (smoke test)")
    p.add_argument("--relevant-only", action="store_true",
                   help="Write only records the LLM judged relevant")
    return p.parse_args()


def main() -> None:
    args = parse_args()

    records: List[Dict[str, object]] = []
    for path in args.input:
        if not path.exists():
            print(colored(f"  skip (missing): {path}", "yellow"))
            continue
        chunk = load_records(path)
        print(colored(f"  loaded {len(chunk)} from {path.name}", "green"))
        records.extend(chunk)
    if not records:
        raise SystemExit("No records found in any input file; nothing to evaluate.")
    if args.limit:
        records = records[: args.limit]
    print(colored(f"✅ {len(records)} record(s) to evaluate", "green"))

    check_server(args.base_url, args.model)
    cache = {} if args.no_cache else load_cache(args.cache)

    evaluated: List[Dict[str, object]] = []
    n_cached = n_called = n_failed = 0

    for i, record in enumerate(records, 1):
        key = cache_key(args.model, record, args.max_desc_chars)
        if not args.no_cache and key in cache:
            verdict = cache[key]
            n_cached += 1
        else:
            raw = call_llm(args.base_url, args.model,
                           build_user_message(record, args.max_desc_chars), args.timeout)
            verdict = _normalise_verdict(raw)
            n_called += 1
            if verdict["llm_relevant"] is None:
                n_failed += 1
            if not args.no_cache and raw is not None:
                cache[key] = verdict
                save_cache(args.cache, cache)  # persist per item → resumable

        merged = {**record, **verdict}
        evaluated.append(merged)

        rel = verdict["llm_relevant"]
        tag = ("✅ relevant" if rel else "✗ reject") if rel is not None else "⚠️ undecided"
        colour = "green" if rel else ("yellow" if rel is None else "red")
        print(colored(f"  [{i}/{len(records)}] {tag} "
                      f"[{verdict.get('llm_category','')}] "
                      f"{str(record.get('listing_title',''))[:55]}", colour))

    out = [r for r in evaluated if r.get("llm_relevant")] if args.relevant_only else evaluated

    headers = determine_columns(out) if out else []
    # Make sure the llm_* columns are present even if a writer reorders.
    for f in LLM_FIELDS:
        if f not in headers:
            headers.append(f)
    write_json(out, args.output)
    write_csv(out, headers, args.csv_output)

    n_relevant = sum(1 for r in evaluated if r.get("llm_relevant") is True)
    n_reject = sum(1 for r in evaluated if r.get("llm_relevant") is False)
    print(colored(f"\n{'='*70}", "cyan"))
    print(colored("📊 LLM evaluation summary", "cyan", attrs=["bold"]))
    print(colored(f"   evaluated : {len(evaluated)}  (cached {n_cached}, called {n_called})", "white"))
    print(colored(f"   relevant  : {n_relevant}", "green"))
    print(colored(f"   rejected  : {n_reject}", "red"))
    print(colored(f"   undecided : {n_failed}", "yellow"))
    print(colored(f"   wrote     : {len(out)} record(s) → {args.output.name} / {args.csv_output.name}", "white"))


if __name__ == "__main__":
    main()
