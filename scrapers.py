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
        "Accept-Encoding": "gzip, deflate, br",
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
        encoded = urllib.parse.quote_plus(query.replace(" ", "-"))
        url = f"https://remoteok.com/remote-{encoded}-jobs"
        r = requests.get(url, headers=_h(), timeout=14)
        if r.status_code != 200:
            return []
        soup = BeautifulSoup(r.text, "lxml")
        rows = soup.select("tr.job")
        for row in rows[:12]:
            title_el   = row.select_one("h2[itemprop='title']") or row.select_one(".position h2")
            company_el = row.select_one("h3[itemprop='name']") or row.select_one(".company")
            loc_el     = row.select_one("div.location") or row.select_one("[class*='location']")
            link_el    = row.get("data-url") or ""
            if not title_el:
                continue
            link = f"https://remoteok.com{link_el}" if link_el.startswith("/") else link_el or "https://remoteok.com"
            jobs.append({
                "title":    title_el.get_text(strip=True),
                "company":  company_el.get_text(strip=True) if company_el else "Company",
                "location": loc_el.get_text(strip=True) if loc_el else "Remote",
                "link":     link,
                "source":   "RemoteOK",
            })
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
        items = soup.select("li.feature") or soup.select("article.job")
        for item in items[:12]:
            title_el   = item.select_one("span.title") or item.select_one("h4")
            company_el = item.select_one("span.company") or item.select_one("h3")
            link_el    = item.select_one("a[href*='/remote-jobs/']")
            if not title_el:
                continue
            href = link_el.get("href", "") if link_el else ""
            link = f"https://weworkremotely.com{href}" if href.startswith("/") else href or "https://weworkremotely.com"
            jobs.append({
                "title":    title_el.get_text(strip=True),
                "company":  company_el.get_text(strip=True) if company_el else "Company",
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

def is_india_location(loc: str) -> bool:
    loc_l = loc.lower()
    return any(city in loc_l for city in INDIA_CITIES)


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

        db.finish_scrape_log(log_id, total_saved, "done")
        print(f"[Scraper] Done. Total jobs in DB: {db.total_jobs()}")
    except Exception as e:
        print(f"[Scraper] Error: {e}")
        db.finish_scrape_log(log_id, total_saved, "error")
    finally:
        _SCRAPE_RUNNING = False


def scrape_live(query: str) -> list[dict]:
    """Quick live scrape — India sources first, then global/remote."""
    all_jobs: list[dict] = []
    india_sources  = [scrape_naukri, scrape_internshala, scrape_timesjobs, scrape_shine, scrape_foundit, scrape_linkedin_india]
    global_sources = [scrape_linkedin, scrape_indeed, scrape_wellfound, scrape_remoteok, scrape_wwr, scrape_glassdoor]
    with ThreadPoolExecutor(max_workers=12) as ex:
        futs = {ex.submit(fn, query): fn.__name__ for fn in india_sources + global_sources}
        for fut in as_completed(futs):
            try:
                all_jobs.extend(fut.result(timeout=18))
            except Exception:
                pass
    # India-first ordering
    india  = [j for j in all_jobs if is_india_location(j.get("location", ""))]
    others = [j for j in all_jobs if not is_india_location(j.get("location", ""))]
    return india + others
