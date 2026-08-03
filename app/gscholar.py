"""Google Scholar collector — scholarly + undetected-chromedriver.

Scholar has no public API and actively blocks automated fetching, so this
goes through two layers.

  1) The query is handed to `scholarly.search_pubs()`. scholarly passes
     Scholar keyword syntax (quotes, OR, -, author:, etc.) straight through
     to Scholar, so no translation is needed.
  2) If that gets blocked, a Chrome instance launched via
     `undetected_chromedriver` fetches the same result pages directly and
     hands them to the existing HTML parser. uc strips automation fingerprints
     like navigator.webdriver to evade bot detection.

To avoid an IP ban, it sleeps 5-10 random seconds between papers, so it's
slow -- every search runs it now (see providers.py), so providers.search_gscholar
clamps its own result count to MAX_RESULTS independently of whatever the
overall search asked for; 1000 results at 5-10s each would take hours.

Two more layers make an IP ban itself less costly if it happens anyway:

  - An optional proxy (GSCHOLAR_PROXY / GSCHOLAR_TOR_*): Scholar bans the
    IP, not the machine, so routing through one means a ban doesn't take the
    whole tool down. Off by default -- see configure_proxy().
  - The uc fallback drives the actual scholar.google.com search box (typed
    into character by character, with human-scale pauses) instead of loading
    a results URL directly, which is a page real users never navigate to by
    hand and is exactly the kind of signal traffic-analysis detection looks for.
"""

from __future__ import annotations

import asyncio
import os
import random
import re
import threading
import time
from typing import Iterator

from .models import Paper
from .scholar import _split_meta, parse_results  # reuse the validated HTML parser

SCHOLAR_URL = "https://scholar.google.com/scholar"
SCHOLAR_HOME = "https://scholar.google.com/"

# Spec: random 5-10s wait between papers
SLEEP_MIN = float(os.environ.get("GSCHOLAR_SLEEP_MIN") or 5.0)
SLEEP_MAX = float(os.environ.get("GSCHOLAR_SLEEP_MAX") or 10.0)

# Scholar now runs on every search (see providers.py's DEFAULT_PROVIDERS) --
# capped independently of the overall max_results ceiling so that "always on"
# doesn't mean "always wait tens of minutes". A user who wants deeper Scholar
# coverage for one search can still raise this.
MAX_RESULTS = int(os.environ.get("GSCHOLAR_MAX_RESULTS") or 20)

_driver_lock = threading.Lock()
_driver = None
_chrome_version: str | None = None


class GScholarError(RuntimeError):
    pass


class GScholarBlocked(GScholarError):
    """Rejected via CAPTCHA / 429."""


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


# --------------------------------------------------------------------------- #
# Optional proxy (reduces the cost of an IP ban, doesn't prevent one)
#
# Scholar bans the requesting IP, not the machine or the tool -- routing
# scholarly's requests through a proxy means a ban lands on the proxy's IP
# instead of the host's, so the rest of the tool (and any other traffic on
# this network) keeps working. Off by default: a free-proxy pool is often
# slower and less reliable than a direct connection, and Tor requires the
# user to actually be running a Tor daemon. Opt in with GSCHOLAR_PROXY:
#   "tor"                  -- routes through a local Tor daemon
#                             (GSCHOLAR_TOR_PORT / _CONTROL_PORT / _PASSWORD)
#   "free"                 -- scholarly's own rotating pool of free proxies
#   "http://host:port"     -- a specific proxy (e.g. a paid provider)
# --------------------------------------------------------------------------- #
_proxy_configured = False
_proxy_lock = threading.Lock()


def configure_proxy() -> None:
    """Idempotent: only touches scholarly's global proxy state once."""
    global _proxy_configured
    mode = _env("GSCHOLAR_PROXY")
    if not mode:
        return
    with _proxy_lock:
        if _proxy_configured:
            return
        from scholarly import ProxyGenerator, scholarly

        pg = ProxyGenerator()
        if mode == "tor":
            ok = pg.Tor_External(
                tor_sock_port=int(_env("GSCHOLAR_TOR_PORT") or "9050"),
                tor_control_port=int(_env("GSCHOLAR_TOR_CONTROL_PORT") or "9051"),
                tor_password=_env("GSCHOLAR_TOR_PASSWORD"),
            )
        elif mode == "free":
            ok = pg.FreeProxies()
        else:
            ok = pg.SingleProxy(http=mode, https=mode)
        if ok:
            scholarly.use_proxy(pg)
        _proxy_configured = True  # don't retry every call if it failed once


# --------------------------------------------------------------------------- #
# undetected-chromedriver
# --------------------------------------------------------------------------- #
def chrome_version() -> str:
    """Full version of the installed Chrome, used for the spoofed UA and uc's version_main."""
    global _chrome_version
    if _chrome_version is not None:
        return _chrome_version
    _chrome_version = ""
    binary = _env("CHROME_BINARY") or "chromium"
    try:
        import subprocess
        out = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=20)
        m = re.search(r"(\d+\.\d+\.\d+\.\d+)", out.stdout or "")
        if m:
            _chrome_version = m.group(1)
    except Exception:
        pass
    return _chrome_version


