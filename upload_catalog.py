"""
upload_catalog_json.py
----------------------

Bulk-upload product images to Cloudinary from a JSON catalog.

JSON format expected
--------------------
[
  {
    "sku": "ABC-123",
    "images": [
      "https://cdn.vendor.com/ABC-123/hero.jpg",
      "https://cdn.vendor.com/ABC-123/detail.jpg",
      "https://cdn.vendor.com/ABC-123/lifestyle.jpg"
    ]
  },
  {
    "sku": "XYZ-999",
    "images": [
      "https://cdn.vendor.com/XYZ-999/blue-1.png",
      "https://cdn.vendor.com/XYZ-999/blue-2.png"
    ]
  }
]

▶︎  Save that as catalog.json (or any name) and run:
    python upload_catalog_json.py catalog.json
"""

from concurrent.futures import ThreadPoolExecutor, as_completed
import argparse
import json
import os
import logging
from typing import List, Dict, Optional

import cloudinary
from cloudinary import uploader
from cloudinary.exceptions import Error as CloudinaryError
import requests
from requests.exceptions import RequestException, Timeout, ConnectionError
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from tqdm import tqdm
from dotenv import load_dotenv
import time

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.FileHandler("upload_errors.log"), logging.StreamHandler()],
)

# ---------------------------------------------------------------------------
# 1️⃣  Configure Cloudinary (reads CLOUDINARY_URL or individual vars)
# ---------------------------------------------------------------------------
load_dotenv()  # allows credentials in .env

# Check for required environment variables
required_vars = ["CLOUDINARY_CLOUD_NAME", "CLOUDINARY_API_KEY", "CLOUDINARY_API_SECRET"]
missing_vars = [var for var in required_vars if not os.getenv(var)]

if missing_vars:
    print(
        f"❌ Error: Missing required environment variables: {', '.join(missing_vars)}"
    )
    print("Please set these in your .env file or environment.")
    exit(1)

cloudinary.config(
    cloud_name=os.getenv("CLOUDINARY_CLOUD_NAME"),
    api_key=os.getenv("CLOUDINARY_API_KEY"),
    api_secret=os.getenv("CLOUDINARY_API_SECRET"),
    secure=True,
)

UPLOAD_PRESET = "product_images"  # same preset created in the console
MAX_CONCURRENCY = 5  # reduced to prevent connection pool issues
MAX_RETRIES = 3  # number of retry attempts for failed uploads
RETRY_DELAY = 1  # delay between retries in seconds
MAX_IMAGES_PER_SKU = 4  # hard cap of images uploaded per SKU

# Configure requests session with connection pooling
session = requests.Session()
retry_strategy = Retry(
    total=3,
    backoff_factor=1,
    status_forcelist=[429, 500, 502, 503, 504],
)
adapter = HTTPAdapter(
    max_retries=retry_strategy,
    pool_connections=20,  # number of connection pools
    pool_maxsize=20,  # number of connections to save in the pool
)
session.mount("http://", adapter)
session.mount("https://", adapter)


