"""
One-time backfill: generate list thumbnails for existing inventory images.

Thumbnails are now created at upload time; the inventory list only does a
cheap path check. Run this once after deploying that change so images
uploaded before it also have thumbnails (otherwise the list falls back to
serving the full-size image for those rows).

Usage (from the app root):
    python3 scripts/backfill_thumbnails.py
"""
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.routes.inventory.items import generate_thumbnail  # noqa: E402
from app.services.db import DB_PATH  # noqa: E402


def main():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, image_url, additional_images FROM inventory_items "
        "WHERE image_url IS NOT NULL OR additional_images IS NOT NULL"
    ).fetchall()
    conn.close()

    urls = []
    for r in rows:
        if r['image_url']:
            urls.append(r['image_url'])
        if r['additional_images']:
            try:
                urls.extend(json.loads(r['additional_images']) or [])
            except (json.JSONDecodeError, TypeError):
                pass

    done = skipped = 0
    for url in urls:
        # generate_thumbnail returns None for external URLs / missing files,
        # and returns early (cheap) if a fresh thumbnail already exists.
        if generate_thumbnail(url):
            done += 1
        else:
            skipped += 1

    print(f"Thumbnails ensured: {done}, skipped (external/missing): {skipped}, "
          f"from {len(urls)} image references on {len(rows)} items")


if __name__ == '__main__':
    main()
