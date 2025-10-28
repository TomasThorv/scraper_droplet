from celery import shared_task
import subprocess, sys, os, json
from pathlib import Path
import django

# Setup Django
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "middleware.settings")
django.setup()

from scraper_api.models import ScraperTask
from scraper_api.task_cache import load_task_snapshot, store_task_data


@shared_task(bind=True)
def scrape_images_task(self, task_id: str, skus: list[str]):
    """
    Celery task to run the complete scraper workflow using run_all.py
    """
    try:
        task_obj = ScraperTask.objects.get(pk=task_id)
    except ScraperTask.DoesNotExist:
        task_obj = None

    logs = list(task_obj.logs or []) if task_obj and task_obj.logs else []
    if not logs:
        logs.append("Task queued for processing...")
    results: dict[str, list[str]] = (
        dict(task_obj.results or {}) if task_obj and task_obj.results else {}
    )
    progress = task_obj.progress if task_obj else 0
    total = task_obj.total or len(skus) if task_obj else len(skus)
    cancelled = bool(task_obj.cancelled) if task_obj else False
    status = ScraperTask.Status.RUNNING

    def persist_state(*, status_override: str | None = None) -> None:
        nonlocal status, task_obj, cancelled

        if status_override is not None:
            status = status_override

        payload = {
            "progress": progress,
            "total": total,
            "logs": logs,
            "status": status,
            "cancelled": cancelled,
        }
        if results is not None:
            payload["results"] = results

        store_task_data(task_id, payload)

        if task_obj:
            task_obj.status = status
            task_obj.progress = progress
            task_obj.total = total
            task_obj.logs = logs
            task_obj.results = results
            task_obj.cancelled = cancelled
            try:
                task_obj.save(
                    update_fields=[
                        "status",
                        "progress",
                        "total",
                        "logs",
                        "results",
                        "cancelled",
                    ]
                )
            except Exception:
                task_obj = None

    def add_log(message: str) -> None:
        logs.append(message)
        persist_state()

    def is_cancelled() -> bool:
        nonlocal cancelled

        if task_obj:
            task_obj.refresh_from_db(fields=["cancelled"])
            cancelled = task_obj.cancelled
        else:
            snapshot = load_task_snapshot(task_id)
            if snapshot is not None:
                cancelled = bool(snapshot.get("cancelled"))

        if cancelled:
            if not logs or logs[-1] != "Task cancelled by user.":
                logs.append("Task cancelled by user.")
            persist_state(status_override=ScraperTask.Status.CANCELLED)
            return True
        return False

    def set_progress(progress_val: int) -> None:
        nonlocal progress
        progress = progress_val
        persist_state()

    persist_state()
    add_log("Worker started processing task...")

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

        # Debug: Check file was created and show contents
        if temp_skus_file.exists():
            with open(temp_skus_file, "r", encoding="utf-8") as f:
                content = f.read()
            add_log(f"SKU file contents: {repr(content)}")
        else:
            add_log("ERROR: SKU file was not created!")

        # Debug: Show working directory and environment
        add_log(f"Working directory: {scraper_dir}")
        add_log(f"run_all.py exists: {(scraper_dir / 'run_all.py').exists()}")

        # Step 2: Run the complete scraper pipeline using run_all.py
        add_log("Starting complete scraper pipeline with run_all.py...")
        if is_cancelled():
            return {"status": "cancelled"}

        # Set environment to use UTF-8 encoding
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"

        # DEBUG: Test basic subprocess and file operations
        add_log("=== DEBUGGING PRODUCTION ENVIRONMENT ===")
        add_log(f"Python executable: {sys.executable}")
        add_log(f"Current working dir: {os.getcwd()}")
        add_log(f"Scraper dir: {scraper_dir}")
        add_log(f"Files dir exists: {files_dir.exists()}")
        add_log(
            f"Can write to files dir: {os.access(str(files_dir), os.W_OK) if files_dir.exists() else 'Unknown'}"
        )

        # Test if we can run a simple subprocess
        try:
            simple_test = subprocess.run(
                [sys.executable, "-c", "print('Hello from subprocess')"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            add_log(
                f"Simple subprocess test: {simple_test.returncode}, stdout: {simple_test.stdout}"
            )
        except Exception as e:
            add_log(f"Simple subprocess failed: {e}")

        # Check if Chrome/Chromium is available (needed for scraping)
        try:
            chrome_test = subprocess.run(
                ["which", "google-chrome"], capture_output=True, text=True, timeout=10
            )
            add_log(
                f"Chrome available: {chrome_test.returncode == 0}, path: {chrome_test.stdout.strip()}"
            )
        except:
            try:
                chrome_test = subprocess.run(
                    ["which", "chromium"], capture_output=True, text=True, timeout=10
                )
                add_log(
                    f"Chromium available: {chrome_test.returncode == 0}, path: {chrome_test.stdout.strip()}"
                )
            except Exception as e:
                add_log(f"No browser found: {e}")

        # Run run_all.py which handles the complete pipeline
        run_all_script = scraper_dir / "run_all.py"
        result = subprocess.run(
            [sys.executable, str(run_all_script)],
            cwd=str(scraper_dir),
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            timeout=900,  # 15 minute timeout for complete pipeline
        )

        # Debug: Always log subprocess results
        add_log(f"Subprocess return code: {result.returncode}")
        add_log(
            f"Subprocess stdout: {result.stdout[:1000] if result.stdout else 'No stdout'}"
        )
        add_log(
            f"Subprocess stderr: {result.stderr[:1000] if result.stderr else 'No stderr'}"
        )

        # Debug: Check what files exist after pipeline
        files_created = (
            list((scraper_dir / "files").glob("*"))
            if (scraper_dir / "files").exists()
            else []
        )
        add_log(f"Files after pipeline: {[f.name for f in files_created]}")

        if result.returncode != 0:
            add_log(f"Scraper pipeline failed: {result.stderr}")
            add_log(f"Pipeline stdout: {result.stdout}")
            persist_state(status_override=ScraperTask.Status.ERROR)
            return {"status": "error", "error": f"Pipeline failed: {result.stderr}"}

        add_log("Scraper pipeline completed successfully")
        add_log(f"Pipeline output: {result.stdout}")
        set_progress(len(skus))

        # Step 3: Read results from the JSON file created by run_all.py
        add_log("Reading results from images.json...")
        results_file = scraper_dir / "files" / "images.json"
        results_data: dict[str, list[str]] = {}

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

        results = results_data
        persist_state(status_override=ScraperTask.Status.FINISHED)

        return {
            "status": "finished",
            "results": results_data,
            "total_skus": len(skus),
            "found_results": len(results_data),
        }

    except subprocess.TimeoutExpired:
        add_log("Scraping timed out")
        persist_state(status_override=ScraperTask.Status.ERROR)
        return {"status": "error", "error": "Scraping timed out"}
    except Exception as e:
        add_log(f"Error: {str(e)}")
        persist_state(status_override=ScraperTask.Status.ERROR)
        return {"status": "error", "error": str(e)}
