#!/usr/bin/env python3
"""
run_all.py – Simple script runner
Run sku_search2.py then image_scraper.py then product_image_cleaner.py then images_to_json.py
"""

import subprocess
import sys


def main():
    print("Running SKU filter...")
    result0 = subprocess.run([sys.executable, "scraping_process/filter_skus.py"])

    print("\nRunning SKU search...")
    result1 = subprocess.run([sys.executable, "scraping_process/sku_search_sites.py"])

    print("\nRunning image scraper...")
    result2 = subprocess.run([sys.executable, "scraping_process/image_scraper.py"])

    print("\nRunning product image cleaner...")
    result3 = subprocess.run(
        [sys.executable, "scraping_process/product_image_cleaner.py"]
    )

    print("\nRunning images to JSON...")
    result4 = subprocess.run([sys.executable, "scraping_process/images_to_json.py"])

    print(
        "\nRunning merge_product_images (merge product_images.txt into files/images.json)..."
    )
    result5 = subprocess.run(
        [sys.executable, "scraping_process/merge_product_images.py"]
    )

    print("\nRunning fix_foot_store_images (filter+sort media.foot-store.com URLs)...")
    result6 = subprocess.run(
        [sys.executable, "scraping_process/fix_foot_store_images.py"]
    )

    print(
        "\nRunning sort_images_by_brand (sort images within each SKU by brand priority)..."
    )
    result7 = subprocess.run(
        [sys.executable, "scraping_process/sort_images_by_brand.py"]
    )

    print("\nDone!")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\nPipeline interrupted by user")
        sys.exit(1)
    except Exception as e:
        print(f"\nUnexpected error: {e}")
        sys.exit(1)
