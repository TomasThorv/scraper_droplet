# Web Scraper Module

This repository contains the scraping pipeline for product images

## Structure

```
scraper_droplet/
├── run_all.py               # Orchestrates the full scraping workflow
├── scraping_process/        # Individual pipeline stages
├── files/                   # Runtime working directory (SKU input & results)
├── web_app.py               # Minimal browser-based TUI for running the pipeline
└── requirements.txt         # Python dependencies
```

## Running scraper

1. ssh -i C:\Users\Tommi\.ssh\id_digitalocean root@206.189.22.92
spyrja Whey God fyrir pass


2. Install the dependencies:
   ```bash
   pip install -r requirements.txt
   ```
3. git pull fyrir dev osg svo run 3 commands fyrir redeployment

cd scraper 2x svo
source /root/scraper/scraper/venv/bin/activate

sudo systemctl restart scraper-web
sudo systemctl status scraper-web --no-pager
sudo journalctl -u scraper-web -f


Annars voða einfalt, nano eða vim f edits og tengt við tha repo svo öll git commands virka :)

3=======D
