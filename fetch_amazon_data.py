"""Enrich Supabase products with Amazon data (price, image, rating, reviews).

Queries the products table for rows where amazon_price IS NULL, searches
Amazon for each product, scrapes the product page with BeautifulSoup, and
writes the results back to Supabase.

Environment variables
---------------------
    SUPABASE_URL          — Supabase project URL
    SUPABASE_KEY          — Supabase service-role or anon API key
    AMAZON_ASSOCIATE_TAG  — Amazon Associates partner tag (default: flawlesscar-20)

Usage
-----
    python fetch_amazon_data.py

The script is idempotent: it only processes products where amazon_price IS NULL,
so it is safe to run repeatedly or on a schedule.
"""

import os
import re
import time
import logging

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
AMAZON_ASSOCIATE_TAG = os.getenv("AMAZON_ASSOCIATE_TAG", "flawlesscar-20")

DELAY_BETWEEN_PRODUCTS = 2  # seconds — avoids Amazon rate-limiting

# Rotate a realistic browser User-Agent so requests are less likely to be
# blocked by Amazon's bot-detection layer.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Supabase helpers
# ---------------------------------------------------------------------------

def _supabase_headers() -> dict:
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
    }


def fetch_products_without_amazon_data() -> list[dict]:
    """Return all products where amazon_price IS NULL."""
    url = (
        f"{SUPABASE_URL}/rest/v1/products"
        "?amazon_price=is.null"
        "&select=id,brand,model_name"
    )
    try:
        resp = requests.get(url, headers=_supabase_headers(), timeout=15)
    except requests.RequestException as exc:
        logger.error("Failed to fetch products from Supabase: %s", exc)
        return []

    if resp.status_code != 200:
        logger.error(
            "Supabase returned HTTP %s when fetching products: %s",
            resp.status_code,
            resp.text[:300],
        )
        return []

    products = resp.json()
    logger.info("Found %d product(s) missing Amazon data.", len(products))
    return products


def update_product(product_id: str, data: dict) -> bool:
    """PATCH a product row in Supabase with the supplied Amazon data fields.

    Parameters
    ----------
    product_id:
        The UUID of the product row to update.
    data:
        Dict of column → value pairs to write (e.g. amazon_price, image_url).

    Returns
    -------
    bool
        True on success, False on any error.
    """
    url = f"{SUPABASE_URL}/rest/v1/products?id=eq.{product_id}"
    try:
        resp = requests.patch(url, headers=_supabase_headers(), json=data, timeout=15)
    except requests.RequestException as exc:
        logger.error("Failed to PATCH product %s: %s", product_id, exc)
        return False

    if resp.status_code in (200, 204):
        return True

    logger.warning(
        "Supabase PATCH returned HTTP %s for product %s: %s",
        resp.status_code,
        product_id,
        resp.text[:300],
    )
    return False

# ---------------------------------------------------------------------------
# Amazon scraping helpers
# ---------------------------------------------------------------------------

def get_asin_from_search(brand: str, model: str) -> str | None:
    """Search Amazon for '{brand} {model}' and return the ASIN of the first result.

    Tries two extraction strategies in order:
    1. ``data-asin`` attribute on a search-result container element.
    2. ``/dp/{ASIN}`` pattern in any anchor href on the page.

    Returns the ASIN string (e.g. ``"B07RFSSQ7L"``) or ``None`` if not found.
    """
    query = f"{brand} {model}".strip()
    search_url = f"https://www.amazon.com/s?k={requests.utils.quote(query)}"

    logger.info("  Searching Amazon: %s", search_url)
    try:
        resp = requests.get(search_url, headers=HEADERS, timeout=15)
    except requests.RequestException as exc:
        logger.warning("  Amazon search request failed: %s", exc)
        return None

    if resp.status_code != 200:
        logger.warning("  Amazon search returned HTTP %s", resp.status_code)
        return None

    soup = BeautifulSoup(resp.text, "html.parser")

    # Strategy 1: data-asin attribute on result containers
    result = soup.find(attrs={"data-asin": True})
    if result:
        asin = result["data-asin"].strip()
        if asin:
            logger.info("  Found ASIN via data-asin: %s", asin)
            return asin

    # Strategy 2: /dp/{ASIN} pattern in any link href
    dp_pattern = re.compile(r"/dp/([A-Z0-9]{10})")
    for tag in soup.find_all("a", href=True):
        match = dp_pattern.search(tag["href"])
        if match:
            asin = match.group(1)
            logger.info("  Found ASIN via /dp/ link: %s", asin)
            return asin

    logger.warning("  Could not extract ASIN for '%s'", query)
    return None


