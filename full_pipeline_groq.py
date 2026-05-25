import requests
import json
import os
import random
import time
from dotenv import load_dotenv
from groq import Groq

# Load environment variables from .env file
load_dotenv()
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

if not GROQ_API_KEY:
    print("ERROR: GROQ_API_KEY not found in .env file")
    exit(1)

# Initialize Groq client
client = Groq(api_key=GROQ_API_KEY)

PREDEFINED_CATEGORIES = [
    "air-purifier", "humidifier", "air-conditioner", "robot-vacuum",
    "smart-doorbell", "smart-thermostat", "heating-cooling", "air-quality"
]

# Realistic User-Agent pool covering Windows, macOS, and Linux browsers
USER_AGENTS = [
    # Chrome on Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 11.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    # Chrome on macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_4) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    # Firefox on Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0",
    # Firefox on macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14.4; rv:125.0) Gecko/20100101 Firefox/125.0",
    # Safari on macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Safari/605.1.15",
    # Chrome on Linux
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:125.0) Gecko/20100101 Firefox/125.0",
]


def get_random_user_agent():
    """Return a random User-Agent string from the pool."""
    return random.choice(USER_AGENTS)


def get_existing_product_names():
    """
    Fetch all products from Supabase and build product-specific search queries
    in the form '{brand} {model_name} review'.

    Returns an empty list if Supabase credentials are missing or the request
    fails, so the pipeline can still fall back to generic queries.
    """
    if not SUPABASE_URL or not SUPABASE_KEY:
        print("[WARN] SUPABASE_URL or SUPABASE_KEY not set — skipping product query fetch")
        return []

    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
    }
    url = f"{SUPABASE_URL}/rest/v1/products"
    params = {"select": "brand,model_name"}

    try:
        resp = requests.get(url, headers=headers, params=params, timeout=15)
        if resp.status_code != 200:
            print(f"[WARN] Supabase returned {resp.status_code} when fetching products: {resp.text[:200]}")
            return []

        products = resp.json()
        queries = []
        seen = set()
        for product in products:
            brand = (product.get("brand") or "").strip()
            model_name = (product.get("model_name") or "").strip()
            if brand and model_name:
                query = f"{brand} {model_name} review"
                if query not in seen:
                    seen.add(query)
                    queries.append(query)

        print(f"[DB] Fetched {len(queries)} product-specific queries from Supabase")
        return queries

    except Exception as e:
        print(f"[WARN] Failed to fetch products from Supabase: {e}")
        return []


def search_reddit(query, limit=20):
    """Search Reddit using the public JSON endpoint with a rotated User-Agent.

    Returns an empty list on any non-200 response (including 403) so the
    pipeline can continue with the next query without crashing.
    """
    url = "https://www.reddit.com/r/all/search.json"
    params = {"q": query, "sort": "relevance", "t": "year", "limit": limit}
    headers = {"User-Agent": get_random_user_agent()}
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=30)
        if resp.status_code == 403:
            print(f"   [SKIP] Reddit returned 403 for query '{query}' — skipping")
            return []
        if resp.status_code != 200:
            print(f"   [WARN] Reddit API returned {resp.status_code} for query '{query}'")
            return []
        data = resp.json()
        posts = []
        for child in data["data"]["children"]:
            post = child["data"]
            posts.append({
                "title": post["title"],
                "selftext": post["selftext"][:800],
                "score": post["score"],
                "subreddit": post.get("subreddit", "")
            })
        return posts
    except Exception as e:
        print(f"   [ERROR] Error searching Reddit: {e}")
        return []


