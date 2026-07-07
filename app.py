import concurrent.futures
import io
import os
import re
import threading
import time
import uuid
from datetime import datetime, timezone

import requests as _requests
from apscheduler.schedulers.background import BackgroundScheduler
from cachetools import TTLCache
from flask import Flask, jsonify, render_template, request, session
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.middleware.proxy_fix import ProxyFix

import db
# PDF validation lives in a shared module (used before enqueue; no Flask dep).
from pdfvalidate import PdfRejected, validate_pdf, MAX_PDF_BYTES, MAX_PDF_PAGES
from taskqueue import get_queue

# Lazy-load scrapers — BeautifulSoup/lxml/requests are heavy; don't load at boot
def _scrapers():
    import scrapers as _s
    return _s

# ── Upload / queue config ─────────────────────────────────────────
UPLOAD_DIR      = os.environ.get("UPLOAD_DIR", os.path.join(
    os.environ.get("DATA_DIR", os.path.dirname(os.path.abspath(__file__))), "uploads"))
os.makedirs(UPLOAD_DIR, exist_ok=True)
MAX_QUEUE_DEPTH = int(os.environ.get("MAX_QUEUE_DEPTH", "40"))   # soft cap for "high demand"
WORKER_STALE_S  = int(os.environ.get("WORKER_STALE_SECONDS", "120"))

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_PDF_BYTES
app.secret_key = os.environ.get("SECRET_KEY", "cvfinder-dev-key-change-in-prod")

# Behind a proxy (Render / nginx): trust exactly ONE forwarded hop so
# get_remote_address (rate-limiting + per-IP guards) sees the real client IP.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

# ── CORS: allow only the configured static frontend origin(s) ─────
# FRONTEND_ORIGIN is a comma-separated list, e.g.
#   "https://cvfinder.vercel.app,https://www.cvfinder.com"
try:
    from flask_cors import CORS
    _origins = [o.strip() for o in os.environ.get("FRONTEND_ORIGIN", "*").split(",") if o.strip()]
    CORS(app, resources={r"/api/*": {"origins": _origins},
                         r"/health": {"origins": _origins}},
         supports_credentials=False, max_age=86400)
except Exception:
    pass  # flask-cors optional in local/dev

# ── Payment ingest token: kept stable across restarts so the phone-side ──
# ── SMS-forwarding setup only needs to be entered once.                  ──
# ── Rate limiting ─────────────────────────────────────────────────
limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=["200 per minute"],
    storage_uri="memory://",
)

# ── In-process caches ─────────────────────────────────────────────
_job_cache        = TTLCache(maxsize=64, ttl=120)    # cache job searches 2 min
_job_cache_lock   = threading.Lock()

# Per-IP guard: one analysis IN FLIGHT per client IP. Maps ip -> job_id while
# that job is queued/running (TTL auto-clears if a job is abandoned).
_ip_active_job    = TTLCache(maxsize=10000, ttl=600)
_ip_lock          = threading.Lock()

# ─────────────────────── ATS keyword data ────────────────────────

ACTION_VERBS = [
    "achieved", "automated", "built", "collaborated", "coordinated", "created",
    "delivered", "designed", "developed", "directed", "established", "executed",
    "generated", "implemented", "improved", "increased", "launched", "led",
    "managed", "mentored", "optimized", "organized", "produced", "reduced",
    "spearheaded", "streamlined", "supervised", "trained", "transformed", "analyzed",
]

SECTION_PATTERNS = {
    "experience": ["experience", "work history", "employment", "professional experience",
                   "work experience", "career history", "professional background"],
    "education":  ["education", "academic", "degree", "university", "college",
                   "school", "qualification", "academics"],
    "skills":     ["skills", "technical skills", "core competencies", "technologies",
                   "tools", "expertise", "proficiencies", "key skills"],
    "summary":    ["summary", "objective", "profile", "about me", "overview",
                   "professional summary", "career objective"],
    "projects":   ["projects", "personal projects", "academic projects",
                   "key projects", "notable projects"],
}

TECH_SKILLS = [
    "python", "java", "javascript", "typescript", "c++", "c#", "ruby", "golang",
    "rust", "swift", "kotlin", "php", "scala", "matlab", "sql", "bash", "shell",
    "html", "css", "sass", "r", "perl",
    "react", "angular", "vue", "svelte", "nextjs", "webpack", "redux", "jquery",
    "bootstrap", "tailwind", "gatsby",
    "nodejs", "django", "flask", "spring", "express", "fastapi", "laravel",
    "rails", "graphql", "rest api", "grpc",
    "tensorflow", "pytorch", "keras", "pandas", "numpy", "scikit-learn",
    "hadoop", "spark", "kafka", "tableau", "power bi", "airflow", "dbt",
    "machine learning", "deep learning", "nlp", "computer vision", "data science",
    "data analysis", "data engineering", "big data", "etl", "mlops",
    "aws", "azure", "gcp", "docker", "kubernetes", "terraform", "ansible",
    "jenkins", "ci/cd", "github actions", "linux", "unix",
    "mysql", "postgresql", "mongodb", "oracle", "cassandra", "elasticsearch",
    "redis", "sqlite", "dynamodb", "bigquery", "snowflake",
    "android", "ios", "flutter", "react native",
    "cybersecurity", "penetration testing", "siem",
    "git", "agile", "scrum", "devops", "microservices", "api", "oop", "tdd",
    "cloud computing", "blockchain", "iot",
    "excel", "salesforce", "sap", "erp", "crm",
    "figma", "sketch", "adobe xd", "photoshop", "illustrator", "ui/ux",
    "ux research", "prototyping", "wireframing",
    "leadership", "project management", "product management",
    "stakeholder management", "communication",
]

JOB_TITLE_TERMS = [
    "software engineer", "software developer", "data scientist", "data analyst",
    "data engineer", "product manager", "project manager", "business analyst",
    "ui designer", "ux designer", "ui/ux designer", "graphic designer",
    "devops engineer", "cloud engineer", "ml engineer", "machine learning engineer",
    "ai engineer", "frontend developer", "front end developer", "backend developer",
    "back end developer", "full stack developer", "fullstack developer",
    "web developer", "mobile developer", "android developer", "ios developer",
    "system administrator", "database administrator", "security engineer",
    "cybersecurity analyst", "qa engineer", "test engineer", "solutions architect",
    "cloud architect", "technical lead", "tech lead", "team lead", "scrum master",
    "product owner", "marketing manager", "content writer", "seo specialist",
    "social media manager", "financial analyst", "accountant", "sales executive",
    "business development", "operations manager", "research analyst",
]

