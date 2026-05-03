#!/usr/bin/env bash
# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  StreamBot — VPS/Linux Setup Script                                      ║
# ║  Tested on Ubuntu 22.04 / Debian 12                                     ║
# ║  Run as root or with sudo                                               ║
# ╚══════════════════════════════════════════════════════════════════════════╝
set -euo pipefail

BOLD='\033[1m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

log()  { echo -e "${GREEN}[+]${NC} $*"; }
warn() { echo -e "${YELLOW}[!]${NC} $*"; }
fail() { echo -e "${RED}[✗]${NC} $*"; exit 1; }
step() { echo -e "\n${BOLD}══ $* ══${NC}"; }

# ── Checks ────────────────────────────────────────────────────────────────────
[[ $EUID -eq 0 ]] || fail "Run as root: sudo bash setup.sh"

step "System Update"
apt-get update -qq
apt-get upgrade -y -qq

step "Install Dependencies"
apt-get install -y --no-install-recommends \
    python3.11 \
    python3.11-venv \
    python3-pip \
    ffmpeg \
    redis-server \
    curl \
    git \
    htop \
    logrotate \
    supervisor

log "FFmpeg version: $(ffmpeg -version 2>&1 | head -1)"
log "Python version: $(python3.11 --version)"

step "Create System User"
id streambot &>/dev/null || useradd -r -s /bin/bash -d /opt/streambot streambot
log "User 'streambot' ready"

step "Create Directories"
mkdir -p \
    /opt/streambot \
    /data/streambot \
    /var/log/streambot \
    /etc/streambot

chown -R streambot:streambot /opt/streambot /data/streambot /var/log/streambot
chmod 750 /opt/streambot /data/streambot

step "Configure Redis"
cat > /etc/redis/redis.conf.d/streambot.conf << 'EOF'
maxmemory 512mb
maxmemory-policy allkeys-lru
save 60 1
loglevel warning
EOF
systemctl enable --now redis-server
redis-cli ping | grep -q PONG && log "Redis OK" || fail "Redis not responding"

step "Copy Application Files"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cp -r "$SCRIPT_DIR"/../* /opt/streambot/
chown -R streambot:streambot /opt/streambot

step "Python Virtual Environment"
python3.11 -m venv /opt/streambot/venv
/opt/streambot/venv/bin/pip install --upgrade pip -q
/opt/streambot/venv/bin/pip install -r /opt/streambot/requirements.txt -q
log "Python packages installed"

step "Environment Configuration"
if [[ ! -f /opt/streambot/.env ]]; then
    cp /opt/streambot/.env.example /opt/streambot/.env
    warn "Created /opt/streambot/.env — EDIT THIS FILE with your values!"
    warn "Especially: TELEGRAM_BOT_TOKEN and TELEGRAM_ADMIN_IDS"
fi

step "Supervisor Configuration (Process Manager)"
cat > /etc/supervisor/conf.d/streambot.conf << 'SUPERVISORD'
[group:streambot]
programs=streambot_bot,streambot_worker,streambot_beat

[program:streambot_bot]
command=/opt/streambot/venv/bin/python -m bot.main
directory=/opt/streambot
user=streambot
environment=PYTHONPATH="/opt/streambot"
autostart=true
autorestart=true
startretries=5
redirect_stderr=false
stdout_logfile=/var/log/streambot/bot.stdout.log
stderr_logfile=/var/log/streambot/bot.stderr.log
stdout_logfile_maxbytes=50MB
stdout_logfile_backups=3

[program:streambot_worker]
command=/opt/streambot/venv/bin/celery -A workers.celery_app worker
         --loglevel=info
         --queues=normal,priority
         --concurrency=4
         --max-tasks-per-child=50
         --hostname=worker@%%h
directory=/opt/streambot
user=streambot
environment=PYTHONPATH="/opt/streambot"
autostart=true
autorestart=true
startretries=5
redirect_stderr=false
stdout_logfile=/var/log/streambot/worker.stdout.log
stderr_logfile=/var/log/streambot/worker.stderr.log
stdout_logfile_maxbytes=50MB
stdout_logfile_backups=3

[program:streambot_beat]
command=/opt/streambot/venv/bin/celery -A workers.celery_app beat
         --loglevel=info
directory=/opt/streambot
user=streambot
environment=PYTHONPATH="/opt/streambot"
autostart=true
autorestart=true
startretries=3
redirect_stderr=false
stdout_logfile=/var/log/streambot/beat.stdout.log
stderr_logfile=/var/log/streambot/beat.stderr.log
SUPERVISORD

step "Log Rotation"
cat > /etc/logrotate.d/streambot << 'LOGROTATE'
/var/log/streambot/*.log {
    daily
    rotate 7
    compress
    delaycompress
    missingok
    notifempty
    create 0640 streambot streambot
    sharedscripts
    postrotate
        supervisorctl signal HUP streambot:* > /dev/null 2>&1 || true
    endscript
}
LOGROTATE

step "Disk Space Monitoring Cron"
cat > /etc/cron.d/streambot-disk << 'CRON'
*/5 * * * * root df -h /data | awk 'NR==2 {if($5+0>90) print "DISK WARNING: "$5" used on /data"}' | logger -t streambot
CRON

step "Start Services"
systemctl enable --now supervisor
supervisorctl reread
supervisorctl update
supervisorctl start streambot:*

step "Health Check"
sleep 3
supervisorctl status streambot: || true

echo ""
echo -e "${GREEN}╔══════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║          StreamBot Setup Complete! 🎬                ║${NC}"
echo -e "${GREEN}╚══════════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "  📝 Edit config:   ${BOLD}nano /opt/streambot/.env${NC}"
echo -e "  🔄 Restart:       ${BOLD}supervisorctl restart streambot:*${NC}"
echo -e "  📊 Logs (bot):    ${BOLD}tail -f /var/log/streambot/bot.stdout.log${NC}"
echo -e "  📊 Logs (worker): ${BOLD}tail -f /var/log/streambot/worker.stdout.log${NC}"
echo -e "  🔍 Status:        ${BOLD}supervisorctl status${NC}"
echo ""
warn "Don't forget to configure /opt/streambot/.env with your bot token!"