def user_agent() -> str:
    """A UA that doesn't reveal headless mode.

    The default headless UA has 'HeadlessChrome' baked in, which by itself
    gets classified as a bot. This uses the real installed Chrome version but
    drops that tell.
    """
    override = _env("GSCHOLAR_USER_AGENT")
    if override:
        return override
    version = chrome_version() or "131.0.0.0"
    return (
        f"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        f"(KHTML, like Gecko) Chrome/{version} Safari/537.36"
    )


def build_driver():
    """A Chrome driver configured to evade bot detection.

    undetected_chromedriver strips navigator.webdriver and other Chrome
    DevTools fingerprints at the CDP level. Options for running in a
    container are added on top of that.
    """
    import undetected_chromedriver as uc

    options = uc.ChromeOptions()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")               # required in a container
    options.add_argument("--disable-dev-shm-usage")    # when /dev/shm is small
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1400,900")
    options.add_argument("--lang=en-US,en")
    options.add_argument("--disable-blink-features=AutomationControlled")
    # remove the headless tell ('HeadlessChrome' is a bot signal on its own)
    options.add_argument(f"--user-agent={user_agent()}")

    binary = _env("CHROME_BINARY") or _env("CHROMIUM_PATH")
    if binary:
        options.binary_location = binary

    kwargs = {"options": options, "use_subprocess": True}
    driver_exe = _env("CHROMEDRIVER_PATH")
    if driver_exe:
        kwargs["driver_executable_path"] = driver_exe
    version = _env("CHROME_MAJOR_VERSION")
    if not version:
        version = (chrome_version().split(".") or [""])[0]
    if version.isdigit():
        kwargs["version_main"] = int(version)

    driver = uc.Chrome(**kwargs)
    # strip the webdriver flag and headless tells once more (covers paths uc misses)
    try:
        driver.execute_cdp_cmd("Network.setUserAgentOverride", {
            "userAgent": user_agent(),
            "acceptLanguage": "en-US,en;q=0.9",
            "platform": "Linux x86_64",
        })
        driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument",
            {"source": """
                Object.defineProperty(navigator,'webdriver',{get:()=>undefined});
                Object.defineProperty(navigator,'languages',{get:()=>['en-US','en']});
                Object.defineProperty(navigator,'plugins',{get:()=>[1,2,3,4,5]});
            """},
        )
    except Exception:
        pass
    driver.set_page_load_timeout(60)
    return driver


def get_driver():
    """The driver is expensive, so build one and reuse it."""
    global _driver
    with _driver_lock:
        if _driver is None:
            _driver = build_driver()
        return _driver


def close_driver() -> None:
    global _driver
    with _driver_lock:
        if _driver is not None:
            try:
                _driver.quit()
            except Exception:
                pass
            _driver = None


def _is_blocked_html(html: str) -> bool:
    low = (html or "")[:6000].lower()
    return any(m in low for m in ("gs_captcha", "/sorry/index", "unusual traffic", 'id="captcha"'))


# --------------------------------------------------------------------------- #
# scholarly
# --------------------------------------------------------------------------- #
def _pub_to_paper(pub: dict) -> Paper | None:
    """scholarly's publication dict -> Paper."""
    bib = pub.get("bib") or {}
    title = (bib.get("title") or "").strip()
    if not title:
        return None

    authors = bib.get("author") or []
    if isinstance(authors, str):
        authors = [a.strip() for a in re.split(r"\band\b|,|;", authors) if a.strip()]

    year = None
    raw_year = bib.get("pub_year") or bib.get("year")
    if raw_year:
        m = re.search(r"(1[5-9]\d{2}|20\d{2})", str(raw_year))
        if m:
            year = int(m.group(1))

    venue = (bib.get("venue") or bib.get("journal") or "").strip()
    if venue.upper() in ("NA", "N/A"):
        venue = ""
    publisher = (bib.get("publisher") or "").strip()

    links: dict[str, str] = {}
    if pub.get("pub_url"):
        links["primary"] = pub["pub_url"]
    if pub.get("eprint_url"):
        links["pdf"] = pub["eprint_url"]
    if pub.get("citedby_url"):
        links["citations"] = pub["citedby_url"]
    if pub.get("url_scholarbib"):
        links["bibtex"] = pub["url_scholarbib"]

    abstract = (bib.get("abstract") or "").strip()
    return Paper(
        title=title,
        authors=[a for a in authors if a],
        publisher=publisher,
        venue=venue,
        year=year,
        abstract=abstract,
        abstract_source="snippet" if abstract else "",
        links=links,
        cited_by=pub.get("num_citations"),
        sources=["gscholar"],
    )


