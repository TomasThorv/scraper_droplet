"""
Configuration settings for the web scraper
"""

import os
from typing import Dict, Any


class ScraperConfig:
    """Configuration class for web scraper settings"""

    # Base settings
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    OUTPUT_DIR = os.path.join(BASE_DIR, "output")

    # Scraping settings
    DEFAULT_TIMEOUT = 30
    DEFAULT_DELAY = 1  # Delay between requests in seconds
    MAX_RETRIES = 3

    # User agent for requests
    USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"

    # Image scraping specific settings
    SUPPORTED_IMAGE_FORMATS = [".jpg", ".jpeg", ".png", ".gif", ".webp"]
    MAX_IMAGE_SIZE_MB = 10

    # Create output directory if it doesn't exist
    @classmethod
    def ensure_output_dir(cls):
        """Ensure the output directory exists"""
        os.makedirs(cls.OUTPUT_DIR, exist_ok=True)
        return cls.OUTPUT_DIR

    @classmethod
    def get_headers(cls) -> Dict[str, str]:
        """Get default headers for requests"""
        return {
            "User-Agent": cls.USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
            "Accept-Encoding": "gzip, deflate",
            "Connection": "keep-alive",
        }
