#!/usr/bin/env python3
"""Flask UI for entering SKUs, running the scraper, and curating images."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List

from flask import Flask, redirect, render_template, request, url_for

FILES_DIR = Path("files")
SKUS_PATH = FILES_DIR / "skus.txt"
IMAGES_PATH = FILES_DIR / "images.json"

app = Flask(__name__)


class ImagesError(RuntimeError):
    """Raised when images.json cannot be loaded."""


def _normalise_sku(value: str) -> str:
    return value.strip().upper()


def parse_skus(text: str) -> List[str]:
    skus: List[str] = []
    seen: set[str] = set()

    for line in text.splitlines():
        cleaned = line.strip()
        if not cleaned:
            continue
        key = _normalise_sku(cleaned)
        if key in seen:
            continue
        seen.add(key)
        skus.append(cleaned)

    return skus


def save_skus(skus: List[str]) -> None:
    FILES_DIR.mkdir(parents=True, exist_ok=True)
    SKUS_PATH.write_text("\n".join(skus) + "\n", encoding="utf-8")


def load_existing_skus() -> List[str]:
    if not SKUS_PATH.exists():
        return []
    return [line.strip() for line in SKUS_PATH.read_text(encoding="utf-8").splitlines() if line.strip()]


def run_pipeline() -> tuple[bool, str]:
    result = subprocess.run([sys.executable, "run_all.py"])
    if result.returncode != 0:
        return False, f"Pipeline failed with exit code {result.returncode}."
    return True, "Scraping pipeline completed successfully."


def load_images() -> List[dict]:
    if not IMAGES_PATH.exists():
        raise ImagesError(f"Expected {IMAGES_PATH} to exist after the pipeline run.")

    try:
        data = json.loads(IMAGES_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ImagesError(f"Could not parse {IMAGES_PATH}: {exc}") from exc

    if not isinstance(data, list):
        raise ImagesError(f"Unexpected format in {IMAGES_PATH}: expected a list of objects.")

    return data


def build_review_entries(data: List[dict], skus: List[str]) -> List[dict]:
    wanted = [_normalise_sku(s) for s in skus]
    wanted_set = set(wanted)
    seen: set[str] = set()
    entries: List[dict] = []

    for entry in data:
        key = _normalise_sku(str(entry.get("sku", "")))
        if key in wanted_set and key not in seen:
            entries.append(
                {
                    "sku": str(entry.get("sku", "")).strip() or key,
                    "key": key,
                    "images": list(entry.get("images") or []),
                    "raw": entry,
                }
            )
            seen.add(key)

    return entries


def backup_images() -> Path:
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    backup_name = f"{IMAGES_PATH.name}.bak.{timestamp}"
    backup_path = IMAGES_PATH.with_name(backup_name)
    shutil.copy2(IMAGES_PATH, backup_path)
    return backup_path


def apply_selections(original: List[dict], selections: Dict[str, dict | None]) -> List[dict]:
    result: List[dict] = []
    for entry in original:
        key = _normalise_sku(str(entry.get("sku", "")))
        if key in selections:
            chosen = selections[key]
            if chosen is None:
                continue
            result.append(chosen)
        else:
            result.append(entry)
    return result


@app.route("/", methods=["GET", "POST"])
def index():
    error: str | None = None
    info: str | None = None

    skus_text = SKUS_PATH.read_text(encoding="utf-8") if SKUS_PATH.exists() else ""

    if request.method == "POST":
        raw = request.form.get("skus", "")
        run_now = request.form.get("run_pipeline") == "on"

        skus = parse_skus(raw)
        if not skus:
            error = "Add at least one SKU before continuing."
        else:
            save_skus(skus)
            info = f"Saved {len(skus)} SKU(s) to {SKUS_PATH}."

            if run_now:
                ok, message = run_pipeline()
                if not ok:
                    error = message
                else:
                    info = message

            if not error:
                return redirect(url_for("review"))

    has_images = IMAGES_PATH.exists()
    return render_template(
        "index.html",
        skus_text=skus_text,
        error=error,
        success=None,
        info=info,
        has_images=has_images,
        SKUS_PATH=SKUS_PATH,
        IMAGES_PATH=IMAGES_PATH,
    )


@app.route("/review", methods=["GET", "POST"])
def review():
    skus = load_existing_skus()
    if not skus:
        return redirect(url_for("index"))

    try:
        data = load_images()
    except ImagesError as exc:
        return render_template(
            "review.html",
            error=str(exc),
            entries=[],
            skus=skus,
            success=None,
            info=None,
            IMAGES_PATH=IMAGES_PATH,
        )

    entries = build_review_entries(data, skus)

    if request.method == "POST":
        selections: Dict[str, dict | None] = {}

        for entry in entries:
            key = entry["key"]
            drop_selected = request.form.get(f"drop[{key}]")
            if drop_selected:
                selections[key] = None
                continue

            if not entry["images"]:
                continue

            raw_values = request.form.getlist(f"selection[{key}]")
            try:
                indexes = sorted({int(value) for value in raw_values})
            except ValueError:
                indexes = []

            if not indexes:
                selections[key] = None
                continue

            chosen_images = [entry["images"][i] for i in indexes if 0 <= i < len(entry["images"]) ]
            if not chosen_images:
                selections[key] = None
                continue

            if chosen_images == entry["images"]:
                continue

            new_entry = dict(entry["raw"])
            new_entry["images"] = chosen_images
            selections[key] = new_entry

        if selections:
            updated = apply_selections(data, selections)
            if updated != data:
                backup_path = backup_images()
                IMAGES_PATH.write_text(json.dumps(updated, indent=2), encoding="utf-8")
                return redirect(url_for("review", saved="1", backup=backup_path.name))

        return redirect(url_for("review", saved="0"))

    saved = request.args.get("saved")
    backup_name = request.args.get("backup")

    success: str | None = None
    info: str | None = None

    if saved == "1":
        success = f"Updated {IMAGES_PATH} with your selections."
        if backup_name:
            success += f" Backup stored as {backup_name}."
    elif saved == "0":
        info = "No changes detected; images.json was left untouched."

    return render_template(
        "review.html",
        entries=entries,
        skus=skus,
        error=None,
        success=success,
        info=info,
        IMAGES_PATH=IMAGES_PATH,
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
