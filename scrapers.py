import json
import os
import random
import re
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from bs4 import BeautifulSoup
from cachetools import TTLCache

import db
from companies import GREENHOUSE_COMPANIES, LEVER_COMPANIES

_SCRAPE_RUNNING = False

# Per (query, country) live-scrape cache — repeat searches are instant for 30 min.
SOURCE_TIMEOUT = int(os.environ.get("SOURCE_TIMEOUT", "6"))   # per-source hard timeout (seconds)
_LIVE_CACHE = TTLCache(maxsize=128, ttl=1800)
_LIVE_CACHE_LOCK = threading.Lock()

# Related-title synonyms used to broaden a search when it returns too few hits.
_TITLE_SYNONYMS = {
    "developer": ["engineer", "programmer"],
    "engineer": ["developer"],
    "sde": ["software engineer", "developer"],
    "ml": ["machine learning"],
    "ai": ["artificial intelligence", "machine learning"],
    "devops": ["sre", "platform engineer"],
    "analyst": ["analytics", "data analyst"],
    "pm": ["product manager"],
    "frontend": ["front end", "ui"],
    "backend": ["back end"],
}

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
]


def _h(json_req=False):
    h = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate",
        "Connection": "keep-alive",
    }
    if json_req:
        h["Accept"] = "application/json"
    else:
        h["Accept"] = "text/html,application/xhtml+xml,*/*;q=0.8"
        h["Upgrade-Insecure-Requests"] = "1"
    return h


# ─────────────────────── Greenhouse API ──────────────────────────

def scrape_greenhouse(company: dict, india_only: bool = False) -> list[dict]:
    slug = company["slug"]
    name = company["name"]
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
    try:
        r = requests.get(url, headers=_h(json_req=True), timeout=10)
        if r.status_code != 200:
            return []
        data = r.json()
        company_name = data.get("name") or name
        jobs = []
        for j in data.get("jobs", []):
            loc_obj = j.get("location") or {}
            loc = loc_obj.get("name") or "Remote/Unspecified"
            if india_only and not is_india_location(loc):
                continue
            link = j.get("absolute_url") or ""
            title = j.get("title") or ""
            if not title or not link:
                continue
            jobs.append({
                "title":    title,
                "company":  company_name,
                "location": loc,
                "link":     link,
                "source":   "Greenhouse",
            })
        return jobs
    except Exception:
        return []


# ─────────────────────── Lever API ───────────────────────────────

def scrape_lever(company: dict, india_only: bool = False) -> list[dict]:
    slug = company["slug"]
    name = company["name"]
    url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    try:
        r = requests.get(url, headers=_h(json_req=True), timeout=10)
        if r.status_code != 200:
            return []
        data = r.json()
        if not isinstance(data, list):
            return []
        jobs = []
        for j in data:
            cats = j.get("categories") or {}
            loc = cats.get("location") or cats.get("office") or "Remote/Unspecified"
            if india_only and not is_india_location(loc):
                continue
            link = j.get("hostedUrl") or ""
            title = j.get("text") or ""
            if not title or not link:
                continue
            jobs.append({
                "title":    title,
                "company":  name,
                "location": loc,
                "link":     link,
                "source":   "Lever",
            })
        return jobs
    except Exception:
        return []


# ─────────────────────── Indeed ──────────────────────────────────

def scrape_indeed(query: str) -> list[dict]:
    jobs = []
    try:
        encoded = urllib.parse.quote_plus(query)
        url = f"https://www.indeed.com/jobs?q={encoded}&l=Remote&sort=date"
        r = requests.get(url, headers=_h(), timeout=14)
        if r.status_code != 200:
            return []
        soup = BeautifulSoup(r.text, "lxml")
        cards = soup.select("div.job_seen_beacon") or soup.select("div.jobsearch-SerpJobCard")
        for card in cards[:12]:
            title_el   = card.select_one("h2.jobTitle span[title]") or card.select_one("h2.jobTitle a")
            company_el = card.select_one("span.companyName") or card.select_one("[data-testid='company-name']")
            loc_el     = card.select_one("div.companyLocation") or card.select_one("[data-testid='text-location']")
            link_el    = card.select_one("h2.jobTitle a") or card.select_one("a.jcs-JobTitle")
            title = (title_el.get("title") or title_el.get_text(strip=True)) if title_el else None
            if not title:
                continue
            href = link_el.get("href", "") if link_el else ""
            link = f"https://www.indeed.com{href}" if href.startswith("/") else href or "https://www.indeed.com"
            jobs.append({
                "title":    title,
                "company":  company_el.get_text(strip=True) if company_el else "Company",
                "location": loc_el.get_text(strip=True) if loc_el else "Remote",
                "link":     link,
                "source":   "Indeed",
            })
    except Exception as e:
        print(f"[Indeed] {e}")
    return jobs


# ─────────────────────── Wellfound (AngelList) ───────────────────

def scrape_wellfound(query: str) -> list[dict]:
    jobs = []
    try:
        encoded = urllib.parse.quote_plus(query)
        url = f"https://wellfound.com/jobs?q={encoded}&remote=true"
        r = requests.get(url, headers=_h(), timeout=14)
        if r.status_code != 200:
            return []
        soup = BeautifulSoup(r.text, "lxml")
        cards = (
            soup.select("div[data-test='StartupResult']") or
            soup.select("div.styles_result__C3mMZ") or
            soup.select("div[class*='JobListing']")
        )
        for card in cards[:12]:
            title_el   = card.select_one("a[data-test='job-link']") or card.select_one("h2 a")
            company_el = card.select_one("a[data-test='startup-link']") or card.select_one("h3")
            loc_el     = card.select_one("span[data-test='location']") or card.select_one("[class*='location']")
            if not title_el:
                continue
            href = title_el.get("href", "")
            link = f"https://wellfound.com{href}" if href.startswith("/") else href or "https://wellfound.com/jobs"
            jobs.append({
                "title":    title_el.get_text(strip=True),
                "company":  company_el.get_text(strip=True) if company_el else "Startup",
                "location": loc_el.get_text(strip=True) if loc_el else "Remote",
                "link":     link,
                "source":   "Wellfound",
            })
    except Exception as e:
        print(f"[Wellfound] {e}")
    return jobs


