"""Backfill missing product images using Amazon search.

Targets products WHERE image_url IS NULL.
For each product:
  1. Search Amazon for "{brand} {model}"
  2. Get the ASIN (Amazon product ID)
  3. Scrape the product page to extract the primary product image
  4. Update Supabase with the image_url

Run locally:  python backfill_product_images.py
Run as batch: python backfill_product_images.py --batch 20 --offset 0
"""

import os
import re
import sys
import time
import logging
import argparse

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
DELAY = 2  # seconds between products to avoid Amazon rate-limiting

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def supabase_headers():
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
    }


def get_asin_from_amazon(brand: str, model: str) -> str | None:
    """Search Amazon for brand + model, return the first ASIN found."""
    query = f"{brand} {model}".strip()
    search_url = f"https://www.amazon.com/s?k={requests.utils.quote(query)}"
    try:
        resp = requests.get(search_url, headers=HEADERS, timeout=15)
    except requests.RequestException as e:
        logger.warning("  Amazon search failed: %s", e)
        return None
    if resp.status_code != 200:
        logger.warning("  Amazon search HTTP %s", resp.status_code)
        return None

    soup = BeautifulSoup(resp.text, "html.parser")

    # Strategy 1: data-asin attribute
    el = soup.find(attrs={"data-asin": True})
    if el and el.get("data-asin", "").strip():
        return el["data-asin"].strip()

    # Strategy 2: /dp/{ASIN} in any href
    dp_pattern = re.compile(r"/dp/([A-Z0-9]{10})")
    for tag in soup.find_all("a", href=True):
        m = dp_pattern.search(tag["href"])
        if m:
            return m.group(1)

    logger.warning("  No ASIN found for '%s'", query)
    return None


def fetch_amazon_image(asin: str) -> str | None:
    """Scrape Amazon product page and extract the primary image URL."""
    url = f"https://www.amazon.com/dp/{asin}"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
    except requests.RequestException as e:
        logger.warning("  Product page scrape failed: %s", e)
        return None
    if resp.status_code != 200:
        logger.warning("  Product page HTTP %s", resp.status_code)
        return None

    soup = BeautifulSoup(resp.text, "html.parser")

    # Strategy 1: #landingImage or #imgBlkFront (standard Amazon image elements)
    for img_id in ("landingImage", "imgBlkFront"):
        img = soup.find("img", id=img_id)
        if img and img.get("src"):
            src = img["src"].strip()
            if src.startswith("http"):
                logger.info("  Image via #%s: %s...", img_id, src[:60])
                return src

    # Strategy 2: any img with data-old-hires attribute (high-res)
    img = soup.find("img", attrs={"data-old-hires": True})
    if img and img.get("data-old-hires", "").startswith("http"):
        logger.info("  Image via data-old-hires: %s...", img["data-old-hires"][:60])
        return img["data-old-hires"].strip()

    # Strategy 3: og:image meta tag
    og = soup.find("meta", property="og:image")
    if og and og.get("content", "").startswith("http"):
        logger.info("  Image via og:image: %s...", og["content"][:60])
        return og["content"].strip()

    logger.warning("  No image found on product page")
    return None


def update_image(product_id: str, image_url: str) -> bool:
    """PATCH the product row in Supabase with the image_url."""
    url = f"{SUPABASE_URL}/rest/v1/products?id=eq.{product_id}"
    try:
        resp = requests.patch(url, headers=supabase_headers(), json={"image_url": image_url}, timeout=15)
    except requests.RequestException as e:
        logger.error("  Supabase PATCH failed: %s", e)
        return False
    if resp.status_code in (200, 204):
        return True
    logger.warning("  Supabase returned HTTP %s: %s", resp.status_code, resp.text[:200])
    return False


def main():
    parser = argparse.ArgumentParser(description="Backfill product images from Amazon")
    parser.add_argument("--batch", type=int, default=0, help="Number of products to process (0 = all)")
    parser.add_argument("--offset", type=int, default=0, help="Start offset")
    parser.add_argument("--delay", type=float, default=DELAY, help="Delay between products (seconds)")
    args = parser.parse_args()

    if not SUPABASE_URL or not SUPABASE_KEY:
        logger.error("Missing SUPABASE_URL or SUPABASE_KEY in .env")
        sys.exit(1)

    # Fetch products without images
    logger.info("Fetching products without images...")
    params = {"select": "id,brand,model_name", "image_url": "is.null", "limit": 1000}
    try:
        resp = requests.get(
            f"{SUPABASE_URL}/rest/v1/products",
            headers=supabase_headers(),
            params=params,
            timeout=15,
        )
    except requests.RequestException as e:
        logger.error("Failed to fetch products: %s", e)
        sys.exit(1)

    if resp.status_code != 200:
        logger.error("Failed to fetch products: HTTP %s - %s", resp.status_code, resp.text[:300])
        sys.exit(1)

    all_products = resp.json()
    logger.info("Found %d products without images", len(all_products))

    if not all_products:
        logger.info("Nothing to backfill!")
        return

    # Apply offset
    products = all_products[args.offset:]
    logger.info("Starting from offset %d, %d products remaining", args.offset, len(products))

    # Apply batch limit
    if args.batch > 0:
        products = products[:args.batch]
        logger.info("Batch size: %d", len(products))

    stats = {"success": 0, "no_asin": 0, "no_image": 0, "failed_update": 0}
    for i, prod in enumerate(products):
        pid = prod["id"]
        brand = prod.get("brand", "")
        model = prod.get("model_name", "")
        logger.info("[%d/%d] %s - %s", args.offset + i + 1, len(all_products), brand, model)

        # Step 1: Find ASIN on Amazon
        asin = get_asin_from_amazon(brand, model)
        if not asin:
            stats["no_asin"] += 1
            time.sleep(args.delay)
            continue

        # Step 2: Get image from product page
        image_url = fetch_amazon_image(asin)
        if not image_url:
            stats["no_image"] += 1
            time.sleep(args.delay)
            continue

        # Step 3: Update Supabase
        if update_image(pid, image_url):
            stats["success"] += 1
            logger.info("  ✅ Image updated")
        else:
            stats["failed_update"] += 1

        time.sleep(args.delay)

    logger.info("=" * 50)
    logger.info("Backfill complete")
    logger.info("  ✅ Success:    %d", stats["success"])
    logger.info("  ⚠️ No ASIN:    %d", stats["no_asin"])
    logger.info("  ⚠️ No image:   %d", stats["no_image"])
    logger.info("  ❌ Failed:     %d", stats["failed_update"])
    logger.info("=" * 50)


if __name__ == "__main__":
    main()
