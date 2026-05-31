from fastapi import FastAPI, Query, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
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
from slowapi import Limiter
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

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

# ---------- Security configuration ----------
# Set ADMIN_API_KEY in your Railway environment variables.
# Use a strong random string (32+ characters), e.g.:
#   python -c "import secrets; print(secrets.token_hex(32))"
ADMIN_API_KEY = os.getenv("ADMIN_API_KEY", "")

# Endpoints that are publicly readable — no API key required.
# Prefix-matched: any path that *starts with* one of these strings is allowed.
PUBLIC_READ_PATHS = {
    "/", "/widget.html", "/debug/routes",
    "/rankings", "/product/", "/search", "/brands", "/categories",
    "/usecase/", "/compare", "/trend/", "/filters", "/recent_activity", "/review_of_week",
}

# Endpoints that mutate state — API key required to prevent spam/abuse.
PROTECTED_WRITE_PATHS = {
    "/user_review", "/vote",
}

# Allowed CORS origin(s). Set ALLOWED_ORIGIN in Railway to your WordPress domain,
# e.g. "https://www.example.com". Defaults to localhost for local development.
ALLOWED_ORIGIN = os.getenv("ALLOWED_ORIGIN", "http://localhost")


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
    return f"{type(exc).__name__}: {sanitized[:200]}"


def _truncate_response_text(text: str, max_len: int = 120) -> str:
    """Truncate a response body string to avoid logging PII or large payloads."""
    if not text:
        return ""
    return text[:max_len] + ("…" if len(text) > max_len else "")

# Redis setup (cache for 15 minutes)
redis_client = None
if REDIS_URL:
    try:
        redis_client = redis.from_url(REDIS_URL, decode_responses=True)
        redis_client.ping()
        logging.info("Redis connected")
    except Exception as e:
        logging.warning(f"Redis connection error: {_sanitize_exception(e)}")
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

# ---------- Rate limiting ----------
limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter

@app.exception_handler(RateLimitExceeded)
async def rate_limit_handler(request: Request, exc: RateLimitExceeded):
    return JSONResponse(
        status_code=429,
        content={"error": "Rate limit exceeded. Please slow down and try again later."},
        headers={"Retry-After": "60"},
    )

# ---------- CORS middleware ----------
# Only the configured WordPress domain (ALLOWED_ORIGIN) may call this API.
# GET and POST are the only permitted methods; X-API-Key must be allowed so
# browsers can include it in pre-flight and actual requests.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[ALLOWED_ORIGIN],
    allow_methods=["GET", "POST"],
    allow_headers=["X-API-Key", "Content-Type"],
)

# ---------- API key authentication middleware ----------
# Security model:
#   - GET requests to PUBLIC_READ_PATHS are open to everyone (widget, browsers).
#   - POST requests to PROTECTED_WRITE_PATHS require a valid X-API-Key header.
#   - All other requests also require the key.
# NOTE: Headers and request bodies are intentionally NOT logged here to
# prevent accidental exposure of credentials or PII in log streams.
@app.middleware("http")
async def require_api_key(request: Request, call_next):
    path = request.url.path

    # Allow all GET requests to public read paths (prefix match).
    if request.method == "GET" and any(path.startswith(p) for p in PUBLIC_READ_PATHS):
        return await call_next(request)

    # Require API key for protected write endpoints and anything else.
    api_key = request.headers.get("X-API-Key", "")
    if not ADMIN_API_KEY or api_key != ADMIN_API_KEY:
        return JSONResponse(
            status_code=403,
            content={"error": "Forbidden: valid X-API-Key header required for write operations."},
        )
    return await call_next(request)

@app.middleware("http")
async def log_requests(request: Request, call_next):
    # Only method + path are logged — headers and body are intentionally
    # excluded to prevent credentials (X-API-Key, Authorization) or PII
    # from appearing in log streams.
    logger.info(f"Request: {request.method} {request.url.path}")
    response = await call_next(request)
    return response

def supabase_get(endpoint, params=None):
    url = f"{SUPABASE_URL}/rest/v1/{endpoint}"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}"
    }
    resp = requests.get(url, headers=headers, params=params, timeout=30)
    if resp.status_code != 200:
        logger.warning(f"Supabase error {resp.status_code} for {endpoint}: {_truncate_response_text(resp.text)}")
        return []
    return resp.json()

def supabase_post(endpoint, data):
    url = f"{SUPABASE_URL}/rest/v1/{endpoint}"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json"
    }
    resp = requests.post(url, headers=headers, json=data, timeout=30)
    if resp.status_code != 201:
        logger.warning(f"Supabase POST error {resp.status_code} for {endpoint}: {_truncate_response_text(resp.text)}")
        return None
    return resp

def supabase_patch(endpoint, data):
    url = f"{SUPABASE_URL}/rest/v1/{endpoint}"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json"
    }
    resp = requests.patch(url, headers=headers, json=data, timeout=30)
    if resp.status_code not in (200, 204):
        logger.warning(f"Supabase PATCH error {resp.status_code} for {endpoint}: {_truncate_response_text(resp.text)}")
        return None
    return resp

# ---------- Root ----------
@app.get("/")
def root():
    return {"message": "RedditRecs API is running", "endpoints": ["/rankings", "/product/{product_id}", "/search", "/brands", "/categories", "/usecase/{case}", "/compare", "/trend/{product_id}", "/user_review", "/filters", "/recent_activity", "/vote", "/review_of_week"]}

