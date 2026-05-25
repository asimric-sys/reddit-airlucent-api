import requests
import json
import os
import random
import time
from urllib.parse import urlparse
from dotenv import load_dotenv
from groq import Groq
from ddgs import DDGS

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

# ---------------------------------------------------------------------------
# Hardcoded target products (brand, model, category)
# ---------------------------------------------------------------------------
PRODUCTS = [
    # Air purifiers
    ("Coway", "Airmega 400", "air-purifier"),
    ("Coway", "AP-1512HH", "air-purifier"),
    ("Levoit", "Core 300", "air-purifier"),
    ("Levoit", "Core 400S", "air-purifier"),
    ("Winix", "5500-2", "air-purifier"),
    ("Winix", "AM90", "air-purifier"),
    ("Blueair", "Blue Pure 211+", "air-purifier"),
    ("Blueair", "HealthProtect 7470i", "air-purifier"),
    ("Dyson", "HP07", "air-purifier"),
    ("Dyson", "HP09", "air-purifier"),
    ("Honeywell", "HPA300", "air-purifier"),
    ("IQAir", "HealthPro Plus", "air-purifier"),
    ("Austin Air", "HealthMate", "air-purifier"),
    ("Alen", "BreatheSmart 75i", "air-purifier"),
    ("Medify", "MA-40", "air-purifier"),
    ("Shark", "HE402", "air-purifier"),
    ("GermGuardian", "AC4825", "air-purifier"),
    ("PuroAir", "HEPA 14", "air-purifier"),
    ("Govee", "H7122", "air-purifier"),
    ("Philips", "AC1215", "air-purifier"),
    # Humidifiers
    ("Levoit", "Classic 300S", "humidifier"),
    ("Levoit", "OasisMist 450S", "humidifier"),
    ("Dyson", "AM10", "humidifier"),
    ("Honeywell", "HCM350W", "humidifier"),
    ("Vicks", "V745A", "humidifier"),
    ("TaoTronics", "TT-AH001", "humidifier"),
    ("Pure Enrichment", "MistAire", "humidifier"),
    ("Crane", "EE-5301", "humidifier"),
    # Portable air conditioners
    ("LG", "LP0821GSSM", "air-conditioner"),
    ("Whynter", "ARC-14S", "air-conditioner"),
    ("Black+Decker", "BPACT08WT", "air-conditioner"),
    ("Midea", "MAP08R1CWT", "air-conditioner"),
    ("Honeywell", "MO08CESWK", "air-conditioner"),
    ("De'Longhi", "PACAN125HPEKC", "air-conditioner"),
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


def is_target_product(brand, product_name):
    """Return True if (brand, product_name) matches any entry in PRODUCTS.

    Matching is case-insensitive and checks whether the extracted product_name
    contains the target model string (or vice-versa) to handle minor variations
    like "Airmega 400S" still matching "Airmega 400".
    """
    brand_lower = (brand or "").strip().lower()
    model_lower = (product_name or "").strip().lower()
    for p_brand, p_model, _ in PRODUCTS:
        if p_brand.lower() == brand_lower:
            p_model_lower = p_model.lower()
            if p_model_lower in model_lower or model_lower in p_model_lower:
                return True
    return False


def search_reddit_direct(query, limit=20):
    """Try the public Reddit JSON search endpoint.

    Returns (posts, ok) where ok=False signals a 403/rate-limit so the caller
    can switch to the DuckDuckGo fallback.
    """
    url = "https://www.reddit.com/r/all/search.json"
    params = {"q": query, "sort": "relevance", "t": "year", "limit": limit}
    headers = {"User-Agent": get_random_user_agent()}
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=30)
        if resp.status_code == 403:
            print(f"   [403] Reddit rate-limited for '{query}' — will use DDG fallback")
            return [], False
        if resp.status_code != 200:
            print(f"   [WARN] Reddit returned {resp.status_code} for '{query}'")
            return [], False
        data = resp.json()
        posts = []
        for child in data["data"]["children"]:
            post = child["data"]
            posts.append({
                "title": post["title"],
                "selftext": post["selftext"][:800],
                "score": post["score"],
                "subreddit": post.get("subreddit", ""),
                "url": post.get("url", ""),
            })
        return posts, True
    except Exception as e:
        print(f"   [ERROR] Reddit direct search failed: {e}")
        return [], False