# ─────────────────────── RemoteOK ────────────────────────────────

def scrape_remoteok(query: str) -> list[dict]:
    """RemoteOK public JSON API — HTML page loads via JS so HTML scraping never worked."""
    jobs = []
    try:
        q_words = query.lower().split()
        # Tag-based API (server-filtered)
        tags = ",".join(q_words)
        r = requests.get(f"https://remoteok.com/api?&tags={tags}",
                         headers=_h(json_req=True), timeout=14)
        data = r.json() if r.status_code == 200 else []
        # Fall back to full listing if tag API returns nothing
        if not any(isinstance(d, dict) and d.get("position") for d in data):
            r = requests.get("https://remoteok.com/api", headers=_h(json_req=True), timeout=14)
            data = r.json() if r.status_code == 200 else []
        for item in data:
            if not isinstance(item, dict) or not item.get("position"):
                continue
            title_l = item["position"].lower()
            if not any(w in title_l for w in q_words):
                continue
            slug = item.get("slug", "")
            jobs.append({
                "title":    item["position"],
                "company":  item.get("company", "Company"),
                "location": "Remote",
                "link":     f"https://remoteok.com/remote-jobs/{slug}" if slug else "https://remoteok.com",
                "source":   "RemoteOK",
            })
            if len(jobs) >= 15:
                break
    except Exception as e:
        print(f"[RemoteOK] {e}")
    return jobs


# ─────────────────────── We Work Remotely ────────────────────────

def scrape_wwr(query: str) -> list[dict]:
    jobs = []
    try:
        encoded = urllib.parse.quote_plus(query)
        url = f"https://weworkremotely.com/remote-jobs/search?term={encoded}"
        r = requests.get(url, headers=_h(), timeout=14)
        if r.status_code != 200:
            return []
        soup = BeautifulSoup(r.text, "lxml")
        items = soup.select("li.feature, li.new-listing-container")
        for item in items[:12]:
            title_el   = (item.select_one("span[class*='title__text']") or
                          item.select_one("span.title") or item.select_one("h4"))
            link_el    = item.select_one("a[href*='/remote-jobs/']")
            if not title_el:
                continue
            href = link_el.get("href", "") if link_el else ""
            link = f"https://weworkremotely.com{href}" if href.startswith("/") else href or "https://weworkremotely.com"
            # Company name is in the tooltip alt text of the logo div
            img = item.select_one("div[alt]")
            company_name = img.get("alt", "Company").split(" is hiring")[0] if img else "Company"
            jobs.append({
                "title":    title_el.get_text(strip=True),
                "company":  company_name,
                "location": "Remote",
                "link":     link,
                "source":   "WeWorkRemotely",
            })
    except Exception as e:
        print(f"[WWR] {e}")
    return jobs


INDIA_CITIES = {
    "india", "bengaluru", "bangalore", "mumbai", "delhi", "hyderabad", "pune",
    "chennai", "kolkata", "noida", "gurgaon", "gurugram", "ahmedabad", "jaipur",
    "chandigarh", "kochi", "trivandrum", "bhubaneswar", "indore", "coimbatore",
}
UK_CITIES = {
    "united kingdom", "uk", "london", "manchester", "birmingham", "edinburgh",
    "glasgow", "bristol", "leeds", "liverpool", "sheffield", "oxford", "cambridge",
    "belfast", "cardiff", "nottingham", "reading", "coventry", "newcastle",
}
US_CITIES = {
    "united states", "usa", "u.s.a", "new york", "san francisco", "seattle",
    "austin", "boston", "chicago", "los angeles", "denver", "atlanta", "miami",
    "dallas", "washington dc", "san jose", "portland", "remote, us", "remote (us)",
}
EU_COUNTRIES = {
    "germany", "berlin", "munich", "frankfurt", "hamburg",
    "netherlands", "amsterdam", "france", "paris",
    "spain", "madrid", "barcelona", "portugal", "lisbon",
    "sweden", "stockholm", "denmark", "copenhagen",
    "ireland", "dublin", "switzerland", "zurich", "geneva",
    "poland", "warsaw", "austria", "vienna", "europe",
}
AU_CITIES = {
    "australia", "sydney", "melbourne", "brisbane", "perth", "adelaide", "canberra",
}
CA_CITIES = {
    "canada", "toronto", "vancouver", "montreal", "calgary", "ottawa",
}

def is_india_location(loc: str) -> bool:
    loc_l = loc.lower()
    return any(city in loc_l for city in INDIA_CITIES)

def detect_country(loc: str) -> str:
    """Return a country code for a location string."""
    loc_l = loc.lower()
    if any(c in loc_l for c in UK_CITIES):    return "uk"
    if any(c in loc_l for c in US_CITIES):    return "us"
    if any(c in loc_l for c in AU_CITIES):    return "au"
    if any(c in loc_l for c in CA_CITIES):    return "ca"
    if any(c in loc_l for c in EU_COUNTRIES): return "eu"
    if any(c in loc_l for c in INDIA_CITIES): return "in"
    if "remote" in loc_l:                     return "remote"
    return "other"


# ─────────────────────── Naukri ──────────────────────────────────

