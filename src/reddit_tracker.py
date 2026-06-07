#!/usr/bin/env python3
"""
BTC Genius — Reddit sentiment tracker (launchd-scheduled).

Polls Reddit's public JSON endpoints for crypto subreddits and maps the chatter
onto the SIGNALS.md framework:

  - capitulation bucket  -> evidence for B-column (seller exhaustion)
  - bottom_calling bucket-> contrarian context (everyone calling bottom = not yet)
  - froth bucket         -> evidence for X-column (exit/froth signals)
  - macro_chain bucket   -> retail awareness of the A-column macro chain
  - rotation bucket      -> the "momentum left crypto for AI/IPOs" thesis

Outputs (under DATA_DIR):
  snapshots.jsonl  - one JSON line per run (the time series; spike detection input)
  latest.md        - human-readable digest of the most recent run
Logs to LOG_FILE. Stdlib only; Python 3.9+.

Auth: prefers Reddit's official OAuth API (immune to the public-JSON IP blocks).
One-time setup:
  1. https://www.reddit.com/prefs/apps -> "create another app" -> type "script"
     (name: btc-genius, redirect uri: http://localhost:8080 — unused but required)
  2. Copy the client id (under the app name) and secret into .reddit_credentials:
       {"client_id": "...", "client_secret": "..."}
Without credentials it falls back to the public .json endpoints.
"""

import base64
import json
import logging
import os
import re
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

import db
from env_loader import load_env

load_env()

# --- Config -----------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent.parent  # src/ -> project root
DATA_DIR = BASE_DIR / "data" / "reddit"
LOG_DIR = BASE_DIR / "logs"
SNAPSHOTS = DATA_DIR / "snapshots.jsonl"
DIGEST = DATA_DIR / "latest.md"
LOG_FILE = LOG_DIR / "reddit_tracker.log"

SUBREDDITS = ["Bitcoin", "CryptoCurrency", "BitcoinMarkets", "wallstreetbets"]

# Reddit blocks some IPs/fingerprints; try several hosts before giving up.
ENDPOINT_TEMPLATES = [
    "https://www.reddit.com/r/{sub}/{listing}.json?limit={limit}&raw_json=1",
    "https://old.reddit.com/r/{sub}/{listing}.json?limit={limit}&raw_json=1",
    "https://api.reddit.com/r/{sub}/{listing}?limit={limit}&raw_json=1",
]
CREDENTIALS_FILE = BASE_DIR / ".reddit_credentials"
TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
OAUTH_TEMPLATE = "https://oauth.reddit.com/r/{sub}/{listing}?limit={limit}&raw_json=1"
USER_AGENT = "macos:btc-genius:v0.1 (personal market dashboard)"
REQUEST_GAP_SECONDS = 2.0  # politeness between requests
TIMEOUT = 20

# Keyword buckets — lowercase substrings/regexes matched against title+selftext.
BUCKETS = {
    "capitulation": [
        "capitulat", "sold everything", "i'm out", "im out", "giving up",
        "i give up", "cut my losses", "down bad", "wiped out", "lost everything",
        "never recover", "it's over", "its over", "dead cat",
    ],
    "bottom_calling": [
        "bottom is in", "the bottom", "buy the dip", "btfd", "accumulat",
        "oversold", "discount", "fire sale", "generational", "max pain",
    ],
    "froth": [
        "all time high", "new ath", "to the moon", "parabolic", "supercycle",
        "100x", "lambo", "going to 1 million", "leveraged long", "all in",
    ],
    "macro_chain": [
        "oil", "brent", "crude", "treasury", "10-year", "10 year", "yield",
        "rate hike", "rate cut", "fed ", "fomc", "inflation", "cpi", "dxy",
        "strait of hormuz", "middle east",
    ],
    "rotation": [
        "saylor", "microstrategy", "strategy sold", "etf outflow", "ibit",
        "blackrock", "ipo", "spacex", "openai", "anthropic", "ai stocks",
        "nvidia", "momentum", "tokeniz",
    ],
}

# Spike detection: compare current bucket counts to the trailing mean of the
# last N snapshots; flag when count >= SPIKE_MULT * mean (and above a floor).
TRAILING_N = 48          # ~16h of history at a 20-min cadence
SPIKE_MULT = 2.0
SPIKE_FLOOR = 5

# --- Fetching ----------------------------------------------------------------

_oauth_token = None  # fetched once per run


