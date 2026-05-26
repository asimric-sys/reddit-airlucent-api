# daily_snapshot.py
import requests
import os
from datetime import date
from dotenv import load_dotenv
load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}", "Content-Type": "application/json"}

def run():
    resp = requests.get(f"{SUPABASE_URL}/rest/v1/products?select=id", headers=headers)
    if resp.status_code != 200:
        print("Failed to fetch products")
        return
    products = resp.json()
    today = date.today().isoformat()
    for prod in products:
        pid = prod["id"]
        pos = requests.get(f"{SUPABASE_URL}/rest/v1/reviews?product_id=eq.{pid}&sentiment=eq.positive&select=id", headers=headers)
        neg = requests.get(f"{SUPABASE_URL}/rest/v1/reviews?product_id=eq.{pid}&sentiment=eq.negative&select=id", headers=headers)
        pos_count = len(pos.json()) if pos.status_code == 200 else 0
        neg_count = len(neg.json()) if neg.status_code == 200 else 0
        total = pos_count + neg_count
        score = pos_count / total if total > 0 else 0
        data = {
            "product_id": pid,
            "date": today,
            "sentiment_score": score,
            "positive_count": pos_count,
            "negative_count": neg_count
        }
        # Upsert (on conflict product_id,date)
        post_resp = requests.post(f"{SUPABASE_URL}/rest/v1/sentiment_history", headers=headers, json=data, params={"on_conflict": "product_id,date"})
        if post_resp.status_code != 201:
            print(f"Failed to upsert for {pid}: {post_resp.text}")
    print("Daily snapshot completed.")

if __name__ == "__main__":
    run()