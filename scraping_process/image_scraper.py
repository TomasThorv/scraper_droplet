#!/usr/bin/env python3
"""
image_scraper.py – **strict hero‑only** version
==============================================
Reads ``sku_links_limited.txt`` (tab‑separated ``SKU    URL``) and writes a
single row per SKU to ``product_images.txt`` containing **only the principal
product image** – no Facebook pixels, no tiny icons, no gallery thumbnails.

Heuristic
---------
1. **Use the page’s OpenGraph photo** (``<meta property="og:image">``) if it
   exists – that tag is almost always the hero shot.
2. If the tag is missing, inspect every ``<img>`` in DOM order and take the
   first element that passes *all* of these checks:

   • **looks_like_product(src)** – src URL must end with ``jpg/png/webp`` and
     must *not* be on any of the skip hosts (facebook.com, gstatic, etc.).

   • **Visible size** – natural width **and** height ≥ ``MIN_DIM`` (100 px by
     default).  This skips 1×1 tracking pixels and small logos even if they’re
     loaded early in the markup.

Nothing else changed: same CMD‑line behaviour, same output format.
"""

import os
import sys
import csv
import re
import time
import json
from contextlib import ExitStack
from urllib.parse import urlparse
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from bs4 import BeautifulSoup
import chromedriver_autoinstaller

from puma import scrape_puma_product
from nike import NikeScraper, scrape_once
from footstore import scrape_footstore_product


INPUT_FILE = "files/sku_links_limited.txt"
OUTPUT_FILE = "files/product_images.txt"
CHROMEDRIVER = "chromedriver.exe"  # adjust if not on PATH
HEADLESS = True  # flip to False to debug visually
NIKE_HEADLESS = True
MIN_DIM = 100  # px – minimum natural width & height
IMG_EXT_RE = re.compile(r"\.(jpe?g|png|webp)(\?|$)", re.I)
SKIP_HOSTS = (
    "facebook.com",
    "google.",
    "gstatic.com",
    "twitter.com",
    "doubleclick.net",
)


# ─── Selenium bootstrap ────────────────────────────────────────────────────


def init_driver():
    # Auto-install the correct ChromeDriver version
    chromedriver_path = chromedriver_autoinstaller.install()

    opts = Options()
    if HEADLESS:
        opts.add_argument("--headless=new")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--ignore-certificate-errors")

    # Use the auto-installed chromedriver path
    service = Service(chromedriver_path)
    return webdriver.Chrome(service=service, options=opts)


# ─── Helpers ───────────────────────────────────────────────────────────────


def extract_meta(soup, prop: str) -> str | None:
    tag = soup.find("meta", attrs={"property": prop})
    return tag["content"].strip() if tag and tag.get("content") else None


def looks_like_product(src: str) -> bool:
    """Filter out tracking pixels, logos, social sprites, etc."""
    if not src or src.startswith("data:"):
        return False
    if any(h in src for h in SKIP_HOSTS):
        return False
    return bool(IMG_EXT_RE.search(src))


SPECIAL_DOMAIN_KEYWORDS = {
    "puma.com": "puma",
    "nike.com": "nike",
    "foot-store.com": "footstore",
}


def classify_handler(url: str) -> str:
    host = urlparse(url).netloc.lower()
    for domain, label in SPECIAL_DOMAIN_KEYWORDS.items():
        if not host.endswith(domain):
            continue
        # For puma, only accept base domain or allowed regional subdomains (us., uk.)
        if domain == "puma.com":
            parts = host.split(".")
            if len(parts) == 2 and host == "puma.com":
                return label
            if parts[0] in ("us", "uk"):
                return label
            continue
        return label
    return "general"


def extract_name_and_hero(
    driver: webdriver.Chrome, url: str
) -> tuple[str, list[str], float]:
    """Return (name, [hero_image_url], fetch_time)."""
    start_time = time.time()

    driver.get(url)
    soup = BeautifulSoup(driver.page_source, "html.parser")

    # Name – og:title or first <h1>
    name = extract_meta(soup, "og:title") or (
        soup.h1.get_text(strip=True) if soup.h1 else ""
    )

    # 1️⃣  OpenGraph image wins
    hero = extract_meta(soup, "og:image")
    if hero:
        fetch_time = time.time() - start_time
        return name, [hero], fetch_time

    # 2️⃣  Fallback: first good‑looking <img>
    img_count = 0
    for img in driver.find_elements("tag name", "img"):
        src = img.get_attribute("src") or img.get_attribute("data-src") or ""
        if not looks_like_product(src):
            continue
        try:
            w = driver.execute_script("return arguments[0].naturalWidth", img)
            h = driver.execute_script("return arguments[0].naturalHeight", img)
        except Exception:
            w = h = 0
        if w >= MIN_DIM and h >= MIN_DIM:
            img_count += 1
            print(f"  📷 Found valid image #{img_count}: {src[:60]}...")
            fetch_time = time.time() - start_time
            return name, [src], fetch_time  # ✅ This should stop at first match!

    fetch_time = time.time() - start_time
    return name, [], fetch_time  # nothing decent found


