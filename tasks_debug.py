from celery import shared_task
import logging

logger = logging.getLogger(__name__)


@shared_task(bind=True)
def test_task(self):
    """Simple test task to verify Celery is working"""
    logger.info("TEST TASK EXECUTED SUCCESSFULLY!")
    return {"status": "success", "message": "Test task completed"}


@shared_task(bind=True)
def debug_production_environment(self):
    """Debug task to test production environment dependencies"""
    import subprocess
    import sys
    import os
    from pathlib import Path

    debug_info = []

    def log(msg):
        debug_info.append(msg)
        logger.info(msg)

    log("=== PRODUCTION ENVIRONMENT DEBUG ===")
    log(f"Python executable: {sys.executable}")
    log(f"Current working directory: {os.getcwd()}")

    # Test basic subprocess
    try:
        result = subprocess.run(
            [sys.executable, "-c", "print('Hello from subprocess')"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        log(f"Basic subprocess test: SUCCESS (return code: {result.returncode})")
        log(f"Subprocess output: {result.stdout.strip()}")
    except Exception as e:
        log(f"Basic subprocess test: FAILED - {e}")

    # Test Chrome/Chromium availability
    browsers = ["google-chrome", "chromium", "chromium-browser", "chrome"]
    browser_found = False

    for browser in browsers:
        try:
            result = subprocess.run(
                ["which", browser], capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                log(f"Browser found: {browser} at {result.stdout.strip()}")
                browser_found = True
                break
        except:
            continue

    if not browser_found:
        log("ERROR: No browser (Chrome/Chromium) found!")
        log("This is likely why the scraper is failing.")

    # Check if scraper files exist
    scraper_dir = Path("./scraper")
    if scraper_dir.exists():
        log(f"Scraper directory exists: {scraper_dir.absolute()}")
        run_all = scraper_dir / "run_all.py"
        log(f"run_all.py exists: {run_all.exists()}")
    else:
        log("ERROR: Scraper directory not found!")

    log("=== END DEBUG INFO ===")

    return {"status": "success", "message": "Debug completed", "debug_info": debug_info}


@shared_task(bind=True)
def scrape_images_task(self, task_id: str, skus: list[str]):
    """
    Celery task to run the complete scraper workflow using run_all.py
    """
    import subprocess, sys, os, json
    from pathlib import Path
    import django
    from django.conf import settings

    # Setup Django
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "middleware.settings")
    django.setup()

    from scraper_api.models import ScraperTask

    logger.info(f"SCRAPER TASK STARTED - Task ID: {task_id}, SKUs: {skus}")

    try:
        task = ScraperTask.objects.get(pk=task_id)
        logger.info(f"Found task in database: {task.id}")
    except ScraperTask.DoesNotExist:
        logger.error(f"Task not found: {task_id}")
        return {"status": "error", "error": "Task not found"}

    def add_log(message):
        logs = list(task.logs or [])
        logs.append(message)
        task.logs = logs
        task.save(update_fields=["logs"])
        logger.info(f"Added log: {message}")

    def is_cancelled():
        task.refresh_from_db()
        return task.cancelled

    def set_progress(progress_val):
        task.progress = progress_val
        task.save(update_fields=["progress"])

    # Get the scraper directory
    scraper_dir = Path(__file__).parent

    try:
        # Step 0: Clear all previous files for completely fresh start
        add_log("Clearing all previous scraper files...")
        files_dir = scraper_dir / "files"

        # Clear all files in the files directory
        if files_dir.exists():
            for file_path in files_dir.glob("*"):
                if file_path.is_file():
                    file_path.unlink()

        add_log("Cleared all previous files")

        # Step 1: Create SKU file
        add_log("Creating SKU file...")
        files_dir.mkdir(exist_ok=True)
        temp_skus_file = scraper_dir / "files" / "skus.txt"
        with open(temp_skus_file, "w", encoding="utf-8") as f:
            for sku in skus:
                f.write(f"{sku}\n")

        add_log(f"Created SKU file with {len(skus)} SKUs")

        # Step 2: Run the complete scraper pipeline using run_all.py
        add_log("Starting complete scraper pipeline with run_all.py...")
        if is_cancelled():
            task.status = ScraperTask.Status.CANCELLED
            task.save(update_fields=["status"])
            return {"status": "cancelled"}

        # Set environment to use UTF-8 encoding
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"

        # Run run_all.py which handles the complete pipeline
        run_all_script = scraper_dir / "run_all.py"
        logger.info(f"About to run: {run_all_script}")

        result = subprocess.run(
            [sys.executable, str(run_all_script)],
            cwd=str(scraper_dir),
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            timeout=900,  # 15 minute timeout for complete pipeline
        )

        if result.returncode != 0:
            add_log(f"Scraper pipeline failed: {result.stderr}")
            add_log(f"Pipeline stdout: {result.stdout}")
            task.status = ScraperTask.Status.ERROR
            task.save(update_fields=["status"])
            logger.error(f"Pipeline failed with return code: {result.returncode}")
            return {"status": "error", "error": f"Pipeline failed: {result.stderr}"}

        add_log("Scraper pipeline completed successfully")
        add_log(f"Pipeline output: {result.stdout}")
        set_progress(len(skus))

        # Step 3: Read results from the JSON file created by run_all.py
        add_log("Reading results from images.json...")
        results_file = scraper_dir / "files" / "images.json"
        results_data = {}

        if results_file.exists():
            with open(results_file, "r", encoding="utf-8") as f:
                raw_results = json.load(f)

            # Convert to the expected format
            for item in raw_results:
                sku = item.get("sku", "")
                images = item.get("images", [])
                if sku:
                    results_data[sku] = images

            add_log(f"Found images for {len(results_data)} SKUs")
        else:
            add_log("No results file found after pipeline completion")

        # Update final results
        task.results = results_data
        task.progress = len(skus)
        task.status = ScraperTask.Status.FINISHED
        task.save(update_fields=["results", "progress", "status"])

        logger.info(f"Task completed successfully: {len(results_data)} results")
        return {
            "status": "finished",
            "results": results_data,
            "total_skus": len(skus),
            "found_results": len(results_data),
        }

    except subprocess.TimeoutExpired:
        add_log("Scraping timed out")
        task.status = ScraperTask.Status.ERROR
        task.save(update_fields=["status"])
        logger.error("Task timed out")
        return {"status": "error", "error": "Scraping timed out"}
    except Exception as e:
        add_log(f"Error: {str(e)}")
        task.status = ScraperTask.Status.ERROR
        task.save(update_fields=["status"])
        logger.error(f"Task failed with exception: {str(e)}")
        return {"status": "error", "error": str(e)}
