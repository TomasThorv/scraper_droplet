#!/usr/bin/env python3
"""
sku_search_limited.py – Search each SKU in **skus.txt** on a restricted list of
domains and write at most **two** product-page links per SKU.

Changes in v0.4 (2025-07-03)
────────────────────────────
* New constant **`MAX_LINKS_PER_SKU = 2`**
* `find_links_for()` stops searching once that many links have been collected,
  so we no longer gather a long tail of results.
* Everything else – input file, output file, CLI flags – stays exactly the same.

Custom Puma logic (2025-09-30)
───────────────────────────────
* For *.puma.com results, we allow URLs that contain only the **base** part of
  the SKU (e.g., 107916). We then open the page and try to extract the **full
  style** (e.g., 107916-03). If found, we use that resolved SKU in the output.
"""

from __future__ import annotations


import pathlib, sys, time, re, random
from urllib.parse import urlparse, unquote
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException
import chromedriver_autoinstaller

# ─── Configuration ────────────────────────────────────────────────────────────
CHROMEDRIVER = "chromedriver"  # executable name or full path
TIMEOUT = 5  # seconds per search/page load (increased for bot detection)
SEARCH_URL = "https://duckduckgo.com/?q={query}&ia=web"
TITLE_SEL = "a[data-testid='result-title-a'], a.result__a"
HEADLESS = True  # flip to False to see browser
STRICT_SKU_MATCH = False  # allow flexible SKU matching with separator tolerance
MAX_LINKS_PER_SKU = 2  # stop after N links per SKU

ALLOWED_DOMAINS = [
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

OUTPUT_FILE = "files/sku_links_limited.txt"
# ──────────────────────────────────────────────────────────────────────────────


def normalize(domain: str) -> str:
    d = domain.lower()
    d2 = d[4:] if d.startswith("www.") else d
    parts = d2.split(".")
    if len(parts) > 2:
        return ".".join(parts[-2:])
    return d2


ALLOWED_NORMALIZED = {normalize(d) for d in ALLOWED_DOMAINS}

# Limit Puma to certain regional subdomains (add more if needed)
ALLOWED_SUBDOMAINS: dict[str, set[str]] = {
    "puma.com": {"us"},
}


def domain_allowed(href_netloc: str, allowed_domain: str) -> bool:
    """Return True if href_netloc is allowed for the configured allowed_domain.

    Behaviour:
    - If allowed_domain has no entry in ALLOWED_SUBDOMAINS, accept any subdomain or the base.
    - If it has entries, accept only those explicit regional subdomains (e.g., us.puma.com, uk.puma.com).
    """
    host = href_netloc.lower()
    if not host.endswith(allowed_domain):
        return False
    subs = ALLOWED_SUBDOMAINS.get(allowed_domain)
    if not subs:
        return True
    parts = host.split(".")
    if len(parts) >= 3 and parts[0] in subs:
        return True
    return False


# ─── Selenium bootstrap ───────────────────────────────────────────────────────
chromedriver_path = chromedriver_autoinstaller.install()
opts = webdriver.ChromeOptions()
opts.add_argument("--headless=new")
opts.add_argument("--no-sandbox")
opts.add_argument("--disable-dev-shm-usage")
opts.add_argument("--disable-gpu")
opts.add_argument(f"--user-data-dir=/tmp/chrome-profile-{random.randint(1000,9999)}")
opts.add_argument("--remote-debugging-port=9222")
# Anti-detection options
opts.add_experimental_option("excludeSwitches", ["enable-automation"])
opts.add_experimental_option("useAutomationExtension", False)
opts.add_argument("--disable-blink-features=AutomationControlled")
opts.add_argument(
    "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.7339.128 Safari/537.36"
)
opts.add_argument("--disable-dev-shm-usage")
opts.add_argument("--disable-extensions")
opts.add_argument("--no-first-run")
opts.add_argument("--disable-default-apps")

try:
    service = Service(chromedriver_path)
    driver = webdriver.Chrome(service=service, options=opts)
    driver.execute_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    )
except Exception as e:
    sys.exit(f"❌ Could not start Chrome: {e}")

wait = WebDriverWait(driver, TIMEOUT)

# ─── Search helpers ───────────────────────────────────────────────────────────

SEPARATOR_RE = re.compile(r"[-_\s]+")


def split_sku_parts(sku: str) -> tuple[str, str | None]:
    # e.g. "107916 03" → ("107916", "03")
    parts = SEPARATOR_RE.split(sku.strip())
    base = parts[0].lower() if parts else ""
    variant = parts[1].lower() if len(parts) > 1 else None
    return base, variant


def normalise_sku(value: str) -> str:
    """Return a lowercase SKU string with separators removed."""
    return SEPARATOR_RE.sub("", value.lower())


def sku_in_href(href: str, sku: str) -> bool:
    """True if *full sku* appears in *href* allowing -, _ or space variations."""
    if STRICT_SKU_MATCH:
        sku_lower = sku.lower()
        sku_regex = re.compile(rf"\b{re.escape(sku_lower)}\b")
        return bool(sku_regex.search(href.lower()))
    cleaned_href = normalise_sku(unquote(href))
    cleaned_sku = normalise_sku(sku)
    if cleaned_sku in cleaned_href:
        return True
    # e.g., 403651?swatch=01 (full sku implied nearby)
    if re.search(re.escape(cleaned_sku) + r"\D{0,3}\d{1,3}", cleaned_href):
        return True
    return False


