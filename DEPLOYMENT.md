# Deployment Guide — CV Analyzer (Vercel frontend + UPI-paid VPS backend)

This is the click-by-click guide to run the new architecture. Written for a
first-timer. Total time: ~45 minutes. Total cost: **~₹430–680/mo (~$7–9)**,
payable by **UPI**.

---

## 0. The architecture (what you're building)

```
   Visitor
     │
     ▼
┌──────────────────┐        HTTPS (CORS)        ┌───────────────────────────────┐
│  Vercel (FREE)   │  ───────────────────────▶ │  Your Ubuntu VPS (UPI, ~$7/mo) │
│  static frontend │   /api/analyze etc.        │                               │
│  • loads instant │                            │  nginx ──▶ gunicorn (web API) │
│  • no cold start │                            │              │ enqueue        │
└──────────────────┘                            │              ▼                │
                                                │            Redis (queue)      │
                                                │              ▲ dequeue        │
                                                │          worker.py (parse +   │
                                                │          scrape + score)      │
                                                └───────────────────────────────┘
```

- **Frontend** (this repo's `frontend/`) is static HTML/CSS/JS → Vercel's global
  CDN. It renders instantly and never cold-starts. It only calls the backend
  when someone clicks **Analyze**.
- **Backend** (the Python code) runs on a VPS that **never sleeps** → no cold
  start. The web process only validates + enqueues (returns in <2s). A separate
  **worker** does the heavy parsing/scraping. **Redis** is the queue.

Why a VPS and not Render? Render/Railway/Fly require an international credit
card. A VPS (Hostinger/DigitalOcean) accepts **UPI**, never sleeps, gives you
2–8 GB RAM, and runs Redis for free on the same box.

---

## PART A — Backend on a VPS (do this first)

### A1. Buy a VPS (UPI)

**Hostinger (recommended, cheapest, UPI):**
1. Go to hostinger.in → **VPS Hosting** → pick **KVM 1** (or KVM 2 for more RAM).
2. Choose **Ubuntu 24.04** (no control panel needed).
3. Pay with **UPI** at checkout.
4. In hPanel, set a **root password** and note the server's **IP address**.

*(DigitalOcean alternative: create a **Basic droplet**, Ubuntu 24.04, $6/mo,
Regular CPU 1GB. Pay via UPI/Razorpay. Note the droplet IP.)*

### A2. Point a domain at the VPS (recommended, enables HTTPS)

In your domain registrar's DNS settings, add an **A record**:

| Type | Name | Value |
|------|------|-------|
| A    | `api` | your-VPS-IP |

So `api.yourdomain.com` → your VPS. (No domain? You can skip and use the raw IP
over HTTP for testing, but HTTPS + a domain is strongly recommended for a real
product — browsers block mixed content and it looks trustworthy.)

### A3. Run the bootstrap script

From your computer's terminal:

```bash
ssh root@YOUR_VPS_IP           # enter the root password you set
```

Then on the VPS:

```bash
# get the code
apt-get update -y && apt-get install -y git
git clone -b architecture-upgrade https://github.com/akshattickoosrcc/cv-ats-job-finder.git /opt/cvfinder
cd /opt/cvfinder

# edit ONLY the CONFIG block at the top: DOMAIN, FRONTEND_ORIGIN, email
nano setup.sh
#   DOMAIN="api.yourdomain.com"
#   FRONTEND_ORIGIN="https://your-app.vercel.app"   # you'll get this in Part B; you can re-run later
#   LETSENCRYPT_EMAIL="you@example.com"
# (Ctrl-O, Enter, Ctrl-X to save)

sudo bash setup.sh
```

The script installs everything, creates the services, sets up nginx, and (if
DOMAIN is set and DNS has propagated) gets a free HTTPS certificate.

### A4. Verify the backend

```bash
curl http://YOUR_VPS_IP/health          # or https://api.yourdomain.com/health
```

You should see `{"status":"ok","queue":true,"worker":true,...}`. If `worker`
is `false`, check `journalctl -u cvfinder-worker -f`.

> You'll come back to A once (A5) after Part B to lock CORS to your real Vercel
> URL. For now the backend is live.

---

## PART B — Frontend on Vercel (free, no card)

### B1. Set your API URL

Edit **`frontend/config.js`** in the repo and set your backend URL:

```js
window.API_BASE = "https://api.yourdomain.com";   // or "http://YOUR_VPS_IP" if no domain
```

Commit and push this change.

### B2. Deploy on Vercel

1. Go to **vercel.com** → sign up with **GitHub** (free, no payment method needed).
2. **Add New → Project** → import `cv-ats-job-finder`.
3. In the import screen:
   - **Root Directory** → click **Edit** → select **`frontend`**.
   - **Framework Preset** → **Other**.
   - Build/Output settings → leave empty (it's plain static files).
4. Click **Deploy**. In ~20s you get a URL like `https://cv-ats-job-finder.vercel.app`.

### B3. Lock CORS to your real Vercel URL (back to the VPS)

Now that you know your Vercel URL, on the VPS:

```bash
nano /opt/cvfinder/.env
#   FRONTEND_ORIGIN=https://cv-ats-job-finder.vercel.app
systemctl restart cvfinder-web
```

Reload your Vercel site → upload a CV → it should analyze end-to-end.

---

## PART C — UptimeRobot (free monitoring + keep-warm)

1. Sign up at **uptimerobot.com** (free).
2. **Add New Monitor** ×2:

| Monitor type | URL | Interval |
|---|---|---|
| HTTP(s) | `https://cv-ats-job-finder.vercel.app` | 5 minutes |
| HTTP(s) | `https://api.yourdomain.com/health` | 5 minutes |

3. Under **Alert Contacts**, add your email and attach it to both monitors.

The `/health` monitor tells you the moment the queue or worker dies (it returns
HTTP 503 if the worker's heartbeat is stale). On a VPS your backend never sleeps,
so these pings are purely for alerting — but they also keep any future
free/keep-warm host awake.

---

## Environment variables reference

### Backend (VPS — in `/opt/cvfinder/.env`, created by setup.sh)

| Var | Example | Purpose |
|---|---|---|
| `SECRET_KEY` | (random hex) | Flask secret. setup.sh generates one. |
| `FRONTEND_ORIGIN` | `https://your-app.vercel.app` | CORS allow-list (comma-separated). |
| `QUEUE_BACKEND` | `redis` | `redis` in prod, `sqlite` for dev. |
| `REDIS_URL` | `redis://localhost:6379/0` | Redis connection. |
| `DATA_DIR` | `/var/lib/cvfinder` | Job cache DB + uploads live here. |
| `UPLOAD_DIR` | `/var/lib/cvfinder/uploads` | Temp CV files (deleted after each job). |
| `PORT` | `8000` | gunicorn port (nginx proxies to it). |
| `WEB_WORKERS` / `WEB_THREADS` | `3` / `8` | Web concurrency (see gunicorn.conf.py). |
| `MAX_QUEUE_DEPTH` | `40` | Show "you're #X in queue" past this. |
| `PARSE_TIMEOUT` | `25` | Seconds before a parse job fails. |
| `PARSE_MEM_LIMIT_MB` | `512` | Address-space cap for the parse subprocess. |
| `WORKER_STALE_SECONDS` | `120` | `/health` fails if worker heartbeat older. |
| `WORKER_SCRAPE` / `SCRAPE_MODE` | `1` / `full` | Worker keeps job cache warm. |
| `PDF_EXTRACTOR` | `pymupdf` | `pypdf` if AGPL licensing is a concern. |

### Frontend (Vercel)

None required — the API URL is set in `frontend/config.js`. (You *can* instead
use a Vercel env var + a build step, but the single-line config.js is simplest.)

---

## Cost breakdown (under $10/mo, UPI)

| Item | Cost | Notes |
|---|---|---|
| Vercel (frontend) | **₹0** | Free tier, no card required. |
| Hostinger KVM 1 VPS | **~₹430–680/mo (~$5–8)** | UPI. Redis + web + worker all on this one box. |
| Redis | **₹0** | Runs locally on the VPS. |
| UptimeRobot | **₹0** | Free plan, 50 monitors. |
| **Total** | **~$5–8/mo** | Under the $10 cap. |

---

## Load test results (measured)

Run against gunicorn (3 workers × 8 threads) with the warm job cache:

```
== 100 concurrent visitors → GET /health ==
  status codes : {200: 100}
  latency p50  : 18 ms   p95 : 36 ms   p99 : 44 ms   max : 50 ms

== 20 simultaneous uploads → POST /api/analyze ==
  status codes : {202: 20}          (all accepted, none dropped)
  upload p95   : 109 ms             (target < 2000 ms ✅)

== 20 enqueued jobs polled to completion ==
  completed    : 20/20
  job time p95 : 6.1 s              (target < 45 s ✅)
```

To reproduce against your staging box:
```bash
python3 loadtest.py https://api.yourdomain.com sample_cv.pdf --visitors 100 --uploads 20
```
The web stays sub-50ms under 100 concurrent hits because it does no heavy work;
uploads return in ~100ms; jobs finish in the background. To process more jobs in
parallel during a huge burst, run 2 worker processes (add a second
`cvfinder-worker` systemd unit) — each is independent.

---

## Rollback plan (back to the old setup in < 5 minutes)

The old single-service app is untouched on the **`main`** branch. Two options:

**Option 1 — point the frontend back (fastest, ~1 min).**
If your old Render/monolith is still deployed, edit `frontend/config.js`:
```js
window.API_BASE = "https://job-finder-2fr1.onrender.com";
```
push → Vercel redeploys in ~20s. Or just reopen the old Render URL directly —
it serves its own UI. The new VPS can keep running or be paused; nothing else
changes.

**Option 2 — redeploy the old monolith on the VPS (~5 min).**
```bash
cd /opt/cvfinder
git checkout main
systemctl stop cvfinder-worker
.venv/bin/pip install -r requirements.txt
# run the old app under gunicorn on the same port nginx proxies to:
systemctl restart cvfinder-web     # main's wsgi.py starts the monolith
```
Because `main`'s `wsgi:app` is the self-contained old app, the same nginx proxy
keeps working. To go forward again: `git checkout architecture-upgrade` and
restart both services.

Nothing in this change touches the **payments/Superprofile** flow.

---

## Troubleshooting

| Symptom | Check |
|---|---|
| Frontend loads but "Could not reach server" | `FRONTEND_ORIGIN` matches your Vercel URL exactly; `curl https://api.../health` works. |
| `/health` shows `worker: false` | `journalctl -u cvfinder-worker -f` — is Redis up? `systemctl status redis-server`. |
| Uploads 429 immediately | Per-IP guard — one analysis at a time per IP is by design. |
| HTTPS cert failed | DNS A record must point to the VPS first; then `certbot --nginx -d api.yourdomain.com`. |
| Jobs stuck "queued" | Worker not running / Redis down. Restart: `systemctl restart cvfinder-worker`. |
| See failed jobs / 5xx | `journalctl -u cvfinder-worker -u cvfinder-web -f` (logs every failure). |
