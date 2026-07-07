#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────
#  Free HTTPS + reachability via Cloudflare Quick Tunnel.
#
#  Why: gives the site a real https:// URL (secure padlock, no "not secure")
#  AND bypasses any inbound network firewall — cloudflared dials OUT to
#  Cloudflare, so blocked inbound ports don't matter. No domain, no account,
#  no cost.
#
#  Trade-off: the quick-tunnel URL is random and changes if the tunnel
#  restarts. Fine to get live + secure for free today; for a permanent
#  branded URL you'd add a cheap domain later (named tunnel).
#
#  USAGE (as root on the VPS):
#     sudo bash tunnel.sh
#  then read the printed https://<random>.trycloudflare.com URL.
# ─────────────────────────────────────────────────────────────────────────
set -euo pipefail

echo ">> Installing cloudflared…"
if ! command -v cloudflared >/dev/null 2>&1; then
  ARCH=$(dpkg --print-architecture)   # amd64 / arm64
  curl -fsSL -o /usr/local/bin/cloudflared \
    "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${ARCH}"
  chmod +x /usr/local/bin/cloudflared
fi

echo ">> Creating cloudflared systemd service (tunnels localhost:80)…"
cat > /etc/systemd/system/cvfinder-tunnel.service <<'EOF'
[Unit]
Description=Cloudflare Quick Tunnel for CV Finder
After=network.target nginx.service

[Service]
# --url points at nginx (which serves the frontend + proxies /api). The
# tunnel exposes the whole site over HTTPS. Output (incl. the public URL)
# goes to the journal: journalctl -u cvfinder-tunnel
ExecStart=/usr/local/bin/cloudflared tunnel --no-autoupdate --url http://localhost:80
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now cvfinder-tunnel

echo ">> Waiting for the tunnel URL…"
sleep 8
URL=$(journalctl -u cvfinder-tunnel --no-pager | grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' | tail -1)

echo ""
echo "════════════════════════════════════════════════════════════════"
if [ -n "$URL" ]; then
  echo " Your LIVE, SECURE site (share this):"
  echo "   $URL"
else
  echo " Tunnel starting… run this in ~10s to see your URL:"
  echo "   journalctl -u cvfinder-tunnel | grep trycloudflare"
fi
echo "════════════════════════════════════════════════════════════════"
