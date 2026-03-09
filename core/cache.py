# core/cache.py
# In-memory cache with TTL
# Prevents repeated API calls — saves ScraperAPI credits

import time
from datetime import datetime

class Cache:
    def __init__(self):
        self._store = {}

    def set(self, key, value, ttl_seconds=60):
        self._store[key] = {
            "value": value,
            "expires": time.time() + ttl_seconds,
            "set_at": str(datetime.now()),
        }

    def get(self, key):
        item = self._store.get(key)
        if not item:
            return None
        if time.time() > item["expires"]:
            del self._store[key]
            return None
        return item["value"]

    def exists(self, key):
        return self.get(key) is not None

    def clear(self, key=None):
        if key:
            self._store.pop(key, None)
        else:
            self._store.clear()

    def age_seconds(self, key):
        item = self._store.get(key)
        if not item:
            return None
        return round(time.time() - (item["expires"] - 60))

    def info(self):
        now = time.time()
        return {
            k: {
                "expires_in": round(v["expires"] - now),
                "set_at": v["set_at"],
            }
            for k, v in self._store.items()
        }

# Global cache instance
cache = Cache()

# TTL config (seconds)
TTL = {
    "indices":  45,   # spot prices — refresh every 45s
    "options":  60,   # option chain — every 60s
    "fii_dii":  300,  # FII data — every 5 min (changes slowly)
    "breadth":  120,  # breadth — every 2 min
    "full":     60,   # full dataset — every 60s
}