def scrape_naukri(query: str) -> list[dict]:
    jobs = []
    try:
        slug = query.lower().replace(" ", "-")
        url  = f"https://www.naukri.com/{slug}-jobs"
        r = requests.get(url, headers=_h(), timeout=14)
        if r.status_code != 200:
            return []
        soup  = BeautifulSoup(r.text, "lxml")
        cards = (
            soup.select("article.jobTuple") or
            soup.select("[class*='jobTuple']") or
            soup.select("[class*='job-tuple']")
        )
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                import json as _json
                data = _json.loads(script.string or "")
                items = data if isinstance(data, list) else [data]
                for item in items:
                    if isinstance(item, dict) and item.get("@type") == "JobPosting":
                        loc = item.get("jobLocation", {})
                        addr = loc.get("address", {}) if isinstance(loc, dict) else {}
                        jobs.append({
                            "title":    item.get("title", "Job"),
                            "company":  item.get("hiringOrganization", {}).get("name", "Company"),
                            "location": addr.get("addressLocality", "India"),
                            "link":     item.get("url", url),
                            "source":   "Naukri",
                        })
            except Exception:
                pass
        for card in cards[:12]:
            title_el   = card.select_one("a.title") or card.select_one("[class*='title'] a") or card.select_one("a[title]")
            company_el = card.select_one("a.subTitle") or card.select_one("[class*='company']")
            loc_el     = card.select_one("li.location") or card.select_one("[class*='location']")
            if title_el:
                jobs.append({
                    "title":    title_el.get("title") or title_el.get_text(strip=True),
                    "company":  company_el.get_text(strip=True) if company_el else "Company",
                    "location": loc_el.get_text(strip=True) if loc_el else "India",
                    "link":     title_el.get("href", url),
                    "source":   "Naukri",
                })
    except Exception as e:
        print(f"[Naukri] {e}")
    return jobs


# ─────────────────────── Internshala ─────────────────────────────

def scrape_internshala(query: str) -> list[dict]:
    jobs = []
    try:
        slug = query.lower().replace(" ", "-")
        url  = f"https://internshala.com/jobs/{slug}-jobs"
        r = requests.get(url, headers=_h(), timeout=14)
        if r.status_code != 200:
            return []
        soup  = BeautifulSoup(r.text, "lxml")
        cards = soup.select("div.individual_internship") or soup.select("div.internship-item")
        for card in cards[:12]:
            title_el   = (card.select_one("h3.job-internship-name a") or
                           card.select_one("a.job-title-href") or card.select_one("h3 a"))
            company_el = card.select_one("p.company-name") or card.select_one(".company-name")
            loc_el     = card.select_one("a#location_names") or card.select_one("[id*='location']")
            if title_el:
                href = title_el.get("href", "")
                link = f"https://internshala.com{href}" if href.startswith("/") else href or url
                jobs.append({
                    "title":    title_el.get_text(strip=True),
                    "company":  company_el.get_text(strip=True) if company_el else "Company",
                    "location": loc_el.get_text(strip=True) if loc_el else "India",
                    "link":     link,
                    "source":   "Internshala",
                })
    except Exception as e:
        print(f"[Internshala] {e}")
    return jobs


# ─────────────────────── TimesJobs ───────────────────────────────

def scrape_timesjobs(query: str) -> list[dict]:
    jobs = []
    try:
        encoded = urllib.parse.quote_plus(query)
        url = f"https://www.timesjobs.com/candidate/job-search.html?searchType=personalizedSearch&from=submit&txtKeywords={encoded}&txtLocation=India"
        r = requests.get(url, headers=_h(), timeout=14)
        if r.status_code != 200:
            return []
        soup  = BeautifulSoup(r.text, "lxml")
        cards = soup.select("li.clearfix.job-bx.wht-shd-bx") or soup.select("[class*='job-bx']")
        for card in cards[:12]:
            title_el   = card.select_one("h2 a") or card.select_one(".job-title a")
            company_el = card.select_one("h3.joblist-comp-name") or card.select_one("[class*='comp-name']")
            loc_el     = card.select_one("li.srp-zindex span.srp-skills") or card.select_one("[class*='location']")
            if title_el:
                href = title_el.get("href", "")
                jobs.append({
                    "title":    title_el.get_text(strip=True),
                    "company":  company_el.get_text(strip=True) if company_el else "Company",
                    "location": loc_el.get_text(strip=True) if loc_el else "India",
                    "link":     href or url,
                    "source":   "TimesJobs",
                })
    except Exception as e:
        print(f"[TimesJobs] {e}")
    return jobs


# ─────────────────────── Shine ───────────────────────────────────

def scrape_shine(query: str) -> list[dict]:
    jobs = []
    try:
        encoded = urllib.parse.quote_plus(query)
        url = f"https://www.shine.com/job-search/{encoded.replace('+','-')}-jobs"
        r = requests.get(url, headers=_h(), timeout=14)
        if r.status_code != 200:
            return []
        soup  = BeautifulSoup(r.text, "lxml")
        cards = soup.select("div.jobCard") or soup.select("[class*='job-card']") or soup.select("article.job")
        for card in cards[:12]:
            title_el   = card.select_one("h3 a") or card.select_one(".job-title a")
            company_el = card.select_one("[class*='company']") or card.select_one("p.company")
            loc_el     = card.select_one("[class*='location']") or card.select_one("span.location")
            if title_el:
                href = title_el.get("href", "")
                link = f"https://www.shine.com{href}" if href.startswith("/") else href or url
                jobs.append({
                    "title":    title_el.get_text(strip=True),
                    "company":  company_el.get_text(strip=True) if company_el else "Company",
                    "location": loc_el.get_text(strip=True) if loc_el else "India",
                    "link":     link,
                    "source":   "Shine",
                })
    except Exception as e:
        print(f"[Shine] {e}")
    return jobs


# ─────────────────────── Foundit (Monster India) ─────────────────

def scrape_foundit(query: str) -> list[dict]:
    jobs = []
    try:
        encoded = urllib.parse.quote_plus(query)
        url = f"https://www.foundit.in/srp/results?query={encoded}&locationId=121"  # 121 = India
        r = requests.get(url, headers=_h(), timeout=14)
        if r.status_code != 200:
            return []
        soup  = BeautifulSoup(r.text, "lxml")
        cards = soup.select("div.srpResultCardContainer") or soup.select("[class*='jobCard']")
        for card in cards[:12]:
            title_el   = card.select_one("h3.jobTitle a") or card.select_one("a.jobTitle")
            company_el = card.select_one("div.companyName") or card.select_one("[class*='company']")
            loc_el     = card.select_one("div.location") or card.select_one("[class*='location']")
            if title_el:
                href = title_el.get("href", "")
                link = f"https://www.foundit.in{href}" if href.startswith("/") else href or url
                jobs.append({
                    "title":    title_el.get_text(strip=True),
                    "company":  company_el.get_text(strip=True) if company_el else "Company",
                    "location": loc_el.get_text(strip=True) if loc_el else "India",
                    "link":     link,
                    "source":   "Foundit",
                })
    except Exception as e:
        print(f"[Foundit] {e}")
    return jobs


