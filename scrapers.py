import json
import random
import re
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from bs4 import BeautifulSoup

import db
from companies import GREENHOUSE_COMPANIES, LEVER_COMPANIES

_SCRAPE_RUNNING = False

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
    jobs = []
    try:
        import json as _json

        def _parse(data: list, strict: bool = False) -> list[dict]:
            out = []
            q_words = query.lower().split()
            threshold = len(q_words) if strict else 1
            for item in data:
                if not isinstance(item, dict) or not item.get("position"):
                    continue
                title_l = item.get("position", "").lower()
                matches = sum(1 for w in q_words if w in title_l)
                if matches < threshold:
                    continue
                slug = item.get("slug", "")
                out.append({
                    "title":    item.get("position", ""),
                    "company":  item.get("company", "Company"),
                    "location": "Remote",
                    "link":     f"https://remoteok.com/remote-jobs/{slug}" if slug else "https://remoteok.com",
                    "source":   "RemoteOK",
                })
                if len(out) >= 15:
                    break
            return out

        # Try tag-based API first (faster, server-filtered)
        q_words = query.lower().split()
        use_strict = len(q_words) >= 2
        tags = ",".join(q_words)
        r = requests.get(f"https://remoteok.com/api?&tags={tags}", headers=_h(json_req=True), timeout=14)
        if r.status_code == 200:
            jobs = _parse(_json.loads(r.text), strict=use_strict)

        # Fall back to full API with stricter client-side title filter
        if not jobs:
            r = requests.get("https://remoteok.com/api", headers=_h(json_req=True), timeout=14)
            if r.status_code == 200:
                jobs = _parse(_json.loads(r.text), strict=True)
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
            company_el = (item.select_one("span[class*='company']") or
                          item.select_one("div[class*='company']"))
            link_el    = item.select_one("a[href*='/remote-jobs/']")
            if not title_el:
                continue
            href = link_el.get("href", "") if link_el else ""
            link = f"https://weworkremotely.com{href}" if href.startswith("/") else href or "https://weworkremotely.com"
            # company often in tooltip alt text
            if not company_el:
                img = item.select_one("div[alt]")
                company_name = img.get("alt", "Company").split(" is hiring")[0] if img else "Company"
            else:
                company_name = company_el.get_text(strip=True)
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
        import json as _json
        encoded = urllib.parse.quote_plus(query)
        url = (
            f"https://www.naukri.com/jobapi/v3/search"
            f"?noOfResults=20&urlType=search_by_keyword&searchType=adv"
            f"&keyword={encoded}&sort=r&seoKey={encoded.replace('+','-')}-jobs"
            f"&src=jobsearchDesk&latLong="
        )
        headers = {
            **_h(json_req=True),
            "appid": "109",
            "systemid": "Naukri",
            "Referer": "https://www.naukri.com/",
        }
        r = requests.get(url, headers=headers, timeout=14)
        if r.status_code != 200:
            return []
        data = _json.loads(r.text)
        for item in (data.get("jobDetails") or [])[:20]:
            title   = item.get("title", "")
            company = (item.get("companyName") or "Company").strip()
            loc     = ", ".join(item.get("placeholders", [{}])[0].get("label", "India").split(",")[:2]) if item.get("placeholders") else "India"
            link    = item.get("jdURL") or item.get("jobId") or ""
            if not link.startswith("http"):
                link = f"https://www.naukri.com{link}"
            jobs.append({"title": title, "company": company, "location": loc, "link": link, "source": "Naukri"})
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
        cards = soup.select("[class*='jobCard']") or soup.select("[class*='job-card']")
        for card in cards[:12]:
            # Shine uses itemprop microdata
            url_meta   = card.select_one("meta[itemprop='url']")
            title_el   = card.select_one("h3[itemprop='name']") or card.select_one("h3")
            company_el = card.select_one("[class*='company']") or card.select_one("[itemprop='hiringOrganization']")
            loc_el     = card.select_one("[class*='location']") or card.select_one("[itemprop='addressLocality']")
            if title_el:
                href = url_meta.get("content", "") if url_meta else ""
                link = href if href.startswith("http") else (f"https://www.shine.com{href}" if href else url)
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


# ─────────────────────── Remotive (Remote — free API) ───────────