# ---------------------------------------------------------------------------
# 2️⃣  Workers
# ---------------------------------------------------------------------------
def _upload_one(url: str, sku: str, index: int) -> Optional[Dict[str, str]]:
    """
    Upload a single remote image and return minimal info.
    - url:    public URL
    - sku:    product SKU
    - index:  1-based position in the images list

    Returns None if upload fails.
    """
    for attempt in range(MAX_RETRIES):
        try:
            # First, check if the URL is accessible using the configured session
            response = session.head(url, timeout=10, allow_redirects=True)
            if response.status_code >= 400:
                logging.error(
                    f"URL not accessible ({response.status_code}): {url} for SKU {sku}"
                )
                return None

            # Some presets lock the folder; embed SKU path in public_id to force per-SKU grouping
            sku_slug = sku.replace("_", "-").strip()
            # public_id = f"{sku_slug}/{index}"  # yields products/<preset-folder>/SKU/<index> or products/SKU/<index>
            result = uploader.upload(
                url,
                folder=f"products/{sku_slug}",  # root folder; SKU handled inside public_id
                public_id=str(index),  # use numeric index only
                overwrite=True,
                invalidate=True,  # ← purge old CDN/derived caches
                use_filename=False,  # we control public_id ourselves
                unique_filename=False,
                tags=[f"sku:{sku}"],
                timeout=30,  # increased timeout
            )
            return {
                "sku": sku,
                "public_id": result["public_id"],  # products/ABC-123/1
                "secure_url": result["secure_url"],
            }

        except (ConnectionError, Timeout, RequestException) as e:
            logging.warning(
                f"Network error (attempt {attempt + 1}/{MAX_RETRIES}) for {url}: {e}"
            )
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY * (attempt + 1))  # exponential backoff
                continue
            else:
                logging.error(
                    f"Network error after {MAX_RETRIES} attempts for {url} for SKU {sku}: {e}"
                )
                return None

        except CloudinaryError as e:
            error_msg = str(e).lower()
            if "rate limit" in error_msg or "too many requests" in error_msg:
                logging.warning(
                    f"Rate limit hit (attempt {attempt + 1}/{MAX_RETRIES}) for {url}"
                )
                if attempt < MAX_RETRIES - 1:
                    time.sleep(
                        RETRY_DELAY * (attempt + 1) * 2
                    )  # longer delay for rate limits
                    continue
                else:
                    logging.error(
                        f"Rate limit error after {MAX_RETRIES} attempts for {url} for SKU {sku}: {e}"
                    )
                    return None
            else:
                logging.error(f"Cloudinary error uploading {url} for SKU {sku}: {e}")
                return None

        except Exception as e:
            if attempt < MAX_RETRIES - 1:
                logging.warning(
                    f"Unexpected error (attempt {attempt + 1}/{MAX_RETRIES}) uploading {url}: {e}"
                )
                time.sleep(RETRY_DELAY * (attempt + 1))
                continue
            else:
                logging.error(
                    f"Unexpected error after {MAX_RETRIES} attempts uploading {url} for SKU {sku}: {e}"
                )
                return None

    return None


def import_catalog(
    catalog: List[Dict], concurrency: int = MAX_CONCURRENCY
) -> List[Dict]:
    """
    catalog: list of {"sku": str, "images": [url, ...], ...}
    concurrency: number of concurrent uploads

    Returns a list of dicts with sku, public_id, secure_url.
    """
    uploaded: List[Dict] = []
    failed_uploads = []

    # Respect global SKUS_TO_IGNORE if present: skip any items whose SKU is in that list
    try:
        ignore_set = set(SKUS_TO_IGNORE)
    except NameError:
        ignore_set = set()

    filtered_catalog = []
    skipped_images = 0
    truncated_images = 0
    for item in catalog:
        sku = item.get("sku")
        if sku in ignore_set:
            logging.info(f"Skipping SKU in SKUS_TO_IGNORE: {sku}")
            skipped_images += len(item.get("images", []))
            continue
        # Enforce max images per SKU (non-destructive copy)
        images = item.get("images", [])
        if len(images) > MAX_IMAGES_PER_SKU:
            truncated_images += len(images) - MAX_IMAGES_PER_SKU
            images = images[:MAX_IMAGES_PER_SKU]
        filtered_catalog.append({**item, "images": images})

    if skipped_images:
        logging.info(f"Skipped {skipped_images} images for SKUs in SKUS_TO_IGNORE")
    if truncated_images:
        logging.info(
            f"Truncated {truncated_images} excess images beyond {MAX_IMAGES_PER_SKU} per SKU"
        )

    with ThreadPoolExecutor(concurrency) as pool:
        futures = []
        for item in filtered_catalog:
            sku = item["sku"]
            for idx, url in enumerate(item["images"], start=1):
                futures.append(pool.submit(_upload_one, url, sku, idx))

        for fut in tqdm(as_completed(futures), total=len(futures), desc="Uploading"):
            try:
                result = fut.result()
                if result is not None:
                    uploaded.append(result)
                else:
                    failed_uploads.append("Upload failed - see logs for details")
            except Exception as e:
                logging.error(f"Unexpected error getting future result: {e}")
                failed_uploads.append(str(e))

    # Log summary
    logging.info(
        f"Upload completed: {len(uploaded)} successful, {len(failed_uploads)} failed"
    )
    if failed_uploads:
        logging.warning(f"Failed uploads: {len(failed_uploads)}")

    return uploaded


