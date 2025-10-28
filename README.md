# Scraper Control Panel & Pipeline

This project bundles the Nike scraping pipeline together with a lightweight
Flask UI that lets you:

- enter one or more product SKUs
- optionally launch the full scraping workflow (`run_all.py`)
- review the gallery images that were fetched for each SKU
- cherry-pick the assets that should be written back to `files/images.json`

## Prerequisites

1. **Python 3.10+** – matches the version used in production.
2. **Google Chrome** – Playwright launches a Chromium build that piggybacks on
   the system Chrome installation.
3. **System packages** required by Chrome/Playwright when running on a fresh
   Ubuntu box:

   ```bash
   sudo apt-get update
   sudo apt-get install -y libnss3 libatk-bridge2.0-0 libgtk-3-0 libdrm2 \
       libgbm1 libxkbcommon0 libasound2
   ```

## Installation

```bash
python -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
playwright install chromium
```

The final command downloads the Playwright-managed Chromium build the scraper
uses when collecting images.

## Running the web UI

```bash
source .venv/bin/activate
python interactive_ui.py
```

By default the app listens on <http://127.0.0.1:5000>. Set the `PORT`
environment variable if you need to bind to a different port, e.g.
`PORT=8080 python interactive_ui.py`.

### Workflow

1. Open the app in your browser.
2. Paste each SKU on its own line in the **SKUs to scrape** field.
3. Tick **Run scraping pipeline now** if you want the UI to call `python
   run_all.py` immediately after saving the SKUs. Leave it unchecked to trigger
   the scraper manually later.
4. Press **Save SKUs**. The app writes them to `files/skus.txt` and, when the
   checkbox is enabled, runs the scraper.
5. Review the collected images on the **Review & curate images** screen. Select
   the thumbnails you want to keep for each SKU or mark the SKU to be dropped.
6. Submit the form to create a timestamped backup of the previous
   `files/images.json` and overwrite it with your curated selection.

## Useful files

| Path | Purpose |
| ---- | ------- |
| `interactive_ui.py` | Flask app that exposes the SKU form and review flow. |
| `run_all.py` | Entry point that orchestrates the full scraping pipeline. |
| `files/skus.txt` | Automatically maintained list of SKUs from the UI. |
| `files/images.json` | Image catalogue updated after each review. |

## Troubleshooting

- **Only one image is captured per SKU** – the Nike scraper aggressively hovers
  and clicks each thumbnail in headless mode to force the hero gallery to cycle
  through every asset. If you still see a single image, re-run the scraper with
  `PLAYWRIGHT_HEADLESS=0` in your environment to launch a visible browser and
  confirm the page renders correctly on your network.
- **Chrome/Playwright fails to start** – double-check the prerequisites above
  and make sure the machine has enough shared memory. When running inside a
  container allocate at least 1 GB of `/dev/shm`.

