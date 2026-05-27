from fastapi import FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
import requests
import os
import re
import logging
import json
import redis
from datetime import datetime, timedelta
from dotenv import load_dotenv
from pydantic import BaseModel
from typing import Optional

load_dotenv()

# ---------------------------------------------------------------------------
# Logging strategy
# ---------------------------------------------------------------------------
# SAFE to log:   request method + path, HTTP status codes, error *types*,
#                performance metrics, non-sensitive user actions, file paths.
# NEVER log:     environment variables (SUPABASE_KEY, GROQ_API_KEY,
#                ADMIN_API_KEY, REDIS_URL), request headers that carry
#                credentials (Authorization, X-API-Key, apikey), full
#                request/response bodies, database credentials, or any
#                value read directly from os.getenv() for a secret.
# Use logger.debug()   for verbose detail (disabled in production).
# Use logger.info()    for important lifecycle events.
# Use logger.warning() for recoverable problems.
# Use logger.error()   for failures that need attention.
# ---------------------------------------------------------------------------

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
REDIS_URL = os.getenv("REDIS_URL")


def _sanitize_exception(exc: Exception) -> str:
    """Return a safe string representation of an exception.

    Strips any embedded URLs or tokens that Redis / requests libraries
    sometimes include in error messages (e.g. redis://:<password>@host).
    Only the exception *type* and a truncated, credential-free message
    are returned — never the raw repr which may contain secrets.
    """
    raw = str(exc)
    # Redact anything that looks like a URL with credentials
    # (scheme://user:password@host or scheme://:password@host)
    sanitized = re.sub(r"[a-z]+://[^@\s]*@[^\s]*", "<redacted-url>", raw, flags=re.IGNORECASE)
    # Truncate to avoid leaking large payloads
    if len(sanitized) > 200:
        sanitized = sanitized[:200] + "…"
    return f"{type(exc).__name__}: {sanitized}"


def _truncate_response_text(text: str, max_len: int = 120) -> str:
    """Truncate a Supabase response body for safe logging.

    Response bodies may contain row data with PII or other sensitive
    fields.  We log only a short prefix so engineers can identify the
    error class without exposing full payloads.
    """
    if len(text) > max_len:
        return text[:max_len] + "… [truncated]"
    return text


# Redis setup (cache for 15 minutes)
redis_client = None
if REDIS_URL:
    try:
        redis_client = redis.from_url(REDIS_URL, decode_responses=True)
        redis_client.ping()
        # Safe: no credentials logged — just a lifecycle confirmation.
        logging.info("Redis connected successfully")
    except Exception as e:
        # Use sanitized message — raw exception may contain the Redis URL
        # with embedded credentials (redis://:<password>@host:port).
        logging.warning("Redis connection failed: %s", _sanitize_exception(e))
CACHE_TTL = 900

def cache_get(key):
    if redis_client:
        data = redis_client.get(key)
        if data:
            return json.loads(data)
    return None

def cache_set(key, value, ttl=CACHE_TTL):
    if redis_client:
        redis_client.setex(key, ttl, json.dumps(value))

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
    # Safe: method and path are not sensitive.
    # Headers are intentionally excluded — they may carry Authorization /
    # X-API-Key values.  The request body is never logged here.
    logger.info("Request: %s %s", request.method, request.url.path)
    response = await call_next(request)
    # Safe: status code only — no response body or headers logged.
    logger.debug("Response: %s %s → %s", request.method, request.url.path, response.status_code)
    return response

def supabase_get(endpoint, params=None):
    url = f"{SUPABASE_URL}/rest/v1/{endpoint}"
    # Credentials are passed in headers only — never logged.
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}"
    }
    resp = requests.get(url, headers=headers, params=params, timeout=30)
    if resp.status_code != 200:
        # Safe: log endpoint name and status code.
        # Response body is truncated — it may contain row data with PII.
        logger.warning(
            "Supabase GET error %s for %s: %s",
            resp.status_code,
            endpoint,
            _truncate_response_text(resp.text),
        )
        return []
    return resp.json()

def supabase_post(endpoint, data):
    url = f"{SUPABASE_URL}/rest/v1/{endpoint}"
    # Credentials are passed in headers only — never logged.
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json"
    }
    resp = requests.post(url, headers=headers, json=data, timeout=30)
    if resp.status_code != 201:
        # Safe: log endpoint name and status code.
        # Response body is truncated — it may contain row data with PII.
        logger.warning(
            "Supabase POST error %s for %s: %s",
            resp.status_code,
            endpoint,
            _truncate_response_text(resp.text),
        )
        return None
    return resp

