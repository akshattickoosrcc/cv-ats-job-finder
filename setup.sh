#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────
#  One-shot bootstrap for CV-analyzer on a fresh Ubuntu 22.04/24.04 VPS.
#  Serving frontend statically from nginx — no domain or HTTPS required.
#  Re-running is safe (idempotent-ish): updates code and restarts services.
#
#  USAGE (as root):
#     git clone -b architecture-upgrade https://github.com/akshattickoosrcc/cv-ats-job-finder.git /opt/cvfinder
#     cd /opt/cvfinder && sudo bash setup.sh
# ─────────────────────────────────────────────────────────────────────────
set -euo pipefail

# ───────────────────────────── CONFIG ───────────────────────────────────
DOMAIN=""                               # blank = IP-only, no HTTPS needed
FRONTEND_ORIGIN="http://200.97.163.35"
REPO_URL="https://github.com/akshattickoosrcc/cv-ats-job-finder.git"
BRANCH="architecture-upgrade"
LETSENCRYPT_EMAIL=""
# ────────────────────────────────────────────────────────────────────────

APP_USER="cvfinder"
APP_DIR="/opt/cvfinder"
DATA_DIR="/var/lib/cvfinder"
PORT="8000"

echo ">> Installing system packages…"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y python3 python3-venv python3-pip redis-server nginx git ufw \
                   certbot python3-certbot-nginx build-essential cron

# ── Swap (2 GB) — critical on 1 GB VPS to survive PyMuPDF + workers ────
echo ">> Setting up 2 GB swap…"
if ! swapon --show | grep -q /swapfile; then
  fallocate -l 2G /swapfile
  chmod 600 /swapfile
  mkswap /swapfile
  swapon /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi
# Only swap when RAM is nearly full (avoids thrashing under normal load)
sysctl -w vm.swappiness=10 >/dev/null
grep -q 'vm.swappiness' /etc/sysctl.conf \
  || echo 'vm.swappiness=10' >> /etc/sysctl.conf

# ── Cap systemd journal so logs never fill the disk ─────────────────────
echo ">> Capping journal size to 500 MB…"
mkdir -p /etc/systemd/journald.conf.d/
cat > /etc/systemd/journald.conf.d/cvfinder.conf <<'JOURNALEOF'
[Journal]
SystemMaxUse=500M
JOURNALEOF
systemctl restart systemd-journald

echo ">> Enabling Redis…"
systemctl enable --now redis-server

echo ">> Creating service user + directories…"
id -u "$APP_USER" &>/dev/null || useradd --system --create-home --shell /usr/sbin/nologin "$APP_USER"
mkdir -p "$APP_DIR" "$DATA_DIR/uploads"
chown -R "$APP_USER:$APP_USER" "$DATA_DIR"

echo ">> Fetching code ($BRANCH)…"
if [ -d "$APP_DIR/.git" ]; then
  git -C "$APP_DIR" fetch --all -q
  git -C "$APP_DIR" checkout "$BRANCH" -q
  git -C "$APP_DIR" pull -q
else
  git clone -q --branch "$BRANCH" "$REPO_URL" "$APP_DIR"
fi
chown -R "$APP_USER:$APP_USER" "$APP_DIR"
# nginx (www-data) needs read access to serve the static frontend
chmod 755 "$APP_DIR"
chmod -R a+rX "$APP_DIR/frontend"

echo ">> Python virtualenv + deps…"
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install -q --upgrade pip
"$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

