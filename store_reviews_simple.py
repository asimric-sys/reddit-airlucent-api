import json
import os
import requests
from dotenv import load_dotenv

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    print("ERROR: SUPABASE_URL and SUPABASE_KEY must be set in .env")
    exit(1)

headers = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json"
}

VALID_SENTIMENTS = {"positive", "negative", "neutral"}


def get_or_create_product(brand, model_name):
    """Return the product id for (brand, model_name), creating the row if needed.

    Returns None when the inputs are invalid or the Supabase call fails.
    """
    if not brand or not model_name or brand == "None" or model_name == "None":
        print(f"   [WARN] Skipping invalid product: brand='{brand}', model='{model_name}'")
        return None

    brand = brand.strip()
    model_name = model_name.strip()

    # Try to find an existing product first
    url = f"{SUPABASE_URL}/rest/v1/products?brand=eq.{brand}&model_name=eq.{model_name}&select=id"
    resp = requests.get(url, headers=headers)
    if resp.status_code == 200 and resp.json():
        return resp.json()[0]["id"]

    # Product not found — create it
    insert_data = {"brand": brand, "model_name": model_name}
    url = f"{SUPABASE_URL}/rest/v1/products"
    resp = requests.post(url, headers=headers, json=insert_data)
    if resp.status_code == 201:
        url = f"{SUPABASE_URL}/rest/v1/products?brand=eq.{brand}&model_name=eq.{model_name}&select=id"
        resp = requests.get(url, headers=headers)
        if resp.status_code == 200 and resp.json():
            return resp.json()[0]["id"]

    print(f"   [ERROR] Failed to create product {brand} {model_name}: {resp.text}")
    return None


def normalise_sentiment(raw):
    """Map any sentiment string to one of positive / negative / neutral."""
    value = (raw or "neutral").lower().strip()
    if value in VALID_SENTIMENTS:
        return value
    # 'mixed' and anything else falls back to neutral
    return "neutral"


def store_reviews_from_json(json_file="reviews_output.json"):
    if not os.path.exists(json_file):
        print(f"File {json_file} not found. Run full_pipeline_groq.py first.")
        return

    with open(json_file, "r", encoding="utf-8") as f:
        reviews = json.load(f)

    if not reviews:
        print("No reviews to store.")
        return

    stored = 0
    skipped_duplicates = 0
    skipped_invalid = 0

    for rev in reviews:
        brand = rev.get("brand") or ""
        model = rev.get("product_name") or ""

        # Validate brand / model
        if not brand or not model or brand == "None" or model == "None":
            print(f"[WARN] Skipping review with missing data: brand='{brand}', model='{model}'")
            skipped_invalid += 1
            continue

        verbatim = rev.get("verbatim") or ""
        if not verbatim:
            print(f"[WARN] Skipping review with empty verbatim: brand='{brand}', model='{model}'")
            skipped_invalid += 1
            continue

        sentiment = normalise_sentiment(rev.get("sentiment"))
        source = rev.get("source_query", "")

        product_id = get_or_create_product(brand, model)
        if not product_id:
            skipped_invalid += 1
            continue

        review_data = {
            "product_id": product_id,
            "reddit_post": verbatim[:500],
            "sentiment": sentiment,
            "verbatim": verbatim,
            "source_query": source,
        }

        url = f"{SUPABASE_URL}/rest/v1/reviews"
        resp = requests.post(url, headers=headers, json=review_data)

        if resp.status_code == 201:
            stored += 1
            print(f"[OK]   Stored:  {brand} {model} — {sentiment}")
        elif resp.status_code == 409:
            # Unique constraint violation: (product_id, verbatim) already exists
            skipped_duplicates += 1
            print(f"[SKIP] Duplicate: {brand} {model} — verbatim already in database")
        else:
            print(f"[ERROR] Failed to store review for {brand} {model} "
                  f"(HTTP {resp.status_code}): {resp.text}")

    total = len(reviews)
    print(
        f"\n[DONE] Stored {stored} new reviews. "
        f"Skipped {skipped_duplicates} duplicates. "
        f"Skipped {skipped_invalid} invalid. "
        f"({total} total processed)"
    )


if __name__ == "__main__":
    store_reviews_from_json()