def _iter_scholarly(query: str, max_results: int) -> Iterator[Paper]:
    """Fetch one result at a time via scholarly.search_pubs().

    scholarly forwards Scholar keyword syntax as-is, so nothing is
    translated. Sleeps 5-10s between results to avoid an IP ban.
    """
    from scholarly import scholarly

    configure_proxy()
    results = scholarly.search_pubs(query)
    produced = 0
    while produced < max_results:
        try:
            pub = next(results)
        except StopIteration:
            return
        except Exception as exc:
            text = str(exc)
            if "captcha" in text.lower() or "429" in text or "blocked" in text.lower():
                raise GScholarBlocked(f"Google Scholar blocked the request: {text[:150]}")
            raise GScholarError(f"scholarly error: {text[:150]}")

        paper = _pub_to_paper(pub)
        if paper is not None:
            produced += 1
            yield paper
        # spec: random wait after every paper fetched
        if produced < max_results:
            time.sleep(random.uniform(SLEEP_MIN, SLEEP_MAX))


# --------------------------------------------------------------------------- #
# undetected-chromedriver fallback
# --------------------------------------------------------------------------- #
def _human_type(element, text: str) -> None:
    """Character-by-character with human-scale gaps, not Selenium's instant send_keys."""
    for ch in text:
        element.send_keys(ch)
        time.sleep(random.uniform(0.04, 0.18))


def _first_page_via_search_box(driver, query: str) -> bool:
    """Reach the first results page by actually using Scholar's own search box.

    A results URL built and loaded directly (the previous approach here) is a
    page real visitors never land on without searching for something first --
    exactly the kind of navigation-pattern gap traffic-analysis detection
    looks for, on top of the fingerprint-level evasion build_driver already
    does. Returns False if the homepage markup didn't match (Google changed
    it, a consent dialog appeared, etc.), so the caller can fall back to
    loading the URL directly instead of failing the whole search over it.
    """
    from selenium.webdriver.common.by import By
    from selenium.webdriver.common.keys import Keys

    try:
        driver.get(SCHOLAR_HOME)
        time.sleep(random.uniform(1.0, 2.5))
        box = driver.find_element(By.ID, "gs_hdr_tsi")
        _human_type(box, query)
        time.sleep(random.uniform(0.3, 0.9))
        box.send_keys(Keys.RETURN)
        time.sleep(random.uniform(1.5, 3.0))
        return True
    except Exception:
        return False


def _iter_driver(query: str, max_results: int) -> Iterator[Paper]:
    """Fetch result pages via the uc Chrome instance and reuse the existing parser."""
    from urllib.parse import urlencode

    driver = get_driver()
    seen: set[str] = set()
    start = 0
    produced = 0
    first_page = True

    while produced < max_results:
        if first_page:
            first_page = False
            if not _first_page_via_search_box(driver, query):
                driver.get(SCHOLAR_URL + "?" + urlencode({
                    "q": query, "hl": "en", "as_sdt": "0,5",
                    "as_occt": "any", "num": "10",   # 20+ gets blocked immediately (measured)
                }))
        else:
            url = SCHOLAR_URL + "?" + urlencode({
                "q": query, "hl": "en", "as_sdt": "0,5",
                "as_occt": "any",   # spec 2.3 — search the whole record, not just the title
                "num": "10",        # 20+ gets blocked immediately (measured)
                "start": str(start),
            })
            driver.get(url)
        html = driver.page_source
        if _is_blocked_html(html):
            raise GScholarBlocked(
                "Google Scholar blocked the request (CAPTCHA). "
                "Try again later, or raise GSCHOLAR_SLEEP_MIN/MAX."
            )

        page = parse_results(html)
        if not page:
            return
        for paper in page:
            if paper.id in seen:
                continue
            seen.add(paper.id)
            paper.sources = ["gscholar"]
            produced += 1
            yield paper
            if produced >= max_results:
                return
            time.sleep(random.uniform(SLEEP_MIN, SLEEP_MAX))

        if len(page) < 10:
            return
        start += 10


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def _collect(query: str, max_results: int) -> tuple[list[Paper], str]:
    """Try scholarly first, then fall back to the uc driver."""
    papers: list[Paper] = []
    note = ""
    try:
        for paper in _iter_scholarly(query, max_results):
            papers.append(paper)
        return papers, note
    except (GScholarBlocked, GScholarError, ImportError) as exc:
        if papers:
            return papers, f"stopped after {len(papers)} results: {exc}"
        note = f"scholarly failed ({str(exc)[:80]}) -> retrying via the browser."

    try:
        for paper in _iter_driver(query, max_results):
            papers.append(paper)
    except (GScholarBlocked, GScholarError) as exc:
        if papers:
            return papers, f"{note} stopped after {len(papers)} results: {exc}"
        raise
    except Exception as exc:  # environment where the driver can't launch at all
        if papers:
            return papers, f"{note} stopped after {len(papers)} results."
        raise GScholarError(
            f"could not launch the Chrome driver: {str(exc)[:150]}"
        ) from exc
    return papers, note


async def search(query: str, max_results: int) -> tuple[list[Paper], str]:
    """Run the synchronous libraries on a thread outside the event loop."""
    if not query.strip():
        return [], ""
    return await asyncio.to_thread(_collect, query, max_results)
