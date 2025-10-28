"""Puma product scraper – robust against video thumbs & dynamic galleries.

Returns: (name, [image_urls], status)

Order of strategies (stop at first success):
1) Parse page source for Puma gallery URLs: .../global/<base>/<variant>/svXX/...
2) Strict, gallery-scoped DOM scrape (skips video nodes)
3) og:image sv01..sv09 family expansion
4) Sanity.io JSON collector (for new PDPs)

Always hero first, capped at 9.
"""

from __future__ import annotations
import re, time, json
from typing import Iterable, List, Tuple
from bs4 import BeautifulSoup
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException

# ---------- config ----------
MAX_IMAGES = 4
MIN_DIM = 100

# Puma gallery URLs look like:
# https://images.puma.com/.../global/107916/03/sv01/.../image.jpg
PUMA_IMAGE_RE = re.compile(
    r'https?://[^"\']+/global/\d{5,6}/\d{2}/sv(?P<sv>\d{2})/[^"\']+\.(?:jpe?g|png|webp|avif)',
    re.I,
)

IMG_EXT_RE = re.compile(r"\.(?:jpe?g|png|webp|avif)(?:\?|$)", re.I)
SV_FAMILY_RE = re.compile(r"/sv(\d{2})(/|$)", re.I)

SKIP_HOSTS = (
    "facebook.com",
    "google.",
    "gstatic.com",
    "twitter.com",
    "doubleclick.net",
    "cookielaw.org",
)
SKIP_PATHS = ("/video/upload/", "/videos/", "/video/")  # avoid video thumbs

GALLERY_SELECTORS = [
    '[data-test-id="product-image-gallery-section"]',
    '[data-testid="product-image-gallery-section"]',
    "section.product-image-gallery",
    "section[data-testid*='image-gallery']",
    "div.product-gallery",
]

THUMB_SELECTORS = (
    "[data-testid*='thumbnail']",
    ".thumbnails img, .thumbnails a, .thumbnail, [class*='thumb'] img, [class*='thumb'] a",
)

OVERLAY_CLOSE_SELECTORS = (
    "button#onetrust-accept-btn-handler",  # cookies
    "button[aria-label='Close']",
    "button[data-testid='modal-close']",
    "button[data-testid='modal-close-button']",
    "button[class*='close']",
    "button[class*='cookie']",
    "button[class*='region']",
)

# ---------- tiny helpers ----------


def _extract_meta(soup: BeautifulSoup, prop: str) -> str:
    tag = soup.find("meta", attrs={"property": prop}) or soup.find(
        "meta", attrs={"name": prop}
    )
    return tag.get("content", "").strip() if tag and tag.get("content") else ""


def _looks_like_product(src: str) -> bool:
    if not src or src.startswith("data:"):
        return False
    if any(b in src for b in SKIP_HOSTS):
        return False
    if any(p in src for p in SKIP_PATHS):
        return False
    return IMG_EXT_RE.search(src) is not None


def _wait_ready(driver: WebDriver, timeout: float = 8.0):
    try:
        WebDriverWait(driver, timeout).until(
            lambda d: d.execute_script("return document.readyState")
            in ("interactive", "complete")
        )
    except TimeoutException:
        pass


def _dismiss_overlays(driver: WebDriver):
    for sel in OVERLAY_CLOSE_SELECTORS:
        try:
            el = driver.find_element(By.CSS_SELECTOR, sel)
            if el.is_displayed():
                el.click()
                time.sleep(0.3)
        except Exception:
            continue
    try:
        driver.switch_to.active_element.send_keys(Keys.ESCAPE)
        time.sleep(0.1)
    except Exception:
        pass


def _locate_gallery(driver: WebDriver, timeout: float = 6.0):
    for sel in GALLERY_SELECTORS:
        try:
            el = WebDriverWait(driver, timeout).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, sel))
            )
            if el and el.is_displayed():
                return el
        except TimeoutException:
            continue
    return None


def _has_video_ancestor(el) -> bool:
    """Skip any node that lives inside a 'video' labeled area."""
    try:
        xpath = (
            ".//ancestor::*["
            "contains(translate(@class,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'video') or "
            "contains(translate(@data-testid,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'video') or "
            "contains(translate(@aria-label,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'video')]"
        )
        anc = el.find_elements(By.XPATH, xpath)
        return bool(anc)
    except Exception:
        return False


