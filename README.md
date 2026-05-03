# 🎬 StreamBot — Production M3U8 Recording Telegram Bot

A production-ready, scalable Telegram bot that records M3U8 HLS live streams using FFmpeg, built with Python 3.11, Celery, Redis, and python-telegram-bot v20+.

---

## 🏗 Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        TELEGRAM USERS                           │
└──────────────────────────────┬──────────────────────────────────┘
                               │ HTTPS
                               ▼
┌─────────────────────────────────────────────────────────────────┐
│                    BOT LAYER  (bot/)                            │
│  ┌─────────────┐  ┌──────────────┐  ┌──────────────────────┐   │
│  │  handlers   │  │   command_   │  │    formatters /      │   │
│  │  .py        │  │   parser.py  │  │    keyboards.py      │   │
│  └──────┬──────┘  └──────┬───────┘  └──────────────────────┘   │
│         │                │                                       │
│  ┌──────▼──────────────────────────────────────────────────┐    │
│  │              middleware.py  (rate limiting)              │    │
│  └──────────────────────────────────────────────────────────┘   │
└──────────────────────────────┬──────────────────────────────────┘
                               │ Celery Tasks
                               ▼
┌─────────────────────────────────────────────────────────────────┐
│                   JOB QUEUE  (Redis + Celery)                   │
│  ┌─────────────────┐  ┌─────────────────────────────────────┐   │
│  │  normal queue   │  │       priority queue (VIP)          │   │
│  └────────┬────────┘  └────────────────┬────────────────────┘   │
└───────────┼────────────────────────────┼────────────────────────┘
            │                            │
            ▼                            ▼
┌─────────────────────────────────────────────────────────────────┐
│                  WORKER LAYER  (workers/)                       │
│  ┌─────────────────────────────────────────────────────────┐    │
│  │              recording_worker.py  (Celery Task)         │    │
│  │  1. ffprobe stream  →  2. launch FFmpeg  →  3. monitor  │    │
│  └────────────────────────┬────────────────────────────────┘    │
│                            │                                     │
│  ┌─────────────────────────▼──────────────────────────────┐     │
│  │              core/ffmpeg_engine.py                     │     │
│  │   subprocess | SIGSTOP/SIGCONT | stderr parser         │     │
│  └─────────────────────────┬──────────────────────────────┘     │
└────────────────────────────┼────────────────────────────────────┘
                             │
            ┌────────────────┼────────────────┐
            ▼                ▼                ▼
┌─────────────────┐  ┌────────────┐  ┌───────────────┐
│  Local Storage  │  │   GDrive   │  │ Telegram Chan  │
│  /data/user_N/  │  │  (optional)│  │  (optional)   │
└─────────────────┘  └────────────┘  └───────────────┘

STATE PERSISTENCE: Redis (Job objects as JSON)
PERIODIC TASKS:   Celery Beat (cleanup, watchdog)
MONITORING:       FastAPI Dashboard (optional)
```

---

## 📁 Project Structure

```
streambot/
├── bot/
│   ├── main.py              # Entry point, graceful shutdown
│   ├── handlers.py          # All /commands + callbacks
│   ├── command_parser.py    # NLP → RecordingOptions
│   ├── formatters.py        # Job → Telegram markdown
│   ├── keyboards.py         # Inline keyboards
│   └── middleware.py        # Rate limiting, quota checks
├── core/
│   ├── models.py            # Job, StreamInfo, state machine
│   ├── job_manager.py       # Redis CRUD (sync + async)
│   ├── ffmpeg_engine.py     # FFmpeg subprocess wrapper
│   └── stream_analyzer.py   # ffprobe + quality selector
├── workers/
│   ├── celery_app.py        # Celery config, queues, beat schedule
│   └── recording_worker.py  # Main recording task + control tasks
├── storage/
│   ├── local_storage.py     # File listing/deletion
│   ├── gdrive_storage.py    # Google Drive upload
│   └── cleanup_manager.py   # Retention policy + emergency cleanup
├── monitoring/
│   ├── logger.py            # Structlog JSON/console setup
│   └── metrics.py           # Disk/CPU/memory stats
├── api/
│   └── dashboard.py         # FastAPI admin dashboard
├── config/
│   └── settings.py          # Pydantic settings from .env
├── docker/
│   ├── Dockerfile
│   └── docker-compose.yml
├── scripts/
│   └── setup.sh             # VPS installer
├── .env.example
└── requirements.txt
```

---

## ⚡ Quick Start

### Option A — Docker (Recommended)

```bash
# 1. Clone and configure
git clone https://github.com/you/streambot.git
cd streambot
cp .env.example .env
nano .env   # set TELEGRAM_BOT_TOKEN at minimum

# 2. Create storage directory
mkdir -p /opt/streambot/recordings

# 3. Start everything
docker compose -f docker/docker-compose.yml up -d

# 4. View logs
docker compose -f docker/docker-compose.yml logs -f bot
```

### Option B — Bare Metal VPS (Ubuntu 22.04)

```bash
# 1. Clone repo
git clone https://github.com/you/streambot.git /opt/streambot-src

# 2. Run installer (as root)
sudo bash /opt/streambot-src/scripts/setup.sh

# 3. Edit config
sudo nano /opt/streambot/.env

