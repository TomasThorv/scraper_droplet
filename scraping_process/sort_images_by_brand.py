#!/usr/bin/env python3
"""
Sort existing images.json by brand priority order
"""

import json
from pathlib import Path

# Define brand priority order (from sku_search_sites.py)
BRAND_PRIORITY = [
    "nike.com",
    "puma.com",
    "foot-store.com",
    "adidas.com",
    "solesense.com",
    "footy.com",
    "kickscrew.com",
    "soccervillage.com",
    "goat.com",
    "soccerpost.com",
    "sportano.com",
    "soccerandrugby.com",
    "adsport.store",
    "mybrand.shoes",
    "u90soccer.com",
    "yoursportsperformance.com",
    "authenticsoccer.com",
]


def get_image_brand_priority(image_url):
    """Get brand priority for a single image URL. Lower number = higher priority."""
    # Check which domain this image belongs to
    for priority, domain in enumerate(BRAND_PRIORITY):
        if domain in image_url:
            return priority

    # If no match found, put at end
    return 999


def sort_images_within_sku(entry):
    """Sort images within a single SKU entry by brand priority."""
    if not entry.get("images"):
        return entry

    # Sort images by brand priority
    sorted_images = sorted(entry["images"], key=get_image_brand_priority)

    # Return updated entry
    return {"sku": entry["sku"], "images": sorted_images}


def main():
    images_json_path = Path("files/images.json")

    if not images_json_path.exists():
        print(f"❌ {images_json_path} not found!")
        return

    # Load existing images.json
    print(f"📖 Loading {images_json_path}...")
    with open(images_json_path, "r", encoding="utf-8") as f:
        images_data = json.load(f)

    print(f"📊 Found {len(images_data)} SKU entries")

    # Show example of current mixed ordering
    print("\n🔍 Example of current image ordering (first SKU with multiple brands):")
    for entry in images_data:
        if len(entry.get("images", [])) > 1:
            print(f"   SKU: {entry['sku']}")
            brands_found = []
            for img in entry["images"][:3]:  # Show first 3 images
                for domain in BRAND_PRIORITY:
                    if domain in img:
                        brands_found.append(domain)
                        break
                else:
                    brands_found.append("unknown")
            print(f"   Current order: {' → '.join(brands_found)}")
            break

    # Sort images within each SKU by brand priority
    print("\n🔄 Sorting images within each SKU by brand priority...")
    sorted_data = []
    changes_made = 0

    for entry in images_data:
        original_images = entry.get("images", [])
        sorted_entry = sort_images_within_sku(entry)
        sorted_images = sorted_entry.get("images", [])

        # Check if order changed
        if original_images != sorted_images:
            changes_made += 1

        sorted_data.append(sorted_entry)

    print(f"📈 Made changes to {changes_made} SKU entries")

    # Show example of new ordering
    print("\n✅ Example of new image ordering (same SKU):")
    for entry in sorted_data:
        if len(entry.get("images", [])) > 1:
            print(f"   SKU: {entry['sku']}")
            brands_found = []
            for img in entry["images"][:3]:  # Show first 3 images
                for domain in BRAND_PRIORITY:
                    if domain in img:
                        brands_found.append(domain)
                        break
                else:
                    brands_found.append("unknown")
            print(f"   New order: {' → '.join(brands_found)}")
            break

    # Create backup
    backup_path = images_json_path.with_suffix(".json.bak")
    print(f"\n💾 Creating backup at {backup_path}...")
    with open(backup_path, "w", encoding="utf-8") as f:
        with open(images_json_path, "r", encoding="utf-8") as original:
            backup_data = json.load(original)
        json.dump(backup_data, f, indent=2)

    # Write sorted data
    print(f"✍️  Writing sorted data to {images_json_path}...")
    with open(images_json_path, "w", encoding="utf-8") as f:
        json.dump(sorted_data, f, indent=2)

    print("✅ Done! Images within each SKU sorted by brand priority.")
    print(
        f"   Priority order: {' → '.join(BRAND_PRIORITY[:3])}... (Nike first, then Puma, then foot-store, etc.)"
    )


if __name__ == "__main__":
    main()