def _get_src_from_el(el) -> str:
    src = (
        el.get_attribute("currentSrc")
        or el.get_attribute("src")
        or el.get_attribute("data-zoom-image")
        or el.get_attribute("data-src")
        or el.get_attribute("data-original")
        or ""
    )
    if not src:
        srcset = el.get_attribute("srcset") or ""
        if srcset:
            last = srcset.split(",")[-1].strip()
            src = last.split(" ")[0]
    return src


def _ensure_hero_first(images: List[str], hero_hint: str | None) -> List[str]:
    if not hero_hint:
        return images
    try:
        images.remove(hero_hint)
    except ValueError:
        pass
    images.insert(0, hero_hint)
    return images


# ---------- collectors ----------


def _collect_from_page_source_sv(
    page_source: str, limit: int = MAX_IMAGES
) -> List[str]:
    """Extract Puma gallery svXX URLs directly from HTML/JSON."""
    hits = list(PUMA_IMAGE_RE.finditer(page_source))
    if not hits:
        return []
    # Sort by sv number (sv01..sv99), then dedupe preserving order.
    hits.sort(key=lambda m: int(m.group("sv")))
    seen, out = set(), []
    for m in hits:
        url = m.group(0)
        if _looks_like_product(url) and url not in seen:
            seen.add(url)
            out.append(url)
        if len(out) >= limit:
            break
    return out


def _click_thumbs(driver: WebDriver, container) -> List[str]:
    """Walk thumbnails to force-load stage image (gallery only)."""
    out, seen = [], set()
    # read current stage (try biggest <img> inside gallery)
    try:
        stage_img = container.find_element(By.CSS_SELECTOR, "img")
        s = _get_src_from_el(stage_img)
        if _looks_like_product(s):
            seen.add(s)
            out.append(s)
    except Exception:
        pass

    for sel in THUMB_SELECTORS:
        try:
            thumbs = container.find_elements(By.CSS_SELECTOR, sel)
            for t in thumbs:
                if _has_video_ancestor(t):  # skip video thumbs
                    continue
                try:
                    driver.execute_script(
                        "arguments[0].scrollIntoView({block:'center'});", t
                    )
                except Exception:
                    pass
                try:
                    t.click()
                except Exception:
                    try:
                        t.find_element(By.TAG_NAME, "img").click()
                    except Exception:
                        continue
                time.sleep(0.25)
                # read stage again
                try:
                    img = container.find_element(By.CSS_SELECTOR, "img")
                    src = _get_src_from_el(img)
                except Exception:
                    src = ""
                if _looks_like_product(src) and src not in seen:
                    seen.add(src)
                    out.append(src)
                if len(out) >= MAX_IMAGES:
                    return out
        except Exception:
            continue
    return out


def _collect_from_gallery_dom(driver: WebDriver, container) -> List[str]:
    """Collect from <picture><source>, CSS bg, and <img> inside gallery only; skip video areas."""
    urls, seen = [], set()

    # <picture><source>
    try:
        for s in container.find_elements(By.CSS_SELECTOR, "picture source[srcset]"):
            if _has_video_ancestor(s):
                continue
            srcset = s.get_attribute("srcset") or ""
            if srcset:
                last = srcset.split(",")[-1].strip().split(" ")[0]
                if _looks_like_product(last) and last not in seen:
                    seen.add(last)
                    urls.append(last)
                    if len(urls) >= MAX_IMAGES:
                        return urls
    except Exception:
        pass

    # CSS background-image
    try:
        for node in container.find_elements(By.CSS_SELECTOR, "[style]"):
            if _has_video_ancestor(node):
                continue
            try:
                bg = node.value_of_css_property("background-image") or ""
            except Exception:
                bg = ""
            if not bg or bg == "none":
                continue
            m = re.search(r'url\(["\']?(?P<u>[^"\')]+)["\']?\)', bg, re.I)
            if not m:
                continue
            u = m.group("u")
            if _looks_like_product(u) and u not in seen:
                seen.add(u)
                urls.append(u)
                if len(urls) >= MAX_IMAGES:
                    return urls
    except Exception:
        pass

    # <img>
    try:
        for img in container.find_elements(By.TAG_NAME, "img"):
            if _has_video_ancestor(img):
                continue
            src = _get_src_from_el(img)
            # ensure it's reasonably sized (avoid tracking pixels)
            if not _looks_like_product(src):
                continue
            w = img.get_property("naturalWidth") or 0
            h = img.get_property("naturalHeight") or 0
            if w < MIN_DIM or h < MIN_DIM:
                try:
                    driver.execute_script(
                        "arguments[0].scrollIntoView({block:'center'});", img
                    )
                except Exception:
                    pass
                time.sleep(0.2)
                w = img.get_property("naturalWidth") or 0
                h = img.get_property("naturalHeight") or 0
            if w >= MIN_DIM and h >= MIN_DIM and src not in seen:
                seen.add(src)
                urls.append(src)
                if len(urls) >= MAX_IMAGES:
                    return urls
    except Exception:
        pass

    return urls


