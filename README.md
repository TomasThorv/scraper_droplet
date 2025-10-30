# Web Scraper Module

This repository contains the scraping pipeline that powers JOI middleware.

## Structure

```
scraper_droplet/
├── run_all.py               # Orchestrates the full scraping workflow
├── scraping_process/        # Individual pipeline stages
├── files/                   # Runtime working directory (SKU input & results)
├── web_app.py               # Minimal browser-based TUI for running the pipeline
└── requirements.txt         # Python dependencies
```

## Running the browser TUI

1. Install the dependencies:
   ```bash
   pip install -r requirements.txt
   ```
2. Launch the FastAPI application:
   ```bash
   uvicorn web_app:app --host 0.0.0.0 --port 8000
   ```
3. Open `http://localhost:8000` in your browser.
4. Paste SKU codes into the textarea (one per line or comma separated) and press **Run pipeline**.
5. Watch live terminal output stream into the page. When the process finishes, the resulting
   `files/images.json` content is displayed below the terminal.

The interface writes the submitted SKUs to `files/skus.txt` and then executes `run_all.py`.
Pipeline logs, status updates, and JSON results are streamed to the browser via Server-Sent Events.

## Running the pipeline manually

If you prefer the command line, you can still execute the pipeline directly:

```bash
python run_all.py
```

The command expects `files/skus.txt` to contain the target SKUs.