# 4. Restart services
sudo supervisorctl restart streambot:*
```

### Option C — Manual / Development

```bash
# 1. System deps
sudo apt install ffmpeg redis-server python3.11 python3.11-venv

# 2. Virtual env
python3.11 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# 3. Configure
cp .env.example .env && nano .env

# 4. Start Redis
redis-server --daemonize yes

# 5. Start worker (terminal 1)
PYTHONPATH=. celery -A workers.celery_app worker \
    --loglevel=info --queues=normal,priority --concurrency=4

# 6. Start beat scheduler (terminal 2)
PYTHONPATH=. celery -A workers.celery_app beat --loglevel=info

# 7. Start bot (terminal 3)
PYTHONPATH=. python -m bot.main
```

---

## 🤖 Bot Commands

| Command | Description |
|---|---|
| `/record <url>` | Start recording an M3U8 stream |
| `/record <url> 2h high` | Record for 2 hours at high quality |
| `/stop <JOB_ID>` | Stop a recording gracefully |
| `/pause <JOB_ID>` | Pause a recording (SIGSTOP) |
| `/resume <JOB_ID>` | Resume a paused recording (SIGCONT) |
| `/status <JOB_ID>` | Show detailed job status |
| `/list` | List all your recordings |
| `/help` | Show help message |

### Natural Language

The bot understands plain English:
- _"record https://stream.example.com/live.m3u8 for 2 hours high quality"_
- _"start recording https://... audio only"_
- _"stop A3F2B1C9"_

---

## ⚙️ Configuration Reference

See `.env.example` for all options with descriptions. Key settings:

| Variable | Default | Description |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | **required** | From @BotFather |
| `TELEGRAM_ADMIN_IDS` | `[]` | User IDs with admin access |
| `REDIS_URL` | `redis://localhost:6379/0` | Redis connection |
| `LOCAL_STORAGE_PATH` | `/data` | Where recordings are saved |
| `MAX_DISK_USAGE_GB` | `50` | Max disk space for recordings |
| `FILE_RETENTION_HOURS` | `72` | Auto-delete after N hours |
| `MAX_CONCURRENT_JOBS_PER_USER` | `3` | Job quota per user |
| `FFMPEG_DEFAULT_TIMEOUT` | `7200` | Max recording duration (s) |
| `STORAGE_BACKEND` | `local` | `local` \| `gdrive` \| `telegram` |
| `DASHBOARD_ENABLED` | `false` | Enable FastAPI dashboard |

---

## 🛡 Security & Reliability

| Feature | Implementation |
|---|---|
| User isolation | Each user gets `/data/user_<id>/` |
| Rate limiting | Redis sliding window (20 msg/60s) |
| Job quotas | 3 concurrent per user (6 for VIP) |
| Crash recovery | Celery retries up to 3× with backoff |
| Watchdog | Periodic task detects orphaned FFmpeg processes |
| Graceful stop | Sends `q` to FFmpeg → SIGTERM → SIGKILL |
| Disk protection | Warns at 80%, emergency cleanup at 95% |
| State machine | Invalid job transitions are rejected |
| Memory safety | `worker_max_tasks_per_child=50` recycles Celery workers |

---

## 📊 Dashboard API

When `DASHBOARD_ENABLED=true`, a REST API runs on port 8080:

```bash
# Metrics
curl -H "Authorization: Bearer $DASHBOARD_SECRET" \
     http://localhost:8080/metrics

# All jobs
curl -H "Authorization: Bearer $DASHBOARD_SECRET" \
     http://localhost:8080/jobs

# Force stop a job
curl -X POST -H "Authorization: Bearer $DASHBOARD_SECRET" \
     http://localhost:8080/jobs/A3F2B1C9/stop

# Swagger UI
open http://localhost:8080/docs
```

---

## 🔧 FFmpeg Strategy

| Concern | Solution |
|---|---|
| Broken HLS streams | `-reconnect 1 -reconnect_at_eof 1 -reconnect_streamed 1` |
| Socket timeout | `-timeout 30000000` (30s) |
| Buffering | `-buffer_size 32M -thread_queue_size 4096` |
| Crash recovery | Celery retry with backoff |
| Long recordings | Segmented output (`%03d.mp4`) |
| Quality options | Copy (no re-encode) / libx264 CRF / scale |
| AV sync | `-async 1 -vsync 1` |

---

## 🐳 Scaling

```bash
# Scale to 8 workers (each can run 4 concurrent FFmpeg processes)
docker compose -f docker/docker-compose.yml up -d --scale worker=8

# With priority queue separation
celery -A workers.celery_app worker --queues=priority --concurrency=2 &
celery -A workers.celery_app worker --queues=normal   --concurrency=6 &
```

---

## 📝 Logs

```bash
# Bot logs
tail -f /var/log/streambot/bot.stdout.log

# Worker logs  
tail -f /var/log/streambot/worker.stdout.log

# Celery task inspect
celery -A workers.celery_app inspect active
celery -A workers.celery_app inspect stats
```

---

## 🧪 Testing a Stream

```bash
# Verify your stream works with ffprobe before bot testing
ffprobe -v quiet -print_format json -show_streams \
    "https://your-stream.example.com/live.m3u8"

# Quick 10-second local test
ffmpeg -i "https://your-stream.example.com/live.m3u8" \
    -t 10 -c copy /tmp/test.mp4 && echo "Stream OK"
```
