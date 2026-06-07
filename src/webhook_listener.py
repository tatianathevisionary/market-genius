#!/usr/bin/env python3
"""
BTC Genius — TradingView alert webhook listener (launchd-managed daemon).

Receives TradingView alert webhooks and appends them to data/alerts.jsonl,
so chart signals (btc_genius.pine alertconditions) land in the same local
data store as the Reddit sentiment tracker.

Endpoints:
  POST /hook/<secret>   - record an alert (JSON body preferred, raw text ok)
  GET  /health          - liveness check
  GET  /dashboard       - chart dashboard UI (dashboard.html)
  GET  /data/<file>     - read-only JSON from data/ (jsonl served as JSON array)

Security: requests must include the shared secret in the URL path. The secret
lives in .env as WEBHOOK_SECRET (chmod 600; auto-generated on first run;
legacy .webhook_secret still honored as a fallback).

Exposure: TradingView must reach this machine — run a tunnel in front, e.g.:
  ngrok http 8787
  cloudflared tunnel --url http://localhost:8787
Then use https://<tunnel-host>/hook/<secret> as the TradingView webhook URL.

Stdlib only; Python 3.9+.
"""

import html
import json
import logging
import os
import re
import secrets
import sqlite3
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import db
from env_loader import ENV_FILE, load_env

load_env()

BASE_DIR = Path(__file__).resolve().parent.parent  # src/ -> project root
DATA_DIR = BASE_DIR / "data"
ALERTS = DATA_DIR / "alerts.jsonl"
SECRET_FILE = BASE_DIR / ".webhook_secret"
LOG_FILE = BASE_DIR / "logs" / "webhook_listener.log"

HOST = "127.0.0.1"   # tunnel runs locally; never bind this to 0.0.0.0 directly
PORT = 8787
MAX_BODY = 64 * 1024  # TradingView alerts are small; reject anything bigger

# News wire: free RSS feeds proxied for the dashboard (avoids browser CORS).
NEWS_FEEDS = {
    "CoinDesk": "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "Cointelegraph": "https://cointelegraph.com/rss",
    "Decrypt": "https://decrypt.co/feed",
}
NEWS_CACHE_SECONDS = 300
_news_cache = {"at": 0.0, "items": []}

# Files whose freshness defines system status (label -> (path, stale_seconds)).
STATUS_FILES = {
    "market_collector": ("market/latest.json", 15 * 60),
    "signal_engine": ("state.json", 2 * 60 * 60),
    "reddit_tracker": ("reddit/snapshots.jsonl", 40 * 60),
    "tv_alerts": ("alerts.jsonl", None),  # event-driven; age shown, never "down"
}


def fetch_news():
    """Fetch + parse RSS feeds into a single sorted list (cached)."""
    now = time.time()
    if now - _news_cache["at"] < NEWS_CACHE_SECONDS:
        return _news_cache["items"]
    def tag(block, name):
        m = re.search(rf"<{name}[^>]*>(.*?)</{name}>", block, re.S)
        if not m:
            return ""
        val = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", m.group(1), flags=re.S)
        return html.unescape(re.sub(r"<[^>]+>", "", val)).strip()

    items = []
    for source, url in NEWS_FEEDS.items():
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "btc-genius/0.1"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                xml = resp.read().decode("utf-8", errors="replace")
            for m in re.finditer(r"<item>(.*?)</item>", xml, re.S):
                block = m.group(1)
                title, link, pub = tag(block, "title"), tag(block, "link"), tag(block, "pubDate")
                if not title:
                    continue
                try:
                    ts = parsedate_to_datetime(pub).timestamp()
                except (ValueError, TypeError):
                    ts = 0
                items.append({"source": source, "title": title[:160],
                              "link": link, "ts": ts})
        except Exception as e:  # noqa: BLE001
            logging.warning("news feed %s failed: %s", source, e)
    items.sort(key=lambda x: -x["ts"])
    items = items[:30]
    _news_cache.update(at=now, items=items)
    return items