def get_reddit_creds():
    """Client id/secret from .env (preferred) or legacy .reddit_credentials."""
    cid, csec = os.environ.get("REDDIT_CLIENT_ID"), os.environ.get("REDDIT_CLIENT_SECRET")
    if cid and csec:
        return {"client_id": cid, "client_secret": csec}
    if CREDENTIALS_FILE.exists():
        return json.loads(CREDENTIALS_FILE.read_text())
    return None


def get_oauth_token():
    """Application-only OAuth token (client_credentials grant). None if no creds."""
    global _oauth_token
    if _oauth_token is not None:
        return _oauth_token or None  # "" sentinel = previously failed this run
    try:
        creds = get_reddit_creds()
        if not creds:
            _oauth_token = ""
            return None
        basic = base64.b64encode(
            f"{creds['client_id']}:{creds['client_secret']}".encode()).decode()
        req = urllib.request.Request(
            TOKEN_URL,
            data=b"grant_type=client_credentials",
            headers={"User-Agent": USER_AGENT,
                     "Authorization": f"Basic {basic}"})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            _oauth_token = json.load(resp)["access_token"]
            logging.info("oauth token acquired")
            return _oauth_token
    except Exception as e:  # noqa: BLE001 - any auth failure -> public fallback
        logging.warning("oauth token fetch failed (%s); using public endpoints", e)
        _oauth_token = ""
        return None