def scrape_remotive(query: str) -> list[dict]:
    jobs = []
    try:
        import json as _json
        r = requests.get(
            f"https://remotive.com/api/remote-jobs?search={urllib.parse.quote_plus(query)}&limit=50",
            headers=_h(json_req=True), timeout=14,
        )
        if r.status_code != 200:
            return []
        q_words = query.lower().split()
        for item in _json.loads(r.text).get("jobs", []):
            title_l = item.get("title", "").lower()
            # client-side filter: title must contain at least one query word
            if not any(w in title_l for w in q_words):
                continue
            jobs.append({
                "title":    item.get("title", ""),
                "company":  item.get("company_name", "Company"),
                "location": item.get("candidate_required_location") or "Remote",
                "link":     item.get("url", "https://remotive.com"),
                "source":   "Remotive",
            })
            if len(jobs) >= 15:
                break
    except Exception as e:
        print(f"[Remotive] {e}")
    return jobs


# ─────────────────────── Jobicy (Remote — free API) ──────────────

def scrape_jobicy(query: str) -> list[dict]:
    """Jobicy free API — remote jobs by tag; uses multiple tags to get broad coverage."""
    jobs = []
    seen_ids: set = set()
    try:
        import json as _json
        # Jobicy caps at 3 results per tag; use individual words + full slug for coverage
        q_words = query.lower().split()
        tags_to_try = list(dict.fromkeys([query.lower().replace(" ", "-")] + q_words))
        for tag in tags_to_try[:4]:
            r = requests.get(
                f"https://jobicy.com/api/v2/remote-jobs?count=20&tag={urllib.parse.quote(tag)}",
                headers=_h(json_req=True), timeout=14,
            )
            if r.status_code != 200:
                continue
            for item in _json.loads(r.text).get("jobs", []):
                jid = item.get("id")
                if jid in seen_ids:
                    continue
                seen_ids.add(jid)
                geo = item.get("jobGeo") or "Remote"
                jobs.append({
                    "title":    item.get("jobTitle", ""),
                    "company":  item.get("companyName", "Company"),
                    "location": f"{geo} (Remote)" if geo not in ("Remote", "Anywhere") else "Remote",
                    "link":     item.get("url", "https://jobicy.com"),
                    "source":   "Jobicy",
                })
                if len(jobs) >= 15:
                    return jobs
    except Exception as e:
        print(f"[Jobicy] {e}")
    return jobs


# ─────────────────────── Arbeitnow (EU + Remote — free API) ──────

def scrape_arbeitnow(query: str) -> list[dict]:
    """Arbeitnow free API — EU-focused + remote, English-language roles."""
    jobs = []
    try:
        import json as _json
        # Arbeitnow paginates; fetch first 2 pages
        for page in range(1, 3):
            r = requests.get(
                f"https://www.arbeitnow.com/api/job-board-api?page={page}",
                headers=_h(json_req=True), timeout=14,
            )
            if r.status_code != 200:
                break
            q = query.lower()
            for item in _json.loads(r.text).get("data", []):
                title = item.get("title", "")
                if not any(w in title.lower() for w in q.split()):
                    continue
                remote = item.get("remote", False)
                loc = "Remote" if remote else item.get("location", "Europe")
                jobs.append({
                    "title":    title,
                    "company":  item.get("company_name", "Company"),
                    "location": loc,
                    "link":     item.get("url", "https://www.arbeitnow.com"),
                    "source":   "Arbeitnow",
                })
                if len(jobs) >= 15:
                    break
            if len(jobs) >= 15:
                break
    except Exception as e:
        print(f"[Arbeitnow] {e}")
    return jobs


# ─────────────────────── Himalayas (Remote — free API) ───────────

def scrape_himalayas(query: str) -> list[dict]:
    """Himalayas free API — quality remote jobs from vetted companies."""
    jobs = []
    try:
        import json as _json
        q_words = query.lower().split()
        # Fetch pages and filter client-side (API q param doesn't filter by title)
        for page_offset in range(0, 200, 50):
            r = requests.get(
                f"https://himalayas.app/jobs/api?limit=50&offset={page_offset}",
                headers=_h(json_req=True), timeout=14,
            )
            if r.status_code != 200:
                break
            for item in _json.loads(r.text).get("jobs", []):
                title_l = item.get("title", "").lower()
                if not any(w in title_l for w in q_words):
                    continue
                company_slug = item.get("companySlug", "")
                job_slug = item.get("slug", "")
                link = (f"https://himalayas.app/companies/{company_slug}/jobs/{job_slug}"
                        if company_slug and job_slug else "https://himalayas.app/jobs")
                jobs.append({
                    "title":    item.get("title", ""),
                    "company":  item.get("companyName", "Company"),
                    "location": item.get("location") or "Remote",
                    "link":     link,
                    "source":   "Himalayas",
                })
                if len(jobs) >= 15:
                    return jobs
    except Exception as e:
        print(f"[Himalayas] {e}")
    return jobs


