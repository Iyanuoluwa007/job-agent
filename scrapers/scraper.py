"""
Job Scraper — scrapes JD text from any job URL using Playwright.
Handles JS-heavy ATS boards (Greenhouse, Lever, Workday, Indeed, LinkedIn).
"""

import logging, re
log = logging.getLogger("scraper")

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
except ImportError:
    sync_playwright = None

# ATS-specific content selectors (tried in order)
CONTENT_SELECTORS = [
    ".jobs-description",          # LinkedIn
    "#job-details",               # LinkedIn alt
    ".content-intro",             # Lever
    "#job_description",           # Greenhouse
    ".job-description",           # generic
    ".description",               # generic
    "article",                    # generic
    "main",                       # fallback
    "body",                       # last resort
]

TITLE_SELECTORS = [
    "h1.posting-headline",        # Lever
    "h1.app-title",               # Greenhouse
    "h1.jobs-unified-top-card__job-title",  # LinkedIn
    "h1",                         # generic
]

# === PATCH 19C: aggregator hosts =========================================
# Domains where the URL host name is NOT the employer. If _extract_company
# falls back to the host for any of these, return "" instead so process_job
# skips the URL entirely rather than shipping letters with the aggregator
# name as the employer. ATS patterns (lever, greenhouse, workday) still
# take priority and extract the real company.
AGGREGATOR_HOSTS = {
    "cv-library.co.uk", "cv-library.com",
    "reed.co.uk", "reed.com",
    "indeed.com", "indeed.co.uk", "uk.indeed.com",
    "linkedin.com", "www.linkedin.com",
    "totaljobs.com", "www.totaljobs.com",
    "jobserve.com", "www.jobserve.com",
    "jobsite.co.uk", "www.jobsite.co.uk",
    "monster.co.uk", "monster.com",
    "glassdoor.com", "glassdoor.co.uk",
    "cwjobs.co.uk",
    "adzuna.co.uk", "adzuna.com",
    # Just-the-slug fallbacks that _extract_company produces from URL paths
    # like linkedin.com/jobs/view/.../uk/... -> "uk" / "us" / "ca"
    "uk", "us", "ca",
}
# === END PATCH 19C =======================================================


# === PATCH 21 (O-1): generic subdomains constant =========================
# Subdomains that are typically a "careers landing" prefix, NOT the actual
# brand. When the URL has one of these as the leading subdomain, take the
# next component as the company name. Example: careers.rolls-royce.com
# has "careers" as parts[0], so we use parts[1] = "rolls-royce" -> "Rolls-Royce".
# Curated to match the existing _GENERIC_EMAIL_PREFIXES set further down,
# but kept as separate constant since the email use case is different.
GENERIC_SUBDOMAINS = frozenset({
    "careers", "career",
    "jobs", "job",
    "apply", "application", "applications",
    "work", "join", "talent",
    "hiring", "recruit", "recruiting", "recruitment",
    "hr", "people",
})
# === END PATCH 21 (O-1) ==================================================


def _extract_company(url: str) -> str:
    url = url.lower()
    for pattern in [
        r"jobs\.lever\.co/([^/]+)",
        r"boards\.greenhouse\.io/([^/]+)",
        r"([^.]+)\.workday\.com",
        r"([^.]+)\.greenhouse\.io",
    ]:
        m = re.search(pattern, url)
        if m:
            return m.group(1).replace("-", " ").title()
    # === PATCH 19C: aggregator fallback check ============================
    # Strip host from URL. If it's an aggregator, return "" — agent.py will
    # skip with "unknown_employer:aggregator" rather than ship a broken
    # letter or burn LLM quota generating one.
    host = url.replace("https://", "").replace("http://", "").split("/")[0]
    host_lower = host.lower()
    # Normalise host variants: strip www. prefix so "www.cv-library.co.uk"
    # matches "cv-library.co.uk" in the set.
    host_clean = host_lower.lstrip().removeprefix("www.")
    # Also extract the bare slug (e.g. "cv-library" from "cv-library.co.uk")
    slug = host_clean.split(".")[0]
    if host_clean in AGGREGATOR_HOSTS or host_lower in AGGREGATOR_HOSTS or slug in AGGREGATOR_HOSTS:
        return ""
    # === PATCH 21 (O-1): generic subdomain handling ======================
    # Many companies host their careers site at a generic subdomain like
    # careers.rolls-royce.com or jobs.wayve.ai. Without this fix,
    # _extract_company would return "Careers" / "Jobs" as the company name
    # and ship cover letters that say "at Careers" instead of "at Rolls-Royce".
    # If the first slug is a known generic, fall back to the SECOND domain
    # component (the actual brand).
    parts = host_clean.split(".")
    if slug in GENERIC_SUBDOMAINS and len(parts) >= 3:
        slug = parts[1]
    # === END PATCH 21 (O-1) ==============================================
    # Not an aggregator -- original fallback: title-case the subdomain slug
    return slug.title()
    # === END PATCH 19C ====================================================


