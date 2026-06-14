#!/usr/bin/env python3
"""
BTC Genius — static site builder for GitHub Pages.

The live console at :8787 is backed by the Python listener; it can't run on
Pages. This publishes the REAL console pages (dashboard with charts, journal,
ledger, social, reports) as a self-contained READ-ONLY snapshot by:

  1. copying web/*.html and injecting a tiny fetch-shim that reroutes the
     backend calls (/data/..., /status, /feed/x, /feed/news, /reports/list)
     to static files committed alongside the HTML;
  2. snapshotting every data file the pages read, plus the three dynamic
     listener routes (/status, /feed/x, /feed/news) from the running server;
  3. rewriting the nav links (/dashboard, /journal, …) to the static pages.

Workflow (main = source, site = published branch):
  python3 src/build_site.py                 # build into _site/ (gitignored)
  python3 src/build_site.py --publish        # build + push to the `site` branch

GitHub Pages is served from the `site` branch (root). The daily launchd job
can call --publish to keep the snapshot fresh.

Stdlib only; Python 3.9+.
"""

import json
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
WEB_DIR = BASE_DIR / "web"
REPORTS = DATA_DIR / "reports"
SITE_OUT = BASE_DIR / "_site"
SITE_BRANCH = "site"
LISTENER = "http://localhost:8787"

# web/<source> -> <name on the static site>. dashboard becomes the landing page.
PAGES = {
    "dashboard.html": "index.html",
    "journal.html": "journal.html",
    "ledger.html": "ledger.html",
    "social.html": "social.html",
    "reports.html": "reports.html",
}

# data files the pages fetch, copied to the same relative path the shim expects.
DATA_COPIES = [
    "series.json", "series_long.json", "state.json", "signals_history.jsonl",
    "objective.json", "whale_ledger.jsonl", "journal.jsonl", "dashboard.md",
    "market/snapshots.jsonl",
]

# dynamic listener routes -> static snapshot file (best-effort; needs :8787 up).
ROUTE_SNAPSHOTS = {"/status": "status.json", "/feed/news": "feed/news.json",
                   "/feed/x": "feed/x.json"}

# nav targets -> static page (both location.href='…' and href="…" forms).
NAV = {
    "/dashboard": "index.html", "/journal": "journal.html",
    "/reports": "reports.html", "/ledger": "ledger.html",
    "/social": "social.html", "/": "index.html",
}

# Reroute the page's backend calls to static files. Runs before page scripts
# (injected at the top of <head>) so it wraps fetch before anything calls it.
SHIM = r"""<script>
(function(){
  var orig = window.fetch.bind(window);
  function remap(p){
    p = p.replace(/^\/+/, '');                 // absolute -> relative to this page
    if (p === 'status') return 'status.json';
    if (p === 'feed/news') return 'feed/news.json';
    if (p.indexOf('feed/x') === 0) return 'feed/x.json';
    if (p === 'reports/list') return 'reports/index.json';
    if (p.indexOf('data/reports/') === 0) return 'reports/' + p.slice('data/reports/'.length);
    return p;                                   // data/... etc. -> served statically
  }
  window.fetch = function(input, init){
    var url = (typeof input === 'string') ? input : (input && input.url) || '';
    if (url.charAt(0) === '/') {
      var path = url.split('?')[0].split('#')[0];
      return orig(remap(path), init);
    }
    return orig(input, init);
  };
})();
</script>
"""

BANNER = ('<div style="background:#e3a008;color:#06090d;font:11px/1.5 ui-monospace,'
          'monospace;text-align:center;padding:4px">READ-ONLY SNAPSHOT — built __BUILT__'
          ' · live console runs locally · not financial advice</div>')


