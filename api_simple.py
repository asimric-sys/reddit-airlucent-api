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
# The CORS middleware uses this value; the validate_origin middleware below
# enforces the airlucent.com allowlist as a hard server-side check.
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
    allow_origins=[
        "https://airlucent.com",
        "https://www.airlucent.com",
        "https://reddit-airlucent-api-production.up.railway.app",
        ALLOWED_ORIGIN,  # kept for local development fallback
    ],
    allow_methods=["GET", "POST"],
    allow_headers=["X-API-Key", "Content-Type", "Origin"],
)

# ---------- Origin validation middleware ----------
# Allowed widget origins — airlucent.com and its subdomains (covers www,
# staging, wp-admin, etc.), the Railway deployment URL for owner testing,
# and localhost for local development.
# This is a defence-in-depth layer on top of CORS: CORS headers tell
# browsers to block cross-origin reads, but this middleware actively
# rejects requests whose Origin header does not match, preventing
# server-side abuse by non-browser clients that forge the header.
ALLOWED_ORIGIN_EXACT = {
    "https://airlucent.com",
    "https://www.airlucent.com",
    "https://reddit-airlucent-api-production.up.railway.app",
    "http://localhost",
    "http://127.0.0.1",
}
ALLOWED_ORIGIN_SUBDOMAIN_SUFFIX = ".airlucent.com"


def _is_allowed_origin(origin: str) -> bool:
    """Return True if the origin is permitted to use this API."""
    if origin in ALLOWED_ORIGIN_EXACT:
        return True
    # Allow any subdomain of airlucent.com (e.g. staging.airlucent.com,
    # wp-admin.airlucent.com) over either http or https.
    try:
        from urllib.parse import urlparse
        host = urlparse(origin).hostname or ""
        if host.endswith(ALLOWED_ORIGIN_SUBDOMAIN_SUFFIX):
            return True
    except Exception:
        pass
    return False


@app.middleware("http")
async def validate_origin(request: Request, call_next):
    # Skip origin enforcement for the widget.html page itself (it is served
    # directly, not fetched cross-origin) and for the root health-check.
    path = request.url.path
    if path in ("/", "/widget.html", "/debug/routes"):
        return await call_next(request)

    origin = request.headers.get("origin", "").strip()

    # Requests without an Origin header are same-origin requests (e.g. the
    # widget fetching its own API on the Railway domain).  Browsers only attach
    # an Origin header for cross-origin requests, so the absence of the header
    # is a reliable signal that the request is same-origin and should be
    # allowed unconditionally.
    if not origin:
        return await call_next(request)

    # Cross-origin request: validate the Origin against the allowlist.
    if not _is_allowed_origin(origin):
        return JSONResponse(
            status_code=403,
            content={"error": "Forbidden: this API is only accessible from airlucent.com."},
        )

    return await call_next(request)

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

# ---------- Helper: compute product stats from reviews (no rankings table) ----------
def compute_product_stats(product_ids: list) -> dict:
    """Return a dict of {product_id: {positive_count, negative_count, review_count, sentiment_score}}
    computed live from the reviews table. Batches queries to avoid Supabase URL length limits."""
    if not product_ids:
        return {}
    stats = {}
    # Batch into chunks of 100 to keep URL under Supabase limits
    batch_size = 100
    for i in range(0, len(product_ids), batch_size):
        batch = product_ids[i:i + batch_size]
        reviews = supabase_get("reviews", params={"product_id": f"in.({','.join(batch)})", "select": "product_id,sentiment"})
        for r in reviews:
            pid = r["product_id"]
            if pid not in stats:
                stats[pid] = {"positive_count": 0, "negative_count": 0, "neutral_count": 0, "review_count": 0}
            stats[pid]["review_count"] += 1
            sent = r.get("sentiment", "neutral")
            if sent == "positive":
                stats[pid]["positive_count"] += 1
            elif sent == "negative":
                stats[pid]["negative_count"] += 1
            else:
                stats[pid]["neutral_count"] += 1
    for pid in stats:
        s = stats[pid]
        total = s["positive_count"] + s["negative_count"]
        s["sentiment_score"] = round(s["positive_count"] / total, 2) if total > 0 else 0.5
    return stats

# ---------- Root ----------
@app.get("/")
def root():
    return {"message": "RedditRecs API is running", "endpoints": ["/rankings", "/product/{product_id}", "/search", "/brands", "/categories", "/usecase/{case}", "/compare", "/trend/{product_id}", "/user_review", "/filters", "/recent_activity", "/vote", "/review_of_week"]}

