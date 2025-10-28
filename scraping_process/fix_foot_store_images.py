"""
Fix media.foot-store.com image lists in scraper/files/images.json
- Remove foot-store URLs that do not contain the SKU (tolerant to '-', '_', ' ')
- Sort foot-store URLs by numeric suffix after the SKU (ascending)
- Backup original images.json to images.json.bak.TIMESTAMP
"""

import json
import re
from pathlib import Path
from datetime import datetime

IMAGES_JSON = Path(__file__).resolve().parents[1] / "files" / "images.json"

FOOT_HOST = "media.foot-store.com"


# tolerant SKU forms to check inside URL
def sku_variants(sku: str):
    s = sku.strip()
    variants = set()
    variants.add(s)
    variants.add(s.replace("_", "-"))
    variants.add(s.replace("-", "_"))
    variants.add(s.replace("-", " "))
    variants.add(s.replace("_", " "))
    variants.add(s.replace(" ", "-"))
    variants.add(s.replace(" ", "_"))
    # also lowercase/uppercase
    variants.add(s.lower())
    variants.add(s.upper())
    return variants


# extract numeric suffix immediately following the sku in a foot-store URL
# e.g. .../106045-02_3_... or .../puma_106045-02_3.jpg -> returns 3
NUM_SUFFIX_RE = re.compile(r"(?:%s)[^\d]*_([0-9]+)" % r"%s")


# More robust: find the sku (with possible separators) then an underscore and number
def extract_suffix_for_url(url: str, sku: str):
    # Build a regex that matches the SKU with -, _, or space in between, case-insensitive
    # Escape sku for regex parts
    esc = re.escape(sku)
    # allow the sku to appear with - or _ or space between parts (but as-is also works)
    # We'll accept any occurrence of the SKU then an underscore and digits
    m = re.search(esc + r"_([0-9]+)", url, flags=re.IGNORECASE)
    if m:
        try:
            return int(m.group(1))
        except Exception:
            return None
    # try with hyphen/underscore space variants
    alt = esc.replace(r"\-", r"[-_ ]")
    m = re.search(alt + r"_([0-9]+)", url, flags=re.IGNORECASE)
    if m:
        try:
            return int(m.group(1))
        except Exception:
            return None
    # fallback: search for last underscore digits in the filename
    m = re.search(r"_([0-9]+)(?:[^0-9]|$)", url)
    if m:
        try:
            return int(m.group(1))
        except Exception:
            return None
    return None


def is_foot_store_url(url: str):
    return FOOT_HOST in url


def foot_store_url_contains_sku(url: str, sku: str):
    variants = sku_variants(sku)
    lower = url.lower()
    for v in variants:
        if v.lower() in lower:
            return True
    return False


def process_entry(entry: dict):
    sku = entry.get("sku")
    if not sku:
        return entry
    images = entry.get("images") or []
    foot = [u for u in images if is_foot_store_url(u)]
    others = [u for u in images if not is_foot_store_url(u)]

    # filter foot-store images that don't contain the sku
    foot_kept = [u for u in foot if foot_store_url_contains_sku(u, sku)]

    # sort foot_kept by extracted numeric suffix if present, otherwise keep original order
    def keyfn(u):
        v = extract_suffix_for_url(u, sku)
        return (v is None, v if v is not None else 10**9)

    foot_kept_sorted = sorted(foot_kept, key=keyfn)

    # combine back: put sorted foot-store images first then others (preserve others order)
    new_images = foot_kept_sorted + [u for u in others if u not in foot_kept_sorted]

    # remove duplicates while preserving order
    seen = set()
    deduped = []
    for u in new_images:
        if u not in seen:
            deduped.append(u)
            seen.add(u)

    entry["images"] = deduped
    return entry


def main():
    if not IMAGES_JSON.exists():
        print(f"images.json not found at {IMAGES_JSON}")
        return
    data = json.loads(IMAGES_JSON.read_text(encoding="utf-8"))
    # backup
    bak = IMAGES_JSON.with_suffix(
        ".json.bak." + datetime.utcnow().strftime("%Y%m%d%H%M%S")
    )
    bak.write_text(json.dumps(data, ensure_ascii=False, indent=4), encoding="utf-8")
    print(f"Backed up original to {bak}")

    new = []
    for entry in data:
        new.append(process_entry(entry.copy()))

    IMAGES_JSON.write_text(
        json.dumps(new, ensure_ascii=False, indent=4), encoding="utf-8"
    )
    print(f"Wrote fixed images.json ({len(new)} SKUs)")


if __name__ == "__main__":
    main()
