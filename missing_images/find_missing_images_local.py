#!/usr/bin/env python3
"""
Alternative version using Django models if BC data is synced locally
"""

import os
import sys
from typing import Set, List
import cloudinary
import cloudinary.api
from pathlib import Path

# Add the project root to Python path
project_root = Path(__file__).parent.parent.parent
sys.path.append(str(project_root))

# Setup Django
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "middleware.settings")
import django

django.setup()

# Import Django models
from products.models import MagentoProduct  # Adjust import based on your model
from categories.models import MagentoCategory
from categories.magento_api_client import MagentoAPIClient

# reuse the helper in the management command which already knows how to call the REST endpoint
from categories.management.commands.sync_category_items_from_bc import (
    _magento_get_category_products,
)
from django.db import models as djmodels


def get_cloudinary_skus() -> Set[str]:
    """Same as the other script"""
    print("🔍 Fetching SKUs from Cloudinary...")

    cloudinary.config(
        cloud_name=os.getenv("CLOUDINARY_CLOUD_NAME"),
        api_key=os.getenv("CLOUDINARY_API_KEY"),
        api_secret=os.getenv("CLOUDINARY_API_SECRET"),
    )

    skus = set()
    next_cursor = None

    try:
        while True:
            response = cloudinary.api.subfolders(
                "products", next_cursor=next_cursor, max_results=500
            )

            folders = response.get("folders", [])
            for folder in folders:
                folder_name = folder.get("name", "")
                if folder_name:
                    skus.add(folder_name)

            next_cursor = response.get("next_cursor")
            if not next_cursor:
                break

        print(f"✅ Found {len(skus)} SKUs in Cloudinary")
        return skus

    except Exception as e:
        print(f"❌ Error fetching from Cloudinary: {e}")
        return set()


