import httpx
import logging
import json

logger = logging.getLogger(__name__)

BASE_URL = "https://dramawave.dramabos.online/api"
TOKEN = "A8D6AB170F7B89F2182561D3B32F390D"

async def get_popular_feed(page=1):
    """Fetches popular/recommended dramas from the new DramaWave API."""
    # We can use /recommend or /home. /recommend works well across pages if needed.
    # The API documentation says 'next' for pagination (ex: 10, 20, 40...)
    # We will just fetch without next for the main feed, or use home.
    # We'll fetch from /home and flatten it.
    url = f"{BASE_URL}/home"
    params = {"lang": "in"}
    
    async with httpx.AsyncClient(timeout=None) as client:
        try:
            response = await client.get(url, params=params)
            response.raise_for_status()
            data = response.json()
            
            dramas = []
            modules = data.get("data", [])
            for module in modules:
                if "items" in module and isinstance(module["items"], list):
                    for item in module["items"]:
                        if item.get("playlet_id") or item.get("id"):
                            dramas.append(item)
            return dramas
        except Exception as e:
            logger.error(f"Error fetching popular feed: {e}")
            return []

async def get_drama_detail(drama_id: str):
    """Fetches drama detail and ALL episodes from new DramaWave API."""
    url = f"{BASE_URL}/drama/{drama_id}"
    params = {
        "lang": "in",
        "code": TOKEN
    }
    
    async with httpx.AsyncClient(timeout=None) as client:
        try:
            response = await client.get(url, params=params)
            response.raise_for_status()
            data = response.json()
            return data.get("data")
        except Exception as e:
            logger.error(f"Error fetching drama detail for {drama_id}: {e}")
            return None

async def search_drama(query: str):
    """Searches for a drama by name."""
    url = f"{BASE_URL}/search"
    params = {
        "q": query,
        "lang": "in"
    }
    
    async with httpx.AsyncClient(timeout=None) as client:
        try:
            response = await client.get(url, params=params)
            response.raise_for_status()
            data = response.json()
            return data.get("data", {}).get("items", [])
        except Exception as e:
            logger.error(f"Error searching drama '{query}': {e}")
            return []

# The episode data is now included in the detail.
# We modify get_episode_data to just return an episode from the detail since it's already fetched,
# or we just remove it and refactor main.py to use detail["items"].
async def get_episode_data(drama_id: str, ep_num: int):
    """Fetches episode data. Since all episodes are in detail, we must fetch detail first."""
    detail = await get_drama_detail(drama_id)
    if not detail or "items" not in detail:
        return None
    
    # ep_num is 1-indexed. Let's find the episode in the items list.
    # Usually they are ordered or we use index.
    items = detail.get("items", [])
    if ep_num - 1 < len(items):
        return items[ep_num - 1]
    return None
