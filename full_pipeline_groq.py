import requests
import json
import os
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

def search_reddit(query, limit=20):
    """Search Reddit using public JSON endpoint with proper User-Agent."""
    url = "https://www.reddit.com/r/all/search.json"
    params = {"q": query, "sort": "relevance", "t": "year", "limit": limit}
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 RedditRecs/1.0"
    }
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=30)
        if resp.status_code != 200:
            print(f"Reddit API returned {resp.status_code} for query '{query}'")
            return []
        data = resp.json()
        posts = []
        for child in data["data"]["children"]:
            post = child["data"]
            posts.append({
                "title": post["title"],
                "selftext": post["selftext"][:800],
                "score": post["score"]
            })
        return posts
    except Exception as e:
        print(f"Error searching Reddit: {e}")
        return []

def extract_reviews(text):
    """
    Extract product reviews from Reddit text using Groq (Llama 3.3).
    Returns a list of dicts with keys: sentiment, brand, product_name, verbatim.
    """
    prompt = f"""
    Extract product reviews from this Reddit text.
    Return ONLY a single JSON array. Example: [{{"sentiment": "positive", "brand": "Dyson", "product_name": "HP07", "verbatim": "..."}}]
    If no product review, return [].
    Do not add any text before or after the JSON array.

    Rules for brand and product_name:
    - Extract the brand (manufacturer) and product_name (model) separately.
    - If a specific model is mentioned (e.g., "Coway Airmega 400"), extract the model name as product_name (e.g., "Airmega 400").
    - If only the brand is mentioned without a specific model, set product_name to "{{brand}} Air Purifier" (e.g., "Coway Air Purifier").
    - Never leave product_name empty.

    Text: {text[:2000]}
    """
    try:
        response = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,           # Low temp for consistent output
            max_tokens=500
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
        # Safety net: if Groq returned an empty product_name despite the prompt,
        # fall back to "{brand} Air Purifier" so no record is left incomplete.
        for rev in reviews:
            brand = (rev.get("brand") or "").strip()
            product_name = (rev.get("product_name") or "").strip()
            if brand and not product_name:
                rev["product_name"] = f"{brand} Air Purifier"
        return reviews
    except json.JSONDecodeError as e:
        print(f"JSON decode error: {e}\nRaw output: {output[:200]}")
        return []
    except Exception as e:
        print(f"Extraction error: {e}")
        return []

def run_pipeline():
    """Main pipeline: search Reddit, extract reviews, save to JSON."""
    queries = [
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
        # Use-case specific queries (NEW)
        "air purifier smoke review",
        "air purifier for pets",
        "best air purifier for allergies",
        "quiet air purifier bedroom",
        "large room air purifier",
        "air purifier energy efficient",
        "smart air purifier wifi",
        "robot vacuum for pet hair",
        "smart thermostat energy saving",
        "humidifier for dry air"
    ]
    all_reviews = []

    for q in queries:
        print(f"\n[SEARCH] Searching: {q}")
        posts = search_reddit(q, limit=25)

        print(f"   Found {len(posts)} posts")
        for i, post in enumerate(posts):
            full_text = f"Title: {post['title']}\nBody: {post['selftext']}"
            reviews = extract_reviews(full_text)
            for rev in reviews:
                rev["source_query"] = q
                all_reviews.append(rev)
            print(f"   Post {i+1}: {len(reviews)} reviews")
        time.sleep(1)  # Small delay between queries to be gentle to Reddit

    print(f"\n[OK] Total reviews collected: {len(all_reviews)}")
    with open("reviews_output.json", "w", encoding="utf-8") as f:
        json.dump(all_reviews, f, indent=2, ensure_ascii=False)
    print("[FILE] Saved to reviews_output.json")

if __name__ == "__main__":
    run_pipeline()