# ─────────────────────── LinkedIn ────────────────────────────────

def scrape_linkedin_india(query: str) -> list[dict]:
    jobs = []
    try:
        encoded = urllib.parse.quote(query)
        url = f"https://www.linkedin.com/jobs/search/?keywords={encoded}&location=India"
        r = requests.get(url, headers=_h(), timeout=14)
        if r.status_code != 200:
            return []
        soup = BeautifulSoup(r.text, "lxml")
        cards = (
            soup.select("ul.jobs-search__results-list li") or
            soup.select("div.base-card") or
            soup.select("div.job-search-card")
        )
        for card in cards[:15]:
            title_el   = card.select_one("h3.base-search-card__title") or card.select_one("h3")
            company_el = card.select_one("h4.base-search-card__subtitle") or card.select_one("a.job-card-container__company-name")
            loc_el     = card.select_one("span.job-search-card__location") or card.select_one("[class*='location']")
            link_el    = card.select_one("a.base-card__full-link") or card.select_one("a[href*='/jobs/view/']")
            if not title_el:
                continue
            jobs.append({
                "title":    title_el.get_text(strip=True),
                "company":  company_el.get_text(strip=True) if company_el else "Company",
                "location": loc_el.get_text(strip=True) if loc_el else "India",
                "link":     link_el["href"] if link_el else url,
                "source":   "LinkedIn",
            })
    except Exception as e:
        print(f"[LinkedIn India] {e}")
    return jobs


def scrape_linkedin(query: str) -> list[dict]:
    jobs = []
    try:
        encoded = urllib.parse.quote(query)
        url = f"https://www.linkedin.com/jobs/search/?keywords={encoded}&f_WT=2"
        r = requests.get(url, headers=_h(), timeout=14)
        if r.status_code != 200:
            return []
        soup = BeautifulSoup(r.text, "lxml")
        cards = (
            soup.select("ul.jobs-search__results-list li") or
            soup.select("div.base-card") or
            soup.select("div.job-search-card")
        )
        for card in cards[:12]:
            title_el   = card.select_one("h3.base-search-card__title") or card.select_one("h3")
            company_el = card.select_one("h4.base-search-card__subtitle") or card.select_one("a.job-card-container__company-name")
            loc_el     = card.select_one("span.job-search-card__location") or card.select_one("[class*='location']")
            link_el    = card.select_one("a.base-card__full-link") or card.select_one("a[href*='/jobs/view/']")
            if not title_el:
                continue
            jobs.append({
                "title":    title_el.get_text(strip=True),
                "company":  company_el.get_text(strip=True) if company_el else "Company",
                "location": loc_el.get_text(strip=True) if loc_el else "Remote",
                "link":     link_el["href"] if link_el else url,
                "source":   "LinkedIn",
            })
    except Exception as e:
        print(f"[LinkedIn] {e}")
    return jobs


# ─────────────────────── Glassdoor ───────────────────────────────

def scrape_glassdoor(query: str) -> list[dict]:
    jobs = []
    try:
        encoded = urllib.parse.quote_plus(query)
        url = f"https://www.glassdoor.com/Job/remote-{encoded}-jobs-SRCH_IL.0,6_IS11047_KO7,{7+len(query)}.htm"
        r = requests.get(url, headers=_h(), timeout=14)
        if r.status_code != 200:
            return []
        soup = BeautifulSoup(r.text, "lxml")
        cards = soup.select("li[data-test='jobListing']") or soup.select("div.react-job-listing")
        for card in cards[:10]:
            title_el   = card.select_one("a[data-test='job-link']") or card.select_one("a.jobLink")
            company_el = card.select_one("div.job-search-key-rlvkpd") or card.select_one("[class*='employerName']")
            loc_el     = card.select_one("[data-test='emp-location']") or card.select_one("[class*='location']")
            if not title_el:
                continue
            href = title_el.get("href", "")
            link = f"https://www.glassdoor.com{href}" if href.startswith("/") else href or "https://www.glassdoor.com/Job"
            jobs.append({
                "title":    title_el.get_text(strip=True),
                "company":  company_el.get_text(strip=True) if company_el else "Company",
                "location": loc_el.get_text(strip=True) if loc_el else "Remote",
                "link":     link,
                "source":   "Glassdoor",
            })
    except Exception as e:
        print(f"[Glassdoor] {e}")
    return jobs


# ─────────────────────── Reed.co.uk (UK jobs) ───────────────────

def scrape_reed_uk(query: str) -> list[dict]:
    jobs = []
    try:
        encoded = urllib.parse.quote_plus(query)
        url = f"https://www.reed.co.uk/jobs/{encoded}-jobs"
        r = requests.get(url, headers=_h(), timeout=14)
        if r.status_code != 200:
            return []
        soup = BeautifulSoup(r.text, "lxml")
        cards = soup.select("article.job-result") or soup.select("[data-testid='job-card']")
        for card in cards[:12]:
            title_el   = card.select_one("h2 a") or card.select_one("a[data-gtm-id='job-card-title']")
            company_el = card.select_one("a[data-gtm-id='company-name']") or card.select_one("[class*='employer']")
            loc_el     = card.select_one("li.location") or card.select_one("[data-testid='location']")
            if not title_el:
                continue
            href = title_el.get("href", "")
            link = f"https://www.reed.co.uk{href}" if href.startswith("/") else href or "https://www.reed.co.uk/jobs"
            jobs.append({
                "title":    title_el.get_text(strip=True),
                "company":  company_el.get_text(strip=True) if company_el else "Company",
                "location": loc_el.get_text(strip=True) if loc_el else "United Kingdom",
                "link":     link,
                "source":   "Reed UK",
            })
    except Exception as e:
        print(f"[Reed UK] {e}")
    return jobs


