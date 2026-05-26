import requests
import os
import time
import sys
from ddgs import DDGS
from dotenv import load_dotenv

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    print("ERROR: Missing SUPABASE_URL or SUPABASE_KEY")
    sys.exit(1)

headers = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json"
}

def fetch_image_url(brand, model):
    """Search DuckDuckGo for product image, return URL or None."""
    query = f"{brand} {model} product"
    print(f"  Searching DDG for: {query}")
    try:
        with DDGS() as ddgs:
            images = list(ddgs.images(query, max_results=1))
            if images and len(images) > 0:
                img_url = images[0].get("image")
                if img_url:
                    print(f"  Found image: {img_url[:80]}...")
                    return img_url
            print("  No image found via DDG")
    except Exception as e:
        print(f"  DDG error: {e}")
    return None

def update_image(product_id, image_url):
    url = f"{SUPABASE_URL}/rest/v1/products?id=eq.{product_id}"
    data = {"image_url": image_url}
    resp = requests.patch(url, headers=headers, json=data)
    if resp.status_code in (200, 204):
        print(f"  ✅ Updated product {product_id}")
        return True
    else:
        print(f"  ❌ Failed to update: {resp.status_code} - {resp.text}")
        return False

def main():
    print("Fetching products without image_url...")
    resp = requests.get(f"{SUPABASE_URL}/rest/v1/products?select=id,brand,model_name&image_url=is.null", headers=headers)
    if resp.status_code != 200:
        print(f"Failed to fetch products: {resp.status_code} - {resp.text}")
        return
    products = resp.json()
    print(f"Found {len(products)} products without images.")
    if not products:
        print("Nothing to update.")
        return

    for prod in products:
        brand = prod["brand"]
        model = prod["model_name"]
        print(f"\nProcessing: {brand} - {model}")
        img_url = fetch_image_url(brand, model)
        if img_url:
            update_image(prod["id"], img_url)
        else:
            print("  No image found, leaving as NULL")
        time.sleep(1)  # polite delay

if __name__ == "__main__":
    main()
