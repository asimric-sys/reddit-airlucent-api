from typing import Optional
from fastapi import FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
import requests
import os
import logging
from dotenv import load_dotenv

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("uvicorn")

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.middleware("http")
async def log_requests(request: Request, call_next):
    logger.info(f"Request: {request.method} {request.url.path}")
    response = await call_next(request)
    return response

def supabase_get(endpoint, params=None):
    url = f"{SUPABASE_URL}/rest/v1/{endpoint}"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}"
    }
    resp = requests.get(url, headers=headers, params=params)
    if resp.status_code != 200:
        logger.warning(f"Supabase error {resp.status_code} for {endpoint}: {resp.text}")
        return []
    return resp.json()

# ---------- Root ----------
@app.get("/")
def root():
    return {"message": "RedditRecs API is running", "endpoints": ["/rankings", "/product/{product_id}", "/search", "/brands", "/categories", "/usecase/{case}"]}

# ---------- Rankings with category filter and pagination ----------
@app.get("/rankings")
def get_rankings(limit: int = 20, offset: int = 0, category: str = None, subreddit: str = None):
    if category:
        product_params = {"category": f"eq.{category}", "select": "id"}
        products_in_cat = supabase_get("products", params=product_params)
        if not products_in_cat:
            return {"rankings": []}
        product_ids = [p["id"] for p in products_in_cat]
        ranking_params = {"product_id": f"in.({','.join(product_ids)})", "order": "rank.asc", "limit": limit, "offset": offset}
    else:
        ranking_params = {"order": "rank.asc", "limit": limit, "offset": offset}
    
    if subreddit:
        reviews = supabase_get("reviews", params={"select": "product_id", "subreddit": f"eq.{subreddit}"})
        if not reviews:
            return {"rankings": []}
        product_ids_sub = list(set([r["product_id"] for r in reviews]))
        if category:
            product_ids = list(set(product_ids) & set(product_ids_sub))
        else:
            product_ids = product_ids_sub
        if not product_ids:
            return {"rankings": []}
        ranking_params["product_id"] = f"in.({','.join(product_ids)})"
    
    rankings = supabase_get("rankings", params=ranking_params)
    if not rankings:
        return {"rankings": []}
    
    product_ids = [r["product_id"] for r in rankings]
    products = supabase_get("products", params={"id": f"in.({','.join(product_ids)})"})
    product_map = {p["id"]: p for p in products}
    for r in rankings:
        r["product"] = product_map.get(r["product_id"], {})
    return {"rankings": rankings, "limit": limit, "offset": offset}

# ---------- Product details (includes aspects) ----------
@app.get("/product/{product_id}")
def product_details(product_id: str):
    product = supabase_get("products", params={"id": f"eq.{product_id}"})
    if not product:
        return {"error": "Product not found"}
    reviews = supabase_get("reviews", params={"product_id": f"eq.{product_id}", "order": "created_at.desc", "limit": 10})
    aspects = supabase_get("product_aspects", params={"product_id": f"eq.{product_id}"})
    return {"product": product[0], "reviews": reviews, "aspects": aspects}

# ---------- Search endpoint ----------
@app.get("/search")
def search_products(q: str = Query(..., min_length=2)):
    params = {
        "or": f"(brand.ilike.*{q}*,model_name.ilike.*{q}*)",
        "select": "id,brand,model_name,category"
    }
    results = supabase_get("products", params=params)
    if results:
        product_ids = [p["id"] for p in results]
        rankings = supabase_get("rankings", params={"product_id": f"in.({','.join(product_ids)})"})
        rank_map = {r["product_id"]: r for r in rankings}
        for p in results:
            p["ranking"] = rank_map.get(p["id"], {})
    return {"query": q, "results": results}