def _collect_from_og_sv_family(soup: BeautifulSoup) -> List[str]:
    """Expand og:image sv01 → sv02..sv09 if present."""
    og = _extract_meta(soup, "og:image")
    if not og:
        return []
    m = SV_FAMILY_RE.search(og)
    if not m:
        return [og]
    base = og[: m.start()]
    tail = og[m.end() - 1 :]
    out = [og]
    for i in range(2, MAX_IMAGES + 1):
        out.append(f"{base}/sv{i:02d}{tail}")
    return out[:MAX_IMAGES]


def _collect_from_sanity(driver: WebDriver) -> List[str]:
    """Extract image URLs from Next.js/Sanity JSON if present."""
    try:
        blob = driver.execute_script(
            "return window.__NEXT_DATA__ ? JSON.stringify(window.__NEXT_DATA__) : null;"
        )
        if not blob:
            return []
        data = json.loads(blob)
        urls: List[str] = []

        def walk(o):
            if isinstance(o, dict):
                for v in o.values():
                    walk(v)
            elif isinstance(o, list):
                for v in o:
                    walk(v)
            elif isinstance(o, str) and ("cdn.sanity.io" in o or "/global/" in o):
                if _looks_like_product(o):
                    urls.append(o)

        walk(data)
        # prefer Puma /global/ svXX images if present
        puma_first = [u for u in urls if "/global/" in u]
        rest = [u for u in urls if u not in puma_first]
        out = []
        seen = set()
        for u in puma_first + rest:
            if u not in seen:
                seen.add(u)
                out.append(u)
            if len(out) >= MAX_IMAGES:
                break
        return out
    except Exception:
        return []


def _expand_sv_family(url: str, limit: int = MAX_IMAGES) -> list[str]:
    """Expand sv01 → sv02..sv09 for Puma gallery."""
    m = SV_FAMILY_RE.search(url)
    if not m:
        return [url]
    base = url[: m.start()]
    tail = url[m.end() - 1 :]
    out = [url]
    for i in range(2, limit + 1):
        out.append(f"{base}/sv{i:02d}{tail}")
    return out


# ---------- main API ----------


def scrape_puma_product(
    driver: WebDriver, sku: str, url: str
) -> Tuple[str, List[str], str]:
    driver.get(url)
    _wait_ready(driver)
    _dismiss_overlays(driver)

    # Product name
    soup = BeautifulSoup(driver.page_source, "html.parser")
    name = _extract_meta(soup, "og:title") or (
        soup.h1.get_text(strip=True) if soup.h1 else ""
    )

    # 1) Page-source svXX extraction (best for correctness & swatches)
    sv_urls = _collect_from_page_source_sv(driver.page_source, limit=MAX_IMAGES)
    if sv_urls:
        # If we only got sv01, expand family to sv09
        if len(sv_urls) == 1 and "sv01" in sv_urls[0]:
            sv_urls = _expand_sv_family(sv_urls[0], limit=MAX_IMAGES)

        hero = _extract_meta(soup, "og:image")
        images = _ensure_hero_first(sv_urls, hero)
        return name, images[:MAX_IMAGES], "OK (sv from source)"

    # 2) Strict gallery-only DOM collection (avoid video areas)
    container = _locate_gallery(driver)
    if container:
        # Click through thumbs to force stage images (gallery only, skip video)
        clicked = _click_thumbs(driver, container)
        gallery_urls = clicked + _collect_from_gallery_dom(driver, container)
        # Dedupe preserving order
        seen, images = set(), []
        for u in gallery_urls:
            if _looks_like_product(u) and u not in seen:
                seen.add(u)
                images.append(u)
            if len(images) >= MAX_IMAGES:
                break
        if images:
            hero = _extract_meta(soup, "og:image")
            images = _ensure_hero_first(images, hero)
            return name, images[:MAX_IMAGES], "OK (gallery DOM)"

    # 3) og:image sv-family
    og_sv = _collect_from_og_sv_family(soup)
    if og_sv:
        return name, og_sv[:MAX_IMAGES], "OK (meta sv family)"

    # 4) Sanity JSON
    if "cdn.sanity.io" in driver.page_source:
        sanity = _collect_from_sanity(driver)
        if sanity:
            return name, sanity[:MAX_IMAGES], "OK (sanity)"

    return name, [], "No images"
