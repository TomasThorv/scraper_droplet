#!/usr/bin/env python3
"""
clean_image_list.py – filter a raw tab‑separated list of
    SKU <TAB> Name <TAB> Image_URL

Rules
-----
1. **Empty URL** → drop the row.
2. URL must contain the SKU (case‑insensitive, any substring). Otherwise drop.

Usage
-----
    python clean_image_list.py raw_images.txt filtered_images.txt

If you omit the filenames it defaults to:
    input  = raw_images.txt
    output = filtered_images.txt

The output file preserves the original three‑column layout and order.
"""

import sys
from pathlib import Path

DEFAULT_IN = "files/product_images.txt"
DEFAULT_OUT = "files/filtered_images.txt"


def keep_row(sku: str, url: str) -> bool:
    """Return True if URL is non‑empty and contains the SKU (case‑insens)."""
    if not url:
        return False
    return sku.lower() in url.lower()


def main(argv: list[str]) -> None:
    in_path = Path(argv[1]) if len(argv) > 1 else Path(DEFAULT_IN)
    out_path = Path(argv[2]) if len(argv) > 2 else Path(DEFAULT_OUT)

    if not in_path.exists():
        sys.exit(f"❌ Input file '{in_path}' not found.")

    rows_in = in_path.read_text(encoding="utf-8").splitlines()
    rows_out = []

    for ln in rows_in:
        parts = ln.split("\t", 2)  # at most 3 splits; keeps names with tabs intact
        if len(parts) < 3:
            continue  # missing URL column
        sku, name, url = (p.strip() for p in parts[:3])
        if keep_row(sku, url):
            rows_out.append(ln)

    out_path.write_text("\n".join(rows_out), encoding="utf-8")
    print(f"✅ Wrote {len(rows_out)} of {len(rows_in)} lines to '{out_path}'.")


if __name__ == "__main__":
    main(sys.argv)
