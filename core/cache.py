# core/cache.py
# In-memory TTL cache — matches server.py v3.2 _cget/_cset pattern
# Drop-in compatible with both dict API and class API

import time

_cache: dict = {}

def _cget(key: str, ttl: int = 60):
    """Get cached value if not expired. Returns None on miss/expiry."""
    e = _cache.get(key)
    if not e or time.time() - e["ts"] > ttl:
        return None
    return e["d"]

def _cset(key: str, d, ttl: int = None):
    """Store value in cache."""
    _cache[key] = {"ts": time.time(), "d": d}

def _cdel(key: str):
    """Delete a cache entry."""
    _cache.pop(key, None)

def _cclear():
    """Clear entire cache."""
    _cache.clear()

def _cinfo() -> dict:
    """Return cache key info for debugging."""
    now = time.time()
    return {
        k: {"age_s": round(now - v["ts"], 1)}
        for k, v in _cache.items()
    }

# ── TTL constants (seconds) ─────────────────────────────
TTL = {
    "idx":       10,   # live index prices
    "chain":     60,   # option chain
    "fii":      300,   # FII/DII (changes slowly)
    "news":     180,   # news headlines
    "signal":   180,   # generated signal
    "ltp":        5,   # live LTP from Kite
}
