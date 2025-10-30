"""Minimal FastAPI web interface for running the scraping pipeline."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

import logging

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel
import uvicorn


PROJECT_ROOT = Path(__file__).resolve().parent
FILES_DIR = PROJECT_ROOT / "files"
SKU_FILE = FILES_DIR / "skus.txt"
RUN_ALL_SCRIPT = PROJECT_ROOT / "run_all.py"


class StartRequest(BaseModel):
    skus: str


class PipelineRunner:
    """Manage execution of the scraper pipeline and stream logs to clients."""

    def __init__(self) -> None:
        self._state_lock = asyncio.Lock()
        self._listeners: set[asyncio.Queue[Tuple[str, str]]] = set()
        self._history: list[Tuple[str, str]] = []
        self._task: Optional[asyncio.Task[None]] = None
        self._status: str = "idle"
        self._last_error: Optional[str] = None
        self._last_results: Optional[list[dict]] = None

    async def register_listener(
        self,
    ) -> Tuple[asyncio.Queue[Tuple[str, str]], list[Tuple[str, str]]]:
        queue: asyncio.Queue[Tuple[str, str]] = asyncio.Queue()
        logger.debug("Registering new listener %s", id(queue))
        async with self._state_lock:
            history_copy = list(self._history)
            self._listeners.add(queue)
            logger.debug(
                "Listener %s registered. Current listeners: %s. History length: %s",
                id(queue),
                len(self._listeners),
                len(self._history),
            )
        return queue, history_copy

    async def unregister_listener(self, queue: asyncio.Queue[Tuple[str, str]]) -> None:
        logger.debug("Unregistering listener %s", id(queue))
        async with self._state_lock:
            self._listeners.discard(queue)
            logger.debug(
                "Listener %s removed. Remaining listeners: %s",
                id(queue),
                len(self._listeners),
            )

    async def _broadcast(self, event: str, data: str) -> None:
        logger.debug("Broadcasting event=%s data=%r", event, data)
        async with self._state_lock:
            self._history.append((event, data))
            listeners = list(self._listeners)
            logger.debug(
                "History length now %s. Notifying %s listeners.",
                len(self._history),
                len(listeners),
            )
        for listener in listeners:
            logger.debug("Queueing event %s for listener %s", event, id(listener))
            listener.put_nowait((event, data))

    async def _set_status(self, status: str) -> None:
        logger.debug("Setting status to %s", status)
        async with self._state_lock:
            self._status = status
        await self._broadcast("status", status)

    async def start(self, raw_skus: str) -> None:
        logger.debug("Start requested with raw_skus=%r", raw_skus)
        cleaned_skus = self._normalise_skus(raw_skus)
        logger.debug("Normalised SKUs: %s", cleaned_skus)
        if not cleaned_skus:
            raise HTTPException(
                status_code=400, detail="Please provide at least one SKU."
            )

        async with self._state_lock:
            if self._task and not self._task.done():
                raise HTTPException(
                    status_code=409, detail="Pipeline is already running."
                )
            self._history.clear()
            self._last_error = None
            self._last_results = None
            logger.debug("Reset state for new pipeline run")

        FILES_DIR.mkdir(parents=True, exist_ok=True)
        SKU_FILE.write_text("\n".join(cleaned_skus) + "\n", encoding="utf-8")
        logger.info("Saved %s SKUs to %s", len(cleaned_skus), SKU_FILE)

        await self._broadcast(
            "log",
            f"Saved {len(cleaned_skus)} SKU(s) to {SKU_FILE.relative_to(PROJECT_ROOT)}",
        )

        if not RUN_ALL_SCRIPT.exists():
            error = f"run_all.py not found at {RUN_ALL_SCRIPT}"
            await self._broadcast("log", error)
            self._last_error = error
            logger.error(error)
            raise HTTPException(status_code=500, detail=error)

        await self._set_status("running")
        logger.info("Scheduling pipeline task")
        self._task = asyncio.create_task(self._run_pipeline())

    async def _run_pipeline(self) -> None:
        env = os.environ.copy()
        env.setdefault("PYTHONIOENCODING", "utf-8")
        env.setdefault("PYTHONUTF8", "1")

        await self._broadcast("log", "Starting scraper pipeline...\n")
        logger.info(
            "Launching %s with env overrides %s",
            RUN_ALL_SCRIPT,
            {k: env[k] for k in ["PYTHONIOENCODING", "PYTHONUTF8"]},
        )

        try:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                str(RUN_ALL_SCRIPT),
                cwd=str(PROJECT_ROOT),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=env,
            )
            logger.debug("Subprocess started with pid %s", process.pid)
        except Exception as exc:  # pragma: no cover - defensive logging
            message = f"Failed to launch pipeline: {exc}"
            await self._broadcast("log", message)
            self._last_error = message
            logger.exception("Failed to launch pipeline")
            await self._set_status("idle")
            return

        assert process.stdout is not None  # for type checkers

        try:
            while True:
                line = await process.stdout.readline()
                if not line:
                    logger.debug("No more output from pipeline process")
                    break
                text = line.decode("utf-8", errors="replace").rstrip("\n")
                logger.debug("Pipeline output: %s", text)
                await self._broadcast("log", text)

            return_code = await process.wait()
            logger.info("Pipeline process exited with code %s", return_code)
            await self._broadcast(
                "log",
                (
                    "Pipeline completed successfully."
                    if return_code == 0
                    else f"Pipeline exited with code {return_code}."
                ),
            )

            if return_code == 0:
                logger.debug("Attempting to load results")
                await self._load_results()
            else:
                self._last_error = f"Pipeline failed with exit code {return_code}."
                logger.warning(self._last_error)
        except asyncio.CancelledError:  # pragma: no cover - cancellation path
            await self._broadcast("log", "Pipeline execution cancelled.")
            self._last_error = "Pipeline was cancelled."
            logger.warning("Pipeline task cancelled")
            raise
        except Exception as exc:  # pragma: no cover - defensive logging
            message = f"Unexpected error: {exc}"
            await self._broadcast("log", message)
            self._last_error = message
            logger.exception("Unexpected error while running pipeline")
        finally:
            await self._set_status("idle")
            async with self._state_lock:
                self._task = None
            logger.debug("Pipeline task cleaned up")

    async def _load_results(self) -> None:
        results_file = FILES_DIR / "images.json"
        logger.debug("Looking for results file at %s", results_file)
        if not results_file.exists():
            await self._broadcast("log", "No images.json file produced.")
            self._last_results = None
            logger.warning("images.json file not found")
            return

        try:
            data = json.loads(results_file.read_text(encoding="utf-8"))
            logger.info(
                "Loaded images.json with %s top-level item(s)",
                len(data) if isinstance(data, list) else 1,
            )
        except json.JSONDecodeError as exc:
            message = f"Unable to parse images.json: {exc}"
            await self._broadcast("log", message)
            self._last_error = message
            self._last_results = None
            logger.exception("Failed to parse images.json")
            return

        if isinstance(data, list):
            summary_lines = [
                f"{entry.get('sku', '<unknown>')}: {len(entry.get('images', []))} image(s)"
                for entry in data
            ]
            logger.debug("Results summary prepared with %s line(s)", len(summary_lines))
            await self._broadcast("log", "--- Results summary ---")
            for line in summary_lines[:20]:
                await self._broadcast("log", line)
            if len(summary_lines) > 20:
                await self._broadcast(
                    "log", f"... and {len(summary_lines) - 20} more SKU(s)"
                )
            await self._broadcast("results", json.dumps(data))
            self._last_results = data
        else:
            await self._broadcast(
                "log", "images.json does not contain a list of results."
            )
            self._last_results = None
            logger.error("images.json payload was not a list: %r", data)

    @staticmethod
    def _normalise_skus(raw_skus: str) -> List[str]:
        candidates = [item.strip() for item in raw_skus.replace(",", "\n").splitlines()]
        filtered = [item for item in candidates if item]
        logger.debug("Normalised SKUs from %r to %r", raw_skus, filtered)
        return filtered

    async def get_status(self) -> dict:
        logger.debug("Status requested")
        async with self._state_lock:
            running = self._task is not None and not self._task.done()
            status = self._status
            error = self._last_error
            results_ready = self._last_results is not None
            results_count = len(self._last_results or [])
            logger.debug(
                "Status snapshot: running=%s status=%s error=%r results_ready=%s results_count=%s",
                running,
                status,
                error,
                results_ready,
                results_count,
            )
        return {
            "running": running,
            "status": status,
            "last_error": error,
            "results_ready": results_ready,
            "results_count": results_count,
        }

    async def get_results(self) -> list[dict]:
        logger.debug("Results requested")
        async with self._state_lock:
            return list(self._last_results or [])


logging.basicConfig(
    level=logging.DEBUG,
    format="[%(asctime)s] %(levelname)s %(name)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("web_app")


runner = PipelineRunner()
app = FastAPI(title="Scraper Runner")


def _format_sse(event: str, data: str) -> str:
    escaped_data = data.replace("\\", "\\\\").replace("\r", "")
    lines = escaped_data.split("\n") or [""]
    payload_lines = [f"data: {line}" for line in lines]
    payload = "\n".join(payload_lines)
    return f"event: {event}\n{payload}\n\n"


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    logger.debug(
        "Serving index for %s with query params %s",
        request.client,
        request.query_params,
    )
    html = (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '  <meta charset="utf-8">\n'
        "  <title>Scraper Runner</title>\n"
        "  <style>\n"
        "    body { font-family: monospace; background:#111; color:#e0e0e0; margin:0; padding:1.5rem; }\n"
        "    h1 { margin-top:0; }\n"
        "    textarea { width:100%; min-height:8rem; background:#000; color:#0f0; border:1px solid #333; padding:0.5rem; }\n"
        "    button { background:#0f0; color:#000; border:none; padding:0.5rem 1rem; font-weight:bold; cursor:pointer; margin-right:0.5rem; }\n"
        "    button:disabled { background:#555; color:#999; cursor:not-allowed; }\n"
        "    pre { background:#000; color:#0f0; padding:1rem; min-height:20rem; overflow:auto; border:1px solid #333; }\n"
        "    .status { margin:0.5rem 0 1rem; }\n"
        "    .results { margin-top:1rem; background:#000; color:#0ff; padding:1rem; border:1px solid #044; }\n"
        "    .gallery { margin-top:1rem; background:#000; color:#0ff; padding:1rem; border:1px solid #044; }\n"
        "    .sku-section { margin-bottom:2rem; border:1px solid #044; padding:1rem; }\n"
        "    .sku-title { color:#0f0; font-size:1.2rem; margin-bottom:0.5rem; }\n"
        "    .image-grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(200px,1fr)); gap:1rem; }\n"
        "    .image-card { position:relative; border:1px solid #333; padding:0.5rem; background:#111; }\n"
        "    .image-card img { width:100%; height:auto; display:block; }\n"
        "    .delete-btn { position:absolute; top:0.5rem; right:0.5rem; background:#f00; color:#fff; border:none; padding:0.25rem 0.5rem; cursor:pointer; font-weight:bold; }\n"
        "    .delete-btn:hover { background:#c00; }\n"
        "    .image-url { font-size:0.7rem; color:#888; word-break:break-all; margin-top:0.25rem; }\n"
        "    .upload-terminal { margin-top:1rem; background:#000; color:#0f0; padding:1rem; border:1px solid #0a0; }\n"
        "  </style>\n"
        "</head>\n"
        "<body>\n"
        "  <h1>Scraper Runner</h1>\n"
        '  <div class="status" id="status">Status: idle</div>\n'
        '  <form id="sku-form">\n'
        '    <label for="skus">Enter SKU codes (one per line or comma separated):</label><br>\n'
        '    <textarea id="skus" name="skus" placeholder="12345&#10;98765"></textarea>\n'
        '    <div style="margin-top:0.5rem;">\n'
        '      <button type="submit" id="run-btn">Run pipeline</button>\n'
        '      <button type="button" id="clear-btn">Clear log</button>\n'
        "    </div>\n"
        "  </form>\n"
        "  <h2>Terminal output</h2>\n"
        '  <pre id="terminal"></pre>\n'
        '  <div class="results" id="results" hidden>\n'
        "    <strong>Results JSON:</strong>\n"
        '    <button type="button" id="view-images-btn" style="margin-left:1rem;">View Images</button>\n'
        '    <pre id="results-json" style="background:#000; color:#0ff; margin-top:0.5rem; max-height:15rem; overflow:auto;"></pre>\n'
        "  </div>\n"
        '  <div class="gallery" id="gallery" hidden>\n'
        "    <h2>Image Gallery</h2>\n"
        '    <div style="background:#fff3cd; border:1px solid #ffc107; padding:0.75rem; margin-bottom:1rem; border-radius:4px; color:#000;">\n'
        "      <strong>⚠️ Reminder:</strong> Only the <strong>first 4 images</strong> for each SKU will be uploaded to Cloudinary.\n"
        "    </div>\n"
        '    <button type="button" id="close-gallery-btn">Close Gallery</button>\n'
        '    <button type="button" id="upload-btn" style="margin-left:1rem; background:#28a745; color:white;">Done & Upload to Cloudinary</button>\n'
        '    <div id="gallery-container" style="margin-top:1rem;"></div>\n'
        "  </div>\n"
        '  <div class="upload-terminal" id="upload-terminal" hidden>\n'
        "    <h2>Upload Progress</h2>\n"
        '    <button type="button" id="close-upload-btn">Close</button>\n'
        '    <pre id="upload-output" style="background:#000; color:#0f0; padding:1rem; max-height:30rem; overflow:auto; margin-top:0.5rem;"></pre>\n'
        "  </div>\n"
        "  <script>\n"
        "    const statusEl = document.getElementById('status');\n"
        "    const terminalEl = document.getElementById('terminal');\n"
        "    const runBtn = document.getElementById('run-btn');\n"
        "    const clearBtn = document.getElementById('clear-btn');\n"
        "    const formEl = document.getElementById('sku-form');\n"
        "    const resultsBox = document.getElementById('results');\n"
        "    const resultsJsonEl = document.getElementById('results-json');\n"
        "    function appendLine(target, text) {\n"
        "      target.textContent += (target.textContent ? '\\n' : '') + text;\n"
        "      target.scrollTop = target.scrollHeight;\n"
        "    }\n"
        "    async function refreshStatus() {\n"
        "      const response = await fetch('/status');\n"
        "      const data = await response.json();\n"
        "      statusEl.textContent = 'Status: ' + data.status + (data.last_error ? ' — ' + data.last_error : '');\n"
        "      runBtn.disabled = data.running;\n"
        "      if (!data.running && !data.results_ready) {\n"
        "        resultsBox.hidden = true;\n"
        "        resultsJsonEl.textContent = '';\n"
        "      }\n"
        "    }\n"
        "\n"
        "    async function startPipelineWithSkus(skus) {\n"
        "      console.log('[startPipelineWithSkus] called with skus:', skus);\n"
        "      try {\n"
        "        console.log('[startPipelineWithSkus] Sending POST /start');\n"
        "        const response = await fetch('/start', {\n"
        "          method: 'POST',\n"
        "          headers: { 'Content-Type': 'application/json' },\n"
        "          body: JSON.stringify({ skus })\n"
        "        });\n"
        "        console.log('[startPipelineWithSkus] Response status:', response.status);\n"
        "        if (!response.ok) {\n"
        "          const payload = await response.json().catch(() => ({}));\n"
        "          alert(payload.detail || 'Unable to start pipeline');\n"
        "          return;\n"
        "        }\n"
        "        console.log('[startPipelineWithSkus] Success, clearing terminal');\n"
        "        terminalEl.textContent = '';\n"
        "        resultsBox.hidden = true;\n"
        "        resultsJsonEl.textContent = '';\n"
        "        await refreshStatus();\n"
        "      } catch (err) {\n"
        "        console.error('[startPipelineWithSkus] Error:', err);\n"
        "        alert('Network error starting pipeline: ' + err);\n"
        "      }\n"
        "    }\n"
        "    formEl.addEventListener('submit', async (event) => {\n"
        "      event.preventDefault();\n"
        "      const skus = document.getElementById('skus').value;\n"
        "      await startPipelineWithSkus(skus);\n"
        "    });\n"
        "    clearBtn.addEventListener('click', () => {\n"
        "      terminalEl.textContent = '';\n"
        "    });\n"
        "    const source = new EventSource('/stream');\n"
        "    source.addEventListener('log', (event) => {\n"
        "      appendLine(terminalEl, event.data);\n"
        "    });\n"
        "    source.addEventListener('status', (event) => {\n"
        "      statusEl.textContent = 'Status: ' + event.data;\n"
        "      runBtn.disabled = event.data === 'running';\n"
        "    });\n"
        "    source.addEventListener('results', (event) => {\n"
        "      resultsBox.hidden = false;\n"
        "      try {\n"
        "        const parsed = JSON.parse(event.data);\n"
        "        resultsJsonEl.textContent = JSON.stringify(parsed, null, 2);\n"
        "      } catch (err) {\n"
        "        resultsJsonEl.textContent = event.data;\n"
        "      }\n"
        "    });\n"
        "    source.onerror = () => {\n"
        "      statusEl.textContent = 'Status: connection lost';\n"
        "    };\n"
        "\n"
        "    let currentResults = [];\n"
        "    const viewImagesBtn = document.getElementById('view-images-btn');\n"
        "    const closeGalleryBtn = document.getElementById('close-gallery-btn');\n"
        "    const galleryDiv = document.getElementById('gallery');\n"
        "    const galleryContainer = document.getElementById('gallery-container');\n"
        "    const uploadBtn = document.getElementById('upload-btn');\n"
        "    const uploadTerminalDiv = document.getElementById('upload-terminal');\n"
        "    const uploadOutputEl = document.getElementById('upload-output');\n"
        "    const closeUploadBtn = document.getElementById('close-upload-btn');\n"
        "\n"
        "    viewImagesBtn.addEventListener('click', async () => {\n"
        "      const response = await fetch('/results');\n"
        "      const data = await response.json();\n"
        "      currentResults = data.results || [];\n"
        "      renderGallery();\n"
        "      galleryDiv.hidden = false;\n"
        "    });\n"
        "\n"
        "    closeGalleryBtn.addEventListener('click', () => {\n"
        "      galleryDiv.hidden = true;\n"
        "    });\n"
        "\n"
        "    uploadBtn.addEventListener('click', async () => {\n"
        "      if (!confirm('Upload curated images to Cloudinary? Only the first 4 images per SKU will be uploaded.')) return;\n"
        "      \n"
        "      galleryDiv.hidden = true;\n"
        "      uploadTerminalDiv.hidden = false;\n"
        "      uploadOutputEl.textContent = '';\n"
        "      uploadBtn.disabled = true;\n"
        "      \n"
        "      const uploadSource = new EventSource('/upload-to-cloudinary');\n"
        "      \n"
        "      uploadSource.addEventListener('log', (e) => {\n"
        "        uploadOutputEl.textContent += e.data;\n"
        "        uploadOutputEl.scrollTop = uploadOutputEl.scrollHeight;\n"
        "      });\n"
        "      \n"
        "      uploadSource.addEventListener('done', (e) => {\n"
        "        uploadBtn.disabled = false;\n"
        "        uploadSource.close();\n"
        "        if (e.data === 'success') {\n"
        "          uploadOutputEl.textContent += '\\n\\n🎉 All done! Images uploaded to Cloudinary.';\n"
        "        }\n"
        "      });\n"
        "      \n"
        "      uploadSource.onerror = () => {\n"
        "        uploadBtn.disabled = false;\n"
        "        uploadSource.close();\n"
        "        uploadOutputEl.textContent += '\\n\\n❌ Connection error during upload.';\n"
        "      };\n"
        "    });\n"
        "\n"
        "    closeUploadBtn.addEventListener('click', () => {\n"
        "      uploadTerminalDiv.hidden = true;\n"
        "    });\n"
        "\n"
        "    function renderGallery() {\n"
        "      galleryContainer.innerHTML = '';\n"
        "      if (!currentResults.length) {\n"
        "        galleryContainer.innerHTML = '<p>No results to display</p>';\n"
        "        return;\n"
        "      }\n"
        "      currentResults.forEach(entry => {\n"
        "        const sku = entry.sku || 'Unknown';\n"
        "        const images = entry.images || [];\n"
        "        const section = document.createElement('div');\n"
        "        section.className = 'sku-section';\n"
        "        const title = document.createElement('div');\n"
        "        title.className = 'sku-title';\n"
        "        title.textContent = `SKU: ${sku} (${images.length} image(s))`;\n"
        "        section.appendChild(title);\n"
        "        if (!images.length) {\n"
        "          const noImg = document.createElement('p');\n"
        "          noImg.textContent = 'No images';\n"
        "          section.appendChild(noImg);\n"
        "        } else {\n"
        "          const grid = document.createElement('div');\n"
        "          grid.className = 'image-grid';\n"
        "          images.forEach(url => {\n"
        "            const card = document.createElement('div');\n"
        "            card.className = 'image-card';\n"
        "            const img = document.createElement('img');\n"
        "            img.src = url;\n"
        "            img.alt = sku;\n"
        "            img.loading = 'lazy';\n"
        "            const deleteBtn = document.createElement('button');\n"
        "            deleteBtn.className = 'delete-btn';\n"
        "            deleteBtn.textContent = 'X';\n"
        "            deleteBtn.onclick = async () => {\n"
        "              if (!confirm(`Delete this image from ${sku}?`)) return;\n"
        "              try {\n"
        "                const response = await fetch('/delete-image', {\n"
        "                  method: 'POST',\n"
        "                  headers: { 'Content-Type': 'application/json' },\n"
        "                  body: JSON.stringify({ sku, image_url: url })\n"
        "                });\n"
        "                if (response.ok) {\n"
        "                  card.remove();\n"
        "                  const idx = currentResults.findIndex(e => e.sku === sku);\n"
        "                  if (idx !== -1) {\n"
        "                    currentResults[idx].images = currentResults[idx].images.filter(u => u !== url);\n"
        "                    title.textContent = `SKU: ${sku} (${currentResults[idx].images.length} image(s))`;\n"
        "                  }\n"
        "                } else {\n"
        "                  const payload = await response.json();\n"
        "                  alert('Delete failed: ' + (payload.detail || 'Unknown error'));\n"
        "                }\n"
        "              } catch (err) {\n"
        "                alert('Network error: ' + err);\n"
        "              }\n"
        "            };\n"
        "            const urlLabel = document.createElement('div');\n"
        "            urlLabel.className = 'image-url';\n"
        "            urlLabel.textContent = url.slice(0, 60) + (url.length > 60 ? '...' : '');\n"
        "            card.appendChild(img);\n"
        "            card.appendChild(deleteBtn);\n"
        "            card.appendChild(urlLabel);\n"
        "            grid.appendChild(card);\n"
        "          });\n"
        "          section.appendChild(grid);\n"
        "        }\n"
        "        galleryContainer.appendChild(section);\n"
        "      });\n"
        "    }\n"
        "\n"
        "    // Auto-start if ?skus= is present in the URL\n"
        "    console.log('[init] Page loaded, checking for ?skus=');\n"
        "    const params = new URLSearchParams(window.location.search);\n"
        "    const urlSkus = params.get('skus') || '';\n"
        "    console.log('[init] urlSkus:', urlSkus);\n"
        "    if (urlSkus) {\n"
        "      console.log('[init] Found URL SKUs, filling textarea and auto-starting');\n"
        "      document.getElementById('skus').value = urlSkus;\n"
        "      // Wait a moment for SSE to establish before starting\n"
        "      setTimeout(() => {\n"
        "        console.log('[init] Calling startPipelineWithSkus now');\n"
        "        startPipelineWithSkus(urlSkus);\n"
        "      }, 500);\n"
        "    }\n"
        "    refreshStatus();\n"
        "    console.log('[init] Setup complete');\n"
        "  </script>\n"
        "</body>\n"
        "</html>\n"
    )
    return HTMLResponse(content=html)


@app.post("/start")
async def start_pipeline(request: StartRequest) -> JSONResponse:
    logger.info("/start invoked with payload length=%s", len(request.skus))
    await runner.start(request.skus)
    return JSONResponse({"status": "started"})


# Optional: GET helper to trigger pipeline from the address bar
@app.get("/start-get")
async def start_pipeline_get(skus: str = "") -> JSONResponse:
    logger.info("/start-get invoked with payload length=%s", len(skus))
    await runner.start(skus)
    return JSONResponse({"status": "started"})


@app.get("/status")
async def pipeline_status() -> JSONResponse:
    logger.debug("/status endpoint called")
    status_payload = await runner.get_status()
    logger.debug("/status returning %s", status_payload)
    return JSONResponse(status_payload)


@app.get("/results")
async def pipeline_results() -> JSONResponse:
    logger.debug("/results endpoint called")
    results_payload = await runner.get_results()
    logger.debug("/results returning %s", results_payload)
    return JSONResponse({"results": results_payload})


class DeleteImageRequest(BaseModel):
    sku: str
    image_url: str


@app.post("/delete-image")
async def delete_image(request: DeleteImageRequest) -> JSONResponse:
    """Remove a specific image from a SKU's image list in images.json"""
    logger.info("Delete image request: sku=%s url=%s", request.sku, request.image_url)

    results_file = FILES_DIR / "images.json"
    if not results_file.exists():
        raise HTTPException(status_code=404, detail="images.json not found")

    try:
        data = json.loads(results_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=500, detail=f"Failed to parse images.json: {exc}"
        )

    if not isinstance(data, list):
        raise HTTPException(status_code=500, detail="images.json is not a list")

    found = False
    for entry in data:
        if entry.get("sku") == request.sku:
            images = entry.get("images", [])
            if request.image_url in images:
                images.remove(request.image_url)
                entry["images"] = images
                found = True
                logger.info(
                    "Removed image from SKU %s, %d images remaining",
                    request.sku,
                    len(images),
                )
                break

    if not found:
        raise HTTPException(status_code=404, detail="SKU or image not found")

    # Write back to file
    results_file.write_text(json.dumps(data, indent=2), encoding="utf-8")

    # Update runner's cached results
    async with runner._state_lock:
        runner._last_results = data

    return JSONResponse(
        {
            "status": "deleted",
            "sku": request.sku,
            "remaining": len(
                [e for e in data if e.get("sku") == request.sku][0].get("images", [])
            ),
        }
    )


