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
import textwrap

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
        self._process: Optional[asyncio.subprocess.Process] = None
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

    async def stop(self) -> None:
        """Stop the currently running pipeline"""
        logger.info("Stop requested")
        async with self._state_lock:
            if not self._task or self._task.done():
                raise HTTPException(status_code=400, detail="No pipeline is running")
            task = self._task
            process = self._process

        if process:
            logger.info("Terminating process with pid %s", process.pid)
            try:
                process.terminate()
                await self._broadcast("log", "⚠️ Pipeline termination requested...")
                try:
                    await asyncio.wait_for(process.wait(), timeout=5.0)
                    logger.info("Process terminated gracefully")
                except asyncio.TimeoutError:
                    logger.warning("Process did not terminate, killing it")
                    process.kill()
                    await process.wait()
                    await self._broadcast("log", "❌ Pipeline forcefully killed")
            except Exception as exc:
                logger.exception("Error stopping process")
                await self._broadcast("log", f"Error stopping process: {exc}")

        if task and not task.done():
            logger.info("Cancelling pipeline task")
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                logger.info("Pipeline task cancelled successfully")

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
            async with self._state_lock:
                self._process = process
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
                self._process = None
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
    html = textwrap.dedent(
        """
        <!DOCTYPE html>
        <html lang="en">
        <head>
          <meta charset="utf-8">
          <title>Scraper Runner</title>
          <style>
            /* site-theme.css
               Dark, high-contrast "MNP" theme used by the app.
               - Put this in your global stylesheet and adjust variables in :root as needed.
            */

            /* --- Theme variables (edit these to recolor the theme) --- */
            :root{
              --bg: #0b0b0d;                  /* page background */
              --panel: #0f1113;               /* panels, tiles */
              --panel-2: #0e1112;             /* alternate panel */
              --muted: #7a7f85;               /* secondary text */
              --text: #e6eef8;                /* main text */
              --accent: #3da3ff;              /* primary blue */
              --accent-2: #2b6fb6;            /* darker accent */
              --border: rgba(125,150,200,0.12);
              --glass: rgba(255,255,255,0.02);
              --glass-2: rgba(255,255,255,0.03);
              --glass-line: linear-gradient(90deg, transparent, rgba(58,140,255,0.12), transparent);
              --shadow-weak: 0 6px 18px rgba(6,12,20,0.4);
              --shadow-strong: 0 10px 30px rgba(13,43,90,0.45);
              --danger: #ff6b6b;
              --success: #2ad07a;
              --mono: ui-monospace, SFMono-Regular, Menlo, Monaco, "Roboto Mono", "Courier New", monospace;
              --ui-sans: Inter, system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial;
              --radius: 0px;
              --grid-accent: rgba(30,40,50,0.06);
            }

            /* --- Base page styles --- */
            html, body, #root {
              height: 100%;
            }
            body {
              background: var(--bg);
              color: var(--text);
              font-family: var(--ui-sans);
              margin: 0;
              -webkit-font-smoothing:antialiased;
              -moz-osx-font-smoothing:grayscale;
              line-height: 1.35;
              font-size: 14px;
            }

            /* Subtle textured background (military-grid) */
            .military-grid {
              background-image:
                repeating-linear-gradient(0deg, rgba(255,255,255,0.01) 0 1px, transparent 1px 40px),
                linear-gradient(180deg, rgba(255,255,255,0.01), transparent 180px),
                var(--bg);
              background-blend-mode: overlay;
              min-height: 100vh;
            }

            /* Decorative thin highlight lines used in header/footer */
            .glow-line {
              height: 1px;
              background: var(--glass-line);
              display:block;
            }

            /* --- Typography helpers (project-specific) --- */
            .wayne-title {
              font-family: var(--ui-sans);
              font-weight: 700;
              letter-spacing: 0.01em;
              color: var(--text);
              margin: 0;
            }

            .wayne-subtitle {
              font-family: var(--ui-sans);
              font-weight: 600;
              color: var(--text);
              text-transform: none;
              margin: 6px 0 0;
            }

            .wayne-mono {
              font-family: var(--mono);
              color: var(--muted);
              letter-spacing: 0.06em;
            }

            /* Small uppercase micro text used across the UI */
            .mini-uppercase {
              font-size: 11px;
              letter-spacing: .14em;
              text-transform: uppercase;
              color: var(--muted);
            }

            /* --- Pulse indicator (blue dot) --- */
            @keyframes pulse-blue {
              0% { box-shadow: 0 0 0 0 rgba(61,163,255,0.45); }
              50% { box-shadow: 0 0 0 6px rgba(61,163,255,0.06); }
              100% { box-shadow: 0 0 0 0 rgba(61,163,255,0.00); }
            }

            /* --- Layout containers --- */
            .app-container {
              max-width: 960px;
              margin: 0 auto;
              padding: 48px 32px 72px;
              display: flex;
              flex-direction: column;
              gap: 28px;
            }

            .page-header {
              display: flex;
              justify-content: space-between;
              align-items: flex-end;
              gap: 16px;
              flex-wrap: wrap;
            }

            .status-chip {
              display: inline-flex;
              align-items: center;
              gap: 10px;
              padding: 8px 12px;
              background: rgba(61,163,255,0.08);
              color: var(--accent);
              border-radius: 4px;
              font-weight: 600;
              font-size: 12px;
              letter-spacing: 0.02em;
              border: 1px solid rgba(61,163,255,0.12);
              text-transform: uppercase;
            }

            .status-chip::before {
              content: "";
              width: 8px;
              height: 8px;
              border-radius: 2px;
              background: var(--accent);
              animation: pulse-blue 1.8s infinite;
              box-shadow: 0 0 0 0 rgba(61,163,255,0.45);
            }

            /* --- Scraper button (spider) --- */
            .scraper-button {
              display:inline-flex;
              align-items:center;
              gap:8px;
              padding:6px 10px;
              background: linear-gradient(90deg,var(--accent) 0%, var(--accent-2) 100%);
              color: white;
              border-radius:6px;
              text-decoration:none;
              font-weight:600;
              font-size:12px;
              box-shadow: var(--shadow-weak);
            }
            .scraper-button:hover {
              opacity: .95;
              transform: translateY(-1px);
            }

            /* --- Command center tiles / primary operation tiles --- */
            .command-interface {
              background: linear-gradient(180deg, rgba(255,255,255,0.01), rgba(255,255,255,0.00)), var(--panel);
              border: 1px solid rgba(61,163,255,0.06);
              padding: 28px;
              border-radius: var(--radius);
              box-shadow: 0 4px 24px rgba(0,0,0,0.6);
            }

            .panel {
              padding: 24px;
              background: var(--panel-2);
              border: 1px solid rgba(255,255,255,0.02);
              border-radius: var(--radius);
              box-shadow: 0 4px 24px rgba(0,0,0,0.35);
            }

            .form-stack {
              display: flex;
              flex-direction: column;
              gap: 16px;
            }

            textarea {
              width: 100%;
              min-height: 10rem;
              background: var(--panel-2);
              color: var(--text);
              border: 1px solid rgba(61,163,255,0.12);
              padding: 14px;
              font-family: var(--mono);
              font-size: 13px;
              border-radius: var(--radius);
              box-shadow: inset 0 0 0 1px rgba(255,255,255,0.02);
            }

            textarea:focus {
              outline: none;
              border-color: rgba(61,163,255,0.35);
              box-shadow: 0 0 0 1px rgba(61,163,255,0.35);
              background: rgba(61,163,255,0.04);
            }

            .button-row {
              display: flex;
              flex-wrap: wrap;
              gap: 12px;
            }

            /* --- Buttons (primary) --- */
            .wayne-button-primary {
              display:inline-flex;
              align-items:center;
              justify-content:center;
              gap: 10px;
              padding: 12px 20px;
              border-radius: 6px;
              color: white;
              background: linear-gradient(90deg,var(--accent) 0%, var(--accent-2) 100%);
              border: 1px solid rgba(61,163,255,0.08);
              font-weight:700;
              font-size:14px;
              text-decoration:none;
              box-shadow: 0 8px 30px rgba(20,60,120,0.12);
              cursor: pointer;
              transition: transform .12s ease, box-shadow .12s ease, opacity .12s ease;
            }

            .wayne-button-primary:hover:not([disabled]) {
              transform: translateY(-1px);
              box-shadow: 0 12px 36px rgba(61,163,255,0.18);
            }

            .wayne-button-primary[disabled] {
              opacity: .5;
              cursor: not-allowed;
              transform:none;
            }

            .button-danger {
              background: linear-gradient(90deg, rgba(255,107,107,0.85), rgba(220,53,69,0.85));
              border-color: rgba(255,107,107,0.32);
              box-shadow: 0 8px 30px rgba(220,53,69,0.18);
            }

            .button-danger:hover:not([disabled]) {
              box-shadow: 0 12px 42px rgba(220,53,69,0.28);
            }

            .wayne-button-ghost {
              display: inline-flex;
              align-items: center;
              justify-content: center;
              gap: 8px;
              padding: 12px 18px;
              border-radius: 6px;
              background: rgba(255,255,255,0.02);
              color: var(--text);
              border: 1px solid rgba(255,255,255,0.06);
              font-weight: 600;
              font-size: 13px;
              cursor: pointer;
              text-decoration: none;
              transition: background .12s ease, border-color .12s ease, color .12s ease;
            }

            .wayne-button-ghost:hover {
              background: rgba(61,163,255,0.08);
              border-color: rgba(61,163,255,0.12);
              color: var(--accent);
            }

            .hint {
              color: var(--muted);
              font-size:12px;
            }

            .section-title {
              margin: 0 0 12px;
              font-size: 20px;
              font-weight: 700;
              letter-spacing: 0.02em;
            }

            .section-header {
              display: flex;
              justify-content: space-between;
              align-items: center;
              gap: 16px;
              flex-wrap: wrap;
              margin-bottom: 16px;
            }

            .log-terminal,
            .results-pre {
              background: rgba(8,10,12,0.9);
              color: var(--success);
              padding: 18px;
              border: 1px solid rgba(61,163,255,0.08);
              min-height: 20rem;
              border-radius: var(--radius);
              overflow: auto;
              font-family: var(--mono);
              font-size: 13px;
              box-shadow: inset 0 0 0 1px rgba(255,255,255,0.02);
            }

            .results-pre {
              color: var(--accent);
              min-height: 12rem;
              max-height: 15rem;
            }

            .gallery-panel .results-pre {
              min-height: unset;
              max-height: unset;
            }

            .warning-banner {
              background: rgba(255,210,120,0.12);
              border: 1px solid rgba(255,210,120,0.26);
              padding: 14px 16px;
              border-radius: 4px;
              color: #f8d57c;
              font-size: 13px;
              margin-bottom: 18px;
            }

            .gallery-grid {
              display:grid;
              grid-template-columns:repeat(auto-fill,minmax(220px,1fr));
              gap:18px;
            }

            .image-grid {
              display:grid;
              grid-template-columns:repeat(auto-fill,minmax(220px,1fr));
              gap:18px;
            }

            .sku-section {
              margin-bottom: 28px;
              padding: 18px;
              border: 1px solid rgba(61,163,255,0.08);
              background: rgba(12,16,20,0.65);
              border-radius: var(--radius);
              box-shadow: inset 0 0 0 1px rgba(255,255,255,0.02);
            }

            .sku-title {
              color: var(--accent);
              font-weight: 700;
              font-size: 14px;
              margin: 0 0 12px;
              text-transform: uppercase;
              letter-spacing: 0.08em;
            }

            .image-card {
              position:relative;
              border:1px solid rgba(61,163,255,0.06);
              padding:12px;
              background: linear-gradient(180deg, rgba(255,255,255,0.012), rgba(0,0,0,0.02));
              border-radius: var(--radius);
              box-shadow: 0 8px 24px rgba(0,0,0,0.45);
            }

            .image-card img {
              width:100%;
              height:auto;
              display:block;
              border-radius: 2px;
            }

            .delete-btn {
              position:absolute;
              top:12px;
              right:12px;
              background: linear-gradient(90deg, rgba(255,107,107,0.9), rgba(220,53,69,0.9));
              color:#fff;
              border:none;
              padding:6px 10px;
              cursor:pointer;
              font-weight:600;
              font-size:11px;
              border-radius:4px;
              box-shadow: 0 10px 28px rgba(220,53,69,0.26);
              transition: transform .12s ease, box-shadow .12s ease;
            }

            .delete-btn:hover {
              transform: translateY(-1px);
              box-shadow: 0 12px 32px rgba(220,53,69,0.35);
            }

            .image-url {
              font-size:11px;
              color: var(--muted);
              word-break:break-all;
              margin-top:8px;
              font-family: var(--mono);
            }

            .upload-terminal {
              display: flex;
              flex-direction: column;
              gap: 18px;
            }

            .upload-terminal .results-pre {
              max-height: 30rem;
            }

            .site-footer {
              border-top: 1px solid var(--border);
              background: linear-gradient(180deg, rgba(255,255,255,0.00), rgba(255,255,255,0.01));
              padding: 20px 28px;
              color: var(--muted);
            }

            @media (max-width: 900px) {
              .app-container {
                padding: 32px 20px 64px;
              }

              .log-terminal,
              .results-pre {
                min-height: 14rem;
              }

              .command-interface,
              .panel {
                padding: 22px;
              }

              .wayne-title { font-size: 1.7rem; }
            }

            @media (max-width: 600px) {
              .button-row {
                flex-direction: column;
                align-items: stretch;
              }

              .wayne-button-primary,
              .wayne-button-ghost {
                width: 100%;
              }
            }
          </style>
        </head>
        <body class="military-grid">
          <div class="glow-line"></div>
          <main class="app-container">
            <header class="page-header">
              <div>
                <h1 class="wayne-title">Scraper Runner</h1>
                <p class="wayne-subtitle">Control the scraping pipeline and review results.</p>
              </div>
              <div class="status-chip" id="status">Status: idle</div>
            </header>
            <section class="command-interface">
              <form id="sku-form" class="form-stack">
                <label for="skus" class="mini-uppercase">Enter SKU codes (one per line or comma separated)</label>
                <textarea id="skus" name="skus" placeholder="12345&#10;98765"></textarea>
                <div class="button-row">
                  <button type="submit" id="run-btn" class="wayne-button-primary">Run pipeline</button>
                  <button type="button" id="stop-btn" class="wayne-button-primary button-danger" disabled>Stop Pipeline</button>
                  <button type="button" id="clear-btn" class="wayne-button-ghost">Clear log</button>
                </div>
              </form>
            </section>
            <section class="panel">
              <h2 class="section-title">Terminal output</h2>
              <pre id="terminal" class="log-terminal"></pre>
            </section>
            <section class="command-interface results-panel" id="results" hidden>
              <div class="section-header">
                <h2 class="section-title">Results JSON</h2>
                <button type="button" id="view-images-btn" class="wayne-button-ghost">View Images</button>
              </div>
              <pre id="results-json" class="results-pre"></pre>
            </section>
            <section class="command-interface gallery-panel" id="gallery" hidden>
              <div class="section-header">
                <h2 class="section-title">Image Gallery</h2>
                <div class="button-row">
                  <button type="button" id="close-gallery-btn" class="wayne-button-ghost">Close Gallery</button>
                  <button type="button" id="upload-btn" class="wayne-button-primary">Done &amp; Upload to Cloudinary</button>
                </div>
              </div>
              <div class="warning-banner">
                <strong>⚠️ Reminder:</strong> Only the <strong>first 4 images</strong> for each SKU will be uploaded to Cloudinary.
              </div>
              <div id="gallery-container" class="gallery-grid"></div>
            </section>
            <section class="command-interface upload-terminal" id="upload-terminal" hidden>
              <div class="section-header">
                <h2 class="section-title">Upload Progress</h2>
                <button type="button" id="close-upload-btn" class="wayne-button-ghost">Close</button>
              </div>
              <pre id="upload-output" class="results-pre"></pre>
            </section>
          </main>
          <div class="glow-line"></div>
        """
    )
    html += (
        "  <script>\n"
        "    (function() {\n"
        "      const largeHeader = document.getElementById('large-header');\n"
        "      const canvas = document.getElementById('demo-canvas');\n"
        "      if (!largeHeader || !canvas || typeof gsap === 'undefined') {\n"
        "        return;\n"
        "      }\n"
        "      const ctx = canvas.getContext('2d');\n"
        "      let width = 0;\n"
        "      let height = 0;\n"
        "      let points = [];\n"
        "      let target = { x: 0, y: 0 };\n"
        "      let animateHeader = true;\n"
        "      function resizeCanvas() {\n"
        "        width = largeHeader.offsetWidth;\n"
        "        height = largeHeader.offsetHeight;\n"
        "        const ratio = window.devicePixelRatio || 1;\n"
        "        canvas.width = width * ratio;\n"
        "        canvas.height = height * ratio;\n"
        "        canvas.style.width = width + 'px';\n"
        "        canvas.style.height = height + 'px';\n"
        "        ctx.setTransform(ratio, 0, 0, ratio, 0, 0);\n"
        "      }\n"
        "      function killTweens() {\n"
        "        points.forEach(point => gsap.killTweensOf(point));\n"
        "      }\n"
        "      function buildPoints() {\n"
        "        killTweens();\n"
        "        points = [];\n"
        "        const stepX = Math.max(width / 20, 40);\n"
        "        const stepY = Math.max(height / 20, 40);\n"
        "        for (let x = 0; x <= width; x += stepX) {\n"
        "          for (let y = 0; y <= height; y += stepY) {\n"
        "            const px = x + Math.random() * stepX;\n"
        "            const py = y + Math.random() * stepY;\n"
        "            const point = { x: px, originX: px, y: py, originY: py };\n"
        "            points.push(point);\n"
        "          }\n"
        "        }\n"
        "        for (let i = 0; i < points.length; i++) {\n"
        "          const point = points[i];\n"
        "          const closest = [];\n"
        "          for (let j = 0; j < points.length; j++) {\n"
        "            const candidate = points[j];\n"
        "            if (point === candidate) continue;\n"
        "            const dist = getDistance(point, candidate);\n"
        "            if (closest.length < 5) {\n"
        "              closest.push({ candidate, dist });\n"
        "              closest.sort((a, b) => a.dist - b.dist);\n"
        "            } else if (dist < closest[closest.length - 1].dist) {\n"
        "              closest[closest.length - 1] = { candidate, dist };\n"
        "              closest.sort((a, b) => a.dist - b.dist);\n"
        "            }\n"
        "          }\n"
        "          point.closest = closest.map(item => item.candidate);\n"
        "          point.circle = new Circle(point, 2 + Math.random() * 2);\n"
        "        }\n"
        "      }\n"
        "      function shiftPoint(point) {\n"
        "        gsap.to(point, {\n"
        "          duration: 1 + Math.random(),\n"
        "          x: point.originX - 50 + Math.random() * 100,\n"
        "          y: point.originY - 50 + Math.random() * 100,\n"
        "          ease: 'circ.inOut',\n"
        "          onComplete: () => shiftPoint(point)\n"
        "        });\n"
        "      }\n"
        "      function animate() {\n"
        "        if (!animateHeader) {\n"
        "          requestAnimationFrame(animate);\n"
        "          return;\n"
        "        }\n"
        "        ctx.clearRect(0, 0, width, height);\n"
        "        points.forEach(point => {\n"
        "          const distance = Math.abs(getDistance(target, point));\n"
        "          if (distance < 4000) {\n"
        "            point.active = 0.3;\n"
        "            point.circle.active = 0.6;\n"
        "          } else if (distance < 20000) {\n"
        "            point.active = 0.1;\n"
        "            point.circle.active = 0.3;\n"
        "          } else if (distance < 40000) {\n"
        "            point.active = 0.02;\n"
        "            point.circle.active = 0.12;\n"
        "          } else {\n"
        "            point.active = 0;\n"
        "            point.circle.active = 0;\n"
        "          }\n"
        "          drawLines(point);\n"
        "          point.circle.draw();\n"
        "        });\n"
        "        requestAnimationFrame(animate);\n"
        "      }\n"
        "      function drawLines(point) {\n"
        "        if (!point.active || !point.closest) return;\n"
        "        point.closest.forEach(closePoint => {\n"
        "          ctx.beginPath();\n"
        "          ctx.moveTo(point.x, point.y);\n"
        "          ctx.lineTo(closePoint.x, closePoint.y);\n"
        "          ctx.strokeStyle = 'rgba(61,163,255,' + point.active + ')';\n"
        "          ctx.lineWidth = 1;\n"
        "          ctx.stroke();\n"
        "        });\n"
        "      }\n"
        "      function Circle(pos, radius) {\n"
        "        this.pos = pos;\n"
        "        this.radius = radius;\n"
        "        this.active = 0;\n"
        "        this.draw = function() {\n"
        "          if (!this.active) return;\n"
        "          ctx.beginPath();\n"
        "          ctx.arc(this.pos.x, this.pos.y, this.radius, 0, 2 * Math.PI, false);\n"
        "          ctx.fillStyle = 'rgba(61,163,255,' + this.active + ')';\n"
        "          ctx.fill();\n"
        "        };\n"
        "      }\n"
        "      function getDistance(p1, p2) {\n"
        "        return Math.pow(p1.x - p2.x, 2) + Math.pow(p1.y - p2.y, 2);\n"
        "      }\n"
        "      function initialise() {\n"
        "        resizeCanvas();\n"
        "        target = { x: width / 2, y: height / 2 };\n"
        "        buildPoints();\n"
        "        points.forEach(shiftPoint);\n"
        "        animateHeader = window.scrollY <= height;\n"
        "      }\n"
        "      largeHeader.addEventListener('mousemove', event => {\n"
        "        const rect = largeHeader.getBoundingClientRect();\n"
        "        target.x = event.clientX - rect.left;\n"
        "        target.y = event.clientY - rect.top;\n"
        "      });\n"
        "      largeHeader.addEventListener('mouseleave', () => {\n"
        "        target = { x: width / 2, y: height / 2 };\n"
        "      });\n"
        "      largeHeader.addEventListener('touchmove', event => {\n"
        "        const touch = event.touches[0];\n"
        "        if (!touch) return;\n"
        "        const rect = largeHeader.getBoundingClientRect();\n"
        "        target.x = touch.clientX - rect.left;\n"
        "        target.y = touch.clientY - rect.top;\n"
        "      }, { passive: true });\n"
        "      window.addEventListener('scroll', () => {\n"
        "        animateHeader = window.scrollY <= height;\n"
        "      });\n"
        "      window.addEventListener('resize', () => {\n"
        "        initialise();\n"
        "      });\n"
        "      document.addEventListener('visibilitychange', () => {\n"
        "        if (document.visibilityState === 'hidden') {\n"
        "          animateHeader = false;\n"
        "          killTweens();\n"
        "        } else {\n"
        "          animateHeader = true;\n"
        "          initialise();\n"
        "        }\n"
        "      });\n"
        "      initialise();\n"
        "      requestAnimationFrame(animate);\n"
        "    })();\n"
        "    const statusEl = document.getElementById('status');\n"
        "    const terminalEl = document.getElementById('terminal');\n"
        "    const runBtn = document.getElementById('run-btn');\n"
        "    const stopBtn = document.getElementById('stop-btn');\n"
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
        "      stopBtn.disabled = !data.running;\n"
        "      if (!data.running && !data.results_ready) {\n"
        "        resultsBox.hidden = true;\n"
        "        resultsJsonEl.textContent = '';\n"
        "      }\n"
        "    }\n"
        "\n"
        "    stopBtn.addEventListener('click', async () => {\n"
        "      if (!confirm('Stop the running pipeline?')) return;\n"
        "      try {\n"
        "        const response = await fetch('/stop', { method: 'POST' });\n"
        "        if (response.ok) {\n"
        "          appendLine(terminalEl, '\\n--- Pipeline stopped by user ---');\n"
        "        } else {\n"
        "          const payload = await response.json().catch(() => ({}));\n"
        "          alert(payload.detail || 'Failed to stop pipeline');\n"
        "        }\n"
        "      } catch (err) {\n"
        "        alert('Error stopping pipeline: ' + err);\n"
        "      }\n"
        "      await refreshStatus();\n"
        "    });\n"
        "\n"
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


@app.post("/stop")
async def stop_pipeline() -> JSONResponse:
    logger.info("/stop invoked")
    await runner.stop()
    return JSONResponse({"status": "stopped"})


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
