# tv-tipps — Admin & Deployment Guide

## Overview

tv-tipps is a self-hosted FastAPI application that runs on the same local network as your Enigma2 satellite receivers (OpenWebif required).
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

## Quick install (recommended)

```bash
git clone https://github.com/joba-1/tv-tipps.git
cd tv-tipps
sudo ./deploy.sh
```

The script installs the app, creates a Python venv, writes a config template, installs and starts a systemd service, and prints next steps.

### Options

```
sudo ./deploy.sh [--prefix DIR] [--port PORT] [--user USER]
```

| Option | Default | Description |
|--------|---------|-------------|
| `--prefix DIR` | `/usr/local` | Install root; app lands in `$PREFIX/lib/tv-tipps/` |
| `--port PORT` | `8844` | Listening port |
| `--user USER` | current sudo user | OS user that owns and runs the service |

Example:

```bash
sudo ./deploy.sh --prefix /opt --port 8765 --user myuser
```

### What the script does

1. Copies app files to `$PREFIX/lib/tv-tipps/` (rsync, skips `.git`, `.env`, DB files)
2. Creates a Python venv at `$PREFIX/lib/tv-tipps/.venv/` and installs `requirements.txt`
3. Creates `/var/lib/tv-tipps/` for the SQLite database
4. Creates `/etc/tv-tipps/env` (config file) — **written once, never overwritten on re-deploy**
5. Installs `/etc/systemd/system/tv-tipps.service` and starts the service

### After install

1. Edit `/etc/tv-tipps/env` — set your receiver IPs, usernames, and Ollama model
2. `sudo systemctl restart tv-tipps`
3. Open `http://<host>:8844/`

### Updating

```bash
cd tv-tipps
git pull
sudo ./deploy.sh   # same options as initial install
```

The deploy script is idempotent: it upgrades the app and venv but never touches your existing config file.

---

## Manual installation

```bash
git clone https://github.com/joba-1/tv-tipps.git
cd tv-tipps

python3 -m venv .venv
source .venv/bin/activate

pip install -r requirements.txt
cp /dev/null .env   # create empty, then edit
```

---

## Configuration

The config file is `/etc/tv-tipps/env` (deploy script) or `.env` in the working directory (manual).

### Full reference

```ini
# ── Receivers ────────────────────────────────────────────────────────────────
# Format: name:ip:default_user[|flag=value ...]
# Flags: priority=<int>   lower = preferred for EPG; default 99
#        has_genre=true   OWIF firmware exposes genre strings
#        wol_mac=<MAC>    enables Wake-on-LAN (power_method=wol)
#        power_method=wol|intertechno|none
#        location=<room>  human-readable label shown in watch toasts
RECEIVERS_RAW=box15:192.168.1.15:alice|priority=2|wol_mac=aa:bb:cc:dd:ee:ff|power_method=wol|location=Wohnzimmer,\
              box17:192.168.1.17:bob|priority=1|has_genre=true|power_method=intertechno|location=Schlafzimmer

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
SESSION_RETENTION_DAYS=90    # how long to keep viewing sessions (profile uses last 30 days)
TIMEZONE=Europe/Berlin

# ── Prime-time window ────────────────────────────────────────────────────────
PRIME_START_HOUR=20          # local hour (inclusive)
PRIME_END_HOUR=23            # local hour (exclusive)

# ── IntertechnoGateway (optional) ────────────────────────────────────────────
# Only needed if a receiver uses power_method=intertechno.
INTERTECHNO_URL=http://intertechnogw
INTERTECHNO_FAMILY=A
INTERTECHNO_DEVICE=1

# ── Misc ─────────────────────────────────────────────────────────────────────
DB_PATH=/var/lib/tv-tipps/tv_tipps.db
LOG_LEVEL=INFO
MOCK_RECEIVERS=false
SSH_ENABLED=false
```

> **Security**: the config file contains no credentials. Keep it readable only by the service user (`chmod 640`).

---

## Running

### Development

```bash
source .venv/bin/activate
uvicorn main:app --host 0.0.0.0 --port 8765 --reload
```

### Production (systemd — installed by deploy.sh)

```bash
sudo systemctl status tv-tipps
sudo systemctl restart tv-tipps
journalctl -u tv-tipps -f
```

Manual service file at `/etc/systemd/system/tv-tipps.service`:

```ini
[Unit]
Description=tv-tipps TV recommendation server
After=network.target

[Service]
Type=simple
User=myuser
WorkingDirectory=/usr/local/lib/tv-tipps
EnvironmentFile=/etc/tv-tipps/env
ExecStart=/usr/local/lib/tv-tipps/.venv/bin/uvicorn main:app --host 0.0.0.0 --port 8844
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
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

### Light standby (standby_newstate)

The Admin "Standby" action calls OpenWebif `/api/powerstate?newstate=N`. The correct
`N` for light standby differs by firmware: **VTi (Vu+) uses 4, openATV (Octagon)
uses 5**. Set it per receiver in the Admin page's edit form (default 4).

### Receiver location

Set a `location=` flag on each receiver (e.g. `location=Wohnzimmer`) so watch toasts say
"▶ Switched to Wohnzimmer" instead of the internal name.

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

If Ollama is on a different machine, set `OLLAMA_URL=http://<host>:11434` in the config.
Recommendations fall back to a rule-based ranking if Ollama is unreachable.

---

## Database

SQLite file at `DB_PATH` (default `/var/lib/tv-tipps/tv_tipps.db` when using deploy.sh).
The schema is created automatically on first start — no manual migrations needed.

### Useful queries