def fetch_amazon_product(asin: str) -> dict:
    """Scrape the Amazon product page for the given ASIN.

    Extracts:
    - ``amazon_price``        — float, e.g. 149.99
    - ``image_url``           — str, primary product image URL
    - ``amazon_rating``       — float, e.g. 4.5
    - ``amazon_review_count`` — int, e.g. 3821
    - ``affiliate_url``       — str, Associates-tagged product URL

    Returns a dict with only the keys that were successfully extracted.
    Missing fields are omitted so that a partial update does not overwrite
    existing data with None.
    """
    product_url = f"https://www.amazon.com/dp/{asin}"
    affiliate_url = f"https://www.amazon.com/dp/{asin}?tag={AMAZON_ASSOCIATE_TAG}"

    logger.info("  Fetching product page: %s", product_url)
    try:
        resp = requests.get(product_url, headers=HEADERS, timeout=15)
    except requests.RequestException as exc:
        logger.warning("  Amazon product request failed: %s", exc)
        return {}

    if resp.status_code != 200:
        logger.warning("  Amazon product page returned HTTP %s", resp.status_code)
        return {}

    soup = BeautifulSoup(resp.text, "html.parser")
    data: dict = {"affiliate_url": affiliate_url}

    # --- Price ---
    # Amazon renders the price as two separate spans: whole and fraction.
    price_whole = soup.select_one(".a-price-whole")
    price_fraction = soup.select_one(".a-price-fraction")
    if price_whole:
        try:
            whole_text = price_whole.get_text(strip=True).replace(",", "").rstrip(".")
            fraction_text = price_fraction.get_text(strip=True) if price_fraction else "00"
            data["amazon_price"] = float(f"{whole_text}.{fraction_text}")
        except (ValueError, AttributeError):
            logger.debug("  Could not parse price for ASIN %s", asin)

    # --- Primary image ---
    img_tag = soup.select_one("#imgTagWrapperId img")
    if img_tag:
        # Prefer the high-res src; fall back to data-old-hires or src
        image_src = (
            img_tag.get("data-old-hires")
            or img_tag.get("src")
        )
        if image_src and image_src.startswith("http"):
            data["image_url"] = image_src

    # --- Customer rating ---
    # The rating widget contains text like "4.5 out of 5 stars"
    rating_tag = soup.select_one(".a-icon-alt")
    if rating_tag:
        rating_text = rating_tag.get_text(strip=True)
        rating_match = re.search(r"([\d.]+)\s+out\s+of", rating_text)
        if rating_match:
            try:
                data["amazon_rating"] = float(rating_match.group(1))
            except ValueError:
                logger.debug("  Could not parse rating for ASIN %s", asin)

    # --- Review count ---
    review_tag = soup.select_one("#acrCustomerReviewText")
    if review_tag:
        review_text = review_tag.get_text(strip=True)
        count_match = re.search(r"([\d,]+)", review_text)
        if count_match:
            try:
                data["amazon_review_count"] = int(count_match.group(1).replace(",", ""))
            except ValueError:
                logger.debug("  Could not parse review count for ASIN %s", asin)

    return data

# ---------------------------------------------------------------------------
# Main orchestration
# ---------------------------------------------------------------------------

def main() -> None:
    """Fetch products missing Amazon data, scrape Amazon, and update Supabase."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        logger.error("SUPABASE_URL and SUPABASE_KEY must be set. Aborting.")
        return

    products = fetch_products_without_amazon_data()
    if not products:
        logger.info("No products to enrich. Exiting.")
        return

    enriched = 0
    skipped = 0

    for product in products:
        product_id = product["id"]
        brand = product.get("brand", "").strip()
        model = product.get("model_name", "").strip()

        logger.info("[%s] Processing: %s %s", product_id, brand, model)

        if not brand or not model:
            logger.warning("  Skipping — missing brand or model name.")
            skipped += 1
            continue

        # Step 1: find the ASIN via Amazon search
        asin = get_asin_from_search(brand, model)
        if not asin:
            logger.warning("  Skipping — could not find ASIN for '%s %s'.", brand, model)
            skipped += 1
            time.sleep(DELAY_BETWEEN_PRODUCTS)
            continue

        # Step 2: scrape the product page
        amazon_data = fetch_amazon_product(asin)
        if not amazon_data:
            logger.warning("  Skipping — could not scrape product page for ASIN %s.", asin)
            skipped += 1
            time.sleep(DELAY_BETWEEN_PRODUCTS)
            continue

        # Always store the ASIN so we can skip this product on future runs
        # even if the price scrape partially failed.
        amazon_data["asin"] = asin

        # Step 3: update Supabase
        fields_found = [k for k in amazon_data if k != "asin"]
        logger.info(
            "  Updating product with: %s",
            ", ".join(f"{k}={v}" for k, v in amazon_data.items()),
        )
        success = update_product(product_id, amazon_data)
        if success:
            enriched += 1
            logger.info(
                "  [OK] Updated %s %s — %d field(s) written.",
                brand,
                model,
                len(fields_found),
            )
        else:
            skipped += 1
            logger.error("  [ERROR] Failed to update %s %s in Supabase.", brand, model)

        # Polite delay between requests
        time.sleep(DELAY_BETWEEN_PRODUCTS)

    logger.info(
        "\n[DONE] Enriched %d product(s). Skipped %d. (%d total processed)",
        enriched,
        skipped,
        len(products),
    )


if __name__ == "__main__":
    main()