# ---------- Brand stats ----------
@app.get("/brands")
def get_brands(category: Optional[str] = None):
    if category:
        products = supabase_get("products", params={"category": f"eq.{category}", "select": "id,brand"})
        if not products:
            return {"brands": []}
        product_ids = [p["id"] for p in products]
        reviews = supabase_get("reviews", params={"product_id": f"in.({','.join(product_ids)})", "select": "product_id,sentiment"})
    else:
        reviews = supabase_get("reviews", params={"select": "product_id,sentiment"})
        products = supabase_get("products", params={"select": "id,brand"})
    product_brand = {p["id"]: p["brand"] for p in products}
    brand_stats = {}
    for rev in reviews:
        pid = rev["product_id"]
        brand = product_brand.get(pid)
        if not brand:
            continue
        if brand not in brand_stats:
            brand_stats[brand] = {"positive": 0, "negative": 0, "neutral": 0}
        sentiment = rev["sentiment"]
        if sentiment in brand_stats[brand]:
            brand_stats[brand][sentiment] += 1
    result = []
    for brand, stats in brand_stats.items():
        total = stats["positive"] + stats["negative"] + stats["neutral"]
        if total == 0:
            continue
        positive_pct = round((stats["positive"] / total) * 100)
        result.append({
            "brand": brand,
            "positive_percent": positive_pct,
            "positive_count": stats["positive"],
            "negative_count": stats["negative"],
            "neutral_count": stats["neutral"],
            "total_reviews": total
        })
    result.sort(key=lambda x: x["positive_percent"], reverse=True)
    return {"brands": result}

# ---------- List categories ----------
@app.get("/categories")
def get_categories():
    products = supabase_get("products", params={"select": "category"})
    categories = set()
    for p in products:
        if p.get("category"):
            categories.add(p["category"])
    return {"categories": sorted(list(categories))}

# ---------- Use‑case endpoint (keyword matching) ----------
USECASE_KEYWORDS = {
    "smoke": ["smoke", "cigarette", "cannabis", "odor", "smell", "cooking smell", "wildfire"],
    "pets": ["pet", "dog", "cat", "dander", "fur", "hair", "allergy", "shedding"],
    "allergies": ["allergy", "pollen", "dust", "mold", "spore", "hay fever"],
    "quiet": ["quiet", "silent", "noise", "loud", "sleep", "bedroom", "noisy"],
    "large-room": ["large room", "open plan", "living room", "big space", "high ceiling"],
    "small-room": ["small room", "bedroom", "office", "dorm", "compact"],
    "energy-efficiency": ["energy", "power consumption", "electricity", "low watt", "eco"],
    "smart-home": ["smart", "wifi", "app", "alexa", "google home", "automation"]
}
@app.get("/usecase/{case}")
def get_usecase(case: str, limit: int = 10):
    case = case.lower()
    if case not in USECASE_KEYWORDS:
        return {"error": f"Unknown use case. Available: {list(USECASE_KEYWORDS.keys())}"}
    keywords = USECASE_KEYWORDS[case]
    reviews = supabase_get("reviews", params={"select": "product_id,verbatim,sentiment", "limit": 500})
    product_scores = {}
    for rev in reviews:
        verbatim = rev.get("verbatim", "").lower()
        if any(kw in verbatim for kw in keywords):
            pid = rev["product_id"]
            if pid not in product_scores:
                product_scores[pid] = {"pos": 0, "neg": 0}
            if rev["sentiment"] == "positive":
                product_scores[pid]["pos"] += 1
            elif rev["sentiment"] == "negative":
                product_scores[pid]["neg"] += 1
    scored = []
    for pid, counts in product_scores.items():
        total = counts["pos"] + counts["neg"]
        if total == 0:
            continue
        score = counts["pos"] / total if total > 0 else 0
        scored.append({"product_id": pid, "score": score, "pos": counts["pos"], "neg": counts["neg"]})
    scored.sort(key=lambda x: x["score"], reverse=True)
    top_ids = [s["product_id"] for s in scored[:limit]]
    if top_ids:
        products = supabase_get("products", params={"id": f"in.({','.join(top_ids)})"})
        product_map = {p["id"]: p for p in products}
        result = []
        for s in scored[:limit]:
            prod = product_map.get(s["product_id"], {})
            result.append({
                "product": prod,
                "positive_count": s["pos"],
                "negative_count": s["neg"],
                "score": round(s["score"], 2)
            })
        return {"usecase": case, "recommendations": result}
    else:
        return {"usecase": case, "recommendations": []}

# ---------- Debug routes ----------
@app.get("/debug/routes")
def list_routes():
    routes = []
    for route in app.routes:
        routes.append({
            "path": route.path,
            "methods": list(route.methods) if hasattr(route, "methods") else []
        })
    return {"routes": routes}

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
