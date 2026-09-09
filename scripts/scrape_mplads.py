"""Scrape the full MPLADS dataset (MPs + their recommended/completed works) from
the Empowered Indian public API into data/raw_works.json and data/mps.json.

Source: https://api.empoweredindian.in/api (backend of https://empoweredindian.in/mplads)
"""

import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

API = "https://api.empoweredindian.in/api"
DATA = Path(__file__).resolve().parent.parent / "data"
CACHE = DATA / "cache"
HEADERS = {
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/139.0 Safari/537.36",
}
SESSION_POOL = {}
RATE_LIMIT_RPS = 1.5  # API allows 1000 requests / 600s per IP
_rate_lock = __import__("threading").Lock()
_next_slot = [0.0]


def throttle():
    """Global token bucket: the API rejects sustained bursts with HTTP 429."""
    with _rate_lock:
        now = time.monotonic()
        slot = max(now, _next_slot[0])
        _next_slot[0] = slot + 1.0 / RATE_LIMIT_RPS
    delay = slot - now
    if delay > 0:
        time.sleep(delay)


def session():
    import threading

    tid = threading.get_ident()
    if tid not in SESSION_POOL:
        s = requests.Session()
        s.headers.update(HEADERS)
        SESSION_POOL[tid] = s
    return SESSION_POOL[tid]


def get(path, params=None, retries=8):
    last = None
    attempt = 0
    while attempt < retries:
        try:
            throttle()
            r = session().get(f"{API}{path}", params=params, timeout=120)
            if r.status_code == 429:
                wait = float(r.headers.get("Retry-After", 0) or 0) or min(60, 5 * 2**attempt)
                print(f"  429 rate limited, sleeping {wait:.0f}s", flush=True)
                time.sleep(wait + 5)
                continue  # rate limiting is not a failure; do not burn a retry
            r.raise_for_status()
            payload = r.json()
            if not payload.get("success"):
                raise RuntimeError(payload.get("error", "unknown API error"))
            return payload["data"]
        except Exception as exc:  # noqa: BLE001 - retry any transport/API error
            last = exc
            attempt += 1
            time.sleep(1.5 * attempt)
    raise RuntimeError(f"GET {path} {params} failed: {last}")


def fetch_mps():
    mps, page = [], 1
    while True:
        data = get("/summary/mps", {"page": page, "limit": 100})
        rows = data if isinstance(data, list) else data.get("mps", [])
        mps.extend(rows)
        if len(rows) < 100:
            break
        page += 1
    return mps


def fetch_mp_works(mp):
    """All works for one MP, both completed and still-recommended (cached on disk)."""
    cache = CACHE / f"{mp['id']}.json"
    if cache.exists():
        return json.loads(cache.read_text())
    out = []
    for status in ("completed", "recommended"):
        page = 1
        while True:
            data = get(
                f"/mplads/mps/{mp['id']}/works",
                {"status": status, "page": page, "limit": 100},
            )
            works = data.get("works", [])
            for w in works:
                w["mp_id"] = mp["id"]
                w["mpName"] = w.get("mpName") or mp["mpName"]
                w["state"] = w.get("state") or mp["state"]
                w["constituency"] = w.get("constituency") or mp["constituency"]
            out.extend(works)
            pagination = data.get("pagination", {})
            if not works or page >= pagination.get("pages", 1):
                break
            page += 1
    cache.write_text(json.dumps(out))
    return out


def main():
    DATA.mkdir(parents=True, exist_ok=True)
    CACHE.mkdir(parents=True, exist_ok=True)
    print("Fetching MP list...")
    mp_file = DATA / "mps.json"
    mps = json.loads(mp_file.read_text()) if mp_file.exists() else fetch_mps()
    print(f"  {len(mps)} MPs", flush=True)
    mp_file.write_text(json.dumps(mps, indent=1))

    works, done = [], 0
    with ThreadPoolExecutor(max_workers=12) as pool:
        for rows in pool.map(fetch_mp_works, mps):
            works.extend(rows)
            done += 1
            if done % 25 == 0:
                print(f"  {done}/{len(mps)} MPs, {len(works)} works", flush=True)
    print(f"Fetched {len(works)} works")
    (DATA / "raw_works.json").write_text(json.dumps(works))
    print(f"Saved -> {DATA / 'raw_works.json'}")


if __name__ == "__main__":
    main()