def scrape_linkedin_uk(query: str) -> list[dict]:
    jobs = []
    try:
        encoded = urllib.parse.quote(query)
        url = f"https://www.linkedin.com/jobs/search/?keywords={encoded}&location=United+Kingdom"
        r = requests.get(url, headers=_h(), timeout=14)
        if r.status_code != 200:
            return []
        soup = BeautifulSoup(r.text, "lxml")
        cards = (
            soup.select("ul.jobs-search__results-list li") or
            soup.select("div.base-card") or
            soup.select("div.job-search-card")
        )
        for card in cards[:12]:
            title_el   = card.select_one("h3.base-search-card__title") or card.select_one("h3")
            company_el = card.select_one("h4.base-search-card__subtitle") or card.select_one("a.job-card-container__company-name")
            loc_el     = card.select_one("span.job-search-card__location") or card.select_one("[class*='location']")
            link_el    = card.select_one("a.base-card__full-link") or card.select_one("a[href*='/jobs/view/']")
            if not title_el:
                continue
            jobs.append({
                "title":    title_el.get_text(strip=True),
                "company":  company_el.get_text(strip=True) if company_el else "Company",
                "location": loc_el.get_text(strip=True) if loc_el else "United Kingdom",
                "link":     link_el["href"] if link_el else url,
                "source":   "LinkedIn UK",
            })
    except Exception as e:
        print(f"[LinkedIn UK] {e}")
    return jobs


def scrape_linkedin_us(query: str) -> list[dict]:
    jobs = []
    try:
        encoded = urllib.parse.quote(query)
        url = f"https://www.linkedin.com/jobs/search/?keywords={encoded}&location=United+States"
        r = requests.get(url, headers=_h(), timeout=14)
        if r.status_code != 200:
            return []
        soup = BeautifulSoup(r.text, "lxml")
        cards = (
            soup.select("ul.jobs-search__results-list li") or
            soup.select("div.base-card") or
            soup.select("div.job-search-card")
        )
        for card in cards[:12]:
            title_el   = card.select_one("h3.base-search-card__title") or card.select_one("h3")
            company_el = card.select_one("h4.base-search-card__subtitle") or card.select_one("a.job-card-container__company-name")
            loc_el     = card.select_one("span.job-search-card__location") or card.select_one("[class*='location']")
            link_el    = card.select_one("a.base-card__full-link") or card.select_one("a[href*='/jobs/view/']")
            if not title_el:
                continue
            jobs.append({
                "title":    title_el.get_text(strip=True),
                "company":  company_el.get_text(strip=True) if company_el else "Company",
                "location": loc_el.get_text(strip=True) if loc_el else "United States",
                "link":     link_el["href"] if link_el else url,
                "source":   "LinkedIn US",
            })
    except Exception as e:
        print(f"[LinkedIn US] {e}")
    return jobs


def scrape_seek_au(query: str) -> list[dict]:
    """Seek.com.au — Australia's largest job board."""
    jobs = []
    try:
        encoded = urllib.parse.quote_plus(query)
        url = f"https://www.seek.com.au/{encoded}-jobs"
        r = requests.get(url, headers=_h(), timeout=14)
        if r.status_code != 200:
            return []
        soup = BeautifulSoup(r.text, "lxml")
        cards = soup.select("article[data-automation='normalJob']") or soup.select("[data-automation='jobCard']")
        for card in cards[:12]:
            title_el   = card.select_one("a[data-automation='jobTitle']") or card.select_one("h3 a")
            company_el = card.select_one("a[data-automation='jobCompany']") or card.select_one("[class*='company']")
            loc_el     = card.select_one("a[data-automation='jobLocation']") or card.select_one("[class*='location']")
            if not title_el:
                continue
            href = title_el.get("href", "")
            link = f"https://www.seek.com.au{href}" if href.startswith("/") else href or "https://www.seek.com.au"
            jobs.append({
                "title":    title_el.get_text(strip=True),
                "company":  company_el.get_text(strip=True) if company_el else "Company",
                "location": loc_el.get_text(strip=True) if loc_el else "Australia",
                "link":     link,
                "source":   "Seek AU",
            })
    except Exception as e:
        print(f"[Seek AU] {e}")
    return jobs


def scrape_linkedin_eu(query: str) -> list[dict]:
    """LinkedIn search scoped to Europe."""
    jobs = []
    try:
        encoded = urllib.parse.quote(query)
        url = f"https://www.linkedin.com/jobs/search/?keywords={encoded}&location=Europe"
        r = requests.get(url, headers=_h(), timeout=14)
        if r.status_code != 200:
            return []
        soup = BeautifulSoup(r.text, "lxml")
        cards = (
            soup.select("ul.jobs-search__results-list li") or
            soup.select("div.base-card") or
            soup.select("div.job-search-card")
        )
        for card in cards[:12]:
            title_el   = card.select_one("h3.base-search-card__title") or card.select_one("h3")
            company_el = card.select_one("h4.base-search-card__subtitle") or card.select_one("a.job-card-container__company-name")
            loc_el     = card.select_one("span.job-search-card__location") or card.select_one("[class*='location']")
            link_el    = card.select_one("a.base-card__full-link") or card.select_one("a[href*='/jobs/view/']")
            if not title_el:
                continue
            jobs.append({
                "title":    title_el.get_text(strip=True),
                "company":  company_el.get_text(strip=True) if company_el else "Company",
                "location": loc_el.get_text(strip=True) if loc_el else "Europe",
                "link":     link_el["href"] if link_el else url,
                "source":   "LinkedIn EU",
            })
    except Exception as e:
        print(f"[LinkedIn EU] {e}")
    return jobs


