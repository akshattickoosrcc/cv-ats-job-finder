#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────
#  One-shot bootstrap for the CV-analyzer BACKEND on a fresh Ubuntu VPS
#  (Hostinger / DigitalOcean / any Ubuntu 22.04 or 24.04 box).
#
#  It installs Python, Redis and nginx; creates a service user; sets up a
#  virtualenv; writes systemd services for the WEB and WORKER; configures an
#  nginx reverse proxy; and (optionally) gets a free HTTPS certificate.
#
#  USAGE (as root on a fresh box):
#     1. Edit the CONFIG block below (DOMAIN + FRONTEND_ORIGIN at minimum).
#     2. scp this repo to the box, or set REPO_URL to clone it.
#     3. sudo bash setup.sh
#
#  Re-running is safe (idempotent-ish): it updates code and restarts services.
# ─────────────────────────────────────────────────────────────────────────
set -euo pipefail

# ───────────────────────────── CONFIG ───────────────────────────────────
DOMAIN=""                                         # leave blank — serving frontend from nginx directly (no domain/HTTPS needed)
FRONTEND_ORIGIN="http://200.97.163.35"            # same-origin, CORS is self-referential
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
                   certbot python3-certbot-nginx build-essential

echo ">> Enabling Redis…"
systemctl enable --now redis-server

echo ">> Creating service user + directories…"
id -u "$APP_USER" &>/dev/null || useradd --system --create-home --shell /usr/sbin/nologin "$APP_USER"
mkdir -p "$APP_DIR" "$DATA_DIR/uploads"
chown -R "$APP_USER:$APP_USER" "$DATA_DIR"

echo ">> Fetching code ($BRANCH)…"
if [ -d "$APP_DIR/.git" ]; then
  git -C "$APP_DIR" fetch --all -q && git -C "$APP_DIR" checkout "$BRANCH" -q && git -C "$APP_DIR" pull -q
else
  git clone -q --branch "$BRANCH" "$REPO_URL" "$APP_DIR"
fi
chown -R "$APP_USER:$APP_USER" "$APP_DIR"

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
WEB_WORKERS=3
WEB_THREADS=8
MAX_QUEUE_DEPTH=40
PARSE_TIMEOUT=25
PARSE_MEM_LIMIT_MB=512
WORKER_STALE_SECONDS=120
WORKER_SCRAPE=1
SCRAPE_MODE=full
PDF_EXTRACTOR=pymupdf
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

    # Serve the static frontend
    root $APP_DIR/frontend;
    index index.html;

    location / {
        try_files \$uri \$uri/ /index.html;
    }

    # Proxy API and health to gunicorn
    location ~ ^/(api|health)(/|\$) {
        proxy_pass http://127.0.0.1:$PORT;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$remote_addr;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 60s;
    }
}
EOF
ln -sf /etc/nginx/sites-available/cvfinder /etc/nginx/sites-enabled/cvfinder
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl reload nginx

echo ">> Firewall…"
ufw allow OpenSSH >/dev/null 2>&1 || true
ufw allow 'Nginx Full' >/dev/null 2>&1 || true
yes | ufw enable >/dev/null 2>&1 || true

echo ""
echo "════════════════════════════════════════════════════════════════"
echo " Done! Open http://200.97.163.35 in your browser."
echo "   Health:  curl http://200.97.163.35/health"
echo "   Logs:    journalctl -u cvfinder-web -f"
echo "            journalctl -u cvfinder-worker -f"
echo "════════════════════════════════════════════════════════════════"
