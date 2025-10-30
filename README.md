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
2. Launch the FastAPI application (either command works):
   ```bash
   python web_app.py --host 0.0.0.0 --port 8000
   # or
   uvicorn web_app:app --host 0.0.0.0 --port 8000
   ```
3. Open `http://localhost:8000` in your browser.
4. Paste SKU codes into the textarea (one per line or comma separated) and press **Run pipeline**.
5. Watch live terminal output stream into the page. When the process finishes, the resulting
   `files/images.json` content is displayed below the terminal.

The interface writes the submitted SKUs to `files/skus.txt` and then executes `run_all.py`.
Pipeline logs, status updates, and JSON results are streamed to the browser via Server-Sent Events.

### Tips for a DigitalOcean Droplet

- Ensure the droplet firewall allows inbound traffic on the chosen port (for example `8000`).
- Run the server inside `screen` or `tmux` so it keeps running after you disconnect:
  ```bash
  screen -S scraper
  python web_app.py --host 0.0.0.0 --port 8000
  ```
  Detach with `Ctrl-A`, `D` and re-attach later with `screen -r scraper`.
- Use environment variables `SCRAPER_TUI_HOST` and `SCRAPER_TUI_PORT` if you prefer configuration
  via the shell:
  ```bash
  export SCRAPER_TUI_HOST=0.0.0.0
  export SCRAPER_TUI_PORT=8080
  python web_app.py
  ```

## Running the pipeline manually

If you prefer the command line, you can still execute the pipeline directly:

```bash
python run_all.py
```

The command expects `files/skus.txt` to contain the target SKUs.