# ─────────────────────── Full background scrape ──────────────────

INDIA_SEARCH_TERMS = [
    "software engineer", "data scientist", "product manager", "data engineer",
    "machine learning engineer", "frontend developer", "backend developer",
    "full stack developer", "devops engineer", "cloud engineer",
    "mobile developer", "ui ux designer", "business analyst",
    "java developer", "python developer", "react developer",
    "android developer", "ios developer", "qa engineer",
]

BOARD_SEARCH_TERMS = [
    "software engineer", "data scientist", "product manager", "data engineer",
    "machine learning engineer", "frontend engineer", "backend engineer",
    "full stack engineer", "devops engineer", "cloud engineer",
    "mobile engineer", "security engineer", "design", "marketing",
    "sales", "operations", "finance", "hr", "customer success",
]


def is_scrape_running() -> bool:
    return _SCRAPE_RUNNING


def run_full_scrape():
    global _SCRAPE_RUNNING
    if _SCRAPE_RUNNING:
        print("[Scraper] Already running, skipping.")
        return
    _SCRAPE_RUNNING = True
    log_id = db.start_scrape_log()
    total_saved = 0
    print(f"[Scraper] Starting full scrape — {len(GREENHOUSE_COMPANIES)} Greenhouse + {len(LEVER_COMPANIES)} Lever companies")

    try:
        db.clear_old_jobs(keep_days=2)

        # ── Greenhouse: India-only pass over ALL companies ──
        gh_jobs: list[dict] = []
        with ThreadPoolExecutor(max_workers=25) as ex:
            futs = {ex.submit(scrape_greenhouse, c, True): c for c in GREENHOUSE_COMPANIES}
            for fut in as_completed(futs):
                gh_jobs.extend(fut.result())
        n = db.save_jobs(gh_jobs)
        total_saved += n
        print(f"[Scraper] Greenhouse India: {len(gh_jobs)} fetched, {n} new saved")

        # ── Greenhouse: full pass for Indian-HQ companies ──
        from companies import GREENHOUSE_COMPANIES as GH_ALL
        indian_gh = [c for c in GH_ALL if any(
            kw in c["name"].lower() for kw in [
                "freshworks","chargebee","browserstack","postman","hasura",
                "razorpay","clevertap","moengage","zoho","paytm","swiggy",
                "zomato","meesho","zepto","phonepe","flipkart","nykaa",
                "unacademy","upgrad","lenskart","groww","smallcase","zerodha",
                "juspay","sarvam","krutrim","sprinklr","innovaccer","fractal",
                "mu sigma","tiger","sigmoid","whatfix","haptik","yellow",
                "uniphore","leena","mindtickle","vymo","observe",
            ]
        )]
        gh_indian_co: list[dict] = []
        with ThreadPoolExecutor(max_workers=20) as ex:
            futs = {ex.submit(scrape_greenhouse, c, False): c for c in indian_gh}
            for fut in as_completed(futs):
                gh_indian_co.extend(fut.result())
        n = db.save_jobs(gh_indian_co)
        total_saved += n
        print(f"[Scraper] Indian GH companies (all roles): {len(gh_indian_co)} fetched, {n} new saved")

        # ── Lever: India-only pass over ALL companies ──
        lv_jobs: list[dict] = []
        with ThreadPoolExecutor(max_workers=25) as ex:
            futs = {ex.submit(scrape_lever, c, True): c for c in LEVER_COMPANIES}
            for fut in as_completed(futs):
                lv_jobs.extend(fut.result())
        n = db.save_jobs(lv_jobs)
        total_saved += n
        print(f"[Scraper] Lever India: {len(lv_jobs)} fetched, {n} new saved")

        # ── Lever: full pass for Indian-HQ companies ──
        from companies import LEVER_COMPANIES as LV_ALL
        indian_lv = [c for c in LV_ALL if any(
            kw in c["name"].lower() for kw in [
                "razorpay","cred","zepto","meesho","phonepe","ola","swiggy","zomato",
                "paytm","flipkart","myntra","udaan","inmobi","sharechat","dream11",
                "mpl","classplus","emeritus","simplilearn","scaler","cars24","spinny",
                "urban","apna","hackerearth","hackerrank","geeks","codechef","whatfix",
                "cashfree","setu","decentro","perfios","signzy","yellow","haptik",
                "observe","uniphore","fractal","tiger","sigmoid","healthifyme",
                "clevertap","moengage","webengage","zoho","leena","mindtickle",
            ]
        )]
        lv_indian_co: list[dict] = []
        with ThreadPoolExecutor(max_workers=20) as ex:
            futs = {ex.submit(scrape_lever, c, False): c for c in indian_lv}
            for fut in as_completed(futs):
                lv_indian_co.extend(fut.result())
        n = db.save_jobs(lv_indian_co)
        total_saved += n
        print(f"[Scraper] Indian LV companies (all roles): {len(lv_indian_co)} fetched, {n} new saved")

        # ── India job boards (parallel, 8 workers) ──
        def scrape_india_for_term(term):
            results = []
            for fn in [scrape_naukri, scrape_internshala, scrape_timesjobs, scrape_shine, scrape_foundit, scrape_linkedin_india]:
                try:
                    results.extend(fn(term))
                    time.sleep(0.4)
                except Exception:
                    pass
            return results

        india_jobs: list[dict] = []
        with ThreadPoolExecutor(max_workers=8) as ex:
            futs = [ex.submit(scrape_india_for_term, t) for t in INDIA_SEARCH_TERMS]
            for fut in as_completed(futs):
                india_jobs.extend(fut.result())
        n = db.save_jobs(india_jobs)
        total_saved += n
        print(f"[Scraper] India boards: {len(india_jobs)} fetched, {n} new saved")

        # ── Global remote job boards ──
        def scrape_boards_for_term(term):
            results = []
            for fn in [scrape_remoteok, scrape_wwr]:
                try:
                    results.extend(fn(term))
                    time.sleep(0.3)
                except Exception:
                    pass
            return results

        board_jobs: list[dict] = []
        with ThreadPoolExecutor(max_workers=8) as ex:
            futs = [ex.submit(scrape_boards_for_term, t) for t in BOARD_SEARCH_TERMS]
            for fut in as_completed(futs):
                board_jobs.extend(fut.result())
        n = db.save_jobs(board_jobs)
        total_saved += n
        print(f"[Scraper] Global boards: {len(board_jobs)} fetched, {n} new saved")

        # ── International boards (UK / US / EU / AU) ──
        INTL_TERMS = [
            "software engineer", "data scientist", "product manager", "data engineer",
            "machine learning engineer", "frontend developer", "backend developer",
            "full stack developer", "devops engineer", "cloud engineer",
        ]
        def scrape_intl_for_term(term):
            results = []
            for fn in [scrape_reed_uk, scrape_linkedin_uk, scrape_linkedin_us, scrape_seek_au, scrape_linkedin_eu]:
                try:
                    results.extend(fn(term))
                    time.sleep(0.4)
                except Exception:
                    pass
            return results

        intl_jobs: list[dict] = []
        with ThreadPoolExecutor(max_workers=6) as ex:
            futs = [ex.submit(scrape_intl_for_term, t) for t in INTL_TERMS]
            for fut in as_completed(futs):
                intl_jobs.extend(fut.result())
        n = db.save_jobs(intl_jobs)
        total_saved += n
        print(f"[Scraper] International (UK/US/EU/AU): {len(intl_jobs)} fetched, {n} new saved")

        db.finish_scrape_log(log_id, total_saved, "done")
        print(f"[Scraper] Done. Total jobs in DB: {db.total_jobs()}")
    except Exception as e:
        print(f"[Scraper] Error: {e}")
        db.finish_scrape_log(log_id, total_saved, "error")
    finally:
        _SCRAPE_RUNNING = False