def supabase_patch(endpoint, data):
    url = f"{SUPABASE_URL}/rest/v1/{endpoint}"
    # Credentials are passed in headers only — never logged.
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json"
    }
    resp = requests.patch(url, headers=headers, json=data, timeout=30)
    if resp.status_code not in (200, 204):
        # Safe: log endpoint name and status code.
        # Response body is truncated — it may contain row data with PII.
        logger.warning(
            "Supabase PATCH error %s for %s: %s",
            resp.status_code,
            endpoint,
            _truncate_response_text(resp.text),
        )
        return None
    return resp

# ---------- Root ----------
@app.get("/")
def root():
    return {"message": "RedditRecs API is running", "endpoints": ["/rankings", "/product/{product_id}", "/search", "/brands", "/categories", "/usecase/{case}", "/compare", "/trend/{product_id}", "/user_review", "/filters", "/recent_activity", "/vote", "/review_of_week"]}

# ---------- Rankings with category, subreddit, pagination, AND spec filters ----------
@app.get("/rankings")
def get_rankings(
    request: Request,
    limit: int = 20,
    offset: int = 0,
    category: str = None,
    subreddit: str = None,
    spec_room_size: str = None,
    spec_noise_level: str = None,
    spec_energy_efficiency: str = None,
    spec_filter_type: str = None
):
    # Build product ID list based on spec filters
    product_ids = None
    spec_filters = {}
    if spec_room_size:
        spec_filters["room_size"] = spec_room_size
    if spec_noise_level:
        spec_filters["noise_level"] = spec_noise_level
    if spec_energy_efficiency:
        spec_filters["energy_efficiency"] = spec_energy_efficiency
    if spec_filter_type:
        spec_filters["filter_type"] = spec_filter_type

    if spec_filters:
        # Fetch all products with specs and filter client‑side (or use JSONB query)
        all_products = supabase_get("products", params={"select": "id,specs"})
        matched_ids = []
        for p in all_products:
            specs = p.get("specs", {})
            match = True
            for key, val in spec_filters.items():
                if specs.get(key) != val:
                    match = False
                    break
            if match:
                matched_ids.append(p["id"])
        if not matched_ids:
            return {"rankings": []}
        product_ids = matched_ids

    # Category filter
    if category:
        cat_products = supabase_get("products", params={"category": f"eq.{category}", "select": "id"})
        if not cat_products:
            return {"rankings": []}
        cat_ids = [p["id"] for p in cat_products]
        if product_ids:
            product_ids = list(set(product_ids) & set(cat_ids))
        else:
            product_ids = cat_ids
        if not product_ids:
            return {"rankings": []}

    ranking_params = {"order": "rank.asc", "limit": limit, "offset": offset}
    if product_ids:
        ranking_params["product_id"] = f"in.({','.join(product_ids)})"

    if subreddit:
        reviews = supabase_get("reviews", params={"select": "product_id", "subreddit": f"eq.{subreddit}"})
        if not reviews:
            return {"rankings": []}
        sub_ids = list(set([r["product_id"] for r in reviews]))
        if product_ids:
            product_ids = list(set(product_ids) & set(sub_ids))
        else:
            product_ids = sub_ids
        if not product_ids:
            return {"rankings": []}
        ranking_params["product_id"] = f"in.({','.join(product_ids)})"

    rankings = supabase_get("rankings", params=ranking_params)
    if not rankings:
        return {"rankings": []}

    # Fetch product details
    all_product_ids = [r["product_id"] for r in rankings]
    products = supabase_get("products", params={"id": f"in.({','.join(all_product_ids)})"})
    product_map = {p["id"]: p for p in products}
    for r in rankings:
        r["product"] = product_map.get(r["product_id"], {})
    return {"rankings": rankings, "limit": limit, "offset": offset}

