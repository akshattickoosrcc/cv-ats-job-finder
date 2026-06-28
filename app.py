import io
import os
import re
import threading
from datetime import datetime, timezone

import pdfplumber
from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, jsonify, render_template, request

import db
import scrapers

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024

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

# ─────────────────────── PDF parsing ─────────────────────────────

def parse_cv_text(file_stream):
    parts = []
    with pdfplumber.open(file_stream) as pdf:
        for page in pdf.pages:
            txt = page.extract_text()
            if txt:
                parts.append(txt)
    return "\n".join(parts).strip()


# ─────────────────────── ATS analysis ────────────────────────────

def analyze_ats(text):
    score = 0
    issues = []
    suggestions = []
    missing_keywords = []
    t = text.lower()
    lines = [l.strip() for l in text.split("\n") if l.strip()]

    has_email    = bool(re.search(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", text))
    has_phone    = bool(re.search(r"(\+?\d[\d\s\-(). ]{7,}\d)", text))
    has_linkedin = "linkedin" in t
    has_github   = "github" in t

    if has_email:    score += 6
    else:            issues.append("No email address detected"); suggestions.append("Add a professional email address at the top of your CV")
    if has_phone:    score += 5
    else:            issues.append("No phone number found"); suggestions.append("Include a phone number with country code")
    if has_linkedin: score += 2
    else:            suggestions.append("Add your LinkedIn profile URL to boost visibility")
    if has_github:   score += 2
    else:            suggestions.append("Include a GitHub or portfolio link if applicable")

    weights = {"experience": 8, "education": 6, "skills": 6, "summary": 3, "projects": 2}
    for section, pts in weights.items():
        if any(kw in t for kw in SECTION_PATTERNS[section]):
            score += pts
        else:
            issues.append(f"'{section.capitalize()}' section not clearly labeled")
            suggestions.append(f"Add a clear '{section.capitalize()}' header — ATS systems scan for these keywords")

    found_verbs   = [v for v in ACTION_VERBS if v in t]
    score        += min(15, len(found_verbs) * 2)
    missing_verbs = [v for v in ACTION_VERBS if v not in t]
    if len(found_verbs) < 5:
        missing_keywords.extend(missing_verbs[:6])
        suggestions.append(f"Add more action verbs: {', '.join(missing_verbs[:4])}")
    elif len(found_verbs) < 8:
        suggestions.append(f"Strengthen bullet points with verbs like: {', '.join(missing_verbs[:3])}")

    qty_patterns = [
        r"\d+\s*%", r"\$\s*[\d,]+", r"\b\d+\s*(million|thousand|k|m)\b",
        r"\b\d{2,}\s*(users|customers|clients|projects|employees|members|students)\b",
        r"(increased|decreased|reduced|improved|grew|saved|generated)\s+\w*\s*\d+",
    ]
    qty = sum(1 for p in qty_patterns if re.search(p, t))
    score += min(15, qty * 5)
    if qty == 0:
        issues.append("No quantifiable achievements found")
        suggestions.append("Quantify your impact: 'Increased sales by 35%', 'Led a team of 8'")
    elif qty < 3:
        suggestions.append("Add more measurable outcomes — aim for 4–6 quantified achievements")

    words = len(text.split())
    if   300 <= words <= 800:   score += 10
    elif 800 < words <= 1200:   score += 6;  suggestions.append(f"CV is {words} words — consider trimming to 1–2 pages")
    elif words < 300:           score += 3;  issues.append(f"CV is very short ({words} words)"); suggestions.append("Expand your experience and skills sections")
    else:                       score += 2;  issues.append(f"CV is quite long ({words} words)"); suggestions.append("Trim to 1–2 pages for best ATS performance")

    has_bullets = any(re.match(r"^[•\-\*·▪▸►]", l) for l in lines)
    has_dates   = bool(re.search(
        r"\b(19|20)\d{2}\b|\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s*\d{4}", t))
    if has_bullets: score += 5
    else:           issues.append("No bullet points detected"); suggestions.append("Use bullet points for responsibilities and achievements")
    if has_dates:   score += 5
    else:           issues.append("Employment/education dates not found"); suggestions.append("Add month–year dates to each role and education entry")

    non_ascii = len(re.findall(r"[^\x00-\x7F]", text))
    if non_ascii > 30:
        score -= 3
        issues.append(f"{non_ascii} non-ASCII characters — may confuse ATS parsers")
        suggestions.append("Use plain ASCII; avoid fancy bullets or emoji")
    if re.search(r"(\|[^\|]+\|)", text):
        score -= 2
        issues.append("Table formatting detected — ATS systems often misread tables")
        suggestions.append("Replace tables with plain bullet lists")

    return {
        "score": max(0, min(100, score)),
        "word_count": words,
        "missing_keywords": list(set(missing_keywords))[:10],
        "issues": issues,
        "suggestions": suggestions,
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


def score_job(job, cv_keywords):
    job_text = f"{job['title']} {job.get('company', '')}".lower()
    matched = [kw for kw in cv_keywords if re.search(r"\b" + re.escape(kw) + r"\b", job_text)]
    return len(matched), matched


# ─────────────────────── Routes ──────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/analyze-cv", methods=["POST"])
def analyze_cv_route():
    if "cv" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400
    f = request.files["cv"]
    if not f.filename:
        return jsonify({"error": "No file selected"}), 400
    if not f.filename.lower().endswith(".pdf"):
        return jsonify({"error": "Only PDF files are supported"}), 400
    try:
        stream = io.BytesIO(f.read())
        text   = parse_cv_text(stream)
        if not text.strip():
            return jsonify({"error": "Could not extract text — is the PDF a scanned image?"}), 400

        result      = analyze_ats(text)
        cv_keywords = extract_cv_keywords(text)
        result["cv_keywords"] = cv_keywords

        # ── Diff against last review ──
        last = db.get_last_ats_review()
        if last:
            prev_score  = last["score"]
            prev_issues = set(last["issues"])
            curr_issues = set(result["issues"])
            result["prev_score"]     = prev_score
            result["score_delta"]    = result["score"] - prev_score
            result["fixed_issues"]   = sorted(prev_issues - curr_issues)
            result["new_issues"]     = sorted(curr_issues - prev_issues)
            result["persisted_issues"] = sorted(curr_issues & prev_issues)
            result["has_prev_review"] = True
        else:
            result["has_prev_review"] = False

        # Persist this review as the new baseline
        db.save_ats_review(
            result["score"], result["word_count"],
            result["issues"], result["suggestions"], cv_keywords
        )
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": f"Error processing CV: {e}"}), 500


@app.route("/api/search-jobs", methods=["POST"])
def search_jobs_route():
    data        = request.get_json() or {}
    field       = (data.get("field") or "").strip()
    cv_keywords = data.get("cv_keywords", [])
    source_filter = (data.get("source") or "").strip()
    if not field:
        return jsonify({"error": "Please enter a job field"}), 400

    # Pull from cache first
    cached = db.search_jobs(field, source=source_filter or None, limit=600)

    # Supplement with live scrape if cache is thin
    live_jobs: list[dict] = []
    if len(cached) < 30:
        live_jobs = scrapers.scrape_live(field)
        db.save_jobs(live_jobs)
        cached = db.search_jobs(field, source=source_filter or None, limit=600)

    # Tag India jobs and score by CV match
    india_cities = {
        "india", "bengaluru", "bangalore", "mumbai", "delhi", "hyderabad", "pune",
        "chennai", "kolkata", "noida", "gurgaon", "gurugram", "ahmedabad", "jaipur",
        "chandigarh", "kochi", "trivandrum", "bhubaneswar", "indore", "coimbatore",
    }
    for job in cached:
        loc = job.get("location", "").lower()
        job["is_india"] = any(c in loc for c in india_cities)
        job["match_score"], job["matched_keywords"] = score_job(job, cv_keywords)

    # Sort: India first within each match-score tier
    cached.sort(key=lambda j: (-j["match_score"], 0 if j["is_india"] else 1))

    return jsonify({
        "jobs":        cached[:300],
        "total":       len(cached),
        "live_scraped": len(live_jobs) > 0,
        "cached":      True,
    })


@app.route("/api/scrape-status")
def scrape_status():
    last = db.get_last_scrape()
    return jsonify({
        "running":    scrapers.is_scrape_running(),
        "total_jobs": db.total_jobs(),
        "last_scrape": last,
    })


@app.route("/api/scrape-now", methods=["POST"])
def scrape_now():
    if scrapers.is_scrape_running():
        return jsonify({"message": "Scrape already in progress"}), 202
    threading.Thread(target=scrapers.run_full_scrape, daemon=True).start()
    return jsonify({"message": "Full scrape started in background"})


# ─────────────────────── Scheduler ───────────────────────────────

def _start_scheduler():
    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(
        scrapers.run_full_scrape,
        trigger="interval",
        hours=24,
        id="daily_scrape",
        replace_existing=True,
    )
    scheduler.start()
    # Initial scrape on startup (non-blocking)
    threading.Thread(target=scrapers.run_full_scrape, daemon=True).start()


if __name__ == "__main__":
    db.init_db()
    _start_scheduler()
    port = int(os.environ.get("PORT", 5001))
    app.run(debug=False, port=port, host="0.0.0.0")