def run_render_scrape():
    """Lightweight scrape for Render free tier (512 MB RAM).
    Only hits Greenhouse + Lever JSON APIs — no HTML scrapers, no ThreadPoolExecutor spam."""
    global _SCRAPE_RUNNING
    if _SCRAPE_RUNNING:
        print("[Scraper] Already running, skipping.")
        return
    _SCRAPE_RUNNING = True
    log_id = db.start_scrape_log()
    total_saved = 0
    print(f"[Scraper] Render lightweight scrape — {len(GREENHOUSE_COMPANIES)} GH + {len(LEVER_COMPANIES)} Lever")
    try:
        db.clear_old_jobs(keep_days=2)

        gh_jobs: list[dict] = []
        with ThreadPoolExecutor(max_workers=10) as ex:
            futs = {ex.submit(scrape_greenhouse, c, True): c for c in GREENHOUSE_COMPANIES}
            for fut in as_completed(futs):
                gh_jobs.extend(fut.result())
        n = db.save_jobs(gh_jobs)
        total_saved += n
        print(f"[Scraper] Greenhouse India: {len(gh_jobs)} fetched, {n} saved")

        lv_jobs: list[dict] = []
        with ThreadPoolExecutor(max_workers=10) as ex:
            futs = {ex.submit(scrape_lever, c, True): c for c in LEVER_COMPANIES}
            for fut in as_completed(futs):
                lv_jobs.extend(fut.result())
        n = db.save_jobs(lv_jobs)
        total_saved += n
        print(f"[Scraper] Lever India: {len(lv_jobs)} fetched, {n} saved")

        db.finish_scrape_log(log_id, total_saved, "done")
        print(f"[Scraper] Done. Total jobs in DB: {db.total_jobs()}")
    except Exception as e:
        print(f"[Scraper] Error: {e}")
        db.finish_scrape_log(log_id, total_saved, "error")
    finally:
        _SCRAPE_RUNNING = False


def _greenhouse_search(query: str) -> list[dict]:
    """Search all cached Greenhouse India jobs by query words."""
    q_words = query.lower().split()
    results = []
    with ThreadPoolExecutor(max_workers=20) as ex:
        futs = {ex.submit(scrape_greenhouse, c, True): c for c in GREENHOUSE_COMPANIES}
        for fut in as_completed(futs):
            for job in fut.result():
                if any(w in job["title"].lower() for w in q_words):
                    results.append(job)
    return results


def _filter_by_query(jobs: list[dict], query: str) -> list[dict]:
    """Keep only jobs where at least one query word appears in the title."""
    q_words = [w for w in query.lower().split() if len(w) > 2]
    if not q_words:
        return jobs
    return [j for j in jobs if any(w in j.get("title", "").lower() for w in q_words)]


