from fastapi import FastAPI, Query, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import requests
import os
import logging
from collections import defaultdict
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

# Configure logging (so we see requests in Railway logs)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("uvicorn")

app = FastAPI(
    title="RedditRecs API",
    description="Reddit-powered air purifier rankings, brand stats, and use-case recommendations",
    version="2.0.0",
)

# ---------------------------------------------------------------------------
# CORS middleware
# ---------------------------------------------------------------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Request logging middleware
# ---------------------------------------------------------------------------
@app.middleware("http")
async def log_requests(request: Request, call_next):
    logger.info(f"-> {request.method} {request.url.path}  params={dict(request.query_params)}")
    response = await call_next(request)
    logger.info(f"<- {response.status_code} {request.url.path}")
    return response

# ---------------------------------------------------------------------------
# Use-case keyword map
# ---------------------------------------------------------------------------
USECASE_KEYWORDS = {
    "smoke":             ["smoke", "cigarette", "cannabis", "odor", "smell", "cooking smell", "wildfire"],
    "pets":              ["pet", "dog", "cat", "dander", "fur", "hair", "allergy", "shedding"],
    "allergies":         ["allergy", "pollen", "dust", "mold", "spore", "hay fever"],
    "quiet":             ["quiet", "silent", "noise", "loud", "sleep", "bedroom", "noisy"],
    "large-room":        ["large room", "open plan", "living room", "big space", "high ceiling"],
    "small-room":        ["small room", "bedroom", "office", "dorm", "compact"],
    "energy-efficiency": ["energy", "power consumption", "electricity", "low watt", "eco"],
    "smart-home":        ["smart", "wifi", "app", "alexa", "google home", "automation"],
}

# ---------------------------------------------------------------------------
# Supabase helpers
# ---------------------------------------------------------------------------
def _supabase_headers():
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
    }

def supabase_get(endpoint, params=None):
    """GET from a Supabase REST endpoint. Returns a list (empty on error)."""
    url = f"{SUPABASE_URL}/rest/v1/{endpoint}"
    try:
        resp = requests.get(url, headers=_supabase_headers(), params=params, timeout=10)
    except requests.RequestException as exc:
        logger.error(f"Supabase request failed for {endpoint}: {exc}")
        return []
    if resp.status_code != 200:
        logger.warning(f"Supabase {resp.status_code} for {endpoint}: {resp.text[:200]}")
        return []
    return resp.json()

def supabase_post(endpoint, payload):
    """POST to a Supabase REST endpoint. Returns the response dict or None on error."""
    url = f"{SUPABASE_URL}/rest/v1/{endpoint}"
    headers = {**_supabase_headers(), "Content-Type": "application/json", "Prefer": "return=representation"}
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=10)
    except requests.RequestException as exc:
        logger.error(f"Supabase POST failed for {endpoint}: {exc}")
        return None
    if resp.status_code not in (200, 201):
        logger.warning(f"Supabase POST {resp.status_code} for {endpoint}: {resp.text[:200]}")
        return None
    return resp.json()

# ---------------------------------------------------------------------------
# Root / health
# ---------------------------------------------------------------------------
@app.get("/")
def root():
    return {
        "message": "RedditRecs API is running",
        "version": "2.0.0",
        "endpoints": [
            "/rankings",
            "/rankings?category=air-purifier",
            "/brands",
            "/brands?category=air-purifier",
            "/categories",
            "/usecase/{case}",
            "/product/{product_id}",
            "/search",
            "/debug/routes",
        ],
    }

# ---------------------------------------------------------------------------
# Debug: list all registered routes
# ---------------------------------------------------------------------------
@app.get("/debug/routes")
def list_routes():
    routes = [
        {
            "path": route.path,
            "methods": sorted(route.methods) if hasattr(route, "methods") and route.methods else [],
        }
        for route in app.routes
    ]
    return {"routes": routes}

