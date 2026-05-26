import requests
import re
import time
from bs4 import BeautifulSoup
from dotenv import load_dotenv
import os

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
headers = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json"
}
AMAZON_TAG = "flawlesscar-20"  # your affiliate tag

def get_asin_from_amazon_search(brand, model):
    """Search Amazon and return the first ASIN."""
    query = f"{brand} {model}".replace(" ", "+")
    url = f"https://www.amazon.com/s?k={query}"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        if resp.status_code != 200:
            return None
        soup = BeautifulSoup(resp.text, "html.parser")
        # Find first product link with /dp/
        link = soup.find("a", href=re.compile(r"/dp/"))
        if link:
            href = link["href"]
            asin = re.search(r"/dp/([A-Z0-9]{10})", href)
            if asin:
                return asin.group(1)
    except Exception as e:
        print(f"Search error for {brand} {model}: {e}")
    return None

def update_affiliate_url(product_id, asin):
    """Update the product's affiliate_url in Supabase."""
    affiliate_url = f"https://www.amazon.com/dp/{asin}?tag={AMAZON_TAG}"
    url = f"{SUPABASE_URL}/rest/v1/products?id=eq.{product_id}"
    data = {"affiliate_url": affiliate_url}
    resp = requests.patch(url, headers=headers, json=data)
    return resp.status_code in (200, 204)

def main():
    # Get all products without an affiliate_url
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
        print(f"Processing {brand} {model}...")
        asin = get_asin_from_amazon_search(brand, model)
        if asin:
            if update_affiliate_url(prod["id"], asin):
                print(f"  ✅ Affiliate link added (ASIN: {asin})")
            else:
                print(f"  ❌ Failed to update database")
        else:
            print(f"  ⚠️ No ASIN found")
        time.sleep(1)  # polite delay

if __name__ == "__main__":
    main()