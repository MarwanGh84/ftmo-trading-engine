"""bars_cache — TTL policy and cache hit/miss behavior (no network)."""
import json
import time

from engine import bars_cache


class _FakeClient:
    def __init__(self):
        self.calls = 0

    def call(self, tool, params):
        self.calls += 1
        return {"bars": [{"close": 1.0}], "truncated": False}


def _clear(symbol, tf, days, limit):
    p = bars_cache._path(symbol, tf, days, limit)
    if p.exists():
        p.unlink()


def test_d1_second_fetch_served_from_cache():
    c = _FakeClient()
    _clear("TESTPAIR", "d1", 30, 25)
    r1 = bars_cache.get_bars(c, "TESTPAIR", "d1", 30, 25)
    r2 = bars_cache.get_bars(c, "TESTPAIR", "d1", 30, 25)
    assert c.calls == 1
    assert r1 == r2
    _clear("TESTPAIR", "d1", 30, 25)


def test_h1_never_cached():
    c = _FakeClient()
    bars_cache.get_bars(c, "TESTPAIR", "h1", 3, 48)
    bars_cache.get_bars(c, "TESTPAIR", "h1", 3, 48)
    assert c.calls == 2


def test_expired_cache_refetches():
    c = _FakeClient()
    _clear("TESTPAIR", "d1", 30, 25)
    bars_cache.get_bars(c, "TESTPAIR", "d1", 30, 25)
    # Age the cache entry past the TTL
    p = bars_cache._path("TESTPAIR", "d1", 30, 25)
    cached = json.loads(p.read_text())
    cached["ts"] = time.time() - bars_cache._TTL["d1"] - 1
    p.write_text(json.dumps(cached))
    bars_cache.get_bars(c, "TESTPAIR", "d1", 30, 25)
    assert c.calls == 2
    _clear("TESTPAIR", "d1", 30, 25)


def test_corrupt_cache_file_refetches():
    c = _FakeClient()
    p = bars_cache._path("TESTPAIR", "d1", 30, 25)
    bars_cache._CACHE_DIR.mkdir(parents=True, exist_ok=True)
    p.write_text("not json{{{")
    r = bars_cache.get_bars(c, "TESTPAIR", "d1", 30, 25)
    assert c.calls == 1
    assert r["bars"]
    _clear("TESTPAIR", "d1", 30, 25)