# ---------------------------------------------------------------------------
# /rankings  -- optionally filtered by category
# ---------------------------------------------------------------------------
@app.get("/rankings")
def get_rankings(
    limit: int = Query(20, ge=1, le=100),
    category: str = Query(None, description="Filter by product category slug"),
):
    """Return ranked products, optionally filtered by category."""
    ranking_params = {"order": "rank.asc", "limit": limit}

    if category:
        # Fetch products in the requested category first, then filter rankings
        products_in_cat = supabase_get(
            "products",
            params={"category": f"eq.{category}", "select": "id,brand,model_name,category"},
        )
        if not products_in_cat:
            logger.info(f"/rankings: no products found for category='{category}'")
            return {"rankings": [], "category": category}

        cat_ids = [p["id"] for p in products_in_cat]
        ranking_params["product_id"] = f"in.({','.join(cat_ids)})"
        product_map = {p["id"]: p for p in products_in_cat}
    else:
        product_map = None  # will be populated below

    rankings = supabase_get("rankings", params=ranking_params)
    if not rankings:
        return {"rankings": [], "category": category}

    if product_map is None:
        # No category filter -- fetch products for the returned ranking rows
        product_ids = [r["product_id"] for r in rankings]
        products = supabase_get("products", params={"id": f"in.({','.join(product_ids)})"})
        product_map = {p["id"]: p for p in products}

    for r in rankings:
        r["product"] = product_map.get(r["product_id"], {})

    return {"rankings": rankings, "category": category}

# ---------------------------------------------------------------------------
# /brands  -- aggregate sentiment statistics per brand
# ---------------------------------------------------------------------------
@app.get("/brands")
def get_brands(
    category: str = Query(None, description="Filter by product category slug"),
):
    """Return per-brand sentiment statistics aggregated from reviews."""
    product_params = {"select": "id,brand,model_name,category"}
    if category:
        product_params["category"] = f"eq.{category}"

    products = supabase_get("products", params=product_params)
    if not products:
        return {"brands": [], "category": category}

    product_ids = [p["id"] for p in products]
    product_map = {p["id"]: p for p in products}

    # Fetch all reviews for those products
    reviews = supabase_get(
        "reviews",
        params={
            "product_id": f"in.({','.join(product_ids)})",
            "select": "product_id,sentiment,score",
        },
    )

    # Aggregate per brand
    brand_stats = defaultdict(lambda: {
        "brand": "",
        "total_reviews": 0,
        "positive": 0,
        "negative": 0,
        "neutral": 0,
        "avg_score": 0.0,
        "_score_sum": 0.0,
    })

    for review in reviews:
        pid = review.get("product_id")
        product = product_map.get(pid, {})
        brand = product.get("brand", "Unknown")

        stats = brand_stats[brand]
        stats["brand"] = brand
        stats["total_reviews"] += 1

        sentiment = (review.get("sentiment") or "").lower()
        if sentiment == "positive":
            stats["positive"] += 1
        elif sentiment == "negative":
            stats["negative"] += 1
        else:
            stats["neutral"] += 1

        score = review.get("score")
        if score is not None:
            try:
                stats["_score_sum"] += float(score)
            except (TypeError, ValueError):
                pass

    # Compute derived fields and clean up internal keys
    result = []
    for brand, stats in brand_stats.items():
        total = stats["total_reviews"]
        stats["avg_score"] = round(stats["_score_sum"] / total, 2) if total else 0.0
        stats["positive_pct"] = round(stats["positive"] / total * 100, 1) if total else 0.0
        del stats["_score_sum"]
        result.append(stats)

    result.sort(key=lambda x: x["positive_pct"], reverse=True)
    return {"brands": result, "category": category}

# ---------------------------------------------------------------------------
# /categories  -- list all distinct categories
# ---------------------------------------------------------------------------
@app.get("/categories")
def get_categories():
    """Return all distinct product categories present in the database."""
    products = supabase_get("products", params={"select": "category"})
    categories = sorted({p["category"] for p in products if p.get("category")})
    return {"categories": categories, "count": len(categories)}

