# tv-tips — Admin & Deployment Guide

## Overview

tv-tips is a self-hosted FastAPI application that runs on the same local network as your Enigma2 satellite receivers (OpenWebif required).
It collects viewing sessions, builds per-user preference profiles, and serves AI-ranked TV recommendations via a browser UI.

**Stack**: Python 3.11+ · FastAPI · SQLite · APScheduler · Ollama (local LLM)

---

## Requirements

| Component | Minimum | Notes |
|-----------|---------|-------|
| Python | 3.11 | `python3 --version` |
| Ollama | any | Running locally; model must be pulled |
| Enigma2 receivers | OpenWebif enabled | HTTP reachable on local network |
| Disk | ~200 MB | venv + SQLite DB |
| RAM | ~150 MB | Uvicorn + APScheduler |

The recommended Ollama model is **qwen3.5:9b** (≈6.6 GB VRAM).
Any model that can produce reliable JSON output works; larger models give better recommendations.

---

## Installation

```bash
git clone https://github.com/joba-1/tv-tips.git
cd tv-tips

python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

pip install -r requirements.txt
```

---

## Configuration

Copy the example and edit:

```bash
cp .env.example .env   # or create .env from scratch
```

### `.env` reference

```ini
# ── Receivers ────────────────────────────────────────────────────────────────
# Format: name:ip:default_user[|flag=value ...]
# Flags: priority=<int>   lower = preferred for EPG; default 99
#        has_genre=true   OWIF firmware exposes genre strings
#        wol_mac=<MAC>    enables Wake-on-LAN (power_method=wol)
#        power_method=wol|intertechno|none
RECEIVERS_RAW=box15:192.168.1.15:alice|priority=2|wol_mac=00:1d:ec:17:0e:a1|power_method=wol,\
              box17:192.168.1.17:bob|priority=1|has_genre=true|power_method=intertechno

# ── Users ────────────────────────────────────────────────────────────────────
# Format: slug:Display Name, comma-separated
USERS_RAW=alice:Alice,bob:Bob

# ── Ollama ───────────────────────────────────────────────────────────────────
OLLAMA_URL=http://localhost:11434
OLLAMA_MODEL=qwen3.5:9b

# ── Poller & EPG ─────────────────────────────────────────────────────────────
POLL_INTERVAL_SEC=45          # how often to check what each receiver is tuned to
MIN_WATCH_SEC=300             # minimum seconds before a session is confirmed
EPG_FULL_REFRESH_HOUR=3      # local hour for nightly full EPG refresh (cron)
EPG_RETENTION_DAYS=30        # how long to keep EPG events in the DB
TIMEZONE=Europe/Berlin

# ── Prime-time window ────────────────────────────────────────────────────────
PRIME_START_HOUR=20          # local hour (inclusive)
PRIME_END_HOUR=23            # local hour (exclusive)

# ── IntertechnoGateway (optional) ────────────────────────────────────────────
# Only needed if a receiver uses power_method=intertechno.
# The gateway is a small HTTP server that controls a 433 MHz RF switch.
INTERTECHNO_URL=http://intertechnogw   # or IP address
INTERTECHNO_FAMILY=A                   # RF family letter
INTERTECHNO_DEVICE=1                   # RF device number

# ── Misc ─────────────────────────────────────────────────────────────────────
DB_PATH=tv_tips.db
LOG_LEVEL=INFO                 # DEBUG | INFO | WARNING | ERROR
MOCK_RECEIVERS=false           # true → load fixtures from tests/fixtures/ instead of live HTTP
SSH_ENABLED=false              # reserved, unused
```

> **Security**: `.env` contains no credentials, but keep it out of version control.
> The repo's `.gitignore` already excludes it.

---

## Running

### Development

```bash
source .venv/bin/activate
uvicorn main:app --host 0.0.0.0 --port 8765 --reload
```

### Production (systemd)

Create `/etc/systemd/system/tv-tips.service`:

```ini
[Unit]
Description=tv-tips recommendation server
After=network.target

[Service]
Type=simple
User=joachim
WorkingDirectory=/home/joachim/git/tv-tips
ExecStart=/home/joachim/git/tv-tips/.venv/bin/uvicorn main:app --host 0.0.0.0 --port 8765
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now tv-tips
sudo systemctl status tv-tips
```

Logs:

```bash
journalctl -u tv-tips -f
```

---

## Receiver setup

### OpenWebif

Enable OpenWebif on each receiver:
- **VU+ (box15)**: Plugins → OpenWebif → Activate
- **SF8008 (box17)**: same path; OWIF 2.x exposes genre strings (`has_genre=true`)

Test reachability:

```bash
curl http://192.168.1.15/api/about
curl http://192.168.1.17/api/about
```

### Wake-on-LAN (WOL)

Requires the receiver to be configured for **light standby** (not deep standby).
On VU+: Setup → System → Standby/Restart → "Wakeup from standby" → enable.

WOL is triggered automatically when the Watch button is tapped and the receiver is in standby.

### IntertechnoGateway