def extract_full_puma_style(text: str, base: str) -> str | None:
    """
    From page text, extract a full style like '107916-03' given base '107916'.
    Accepts separators '-', '_' or space and 2–3 digit variants.
    Tries to prefer 2-digit variant if multiple are present.
    """
    t = text.lower()
    base_re = re.escape(base.lower())
    # Pattern: base + optional spaces + sep + 2-3 digits
    pat = re.compile(rf"\b{base_re}\s*[-_\s]\s*(\d{{2,3}})\b")
    matches = pat.findall(t)
    if not matches:
        # also consider concatenated e.g. '10791603' (less common on Puma, but cheap to try)
        pat2 = re.compile(rf"\b{base_re}(\d{{2,3}})\b")
        matches = pat2.findall(t)
    if not matches:
        return None
    # prefer 2-digit variants (most Puma colorways) else first
    two_digit = [m for m in matches if len(m) == 2]
    variant = two_digit[0] if two_digit else matches[0]
    return f"{base}-{variant}"


def resolve_full_puma_sku_from_page(url: str, base: str) -> str | None:
    """
    Navigate to *url*, read visible text, and try to resolve the full style SKU.
    Returns e.g. '107916-03' or None if not found.
    """
    try:
        driver.get(url)
        try:
            text = (
                driver.execute_script(
                    "return document.body ? document.body.innerText : ''"
                )
                or ""
            )
        except Exception:
            text = ""
        return extract_full_puma_style(text, base)
    except Exception:
        return None


def find_links_for(sku: str) -> tuple[list[str], list[tuple[str, float]], str]:
    """
    Return up to *MAX_LINKS_PER_SKU* product links for *sku* and timing info,
    along with a possibly *resolved_sku* (for Puma pages where we only had the base in URL).
    """
    collected: dict[str, str] = {}
    timings: list[tuple[str, float]] = []
    resolved_sku: str = sku  # default to the input; may change for Puma

    for domain in ALLOWED_DOMAINS:
        if len(collected) >= MAX_LINKS_PER_SKU:
            break

        time.sleep(random.uniform(2, 5))  # human-ish delay

        domain_start = time.time()
        query = f"site:{domain} {sku}"
        driver.get(SEARCH_URL.format(query=query))

        page_source = driver.page_source.lower()
        if "verify you are not a robot" in page_source or "captcha" in page_source:
            print(f"⚠️ Bot detection detected on domain {domain}, skipping...")
            continue

        try:
            wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, TITLE_SEL)))
        except TimeoutException:
            continue

        # after wait.until(...) succeeds, REPLACE the anchor-iteration block with this:
        try:
            hrefs = (
                driver.execute_script(
                    """
                const sel = arguments[0];
                return Array.from(document.querySelectorAll(sel))
                    .map(a => a.href || '')
                    .filter(h => h && h.startsWith('http'));
                """,
                    TITLE_SEL,
                )
                or []
            )
        except Exception:
            hrefs = []

        found_link = False
        for href in hrefs:
            if len(collected) >= MAX_LINKS_PER_SKU:
                break

            href_netloc = urlparse(href).netloc
            if not domain_allowed(href_netloc, domain):
                continue

            # Regular rule: full-SKU in URL
            if sku_in_href(href, sku):
                collected[normalize(domain)] = href
                found_link = True
                break

            # Puma-only: allow base SKU in URL → open once → extract full style
            if domain.endswith("puma.com"):
                base, _ = split_sku_parts(sku)
                if base and base in normalise_sku(unquote(href)):
                    full = resolve_full_puma_sku_from_page(href, base)
                    if not full:
                        continue
                    resolved_sku = full
                    collected[normalize(domain)] = href
                    found_link = True
                    break
                else:
                    continue

            # Non-Puma stay strict
            continue

        if found_link:
            domain_time = time.time() - domain_start
            timings.append((domain, domain_time))

        time.sleep(0.2)  # politeness delay

    return list(collected.values()), timings, resolved_sku


# ─── Driver code ──────────────────────────────────────────────────────────────


def main() -> None:
    skus = [
        s.strip()
        for s in pathlib.Path("files/skus.txt").read_text().splitlines()
        if s.strip()
    ]
    total = len(skus)

    print(f"🔍 Starting search for {total} SKUs across {len(ALLOWED_DOMAINS)} domains")
    print(
        f"📋 STRICT_SKU_MATCH={STRICT_SKU_MATCH}, MAX_LINKS_PER_SKU={MAX_LINKS_PER_SKU}"
    )
    print("─" * 60)

    with open(OUTPUT_FILE, "w", encoding="utf-8") as out:
        for idx, sku in enumerate(skus, 1):
            print(f"[{idx}/{total}] {sku}", end=" … ")
            links, timings, resolved = find_links_for(sku)
            if links:
                for link in links:
                    # Write using the resolved SKU (if Puma page revealed a style)
                    out.write(f"{resolved}\t{link}\n")
                timing_info = ""
                if timings:
                    timing_strs = [
                        f"{domain}: {time_taken:.2f}s" for domain, time_taken in timings
                    ]
                    timing_info = f" ({', '.join(timing_strs)})"
                print(f"✓ {len(links)} link(s){timing_info}")
            else:
                out.write(f"{sku}\tNOT_FOUND\n")
                print("❌ No results")

    print("─" * 60)
    print(f"✅ Done. Results → {OUTPUT_FILE}")
    driver.quit()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        driver.quit()
        print("\nInterrupted by user.")