@app.get("/upload-to-cloudinary")
async def upload_to_cloudinary() -> StreamingResponse:
    """Execute upload_catalog.py and stream the output"""
    logger.info("Starting Cloudinary upload")

    upload_script = PROJECT_ROOT / "upload_catalog.py"
    images_json = FILES_DIR / "images.json"

    if not upload_script.exists():
        raise HTTPException(status_code=404, detail="upload_catalog.py not found")
    if not images_json.exists():
        raise HTTPException(status_code=404, detail="images.json not found")

    from typing import AsyncGenerator

    async def upload_stream() -> AsyncGenerator[str, None]:
        try:
            env = os.environ.copy()
            env.setdefault("PYTHONIOENCODING", "utf-8")
            env.setdefault("PYTHONUTF8", "1")

            process = await asyncio.create_subprocess_exec(
                sys.executable,
                str(upload_script),
                str(images_json),
                cwd=str(PROJECT_ROOT),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=env,
            )

            yield _format_sse("log", "Starting Cloudinary upload...\n")

            if process.stdout:
                async for line_bytes in process.stdout:
                    line = line_bytes.decode("utf-8", errors="replace")
                    yield _format_sse("log", line)

            await process.wait()

            if process.returncode == 0:
                yield _format_sse("log", "\n✅ Upload complete!\n")
                yield _format_sse("done", "success")
            else:
                yield _format_sse(
                    "log", f"\n❌ Upload failed with code {process.returncode}\n"
                )
                yield _format_sse("done", "error")

        except Exception as exc:
            logger.exception("Upload error")
            yield _format_sse("log", f"Error: {exc}\n")
            yield _format_sse("done", "error")

    return StreamingResponse(
        upload_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/stream")
async def stream() -> StreamingResponse:
    logger.debug("/stream connection opened")
    queue, history = await runner.register_listener()
    logger.debug("/stream history length %s for listener %s", len(history), id(queue))

    from typing import AsyncGenerator

    async def event_generator() -> AsyncGenerator[str, None]:
        try:
            for event, data in history:
                logger.debug("Sending historical event=%s data=%r", event, data)
                yield _format_sse(event, data)
            while True:
                event, data = await queue.get()
                logger.debug("Sending live event=%s data=%r", event, data)
                yield _format_sse(event, data)
        finally:
            logger.debug("/stream connection closing for listener %s", id(queue))
            await runner.unregister_listener(queue)

    return StreamingResponse(event_generator(), media_type="text/event-stream")


__all__ = ["app"]


def main(argv: Optional[List[str]] = None) -> None:
    """Run the FastAPI application with configurable host/port."""

    parser = argparse.ArgumentParser(description="Run the scraper web TUI server.")
    parser.add_argument(
        "--host",
        default=os.getenv("SCRAPER_TUI_HOST", "0.0.0.0"),
        help="Network host to bind to (default: 0.0.0.0 or SCRAPER_TUI_HOST).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("SCRAPER_TUI_PORT", "8000")),
        help="Port to listen on (default: 8000 or SCRAPER_TUI_PORT).",
    )
    parser.add_argument(
        "--reload",
        action="store_true",
        help="Enable auto-reload (useful for development only).",
    )

    args = parser.parse_args(argv)
    logger.info(
        "Starting uvicorn with host=%s port=%s reload=%s",
        args.host,
        args.port,
        args.reload,
    )

    uvicorn.run("web_app:app", host=args.host, port=args.port, reload=args.reload)


if __name__ == "__main__":  # pragma: no cover - convenience CLI
    main()
