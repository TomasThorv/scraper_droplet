"""
Web Scraper Package for JOI Middleware

This package provides web scraping functionality.
"""

__version__ = "1.0.0"
__author__ = "JOI Team"

# Import main classes for easy access
try:
    from .image_scraper import ImageScraper
    from .config import ScraperConfig
except ImportError:
    # Handle case where dependencies aren't installed yet
    pass