# --- X / Twitter feed (bearer-token API v2, heavily cached for the free tier) --
X_CRED_FILE = BASE_DIR / ".x_credentials"
X_CACHE_FILE = DATA_DIR / "x_cache.json"
FEED_DB = DATA_DIR / "feed.db"


def feed_db():
    """SQLite archive of every social post ever pulled (cache is overwritten; this isn't)."""
    con = sqlite3.connect(FEED_DB)
    con.execute("""CREATE TABLE IF NOT EXISTS tweets(
        id TEXT PRIMARY KEY, author TEXT, text TEXT, created_at TEXT,
        likes INTEGER, sentiment TEXT, source TEXT, url TEXT, first_seen TEXT)""")
    return con


def archive_tweets(feed):
    """Upsert the current feed into the archive; likes refresh, first_seen sticks."""
    tweets = feed.get("tweets") or []
    if not tweets:
        return 0
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with feed_db() as con:
        con.executemany(
            "INSERT INTO tweets VALUES(?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET likes=excluded.likes",
            [(str(t.get("id")), t.get("author"), t.get("text"), t.get("created_at"),
              t.get("likes", 0), t.get("sentiment"), feed.get("source", "x"),
              t.get("url"), now) for t in tweets])
        return con.execute("SELECT COUNT(*) FROM tweets").fetchone()[0]
X_TTL_SECONDS = 6 * 3600  # free tier is tiny; ~4 refreshes/day max

X_BULL = ["bull", "buy", "long", "ath", "all-time high", "pump", "accumul",
          "moon", "breakout", "support held", "bottom is in", "oversold"]
X_BEAR = ["bear", "sell", "short", "dump", "crash", "capitul", "breakdown",
          "liquidat", "fear", "lower low", "resistance rejected", "bubble"]


def get_x_creds():
    """X credentials: .env (preferred) -> legacy .x_credentials -> None."""
    bearer = os.environ.get("X_BEARER_TOKEN", "").strip()
    if bearer:
        accounts = [a.strip() for a in
                    os.environ.get("X_ACCOUNTS", "BitcoinMagazine").split(",") if a.strip()]
        return {"bearer_token": bearer, "accounts": accounts}
    if X_CRED_FILE.exists():
        return json.loads(X_CRED_FILE.read_text())
    return None


def x_sentiment(text):
    t = text.lower()
    bull = sum(w in t for w in X_BULL)
    bear = sum(w in t for w in X_BEAR)
    return "bull" if bull > bear else "bear" if bear > bull else "neutral"


def fetch_bluesky():
    """Free fallback: live Bitcoin posts from Bluesky's public search."""
    url = ("https://api.bsky.app/xrpc/app.bsky.feed.searchPosts"
           "?q=bitcoin&limit=25&sort=latest")
    req = urllib.request.Request(url, headers={"User-Agent": "btc-genius/0.1"})
    with urllib.request.urlopen(req, timeout=15) as r:
        posts = json.load(r).get("posts", [])
    out = []
    for p in posts:
        text = p.get("record", {}).get("text", "")
        handle = p.get("author", {}).get("handle", "?")
        rkey = p.get("uri", "").rsplit("/", 1)[-1]
        out.append({
            "author": handle, "id": rkey, "text": text[:280],
            "created_at": p.get("record", {}).get("createdAt"),
            "likes": p.get("likeCount", 0),
            "sentiment": x_sentiment(text),
            "url": f"https://bsky.app/profile/{handle}/post/{rkey}",
        })
    return out


