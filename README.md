# Web Scraper Module

This directory contains web scraping functionality for JOI middleware.

## Structure

```
scraper/
├── __init__.py          # Package initialization
├── config.py            # Configuration settings
├── image_scraper.py     # Image scraping functionality
├── utils.py             # Utility functions
├── models/              # Data models (if needed)
└── output/              # Output directory for scraped data
```

## Usage

```python
from scraper.image_scraper import ImageScraper

scraper = ImageScraper()
scraper.run()
```