# ---------------------------------------------------------------------------
# /usecase/{case}  -- top products for a specific use case
# ---------------------------------------------------------------------------
@app.get("/usecase/{case}")
def get_usecase_recommendations(
    case: str,
    limit: int = Query(10, ge=1, le=50),
    category: str = Query(None, description="Optionally restrict to a product category"),
):
    """
    Return top products for a given use case based on review sentiment.

    Supported cases: smoke, pets, allergies, quiet, large-room, small-room,
    energy-efficiency, smart-home.
    """
    case = case.lower()
    keywords = USECASE_KEYWORDS.get(case)
    if keywords is None:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown use case '{case}'. Valid options: {sorted(USECASE_KEYWORDS.keys())}",
        )

    product_params = {"select": "id,brand,model_name,category"}
    if category:
        product_params["category"] = f"eq.{category}"

    products = supabase_get("products", params=product_params)
    if not products:
        return {"use_case": case, "keywords": keywords, "recommendations": []}

    product_ids = [p["id"] for p in products]
    product_map = {p["id"]: p for p in products}

    # Fetch reviews that mention any of the use-case keywords via Supabase ilike OR filter
    ilike_clauses = ",".join(f"body.ilike.*{kw}*" for kw in keywords)
    reviews = supabase_get(
        "reviews",
        params={
            "product_id": f"in.({','.join(product_ids)})",
            "or": f"({ilike_clauses})",
            "select": "product_id,sentiment,score",
        },
    )

    if not reviews:
        logger.info(f"/usecase/{case}: no matching reviews found")
        return {"use_case": case, "keywords": keywords, "recommendations": []}

    # Score each product: +1 positive, -1 negative, 0 neutral
    scores = defaultdict(lambda: {
        "product_id": "",
        "mention_count": 0,
        "positive": 0,
        "negative": 0,
        "neutral": 0,
        "score": 0,
    })

    for review in reviews:
        pid = review.get("product_id")
        if not pid:
            continue
        entry = scores[pid]
        entry["product_id"] = pid
        entry["mention_count"] += 1

        sentiment = (review.get("sentiment") or "").lower()
        if sentiment == "positive":
            entry["positive"] += 1
            entry["score"] += 1
        elif sentiment == "negative":
            entry["negative"] += 1
            entry["score"] -= 1
        else:
            entry["neutral"] += 1

    # Sort by score descending, then by mention count
    ranked = sorted(scores.values(), key=lambda x: (x["score"], x["mention_count"]), reverse=True)

    # Attach product details and return top N
    recommendations = []
    for entry in ranked[:limit]:
        pid = entry["product_id"]
        product = product_map.get(pid, {})
        recommendations.append({
            "product": product,
            "use_case_score": entry["score"],
            "mention_count": entry["mention_count"],
            "positive_mentions": entry["positive"],
            "negative_mentions": entry["negative"],
            "neutral_mentions": entry["neutral"],
        })

    return {
        "use_case": case,
        "keywords": keywords,
        "category": category,
        "recommendations": recommendations,
    }

# ---------------------------------------------------------------------------
# /product/{product_id}  -- full product details with recent reviews
# ---------------------------------------------------------------------------
@app.get("/product/{product_id}")
def product_details(product_id: str):
    """Return product details and its 10 most recent reviews."""
    product = supabase_get("products", params={"id": f"eq.{product_id}"})
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    reviews = supabase_get(
        "reviews",
        params={"product_id": f"eq.{product_id}", "order": "created_at.desc", "limit": 10},
    )
    return {"product": product[0], "reviews": reviews}

# ---------------------------------------------------------------------------
# /search  -- full-text search across brand and model name
# ---------------------------------------------------------------------------
@app.get("/search")
def search_products(q: str = Query(..., min_length=2, description="Search query (brand or model name)")):
    """Search products by brand or model name and include their ranking if available."""
    params = {
        "or": f"(brand.ilike.*{q}*,model_name.ilike.*{q}*)",
        "select": "id,brand,model_name,category",
    }
    results = supabase_get("products", params=params)

    if results:
        product_ids = [p["id"] for p in results]
        rankings = supabase_get("rankings", params={"product_id": f"in.({','.join(product_ids)})"})
        rank_map = {r["product_id"]: r for r in rankings}
        for p in results:
            p["ranking"] = rank_map.get(p["id"], {})

    return {"query": q, "count": len(results), "results": results}

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