# ---------- Rankings with category, subreddit, pagination, AND spec filters ----------
@app.get("/rankings")
@limiter.limit("100/minute")
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

    # Category filter — fetch products directly from products table so new
    # products without ranking entries are still included in the response.
    if category:
        cat_products = supabase_get("products", params={"category": f"eq.{category}", "select": "id,brand,model_name,image_url,specs"})
        if not cat_products:
            return {"rankings": []}
        cat_ids = [p["id"] for p in cat_products]
        if product_ids:
            product_ids = list(set(product_ids) & set(cat_ids))
        else:
            product_ids = cat_ids
        if not product_ids:
            return {"rankings": []}

    # Fetch rankings for the resolved product set
    ranking_params = {"order": "rank.asc", "limit": limit + 100, "offset": 0}
    if product_ids:
        ranking_params["product_id"] = f"in.({','.join(product_ids)})"

    rankings = supabase_get("rankings", params=ranking_params)

    # Determine which products have no ranking entry yet
    ranked_product_ids = set(r["product_id"] for r in rankings)
    unranked_ids = [pid for pid in (product_ids or []) if pid not in ranked_product_ids]

    # Build default ranking stubs for unranked products so they still appear
    if unranked_ids:
        unranked_products = supabase_get("products", params={"id": f"in.({','.join(unranked_ids)})"})
        for prod in unranked_products:
            reviews = supabase_get("reviews", params={"product_id": f"eq.{prod['id']}", "select": "id"})
            review_count = len(reviews) if reviews else 0
            rankings.append({
                "product_id": prod["id"],
                "rank": 999,
                "sentiment_score": 0.5,
                "positive_count": 0,
                "negative_count": 0,
                "review_count": review_count,
            })

    # Apply subreddit filter
    if subreddit:
        reviews = supabase_get("reviews", params={"select": "product_id", "subreddit": f"eq.{subreddit}"})
        if not reviews:
            return {"rankings": []}
        sub_ids = set(r["product_id"] for r in reviews)
        rankings = [r for r in rankings if r["product_id"] in sub_ids]

    if not rankings:
        return {"rankings": []}

    # Sort by rank then apply pagination
    rankings.sort(key=lambda x: x.get("rank", 999))
    rankings = rankings[offset:offset + limit]

    # Fetch product details for the final page
    all_product_ids = [r["product_id"] for r in rankings]
    products = supabase_get("products", params={"id": f"in.({','.join(all_product_ids)})"})
    product_map = {p["id"]: p for p in products}

    for r in rankings:
        r["product"] = product_map.get(r["product_id"], {})
        if "review_count" not in r:
            reviews = supabase_get("reviews", params={"product_id": f"eq.{r['product_id']}", "select": "id"})
            r["review_count"] = len(reviews) if reviews else 0

    return {"rankings": rankings, "limit": limit, "offset": offset}

# ---------- Product details (includes aspects, specs, percentile) ----------
@app.get("/product/{product_id}")
@limiter.limit("100/minute")
def product_details(request: Request, product_id: str):
    product = supabase_get("products", params={"id": f"eq.{product_id}"})
    if not product:
        return {"error": "Product not found"}
    reviews = supabase_get("reviews", params={"product_id": f"eq.{product_id}", "order": "created_at.desc", "limit": 50})
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
@limiter.limit("100/minute")
def search_products(request: Request, q: str = Query(..., min_length=2)):
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
@limiter.limit("100/minute")
def get_brands(request: Request, category: Optional[str] = None):
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
@limiter.limit("100/minute")
def get_categories(request: Request):
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
@limiter.limit("100/minute")
def get_usecase(request: Request, case: str, limit: int = 10):
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
@limiter.limit("100/minute")
def compare_products(request: Request, ids: str = Query(...)):
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
@limiter.limit("100/minute")
def get_trend(request: Request, product_id: str, months: int = 12):
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
@limiter.limit("50/minute")
def submit_user_review(request: Request, review: UserReview):
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
@limiter.limit("100/minute")
def get_filters(request: Request):
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
    # Patterns to normalize
    ignore_terms = re.compile(r'\b(not mentioned|not specified|none mentioned|unknown|none|null|n/a)\b', re.IGNORECASE)
    
    for p in products:
        specs = p.get("specs", {})
        for key in filters.keys():
            val = specs.get(key)
            if not val or not isinstance(val, str):
                continue
            
            # Normalize: clean whitespace, lower, replace ignore terms with "Not specified"
            clean = val.strip()
            if ignore_terms.search(clean):
                clean = "Not specified"
            else:
                # Optionally shorten common phrases
                clean = re.sub(r'\bup to\b', '≤', clean)
                clean = re.sub(r'\b(?:square feet|sq ft|ft2)\b', 'sq ft', clean)
            
            if clean:
                filters[key].add(clean)
    
    # Convert sets to sorted lists, putting "Not specified" at the end
    result = {}
    for k, v in filters.items():
        lst = sorted(v)
        if "Not specified" in lst:
            lst.remove("Not specified")
            lst.append("Not specified")
        result[k] = lst
    
    cache_set(cache_key, result, ttl=3600)
    return result

# ---------- NEW: Recent activity feed ----------
@app.get("/recent_activity")
@limiter.limit("100/minute")
def recent_activity(request: Request, limit: int = 5):
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
@limiter.limit("50/minute")
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
@limiter.limit("100/minute")
def review_of_week(request: Request):
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
                logger.info(f"Serving widget.html from: {path}")
                return HTMLResponse(content=f.read(), status_code=200)
        except FileNotFoundError:
            logger.warning(f"widget.html not found at: {path}")
        except Exception as e:
            logger.error(f"Error reading widget.html at {path}: {e}")

    logger.error(f"widget.html not found. Tried: {paths_tried}")
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