def fetch_listing(sub, listing, limit):
    """Fetch a subreddit listing: OAuth API first, public endpoints as fallback."""
    last_err = None
    token = get_oauth_token()
    if token:
        url = OAUTH_TEMPLATE.format(sub=sub, listing=listing, limit=limit)
        req = urllib.request.Request(url, headers={
            "User-Agent": USER_AGENT,
            "Authorization": f"Bearer {token}",
        })
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                payload = json.load(resp)
                return [c["data"] for c in payload["data"]["children"]]
        except (urllib.error.HTTPError, urllib.error.URLError,
                json.JSONDecodeError, KeyError) as e:
            last_err = e
            logging.warning("oauth fetch failed: %s -> %s", url, e)
            time.sleep(REQUEST_GAP_SECONDS)
    for template in ENDPOINT_TEMPLATES:
        url = template.format(sub=sub, listing=listing, limit=limit)
        req = urllib.request.Request(url, headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                payload = json.load(resp)
                return [c["data"] for c in payload["data"]["children"]]
        except (urllib.error.HTTPError, urllib.error.URLError,
                json.JSONDecodeError, KeyError) as e:
            last_err = e
            logging.warning("fetch failed: %s -> %s", url, e)
            time.sleep(REQUEST_GAP_SECONDS)
    raise RuntimeError(f"all endpoints failed for r/{sub}/{listing}: {last_err}")


# --- Analysis ----------------------------------------------------------------


def bucket_hits(posts):
    """Count keyword-bucket matches across posts; return counts + matched posts."""
    counts = {name: 0 for name in BUCKETS}
    matches = {name: [] for name in BUCKETS}
    for post in posts:
        text = (post.get("title", "") + " " + post.get("selftext", "")).lower()
        for name, needles in BUCKETS.items():
            if any(n in text for n in needles):
                counts[name] += 1
                matches[name].append({
                    "title": post.get("title", "")[:140],
                    "score": post.get("score", 0),
                    "comments": post.get("num_comments", 0),
                    "permalink": "https://reddit.com" + post.get("permalink", ""),
                })
    for name in matches:  # keep only the highest-engagement examples
        matches[name].sort(key=lambda p: p["score"] + p["comments"], reverse=True)
        matches[name] = matches[name][:3]
    return counts, matches


def posts_per_hour(new_posts):
    """Posting velocity from a /new listing: posts created in the last hour."""
    cutoff = time.time() - 3600
    return sum(1 for p in new_posts if p.get("created_utc", 0) >= cutoff)


def trailing_means(snapshot_path, n):
    """Mean bucket counts over the last n snapshots (for spike detection)."""
    if not snapshot_path.exists():
        return {}
    lines = snapshot_path.read_text().strip().splitlines()[-n:]
    sums, count = {}, 0
    for line in lines:
        try:
            snap = json.loads(line)
        except json.JSONDecodeError:
            continue
        count += 1
        for name, val in snap.get("totals", {}).get("buckets", {}).items():
            sums[name] = sums.get(name, 0) + val
    if count == 0:
        return {}
    return {name: total / count for name, total in sums.items()}


# --- Output ------------------------------------------------------------------


def write_digest(snapshot, spikes, top_matches):
    ts = snapshot["fetched_at"]
    lines = [
        "# Reddit Sentiment Digest",
        "",
        f"_Generated {ts} — cadence ~20 min via launchd_",
        "",
    ]
    if spikes:
        lines.append("## ⚠ Spikes vs trailing average")
        lines.append("")
        for name, (cur, mean) in spikes.items():
            lines.append(f"- **{name}**: {cur} hits vs {mean:.1f} avg "
                         f"({cur / mean:.1f}x) — check SIGNALS.md mapping")
        lines.append("")
    lines.append("## Bucket totals (all subreddits)")
    lines.append("")
    lines.append("| Bucket | Hits | Signal mapping |")
    lines.append("|---|---|---|")
    mapping = {
        "capitulation": "B-column: seller exhaustion evidence",
        "bottom_calling": "Contrarian: crowded bottom-calls = early",
        "froth": "X-column: exit/froth evidence",
        "macro_chain": "A-column context: retail sees the macro chain",
        "rotation": "Momentum-rotation thesis (Schwab)",
    }
    for name, val in snapshot["totals"]["buckets"].items():
        lines.append(f"| {name} | {val} | {mapping.get(name, '')} |")
    lines.append("")
    lines.append("## Per-subreddit activity")
    lines.append("")
    lines.append("| Subreddit | Posts/hr (new) | Top bucket |")
    lines.append("|---|---|---|")
    for sub, stats in snapshot["subreddits"].items():
        if "error" in stats:
            lines.append(f"| r/{sub} | — | fetch error: {stats['error'][:60]} |")
            continue
        top = max(stats["buckets"], key=stats["buckets"].get)
        lines.append(f"| r/{sub} | {stats['posts_per_hour']} | "
                     f"{top} ({stats['buckets'][top]}) |")
    lines.append("")
    lines.append("## Highest-engagement matched posts")
    lines.append("")
    for name, posts in top_matches.items():
        if not posts:
            continue
        lines.append(f"### {name}")
        for p in posts:
            lines.append(f"- [{p['title']}]({p['permalink']}) "
                         f"(▲{p['score']}, {p['comments']} comments)")
        lines.append("")
    DIGEST.write_text("\n".join(lines))


# --- Main --------------------------------------------------------------------


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=str(LOG_FILE), level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    logging.info("run start")

    snapshot = {
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "subreddits": {},
        "totals": {"buckets": {name: 0 for name in BUCKETS}},
    }
    all_matches = {name: [] for name in BUCKETS}
    failures = 0

    for sub in SUBREDDITS:
        try:
            hot = fetch_listing(sub, "hot", 50)
            time.sleep(REQUEST_GAP_SECONDS)
            new = fetch_listing(sub, "new", 100)
            time.sleep(REQUEST_GAP_SECONDS)
        except RuntimeError as e:
            logging.error("r/%s unavailable: %s", sub, e)
            snapshot["subreddits"][sub] = {"error": str(e)}
            failures += 1
            continue

        counts, matches = bucket_hits(hot + new)
        snapshot["subreddits"][sub] = {
            "posts_per_hour": posts_per_hour(new),
            "hot_count": len(hot),
            "buckets": counts,
        }
        for name in BUCKETS:
            snapshot["totals"]["buckets"][name] += counts[name]
            all_matches[name].extend(matches[name])

    # Spike detection against trailing history (before appending this run).
    means = trailing_means(SNAPSHOTS, TRAILING_N)
    spikes = {}
    for name, cur in snapshot["totals"]["buckets"].items():
        mean = means.get(name, 0)
        if mean > 0 and cur >= SPIKE_FLOOR and cur >= SPIKE_MULT * mean:
            spikes[name] = (cur, mean)
    snapshot["spikes"] = {k: {"count": v[0], "trailing_mean": round(v[1], 2)}
                          for k, v in spikes.items()}

    for name in all_matches:
        all_matches[name].sort(key=lambda p: p["score"] + p["comments"],
                               reverse=True)
        all_matches[name] = all_matches[name][:3]

    with SNAPSHOTS.open("a") as f:
        f.write(json.dumps(snapshot) + "\n")
    db.save_reddit_snapshot(snapshot)
    write_digest(snapshot, spikes, all_matches)

    if failures == len(SUBREDDITS):
        logging.error("run end: ALL subreddits failed — likely IP block (VPN?)")
    else:
        logging.info("run end: %d/%d subreddits ok, buckets=%s, spikes=%s",
                     len(SUBREDDITS) - failures, len(SUBREDDITS),
                     snapshot["totals"]["buckets"], list(spikes) or "none")


if __name__ == "__main__":
    main()
