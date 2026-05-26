from typing import Optional, List
from fastapi import FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
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

def supabase_post(endpoint, payload):
    url = f"{SUPABASE_URL}/rest/v1/{endpoint}"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation"
    }
    resp = requests.post(url, headers=headers, json=payload)
    if resp.status_code not in (200, 201):
        logger.warning(f"Supabase POST error {resp.status_code} for {endpoint}: {resp.text}")
        return None
    return resp.json()

# ---------- Root ----------
@app.get("/")
def root():
    return {
        "message": "RedditRecs API is running",
        "endpoints": [
            "/rankings",
            "/product/{product_id}",
            "/compare",
            "/trend/{product_id}",
            "/search",
            "/brands",
            "/categories",
            "/usecase/{case}",
            "/user_review"
        ]
    }

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

# ---------- Product details (includes aspects + Reddit percentile) ----------
@app.get("/product/{product_id}")
def product_details(product_id: str):
    product = supabase_get("products", params={"id": f"eq.{product_id}"})
    if not product:
        return {"error": "Product not found"}
    reviews = supabase_get("reviews", params={"product_id": f"eq.{product_id}", "order": "created_at.desc", "limit": 10})
    aspects = supabase_get("product_aspects", params={"product_id": f"eq.{product_id}"})

    # Reddit percentile: rank this product's sentiment_score against all others
    all_rankings = supabase_get("rankings", params={"select": "product_id,sentiment_score", "order": "sentiment_score.asc"})
    reddit_percentile = None
    if all_rankings:
        scores = [r.get("sentiment_score") for r in all_rankings if r.get("sentiment_score") is not None]
        this_ranking = supabase_get("rankings", params={"product_id": f"eq.{product_id}", "select": "sentiment_score"})
        if this_ranking and scores:
            this_score = this_ranking[0].get("sentiment_score")
            if this_score is not None:
                below = sum(1 for s in scores if s < this_score)
                reddit_percentile = round((below / len(scores)) * 100)

    return {
        "product": product[0],
        "reviews": reviews,
        "aspects": aspects,
        "reddit_percentile": reddit_percentile
    }

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

# ---------- Compare endpoint (2–3 products side-by-side) ----------
@app.get("/compare")
def compare_products(ids: str = Query(..., description="Comma-separated product IDs (2–3)")):
    product_ids = [pid.strip() for pid in ids.split(",") if pid.strip()]
    if len(product_ids) < 2 or len(product_ids) > 3:
        return {"error": "Please provide 2 or 3 comma-separated product IDs"}

    products = supabase_get("products", params={"id": f"in.({','.join(product_ids)})"})
    if not products:
        return {"error": "No products found for the given IDs"}
    product_map = {p["id"]: p for p in products}

    rankings = supabase_get("rankings", params={"product_id": f"in.({','.join(product_ids)})"})
    ranking_map = {r["product_id"]: r for r in rankings}

    aspects = supabase_get("product_aspects", params={"product_id": f"in.({','.join(product_ids)})"})
    aspects_map: dict = {}
    for a in aspects:
        pid = a["product_id"]
        aspects_map.setdefault(pid, []).append(a)

    reviews = supabase_get("reviews", params={
        "product_id": f"in.({','.join(product_ids)})",
        "select": "product_id,sentiment,verbatim,subreddit",
        "limit": 200
    })
    subreddits_map: dict = {}
    pros_map: dict = {}
    cons_map: dict = {}
    for rev in reviews:
        pid = rev["product_id"]
        if rev.get("subreddit"):
            subreddits_map.setdefault(pid, set()).add(rev["subreddit"])
        verbatim = rev.get("verbatim", "")
        if rev.get("sentiment") == "positive" and verbatim:
            pros_map.setdefault(pid, []).append(verbatim[:120])
        elif rev.get("sentiment") == "negative" and verbatim:
            cons_map.setdefault(pid, []).append(verbatim[:120])

    comparison = []
    for pid in product_ids:
        prod = product_map.get(pid)
        if not prod:
            comparison.append({"product_id": pid, "error": "Not found"})
            continue
        rank = ranking_map.get(pid, {})
        comparison.append({
            "product_id": pid,
            "brand": prod.get("brand"),
            "model_name": prod.get("model_name"),
            "category": prod.get("category"),
            "sentiment_score": rank.get("sentiment_score"),
            "rank": rank.get("rank"),
            "reddit_percentile": None,  # populated below
            "pros": pros_map.get(pid, [])[:3],
            "cons": cons_map.get(pid, [])[:3],
            "subreddits": sorted(subreddits_map.get(pid, set())),
            "aspects": aspects_map.get(pid, [])
        })

    # Compute reddit_percentile for each compared product
    all_rankings = supabase_get("rankings", params={"select": "sentiment_score", "order": "sentiment_score.asc"})
    all_scores = [r.get("sentiment_score") for r in all_rankings if r.get("sentiment_score") is not None]
    if all_scores:
        for item in comparison:
            score = item.get("sentiment_score")
            if score is not None:
                below = sum(1 for s in all_scores if s < score)
                item["reddit_percentile"] = round((below / len(all_scores)) * 100)

    return {"comparison": comparison}


# ---------- Sentiment trend for a product ----------
@app.get("/trend/{product_id}")
def sentiment_trend(product_id: str):
    product = supabase_get("products", params={"id": f"eq.{product_id}", "select": "id,brand,model_name"})
    if not product:
        return {"error": "Product not found"}

    history = supabase_get("sentiment_history", params={
        "product_id": f"eq.{product_id}",
        "order": "recorded_at.asc",
        "select": "recorded_at,sentiment_score,positive_count,negative_count,neutral_count"
    })

    return {
        "product_id": product_id,
        "product": product[0],
        "trend": history
    }


# ---------- UserReview model + POST endpoint ----------
class UserReview(BaseModel):
    product_id: str
    reviewer_name: Optional[str] = None
    rating: Optional[int] = None          # 1–5
    verbatim: str
    sentiment: Optional[str] = None       # "positive" | "negative" | "neutral"
    source: Optional[str] = "user"


@app.post("/user_review")
def submit_user_review(review: UserReview):
    valid_sentiments = {"positive", "negative", "neutral", None}
    if review.sentiment not in valid_sentiments:
        return {"error": f"Invalid sentiment '{review.sentiment}'. Must be one of: positive, negative, neutral"}

    if review.rating is not None and not (1 <= review.rating <= 5):
        return {"error": "Rating must be between 1 and 5"}

    product = supabase_get("products", params={"id": f"eq.{review.product_id}", "select": "id"})
    if not product:
        return {"error": f"Product '{review.product_id}' not found"}

    payload = {
        "product_id": review.product_id,
        "reviewer_name": review.reviewer_name,
        "rating": review.rating,
        "verbatim": review.verbatim,
        "sentiment": review.sentiment,
        "source": review.source or "user",
        "verified": False
    }
    result = supabase_post("user_reviews", payload)
    if result is None:
        return {"error": "Failed to save review. Please try again later."}
    return {"success": True, "review": result[0] if isinstance(result, list) else result}


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