def get_magento_skus() -> Set[str]:
    """
    Get SKUs from Magento configurable products only
    """
    print("🔍 Fetching SKUs from Magento configurable products...")
    # We're interested in all products assigned to the JoiUtherji store view.
    # Default website/store id is 2 (common in this project). Can override with MAGENTO_WEBSITE_ID env var.
    try:
        website_env = os.getenv("MAGENTO_WEBSITE_ID")
        website_id = int(website_env) if website_env else 2
    except Exception:
        website_id = 2

    try:
        # Query for all enabled products that claim the category_id in assigned_category_ids
        qs = (
            MagentoProduct.objects.values_list("sku", flat=True)
            .filter(sku__isnull=False, status=1)
            .exclude(sku="")
        )

        # Avoid DB-specific JSON contains lookups: fetch sku + assigned_category_ids and filter in Python
        try:
            # Diagnostics: how many enabled products
            total_enabled = (
                MagentoProduct.objects.filter(status=1).exclude(sku="").count()
            )
            print(f"ℹ️ Total enabled products (non-empty SKU): {total_enabled}")

            # Build product list and filter by website_ids (Python-side)
            # Determine ROOT_PARENT_ID from env (optional). If provided we will restrict products to
            # those assigned to categories in that subtree. This is done in Python to avoid DB JSON
            # contains lookups which may not be supported by the local DB backend.
            root_parent_id_env = os.getenv("ROOT_PARENT_ID")
            root_parent_id = None
            category_subtree_ids = None
            if root_parent_id_env:
                try:
                    root_parent_id = int(root_parent_id_env)
                except Exception:
                    root_parent_id = None

            # Build a set of category IDs in the subtree (including the root) if ROOT_PARENT_ID is set.
            # Prefer calling Magento API to get a reliable category tree and then collect SKUs per category
            if root_parent_id:
                try:
                    client = MagentoAPIClient()
                    # depth tuned to typical category trees; adjust via env or argument if needed
                    tree = client.get_category_tree(root_parent_id, depth=8)

                    # flatten the tree nodes to extract ids
                    def _flatten(tree_node: dict, out: list):
                        if not isinstance(tree_node, dict):
                            return
                        if "id" in tree_node:
                            out.append(tree_node)
                        for ch in tree_node.get("children_data") or []:
                            _flatten(ch, out)

                    nodes = []
                    _flatten(tree or {}, nodes)
                    cat_ids = {
                        int(n.get("id")) for n in nodes if n.get("id") is not None
                    }
                    # include the root explicitly
                    cat_ids.add(root_parent_id)

                    if cat_ids:
                        print(
                            f"ℹ️ Fetched {len(cat_ids)} categories under ROOT_PARENT_ID={root_parent_id} via Magento API"
                        )
                        # Collect SKUs by querying each category via the helper (which calls the REST endpoint)
                        api_skus = set()
                        for cid in sorted(cat_ids):
                            try:
                                links = _magento_get_category_products(
                                    client, cid, verify=True
                                )
                                for link in links:
                                    sku = link.get("sku")
                                    if sku:
                                        api_skus.add(sku)
                            except Exception as e:
                                print(
                                    f"⚠️ Could not fetch products for category {cid}: {e}"
                                )

                        print(
                            f"ℹ️ Collected {len(api_skus)} SKUs from Magento categories under root {root_parent_id}"
                        )
                        # Now filter these SKUs against our local DB for status, type and website membership
                        if api_skus:
                            qs_local = (
                                MagentoProduct.objects.filter(
                                    sku__in=list(api_skus),
                                    status=1,
                                    type_id="configurable",
                                )
                                .exclude(sku="")
                                .values("sku", "website_ids")
                            )
                            filtered = set()
                            for row in qs_local.iterator():
                                sku = row.get("sku")
                                websites = row.get("website_ids") or []
                                try:
                                    websites_int = set(int(x) for x in websites)
                                except Exception:
                                    websites_int = set(websites)
                                if website_id in websites_int:
                                    filtered.add(sku)

                            print(
                                f"✅ Found {len(filtered)} SKUs in Magento for website/store id {website_id} (after API category filtering)"
                            )
                            sample_skus = list(filtered)[:10]
                            print(f"   Sample SKUs: {', '.join(sample_skus)}")
                            return filtered
                        else:
                            print(
                                f"⚠️ No SKUs returned from Magento API for categories under root {root_parent_id}"
                            )
                    else:
                        print(
                            f"⚠️ ROOT_PARENT_ID={root_parent_id} returned no categories from Magento API"
                        )
                except Exception as e:
                    print(
                        f"⚠️ Error while fetching categories from Magento API for ROOT_PARENT_ID={root_parent_id}: {e}"
                    )

            qs_values = (
                MagentoProduct.objects.filter(
                    sku__isnull=False, status=1, type_id="configurable"
                )
                .exclude(sku="")
                .values("sku", "website_ids", "assigned_category_ids")
            )

            filtered = set()
            for row in qs_values.iterator():
                sku = row.get("sku")
                websites = row.get("website_ids") or []
                assigned_categories = row.get("assigned_category_ids") or []
                try:
                    websites_int = set(int(x) for x in websites)
                except Exception:
                    websites_int = set(websites)

                if website_id not in websites_int:
                    continue

                # If we have a category subtree restriction, ensure product assigned_category_ids intersects it
                if category_subtree_ids:
                    try:
                        assigned_ints = set(int(x) for x in assigned_categories)
                    except Exception:
                        assigned_ints = set(assigned_categories)

                    if not (assigned_ints & category_subtree_ids):
                        # product is not assigned to the requested subtree
                        continue

                filtered.add(sku)

            print(
                f"✅ Found {len(filtered)} SKUs in Magento for website/store id {website_id} [python-filter]"
            )
            sample_skus = list(filtered)[:10]
            print(f"   Sample SKUs: {', '.join(sample_skus)}")
            return filtered
        except Exception as e:
            print(f"❌ Error querying Magento products for category filtering: {e}")
            return set()

    except Exception as e:
        print(f"❌ Error fetching configurable products from Magento: {e}")
        return set()


def main():
    print("🚀 Starting missing images analysis (Configurable Products only)...")
    print("=" * 60)

    # Fetch SKUs from both sources
    cloudinary_skus = get_cloudinary_skus()
    magento_skus = get_magento_skus()

    if not cloudinary_skus or not magento_skus:
        print("❌ Failed to fetch data from one or both sources")
        return

    print("\n📊 Analysis Results:")
    print("-" * 40)
    print(f"Total configurable products in Magento: {len(magento_skus)}")
    print(f"Total SKUs with images in Cloudinary: {len(cloudinary_skus)}")

    # Find missing images
    missing_skus = sorted(list(magento_skus - cloudinary_skus))

    print(f"Configurable products missing images: {len(missing_skus)}")
    if len(magento_skus) > 0:
        coverage = (len(magento_skus) - len(missing_skus)) / len(magento_skus) * 100
        print(f"Coverage: {coverage:.1f}%")

    # Save and display results
    if missing_skus:
        print(f"\n📋 First 20 missing configurable product SKUs:")
        for i, sku in enumerate(missing_skus[:20]):
            print(f"   {i+1}. {sku}")

        if len(missing_skus) > 20:
            print(f"   ... and {len(missing_skus) - 20} more")

        # Save to file
        output_path = Path(__file__).parent / "missing_images_configurable.txt"
        with open(output_path, "w", encoding="utf-8") as f:
            for sku in missing_skus:
                f.write(f"{sku}\n")

        print(f"\n💾 Saved to {output_path}")
        print(
            f"🎯 Perfect! Only {len(missing_skus)} configurable products need images!"
        )
    else:
        print("\n🎉 All configurable products have images in Cloudinary!")


if __name__ == "__main__":
    main()