# ---------- Product details (includes aspects, specs, percentile) ----------
@app.get("/product/{product_id}")
def product_details(product_id: str):
    product = supabase_get("products", params={"id": f"eq.{product_id}"})
    if not product:
        return {"error": "Product not found"}
    reviews = supabase_get("reviews", params={"product_id": f"eq.{product_id}", "order": "created_at.desc", "limit": 10})
    aspects = supabase_get("product_aspects", params={"product_id": f"eq.{product_id}"})
    # Compute percentile
    all_scores = supabase_get("rankings", params={"select": "sentiment_score", "order": "sentiment_score.asc"})
    product_rankings = supabase_get("rankings", params={"product_id": f"eq.{product_id}"})
    product_score = product_rankings[0]["sentiment_score"] if product_rankings else 0
    scores = [s["sentiment_score"] for s in all_scores if s["sentiment_score"] is not None]
    if scores:
        rank = sum(1 for s in scores if s < product_score) + 1
        percentile = int((rank / len(scores)) * 100)
    else:
        percentile = 0
    product[0]["reddit_percentile"] = percentile
    return {"product": product[0], "reviews": reviews, "aspects": aspects}

# ---------- Search ----------
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

# ---------- Categories ----------
@app.get("/categories")
def get_categories():
    products = supabase_get("products", params={"select": "category"})
    categories = set()
    for p in products:
        if p.get("category"):
            categories.add(p["category"])
    return {"categories": sorted(list(categories))}

# ---------- Use-case ----------
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

# ---------- Comparison ----------
@app.get("/compare")
def compare_products(ids: str = Query(...)):
    product_ids = [pid.strip() for pid in ids.split(",")]
    if len(product_ids) < 2 or len(product_ids) > 3:
        return {"error": "Please provide 2 or 3 product IDs"}
    products_data = []
    for pid in product_ids:
        product = supabase_get("products", params={"id": f"eq.{pid}"})
        if not product:
            continue
        ranking = supabase_get("rankings", params={"product_id": f"eq.{pid}"})
        aspects = supabase_get("product_aspects", params={"product_id": f"eq.{pid}"})
        pros = [a for a in aspects if a["sentiment"] == "positive"][:3]
        cons = [a for a in aspects if a["sentiment"] == "negative"][:3]
        products_data.append({
            "id": pid,
            "brand": product[0]["brand"],
            "model": product[0]["model_name"],
            "sentiment_score": ranking[0]["sentiment_score"] if ranking else None,
            "positive_count": ranking[0]["positive_count"] if ranking else 0,
            "negative_count": ranking[0]["negative_count"] if ranking else 0,
            "price": product[0].get("amazon_price"),
            "image_url": product[0].get("image_url"),
            "pros": [{"aspect": p["aspect_name"], "count": p["positive_count"]} for p in pros],
            "cons": [{"aspect": c["aspect_name"], "count": c["negative_count"]} for c in cons],
            "subreddits": list(set(r.get("subreddit") for r in supabase_get("reviews", params={"product_id": f"eq.{pid}", "select": "subreddit"})))
        })
    return {"products": products_data}

# ---------- Sentiment Trend ----------
@app.get("/trend/{product_id}")
def get_trend(product_id: str, months: int = 12):
    history = supabase_get("sentiment_history", params={"product_id": f"eq.{product_id}", "order": "date.asc", "limit": months})
    return {"product_id": product_id, "history": history}

# ---------- User review submission (unchanged) ----------
class UserReview(BaseModel):
    product_id: str
    username: str
    email: Optional[str] = None
    sentiment: str
    verbatim: str

@app.post("/user_review")
def submit_user_review(review: UserReview):
    if review.sentiment not in ["positive", "negative", "neutral"]:
        return {"error": "Invalid sentiment"}
    data = review.dict()
    data["verified"] = False
    resp = supabase_post("user_reviews", data)
    if resp is None:
        return {"error": "Failed to submit review"}
    # Invalidate relevant caches (optional)
    if redis_client:
        for key in redis_client.scan_iter("rankings:*"):
            redis_client.delete(key)
    return {"message": "Review submitted, awaiting verification"}

# ---------- NEW: Dynamic filters ----------
@app.get("/filters")
def get_filters():
    cache_key = "filters"
    cached = cache_get(cache_key)
    if cached is not None:
        return cached
    products = supabase_get("products", params={"select": "specs"})
    filters = {
        "room_size": set(),
        "noise_level": set(),
        "energy_efficiency": set(),
        "filter_type": set()
    }
    for p in products:
        specs = p.get("specs", {})
        for key in filters.keys():
            val = specs.get(key)
            if val and isinstance(val, str):
                filters[key].add(val.strip())
    result = {k: sorted(list(v)) for k, v in filters.items()}
    cache_set(cache_key, result, ttl=3600)  # cache for 1 hour
    return result

