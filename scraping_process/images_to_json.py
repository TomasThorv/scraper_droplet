#!/usr/bin/env python3
"""
images_to_json.py – Convert a cleaned, tab-separated image list into the
nested-JSON structure you showed.

Expected input (default: ``filtered_images.txt``)
================================================
```
SKU<TAB>Name<TAB>Image_URL
SKU<TAB>Name<TAB>Image_URL
...
```

• Lines with an empty URL should already have been filtered out by
  ``clean_image_list.py`` – but they’re skipped again here just in case.
• Duplicate image URLs for the same SKU are removed while preserving order.

Command-line usage
==================
```
python images_to_json.py [input.tsv] [output.json]
```
Defaults:
    input  = filtered_images.txt
    output = images.json
"""

import json
import sys
from collections import OrderedDict
from pathlib import Path
from typing import Dict, List, Set, Tuple

DEFAULT_IN = "files/filtered_images.txt"
DEFAULT_OUT = "files/images.json"


def _normalise_sku(sku: str) -> str:
    return sku.strip().upper()


def load_existing_json(path: Path) -> Tuple["OrderedDict[str, List[str]]", Dict[str, Set[str]], Dict[str, str], Dict[str, str]]:
    """Load the existing JSON so Nike/Puma images aren't lost."""

    mapping: "OrderedDict[str, List[str]]" = OrderedDict()
    seen_per_sku: Dict[str, Set[str]] = {}
    names: Dict[str, str] = {}
    original_skus: Dict[str, str] = {}

    if not path.exists():
        return mapping, seen_per_sku, names, original_skus

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        # Corrupt JSON? Start fresh instead of crashing – the cleaner will
        # repopulate the file below.
        return mapping, seen_per_sku, names, original_skus

    if not isinstance(data, list):
        return mapping, seen_per_sku, names, original_skus

    for entry in data:
        if not isinstance(entry, dict):
            continue
        raw_sku = str(entry.get("sku", "")).strip()
        if not raw_sku:
            continue
        sku_key = _normalise_sku(raw_sku)
        images = entry.get("images") or []
        if sku_key not in mapping:
            mapping[sku_key] = []
            seen_per_sku[sku_key] = set()
            original_skus[sku_key] = raw_sku
        name = entry.get("name", "")
        if name:
            names.setdefault(sku_key, str(name))
        for url in images:
            url = str(url).strip()
            if not url:
                continue
            if url not in seen_per_sku[sku_key]:
                mapping[sku_key].append(url)
                seen_per_sku[sku_key].add(url)

    return mapping, seen_per_sku, names, original_skus


def update_mapping_from_lines(
    lines: List[str],
    mapping: "OrderedDict[str, List[str]]",
    seen_per_sku: Dict[str, Set[str]],
    names: Dict[str, str],
    original_skus: Dict[str, str],
):
    """Update mapping with rows from the TSV list produced by the scraper."""

    for ln in lines:
        parts = ln.split("\t", 2)
        if len(parts) < 3:
            continue  # malformed
        sku, name, url = (p.strip() for p in parts[:3])
        if not sku:
            continue
        sku_key = _normalise_sku(sku)
        if sku_key not in mapping:
            mapping[sku_key] = []
            seen_per_sku[sku_key] = set()
            original_skus[sku_key] = sku
        elif sku_key not in original_skus:
            original_skus[sku_key] = sku

        if name and not names.get(sku_key):
            names[sku_key] = name

        if not url:
            continue
        if url not in seen_per_sku[sku_key]:
            mapping[sku_key].append(url)
            seen_per_sku[sku_key].add(url)


def build_output(
    mapping: "OrderedDict[str, List[str]]",
    names: Dict[str, str],
    original_skus: Dict[str, str],
) -> List[dict]:
    output: List[dict] = []
    for sku_key, urls in mapping.items():
        if not urls:
            continue
        sku_value = original_skus.get(sku_key, sku_key)
        record: dict = {"sku": sku_value, "images": urls}
        name = names.get(sku_key, "").strip()
        if name:
            record["name"] = name
        output.append(record)
    return output


def main(argv: list[str]):
    in_path = Path(argv[1]) if len(argv) > 1 else Path(DEFAULT_IN)
    out_path = Path(argv[2]) if len(argv) > 2 else Path(DEFAULT_OUT)

    if not in_path.exists():
        sys.exit(f"❌ Input file '{in_path}' not found.")

    existing_mapping, seen_per_sku, names, original_skus = load_existing_json(out_path)

    lines = in_path.read_text(encoding="utf-8").splitlines()
    update_mapping_from_lines(lines, existing_mapping, seen_per_sku, names, original_skus)

    output = build_output(existing_mapping, names, original_skus)

    out_path.write_text(json.dumps(output, indent=4), encoding="utf-8")
    print(f"✅ Wrote {len(output)} SKUs → '{out_path}'.")


if __name__ == "__main__":
    main(sys.argv)