The gateway controls a 433 MHz RF mains switch shared between the TV and a receiver.
Manual on/off is available via the Admin page → Wake / Sleep buttons.
**Automatic power scheduling is intentionally disabled** because the TV shares the circuit.

---

## Ollama setup

```bash
# Install Ollama (see https://ollama.com)
ollama pull qwen3.5:9b
ollama serve          # runs on http://localhost:11434 by default
```

Verify:

```bash
curl http://localhost:11434/api/tags | python3 -m json.tool
```

If Ollama is on a different machine, set `OLLAMA_URL=http://<host>:11434` in `.env`.
Recommendations fall back to a rule-based ranking if Ollama is unreachable.

---

## Database

SQLite file at `DB_PATH` (default `tv_tips.db`).
The schema is created automatically on first start.
No manual migrations are needed when upgrading — `init_db()` calls `create_all()`.

### Useful queries

```bash
sqlite3 tv_tips.db

# Confirmed viewing sessions per user
SELECT u.name, COUNT(*) FROM viewing_sessions vs
JOIN users u ON vs.user_id = u.id
WHERE vs.confirmed = 1 GROUP BY u.name;

# Translation cache
SELECT lang, COUNT(*), MAX(generated_at) FROM translations GROUP BY lang;

# Recommendation cache entries
SELECT user_id, prompt_hash, valid_until FROM recommendation_cache;
```

### Backup

```bash
sqlite3 tv_tips.db ".backup tv_tips_backup.db"
```

---

## Admin page

Open the app → **Admin** tab.

| Button | Action |
|--------|--------|
| **↺ All** | Refresh channels + EPG from all online receivers |
| **Channels** | Refresh channel list only |
| **Full EPG** | Fetch 24h EPG for every channel (slow, ~1 min) |
| **⚡ Wake** | Send WOL packet or toggle Intertechno switch on |
| **💤 Sleep** | Put receiver into light standby (WOL) or switch Intertechno off |

Scheduled jobs run automatically:
- EPG now/next: every hour
- Full EPG: nightly at `EPG_FULL_REFRESH_HOUR`:30 local time
- Channel list: every 24 hours
- EPG cleanup: daily at 04:00 (removes events older than `EPG_RETENTION_DAYS`)

---

## Internationalisation

UI strings are served from `static/i18n/de.json` (German, canonical) and `static/i18n/en.json` (English, curated).

For any other browser language, a one-shot Ollama batch translation is triggered the first time that language is seen.
Results are cached in the `translations` DB table.
The UI shows a low-profile banner while translation is in progress and switches automatically when done.

To add a hand-curated translation for a new language (e.g. French):

1. Copy `static/i18n/de.json` → `static/i18n/fr.json`
2. Translate the values (keep the keys unchanged)
3. Restart the server (the file is read on every request, no restart strictly needed)

Curated file entries always override AI-generated entries.

---

## API reference (brief)

All endpoints are on the same host/port.

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/recommendations?context=now\|next\|prime\|today` | AI-ranked recommendations for the current user (cookie) |
| GET | `/api/now-next` | Current + next programme per channel |
| GET | `/api/epg?hours=N` or `?context=tonight` | EPG range |
| GET | `/api/receivers` | Receiver list with live status |
| GET | `/api/users` | Configured user list |
| GET | `/api/i18n/{lang}` | UI string dict for a language |
| POST | `/api/remote/zap` | `{"sref":"..."}` — switch channel (auto-wake) |
| GET | `/api/admin/status` | Receiver status + DB counts |
| POST | `/api/admin/refresh?target=all\|channels\|epg\|epg_full` | Trigger data refresh |
| POST | `/api/admin/power?receiver=NAME&action=wake\|sleep` | Manual power control |

Interactive API docs: `http://<server>:8765/docs`

---

## Updating

```bash
cd /home/joachim/git/tv-tips
git pull
source .venv/bin/activate
pip install -r requirements.txt    # pick up any new deps
sudo systemctl restart tv-tips
```

Check the version: `cat VERSION`

---

## Troubleshooting

| Symptom | Check |
|---------|-------|
| Recommendations always rule-based | Is Ollama running? `curl $OLLAMA_URL/api/tags` |
| No channels in DB | Are receivers reachable? Check Admin → receiver badge colour |
| EPG stale banner | Run Admin → ↺ All; check logs for `epg.refresh_error` |
| Translation pending forever | Check logs for `i18n.batch_failed`; verify Ollama is up |
| Watch button returns 503 | No receiver is online; check power and network |
| Sessions not confirming | `MIN_WATCH_SEC` is 300 (5 min); watch a channel for that long |

Logs (structured JSON):

```bash
journalctl -u tv-tips -f | python3 -m json.tool
```

Or in development mode the logs print to stdout in human-readable format.

---

## Version history

| Version | Highlights |
|---------|-----------|
| 0.1.0 | Initial release — EPG, Now&Next, viewing session poller |
| 0.2.0 | Feature-based receiver config, generic power management |
| 0.3.0 | AI recommendation pipeline, recommendations landing page, zap endpoint |
| 0.4.0 | Internationalisation — browser language detection, AI batch translation, genre localisation |