def _fetch_route(path):
    try:
        req = urllib.request.Request(LISTENER + path, headers={"User-Agent": "btc-genius/site"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.read()
    except Exception as e:  # noqa: BLE001 - listener may be down; use a safe stub
        print(f"  warn: {path} unavailable ({e}); writing empty stub")
        return b"[]" if path == "/feed/news" else b"{}"


def build(out=SITE_OUT):
    if out.exists():
        shutil.rmtree(out)
    (out / "data" / "market").mkdir(parents=True)
    (out / "feed").mkdir(parents=True)
    (out / "reports").mkdir(parents=True)

    # 1. pages: inject shim + banner, rewrite nav links
    built = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    for src, dest in PAGES.items():
        html = (WEB_DIR / src).read_text()
        html = html.replace("<head>", "<head>\n" + SHIM, 1)
        html = html.replace("<body>", "<body>\n" + BANNER.replace("__BUILT__", built), 1)
        for route, page in NAV.items():
            html = html.replace(f"location.href='{route}'", f"location.href='{page}'")
            html = html.replace(f'href="{route}"', f'href="{page}"')
        (out / dest).write_text(html)

    # 2. data files the pages read
    for rel in DATA_COPIES:
        src = DATA_DIR / rel
        if src.exists():
            dst = out / "data" / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)

    # 3. dynamic routes -> static snapshots
    for route, fname in ROUTE_SNAPSHOTS.items():
        (out / fname).parent.mkdir(parents=True, exist_ok=True)
        (out / fname).write_bytes(_fetch_route(route))

    # 4. report archive (+ frozen chart assets) and its index
    report_files = sorted(p.name for p in REPORTS.glob("*.md")) if REPORTS.exists() else []
    for fn in report_files:
        shutil.copy2(REPORTS / fn, out / "reports" / fn)
        assets = REPORTS / "assets" / fn[:-3]
        if assets.is_dir():
            shutil.copytree(assets, out / "reports" / "assets" / fn[:-3])
    (out / "reports" / "index.json").write_text(json.dumps(report_files))

    (out / ".nojekyll").write_text("")
    print(f"built {len(PAGES)} pages + {len(report_files)} report(s) → "
          f"{out.relative_to(BASE_DIR)}")
    return out


def _git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True,
                          capture_output=True, text=True)


def publish():
    build(SITE_OUT)
    wt = Path(tempfile.mkdtemp(prefix="mg-site-"))
    try:
        ls = subprocess.run(["git", "ls-remote", "--heads", "origin", SITE_BRANCH],
                            cwd=BASE_DIR, capture_output=True, text=True)
        if ls.stdout.strip():
            _git("fetch", "origin", SITE_BRANCH, cwd=BASE_DIR)
            _git("worktree", "add", str(wt), f"origin/{SITE_BRANCH}", cwd=BASE_DIR)
            _git("checkout", "-B", SITE_BRANCH, cwd=wt)
        else:
            _git("worktree", "add", "--orphan", "-b", SITE_BRANCH, str(wt), cwd=BASE_DIR)

        for child in wt.iterdir():
            if child.name == ".git":
                continue
            shutil.rmtree(child) if child.is_dir() else child.unlink()
        for child in SITE_OUT.iterdir():
            dest = wt / child.name
            shutil.copytree(child, dest) if child.is_dir() else shutil.copy2(child, dest)

        _git("add", "-A", cwd=wt)
        if not subprocess.run(["git", "status", "--porcelain"], cwd=wt,
                              capture_output=True, text=True).stdout.strip():
            print("site already up to date — nothing to publish")
            return
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        _git("-c", "user.name=BTC Genius", "-c", "user.email=noreply@btcgenius.local",
             "commit", "-m", f"Publish site snapshot {stamp}", cwd=wt)
        _git("push", "-u", "origin", SITE_BRANCH, cwd=wt)
        print(f"published → origin/{SITE_BRANCH}")
    finally:
        subprocess.run(["git", "worktree", "remove", "--force", str(wt)],
                       cwd=BASE_DIR, capture_output=True, text=True)


if __name__ == "__main__":
    publish() if "--publish" in sys.argv else build()
