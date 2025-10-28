"""Nike scraping helpers used by the image scraping pipeline.

The original module acted as a standalone script that wrote CSV files.  For the
pipeline we expose a light-weight :class:`NikeScraper` context manager with a
``scrape`` method returning the product title together with the list of hero
images discovered on the page.  The low-level Playwright helpers are retained
so we still benefit from the battle-tested DOM traversal logic.
"""

from __future__ import annotations

from typing import List, Tuple, Optional

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


def _collect_current_hero_images(page: Page, existing: List[str]) -> None:
    hero = page.locator('[data-testid="HeroImgContainer"] img')
    try:
        hero.first.wait_for(state="visible", timeout=2_000)
    except (PlaywrightTimeoutError, TimeoutError):
        pass

    for handle in hero.element_handles():
        hero_src = handle.get_attribute("src")
        if hero_src and hero_src not in existing:
            existing.append(hero_src)


def extract_hero_images(page: Page) -> List[str]:
    thumbnails = page.locator('[data-testid^="Thumbnail-"]:not([data-testid*="Thumbnail-Img-"])')

    had_thumbnails = False
    try:
        if thumbnails.count() > 0:
            thumbnails.first.wait_for(state="visible", timeout=5_000)
            had_thumbnails = True
    except (PlaywrightTimeoutError, TimeoutError):
        had_thumbnails = False

    handle_geo_modal(page)

    hero_images: List[str] = []

    if had_thumbnails:
        for thumb in thumbnails.element_handles():
            try:
                thumb.hover(force=True)
            except Exception:
                continue
            page.wait_for_timeout(120)
            _collect_current_hero_images(page, hero_images)
            page.wait_for_timeout(300)

        try:
            if thumbnails.count() > 0:
                thumbnails.nth(0).hover(force=True)
        except Exception:
            pass
    else:
        _collect_current_hero_images(page, hero_images)

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
