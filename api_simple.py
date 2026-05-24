from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
import requests
import os
from dotenv import load_dotenv

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

def supabase_get(endpoint, params=None):
    url = f"{SUPABASE_URL}/rest/v1/{endpoint}"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}"
    }
    resp = requests.get(url, headers=headers, params=params)
    if resp.status_code != 200:
        return []
    return resp.json()

@app.get("/")
def root():
    return {"message": "RedditRecs API is running", "endpoints": ["/rankings", "/product/{product_id}", "/search"]}

@app.get("/rankings")
def get_rankings(limit: int = 20):
    rankings = supabase_get("rankings", params={"order": "rank.asc", "limit": limit})
    if not rankings:
        return {"rankings": []}
    
    product_ids = [r["product_id"] for r in rankings]
    products = supabase_get("products", params={"id": f"in.({','.join(product_ids)})"})
    product_map = {p["id"]: p for p in products}
    
    for r in rankings:
        r["product"] = product_map.get(r["product_id"], {})
    
    return {"rankings": rankings}

@app.get("/product/{product_id}")
def product_details(product_id: str):
    product = supabase_get("products", params={"id": f"eq.{product_id}"})
    if not product:
        return {"error": "Product not found"}
    reviews = supabase_get("reviews", params={"product_id": f"eq.{product_id}", "order": "created_at.desc", "limit": 10})
    return {"product": product[0], "reviews": reviews}

@app.get("/search")
def search_products(q: str = Query(..., min_length=2)):
    url = f"{SUPABASE_URL}/rest/v1/products"
    params = {
        "or": f"(brand.ilike.*{q}*,model_name.ilike.*{q}*)",
        "select": "id,brand,model_name"
    }
    results = supabase_get("products", params=params)
    
    if results:
        product_ids = [p["id"] for p in results]
        rankings = supabase_get("rankings", params={"product_id": f"in.({','.join(product_ids)})"})
        rank_map = {r["product_id"]: r for r in rankings}
        for p in results:
            p["ranking"] = rank_map.get(p["id"], {})
    
    return {"query": q, "results": results}

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