# ---------- Rankings (computed live from products + reviews — no rankings table) ----------
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
    # Start with all products
    product_params = {"select": "id,brand,model_name,category,image_url,specs,created_at"}

    # Category filter
    if category:
        product_params["category"] = f"eq.{category}"

    products = supabase_get("products", params=product_params)
    if not products:
        return {"rankings": []}

    # Build product lookup
    product_map = {p["id"]: p for p in products}
    product_ids = list(product_map.keys())

    # Spec filter — client-side matching
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
        matched_ids = []
        for pid, prod in product_map.items():
            specs = prod.get("specs", {}) or {}
            match = True
            for key, val in spec_filters.items():
                if specs.get(key, "").lower() != val.lower():
                    match = False
                    break
            if match:
                matched_ids.append(pid)
        if not matched_ids:
            return {"rankings": []}
        product_ids = matched_ids

    # Subreddit filter
    if subreddit:
        sub_reviews = supabase_get("reviews", params={"select": "product_id", "subreddit": f"eq.{subreddit}"})
        if not sub_reviews:
            return {"rankings": []}
        sub_ids = set(r["product_id"] for r in sub_reviews)
        product_ids = [pid for pid in product_ids if pid in sub_ids]
        if not product_ids:
            return {"rankings": []}

    # Compute stats live from reviews
    stats = compute_product_stats(product_ids)

    # Build rankings response
    rankings = []
    for pid in product_ids:
        prod = product_map.get(pid, {})
        s = stats.get(pid, {"positive_count": 0, "negative_count": 0, "review_count": 0, "sentiment_score": 0.5})
        rankings.append({
            "product_id": pid,
            "rank": 0,  # will be set after sorting
            "sentiment_score": s["sentiment_score"],
            "positive_count": s["positive_count"],
            "negative_count": s["negative_count"],
            "review_count": s["review_count"],
            "product": prod,
        })

    # Sort by review_count descending, then newest first
    rankings.sort(key=lambda x: (-x["review_count"], x["product"].get("created_at", "") or ""), reverse=False)
    # Actually sort by review_count desc
    rankings.sort(key=lambda x: x["review_count"], reverse=True)

    # Assign sequential rank
    for i, r in enumerate(rankings):
        r["rank"] = i + 1

    # Paginate
    total = len(rankings)
    rankings = rankings[offset:offset + limit]

    return {"rankings": rankings, "limit": limit, "offset": offset, "total": total}

# ---------- Product details (no rankings table needed) ----------
@app.get("/product/{product_id}")
@limiter.limit("100/minute")
def product_details(request: Request, product_id: str):
    product = supabase_get("products", params={"id": f"eq.{product_id}"})
    if not product:
        return {"error": "Product not found"}
    reviews = supabase_get("reviews", params={"product_id": f"eq.{product_id}", "order": "created_at.desc", "limit": 50})
    aspects = supabase_get("product_aspects", params={"product_id": f"eq.{product_id}"})
    # Compute stats live
    stats = compute_product_stats([product_id])
    s = stats.get(product_id, {"positive_count": 0, "negative_count": 0, "review_count": 0, "sentiment_score": 0.5})
    product[0]["review_count"] = s["review_count"]
    product[0]["positive_count"] = s["positive_count"]
    product[0]["negative_count"] = s["negative_count"]
    product[0]["sentiment_score"] = s["sentiment_score"]
    return {"product": product[0], "reviews": reviews, "aspects": aspects}

# ---------- Search ----------
@app.get("/search")
@limiter.limit("100/minute")
def search_products(request: Request, q: str = Query(..., min_length=2)):
    params = {
        "or": f"(brand.ilike.*{q}*,model_name.ilike.*{q}*)",
        "select": "id,brand,model_name,category,image_url"
    }
    results = supabase_get("products", params=params)
    if results:
        product_ids = [p["id"] for p in results]
        stats = compute_product_stats(product_ids)
        for p in results:
            s = stats.get(p["id"], {"review_count": 0, "sentiment_score": 0.5, "positive_count": 0, "negative_count": 0})
            p["ranking"] = s
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
    cache_key = "categories"
    cached = cache_get(cache_key)
    if cached is not None:
        return cached
    products = supabase_get("products", params={"select": "category"})
    categories = set()
    for p in products:
        if p.get("category"):
            categories.add(p["category"])
    result = {"categories": sorted(list(categories))}
    cache_set(cache_key, result, ttl=60)
    return result

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
    stats = compute_product_stats(product_ids)
    for pid in product_ids:
        product = supabase_get("products", params={"id": f"eq.{pid}"})
        if not product:
            continue
        aspects = supabase_get("product_aspects", params={"product_id": f"eq.{pid}"})
        pros = [a for a in aspects if a["sentiment"] == "positive"][:3]
        cons = [a for a in aspects if a["sentiment"] == "negative"][:3]
        s = stats.get(pid, {"positive_count": 0, "negative_count": 0, "sentiment_score": None})
        products_data.append({
            "id": pid,
            "brand": product[0]["brand"],
            "model": product[0]["model_name"],
            "sentiment_score": s.get("sentiment_score"),
            "positive_count": s["positive_count"],
            "negative_count": s["negative_count"],
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

# ---------- User review submission ----------
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
    # Invalidate caches so new products/categories appear immediately
    if redis_client:
        redis_client.delete("categories")
        redis_client.delete("filters")
    return {"message": "Review submitted, awaiting verification"}

# ---------- Dynamic filters ----------
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
    
    cache_set(cache_key, result, ttl=60)
    return result

# ---------- Recent activity feed ----------
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

# ---------- User voting (upvote/downvote) ----------
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

# ---------- Review of the week ----------
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
