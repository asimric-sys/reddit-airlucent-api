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

if not GROQ_API_KEY:
    print("ERROR: GROQ_API_KEY not found in .env file")
    exit(1)

# Initialize Groq client
client = Groq(api_key=GROQ_API_KEY)

# ---------------------------------------------------------------------------
# Hardcoded target products (brand, model, category)
# ---------------------------------------------------------------------------
PRODUCTS = [
    # Air purifiers (9)
    ("Coway", "AP-1512HH Mighty", "air-purifier"),
    ("Winix", "5500-2", "air-purifier"),
    ("Levoit", "Vital 200S-P", "air-purifier"),
    ("Coway", "Airmega Mighty", "air-purifier"),
    ("Winix", "5510", "air-purifier"),
    ("Winix", "5520", "air-purifier"),
    ("CleanAirKits", "Corsi-Rosenthal Box", "air-purifier"),
    ("Levoit", "Vital 200S", "air-purifier"),
    ("Coway", "Air Mega 250", "air-purifier"),
    # Humidifiers (8)
    ("Levoit", "Superior 6000S", "humidifier"),
    ("LEVOIT", "LV600S", "humidifier"),
    ("Vornado", "Evap40", "humidifier"),
    ("Aprilaire", "800", "humidifier"),
    ("Canopy", "Humidifier 1.0", "humidifier"),
    ("Aprilaire", "Model 600", "humidifier"),
    ("AIRCARE", "SPACE SAVER 831000", "humidifier"),
    ("Venta", "LW45", "humidifier"),
    # Portable AC (8)
    ("Midea", "4-in-1 PortaSplit", "air-conditioner"),
    ("Midea", "Duo Series", "air-conditioner"),
    ("Whynter", "ARC-1230WN", "air-conditioner"),
    ("Whynter", "ARC-14S", "air-conditioner"),
    ("Whynter", "ARC-14SH", "air-conditioner"),
    ("LG Electronics", "LP1419IVSM", "air-conditioner"),
    ("Danby", "DPA120CBIMBDB", "air-conditioner"),
    ("Hisense", "AP0825TW1SAHP", "air-conditioner"),
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


def is_target(brand, product_name):
    """Return True if (brand, product_name) matches any entry in PRODUCTS.

    Matching is case-insensitive and checks whether the extracted product_name
    contains the target model string (or vice-versa) to handle minor variations.
    """
    brand_lower = (brand or "").strip().lower()
    model_lower = (product_name or "").strip().lower()
    for p_brand, p_model, _ in PRODUCTS:
        if p_brand.lower() == brand_lower:
            p_model_lower = p_model.lower()
            if p_model_lower in model_lower or model_lower in p_model_lower:
                return True
    return False


def search_reddit(brand, model, limit=20):
    """Search Reddit directly for posts about a product.

    Returns a list of post dicts with title, selftext, score, subreddit, url.
    Returns an empty list on any error — no fallback.
    """
    query = f"{brand} {model} review"
    url = "https://www.reddit.com/r/all/search.json"
    params = {"q": query, "sort": "relevance", "t": "year", "limit": limit}
    headers = {"User-Agent": get_random_user_agent()}
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=30)
        if resp.status_code != 200:
            print(f"   [WARN] Reddit returned {resp.status_code} for '{query}'")
            return []
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
        return posts
    except Exception as e:
        print(f"   [ERROR] Reddit search failed: {e}")
        return []


def extract_reviews(text, subreddit=""):
    """Extract product reviews from Reddit text using Groq (Llama 3.3).

    Returns a list of dicts with keys: sentiment, brand, product_name,
    category, verbatim.
    """
    subreddit_context = f" The post is from r/{subreddit}." if subreddit else ""

    prompt = f"""
    Extract product reviews from this Reddit text.{subreddit_context}
    Return ONLY a single JSON array. Example: [{{"sentiment": "positive", "brand": "Winix", "product_name": "5500-2", "category": "air-purifier", "verbatim": "..."}}]
    If no product review, return [].
    Do not add any text before or after the JSON array.

    Rules for brand and product_name:
    - Extract the brand (manufacturer) and product_name (model) separately.
    - If a specific model is mentioned (e.g., "Whynter ARC-14S"), extract the model name as product_name (e.g., "ARC-14S").
    - Never leave product_name empty.

    Rules for category:
    - Use one of: "air-purifier", "humidifier", "air-conditioner".
    - Default to "other" only if none of the above fit.
    - Never leave category empty.

    Text: {text[:2000]}
    """
    try:
        response = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
            max_tokens=600,
        )
        output = response.choices[0].message.content.strip()
        # Remove markdown code blocks if present
        output = output.replace("```json", "").replace("```", "").strip()
        # Extract just the JSON array
        start = output.find("[")
        end = output.rfind("]") + 1
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
                rev["product_name"] = f"{brand} product"
            # Safety net: ensure category is never empty
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
    """Main pipeline: search Reddit for each of the 25 target products,
    extract reviews with Groq, filter to matching products, and save to JSON.
    """
    print(f"[INFO] Running pipeline for {len(PRODUCTS)} target products")

    all_reviews = []

    for brand, model, category in PRODUCTS:
        print(f"\n[PRODUCT] {brand} {model} ({category})")
        posts = search_reddit(brand, model, limit=20)
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
                if not is_target(rev_brand, rev_model):
                    continue
                rev["source_query"] = f"{brand} {model} review"
                rev["subreddit"] = subreddit
                all_reviews.append(rev)
                kept += 1

            print(f"   Post {i+1}: {len(reviews)} extracted, {kept} kept (target products)")

            # Short delay between posts (1-2 seconds)
            time.sleep(random.uniform(1, 2))

        # Longer delay between products to avoid rate-limiting (3-5 seconds)
        delay = random.uniform(3, 5)
        print(f"   [DELAY] Waiting {delay:.1f}s before next product...")
        time.sleep(delay)

    print(f"\n[OK] Total reviews collected: {len(all_reviews)}")
    with open("reviews_output.json", "w", encoding="utf-8") as f:
        json.dump(all_reviews, f, indent=2, ensure_ascii=False)
    print("[FILE] Saved to reviews_output.json")


if __name__ == "__main__":
    run_pipeline()