```bash
sqlite3 /var/lib/tv-tipps/tv_tipps.db

# Confirmed viewing sessions per user
SELECT u.name, COUNT(*) FROM viewing_sessions vs
JOIN users u ON vs.user_id = u.id
WHERE vs.confirmed = 1 GROUP BY u.name;

# Translation cache
SELECT lang, COUNT(*), MAX(generated_at) FROM translations GROUP BY lang;

# Per-user event match scores (drive the Tipps page)
SELECT user_id, source, COUNT(*) FROM user_event_scores GROUP BY user_id, source;

# Liked events per user
SELECT u.name, COUNT(*) FROM user_likes l
JOIN users u ON l.user_id = u.id GROUP BY u.name;
```

### Backup

```bash
sqlite3 /var/lib/tv-tipps/tv_tipps.db ".backup tv_tipps_backup.db"
```

---

## Admin page

Open the app → **⚙ Admin** tab.

| Button | Action |
|--------|--------|
| **↺ All** | Refresh channels + EPG from all online receivers |
| **Channels** | Refresh channel list only |
| **Full EPG** | Fetch 24h EPG for every channel (slow, ~1 min) |
| **⚡ Wake** | Send WOL packet or toggle Intertechno switch on |
| **💤 Sleep** | Put receiver into light standby (WOL) or switch Intertechno off |
| **Preferences** | Edit per-user stated preferences (bypasses cold-start gate) |

Scheduled jobs run automatically:
- EPG now/next: every hour
- Full EPG: nightly at `EPG_FULL_REFRESH_HOUR`:30 local time
- Channel list: every 24 hours
- EPG cleanup: daily at 04:00 (removes viewing sessions older than `SESSION_RETENTION_DAYS`, then events older than `EPG_RETENTION_DAYS` and overlapping future duplicates)

---

## Internationalisation

UI strings are served from `static/i18n/de.json` (German, canonical) and `static/i18n/en.json` (English, curated).

For any other browser language, a one-shot Ollama batch translation is triggered the first time that language is seen.
Results are cached in the `translations` DB table.
The UI shows a low-profile banner while translation is in progress and switches automatically when done.

To add a hand-curated translation for a new language (e.g. French):

1. Copy `static/i18n/de.json` → `static/i18n/fr.json`
2. Translate the values (keep the keys unchanged)
3. No restart needed — the file is read on every request

Curated file entries always override AI-generated entries.

---

## Running tests

```bash
source .venv/bin/activate
pytest tests/ -q
```

Tests use an in-memory SQLite database. No receivers or Ollama instance is required.

Test coverage:
- `test_parser.py` — EPG/channel fixture parsing
- `test_timezones.py` — range functions (prime, tonight, today)
- `test_i18n.py` — language normalisation, translation merge priority
- `test_recommendations.py` — rule-based ranking, cold-start gate, LLM index parsing
- `test_profile.py` — profile computation, caching, stated preferences
- `test_epg.py` — EPG range queries (future_only semantics), cleanup

---

## API reference (brief)

All endpoints are on the same host/port.

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/recommendations?context=now\|next\|prime\|today` | AI-ranked recommendations for the current user (cookie) |
| GET | `/api/now-next` | Current + next programme per channel |
| GET | `/api/epg?hours=N` or `?context=tonight` | EPG range |
| GET | `/api/epg/search?q=...&days=7` | Full-text EPG search |
| GET | `/api/receivers` | Receiver list with live status |
| GET | `/api/users` | Configured user list |
| GET | `/api/i18n/{lang}` | UI string dict for a language |
| POST | `/api/remote/zap` | `{"sref":"..."}` — switch channel (auto-wake) |
| POST | `/api/likes/toggle` | Toggle like on an EPG event |
| GET | `/api/likes` | Current user's liked events |
| GET | `/api/admin/status` | Receiver status + DB counts + app version |
| POST | `/api/admin/refresh?target=all\|channels\|epg\|epg_full` | Trigger data refresh |
| POST | `/api/admin/power?receiver=NAME&action=wake\|sleep` | Manual power control |
| GET | `/api/admin/user-preferences?user=slug` | Get stated preferences |
| POST | `/api/admin/user-preferences?user=slug` | Set stated preferences |

Interactive API docs: `http://<server>:<port>/docs`

---

## Updating

```bash
cd tv-tipps
git pull
sudo ./deploy.sh   # re-runs with same options; never overwrites config
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
| Save button stays grey in Admin | No changes detected yet — edit the text area first |

Logs (structured JSON):

```bash
journalctl -u tv-tipps -f | python3 -m json.tool
```

---

## Version history

| Version | Highlights |
|---------|-----------|
| 0.1.0 | Initial release — EPG, Now&Next, viewing session poller |
| 0.2.0 | Feature-based receiver config, generic power management |
| 0.3.0 | AI recommendation pipeline, recommendations landing page, zap endpoint |
| 0.4.0 | Internationalisation — browser language detection, AI batch translation, genre localisation |
| 0.5.0 | AI recommendation improvements: short_desc in prompt, likes signal, stated preferences bypass cold-start |
| 1.0.0 | Like button, EPG search, admin preferences UI, receiver location in toasts |
| 1.0.1 | Nav icons, watch toast shows room name, admin Save button disabled when unchanged; deploy.sh; full test suite |
| 2.3.0 | Reliability + UX batch: nightly cron jobs actually fire (03:30 sweep, 04:15 rerate), non-blocking startup, scoring queue with now/next ingest scoring, overlapping same-channel events unified, "today" context ends 04:00, 90-day session retention, dead recs pipeline removed, standby_newstate editable in admin, calmer UI refresh without scroll jumps |
