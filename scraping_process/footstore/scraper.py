from __future__ import annotations
import re, time
from typing import List, Tuple, Iterable
from bs4 import BeautifulSoup
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.common.by import By

MIN_DIM = 100
IMG_EXT_RE = re.compile(r"\.(jpe?g|png|webp)(\?|$)", re.I)
PREFERRED_HOST_SNIPPET = "media.foot-store.com/"

MAX_IMAGES = 9

GALLERY_SELECTORS = (
    "div.product.media",
    "div.product-view",
    "div.product-img-box",
    "div.product-gallery",
    "section.product-media",
    "div.gallery-placeholder",
)


def _extract_meta(soup: BeautifulSoup, prop: str) -> str:
    tag = soup.find("meta", attrs={"property": prop})
    return tag.get("content", "").strip() if tag else ""


def _looks_like_product(src: str) -> bool:
    return (
        bool(src)
        and not src.startswith("data:")
        and PREFERRED_HOST_SNIPPET in src
        and IMG_EXT_RE.search(src)
    )


def _iter_images(driver: WebDriver, container):
    return (container or driver).find_elements(By.TAG_NAME, "img")


def _collect_sources(driver: WebDriver, elements: Iterable) -> List[str]:
    seen, out = set(), []
    for el in elements:
        try:
            src = (
                el.get_attribute("currentSrc")
                or el.get_attribute("src")
                or el.get_attribute("data-src")
                or el.get_attribute("data-original")
                or ""
            )
            if not _looks_like_product(src):
                continue
            w = el.get_property("naturalWidth") or 0
            h = el.get_property("naturalHeight") or 0
            if w < MIN_DIM or h < MIN_DIM:
                try:
                    driver.execute_script(
                        "arguments[0].scrollIntoView({block:'center'});", el
                    )
                except Exception:
                    pass
                WebDriverWait(driver, 10).until(
		    EC.presence_of_element_located((By.CSS_SELECTOR, 'img[data-lazy]'))
		)

                w = el.get_property("naturalWidth") or 0
                h = el.get_property("naturalHeight") or 0
            if w >= MIN_DIM and h >= MIN_DIM and src not in seen:
                seen.add(src)
                out.append(src)
        except Exception:
            continue
    return out


def _locate_gallery(driver: WebDriver):
    for sel in GALLERY_SELECTORS:
        try:
            el = driver.find_element(By.CSS_SELECTOR, sel)
            if el and el.is_displayed():
                return el
        except Exception:
            pass
    return None


def scrape_footstore_product(
    driver: WebDriver, sku: str, url: str
) -> Tuple[str, List[str], str]:
    driver.get(url)
    try:
        driver.execute_script("return document.readyState")
    except Exception:
        pass
    soup = BeautifulSoup(driver.page_source, "html.parser")
    name = _extract_meta(soup, "og:title") or (
        soup.h1.get_text(strip=True) if soup.h1 else ""
    )
    container = _locate_gallery(driver)
    images = _collect_sources(driver, _iter_images(driver, container))
    if not images:
        images = _collect_sources(driver, _iter_images(driver, None))

    if len(images) > MAX_IMAGES:
        images = images[:MAX_IMAGES]

    return (name, images, "OK" if images else "No images")
