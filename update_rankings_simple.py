import os
import math
import requests
from dotenv import load_dotenv

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

headers = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json"
}

def delete_all_rankings():
    """Delete all rows from rankings table using a filter that matches all."""
    # Use a filter that always evaluates to true: product_id is not null
    params = {"product_id": "is.not.null"}
    url = f"{SUPABASE_URL}/rest/v1/rankings"
    resp = requests.delete(url, headers=headers, params=params)
    if resp.status_code in (200, 204):
        print(f"[OK] Deleted all existing rankings.")
        return True
    else:
        print(f"[WARN] DELETE returned {resp.status_code}: {resp.text}")
        # Fallback: fetch all existing ranking IDs and delete one by one
        print("Attempting fallback: delete each ranking individually...")
        get_resp = requests.get(url, headers=headers)
        if get_resp.status_code == 200:
            rankings = get_resp.json()
            for rank in rankings:
                pid = rank["product_id"]
                del_resp = requests.delete(url, headers=headers, params={"product_id": f"eq.{pid}"})
                if del_resp.status_code not in (200, 204):
                    print(f"Failed to delete ranking for product {pid}")
            return True
        return False

def update_rankings():
    # First, get all products
    url = f"{SUPABASE_URL}/rest/v1/products?select=id,brand,model_name"
    resp = requests.get(url, headers=headers)
    if resp.status_code != 200:
        print("Failed to fetch products:", resp.text)
        return
    products = resp.json()
    
    if not products:
        print("No products found in database.")
        return
    
    scores = []
    for product in products:
        pid = product["id"]
        # Count positive reviews
        url_pos = f"{SUPABASE_URL}/rest/v1/reviews?product_id=eq.{pid}&sentiment=eq.positive&select=id"
        pos_resp = requests.get(url_pos, headers=headers)
        pos_count = len(pos_resp.json()) if pos_resp.status_code == 200 else 0
        
        # Count negative reviews
        url_neg = f"{SUPABASE_URL}/rest/v1/reviews?product_id=eq.{pid}&sentiment=eq.negative&select=id"
        neg_resp = requests.get(url_neg, headers=headers)
        neg_count = len(neg_resp.json()) if neg_resp.status_code == 200 else 0
        
        total = pos_count + neg_count
        if total == 0:
            continue
        
        ratio = pos_count / total
        pop_weight = math.log(total + 1) / 5.0
        score = (0.8 * ratio) + (0.2 * min(pop_weight, 0.5))
        scores.append({
            "product_id": pid,
            "score": score,
            "pos": pos_count,
            "neg": neg_count,
            "brand": product["brand"],
            "model": product["model_name"]
        })
    
    if not scores:
        print("No products with reviews.")
        return
    
    # Sort by score descending
    scores.sort(key=lambda x: x["score"], reverse=True)
    
    # Delete all existing rankings cleanly
    if not delete_all_rankings():
        print(f"[ERROR] Failed to clear rankings table. Aborting.")
        return
    
    # Insert new rankings
    inserted = 0
    for idx, item in enumerate(scores):
        rank_data = {
            "product_id": item["product_id"],
            "positive_count": item["pos"],
            "negative_count": item["neg"],
            "sentiment_score": item["score"],
            "rank": idx + 1,
            "updated_at": "now()"
        }
        url = f"{SUPABASE_URL}/rest/v1/rankings"
        resp = requests.post(url, headers=headers, json=rank_data)
        if resp.status_code == 201:
            inserted += 1
        else:
            print(f"Failed to insert rank for {item['brand']} {item['model']}: {resp.text}")
    
    print(f"[OK] Inserted {inserted} out of {len(scores)} rankings.")
    print("\n📊 Top 5 Products:")
    for i, item in enumerate(scores[:5]):
        print(f"   Rank {i+1}: {item['brand']} {item['model']} - Score: {item['score']:.2f} ({item['pos']} positive, {item['neg']} negative)")

if __name__ == "__main__":
    update_rankings()