# ─────────────────────── The Muse (US/Global — free API) ─────────

def scrape_themuse(query: str) -> list[dict]:
    """The Muse public API — curated jobs, 410k+ listings, US/global."""
    jobs = []
    try:
        import json as _json
        q_words = query.lower().split()
        # Fetch multiple pages and filter client-side
        for page in range(1, 4):
            r = requests.get(
                f"https://www.themuse.com/api/public/jobs?page={page}&descending=true",
                headers=_h(json_req=True), timeout=14,
            )
            if r.status_code != 200:
                break
            for item in _json.loads(r.text).get("results", []):
                title_l = item.get("name", "").lower()
                if not any(w in title_l for w in q_words):
                    continue
                locations = item.get("locations", [{}])
                loc = locations[0].get("name", "USA") if locations else "USA"
                link = item.get("refs", {}).get("landing_page", "https://www.themuse.com/jobs")
                jobs.append({
                    "title":    item.get("name", ""),
                    "company":  item.get("company", {}).get("name", "Company"),
                    "location": loc,
                    "link":     link,
                    "source":   "The Muse",
                })
                if len(jobs) >= 15:
                    return jobs
    except Exception as e:
        print(f"[TheMuse] {e}")
    return jobs


# ─────────────────────── Adzuna (Global — free API w/ key) ───────

