import requests
import os
import time
from ddgs import DDGS
from dotenv import load_dotenv

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
headers = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json"
}

def fetch_image_url(brand, model):
    """Search DuckDuckGo for the product and return first image URL."""
    query = f"{brand} {model} product"
    try:
        with DDGS() as ddgs:
            images = list(ddgs.images(query, max_results=1))
            if images and len(images) > 0:
                return images[0].get("image")
    except Exception as e:
        print(f"Image search error for {brand} {model}: {e}")
    return None

def update_image(product_id, image_url):
    url = f"{SUPABASE_URL}/rest/v1/products?id=eq.{product_id}"
    data = {"image_url": image_url}
    resp = requests.patch(url, headers=headers, json=data)
    return resp.status_code in (200, 204)

def main():
    # Get products without image_url
    resp = requests.get(f"{SUPABASE_URL}/rest/v1/products?select=id,brand,model_name&image_url=is.null", headers=headers)
    if resp.status_code != 200:
        print("Failed to fetch products")
        return
    products = resp.json()
    print(f"Found {len(products)} products without images.")
    for prod in products:
        brand = prod["brand"]
        model = prod["model_name"]
        print(f"Processing {brand} {model}...")
        img_url = fetch_image_url(brand, model)
        if img_url:
            if update_image(prod["id"], img_url):
                print(f"  ✅ Image added")
            else:
                print(f"  ❌ Failed to update")
        else:
            print(f"  ⚠️ No image found")
        time.sleep(1)  # polite delay

if __name__ == "__main__":
    main()