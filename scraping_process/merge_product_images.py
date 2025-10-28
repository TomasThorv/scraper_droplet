#!/usr/bin/env python3
"""
Merge product_images.txt into files/images.json.

Behavior:
- Read `scraper/files/product_images.txt` (tab-separated: sku\tname\timage_url)
- Normalize SKUs by replacing '_' with '-' (only for merging step).
- Merge entries into `scraper/files/images.json` (array of {sku, images}).
- If a SKU already exists, append new image URLs that are not already present (preserve order).

The output intentionally omits product `name` to keep the JSON compact.
"""

from __future__ import annotations

import json
from collections import OrderedDict
from pathlib import Path
from typing import Dict, List


ROOT = Path(__file__).resolve().parents[2]
FILES_DIR = ROOT / "scraper" / "files"
PRODUCT_IMAGES_TXT = FILES_DIR / "product_images.txt"
IMAGES_JSON = FILES_DIR / "images.json"

# URLs or substrings we never want to appear in images.json
BANNED_PATTERNS = [
    "assets.solesense.com/site/images/logos/solesense/square-black.png",
    "https://media.foot-store.com/lazyload/websites/1/fs.jpg",
]


def _is_banned(url: str) -> bool:
    if not url:
        return False
    lower = url.lower()
    for pat in BANNED_PATTERNS:
        if pat in lower:
            return True
    return False


def _is_foot_store(url: str) -> bool:
    return bool(url and "media.foot-store.com" in url)


def _foot_store_url_contains_sku(url: str, sku: str) -> bool:
    """Return True if the given foot-store url contains the sku in either '-' or '_' form.

    Comparison is case-insensitive. If sku is empty, returns False.
    """
    if not url or not sku:
        return False
    low = url.lower()
    sku_dash = sku.replace("_", "-").lower()
    sku_underscore = sku.replace("-", "_").lower()
    return sku_dash in low or sku_underscore in low


def load_existing() -> "OrderedDict[str, Dict]":
    """Load existing images.json into an ordered dict keyed by normalized SKU."""
    out: "OrderedDict[str, Dict]" = OrderedDict()
    if not IMAGES_JSON.exists():
        return out
    try:
        data = json.loads(IMAGES_JSON.read_text(encoding="utf8"))
    except Exception:
        # If file is corrupt, back it up and start fresh
        backup = IMAGES_JSON.with_suffix(".json.bak")
        IMAGES_JSON.replace(backup)
        return out

    for entry in data:
        sku = entry.get("sku")
        if not sku:
            continue
        norm = normalize_sku(sku)
        images = entry.get("images") or []
        # drop banned images and preserve order
        filtered = [u for u in images if not _is_banned(u)]
        out[norm] = {"sku": sku, "images": list(filtered)}
    return out


def normalize_sku(sku: str) -> str:
    """Normalize SKU for keying: replace underscores with dashes and uppercase.

    We keep the original case/format in the stored `sku` field, but use a
    canonical uppercase-with-dashes key for merging.
    """
    if sku is None:
        return ""
    return sku.replace("_", "-").upper()


def read_product_images() -> List[Dict]:
    """Read product_images.txt and return list of dicts: {sku, name, url}.

    Expected to be tab-separated. Skip lines without url.
    """
    if not PRODUCT_IMAGES_TXT.exists():
        return []
    rows: List[Dict] = []
    for raw in PRODUCT_IMAGES_TXT.read_text(encoding="utf8").splitlines():
        if not raw.strip():
            continue
        parts = raw.split("\t")
        # expected: sku, name, url (some rows may be shorter)
        sku = parts[0].strip() if len(parts) > 0 else ""
        name = parts[1].strip() if len(parts) > 1 else ""
        url = parts[2].strip() if len(parts) > 2 else ""
        if not sku or not url:
            continue
        rows.append({"sku": sku, "name": name, "url": url})
    return rows


def merge(
    existing: "OrderedDict[str, Dict]", rows: List[Dict]
) -> "OrderedDict[str, Dict]":
    """Merge rows into existing mapping and return updated ordered dict."""
    for row in rows:
        raw_sku = row["sku"]
        url = row["url"]
        name = row.get("name", "")
        key = normalize_sku(raw_sku)

        if _is_banned(url):
            # skip any banned images
            continue

        if key in existing:
            images = existing[key]["images"]
            if url not in images:
                images.append(url)
        else:
            # create new entry; keep original sku formatting but normalize underscores->dashes
            stored_sku = raw_sku.replace("_", "-")
            existing[key] = {"sku": stored_sku, "images": [url]}
    return existing


def write_out(mapping: "OrderedDict[str, Dict]") -> None:
    """Write the ordered mapping back to IMAGES_JSON as a list, preserving order."""
    out_list = []
    for key, v in mapping.items():
        imgs = [u for u in (v.get("images", []) or []) if not _is_banned(u)]
        # For media.foot-store.com URLs: ensure the URL contains the SKU (dash or underscore form)
        sku = (v.get("sku") or "").strip()
        imgs = [
            u
            for u in imgs
            if not (_is_foot_store(u) and not _foot_store_url_contains_sku(u, sku))
        ]
        # numeric-sort foot-store urls by the _<number> suffix (ascending)
        imgs = _sort_foot_store_images(imgs)
        out_list.append({"sku": v.get("sku"), "images": imgs})

    IMAGES_JSON.write_text(
        json.dumps(out_list, indent=4, ensure_ascii=False), encoding="utf8"
    )


def main() -> None:
    existing = load_existing()
    rows = read_product_images()
    if not rows:
        # No new rows — but still rewrite existing file using the canonical shape
        # (this will remove any `name` fields that may be present).
        if existing:
            write_out(existing)
            print(f"Rewrote {IMAGES_JSON} with {len(existing)} SKUs (names removed)")
        else:
            print(
                "No product_images rows found and no existing images.json to migrate; nothing to do."
            )
        return

    merged = merge(existing, rows)
    write_out(merged)
    print(f"Merged {len(rows)} rows into {IMAGES_JSON} (total SKUs: {len(merged)})")


def _sort_foot_store_images(urls: List[str]) -> List[str]:
    """Stable sort of media.foot-store.com URLs by numeric suffix after SKU.

    For each url that contains media.foot-store.com and matches pattern '_<digits>' after the SKU
    we extract the first such number and sort ascending. Other URLs stay in their original positions.
    """
    if not urls:
        return []
    import re

    # Collect positions and parsed index (None if not foot-store or no index)
    annotated = []
    for i, u in enumerate(urls):
        if not u or "media.foot-store.com" not in u:
            annotated.append((i, u, None))
            continue
        # find first _<digits> after sku; the regex picks the first group of digits preceded by underscore
        m = re.search(r"_([0-9]+)(?:[^/]*)?(?:\.|$)", u)
        idx = int(m.group(1)) if m else None
        annotated.append((i, u, idx))

    # Extract entries that have numeric index
    foot_with_idx = [(i, u, idx) for (i, u, idx) in annotated if idx is not None]
    if not foot_with_idx:
        return [u for _, u, _ in annotated]

    # Sort the foot entries by idx then original index to stabilize
    foot_sorted = sorted(foot_with_idx, key=lambda t: (t[2], t[0]))
    foot_iter = iter([u for _, u, _ in foot_sorted])

    # Rebuild result replacing foot entries in order with sorted ones
    result = []
    for i, u, idx in annotated:
        if idx is not None and "media.foot-store.com" in (u or ""):
            result.append(next(foot_iter))
        else:
            result.append(u)

    return result


if __name__ == "__main__":
    main()