def _dedupe(jobs: list[dict]) -> list[dict]:
    """Drop the same posting appearing on multiple platforms (normalized
    title + company), keeping the first occurrence."""
    seen, out = set(), []
    for j in jobs:
        key = (
            re.sub(r"[^a-z0-9]", "", (j.get("title") or "").lower()),
            re.sub(r"[^a-z0-9]", "", (j.get("company") or "").lower()),
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(j)
    return out


def _synonym_queries(query: str) -> list[str]:
    """Related-title variants to broaden a thin result set."""
    words = query.lower().split()
    variants: list[str] = []
    for i, w in enumerate(words):
        for syn in _TITLE_SYNONYMS.get(w, []):
            variants.append(" ".join(words[:i] + [syn] + words[i + 1:]))
    # Also a broadened 2-word version of the original query.
    meaningful = [w for w in words if len(w) > 2]
    if len(meaningful) >= 2:
        variants.append(" ".join(meaningful[:2]))
    # De-dup, drop the original.
    out, seen = [], {query.lower()}
    for v in variants:
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out


# ─────────── Extra free JSON-API sources (reliable, rarely blocked) ──────────
# These use official public JSON endpoints (no key), so they don't break the
# way HTML scraping does. They dramatically widen coverage — especially Europe.

def _kw_match(title: str, query: str) -> bool:
    q_words = [w for w in query.lower().split() if len(w) > 1]
    t = (title or "").lower()
    return any(w in t for w in q_words) if q_words else True


def scrape_arbeitnow(query: str) -> list[dict]:
    """Arbeitnow — Europe-wide job board (Germany/NL/EU heavy). Free JSON API."""
    jobs = []
    try:
        r = requests.get("https://www.arbeitnow.com/api/job-board-api",
                         headers=_h(json_req=True), timeout=12)
        if r.status_code != 200:
            return []
        for j in r.json().get("data", []):
            title = j.get("title") or ""
            if not title or not _kw_match(title, query):
                continue
            loc = j.get("location") or ("Remote" if j.get("remote") else "Europe")
            jobs.append({
                "title":    title,
                "company":  j.get("company_name") or "Company",
                "location": loc,
                "link":     j.get("url") or "https://www.arbeitnow.com",
                "source":   "Arbeitnow",
            })
            if len(jobs) >= 15:
                break
    except Exception as e:
        print(f"[Arbeitnow] {e}")
    return jobs


def scrape_remotive(query: str) -> list[dict]:
    """Remotive — remote jobs worldwide (many Europe-friendly). Free JSON API."""
    jobs = []
    try:
        r = requests.get("https://remotive.com/api/remote-jobs",
                         params={"search": query, "limit": 40},
                         headers=_h(json_req=True), timeout=12)
        if r.status_code != 200:
            return []
        for j in r.json().get("jobs", []):
            title = j.get("title") or ""
            if not title:
                continue
            jobs.append({
                "title":    title,
                "company":  j.get("company_name") or "Company",
                "location": j.get("candidate_required_location") or "Remote",
                "link":     j.get("url") or "https://remotive.com",
                "source":   "Remotive",
            })
            if len(jobs) >= 15:
                break
    except Exception as e:
        print(f"[Remotive] {e}")
    return jobs


def scrape_jobicy(query: str) -> list[dict]:
    """Jobicy — remote jobs with geo filtering. Free JSON API."""
    jobs = []
    try:
        r = requests.get("https://jobicy.com/api/v2/remote-jobs",
                         params={"count": 50, "tag": query},
                         headers=_h(json_req=True), timeout=12)
        if r.status_code != 200:
            return []
        for j in r.json().get("jobs", []):
            title = j.get("jobTitle") or ""
            if not title or not _kw_match(title, query):
                continue
            jobs.append({
                "title":    title,
                "company":  j.get("companyName") or "Company",
                "location": j.get("jobGeo") or "Remote",
                "link":     j.get("url") or "https://jobicy.com",
                "source":   "Jobicy",
            })
            if len(jobs) >= 15:
                break
    except Exception as e:
        print(f"[Jobicy] {e}")
    return jobs


def _fetch_sources(sources, query: str, include_greenhouse: bool = False) -> list[dict]:
    """Run each source concurrently with a per-source timeout; a slow/broken
    source is skipped, never blocking the batch."""
    raw: list[dict] = []
    with ThreadPoolExecutor(max_workers=10) as ex:
        futs = {ex.submit(fn, query): fn for fn in sources}
        gh_fut = ex.submit(_greenhouse_search, query) if include_greenhouse else None
        for fut in as_completed(futs):
            try:
                raw.extend(fut.result(timeout=SOURCE_TIMEOUT))
            except Exception:
                pass
        if gh_fut is not None:
            try:
                raw.extend(gh_fut.result(timeout=SOURCE_TIMEOUT + 4))
            except Exception:
                pass
    return raw


def scrape_live(query: str, country: str = "in", include_greenhouse: bool = False) -> list[dict]:
    """
    Live scrape for a query across the fast/reliable sources for the target
    country. Every source runs concurrently with a per-source timeout; slow
    platforms are skipped rather than blocking. Results are query-filtered,
    de-duplicated, and (if thin) broadened with related-title synonyms.
    Cached 30 min per (query, country).

    include_greenhouse defaults to FALSE: the Greenhouse search fans out to
    ~250 company boards and takes 1-2 min, so it must never run inside a
    user-facing request. Greenhouse/Lever coverage instead comes from the
    worker's periodic background full-scrape, which populates the cache.
    """
    cache_key = (query.lower().strip(), country)
    with _LIVE_CACHE_LOCK:
        cached = _LIVE_CACHE.get(cache_key)
    if cached is not None:
        return cached

    # Reliable API-based sources — work for all countries (remote coverage).
    api_sources = [scrape_remoteok, scrape_wwr, scrape_remotive, scrape_jobicy]

    if country == "uk":
        html_sources = [scrape_reed_uk, scrape_linkedin_uk, scrape_arbeitnow]
    elif country == "us":
        html_sources = [scrape_indeed, scrape_wellfound]
    elif country == "au":
        html_sources = [scrape_seek_au]
    elif country == "eu":
        # Europe now has real coverage: LinkedIn EU + Arbeitnow (DE/NL/EU-wide)
        # + the remote APIs in api_sources (many EU-eligible roles).
        html_sources = [scrape_linkedin_eu, scrape_arbeitnow]
    elif country == "remote":
        html_sources = [scrape_wellfound]
    else:  # "in" or default
        html_sources = [scrape_internshala, scrape_shine]

    raw_jobs = _fetch_sources(api_sources + html_sources, query, include_greenhouse=include_greenhouse)
    jobs = _dedupe(_filter_by_query(raw_jobs, query))

    # Broaden with related titles / synonyms if the result set is thin.
    if len(jobs) < 5:
        seen = {j.get("link") for j in jobs}
        for broad_query in _synonym_queries(query):
            broad_raw = _fetch_sources(api_sources, broad_query)
            for j in _filter_by_query(broad_raw, broad_query):
                if j.get("link") not in seen:
                    seen.add(j.get("link"))
                    jobs.append(j)
            if len(jobs) >= 8:
                break
        jobs = _dedupe(jobs)

    with _LIVE_CACHE_LOCK:
        _LIVE_CACHE[cache_key] = jobs
    return jobs
