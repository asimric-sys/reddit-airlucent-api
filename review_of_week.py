# review_of_week.py
import requests
import os
import datetime
from dotenv import load_dotenv

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
headers = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json"
}

def run():
    # Calculate start of current week (Monday)
    today = datetime.date.today()
    start_of_week = today - datetime.timedelta(days=today.weekday())
    
    # Get the review with the highest helpful_score from the last 7 days
    url = f"{SUPABASE_URL}/rest/v1/reviews?select=id,helpful_score&created_at=gte.{start_of_week}&order=helpful_score.desc&limit=1"
    resp = requests.get(url, headers=headers)
    
    if resp.status_code == 200 and resp.json():
        best_review = resp.json()[0]
        # Upsert into weekly_review table (overwrite if week already exists)
        upsert_url = f"{SUPABASE_URL}/rest/v1/weekly_review"
        data = {"review_id": best_review["id"], "week_start": start_of_week.isoformat()}
        post_resp = requests.post(upsert_url, headers=headers, json=data, params={"on_conflict": "week_start"})
        if post_resp.status_code == 201:
            print(f"Weekly review set: {best_review['id']}")
        else:
            print(f"Failed to upsert: {post_resp.text}")
    else:
        print("No reviews this week")

if __name__ == "__main__":
    run()