# PDF validation (validate_pdf, PdfRejected) is imported from pdfvalidate.
# Text extraction runs in the worker's memory-capped subprocess (pdftext.py).

# ─────────────────────── ATS analysis ────────────────────────────

def analyze_ats(text):
    """
    Research-backed ATS scoring (Belsack + industry best practices).
    5 sections: Contact(15) + Structure(25) + Content(30) + Formatting(20) + Keywords(10)
    """
    t = text.lower()
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    issues = []
    suggestions = []
    missing_keywords = []
    sections: dict[str, dict] = {}

    # ── Section 1: Contact (15 pts) ──────────────────────────────
    c_score = 0
    has_email    = bool(re.search(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", text))
    has_phone    = bool(re.search(r"(\+?\d[\d\s\-(). ]{7,}\d)", text))
    has_linkedin = "linkedin" in t
    has_github   = "github" in t or "portfolio" in t

    if has_email:    c_score += 6
    else:            issues.append("No email address detected"); suggestions.append("Add a professional email address at the top of your CV")
    if has_phone:    c_score += 5
    else:            issues.append("No phone number found"); suggestions.append("Include a phone number with country code (+91 for India)")
    if has_linkedin: c_score += 2
    # Research: bare-bones LinkedIn hurts — warn if present but flag if missing
    else:            suggestions.append("Add your LinkedIn profile URL — recruiters always verify it (ensure it's complete with headshot and full work history)")
    if has_github:   c_score += 2
    else:            suggestions.append("Add a GitHub or portfolio link — shows real work beyond the CV")
    sections["contact"] = {"label": "Contact Info", "score": c_score, "max": 15}

    # ── Section 2: Structure (25 pts) ────────────────────────────
    # Research: Skills section should be at top; Experience before Education for candidates with work history
    s_score = 0
    weights = {"experience": 9, "education": 6, "skills": 6, "summary": 2, "projects": 2}
    has_experience = any(kw in t for kw in SECTION_PATTERNS["experience"])
    for section, pts in weights.items():
        if any(kw in t for kw in SECTION_PATTERNS[section]):
            s_score += pts
        else:
            issues.append(f"'{section.capitalize()}' section not clearly labeled")
            suggestions.append(f"Add a clear '{section.capitalize()}' heading — ATS parsers need exact labels")

    # Research: projects section critical for students/freshers
    if not any(kw in t for kw in SECTION_PATTERNS["projects"]):
        if not has_experience:
            issues.append("No Projects section — critical for candidates with limited experience")
            suggestions.append("Add 3–4 significant projects with technologies used and outcomes")

    sections["structure"] = {"label": "Structure", "score": s_score, "max": 25}

    # ── Section 3: Content quality (30 pts) ──────────────────────
    # Research: XYZ formula, 5+ metrics doubles interview rate, action verbs mandatory
    q_score = 0

    # Action verbs check
    found_verbs   = [v for v in ACTION_VERBS if re.search(r"\b" + v + r"\b", t)]
    q_score      += min(10, len(found_verbs) * 1)
    missing_verbs = [v for v in ACTION_VERBS if v not in t]
    if len(found_verbs) < 4:
        issues.append("Too few action verbs — bullet points lack impact")
        missing_keywords.extend(missing_verbs[:6])
        suggestions.append(f"Start bullet points with strong verbs: {', '.join(missing_verbs[:5])}")
    elif len(found_verbs) < 8:
        suggestions.append(f"Add more impact verbs: try {', '.join(missing_verbs[:3])}")

    # Metric density — research shows 5+ metrics doubles interview rate
    qty_patterns = [
        r"\d+\s*%",
        r"[₹$€£]\s*[\d,]+",
        r"\b\d+\s*(million|thousand|crore|lakh|k\b|m\b)",
        r"\b\d{2,}\s*(users|customers|clients|projects|employees|members|students|leads|orders|tickets|calls|deals)",
        r"(increased|decreased|reduced|improved|grew|saved|generated|drove|boosted|cut|doubled|tripled)\s+[\w\s]*\d+",
        r"\b(top|rank(ed)?|first|second)\s+\d+\s*%",
        r"\b\d+\s*(award|certification|course|module|feature|product)",
    ]
    metric_count = sum(1 for p in qty_patterns if re.search(p, t))

    if metric_count == 0:
        issues.append("No measurable metrics found — CVs with 5+ metrics double interview rates")
        suggestions.append("Quantify everything: '30% increase', 'managed 8-person team', '500+ customers served'")
        q_score += 0
    elif metric_count < 3:
        issues.append(f"Only {metric_count} metric(s) found — aim for at least 5 measurable achievements")
        suggestions.append("Use the XYZ formula: 'Accomplished [X] as measured by [Y], by doing [Z]'")
        q_score += min(8, metric_count * 3)
    elif metric_count < 5:
        suggestions.append(f"Good — {metric_count} metrics found. Add {5 - metric_count} more to reach the high-impact threshold")
        q_score += min(14, metric_count * 3)
    else:
        q_score += min(20, metric_count * 2)

    # Buzzword / fluff detection — research: 51% of CVs hurt by buzzwords
    BUZZWORDS = [
        "rockstar", "ninja", "guru", "wizard", "detail-oriented", "detail oriented",
        "team player", "hard worker", "passionate about", "go-getter", "self-starter",
        "think outside the box", "synergy", "leverage", "proactive", "dynamic",
        "results-driven", "motivated individual",
    ]
    found_buzz = [b for b in BUZZWORDS if b in t]
    if found_buzz:
        issues.append(f"Buzzwords detected: '{found_buzz[0]}' — these reduce credibility")
        suggestions.append(f"Replace '{found_buzz[0]}' with a specific achievement. Show don't tell.")
        q_score = max(0, q_score - 2)

    # Irrelevant skills — research: MS Word, PowerPoint waste space
    IRRELEVANT = ["microsoft word", "ms word", "microsoft powerpoint", "ms powerpoint", "microsoft excel basic"]
    found_irrelevant = [s for s in IRRELEVANT if s in t]
    if found_irrelevant:
        suggestions.append("Remove basic tools like 'MS Word' or 'PowerPoint' — they waste space; add domain-specific skills instead")

    # Self-rating check — research: subjective ratings are a red flag
    if re.search(r"\b[1-5]\s*/\s*5\b|\b[1-9]\s*/\s*10\b|\d+\s*stars?", t):
        issues.append("Self-rated skills (e.g. '4/5') detected — these are subjective and hurt credibility")
        suggestions.append("Remove skill ratings — list tools you've used without rating yourself")
        q_score = max(0, q_score - 2)

    sections["content"] = {"label": "Content Quality", "score": min(30, q_score), "max": 30}

    # ── Section 4: Formatting (20 pts) ───────────────────────────
    # Research: 475-600 word sweet spot = 2x interview rate; 7s recruiter scan; bullets essential
    f_score = 0
    words = len(text.split())

    # Word count — research sweet spot is 475-600 words
    if 475 <= words <= 600:
        f_score += 10
    elif 400 <= words < 475 or 600 < words <= 800:
        f_score += 7
        suggestions.append(f"CV is {words} words — the research-backed sweet spot is 475–600 words for 2x interview rate")
    elif 300 <= words < 400 or 800 < words <= 1000:
        f_score += 4
        if words < 400:
            issues.append(f"CV is too short ({words} words) — sweet spot is 475–600 words")
            suggestions.append("Expand bullet points with specific achievements and context")
        else:
            issues.append(f"CV is too long ({words} words) — trim to 475–600 words for best results")
            suggestions.append("Remove irrelevant experience, cut buzzwords, and tighten bullet points to 3 lines max")
    else:
        f_score += 1
        if words < 300:
            issues.append(f"CV is very short ({words} words) — needs significant expansion")
        else:
            issues.append(f"CV is very long ({words} words) — cut aggressively to under 700 words")

    # Bullet points — research: essential for 7-second recruiter scan
    has_bullets = any(re.match(r"^[•\-\*·▪▸►]", l) for l in lines)
    if has_bullets: f_score += 5
    else:           issues.append("No bullet points detected"); suggestions.append("Use bullet points — recruiters scan in 7 seconds and need structured lists")

    # Dates
    has_dates = bool(re.search(r"\b(19|20)\d{2}\b|\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s*\d{4}", t))
    if has_dates: f_score += 5
    else:         issues.append("Employment/education dates missing"); suggestions.append("Add month–year dates (e.g. 'Jun 2022 – Present') to every role and degree")

    # Non-ASCII / special characters
    non_ascii = len(re.findall(r"[^\x00-\x7F]", text))
    if non_ascii > 30:
        f_score = max(0, f_score - 3)
        issues.append(f"{non_ascii} non-ASCII characters — may corrupt ATS parsing")
        suggestions.append("Remove special characters, fancy bullets, or symbols; use plain hyphens (-)")

    # Table formatting
    if re.search(r"(\|[^\|]+\|){2,}", text):
        f_score = max(0, f_score - 2)
        issues.append("Table formatting detected — ATS systems cannot read tables")
        suggestions.append("Replace tables with plain bullet lists")

    sections["formatting"] = {"label": "Formatting", "score": min(20, f_score), "max": 20}

    # ── Section 5: Keyword density (10 pts) ──────────────────────
    # Research: average JD has 43 keywords; most candidates only match 51%
    found_skills = [s for s in TECH_SKILLS if re.search(r"\b" + re.escape(s) + r"\b", t)]
    k_score = min(10, len(found_skills))
    if len(found_skills) < 5:
        issues.append("Few technical/domain keywords — likely to be filtered by ATS before a human sees it")
        suggestions.append("Add a dedicated Skills section: list languages, tools, frameworks, certifications")
    elif len(found_skills) < 10:
        suggestions.append(f"{len(found_skills)} technical keywords found — try to reach 15+ for strong ATS pass rate")
    sections["keywords"] = {"label": "Keywords", "score": k_score, "max": 10}

    total = sum(s["score"] for s in sections.values())
    return {
        "score": max(0, min(100, total)),
        "word_count": words,
        "metric_count": metric_count,
        "missing_keywords": list(set(missing_keywords))[:10],
        "issues": issues,
        "suggestions": suggestions,
        "sections": sections,
    }


# ─────────────────────── JD Matcher ──────────────────────────────

def match_jd(cv_text: str, jd_text: str, cv_keywords: list[str]) -> dict:
    """
    Compares CV against a job description.
    Returns match_score, matched, missing, tailored_suggestions, jd_keywords.
    """
    jd_t   = jd_text.lower()
    cv_t   = cv_text.lower()

    # Extract all meaningful terms from JD
    jd_keywords: set[str] = set()
    for skill in TECH_SKILLS:
        if re.search(r"\b" + re.escape(skill) + r"\b", jd_t):
            jd_keywords.add(skill)

    # Also pull domain signals from JD
    for domain, signals in DOMAIN_SIGNALS.items():
        for kw in signals:
            if re.search(r"\b" + re.escape(kw) + r"\b", jd_t) and len(kw) > 3:
                jd_keywords.add(kw)

    # Extract requirements phrases ("X+ years", "must have", "required", "proficient in")
    req_phrases = re.findall(
        r"(?:proficient|experience|expertise|knowledge|skilled)\s+(?:in|with)\s+([a-zA-Z0-9\s\+#\.]+?)(?:[,\.\n]|$)",
        jd_t
    )
    for phrase in req_phrases:
        tokens = [t.strip() for t in re.split(r"\s+and\s+|\s*/\s*|,", phrase) if 2 < len(t.strip()) < 35]
        jd_keywords.update(tokens[:5])

    if not jd_keywords:
        return {"error": "Could not extract keywords from job description — try pasting more text"}

    cv_kw_set = set(cv_keywords)
    matched   = sorted(jd_keywords & cv_kw_set)
    missing   = sorted(jd_keywords - cv_kw_set)

    match_pct = round(len(matched) / len(jd_keywords) * 100) if jd_keywords else 0

    # Tailored suggestions
    tips = []
    if missing:
        critical = missing[:5]
        tips.append(f"Add these missing keywords to your CV: {', '.join(critical)}")
    if match_pct < 50:
        tips.append("Your CV matches less than half the JD keywords — consider tailoring the skills section directly")
    if match_pct >= 70:
        tips.append("Strong match — make sure these keywords also appear in your job titles and bullet points, not just the skills list")

    # Check for years-of-experience requirement
    yrs_req = re.findall(r"(\d+)\+?\s*(?:to\s*\d+)?\s*years?\s*(?:of\s*)?(?:experience|exp)", jd_t)
    if yrs_req:
        tips.append(f"JD requires {yrs_req[0]}+ years experience — make sure your experience duration is clearly stated")

    return {
        "match_pct": match_pct,
        "matched":   matched[:20],
        "missing":   missing[:20],
        "jd_total":  len(jd_keywords),
        "tips":      tips,
    }


# ─────────────────────── Keyword extraction ──────────────────────

def extract_cv_keywords(text):
    found = set()
    t = text.lower()
    for skill in TECH_SKILLS:
        if re.search(r"\b" + re.escape(skill) + r"\b", t):
            found.add(skill)
    for title in JOB_TITLE_TERMS:
        if title in t:
            found.add(title)
    block = re.search(
        r"\b(?:skills|technologies|tools|expertise|proficiencies|competencies)[:\s\n]+(.{10,600}?)(?:\n{2,}|$)",
        t, re.IGNORECASE | re.DOTALL,
    )
    if block:
        for item in re.split(r"[,•|\n\t·▪]+", block.group(1))[:40]:
            item = item.strip().lower()
            if 2 <= len(item) <= 30 and re.match(r"^[a-z0-9][a-z0-9\s+#./\-_]*$", item):
                if item not in {"and", "or", "the", "in", "of", "for", "to", "with", "a", "an", "etc"}:
                    found.add(item)
    return sorted(found)[:50]


# ─────────────────────── CV Intelligence Engine ──────────────────

# 14 domains, each with weighted keywords (higher weight = stronger signal)
DOMAIN_SIGNALS: dict[str, dict[str, float]] = {
    "software_engineering": {
        "python": 2, "java": 2, "javascript": 2, "typescript": 2, "c++": 2, "golang": 2,
        "rust": 2, "scala": 2, "c#": 2, "php": 1.5, "ruby": 1.5, "react": 2, "angular": 2,
        "vue": 2, "nodejs": 2, "django": 2, "flask": 2, "spring": 2, "fastapi": 2,
        "graphql": 1.5, "rest api": 1.5, "microservices": 2, "oop": 1.5, "tdd": 1.5,
        "git": 1, "agile": 1, "scrum": 1, "software engineer": 3, "software developer": 3,
        "backend": 2, "frontend": 2, "full stack": 2, "web developer": 2,
    },
    "data_science_ml": {
        "machine learning": 3, "deep learning": 3, "nlp": 3, "computer vision": 3,
        "pytorch": 3, "tensorflow": 3, "keras": 2, "scikit-learn": 2, "pandas": 2,
        "numpy": 2, "statistics": 2, "neural network": 3, "transformer": 3, "llm": 3,
        "artificial intelligence": 2, "mlops": 2, "model training": 2, "feature engineering": 2,
        "data scientist": 3, "ml engineer": 3, "ai engineer": 2, "research": 1,
        "hypothesis": 1, "a/b testing": 1.5, "regression": 1.5, "classification": 1.5,
    },
    "data_engineering": {
        "spark": 3, "kafka": 3, "airflow": 3, "dbt": 2, "hadoop": 2, "hive": 2,
        "snowflake": 2, "bigquery": 2, "redshift": 2, "databricks": 2, "pyspark": 3,
        "etl": 3, "pipeline": 2, "data warehouse": 2, "data lake": 2, "data engineer": 3,
        "sql": 1.5, "postgresql": 1.5, "cassandra": 1.5, "elasticsearch": 1.5,
    },
    "data_analytics": {
        "sql": 2, "excel": 2, "tableau": 3, "power bi": 3, "google analytics": 2,
        "data analysis": 3, "business intelligence": 2, "reporting": 2, "dashboards": 2,
        "looker": 2, "metabase": 2, "data visualization": 2, "analytics": 2,
        "data analyst": 3, "business analyst": 2, "insights": 1, "kpi": 1.5,
    },
    "devops_cloud": {
        "aws": 2, "azure": 2, "gcp": 2, "terraform": 3, "ansible": 2, "jenkins": 2,
        "ci/cd": 3, "github actions": 2, "kubernetes": 3, "docker": 2, "cloud": 2,
        "devops": 3, "sre": 3, "infrastructure": 2, "monitoring": 1.5, "linux": 1.5,
        "bash": 1.5, "devops engineer": 3, "cloud engineer": 3, "cloudformation": 2,
    },
    "product_management": {
        "product": 2, "roadmap": 3, "user stories": 2, "agile": 1.5, "scrum": 1.5,
        "product manager": 3, "product management": 3, "prd": 2, "feature": 1,
        "stakeholder": 2, "prioritization": 2, "okr": 2, "go-to-market": 2,
        "product strategy": 2, "product owner": 2, "wireframe": 1.5, "user research": 1.5,
    },
    "design_ux": {
        "figma": 3, "sketch": 3, "adobe xd": 3, "prototyping": 2, "wireframing": 2,
        "ui": 2, "ux": 2, "user research": 2, "user experience": 3, "interaction design": 3,
        "visual design": 2, "design system": 2, "typography": 1.5, "adobe": 1.5,
        "illustrator": 2, "photoshop": 1.5, "ui/ux": 3, "graphic designer": 2,
    },
    "sales_bd": {
        "sales": 3, "business development": 3, "lead generation": 3, "cold calling": 3,
        "crm": 2, "b2b": 2, "b2c": 2, "revenue target": 2, "quota": 3,
        "account management": 2, "client acquisition": 3, "prospecting": 3,
        "negotiation": 2, "salesforce": 2, "hubspot": 2, "bdm": 3,
        "sales executive": 3, "business development executive": 3, "inside sales": 3,
        "sales funnel": 3, "deal closure": 3,
    },
    "marketing": {
        "marketing": 3, "seo": 3, "sem": 3, "social media marketing": 2,
        "email marketing": 2, "campaigns": 2, "google ads": 2, "facebook ads": 2,
        "digital marketing": 3, "performance marketing": 3, "growth hacking": 2,
        "copywriting": 2, "influencer marketing": 2, "content marketing": 2,
        "marketing manager": 3, "content writer": 2, "seo specialist": 3,
        "brand marketing": 2, "marketing analytics": 2,
    },
    "finance": {
        "finance": 2, "accounting": 2, "financial modeling": 3, "excel": 1.5,
        "p&l": 2, "balance sheet": 2, "forecasting": 2, "budgeting": 2,
        "valuation": 2, "investment": 1.5, "private equity": 2, "venture capital": 2,
        "cfa": 3, "ca": 2, "chartered accountant": 3, "audit": 2, "tax": 1.5,
        "financial analysis": 3, "accountant": 3, "financial analyst": 3,
        "tally": 2, "gst": 1.5, "mba finance": 2,
    },
    "operations": {
        "operations": 2, "supply chain": 3, "logistics": 3, "process improvement": 2,
        "lean": 2, "six sigma": 2, "project management": 2, "vendor management": 2,
        "procurement": 2, "inventory": 1.5, "warehouse": 1.5, "scm": 2,
        "operations manager": 3, "project manager": 2, "pmp": 2,
    },
    "hr": {
        "hr": 2, "human resources": 3, "recruitment": 3, "talent acquisition": 3,
        "onboarding": 2, "employee engagement": 2, "hris": 2, "performance management": 2,
        "learning & development": 2, "compensation": 1.5, "payroll": 1.5,
        "people operations": 2, "hr manager": 3, "recruiter": 3,
    },
    "consulting": {
        "consulting": 3, "management consulting": 3, "strategy consulting": 3,
        "case study": 2, "client management": 2, "advisory": 3,
        "mckinsey": 3, "bcg": 3, "bain": 3, "deloitte": 3, "pwc": 2, "kpmg": 2,
        "business consulting": 3, "strategy consultant": 3, "consultant": 2,
        "engagement manager": 2, "due diligence": 2,
    },
    "mobile": {
        "android": 3, "ios": 3, "flutter": 3, "react native": 3, "kotlin": 2,
        "swift": 2, "mobile": 2, "app development": 2, "mobile developer": 3,
        "android developer": 3, "ios developer": 3,
    },
}

DOMAIN_DISPLAY = {
    "software_engineering": ("Software Engineering", "💻"),
    "data_science_ml":      ("Data Science / AI-ML", "🤖"),
    "data_engineering":     ("Data Engineering",      "🔧"),
    "data_analytics":       ("Data Analytics",        "📊"),
    "devops_cloud":         ("DevOps / Cloud",        "☁️"),
    "product_management":   ("Product Management",    "📱"),
    "design_ux":            ("Design / UX",           "🎨"),
    "sales_bd":             ("Sales / Business Dev",  "💼"),
    "marketing":            ("Marketing / Growth",    "📣"),
    "finance":              ("Finance / Accounting",  "💰"),
    "operations":           ("Operations / SCM",      "⚙️"),
    "hr":                   ("HR / People Ops",       "👥"),
    "consulting":           ("Consulting / Strategy", "🎯"),
    "mobile":               ("Mobile Development",    "📲"),
}

# job titles per domain × experience level
# Fresher = 0-1yr (internship only), Junior = 1-3yr, Mid = 3-6yr, Senior = 6-10yr, Lead = 10+yr
DOMAIN_ROLES: dict[str, dict[str, list[str]]] = {
    "software_engineering": {
        "fresher": ["associate software engineer", "junior software developer", "software engineer trainee", "sde intern"],
        "junior":  ["software engineer", "software developer", "associate engineer"],
        "mid":     ["senior software engineer", "software engineer ii", "tech lead"],
        "senior":  ["staff engineer", "principal engineer", "technical lead"],
        "lead":    ["engineering manager", "vp engineering", "director of engineering"],
    },
    "data_science_ml": {
        "fresher": ["data science associate", "junior data scientist", "ml intern", "ai associate"],
        "junior":  ["data scientist", "junior ml engineer", "research analyst"],
        "mid":     ["senior data scientist", "ml engineer", "applied scientist"],
        "senior":  ["principal data scientist", "senior ml engineer", "head of data science"],
        "lead":    ["vp data science", "chief data scientist", "director of ai"],
    },
    "data_engineering": {
        "fresher": ["data engineer trainee", "junior data engineer", "analytics engineer associate"],
        "junior":  ["data engineer", "junior data engineer", "analytics engineer"],
        "mid":     ["senior data engineer", "data platform engineer"],
        "senior":  ["staff data engineer", "principal data engineer", "data architect"],
        "lead":    ["head of data engineering", "director of data"],
    },
    "data_analytics": {
        "fresher": ["data analyst trainee", "junior data analyst", "business analyst fresher", "reporting associate"],
        "junior":  ["data analyst", "business analyst", "operations analyst"],
        "mid":     ["senior data analyst", "senior business analyst", "analytics manager"],
        "senior":  ["principal analyst", "director of analytics"],
        "lead":    ["head of analytics", "vp analytics"],
    },
    "devops_cloud": {
        "fresher": ["junior devops engineer", "cloud support associate", "devops trainee"],
        "junior":  ["devops engineer", "cloud engineer", "site reliability engineer"],
        "mid":     ["senior devops engineer", "senior sre", "platform engineer"],
        "senior":  ["staff devops engineer", "devops architect"],
        "lead":    ["head of infrastructure", "vp devops"],
    },
    "product_management": {
        "fresher": ["associate product manager", "product analyst", "junior product manager"],
        "junior":  ["product manager", "product analyst"],
        "mid":     ["senior product manager", "group product manager"],
        "senior":  ["principal product manager", "director of product"],
        "lead":    ["vp product", "chief product officer", "head of product"],
    },
    "design_ux": {
        "fresher": ["junior ui designer", "ux associate", "design intern", "visual designer trainee"],
        "junior":  ["ui/ux designer", "product designer", "graphic designer"],
        "mid":     ["senior ux designer", "senior product designer", "ui lead"],
        "senior":  ["principal designer", "design lead"],
        "lead":    ["head of design", "vp design", "design director"],
    },
    "sales_bd": {
        "fresher": ["business development associate", "sales executive trainee", "inside sales associate", "sales development representative", "bdm fresher"],
        "junior":  ["business development executive", "sales executive", "account executive"],
        "mid":     ["senior sales executive", "business development manager", "account manager"],
        "senior":  ["sales manager", "regional sales manager", "key account manager"],
        "lead":    ["head of sales", "vp sales", "director of business development"],
    },
    "marketing": {
        "fresher": ["marketing executive trainee", "digital marketing associate", "content associate", "social media executive fresher"],
        "junior":  ["digital marketing executive", "content writer", "seo executive", "social media manager"],
        "mid":     ["senior marketing manager", "growth manager", "performance marketing manager"],
        "senior":  ["marketing director", "head of growth", "vp marketing"],
        "lead":    ["chief marketing officer", "vp marketing", "head of brand"],
    },
    "finance": {
        "fresher": ["accounts executive trainee", "finance associate fresher", "junior financial analyst", "finance intern"],
        "junior":  ["financial analyst", "accounts executive", "finance executive"],
        "mid":     ["senior financial analyst", "finance manager", "fp&a analyst"],
        "senior":  ["finance director", "senior finance manager", "head of finance"],
        "lead":    ["cfo", "vp finance", "head of treasury"],
    },
    "operations": {
        "fresher": ["operations associate trainee", "supply chain associate fresher", "junior operations analyst"],
        "junior":  ["operations analyst", "supply chain executive", "logistics executive"],
        "mid":     ["operations manager", "project manager", "supply chain manager"],
        "senior":  ["senior operations manager", "head of operations"],
        "lead":    ["vp operations", "director of operations", "coo"],
    },
    "hr": {
        "fresher": ["hr associate fresher", "talent acquisition trainee", "hr executive trainee", "recruiter associate"],
        "junior":  ["hr executive", "talent acquisition executive", "recruiter"],
        "mid":     ["hr manager", "talent acquisition manager", "people operations manager"],
        "senior":  ["senior hr manager", "head of people", "director of hr"],
        "lead":    ["chro", "vp people", "director of talent"],
    },
    "consulting": {
        "fresher": ["business analyst associate", "analyst fresher", "management trainee", "strategy associate"],
        "junior":  ["business analyst", "analyst", "junior consultant"],
        "mid":     ["consultant", "senior analyst", "associate consultant"],
        "senior":  ["senior consultant", "manager", "engagement manager"],
        "lead":    ["partner", "associate director", "principal consultant"],
    },
    "mobile": {
        "fresher": ["junior android developer", "ios developer trainee", "mobile app developer fresher"],
        "junior":  ["android developer", "ios developer", "flutter developer"],
        "mid":     ["senior android developer", "senior mobile engineer"],
        "senior":  ["staff mobile engineer", "mobile tech lead"],
        "lead":    ["head of mobile", "mobile engineering manager"],
    },
}


def detect_experience_level(text: str) -> dict:
    """
    Returns {level, years, has_internship_only}.
    level is one of: fresher | junior | mid | senior | lead
    """
    t = text.lower()

    # Explicit years of experience
    exp_patterns = [
        r'(\d+)\+?\s*years?\s*of\s*(?:work\s*|total\s*|professional\s*)?experience',
        r'(\d+)\+?\s*yrs?\s*(?:of\s*)?(?:work\s*|professional\s*)?experience',
        r'experience\s*(?:of\s*)?(\d+)\+?\s*years?',
    ]
    explicit_years = 0
    for pat in exp_patterns:
        for m in re.findall(pat, t):
            explicit_years = max(explicit_years, int(m))

    # Detect if candidate is explicitly a fresher
    fresher_flags = [
        "fresher", "fresh graduate", "recent graduate", "0 years", "no prior experience",
        "entry level", "entry-level", "looking for first job", "first job",
    ]
    is_explicit_fresher = any(f in t for f in fresher_flags)

    # Detect internship-only vs full-time work
    has_internship = bool(re.search(r"\b(intern|internship|trainee|apprentice)\b", t))

    # Check for full-time employment signals (beyond internship)
    fulltime_signals = [
        r"\b(full.?time|permanent|confirmed|joined as|currently working|employed at|working at)\b",
        r"\b(engineer|developer|analyst|manager|executive|consultant|specialist|associate|lead)\s+at\b",
        r"\b(at|@)\s+[A-Z][A-Za-z]+\s*[\|\n]",  # "at CompanyName" with pipe/newline
    ]
    has_fulltime = any(re.search(p, t) for p in fulltime_signals)

    # Estimate years from date ranges in CV if no explicit mention
    if explicit_years == 0 and not is_explicit_fresher:
        year_matches = [int(y) for y in re.findall(r"\b(20\d{2})\b", t)
                        if 2000 <= int(y) <= 2026]
        if len(year_matches) >= 2:
            span = max(year_matches) - min(year_matches)
            # Conservative: halve because many dates are education, not all work
            explicit_years = min(span // 2, 20)

    # Final classification
    if is_explicit_fresher or (has_internship and not has_fulltime) or explicit_years <= 1:
        level = "fresher"
    elif explicit_years <= 3:
        level = "junior"
    elif explicit_years <= 6:
        level = "mid"
    elif explicit_years <= 10:
        level = "senior"
    else:
        level = "lead"

    internship_only = has_internship and not has_fulltime

    return {"level": level, "years": explicit_years, "internship_only": internship_only}


def detect_domains(text: str, cv_keywords: list[str]) -> list[dict]:
    """
    Score the CV against each domain.
    Returns list of {domain, display, icon, score, pct} sorted by score desc.
    Only includes domains with meaningful signal — weak domains are suppressed
    if a much stronger domain exists (prevents generic keywords from polluting results).
    """
    t = text.lower()
    scores: dict[str, float] = {}

    for domain, signals in DOMAIN_SIGNALS.items():
        s = 0.0
        for kw, weight in signals.items():
            if re.search(r"\b" + re.escape(kw) + r"\b", t):
                s += weight
        scores[domain] = s

    sorted_scores = sorted(scores.items(), key=lambda x: -x[1])
    top_score = sorted_scores[0][1] if sorted_scores else 1
    total = sum(scores.values()) or 1

    results = []
    for domain, score in sorted_scores:
        if score < 2.0:
            continue
        # Suppress weak domains: must be at least 25% of top domain's score
        if top_score > 0 and score / top_score < 0.25:
            continue
        display, icon = DOMAIN_DISPLAY[domain]
        results.append({
            "domain": domain,
            "display": display,
            "icon": icon,
            "score": round(score, 1),
            "pct": round(score / total * 100),
        })
    return results


def build_job_suggestions(domains: list[dict], exp: dict, cv_keywords: list[str]) -> list[dict]:
    """
    Build 1–3 job suggestions based on dominant domains + experience level.
    Uses actual CV keywords to build personalised search queries.
    """
    level = exp["level"]
    suggestions = []
    seen_domains: set[str] = set()
    cv_kw_set = set(cv_keywords)

    for d in domains[:4]:
        domain = d["domain"]
        if domain in seen_domains:
            continue
        seen_domains.add(domain)

        roles = DOMAIN_ROLES.get(domain, {}).get(level, [])
        if not roles:
            continue

        primary_role = roles[0]

        # Pick the most specific skill the user has from this domain
        # Prefer longer, more specific keywords over single-word ones
        domain_skills = sorted(
            [kw for kw in DOMAIN_SIGNALS.get(domain, {}) if kw in cv_kw_set and len(kw) > 4],
            key=lambda k: (-DOMAIN_SIGNALS[domain].get(k, 0), -len(k))
        )
        qualifier = domain_skills[0] if domain_skills else ""

        # Build a targeted query: role + most distinctive skill
        query = primary_role
        if qualifier and qualifier.lower() not in primary_role.lower():
            query = f"{primary_role} {qualifier}"

        pct  = d["pct"]
        display = d["display"]
        icon = d["icon"]

        level_labels = {
            "fresher": f"Entry-level {display} role — {pct}% match to your profile",
            "junior":  f"{display} role for 1–3 year experience — {pct}% match",
            "mid":     f"Mid-level {display} — {pct}% match to your profile",
            "senior":  f"Senior {display} role — {pct}% match to your profile",
            "lead":    f"Leadership {display} role — {pct}% match to your profile",
        }

        suggestions.append({
            "query":          query,
            "title":          primary_role.title(),
            "reason":         level_labels.get(level, f"{display} match"),
            "domain":         domain,
            "domain_display": display,
            "icon":           icon,
            "pct":            pct,
            "all_roles":      [r.title() for r in roles[:4]],
            "top_skill":      qualifier,
        })

        if len(suggestions) >= 3:
            break

    return suggestions


def derive_recommended_query(text: str, cv_keywords: list[str]) -> dict:
    """
    Orchestrates domain detection + experience detection → smart job recommendations.
    Returns {query, reason, top_skills, detected_role, experience, domains, suggestions}
    """
    exp     = detect_experience_level(text)
    domains = detect_domains(text, cv_keywords)
    suggestions = build_job_suggestions(domains, exp, cv_keywords)

    if suggestions:
        primary = suggestions[0]
        query   = primary["query"]
        reason  = primary["reason"]
        role    = primary["title"]
    else:
        query  = "software engineer"
        reason = "General technology role"
        role   = "Software Engineer"

    top_skills = [kw for kw in cv_keywords if len(kw) > 3][:5]

    return {
        "query":        query,
        "reason":       reason,
        "top_skills":   top_skills,
        "detected_role": role,
        "experience":   exp,
        "domains":      domains[:6],
        "suggestions":  suggestions,
    }


def score_job(job, cv_keywords):
    job_text = f"{job['title']} {job.get('company', '')}".lower()
    matched = [kw for kw in cv_keywords if re.search(r"\b" + re.escape(kw) + r"\b", job_text)]
    return len(matched), matched


_STOP_WORDS = {"the", "and", "for", "with", "job", "jobs", "role", "senior", "junior"}

def _title_similarity(query: str, title: str) -> float:
    """Token-overlap between the search query and a job title, 0..1."""
    q = {w for w in re.findall(r"[a-z0-9+#]+", query.lower()) if len(w) > 2 and w not in _STOP_WORDS}
    t = {w for w in re.findall(r"[a-z0-9+#]+", title.lower()) if len(w) > 2}
    if not q:
        return 0.0
    return len(q & t) / len(q)


def _recency_score(scraped_at: str) -> float:
    """1.0 for jobs scraped in the last 24h, decaying to 0.3 by ~5 days."""
    if not scraped_at:
        return 0.5
    try:
        ts = datetime.fromisoformat(scraped_at)
        age_h = (datetime.utcnow() - ts).total_seconds() / 3600
    except Exception:
        return 0.5
    if age_h <= 24:
        return 1.0
    if age_h >= 120:
        return 0.3
    return 1.0 - 0.7 * (age_h - 24) / 96


def compute_match(job, cv_keywords, query, country):
    """Weighted CV↔job match → (matched_keywords, pct 0-100).
    Weights: skills > title similarity > location > recency."""
    n_matched, matched = score_job(job, cv_keywords)

    # Skills: share of the CV's keywords that appear in the job (capped so a
    # handful of strong hits already scores high).
    denom = max(1, min(len(cv_keywords), 10))
    skills = min(1.0, n_matched / denom)

    title = _title_similarity(query, job.get("title", ""))

    loc_code = job.get("country") or _scrapers().detect_country(job.get("location", ""))
    if loc_code == country:
        location = 1.0
    elif loc_code == "remote":
        location = 0.7
    else:
        location = 0.2

    recency = _recency_score(job.get("scraped_at", ""))

    pct = round(100 * (0.55 * skills + 0.30 * title + 0.10 * location + 0.05 * recency))
    return matched, max(0, min(100, pct))


def _dedupe_jobs(jobs):
    """Drop the same posting listed on multiple platforms (normalized
    title + company). Keeps the first (highest-ranked) occurrence."""
    seen = set()
    out = []
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


# ─────────────────────── Routes ──────────────────────────────────

@app.after_request
def set_security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "connect-src 'self';"
    )
    return response


@app.errorhandler(429)
def too_many_requests(e):
    return jsonify({"error": "Too many requests — please wait a moment and try again"}), 429

@app.errorhandler(413)
def payload_too_large(e):
    return jsonify({"error": "File too large — please upload a CV under 2 MB."}), 413

@app.route("/")
def index():
    # This service is now a pure JSON API; the UI is the static Vercel frontend.
    return jsonify({
        "service": "cv-ats-job-finder API",
        "status": "ok",
        "endpoints": ["/api/analyze", "/api/status/<id>", "/api/results/<id>",
                      "/api/search-jobs", "/api/match-jd", "/health"],
    })



_VALID_COUNTRIES = {"in", "us", "uk", "eu", "au", "remote"}


def _ip_has_active_job(client_ip: str) -> bool:
    """True if this IP already has a queued/running job (1 analysis per IP)."""
    with _ip_lock:
        jid = _ip_active_job.get(client_ip)
    if not jid:
        return False
    st = get_queue().get_status(jid).get("status")
    return st in ("queued", "running")


@app.route("/api/analyze", methods=["POST"])
@app.route("/api/analyze-cv", methods=["POST"])          # legacy alias
@limiter.limit("10 per minute")
def analyze_cv_route():
    """Validate the upload (fast), store it, ENQUEUE an analysis job, and return
    a job_id immediately. All heavy work (parse + scrape + score) runs in the
    worker. Target: < 2s response."""
    # Only 1 file per request — reject multi-file uploads outright.
    files = request.files.getlist("cv")
    if not files:
        return jsonify({"error": "No file uploaded"}), 400
    if len(files) > 1:
        return jsonify({"error": "Please upload a single CV file."}), 400
    f = files[0]
    if not f.filename:
        return jsonify({"error": "No file selected"}), 400
    if not f.filename.lower().endswith(".pdf"):
        return jsonify({"error": "Only PDF files are supported"}), 400

    client_ip = get_remote_address()
    if _ip_has_active_job(client_ip):
        return jsonify({
            "error": "You already have an analysis in progress. Please wait for your results."
        }), 429

    raw = f.read()
    if len(raw) > MAX_PDF_BYTES:
        return jsonify({"error": "File too large — please upload a CV under 2 MB."}), 400

    # Full validation BEFORE enqueue (magic bytes, pages, encryption, embedded).
    try:
        validate_pdf(raw)
    except PdfRejected as pe:
        return jsonify({"error": str(pe)}), 400

    country = (request.form.get("country") or "in").strip().lower()
    if country not in _VALID_COUNTRIES:
        country = "in"

    q = get_queue()
    depth = q.depth()

    # Store the file with a server-generated UUID name (never the user's name).
    pdf_path = os.path.join(UPLOAD_DIR, uuid.uuid4().hex + ".pdf")
    try:
        with open(pdf_path, "wb") as out:
            out.write(raw)
        job_id = q.enqueue({"pdf_path": pdf_path, "country": country})
    except Exception as e:
        app.logger.error("enqueue failed: %s", e, exc_info=True)
        try:
            if os.path.exists(pdf_path):
                os.remove(pdf_path)
        except Exception:
            pass
        return jsonify({"error": "Could not start analysis — please try again"}), 503

    with _ip_lock:
        _ip_active_job[client_ip] = job_id

    resp = {"job_id": job_id, "status": "queued"}
    # Friendly high-demand message with queue position during a burst.
    if depth >= MAX_QUEUE_DEPTH:
        pos = q.position(job_id)
        resp["high_demand"] = True
        resp["position"] = pos
        resp["message"] = f"High demand right now — you're #{pos} in the queue. Hang tight!"
    return jsonify(resp), 202


@app.route("/api/status/<job_id>")
@limiter.limit("120 per minute")
def status_route(job_id):
    """Poll target. Returns {status: queued|running|done|failed|unknown, ...}."""
    st = get_queue().get_status(job_id)
    return jsonify(st)


@app.route("/api/results/<job_id>")
@limiter.limit("60 per minute")
def results_route(job_id):
    """Fetch the finished result. 202 while pending, 500 on failure."""
    q = get_queue()
    res = q.get_result(job_id)
    if res is None:
        st = q.get_status(job_id)
        if st.get("status") == "failed":
            return jsonify({"error": st.get("error", "Something went wrong — please re-upload")}), 500
        return jsonify({"status": st.get("status", "pending")}), 202
    # Never ship the raw CV text to the client — JD matching uses it server-side.
    public = {k: v for k, v in res.items() if k != "cv_text"}
    return jsonify(public)


@app.route("/api/search-jobs", methods=["POST"])
@limiter.limit("30 per minute")
def search_jobs_route():
    """Job search for the results page. Reads the cached job DB ONLY — no live
    scraping in the web process (that keeps every request cheap under bursts).
    The worker keeps the job DB warm via its periodic scrape + per-analysis
    scrapes, so this stays fast and safe for 100 concurrent users."""
    data          = request.get_json() or {}
    field         = (data.get("field") or "").strip()
    cv_keywords   = data.get("cv_keywords", [])
    source_filter = (data.get("source") or "").strip()
    if not field:
        return jsonify({"error": "Please enter a job field"}), 400
    if len(field) > 200:
        return jsonify({"error": "Search field too long"}), 400

    country = (data.get("country") or "in").strip().lower()
    if country not in _VALID_COUNTRIES:
        country = "in"

    sc = _scrapers()
    base_jobs = db.search_jobs(field, source=source_filter or None, limit=600)
    if country != "in":
        base_jobs = [j for j in base_jobs if sc.detect_country(j.get("location", "")) == country]

    deduped = _dedupe_jobs(base_jobs)
    jobs = []
    for job in deduped:
        j = dict(job)
        j["country"] = sc.detect_country(j.get("location", ""))
        j["matched_keywords"], j["match_pct"] = compute_match(j, cv_keywords, field, country)
        j["match_score"] = len(j["matched_keywords"])
        jobs.append(j)
    jobs.sort(key=lambda j: (-j["match_pct"], -j["match_score"]))

    return jsonify({
        "jobs":    jobs[:300],
        "total":   len(jobs),
        "cached":  True,
        "country": country,
    })


@app.route("/api/match-jd", methods=["POST"])
@limiter.limit("15 per minute")
def match_jd_route():
    """JD matching. cv_text is looked up server-side from the stored job result
    (via job_id) so it never travels to the client."""
    data    = request.get_json() or {}
    jd_text = (data.get("jd_text") or "").strip()
    job_id  = (data.get("job_id") or "").strip()
    if not jd_text:
        return jsonify({"error": "Paste a job description first"}), 400
    res = get_queue().get_result(job_id) if job_id else None
    if not res or not res.get("cv_text"):
        return jsonify({"error": "Upload and analyze your CV first"}), 400
    result = match_jd(res["cv_text"], jd_text, res.get("cv_keywords", []))
    return jsonify(result)


@app.route("/health")
def health():
    """Liveness + readiness. Checks queue reachability and worker freshness
    (worker writes a heartbeat every 30s; stale after WORKER_STALE_SECONDS)."""
    q = get_queue()
    queue_ok = q.ping()
    hb = q.last_heartbeat() if queue_ok else None
    worker_ok = hb is not None and (time.time() - hb) < WORKER_STALE_S
    ok = queue_ok and worker_ok
    return jsonify({
        "status":              "ok" if ok else "degraded",
        "web":                 True,
        "queue":               queue_ok,
        "worker":              worker_ok,
        "worker_last_seen_s":  round(time.time() - hb, 1) if hb else None,
        "queue_depth":         q.depth() if queue_ok else None,
    }), (200 if ok else 503)


@app.route("/api/ping")
def ping():
    return jsonify({"ok": True})


# The web process is now PURE API — no scraping/scheduler runs here. The worker
# (worker.py) owns all heavy work: per-job scraping + a periodic cache refresh.
db.init_db()

if __name__ == "__main__":
    # Local dev of the API only (production uses gunicorn via the Procfile).
    port = int(os.environ.get("PORT", 5001))
    app.run(debug=False, port=port, host="0.0.0.0")