# ---------- NEW: Recent activity feed ----------
@app.get("/recent_activity")
def recent_activity(limit: int = 5):
    reviews = supabase_get("reviews", params={"order": "created_at.desc", "limit": limit, "select": "id,verbatim,created_at,subreddit,product_id,helpful_score"})
    if not reviews:
        return {"activity": []}
    product_ids = list(set([r["product_id"] for r in reviews]))
    products = supabase_get("products", params={"id": f"in.({','.join(product_ids)})", "select": "id,brand,model_name"})
    product_map = {p["id"]: p for p in products}
    for r in reviews:
        r["product"] = product_map.get(r["product_id"], {})
        r["snippet"] = (r["verbatim"][:120] + "...") if len(r["verbatim"]) > 120 else r["verbatim"]
    return {"activity": reviews}

# ---------- NEW: User voting (upvote/downvote) ----------
@app.post("/vote")
async def vote_review(request: Request):
    try:
        data = await request.json()
    except:
        return {"error": "Invalid JSON"}
    review_id = data.get("review_id")
    vote = data.get("vote")  # 1 or -1
    if not review_id or vote not in (1, -1):
        return {"error": "Invalid data"}
    client_ip = request.client.host
    # Check existing vote
    existing = supabase_get("user_votes", params={"review_id": f"eq.{review_id}", "user_ip": f"eq.{client_ip}"})
    if existing:
        supabase_patch(f"user_votes?id=eq.{existing[0]['id']}", {"vote": vote})
    else:
        supabase_post("user_votes", {"review_id": review_id, "user_ip": client_ip, "vote": vote})
    # Get updated helpful_score
    updated = supabase_get("reviews", params={"id": f"eq.{review_id}", "select": "helpful_score"})
    new_score = updated[0]["helpful_score"] if updated else 0
    return {"message": "Vote recorded", "new_score": new_score}

# ---------- NEW: Review of the week ----------
@app.get("/review_of_week")
def review_of_week():
    today = datetime.now().date()
    start_of_week = today - timedelta(days=today.weekday())  # Monday
    week_entry = supabase_get("weekly_review", params={"week_start": f"eq.{start_of_week.isoformat()}", "select": "review_id"})
    if not week_entry:
        return {"review": None}
    review = supabase_get("reviews", params={"id": f"eq.{week_entry[0]['review_id']}", "select": "*,product_id"})
    if not review:
        return {"review": None}
    product = supabase_get("products", params={"id": f"eq.{review[0]['product_id']}", "select": "brand,model_name"})
    if product:
        review[0]["product"] = product[0]
    else:
        review[0]["product"] = {}
    # Add snippet
    review[0]["snippet"] = (review[0]["verbatim"][:200] + "...") if len(review[0]["verbatim"]) > 200 else review[0]["verbatim"]
    return {"review": review[0]}

# ---------- Widget ----------
@app.get("/widget.html", response_class=HTMLResponse)
def serve_widget():
    # Strategy 1: explicit container working directory path
    paths_tried = []
    candidate_paths = [
        "/app/widget.html",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "widget.html"),
        os.path.join(os.getcwd(), "widget.html"),
    ]
    for path in candidate_paths:
        paths_tried.append(path)
        try:
            with open(path, "r", encoding="utf-8") as f:
                # Safe: file path is not sensitive.
                logger.info("Serving widget.html from: %s", path)
                return HTMLResponse(content=f.read(), status_code=200)
        except FileNotFoundError:
            # Safe: file path is not sensitive.
            logger.warning("widget.html not found at: %s", path)
        except Exception as e:
            # Safe: path is not sensitive; exception message is for a file
            # read error and will not contain credentials.
            logger.error("Error reading widget.html at %s: %s", path, e)

    # Safe: only file-system paths are included — no credentials.
    logger.error("widget.html not found. Tried: %s", paths_tried)
    return HTMLResponse(
        content=(
            f"<h1>widget.html not found</h1>"
            f"<p>Searched the following paths:</p>"
            f"<ul>{''.join(f'<li>{p}</li>' for p in paths_tried)}</ul>"
            f"<p>CWD: {os.getcwd()}</p>"
            f"<p>__file__: {os.path.abspath(__file__)}</p>"
        ),
        status_code=404,
    )

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