def fetch_reddit_post_json(post_url):
    """Fetch a Reddit post's JSON representation from its URL.

    Appends '.json' to the post URL and parses the response to extract the
    post title, body, subreddit, score, and top-level comments.

    Returns a post dict on success, or None on any error.
    """
    # Normalise: strip trailing slash, remove query string, append .json
    parsed = urlparse(post_url)
    clean_path = parsed.path.rstrip("/")
    json_url = f"https://www.reddit.com{clean_path}.json"

    headers = {"User-Agent": get_random_user_agent()}
    try:
        time.sleep(random.uniform(0.5, 1.5))
        resp = requests.get(json_url, headers=headers, timeout=30)
        if resp.status_code != 200:
            print(f"      [WARN] Post JSON fetch returned {resp.status_code} for {json_url}")
            return None
        data = resp.json()
        # Reddit returns a two-element list: [post_listing, comments_listing]
        if not isinstance(data, list) or len(data) < 1:
            return None
        post_data = data[0]["data"]["children"][0]["data"]

        # Collect top-level comment bodies to enrich the text
        comments = []
        if len(data) >= 2:
            for child in data[1]["data"]["children"]:
                body = child.get("data", {}).get("body", "")
                if body and body != "[deleted]" and body != "[removed]":
                    comments.append(body[:400])
                if len(comments) >= 5:
                    break

        combined_body = post_data.get("selftext", "")[:800]
        if comments:
            combined_body += "\n\n" + "\n".join(comments)

        return {
            "title": post_data.get("title", ""),
            "selftext": combined_body,
            "score": post_data.get("score", 0),
            "subreddit": post_data.get("subreddit", ""),
            "url": post_url,
        }
    except Exception as e:
        print(f"      [ERROR] Failed to fetch post JSON for {post_url}: {e}")
        return None


def search_reddit_via_duckduckgo(brand, model, limit=10):
    """Use DuckDuckGo to find Reddit posts about a product, then fetch each
    post's JSON directly.

    Returns a list of post dicts in the same format as search_reddit_direct().
    """
    query = f'site:reddit.com "{brand} {model}" review'
    print(f"   [DDG] Searching DuckDuckGo: {query}")
    posts = []
    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=limit))
    except Exception as e:
        print(f"   [ERROR] DuckDuckGo search failed: {e}")
        return []

    for result in results:
        url = result.get("href", "")
        # Only process actual Reddit post URLs (not subreddit index pages)
        if "reddit.com/r/" not in url or "/comments/" not in url:
            continue
        print(f"      [DDG] Fetching post: {url}")
        post = fetch_reddit_post_json(url)
        if post:
            posts.append(post)

    print(f"   [DDG] Retrieved {len(posts)} posts via DuckDuckGo fallback")
    return posts


def search_reddit(brand, model, limit=20):
    """Search for Reddit posts about a product.

    Tries the direct Reddit JSON endpoint first.  If Reddit returns 403 (rate
    limit) or any other failure, falls back to DuckDuckGo + direct post fetch.
    """
    query = f'"{brand} {model}" review'
    posts, ok = search_reddit_direct(query, limit=limit)
    if ok:
        return posts
    # Fallback: DuckDuckGo
    return search_reddit_via_duckduckgo(brand, model, limit=limit)


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
    """Main pipeline: search Reddit for each target product, extract reviews,
    filter to target products only, and save to JSON.

    For each product in PRODUCTS:
    1. Try direct Reddit JSON search for '"{brand} {model}" review'.
    2. On 403 / failure, fall back to DuckDuckGo + direct post JSON fetch.
    3. Extract reviews with Groq and keep only those matching a target product.
    """
    print(f"[INFO] Running pipeline for {len(PRODUCTS)} target products")

    all_reviews = []

    for brand, model, category in PRODUCTS:
        print(f"\n[PRODUCT] {brand} {model} ({category})")
        posts = search_reddit(brand, model, limit=25)
        print(f"   Found {len(posts)} posts")

        for i, post in enumerate(posts):
            subreddit = post.get("subreddit", "")
            full_text = f"Title: {post['title']}\nBody: {post['selftext']}"
            reviews = extract_reviews(full_text, subreddit=subreddit)

            kept = 0
            for rev in reviews:
                rev_brand = (rev.get("brand") or "").strip()
                rev_model = (rev.get("product_name") or "").strip()
                # Only keep reviews that match a known target product
                if not is_target_product(rev_brand, rev_model):
                    continue
                rev["source_query"] = f"{brand} {model} review"
                rev["subreddit"] = subreddit
                all_reviews.append(rev)
                kept += 1

            print(f"   Post {i+1}: {len(reviews)} extracted, {kept} kept (target products)")

            # Short delay between posts
            time.sleep(random.uniform(1, 3))

        # Longer delay between products to avoid rate-limiting
        delay = random.uniform(3, 6)
        print(f"   [DELAY] Waiting {delay:.1f}s before next product…")
        time.sleep(delay)

    print(f"\n[OK] Total reviews collected: {len(all_reviews)}")
    with open("reviews_output.json", "w", encoding="utf-8") as f:
        json.dump(all_reviews, f, indent=2, ensure_ascii=False)
    print("[FILE] Saved to reviews_output.json")


if __name__ == "__main__":
    run_pipeline()

