import requests
import re
import time
from bs4 import BeautifulSoup
from dotenv import load_dotenv
import os
from ddgs import DDGS

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
headers = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json"
}
AMAZON_TAG = "flawlesscar-20"

def is_generic_product(brand, model):
    """Return True if the product name is too generic to search."""
    generic_terms = ["air purifier", "humidifier", "air conditioner", "portable ac", "filter", "replacement"]
    text = f"{brand} {model}".lower()
    if len(model) < 5:
        return True
    for term in generic_terms:
        if text == term or text.endswith(term) or text.startswith(term):
            return True
    return False

def get_asin_from_duckduckgo(brand, model):
    """Use DuckDuckGo to search for the product on Amazon and extract ASIN."""
    query = f"{brand} {model} amazon"
    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=5))
            for r in results:
                url = r.get("href", "")
                if "amazon.com/dp/" in url:
                    asin = re.search(r"/dp/([A-Z0-9]{10})", url)
                    if asin:
                        return asin.group(1)
    except Exception as e:
        print(f"DDGS error: {e}")
    return None

def update_affiliate_url(product_id, asin):
    affiliate_url = f"https://www.amazon.com/dp/{asin}?tag={AMAZON_TAG}"
    url = f"{SUPABASE_URL}/rest/v1/products?id=eq.{product_id}"
    data = {"affiliate_url": affiliate_url}
    resp = requests.patch(url, headers=headers, json=data)
    return resp.status_code in (200, 204)

def main():
    # Get products without affiliate_url
    url = f"{SUPABASE_URL}/rest/v1/products?select=id,brand,model_name&affiliate_url=is.null"
    resp = requests.get(url, headers=headers)
    if resp.status_code != 200:
        print("Failed to fetch products")
        return
    products = resp.json()
    print(f"Found {len(products)} products without affiliate link.")
    for prod in products:
        brand = prod["brand"]
        model = prod["model_name"]
        if is_generic_product(brand, model):
            print(f"Skipping generic product: {brand} {model}")
            continue
        print(f"Processing {brand} {model}...")
        asin = get_asin_from_duckduckgo(brand, model)
        if asin:
            if update_affiliate_url(prod["id"], asin):
                print(f"  ✅ Affiliate link added (ASIN: {asin})")
            else:
                print(f"  ❌ Failed to update database")
        else:
            print(f"  ⚠️ No ASIN found")
        time.sleep(1)

if __name__ == "__main__":
    main()
