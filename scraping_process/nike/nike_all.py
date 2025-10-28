"""Nike scraping helpers used by the image scraping pipeline.

The original module acted as a standalone script that wrote CSV files.  For the
pipeline we expose a light-weight :class:`NikeScraper` context manager with a
``scrape`` method returning the product title together with the list of hero
images discovered on the page.  The low-level Playwright helpers are retained
so we still benefit from the battle-tested DOM traversal logic.
"""

from __future__ import annotations

from typing import List, Tuple, Optional, Set

from playwright.sync_api import (
    sync_playwright,
    TimeoutError as PlaywrightTimeoutError,
    Page,
)


def extract_product_info(page: Page) -> dict:
    product_title = page.query_selector('[data-testid="product_title"]')
    style_color = page.query_selector('[data-testid="product-description-style-color"]')
    color_description = page.query_selector(
        '[data-testid="product-description-color-description"]'
    )

    title_text = product_title.inner_text() if product_title else ""
    style_color_text = style_color.inner_text() if style_color else ""
    color_description_text = color_description.inner_text() if color_description else ""

    return {
        "title": title_text,
        "style_color": style_color_text,
        "color_description": color_description_text,
    }


def handle_geo_modal(page: Page) -> None:
    try:
        page.keyboard.press("Escape")
        page.locator("#modal-root").wait_for(state="hidden", timeout=5_000)
    except (PlaywrightTimeoutError, TimeoutError):
        pass


def _parse_src_or_srcset(el) -> Optional[str]:
    """Return the best image URL from an <img> element, preferring largest srcset."""
    src = el.get_attribute("src") or ""
    srcset = el.get_attribute("srcset") or ""

    # If srcset present, pick the last (usually largest) candidate
    if srcset:
        try:
            candidates = [c.strip() for c in srcset.split(",") if c.strip()]
            if candidates:
                last = candidates[-1].split()[0]
                return last
        except Exception:
            pass
    return src or None


def _collect_current_hero_images(page: Page, existing: List[str]) -> None:
    """Collect currently displayed hero images using robust selectors and srcset parsing."""
    # Try a few hero image containers/selectors seen across Nike PDPs
    hero_locators = [
        '[data-testid="HeroImgContainer"] img',
        '[data-testid*="Hero"] img',
        '[data-testid*="hero"] img',
        '[data-testid="pdp-image"] img',
        'figure img[alt][src]'
    ]

    seen: Set[str] = set(existing)
    for css in hero_locators:
        hero = page.locator(css)
        try:
            if hero.count() > 0:
                hero.first.wait_for(state="visible", timeout=2_000)
        except (PlaywrightTimeoutError, TimeoutError):
            pass

        for handle in hero.element_handles():
            best = _parse_src_or_srcset(handle)
            if best and best not in seen:
                existing.append(best)
                seen.add(best)


def extract_hero_images(page: Page) -> List[str]:
    """Collect as many PDP images as possible.

    Strategy:
    1) Try thumbnails via robust selectors; click (not hover) to force hero change.
    2) After iterating visible thumbs, attempt to click a "next" chevron if present.
    3) Always collect currently displayed hero after each interaction.
    4) Fallback: scrape all gallery-like <img> URLs that look like Nike CDN images.
    """
    # A union of common thumbnail selectors observed on Nike PDPs over time
    thumb_selectors = [
        '[data-testid^="Thumbnail-"]',
        '[data-testid*="thumbnail"]',
        'button[aria-label*="image" i] img',
        '[data-testid^="PDP-ProductImageThumbnail"]',
        'li[role="presentation"] button img'
    ]

    thumbnails = None
    for sel in thumb_selectors:
        loc = page.locator(sel)
        try:
            if loc.count() > 0:
                loc.first.wait_for(state="visible", timeout=3_000)
                thumbnails = loc
                break
        except (PlaywrightTimeoutError, TimeoutError):
            continue

    handle_geo_modal(page)

    hero_images: List[str] = []

    if thumbnails and thumbnails.count() > 0:
        # Scroll thumbnails into view to ensure they load
        try:
            thumbnails.first.scroll_into_view_if_needed()
        except Exception:
            pass

        count = thumbnails.count()
        for i in range(count):
            try:
                el = thumbnails.nth(i)
                el.scroll_into_view_if_needed()
                # Prefer click over hover for reliability
                try:
                    # If it's an <img>, click its parent button if present
                    handle = el.element_handle()
                    tag = handle.evaluate("el => el.tagName.toLowerCase()") if handle else "img"
                    if tag == "img":
                        parent_button = el.locator("xpath=ancestor::button[1]")
                        if parent_button.count() > 0:
                            parent_button.first.click(timeout=2_000)
                        else:
                            el.click(timeout=2_000)
                    else:
                        el.click(timeout=2_000)
                except Exception:
                    # Fallback hover if click not possible
                    try:
                        el.hover(force=True)
                    except Exception:
                        pass

                page.wait_for_timeout(250)
                _collect_current_hero_images(page, hero_images)
                page.wait_for_timeout(150)
            except Exception:
                continue

        # Try clicking a next/chevron button a few times to reveal hidden thumbs
        chevron_selectors = [
            '[data-testid="chevronRight"]',
            'button[aria-label*="Next" i]',
            'button[title*="Next" i]'
        ]
        for cs in chevron_selectors:
            try:
                next_btn = page.locator(cs)
                if next_btn.count() > 0:
                    for _ in range(6):  # arbitrary cap
                        try:
                            next_btn.first.click(timeout=1_000)
                            page.wait_for_timeout(200)
                            _collect_current_hero_images(page, hero_images)
                        except Exception:
                            break
                    break
            except Exception:
                pass
    else:
        # No thumbnails found – at least collect whatever hero is present
        _collect_current_hero_images(page, hero_images)

    # Fallback: collect gallery-like images from the page (Nike CDN)
    try:
        all_imgs = page.query_selector_all("img[src], img[srcset]")
        for img in all_imgs:
            best = _parse_src_or_srcset(img)
            if not best:
                continue
            # Heuristic filter for Nike product CDN assets
            if "static.nike.com" in best and "/images/" in best:
                if best not in hero_images:
                    hero_images.append(best)
    except Exception:
        pass

    return hero_images


class NikeScraper:
    """Context manager that keeps a single Playwright browser alive."""

    def __init__(self, headless: bool = True):
        self.headless = headless
        self._playwright = None
        self._browser = None
        self._page: Optional[Page] = None

    def __enter__(self) -> "NikeScraper":
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(headless=self.headless,
            args=["--disable-blink-features=AutomationControlled",
                  "--disable-dev-shm-usage",
                  "--disable-gpu",
                  "--no-sandbox",
                  "--window-size=1920,1080"])
        self._page = self._browser.new_page()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._browser:
            self._browser.close()
        if self._playwright:
            self._playwright.stop()
        self._browser = None
        self._playwright = None
        self._page = None

    def scrape(self, url: str) -> Tuple[str, List[str]]:
        if not self._page:
            raise RuntimeError("NikeScraper context has not been entered")

        page = self._page
        page.goto(url, wait_until="domcontentloaded")
        handle_geo_modal(page)
        page.wait_for_timeout(2000)
        product_info = extract_product_info(page)
        hero_images = extract_hero_images(page)
        return product_info.get("title", ""), hero_images


def scrape_once(url: str, headless: bool = True) -> Tuple[str, List[str]]:
    """Convenience wrapper to scrape a single Nike URL."""

    with NikeScraper(headless=headless) as scraper:
        return scraper.scrape(url)