def extract_reviews(text, subreddit=""):
    """
    Extract product reviews from Reddit text using Groq (Llama 3.3).
    Returns a list of dicts with keys: sentiment, brand, product_name,
    category, verbatim.
    """
    subreddit_context = f" The post is from r/{subreddit}." if subreddit else ""
    predefined = ", ".join(f'"{c}"' for c in PREDEFINED_CATEGORIES)

    prompt = f"""
    Extract product reviews from this Reddit text.{subreddit_context}
    Return ONLY a single JSON array. Example: [{{"sentiment": "positive", "brand": "Dyson", "product_name": "HP07", "category": "air-purifier", "verbatim": "..."}}]
    If no product review, return [].
    Do not add any text before or after the JSON array.

    Rules for brand and product_name:
    - Extract the brand (manufacturer) and product_name (model) separately.
    - If a specific model is mentioned (e.g., "Coway Airmega 400"), extract the model name as product_name (e.g., "Airmega 400").
    - If only the brand is mentioned without a specific model, set product_name to "{{brand}} Air Purifier" (e.g., "Coway Air Purifier").
    - Never leave product_name empty.

    Rules for category:
    - Prefer one of the predefined categories if it fits well: {predefined}.
    - If NONE of the predefined categories match, INVENT a new, short, descriptive category name (lowercase, use hyphens).
      Examples of invented categories: "dehumidifier", "air-quality-monitor", "portable-fan", "smart-plug".
    - Default to "other" ONLY if you cannot determine any meaningful category at all.
    - Never leave category empty.

    Text: {text[:2000]}
    """
    try:
        response = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,           # Low temp for consistent output
            max_tokens=600
        )
        output = response.choices[0].message.content.strip()
        # Remove markdown code blocks
        output = output.replace("```json", "").replace("```", "").strip()
        # Find the first '[' and last ']' to extract just the JSON array
        start = output.find('[')
        end = output.rfind(']') + 1
        if start != -1 and end != 0:
            output = output[start:end]
        reviews = json.loads(output)
        # Ensure it's a list (in case the model returns a single object)
        if isinstance(reviews, dict):
            reviews = [reviews]
        for rev in reviews:
            # Safety net: ensure product_name is never empty
            brand = (rev.get("brand") or "").strip()
            product_name = (rev.get("product_name") or "").strip()
            if brand and not product_name:
                rev["product_name"] = f"{brand} Air Purifier"
            # Safety net: ensure category is never empty; default to "other"
            category = (rev.get("category") or "").strip().lower()
            if not category:
                category = "other"
            rev["category"] = category
        return reviews
    except json.JSONDecodeError as e:
        print(f"JSON decode error: {e}\nRaw output: {output[:200]}")
        return []
    except Exception as e:
        print(f"Extraction error: {e}")
        return []


def run_pipeline():
    """Main pipeline: search Reddit, extract reviews, save to JSON.

    Query strategy:
    1. Fetch product-specific queries from Supabase (e.g. "Dyson HP07 review")
       so we collect more reviews for products already in the database.
    2. Fall back to a broad set of generic queries to discover new products.
    Duplicate queries are deduplicated before the run starts.
    """
    # --- Fallback generic queries (used when no DB products exist yet) ---
    generic_queries = [
        # General
        "best air purifier", "air purifier review", "air purifier recommendation",
        # Brands
        "Coway air purifier", "Levoit air purifier", "Winix air purifier", "Blueair air purifier",
        "Philips air purifier", "Honeywell air purifier", "Austin air purifier", "IQAir",
        "Alen air purifier", "Medify air purifier", "Shark air purifier", "GermGuardian",
        "Cuckoo air purifier", "PuroAir", "Govee air purifier", "CleanAirKits",
        # Specific models
        "Coway Airmega 400", "Levoit Core 300", "Winix 5500-2", "Blueair 211+",
        "Philips 1000i", "Honeywell HPA300",
        # Use cases
        "air purifier for allergies", "air purifier for smoke", "air purifier for pets",
        "air purifier for large room", "quiet air purifier",
        # HVAC & air quality
        "whole house air purifier", "HVAC air cleaner", "air quality monitor",
        "humidifier review", "portable air conditioner review",
        # Use-case specific queries
        "air purifier smoke review",
        "best air purifier for allergies",
        "quiet air purifier bedroom",
        "large room air purifier",
        "air purifier energy efficient",
        "smart air purifier wifi",
        "robot vacuum for pet hair",
        "smart thermostat energy saving",
        "humidifier for dry air",
        # New product types
        "dehumidifier review", "best dehumidifier",
        "air conditioner window unit review",
        "smart plug review", "best smart plug",
    ]

    # --- Build the final deduplicated query list ---
    # Product-specific queries come first so existing products get enriched
    # before we branch out to generic discovery.
    product_queries = get_existing_product_names()
    seen = set()
    queries = []
    for q in product_queries + generic_queries:
        if q not in seen:
            seen.add(q)
            queries.append(q)

    print(f"[INFO] Running pipeline with {len(queries)} queries "
          f"({len(product_queries)} product-specific, "
          f"{len(queries) - len(product_queries)} generic)")

    all_reviews = []

    for q in queries:
        print(f"\n[SEARCH] Searching: {q}")
        posts = search_reddit(q, limit=25)

        print(f"   Found {len(posts)} posts")
        for i, post in enumerate(posts):
            subreddit = post.get("subreddit", "")
            full_text = f"Title: {post['title']}\nBody: {post['selftext']}"
            reviews = extract_reviews(full_text, subreddit=subreddit)
            for rev in reviews:
                rev["source_query"] = q
                rev["subreddit"] = subreddit
                all_reviews.append(rev)
            print(f"   Post {i+1}: {len(reviews)} reviews")

        # Random delay between queries to reduce the chance of rate-limiting
        delay = random.uniform(2, 5)
        print(f"   [DELAY] Waiting {delay:.1f}s before next query…")
        time.sleep(delay)

    print(f"\n[OK] Total reviews collected: {len(all_reviews)}")
    with open("reviews_output.json", "w", encoding="utf-8") as f:
        json.dump(all_reviews, f, indent=2, ensure_ascii=False)
    print("[FILE] Saved to reviews_output.json")


if __name__ == "__main__":
    run_pipeline()

