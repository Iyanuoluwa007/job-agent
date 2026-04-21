"""
Autonomous Form Filler — Playwright
Detects ATS platform (Greenhouse, Lever, Workday, generic) and fills accordingly.
Zero human interaction. Fully autonomous submit.
"""

import logging, time
log = logging.getLogger("form_filler")

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    PW_AVAILABLE = True
except ImportError:
    PW_AVAILABLE = False
    log.warning("[FORM] Playwright not installed — run: pip install playwright && playwright install chromium")


# ── ATS-specific selectors ───────────────────────────────────────────────────
ATS_PROFILES = {
    "greenhouse": {
        "name"       : "#first_name, #last_name, input[name='first_name'], input[name='last_name']",
        "email"      : "#email, input[name='email']",
        "phone"      : "#phone, input[name='phone']",
        "resume"     : "input[type='file']",
        "cover"      : "#cover_letter_text, textarea[name='cover_letter']",
        "submit"     : "input[type='submit'], #submit_app",
    },
    "lever": {
        "name"       : "input[name='name'], input[placeholder*='name' i]",
        "email"      : "input[name='email']",
        "phone"      : "input[name='phone']",
        "resume"     : "input[type='file']",
        "cover"      : "textarea[name='comments'], textarea[placeholder*='cover' i]",
        "submit"     : ".submit-app-btn, button[type='submit']",
    },
    "workday": {
        "name"       : "input[data-automation-id='legalNameSection_firstName']",
        "email"      : "input[data-automation-id='email']",
        "phone"      : "input[data-automation-id='phone-number']",
        "resume"     : "input[type='file']",
        "cover"      : "textarea[data-automation-id='coverLetter']",
        "submit"     : "button[data-automation-id='bottomNavigationApplyButton']",
    },
    "generic": {
        "name"       : "input[name*='name' i], input[placeholder*='name' i]",
        "email"      : "input[type='email'], input[name*='email' i]",
        "phone"      : "input[type='tel'], input[name*='phone' i]",
        "resume"     : "input[type='file']",
        "cover"      : "textarea[name*='cover' i], textarea[placeholder*='cover' i], textarea[name*='letter' i], div[contenteditable='true']",
        "submit"     : "button[type='submit'], input[type='submit'], button:text-matches('Apply|Submit', 'i')",
    },
}

def _detect_ats(url: str) -> str:
    url = url.lower()
    if "greenhouse.io"   in url or "boards.greenhouse" in url: return "greenhouse"
    if "jobs.lever.co"   in url or "lever.co"          in url: return "lever"
    if "myworkdayjobs"   in url or "workday.com"        in url: return "workday"
    return "generic"

def _try_fill(page, selector: str, value: str, method="fill") -> bool:
    """Try each selector variant. Returns True if something was filled."""
    for sel in selector.split(","):
        sel = sel.strip()
        try:
            loc = page.locator(sel).first
            if loc.count() > 0:
                if method == "fill":
                    loc.fill(value)
                elif method == "upload":
                    loc.set_input_files(value)
                return True
        except Exception:
            pass
    return False


class FormFiller:
    def __init__(self, profile: dict):
        self.profile = profile

    def apply(self, url: str, cover_letter: str, resume_path: str) -> bool:
        if not PW_AVAILABLE:
            log.error("[FORM] Playwright unavailable.")
            return False

        ats  = _detect_ats(url)
        sels = ATS_PROFILES[ats]
        log.info(f"[FORM] ATS={ats}  url={url}")

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx     = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                )
            )
            page = ctx.new_page()

            try:
                page.goto(url, timeout=30_000)
                page.wait_for_load_state("networkidle", timeout=15_000)

                p_name  = self.profile
                first   = p_name.get("first_name") or p_name.get("full_name","").split()[0]
                last    = p_name.get("last_name")  or " ".join(p_name.get("full_name","").split()[1:])

                # Fill first/last name (try both combined and split)
                if not _try_fill(page, sels["name"], p_name.get("full_name","")):
                    _try_fill(page, "input[name='first_name'], input[id*='first' i]", first)
                    _try_fill(page, "input[name='last_name'],  input[id*='last' i]",  last)

                _try_fill(page, sels["email"], p_name.get("email",""))
                _try_fill(page, sels["phone"], p_name.get("phone",""))

                # LinkedIn / website
                _try_fill(page, "input[name*='linkedin' i], input[placeholder*='linkedin' i]",
                          p_name.get("linkedin",""))
                _try_fill(page, "input[name*='website' i], input[placeholder*='website' i]",
                          p_name.get("website",""))

                # Resume upload
                if resume_path:
                    _try_fill(page, sels["resume"], resume_path, method="upload")

                # Cover letter
                if cover_letter:
                    _try_fill(page, sels["cover"], cover_letter)

                # Small pause for JS validation to settle
                time.sleep(1.5)

                # Submit
                submitted = _try_fill(page, sels["submit"], "", method="click_submit")
                if not submitted:
                    # Direct click fallback
                    for sel in sels["submit"].split(","):
                        try:
                            btn = page.locator(sel.strip()).first
                            if btn.count() > 0:
                                btn.click()
                                submitted = True
                                break
                        except Exception:
                            pass

                time.sleep(2)

                # Confirmation heuristic
                body = page.inner_text("body").lower()
                confirmed = any(w in body for w in [
                    "application submitted", "thank you for applying",
                    "we received your", "successfully applied",
                    "your application has been", "application complete",
                ])
                log.info(f"[FORM] Submitted={submitted}  Confirmed={confirmed}")
                return submitted or confirmed

            except PWTimeout:
                log.warning(f"[FORM] Timeout on {url}")
                return False
            except Exception as e:
                log.error(f"[FORM] Error: {e}")
                return False
            finally:
                browser.close()