# ─── Main driver ───────────────────────────────────────────────────────────


def main():
    if not os.path.exists(INPUT_FILE):
        sys.exit(f"❌ '{INPUT_FILE}' not found.")

    rows: list[tuple[str, str]] = []
    with open(INPUT_FILE, encoding="utf-8") as f:
        for ln in f:
            parts = ln.strip().split("\t", 1)
            if len(parts) == 2:
                rows.append(tuple(parts))
    if not rows:
        sys.exit(f"❌ '{INPUT_FILE}' is empty or malformed.")

    nike_json = []
    puma_json = []
    footstore_json = []

    with ExitStack() as stack:
        driver = init_driver()
        stack.callback(lambda: driver.quit())

        nike_scraper = None
        if any(classify_handler(url) == "nike" for _, url in rows):
            try:
                nike_scraper = stack.enter_context(NikeScraper(headless=NIKE_HEADLESS))
            except Exception as exc:
                print(f"⚠️  Could not initialise Nike scraper: {exc}")
                nike_scraper = None

        with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as out:
            wr = csv.writer(out, delimiter="\t")
            wr.writerow(["SKU", "Name", "Image_URL"])

            for sku, url in rows:
                handler = classify_handler(url)
                print(f"• {sku} → {url}", end=" … ")

                name = ""
                images: list[str] = []
                log_suffix = ""
                fetch_time = 0.0

                if handler == "puma":
                    try:
                        name, images, status = scrape_puma_product(driver, sku, url)
                        if images:
                            log_suffix = f"✓ Found {len(images)} image(s)"
                        else:
                            log_suffix = f"❌ {status}"
                    except Exception as exc:
                        log_suffix = f"⚠️  Failed: {exc}"
                        images = []
                    # Add to Puma JSON (do not write to product_images.txt)
                    puma_json.append({"sku": sku, "images": images})
                elif handler == "nike":
                    try:
                        if nike_scraper is not None:
                            name, images = nike_scraper.scrape(url)
                        else:
                            name, images = scrape_once(url, headless=NIKE_HEADLESS)
                        if images:
                            log_suffix = f"✓ Found {len(images)} image(s)"
                        else:
                            log_suffix = "❌ No images"
                    except Exception as exc:
                        log_suffix = f"⚠️  Failed: {exc}"
                        images = []
                    # Add to JSON
                    nike_json.append({"sku": sku, "images": images})
                elif handler == "footstore":
                    try:
                        name, images, status = scrape_footstore_product(
                            driver, sku, url
                        )
                        log_suffix = (
                            f"✓ {len(images)} image(s)" if images else f"❌ {status}"
                        )
                    except Exception as exc:
                        images, name = [], ""
                        log_suffix = f"⚠️ Failed: {exc}"
                    footstore_json.append({"sku": sku, "images": images})
                else:
                    try:
                        name, images, fetch_time = extract_name_and_hero(driver, url)
                        if images:
                            log_suffix = f"✓ Found image ({fetch_time:.2f}s)"
                        else:
                            log_suffix = f"❌ No image ({fetch_time:.2f}s)"
                    except Exception as exc:
                        log_suffix = f"⚠️  Failed: {exc}"
                        images = []

                print(log_suffix)

                name_to_write = name or ""
                # For Nike and Puma we collect JSON entries and skip writing to product_images.txt
                if handler in ("nike", "puma"):
                    # Nike already appended inside handler; Puma appended above. Skip writing.
                    if not images:
                        # still write an empty row to keep alignment for non-Nike/Puma entries? skip it
                        pass
                else:
                    if images:
                        for img in images:
                            wr.writerow([sku, name_to_write, img])
                    else:
                        wr.writerow([sku, name_to_write, ""])

    # Write Nike and Puma images to JSON with brand priority sorting
    images_json = nike_json + puma_json + footstore_json

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

    def get_brand_priority(entry):
        """Get brand priority for sorting. Lower number = higher priority."""
        if not entry.get("images"):
            return 999  # Empty entries go last

        # Get the first image URL to determine brand
        first_image = entry["images"][0]

        # Check which domain this image belongs to
        for priority, domain in enumerate(BRAND_PRIORITY):
            if domain.replace(".", r"\.") in first_image:
                return priority

        # If no match found, put at end
        return 999

    # Sort images by brand priority, then by SKU within each brand
    images_json.sort(key=lambda x: (get_brand_priority(x), x.get("sku", "")))

    with open("files/images.json", "w", encoding="utf-8") as jout:
        json.dump(images_json, jout, indent=2)

    print(f"✅ Done – wrote '{OUTPUT_FILE}'")


if __name__ == "__main__":
    main()