def fetch_hn():
    """Second fallback: latest Bitcoin stories from Hacker News (Algolia)."""
    url = ("https://hn.algolia.com/api/v1/search_by_date"
           "?query=bitcoin&tags=story&hitsPerPage=20")
    req = urllib.request.Request(url, headers={"User-Agent": "btc-genius/0.1"})
    with urllib.request.urlopen(req, timeout=15) as r:
        hits = json.load(r).get("hits", [])
    return [{
        "author": h.get("author", "?"), "id": str(h.get("objectID")),
        "text": h.get("title", "")[:280], "created_at": h.get("created_at"),
        "likes": h.get("points", 0), "sentiment": x_sentiment(h.get("title", "")),
        "url": f"https://news.ycombinator.com/item?id={h.get('objectID')}",
    } for h in hits]


def fetch_x_feed():
    try:
        cache = json.loads(X_CACHE_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        cache = {}
    ttl = X_TTL_SECONDS if cache.get("source") == "x" else 900
    if time.time() - cache.get("at", 0) < ttl and cache.get("tweets"):
        return cache
    creds = get_x_creds()
    if not creds:
        return _social_fallback(cache, "no X credentials")
    bearer = creds["bearer_token"]
    tokens = [bearer, urllib.parse.unquote(bearer)]  # tolerate %3D-style pastes

    def xget(url):
        last_err = None
        for tok in tokens:
            req = urllib.request.Request(url, headers={
                "Authorization": f"Bearer {tok}", "User-Agent": "btc-genius/0.1"})
            try:
                with urllib.request.urlopen(req, timeout=15) as r:
                    return json.load(r)
            except urllib.error.HTTPError as e:
                last_err = e
                if e.code != 401:
                    break
        raise last_err

    tweets, ids = [], cache.get("user_ids", {})
    last_code = None
    for acct in creds.get("accounts", ["BitcoinMagazine"]):
        try:
            if acct not in ids:
                ids[acct] = xget(
                    f"https://api.twitter.com/2/users/by/username/{acct}")["data"]["id"]
            tl = xget(f"https://api.twitter.com/2/users/{ids[acct]}/tweets"
                      f"?max_results=10&tweet.fields=created_at,public_metrics"
                      f"&exclude=retweets,replies")
            for t in tl.get("data", []):
                tweets.append({
                    "author": acct, "id": t["id"], "text": t["text"][:280],
                    "created_at": t.get("created_at"),
                    "likes": t.get("public_metrics", {}).get("like_count", 0),
                    "sentiment": x_sentiment(t["text"]),
                })
        except Exception as e:  # noqa: BLE001 - per-account; others may succeed
            last_code = getattr(e, "code", None)
            logging.warning("x feed %s failed: %s", acct, e)
    if tweets:
        tweets.sort(key=lambda t: t.get("created_at") or "", reverse=True)
        cache = {"at": time.time(), "user_ids": ids, "tweets": tweets, "source": "x"}
        X_CACHE_FILE.write_text(json.dumps(cache))
        return cache
    reason = ("X auth OK but 0 API credits (paid feature)"
              if last_code == 402 else f"X unavailable ({last_code})")
    return _social_fallback(cache, reason)


def _social_fallback(cache, reason):
    """X failed — serve a free live social feed instead (Bluesky, then HN)."""
    for name, fn in (("bluesky", fetch_bluesky), ("hackernews", fetch_hn)):
        try:
            tweets = fn()
            if tweets:
                out = {"at": time.time(), "tweets": tweets, "source": name,
                       "note": reason}
                X_CACHE_FILE.write_text(json.dumps(out))
                return out
        except Exception as e:  # noqa: BLE001 - try the next source
            logging.warning("social fallback %s failed: %s", name, e)
    if cache.get("tweets"):
        cache["stale"] = True
        return cache
    return {"error": f"{reason}; all fallbacks failed — will retry"}


def system_status():
    """Freshness of each pipeline component, from data-file ages."""
    out = {}
    for label, (rel, stale) in STATUS_FILES.items():
        p = DATA_DIR / rel
        if not p.exists():
            out[label] = {"state": "missing", "age_s": None}
            continue
        age = int(time.time() - p.stat().st_mtime)
        state = "ok" if stale is None or age <= stale else "stale"
        out[label] = {"state": state, "age_s": age}
    return out


def load_or_create_secret():
    """Webhook secret: .env (preferred) -> legacy .webhook_secret -> generate."""
    secret = os.environ.get("WEBHOOK_SECRET", "").strip()
    if secret:
        return secret
    if SECRET_FILE.exists():
        return SECRET_FILE.read_text().strip()
    secret = secrets.token_urlsafe(24)
    with ENV_FILE.open("a") as f:
        f.write(f"WEBHOOK_SECRET={secret}\n")
    os.chmod(ENV_FILE, 0o600)
    logging.info("generated new webhook secret in %s", ENV_FILE)
    return secret


class Handler(BaseHTTPRequestHandler):
    server_version = "BTCGenius/0.1"
    secret = ""  # set at startup

    def _respond(self, code, body):
        payload = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _respond_raw(self, code, payload, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        if self.path == "/health":
            self._respond(200, {"status": "ok"})
        elif self.path == "/favicon.ico":
            self._respond_raw(200, b"", "image/x-icon")
        elif self.path == "/feed/news":
            self._respond_raw(200, json.dumps(fetch_news()).encode(), "application/json")
        elif self.path == "/feed/x":
            feed = fetch_x_feed()
            try:
                feed["archived_total"] = archive_tweets(feed)
            except Exception as e:  # noqa: BLE001 - archive is best-effort
                logging.warning("feed archive failed: %s", e)
            self._respond_raw(200, json.dumps(feed).encode(), "application/json")
        elif self.path.startswith("/feed/x/history"):
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            limit = min(int(qs.get("limit", ["200"])[0] or 200), 1000)
            term = qs.get("q", [""])[0]
            with feed_db() as con:
                con.row_factory = sqlite3.Row
                sql = "SELECT * FROM tweets"
                args = []
                if term:
                    sql += " WHERE text LIKE ?"
                    args.append(f"%{term}%")
                sql += " ORDER BY created_at DESC LIMIT ?"
                rows = [dict(r) for r in con.execute(sql, args + [limit])]
                total = con.execute("SELECT COUNT(*) FROM tweets").fetchone()[0]
            self._respond_raw(200, json.dumps({"total": total, "tweets": rows}).encode(),
                              "application/json")
        elif self.path == "/status":
            self._respond_raw(200, json.dumps(system_status()).encode(), "application/json")
        elif self.path in ("/", "/dashboard"):
            page = BASE_DIR / "web" / "dashboard.html"
            if page.exists():
                self._respond_raw(200, page.read_bytes(), "text/html; charset=utf-8")
            else:
                self._respond(404, {"error": "dashboard.html missing"})
        elif self.path == "/journal":
            page = BASE_DIR / "web" / "journal.html"
            if page.exists():
                self._respond_raw(200, page.read_bytes(), "text/html; charset=utf-8")
            else:
                self._respond(404, {"error": "journal.html missing"})
        elif self.path == "/ledger":
            page = BASE_DIR / "web" / "ledger.html"
            if page.exists():
                self._respond_raw(200, page.read_bytes(), "text/html; charset=utf-8")
            else:
                self._respond(404, {"error": "ledger.html missing"})
        elif self.path == "/social":
            page = BASE_DIR / "web" / "social.html"
            if page.exists():
                self._respond_raw(200, page.read_bytes(), "text/html; charset=utf-8")
            else:
                self._respond(404, {"error": "social.html missing"})
        elif self.path == "/reports":
            page = BASE_DIR / "web" / "reports.html"
            if page.exists():
                self._respond_raw(200, page.read_bytes(), "text/html; charset=utf-8")
            else:
                self._respond(404, {"error": "reports.html missing"})
        elif self.path == "/reports/list":
            names = sorted(p.name for p in (DATA_DIR / "reports").glob("*.md"))
            self._respond_raw(200, json.dumps(names).encode(), "application/json")
        elif self.path.startswith("/data/"):
            self._serve_data(self.path[len("/data/"):])
        else:
            self._respond(404, {"error": "not found"})

    def _serve_data(self, rel):
        """Read-only access to data/ files; .jsonl is converted to a JSON array."""
        target = (DATA_DIR / rel.split("?")[0]).resolve()
        if not str(target).startswith(str(DATA_DIR.resolve())) or not target.is_file():
            self._respond(404, {"error": "not found"})
            return
        if target.suffix == ".jsonl":
            rows = []
            for line in target.read_text().strip().splitlines()[-1000:]:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
            self._respond_raw(200, json.dumps(rows).encode(), "application/json")
        elif target.suffix in (".json", ".md"):
            ctype = "application/json" if target.suffix == ".json" else "text/plain; charset=utf-8"
            self._respond_raw(200, target.read_bytes(), ctype)
        elif target.suffix in (".png", ".jpg", ".svg"):
            ctype = {"png": "image/png", "jpg": "image/jpeg",
                     "svg": "image/svg+xml"}[target.suffix[1:]]
            self._respond_raw(200, target.read_bytes(), ctype)
        else:
            self._respond(404, {"error": "unsupported type"})

    def do_POST(self):
        if self.path == "/journal/add":
            self._journal_add()
            return
        if self.path != f"/hook/{self.secret}":
            logging.warning("rejected POST %s from %s",
                            self.path[:40], self.client_address[0])
            self._respond(403, {"error": "forbidden"})
            return

        length = int(self.headers.get("Content-Length", 0))
        if length > MAX_BODY:
            self._respond(413, {"error": "payload too large"})
            return
        raw = self.rfile.read(length).decode("utf-8", errors="replace")

        # TradingView sends the alert "Message" verbatim — JSON if you wrote
        # JSON in the alert config, plain text otherwise. Accept both.
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {"raw_text": raw}

        record = {
            "received_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "source": "tradingview",
            "payload": payload,
        }
        with ALERTS.open("a") as f:
            f.write(json.dumps(record) + "\n")
        db.save_alert(record)
        logging.info("alert recorded: %s", str(payload)[:200])
        self._respond(200, {"status": "recorded"})

    def _journal_add(self):
        """Append a call-journal entry, snapshotting current dashboard state."""
        length = int(self.headers.get("Content-Length", 0))
        if length > MAX_BODY:
            self._respond(413, {"error": "payload too large"})
            return
        try:
            call = json.loads(self.rfile.read(length).decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            self._respond(400, {"error": "invalid json"})
            return
        snapshot = {}
        state_file = DATA_DIR / "state.json"
        if state_file.exists():
            try:
                s = json.loads(state_file.read_text())
                snapshot = {k: s.get(k) for k in
                            ("price", "regime", "a_score", "b_score",
                             "corr_btc_nasdaq_30d", "volume_vs_30d_avg",
                             "dist_from_50d_pct", "ma200")}
                if s.get("price") and s.get("ma200"):
                    snapshot["mayer"] = round(s["price"] / s["ma200"], 2)
            except json.JSONDecodeError:
                pass
        record = {
            "logged_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "author": "human",
            "call": {k: call.get(k) for k in
                     ("direction", "thesis", "confidence_pct",
                      "invalidation", "horizon")},
            "snapshot": snapshot,
        }
        with (DATA_DIR / "journal.jsonl").open("a") as f:
            f.write(json.dumps(record) + "\n")
        db.save_journal(record)
        logging.info("journal entry: %s @ %s", call.get("direction"),
                     snapshot.get("price"))
        self._respond(200, {"status": "recorded"})

    def log_message(self, fmt, *args):  # route http.server chatter to our log
        logging.debug("%s - %s", self.client_address[0], fmt % args)


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=str(LOG_FILE), level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    Handler.secret = load_or_create_secret()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    logging.info("listening on http://%s:%d (POST /hook/<secret>)", HOST, PORT)
    server.serve_forever()


if __name__ == "__main__":
    main()