echo ">> Writing environment file…"
SECRET=$(python3 -c "import secrets;print(secrets.token_hex(32))")
cat > "$APP_DIR/.env" <<EOF
SECRET_KEY=$SECRET
FRONTEND_ORIGIN=$FRONTEND_ORIGIN
QUEUE_BACKEND=redis
REDIS_URL=redis://localhost:6379/0
DATA_DIR=$DATA_DIR
UPLOAD_DIR=$DATA_DIR/uploads
PORT=$PORT
# 2 workers on a 1 GB box: each Flask worker ~100 MB, leaves room for Redis + worker
WEB_WORKERS=2
WEB_THREADS=8
MAX_QUEUE_DEPTH=40
PARSE_TIMEOUT=25
PARSE_MEM_LIMIT_MB=400
WORKER_STALE_SECONDS=120
WORKER_SCRAPE=1
SCRAPE_MODE=full
PDF_EXTRACTOR=pymupdf
# Faster analysis: cap each job-source scrape at 6s so results land in ~3-5s
SOURCE_TIMEOUT=6
EOF
chown "$APP_USER:$APP_USER" "$APP_DIR/.env"
chmod 600 "$APP_DIR/.env"

echo ">> Creating systemd services…"
cat > /etc/systemd/system/cvfinder-web.service <<EOF
[Unit]
Description=CV Finder web API (gunicorn)
After=network.target redis-server.service
Requires=redis-server.service

[Service]
User=$APP_USER
WorkingDirectory=$APP_DIR
EnvironmentFile=$APP_DIR/.env
ExecStart=$APP_DIR/.venv/bin/gunicorn wsgi:app -c gunicorn.conf.py
Restart=always
RestartSec=3
StartLimitIntervalSec=60
StartLimitBurst=5

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/cvfinder-worker.service <<EOF
[Unit]
Description=CV Finder analysis worker
After=network.target redis-server.service
Requires=redis-server.service

[Service]
User=$APP_USER
WorkingDirectory=$APP_DIR
EnvironmentFile=$APP_DIR/.env
ExecStart=$APP_DIR/.venv/bin/python worker.py
Restart=always
RestartSec=3
StartLimitIntervalSec=60
StartLimitBurst=5
# Cap worker memory: PyMuPDF parse subprocess + scraping peaks at ~350 MB.
# If it goes over, systemd kills just the worker (which auto-restarts);
# this protects the web process and Redis from being OOM-killed instead.
MemoryMax=450M
OOMScoreAdj=500

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now cvfinder-web cvfinder-worker

echo ">> Configuring nginx (static frontend + API proxy)…"
cat > /etc/nginx/sites-available/cvfinder <<EOF
server {
    listen 80 default_server;
    server_name _;
    client_max_body_size 3m;

    # Static frontend — served by nginx, instant, no cold start
    root $APP_DIR/frontend;
    index index.html;

    location / {
        try_files \$uri \$uri/ /index.html;
    }

    # API + health → gunicorn (all heavy work lives here)
    location ~ ^/(api|health)(/|\$) {
        proxy_pass http://127.0.0.1:$PORT;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$remote_addr;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 60s;
        proxy_connect_timeout 5s;
    }
}
EOF
ln -sf /etc/nginx/sites-available/cvfinder /etc/nginx/sites-enabled/cvfinder
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl enable --now nginx && systemctl reload nginx

echo ">> Firewall…"
ufw allow OpenSSH >/dev/null 2>&1 || true
ufw allow 'Nginx Full' >/dev/null 2>&1 || true
yes | ufw enable >/dev/null 2>&1 || true

# ── Cleanup cron: delete uploaded PDFs older than 1 h (worker deletes ──
# ── immediately on success/failure, but crashed jobs leave orphans).   ──
echo ">> Installing upload-cleanup cron…"
echo "*/30 * * * * $APP_USER find $DATA_DIR/uploads -mmin +60 -type f -delete 2>/dev/null" \
  > /etc/cron.d/cvfinder-cleanup
chmod 644 /etc/cron.d/cvfinder-cleanup
systemctl enable --now cron 2>/dev/null || true

echo ""
echo "════════════════════════════════════════════════════════════════"
echo " Done! Open in your browser: http://200.97.163.35"
echo "   Health check:  curl http://200.97.163.35/health"
echo "   Web logs:      journalctl -u cvfinder-web -f"
echo "   Worker logs:   journalctl -u cvfinder-worker -f"
echo "════════════════════════════════════════════════════════════════"
