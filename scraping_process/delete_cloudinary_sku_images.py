#!/usr/bin/env python3
import argparse, os, sys, logging, time
from typing import List, Iterable
from dotenv import load_dotenv

import cloudinary
from cloudinary import api
from cloudinary.exceptions import Error as CloudinaryError

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("cloudinary-delete")


# -------- helpers --------
def parse_skus_arg(s: str) -> List[str]:
    return [x.strip() for x in s.replace(",", " ").split() if x.strip()]


def read_skus_file(path: str) -> List[str]:
    with open(path, "r", encoding="utf-8") as f:
        return parse_skus_arg(f.read())


def sku_slug(sku: str) -> str:
    # match the uploader’s normalization (replace _ with - and strip) :contentReference[oaicite:1]{index=1}
    return sku.replace("_", "-").strip()


def list_public_ids_by_prefix(prefix: str) -> List[str]:
    """List all upload resources with a given prefix (paginated)."""
    public_ids: List[str] = []
    next_cursor = None
    while True:
        resp = api.resources(
            type="upload",
            resource_type="image",
            prefix=prefix,
            max_results=500,
            next_cursor=next_cursor,
        )
        public_ids.extend([r["public_id"] for r in resp.get("resources", [])])
        next_cursor = resp.get("next_cursor")
        if not next_cursor:
            break
    return public_ids


def delete_public_ids(public_ids: Iterable[str], dry_run: bool) -> int:
    public_ids = list(public_ids)
    if not public_ids:
        return 0
    if dry_run:
        for pid in public_ids:
            log.info(f"[dry-run] delete: {pid}")
        return len(public_ids)

    deleted_total = 0
    # Cloudinary handles batches up to ~100; we’ll chunk defensively
    CHUNK = 100
    for i in range(0, len(public_ids), CHUNK):
        chunk = public_ids[i : i + CHUNK]
        try:
            resp = api.delete_resources(
                chunk,
                type="upload",
                resource_type="image",
                invalidate=True,
            )
            # Count successes (status 'deleted' or 'not_found' is fine for idempotency)
            for pid, status in resp.get("deleted", {}).items():
                if status in ("deleted", "not_found"):
                    deleted_total += 1
        except CloudinaryError as e:
            log.warning(f"Error deleting batch starting {chunk[0]}: {e}")
            time.sleep(1)
    return deleted_total


def remove_folder(prefix_folder: str, dry_run: bool) -> bool:
    """Remove a (now empty) Cloudinary folder, e.g. products/SKU."""
    if dry_run:
        log.info(f"[dry-run] delete folder: {prefix_folder}")
        return True
    try:
        api.delete_folder(prefix_folder)
        log.info(f"folder removed: {prefix_folder}")
        return True
    except CloudinaryError as e:
        log.info(f"could not remove folder {prefix_folder}: {e}")
        return False


# -------- main --------
def main():
    parser = argparse.ArgumentParser(
        description="Delete Cloudinary images that live under products/<SKU>/"
    )
    parser.add_argument(
        "root_folder",
        nargs="?",
        default="products",
        help="Root folder (default: products)",
    )
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument("--skus", help="SKUs separated by comma/space")
    g.add_argument("--sku-file", help="Path to file containing SKUs")
    parser.add_argument(
        "--yes", action="store_true", help="Actually delete (default is dry-run)"
    )
    parser.add_argument(
        "--remove-folder",
        action="store_true",
        help="Attempt to remove the empty Cloudinary folder per SKU after deletions",
    )
    args = parser.parse_args()

    # Cloudinary config (same as your uploader) :contentReference[oaicite:2]{index=2}
    load_dotenv()
    required = ["CLOUDINARY_CLOUD_NAME", "CLOUDINARY_API_KEY", "CLOUDINARY_API_SECRET"]
    miss = [v for v in required if not os.getenv(v)]
    if miss:
        log.error(f"Missing env vars: {', '.join(miss)}")
        return 1
    cloudinary.config(
        cloud_name=os.getenv("CLOUDINARY_CLOUD_NAME"),
        api_key=os.getenv("CLOUDINARY_API_KEY"),
        api_secret=os.getenv("CLOUDINARY_API_SECRET"),
        secure=True,
    )

    skus = parse_skus_arg(args.skus) if args.skus else read_skus_file(args.sku_file)
    if not skus:
        log.error("No SKUs provided.")
        return 1

    dry_run = not args.yes
    if dry_run:
        log.info("ℹ️  DRY-RUN — no deletions will be made. Add --yes to apply.\n")

    total_found = total_deleted = removed_folders = 0

    for sku in skus:
        slug = sku_slug(sku)
        prefix = f"{args.root_folder}/{slug}/"  # e.g. products/ABC-123/
        log.info(f"SKU {sku} → prefix '{prefix}'")

        try:
            pids = list_public_ids_by_prefix(prefix)
        except CloudinaryError as e:
            log.error(f"  ✖ error listing resources: {e}")
            continue

        total_found += len(pids)
        if not pids:
            log.info("  (no assets found)")
        else:
            log.info(f"  found {len(pids)} asset(s)")
            deleted = delete_public_ids(pids, dry_run)
            total_deleted += deleted
            log.info(f"  deleted {deleted} asset(s){' (dry-run)' if dry_run else ''}")

        if args.remove_folder:
            ok = remove_folder(prefix.rstrip("/"), dry_run)
            if ok:
                removed_folders += 1

        log.info("")

    log.info("✅ Done.")
    log.info(f"   Assets found:   {total_found}")
    log.info(f"   Assets deleted: {total_deleted}{' (dry-run)' if dry_run else ''}")
    if args.remove_folder:
        log.info(
            f"   Folders removed: {removed_folders}{' (dry-run)' if dry_run else ''}"
        )
    if dry_run:
        log.info("   (Nothing was deleted. Re-run with --yes to apply.)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