# ---------------------------------------------------------------------------
# 3️⃣  CLI
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Upload product images to Cloudinary.")
    parser.add_argument("json_file", help="Path to catalog.json")
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="Enable verbose logging"
    )
    parser.add_argument(
        "--concurrency",
        "-c",
        type=int,
        default=5,
        help="Number of concurrent uploads (default: 5)",
    )
    parser.add_argument(
        "--timeout",
        "-t",
        type=int,
        default=30,
        help="Upload timeout in seconds (default: 30)",
    )
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # Use the concurrency from command line or default
    concurrency = args.concurrency

    if concurrency > 10:
        print(
            "⚠️  Warning: High concurrency may cause connection pool issues. Consider using <= 10."
        )

    try:
        with open(args.json_file, "r", encoding="utf-8") as f:
            catalog = json.load(f)
    except FileNotFoundError:
        print(f"❌ Error: File '{args.json_file}' not found.")
        return 1
    except json.JSONDecodeError as e:
        print(f"❌ Error: Invalid JSON in '{args.json_file}': {e}")
        return 1
    except Exception as e:
        print(f"❌ Error reading file '{args.json_file}': {e}")
        return 1

    if not catalog:
        print("❌ Error: Catalog is empty.")
        return 1

    # Validate catalog structure and build a cleaned list
    validated_catalog = []
    for i, item in enumerate(catalog):
        if not isinstance(item, dict):
            print(f"❌ Error: Item {i} is not a dictionary.")
            return 1
        if "sku" not in item:
            print(f"❌ Error: Item {i} missing 'sku' field.")
            return 1
        if "images" not in item:
            print(f"❌ Error: Item {i} missing 'images' field.")
            return 1
        if not isinstance(item["images"], list):
            print(f"❌ Error: Item {i} 'images' field is not a list.")
            return 1
        validated_catalog.append(item)

    # Apply SKUS_TO_IGNORE (if defined) to determine what we'll actually upload
    try:
        ignore_set = set(SKUS_TO_IGNORE)
    except NameError:
        ignore_set = set()

    filtered_catalog = []
    skipped_skus = 0
    skipped_images = 0
    truncated_images = 0
    for item in validated_catalog:
        sku = item.get("sku")
        if sku in ignore_set:
            skipped_skus += 1
            logging.info(f"Skipping SKU in SKUS_TO_IGNORE: {sku}")
            skipped_images += len(item.get("images", []))
            continue
        images = item.get("images", [])
        if len(images) > MAX_IMAGES_PER_SKU:
            truncated_images += len(images) - MAX_IMAGES_PER_SKU
            images = images[:MAX_IMAGES_PER_SKU]
        filtered_catalog.append({**item, "images": images})

    total_images = sum(len(item.get("images", [])) for item in filtered_catalog)

    if skipped_skus:
        print(f"ℹ️  Skipped {skipped_skus} SKUs in SKUS_TO_IGNORE")
        print(f"ℹ️  Skipped {skipped_images} images for SKUs in SKUS_TO_IGNORE")
    if truncated_images:
        print(
            f"ℹ️  Truncated {truncated_images} images beyond {MAX_IMAGES_PER_SKU} per SKU"
        )

    print(
        f"📊 Processing {len(filtered_catalog)} products with {total_images} total images..."
    )
    print(f"⚙️  Using concurrency: {concurrency}, timeout: {args.timeout}s")

    try:
        if not filtered_catalog:
            print("ℹ️  No products to upload after applying SKUS_TO_IGNORE.")
            return 0

        results = import_catalog(filtered_catalog, concurrency)
        success_count = len(results)
        failed_count = total_images - success_count

        print(f"✅ Upload completed!")
        print(f"   Successful: {success_count}")
        if failed_count > 0:
            print(f"   Failed: {failed_count}")
            print(f"   Check 'upload_errors.log' for detailed error information.")

        return 0 if failed_count == 0 else 1

    except KeyboardInterrupt:
        print("\n⚠️  Upload interrupted by user.")
        return 1
    except Exception as e:
        logging.error(f"Fatal error during upload: {e}")
        print(f"❌ Fatal error: {e}")
        return 1


SKUS_TO_IGNORE = []

if __name__ == "__main__":
    exit_code = main()
    exit(exit_code)