# === PATCH 14: recruiter email extraction ==================================
# Generic email prefixes that usually mean "no named recruiter".
# When a JD has ONLY these, we still keep the first one as fallback.
# When a JD has both, we prefer the personal one.
_GENERIC_EMAIL_PREFIXES = {
    "info", "hello", "contact", "support", "help", "admin",
    "careers", "career", "jobs", "job", "apply", "application",
    "recruiting", "recruitment", "recruiter", "talent", "hr",
    "people", "noreply", "no-reply", "donotreply", "do-not-reply",
    "mailer-daemon", "postmaster", "webmaster",
}

_EMAIL_REGEX = re.compile(
    r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"
)


def _is_personal_email(addr: str) -> bool:
    """True if the local-part (before @) looks like a person, not a team alias."""
    try:
        local = addr.split("@", 1)[0].lower()
    except Exception:
        return False
    # Strip digits / separators to compare against the generic set.
    clean = re.sub(r"[._\-0-9]", "", local)
    return clean not in _GENERIC_EMAIL_PREFIXES


def extract_recruiter_email(jd_text: str) -> str:
    """Extract a contact email from JD text.
    Prefers personal addresses; falls back to careers@/recruiting@ if nothing
    else. Returns empty string if no email is present at all.
    """
    if not jd_text:
        return ""
    matches = _EMAIL_REGEX.findall(jd_text)
    if not matches:
        return ""
    # Dedup while preserving order.
    seen = set()
    uniq = []
    for m in matches:
        low = m.lower()
        if low not in seen:
            seen.add(low)
            uniq.append(m)
    # Prefer first personal-looking email.
    for m in uniq:
        if _is_personal_email(m):
            return m
    # Fallback: first generic email (careers@, recruiting@, etc.)
    return uniq[0]


# === END PATCH 14 =========================================================


class JobScraper:
    def scrape(self, url: str) -> dict:
        if not sync_playwright:
            return {"url": url, "title": "Unknown", "company": _extract_company(url), "description": ""}

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page    = browser.new_page(user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ))
            try:
                page.goto(url, timeout=25_000)
                page.wait_for_load_state("networkidle", timeout=12_000)

                # Title
                title = page.title()
                for sel in TITLE_SELECTORS:
                    try:
                        el = page.locator(sel).first
                        if el.count() > 0:
                            t = el.inner_text().strip()
                            if t and len(t) < 200:
                                title = t
                                break
                    except Exception:
                        pass

                # JD Content
                description = ""
                for sel in CONTENT_SELECTORS:
                    try:
                        el = page.locator(sel).first
                        if el.count() > 0:
                            text = el.inner_text().strip()
                            if len(text) > 200:
                                description = text
                                break
                    except Exception:
                        pass

                recruiter = extract_recruiter_email(description)
                return {
                    "url"            : url,
                    "title"          : title,
                    "company"        : _extract_company(url),
                    "description"    : description[:8000],
                    "recruiter_email": recruiter,
                }
            except PWTimeout:
                log.warning(f"[SCRAPER] Timeout: {url}")
                return {"url": url, "title": "Timeout", "company": _extract_company(url), "description": ""}
            finally:
                browser.close()
