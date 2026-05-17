# AGENTS.md — operating cheatsheet for tv-tipps

This file is loaded automatically by Claude Code. Keep it short, factual, command-first.
Authoritative user/admin docs live in `docs/user-guide.md` and `docs/deploy.md` — update them whenever behaviour or flags change.

## Project shape

- FastAPI + SQLAlchemy + SQLite + APScheduler backend, Alpine.js + Pico CSS frontend.
- Talks to Enigma2 receivers via OpenWebif; uses Ollama (default `qwen3.5:9b`) for ranking/translation with rule-based fallback.
- Repo: `https://github.com/joba-1/tv-tipps` (public). Service unit name: `tv-tipps.service`. Working dir: `/usr/local/lib/tv-tipps`. Data dir: `/var/lib/tv-tipps` (DB `tv_tipps.db`). Config: `/etc/tv-tipps/env`.

## Versioning, commit, push

- `VERSION` is the single source of truth (semver `MAJOR.MINOR.PATCH`).
- `.git/hooks/pre-commit` auto-bumps the **patch** and stages `VERSION` — never bump patch by hand.
- For a feature bump, edit `VERSION` to the new `MINOR.0` *before* committing (hook will move it to `.1`); same trick for `MAJOR.0.0`. Use a `feat!:` / `BREAKING CHANGE:` footer for majors.
- Conventional commits: `feat:`, `fix:`, `refactor:`, `docs:`, `test:`, `chore:`. Scopes seen in history: `ui`, `power`, `zap`, `recs`, `epg`, `i18n`.
- Push only when the user asks. Never `--no-verify`, never amend after a hook failure (the commit didn't happen — fix and recommit).

## Tests

```bash
source .venv/bin/activate
pytest tests/ -q                  # unit + light integration; in-memory SQLite, no receivers/Ollama needed
pytest tests/test_recommendations.py -q   # focused subset
```

System smoke (against the running service):

```bash
curl -s http://localhost:8844/api/admin/status | python3 -m json.tool
curl -s 'http://localhost:8844/api/recommendations?context=now' -H 'cookie: user=joachim' | jq '.recommendations|length'
curl -s http://localhost:8844/api/receivers | jq '.[].online'
```

Run tests + a quick curl before declaring backend changes done. UI changes need a browser check — say so explicitly if you can't run one.

## Database

- Path: `/var/lib/tv-tipps/tv_tipps.db` (prod), `./tv_tips.db` (dev — legacy name from before the rename, leave as-is unless migrating).
- Schema is created on startup; no Alembic, no manual migrations.
- Backup before risky changes:
  ```bash
  sqlite3 /var/lib/tv-tipps/tv_tipps.db ".backup /var/lib/tv-tipps/tv_tipps.$(date +%F).db"
  ```
- Common ad-hoc queries are in `docs/deploy.md` under "Database". Receiver config edits go through the Admin UI; only touch the `receivers` table directly when the UI can't express it (e.g. intertechno family fix: `UPDATE receivers SET intertechno_family='C' WHERE name='octagon';`).
- Stop the service before writing to the DB from outside the app: `sudo systemctl stop tv-tipps && … && sudo systemctl start tv-tipps`.

## Deploy / undeploy

```bash
sudo ./deploy.sh                                  # idempotent install/upgrade; never overwrites /etc/tv-tipps/env
sudo ./deploy.sh --prefix /opt --port 8765 --user me
sudo ./undeploy.sh                                # remove tv-tipps; keeps DB
sudo ./undeploy.sh --purge-db                     # also remove /var/lib/tv-tipps
sudo ./undeploy.sh --legacy --purge-db            # remove pre-rename tv-tips install
```

After deploy:

```bash
sudo systemctl restart tv-tipps
sudo systemctl status tv-tipps
```

## Logs

Logs are structured JSON via stdlib logging → journald.

```bash
journalctl -u tv-tipps -f                                       # live tail
journalctl -u tv-tipps -n 200 --no-pager
journalctl -u tv-tipps --since '10 min ago' -o cat | jq .       # JSON pretty
journalctl -u tv-tipps -o cat | jq 'select(.level=="ERROR")'
journalctl -u tv-tipps -o cat | jq 'select(.event|startswith("recs."))'
```

Useful event prefixes when diagnosing: `recs.*`, `epg.*`, `power.*`, `remote.*`, `i18n.*`, `poller.*`. `level` is `INFO|WARNING|ERROR`.

## Local dev loop

```bash
source .venv/bin/activate
uvicorn main:app --reload --host 0.0.0.0 --port 8765
```

`.env` in cwd is read in dev; `/etc/tv-tipps/env` in prod (set by the systemd unit's `EnvironmentFile`).

## Ollama / AI tips

- Model is configured in `OLLAMA_MODEL`; current default in this deployment is `qwen3.5:9b`. Any model that reliably emits JSON works.
- Pull/check: `ollama pull qwen3.5:9b` · `curl -s $OLLAMA_URL/api/tags | jq '.models[].name'`.
- Recs path: cached JSON (stale-while-revalidate) → `_realtime_adjust_now` filters expired items → rule-based fallback if Ollama is down or the cache drains. `regenerating: true` in the response triggers the client's fast-poll.
- Cold-cache drain (every item expired) is a real production failure mode — keep the rule-based fallback wired in; don't return an empty list from `get_recommendations` for `context=now`.
- Translations: one-shot batch per new browser language, cached in the `translations` table. Curated `static/i18n/<lang>.json` always wins over AI entries.
- If Ollama is unreachable the app logs `recs.llm_unavailable` / `i18n.batch_failed` and degrades gracefully — don't add retries or hard failures around it.
- **Thinking models** (e.g. `qwen3.5:9b`) emit their JSON into the `thinking` field with `response=""` when `format=json` is set. `ask_json` falls back to `thinking` automatically — verify by checking `ollama.ok` events fire (not `ollama.parse_failed`).
- **Context-usage monitoring**: every Ollama call logs `ollama.usage` with `caller`, `prompt_tokens`, `completion_tokens`, `num_ctx`, `ctx_used_pct`. Per-caller running min/avg/max/sum are exposed at `GET /api/admin/ollama-stats`; reset with `POST /api/admin/ollama-stats/reset`. Callers in use: `recs`, `i18n`. Stats reset on process restart.

## Receiver power & remote

- Power methods per receiver: `wol`, `intertechno`, `none`. WOL needs `wol_mac=`; intertechno needs `intertechno_family=` + `intertechno_device=`.
- IntertechnoGateway protocol (joba-1/IntertechnoGateway): two POSTs to `/change` — `button=button-<a..d>` then `button=button-<1..3>-<on|off>`. **HTTP 302 = success** (treat `<400` as ok). Only devices 1–3 work over HTTP.
- Wake wait window in `app/routers/remote.py`: 45s for WOL, 150s for intertechno (cold-boot takes a minute+).
- Vu+ box deep-sleep nuisance: `~/bin/vu-autoshutdown [show|on|off]` toggles the VTi AutoShutdown plugin via SSH (kills enigma2 so the wrapper respawns and doesn't overwrite settings on graceful exit).

## Documentation hygiene

When you change behaviour, flags, endpoints, or commands, update the matching doc in the **same commit**:

- User-visible UI/flow change → `docs/user-guide.md`
- Config, deploy, admin, API, troubleshooting → `docs/deploy.md` (plus the "Version history" table for releases)
- Operating/dev commands or new tooling → this file
- README is intentionally thin; let it point to the docs.

## Safety reminders (project-specific)

- Never commit secrets. `/etc/tv-tipps/env` and real MAC/IP values must not appear in the public repo — scrub before pushing.
- Don't `systemctl stop tv-tipps` on the user's box without saying so; recs/EPG poller misses windows while it's down.
- Editing `/etc/enigma2/settings` on a Vu+ requires stopping enigma2 first or the change is overwritten on graceful exit. BusyBox: no `cp -n`, no `init`, no sftp-server — use `ssh host "cat …" > local` and `killall enigma2`.