def scrape_adzuna(query: str, country: str = "in") -> list[dict]:
    """
    Adzuna free API — real jobs from 16 countries. Requires ADZUNA_APP_ID
    and ADZUNA_APP_KEY env vars. Returns empty list if keys not set.
    Sign up free: https://developer.adzuna.com/
    """
    import os as _os, json as _json
    app_id  = _os.environ.get("ADZUNA_APP_ID", "")
    app_key = _os.environ.get("ADZUNA_APP_KEY", "")
    if not app_id or not app_key:
        return []
    # Map our country codes to Adzuna country codes
    country_map = {"in": "in", "us": "us", "uk": "gb", "au": "au", "ca": "ca",
                   "de": "de", "fr": "fr", "nl": "nl", "sg": "sg", "remote": "us"}
    az_country = country_map.get(country, "in")
    jobs = []
    try:
        r = requests.get(
            f"https://api.adzuna.com/v1/api/jobs/{az_country}/search/1"
            f"?app_id={app_id}&app_key={app_key}"
            f"&results_per_page=20&what={urllib.parse.quote_plus(query)}"
            f"&content-type=application/json",
            headers=_h(json_req=True), timeout=14,
        )
        if r.status_code != 200:
            return []
        for item in _json.loads(r.text).get("results", []):
            loc = item.get("location", {}).get("display_name", "")
            jobs.append({
                "title":    item.get("title", ""),
                "company":  item.get("company", {}).get("display_name", "Company"),
                "location": loc,
                "link":     item.get("redirect_url", "https://www.adzuna.com"),
                "source":   "Adzuna",
            })
            if len(jobs) >= 15:
                break
    except Exception as e:
        print(f"[Adzuna] {e}")
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

        # ── Global remote job boards + new free APIs ──
        def scrape_boards_for_term(term):
            results = []
            for fn in [scrape_remoteok, scrape_wwr, scrape_remotive, scrape_jobicy,
                       scrape_himalayas, scrape_themuse, scrape_adzuna]:
                try:
                    r = fn(term) if fn != scrape_adzuna else fn(term, country="us")
                    results.extend(r)
                    time.sleep(0.3)
                except Exception:
                    pass
            return results

        board_jobs: list[dict] = []
        with ThreadPoolExecutor(max_workers=6) as ex:
            futs = [ex.submit(scrape_boards_for_term, t) for t in BOARD_SEARCH_TERMS]
            for fut in as_completed(futs):
                board_jobs.extend(fut.result())
        n = db.save_jobs(board_jobs)
        total_saved += n
        print(f"[Scraper] Global boards: {len(board_jobs)} fetched, {n} new saved")

        # ── Himalayas bulk fetch (Remote — all vetted jobs) ──
        try:
            import json as _json
            hima_jobs: list[dict] = []
            for offset in range(0, 500, 50):
                r = requests.get(f"https://himalayas.app/jobs/api?limit=50&offset={offset}",
                                 headers=_h(json_req=True), timeout=14)
                if r.status_code != 200:
                    break
                page_jobs = _json.loads(r.text).get("jobs", [])
                if not page_jobs:
                    break
                for item in page_jobs:
                    cs = item.get("companySlug", "")
                    js = item.get("slug", "")
                    link = (f"https://himalayas.app/companies/{cs}/jobs/{js}"
                            if cs and js else "https://himalayas.app/jobs")
                    hima_jobs.append({
                        "title":    item.get("title", ""),
                        "company":  item.get("companyName", "Company"),
                        "location": item.get("location") or "Remote",
                        "link":     link,
                        "source":   "Himalayas",
                    })
            n = db.save_jobs(hima_jobs)
            total_saved += n
            print(f"[Scraper] Himalayas: {len(hima_jobs)} fetched, {n} new saved")
        except Exception as e:
            print(f"[Scraper] Himalayas bulk failed: {e}")

        # ── Arbeitnow bulk fetch (EU/Remote) — paginate once over all pages ──
        try:
            import json as _json
            arb_jobs: list[dict] = []
            for page in range(1, 4):
                r = requests.get(f"https://www.arbeitnow.com/api/job-board-api?page={page}",
                                 headers=_h(json_req=True), timeout=14)
                if r.status_code != 200:
                    break
                for item in _json.loads(r.text).get("data", []):
                    remote = item.get("remote", False)
                    loc = "Remote" if remote else item.get("location", "Europe")
                    arb_jobs.append({
                        "title":    item.get("title", ""),
                        "company":  item.get("company_name", "Company"),
                        "location": loc,
                        "link":     item.get("url", "https://www.arbeitnow.com"),
                        "source":   "Arbeitnow",
                    })
            n = db.save_jobs(arb_jobs)
            total_saved += n
            print(f"[Scraper] Arbeitnow: {len(arb_jobs)} fetched, {n} new saved")
        except Exception as e:
            print(f"[Scraper] Arbeitnow bulk failed: {e}")

        # ── International boards (UK / US / EU / AU) — skip on Render free tier ──
        import os as _os
        if not _os.environ.get("RENDER"):
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


def scrape_live(query: str, country: str = "in") -> list[dict]:
    """Quick live scrape — routes to the right sources based on country."""
    all_jobs: list[dict] = []
    # Free API sources always included (no scraping, real verified jobs)
    free_api_sources = [scrape_remotive, scrape_jobicy, scrape_remoteok, scrape_wwr]
    if country == "uk":
        sources = [scrape_reed_uk, scrape_linkedin_uk, scrape_glassdoor, *free_api_sources]
    elif country == "us":
        sources = [scrape_linkedin_us, scrape_indeed, scrape_wellfound, scrape_themuse,
                   *free_api_sources, lambda q: scrape_adzuna(q, "us")]
    elif country == "au":
        sources = [scrape_seek_au, *free_api_sources, lambda q: scrape_adzuna(q, "au")]
    elif country == "eu":
        sources = [scrape_linkedin_eu, scrape_arbeitnow, *free_api_sources,
                   lambda q: scrape_adzuna(q, "de")]
    elif country == "remote":
        sources = [*free_api_sources, scrape_wellfound, scrape_arbeitnow, scrape_themuse]
    else:  # "in" or default
        sources = [scrape_internshala, scrape_shine, scrape_linkedin_india,
                   *free_api_sources, lambda q: scrape_adzuna(q, "in")]

    with ThreadPoolExecutor(max_workers=10) as ex:
        futs = {ex.submit(fn, query): getattr(fn, "__name__", "lambda") for fn in sources}
        for fut in as_completed(futs):
            try:
                all_jobs.extend(fut.result(timeout=18))
            except Exception:
                pass
    return all_jobs
