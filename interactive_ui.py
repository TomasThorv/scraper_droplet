#!/usr/bin/env python3
"""Simple interactive helper to run the scraping pipeline and curate results."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List


FILES_DIR = Path("files")
SKUS_PATH = FILES_DIR / "skus.txt"
IMAGES_PATH = FILES_DIR / "images.json"


def _normalise_sku(value: str) -> str:
    return value.strip().upper()


def prompt_for_skus() -> List[str]:
    print("Enter the SKUs you want to scrape. Press Enter on an empty line to finish.")
    print("Duplicates will be removed while preserving order.\n")

    skus: List[str] = []
    seen: set[str] = set()

    while True:
        try:
            entered = input("SKU: ").strip()
        except EOFError:
            print()
            break

        if not entered:
            break

        key = _normalise_sku(entered)
        if key in seen:
            print("  ↺ SKU already entered; skipping duplicate.")
            continue
        seen.add(key)
        skus.append(entered)

    if not skus:
        print("No SKUs entered – nothing to do.")
        sys.exit(0)

    FILES_DIR.mkdir(parents=True, exist_ok=True)
    SKUS_PATH.write_text("\n".join(skus) + "\n", encoding="utf-8")
    print(f"\n✅ Wrote {len(skus)} SKU(s) to {SKUS_PATH}.")
    return skus


def run_pipeline() -> None:
    print("\n▶️  Running full scraping pipeline (run_all.py)…")
    result = subprocess.run([sys.executable, "run_all.py"])
    if result.returncode != 0:
        sys.exit(f"Pipeline failed with exit code {result.returncode}.")


def load_images() -> List[dict]:
    if not IMAGES_PATH.exists():
        sys.exit(f"Expected {IMAGES_PATH} to exist after the pipeline run.")

    try:
        data = json.loads(IMAGES_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        sys.exit(f"Could not parse {IMAGES_PATH}: {exc}")

    if not isinstance(data, list):
        sys.exit(f"Unexpected format in {IMAGES_PATH}: expected a list of objects.")

    return data


def prompt_for_images(entry: dict) -> List[int] | None:
    sku = str(entry.get("sku", "")).strip() or "(unknown SKU)"
    images = entry.get("images") or []

    print(f"\n📦 {sku}")

    if not images:
        print("  No images were scraped for this SKU.")
        return None

    for idx, url in enumerate(images, start=1):
        print(f"  [{idx}] {url}")

    print(
        "  Choose which images to keep. Options:\n"
        "    • Press Enter to keep all images\n"
        "    • Type numbers separated by spaces (e.g. '1 3 4')\n"
        "    • Type 'n' to drop this SKU entirely"
    )

    while True:
        response = input("  Selection: ").strip().lower()
        if response in {"", "a", "all"}:
            return list(range(len(images)))
        if response in {"n", "none"}:
            return []

        parts = response.replace(",", " ").split()
        try:
            indices = sorted({int(p) for p in parts})
        except ValueError:
            print("  ✖️  Invalid input – use numbers like '1 2 3', or Enter for all.")
            continue

        if not indices:
            print("  ✖️  No numbers recognised – try again.")
            continue

        if any(i < 1 or i > len(images) for i in indices):
            print("  ✖️  Selection outside valid range – try again.")
            continue

        # Convert to zero-based indexes for easier slicing later on.
        return [i - 1 for i in indices]


def apply_selections(
    original: List[dict], selections: Dict[str, dict | None]
) -> List[dict]:
    result: List[dict] = []
    for entry in original:
        key = _normalise_sku(str(entry.get("sku", "")))
        if key in selections:
            chosen = selections[key]
            if chosen is None:
                continue  # User opted to drop this SKU entirely
            result.append(chosen)
        else:
            result.append(entry)
    return result


def main() -> None:
    skus = prompt_for_skus()

    answer = input("\nRun the scraping pipeline now? [Y/n]: ").strip().lower()
    if answer in {"", "y", "yes"}:
        run_pipeline()
    else:
        print("Skipping pipeline run – using existing files/images.json.")

    data = load_images()

    keys_to_review = {_normalise_sku(s) for s in skus}
    filtered_map: Dict[str, dict | None] = {}

    for entry in data:
        key = _normalise_sku(str(entry.get("sku", "")))
        if key not in keys_to_review:
            continue

        selection = prompt_for_images(entry)
        if selection is None:
            filtered_map[key] = entry.copy()
            continue

        images = entry.get("images") or []
        if not selection:
            print("  ↳ Dropping this SKU from the final list.")
            filtered_map[key] = None
            continue

        chosen_images = [images[i] for i in selection if 0 <= i < len(images)]
        new_entry = dict(entry)
        new_entry["images"] = chosen_images
        filtered_map[key] = new_entry
        print(f"  ↳ Keeping {len(chosen_images)} image(s).")

    if not filtered_map:
        print("\nNo matching SKUs found in images.json – nothing to update.")
        return

    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    backup_name = f"{IMAGES_PATH.name}.bak.{timestamp}"
    backup_path = IMAGES_PATH.with_name(backup_name)
    shutil.copy2(IMAGES_PATH, backup_path)
    print(f"\n🗃️  Backed up existing images to {backup_path}.")

    updated = apply_selections(data, filtered_map)

    IMAGES_PATH.write_text(json.dumps(updated, indent=2), encoding="utf-8")
    print(f"✅ Updated {IMAGES_PATH} with your selections.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted by user.")
