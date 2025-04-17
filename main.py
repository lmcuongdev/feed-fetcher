import asyncio
import json
import random
from typing import Any, Dict, List

import httpx
import redis
from facebook_page_scraper import Facebook_scraper
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from twikit import Client as TwitterClient
from twikit import Tweet

app = FastAPI(title="FastAPI App", description="A FastAPI application", version="1.0.0")

# Configure Redis
redis_client = redis.Redis(host="localhost", port=6379, db=0, decode_responses=True)
CACHE_TTL = 300  # 5 minutes in seconds

# Configure CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # In production, replace with specific origins
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class FacebookScraper:
    def __init__(self):
        # self.scraper = Facebook_scraper("VTV24")
        with open("rapid_api_keys.json", "r") as f:
            self.api_keys = json.load(f)

    async def get_page_id(self, page_url: str) -> str:
        """Get Facebook page ID from page URL"""
        # Check cache first
        cache_key = f"fb_page_id:{page_url}"
        cached_id = redis_client.get(cache_key)
        if cached_id:
            print(f"Cache hit for Facebook page {page_url} with id {cached_id}")
            return cached_id
        print(f"Cache MISS for Facebook page {page_url}")

        selected_api_key = random.choice(self.api_keys)

        # Call Facebook API to get page ID
        url = f"https://facebook-scraper3.p.rapidapi.com/page/page_id"
        headers = {
            "x-rapidapi-host": "facebook-scraper3.p.rapidapi.com",
            "x-rapidapi-key": selected_api_key,
        }
        params = {"url": page_url}

        async with httpx.AsyncClient() as client:
            response = await client.get(url, headers=headers, params=params)
            response.raise_for_status()

            # Return the page ID from response
            page_id = response.json()["page_id"]
            redis_client.set(cache_key, page_id)
            return page_id
    
    async def _get_posts_by_page_id(
        self, page_id: str
    ) -> List[Dict[str, Any]]:
        """Fetch posts from a Facebook page"""
        
        cache_key = f"fb_posts:{page_id}"
        cached_posts = redis_client.get(cache_key)
        if cached_posts:
            print(f"Cache hit for Facebook posts from {page_id}")
            return json.loads(cached_posts)
        print(f"Cache MISS for Facebook posts from page ID {page_id}")
        
        selected_api_key = random.choice(self.api_keys)
        url = f"https://facebook-scraper3.p.rapidapi.com/page/posts"
        headers = {
            "x-rapidapi-host": "facebook-scraper3.p.rapidapi.com",
            "x-rapidapi-key": selected_api_key,
        }
        params = {"page_id": page_id}

        async with httpx.AsyncClient() as client:
            response = await client.get(url, headers=headers, params=params)
            response.raise_for_status()

            res = response.json()['results']
            redis_client.setex(cache_key, CACHE_TTL, json.dumps(res))
            return res

    async def get_page_posts(
        self, page_url: str, post_count: int = 3
    ) -> List[Dict[str, Any]]:
        """Fetch posts from a Facebook page"""

        try:
            page_id = await self.get_page_id(page_url)
            # By default this returns 3 posts
            posts = await self._get_posts_by_page_id(page_id)
            
            results = []
            for post in posts[:post_count]:
                media = []
                if post.get('image'):
                    media.append(post['image'].get('uri'))
                if post.get('video_files'):
                    media.append(post['video_files'].get('video_hd_file'))
                if post.get('album_preview'):
                    media.extend([image_obj['image_file_uri'] for image_obj in post['album_preview']])
                obj = {
                    'url': post.get('url', ''),
                    'content': post.get('message', ''),
                    'media': media
                }
                results.append(obj)
            return {"url": page_url, "posts": results}
        except Exception as e:
            print(f"Error scraping Facebook page {page_url}: {str(e)}")
            return {"url": page_url, "posts": []}


class TwitterScraper:
    def __init__(self):
        # Initialize with guest session since we're just reading public data
        self.client = TwitterClient("en-US")

    async def setup(self):
        # TODO: If loading failed then login
        # Potential issue when logging in: "In order to protect your account from suspicious activity, we've sent a confirmation code to cu*********@g****.***. Enter it below to sign in."
        self.client.load_cookies("x_cookies.json")

    def format_content(self, tweet: Tweet) -> str:
        content = (
            tweet.retweeted_tweet.full_text
            if tweet.retweeted_tweet
            else tweet.full_text
        )
        if tweet.quote:
            content = f"""{content}
----- QUOTED_TWEET -----
{tweet.quote.full_text}
-----"""
        return content

    async def get_user_posts(
        self, username: str, post_count: int = 3
    ) -> Dict[str, Any]:
        # Check cache first
        cache_key = f"twitter:{username}"
        cached_data = redis_client.get(cache_key)
        if cached_data:
            print(f"Cache hit for X/{username}")
            return json.loads(cached_data)
        print(f"Cache miss for X/{username}")

        await self.setup()
        """Fetch posts from a Twitter/X user"""
        try:
            # Get user info first
            user = await self.client.get_user_by_screen_name(username)
            if not user:
                return {"url": f"https://x.com/{username}", "posts": []}

            # Get recent tweets
            tweets = await user.get_tweets("Tweets", count=post_count)

            # Format tweets
            posts = []
            for tweet in tweets[:3]:
                posts.append(
                    {
                        "url": f"https://x.com/{username}/status/{tweet.id}",
                        "content": self.format_content(tweet),
                        "media": [media.media_url for media in tweet.media],
                    }
                )

            result = {"url": f"https://x.com/{username}", "posts": posts}

            # Cache the result
            redis_client.setex(cache_key, CACHE_TTL, json.dumps(result))

            return result
        except Exception as e:
            print(f"Error scraping Twitter user {username}: {str(e)}")
            return {"url": f"https://x.com/{username}", "posts": []}


# Initialize scrapers
facebook_scraper = FacebookScraper()
twitter_scraper = TwitterScraper()


@app.get("/")
async def root():
    return {"message": "Welcome to FastAPI"}


@app.post("/api/fetch")
async def receive_payload(request: Request):
    raw_payload = await request.body()
    raw_payload_string = raw_payload.decode()
    urls = raw_payload_string.strip().split("\n")
    urls = [url.strip() for url in urls if url.strip()]

    results = []

    for url in urls:
        if url.startswith("https://x.com/") or url.startswith("https://twitter.com/"):
            # Extract username from Twitter URL
            username = url.split("/")[-1]
            result = await twitter_scraper.get_user_posts(username)
            results.append(result)

        elif url.startswith("https://www.facebook.com/"):
            # Extract page name from Facebook URL
            # page_name = url.split("/")[-1]
            result = await facebook_scraper.get_page_posts(url)
            results.append(result)

        else:
            print(f"Invalid URL: {url}")
            # raise ValueError(f"Invalid URL: {url}")

    return results


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
