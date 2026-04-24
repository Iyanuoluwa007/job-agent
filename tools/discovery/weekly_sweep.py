#!/usr/bin/env python3
"""
tools/discovery/weekly_sweep.py

System A of A+B discovery pipeline. Runs weekly from GitHub Actions
(Sundays 06:00 UTC). Produces `discovered_companies.json` at repo root.

Pipeline:
  1. Load existing discovered_companies.json + BUILTIN_CAREER_PAGES
  2. For each of 17 queries: DDG html endpoint -> candidate URLs
  3. Filter candidates to careers-pattern URLs, reject known aggregators
  4. HEAD + GET validate (200 + careers keyword in body)
  5. Merge new entries into discovered_companies.json
  6. Write back (workflow commits if diff non-empty)

Runs from GitHub runner IPs -- datacenter-IP blocks that hit VPS do NOT hit
these runners (Azure/AWS IP pools with high rotation).

Design decisions:
  - DDG html endpoint only, no Brave/Bing yet (Phase 2)
  - No LLM relevance filter yet (keyword filter is enough for Phase 1)
  - Writes `discovered_companies.json` at repo ROOT to match existing
    container path /app/discovered_companies.json (loaded by
    scrapers/company_discovery.py:load_all_discovered_urls)
  - Output schema is backwards-compatible: {url: metadata_dict}
  - Script is idempotent; safe to re-run
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse, urljoin, quote_plus, urlunparse

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
REPO_ROOT      = Path(__file__).resolve().parents[2]
DISCOVERED     = REPO_ROOT / "discovered_companies.json"
FINDER_PY      = REPO_ROOT / "scrapers" / "finder.py"
DRY_RUN        = os.environ.get("DRY_RUN", "false").lower() == "true"

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
HTTP_TIMEOUT = 15
DDG_RATE_LIMIT_S = 2.0
MAX_CANDIDATES_PER_QUERY = 15
MIN_CAREERS_KEYWORDS_IN_BODY = 2

# Same 17 queries as existing scrapers/company_discovery.py
QUERIES = [
    "AI Systems Engineer company UK careers hiring",
    "Computer Vision Engineer company UK careers hiring",
    "Machine Learning Engineer company UK careers hiring",
    "Autonomous Systems Engineer company UK careers hiring",
    "robotics AI startup UK careers jobs site",
    "computer vision startup London Manchester hiring",
    "autonomous systems company UK engineering jobs",
    "machine learning startup UK careers page",
    "embedded systems AI company UK careers",
    "perception autonomy startup UK jobs site",
    "UK robotics scaleup hiring 2025 2026",
    "autonomous vehicle startup UK careers",
    "drone robotics company UK engineering roles",
    "medical robotics startup UK jobs",
    "agricultural robotics company UK careers",
    "industrial automation AI UK startup hiring",
    "SLAM navigation robotics startup UK",
]

AGGREGATOR_HOSTS = {
    "linkedin.com", "www.linkedin.com", "uk.linkedin.com", "ca.linkedin.com",
    "indeed.com", "www.indeed.com", "uk.indeed.com",
    "cv-library.co.uk", "www.cv-library.co.uk",
    "reed.co.uk", "www.reed.co.uk",
    "glassdoor.com", "www.glassdoor.com", "www.glassdoor.co.uk",
    "welcometothejungle.com", "www.welcometothejungle.com",
    "otta.com", "app.otta.com",
    "totaljobs.com", "www.totaljobs.com",
    "adzuna.co.uk", "www.adzuna.co.uk",
    "jobs.theguardian.com",
    "jobserve.com",
    "monster.co.uk",
}

NOISE_HOSTS = {
    "twitter.com", "x.com", "medium.com", "crunchbase.com", "wikipedia.org",
    "en.wikipedia.org", "youtube.com", "www.youtube.com", "duckduckgo.com",
    "google.com", "www.google.com", "bing.com", "www.bing.com",
    "facebook.com", "www.facebook.com", "instagram.com",
    "news.ycombinator.com", "reddit.com", "www.reddit.com", "old.reddit.com",
    "github.com", "www.github.com",
    "angel.co", "wellfound.com",
    "blog.google", "www.blog.google",
}

ATS_HOSTS = {
    "boards.greenhouse.io", "jobs.lever.co", "jobs.ashbyhq.com",
    "jobs.smartrecruiters.com", "apply.workable.com",
}

CAREERS_PATH_PATTERNS = re.compile(
    r"/(careers?|jobs?|join[-_]us|work[-_]with[-_]us|hiring|join[-_]the[-_]team|"
    r"openings|positions|vacancies|employment)/?(\?|$|#)",
    re.IGNORECASE,
)

CAREERS_BODY_KEYWORDS = [
    "career", "careers", "job", "jobs", "hiring", "positions", "openings",
    "join us", "join our team", "work with us", "vacanc",
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("discovery")


# ---------------------------------------------------------------------------
# Existing-state loaders
# ---------------------------------------------------------------------------
def load_discovered() -> dict:
    if not DISCOVERED.exists():
        return {}
    try:
        data = json.loads(DISCOVERED.read_text() or "{}")
        if not isinstance(data, dict):
            log.warning("discovered_companies.json is not a dict, treating as empty")
            return {}
        return data
    except json.JSONDecodeError as e:
        log.warning(f"discovered_companies.json parse error: {e} -- treating as empty")
        return {}


def load_builtin_pages() -> set[str]:
    """Parse BUILTIN_CAREER_PAGES from finder.py. Best-effort regex; falls
    back to empty set on failure (worse dedup but still safe)."""
    if not FINDER_PY.exists():
        log.warning(f"{FINDER_PY} not found -- skipping BUILTIN dedup")
        return set()
    try:
        src = FINDER_PY.read_text()
        m = re.search(r"BUILTIN_CAREER_PAGES\s*=\s*\[(.*?)\n\]", src, re.DOTALL)
        if not m:
            return set()
        block = m.group(1)
        return set(re.findall(r'"(https?://[^"]+)"', block))
    except Exception as e:
        log.warning(f"failed parsing BUILTIN_CAREER_PAGES: {e}")
        return set()


# ---------------------------------------------------------------------------
# Search backend: DDG html endpoint
# ---------------------------------------------------------------------------
def ddg_search(query: str, max_results: int = 15) -> list[str]:
    """Query DDG html endpoint, return candidate URLs in order."""
    url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": UA},
            timeout=HTTP_TIMEOUT,
            allow_redirects=True,
        )
    except requests.RequestException as e:
        log.warning(f"[DDG] {query!r} -> request error: {e}")
        return []

    if resp.status_code != 200:
        log.warning(f"[DDG] {query!r} -> HTTP {resp.status_code}")
        return []

    body = resp.text
    if len(body) < 500 or any(
        k in body.lower() for k in ("captcha", "unusual traffic", "anomal")
    ):
        log.warning(f"[DDG] {query!r} -> blocked-looking response ({len(body)} bytes)")
        return []

    soup = BeautifulSoup(body, "lxml")
    urls: list[str] = []

    # DDG html: <a class="result__a" href="...">
    for a in soup.select("a.result__a"):
        href = a.get("href", "")
        if not href:
            continue
        # DDG often returns /l/?uddg=<encoded-url> redirects. Decode.
        if "uddg=" in href:
            m = re.search(r"uddg=([^&]+)", href)
            if m:
                from urllib.parse import unquote
                href = unquote(m.group(1))
        if href.startswith("http"):
            urls.append(href)
            if len(urls) >= max_results:
                break

    log.info(f"[DDG] {query!r} -> {len(urls)} raw results")
    return urls


# ---------------------------------------------------------------------------
# URL filter + validate
# ---------------------------------------------------------------------------
def canonical_url(u: str) -> str:
    """Strip query/fragment + trailing slash for dedup stability."""
    try:
        p = urlparse(u)
        path = p.path.rstrip("/") or "/"
        return urlunparse((p.scheme, p.netloc.lower(), path, "", "", ""))
    except Exception:
        return u.rstrip("/")


def looks_like_careers_url(u: str) -> bool:
    p = urlparse(u)
    host = (p.netloc or "").lower()
    if not host:
        return False
    if host in AGGREGATOR_HOSTS or host in NOISE_HOSTS:
        return False
    # ATS sub-paths: jobs.lever.co/<company> etc.
    for ats in ATS_HOSTS:
        if host == ats and p.path.strip("/"):
            return True
    # Greenhouse embed: boards.greenhouse.io/<company>
    if host == "boards.greenhouse.io" and p.path.strip("/"):
        return True
    # Generic careers path detection
    if CAREERS_PATH_PATTERNS.search(u):
        return True
    return False


def validate_url(u: str) -> tuple[bool, str]:
    """HEAD then GET. Returns (ok, reason)."""
    try:
        h = requests.head(
            u, headers={"User-Agent": UA}, timeout=HTTP_TIMEOUT,
            allow_redirects=True,
        )
        if h.status_code >= 400:
            return False, f"HEAD {h.status_code}"
    except requests.RequestException as e:
        return False, f"HEAD error: {type(e).__name__}"

    try:
        g = requests.get(
            u, headers={"User-Agent": UA}, timeout=HTTP_TIMEOUT,
            allow_redirects=True,
        )
    except requests.RequestException as e:
        return False, f"GET error: {type(e).__name__}"

    if g.status_code != 200:
        return False, f"GET {g.status_code}"
    ct = g.headers.get("content-type", "").lower()
    if "html" not in ct:
        return False, f"content-type {ct}"

    body_lower = g.text.lower()
    hits = sum(1 for kw in CAREERS_BODY_KEYWORDS if kw in body_lower)
    if hits < MIN_CAREERS_KEYWORDS_IN_BODY:
        return False, f"career keywords in body: {hits}"
    return True, "ok"


def extract_company_name(url: str) -> str:
    """Best-effort company name guess from URL. Refined in Phase 2 by LLM."""
    p = urlparse(url)
    host = p.netloc.lower().replace("www.", "")
    # For ATS URLs the company is in the path
    if host in ATS_HOSTS or host == "boards.greenhouse.io":
        slug = p.path.strip("/").split("/")[0] if p.path.strip("/") else host
        return slug.replace("-", " ").title()
    # Otherwise take the domain root
    root = host.split(".")[0]
    return root.replace("-", " ").title()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    log.info("=== Discovery A weekly sweep ===")
    log.info(f"REPO_ROOT: {REPO_ROOT}")
    log.info(f"DISCOVERED: {DISCOVERED}")
    log.info(f"DRY_RUN: {DRY_RUN}")

    existing = load_discovered()
    builtin = load_builtin_pages()
    known = set(canonical_url(u) for u in existing) | set(canonical_url(u) for u in builtin)
    log.info(f"Starting: {len(existing)} in discovered.json, {len(builtin)} in BUILTIN, {len(known)} unique known")

    all_candidates: list[str] = []
    for q in QUERIES:
        time.sleep(DDG_RATE_LIMIT_S)
        results = ddg_search(q, max_results=MAX_CANDIDATES_PER_QUERY)
        all_candidates.extend(results)

    log.info(f"Raw candidates from {len(QUERIES)} queries: {len(all_candidates)}")

    # Filter + dedupe
    filtered: list[str] = []
    seen_canonical = set()
    for u in all_candidates:
        cu = canonical_url(u)
        if cu in seen_canonical or cu in known:
            continue
        if not looks_like_careers_url(u):
            continue
        seen_canonical.add(cu)
        filtered.append(u)

    log.info(f"Post-filter unique candidates: {len(filtered)}")

    # Validate
    validated: dict[str, dict] = {}
    for i, u in enumerate(filtered, 1):
        ok, reason = validate_url(u)
        if not ok:
            log.info(f"  [{i}/{len(filtered)}] REJECT {u!r} -- {reason}")
            continue
        cu = canonical_url(u)
        validated[cu] = {
            "company": extract_company_name(u),
            "added_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "source": "discovery-weekly-a",
            "original_url": u,
        }
        log.info(f"  [{i}/{len(filtered)}] ACCEPT {u!r}")

    log.info(f"Validated accepts: {len(validated)}")

    # Merge
    merged = {**existing, **validated}
    added = len(merged) - len(existing)

    # Write
    if DRY_RUN:
        log.info(f"[DRY_RUN] would write {len(merged)} entries ({added} new)")
    else:
        DISCOVERED.write_text(json.dumps(merged, indent=2, sort_keys=True) + "\n")
        log.info(f"Wrote {DISCOVERED} ({len(merged)} entries, {added} new)")

    print(f"SUMMARY: queries={len(QUERIES)} raw={len(all_candidates)} "
          f"filtered={len(filtered)} validated={len(validated)} new={added} "
          f"total={len(merged)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
