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
pytest tests/test_scoring.py -q   # focused subset (score pipeline)
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

- Model is configured in `OLLAMA_MODEL`; current default in this deployment is `qwen3.6:latest`. Any model that reliably emits JSON works.
- Pull/check: `ollama pull qwen3.5:9b` · `curl -s $OLLAMA_URL/api/tags | jq '.models[].name'`.
- Recs path (score-backed, since 2.3): EPG ingest enqueues events → a single `scoring_worker` batches them to the LLM → per-(user, event) rows in `user_event_scores`. `/api/recommendations` is pure SQL over those rows per context window; unscored events get an inline rule score plus a background enqueue, and `regenerating: true` triggers the client's fast-poll. There is no LLM call on the request path.
- Keep the rule-based fallback (`_rule_score`) wired in — Ollama being down must degrade to rule-sourced rows (upgraded later by `ai_availability_watcher`), never to an empty Tipps list.
- Translations: one-shot batch per new browser language, cached in the `translations` table. Curated `static/i18n/<lang>.json` always wins over AI entries.
- If Ollama is unreachable the app logs `recs.llm_unavailable` / `i18n.batch_failed` and degrades gracefully — don't add retries or hard failures around it.
- **Thinking models** (e.g. `qwen3.5:9b`) emit their JSON into the `thinking` field with `response=""` when `format=json` is set. `ask_json` falls back to `thinking` automatically — verify by checking `ollama.ok` events fire (not `ollama.parse_failed`).
- **Scoring prompt shape**: candidate text is the synopsis (`short_desc + long_desc`, whitespace-collapsed) capped at `_CAND_DESC_MAX_CHARS = 600`, **plus the credits block** (`_CAND_CREDITS_MAX_CHARS = 600`, `_CREDITS_RE`), so a long synopsis never costs the model the director or the cast — 12.6 % of upcoming events carry such a block and 5.5 % of those used to start beyond the cap. Likes/dislikes/history each carry a compact `Regie: … Darsteller: …` line (`_credits_line`, priority `_CREDITS_PRIORITY`, 120 chars) resolved via their `epg_event_id`; the prompt tells the model to name a matching person in the `reason`. The user prefix carries up to `_REACTION_LIMIT = 200` likes/dislikes and `_HISTORY_LIMIT = 70` sessions; both were raised from 52/40 on 2026-09-18 because the old caps silently dropped the longest-standing signals. Whole batch ≈ 20.5k prompt + ~3.5k output tokens, ~45 s.
- **Response attribution**: every entry in the scoring answer carries an `index` (the candidate's LISTE number), and the schema *requires* it, because `minItems == maxItems` fixes only the entry *count* — not which candidate an entry is about. A model that answers one candidate twice and skips another satisfies the grammar while shifting every later score onto the wrong event, silently and with the reasons still plausible-looking. `_align_to_chunk` decides the mapping: exactly `1..n` → sort by index (an out-of-order answer becomes correct instead of corrupt); no index at all, or the same index in every entry (a model filling the field with a constant) → positional matching, same as before the field existed; anything in between (duplicated, out of range, partially missing) → unattributable → the halve/retry path a count mismatch already took. Never make the inconsistent case fall back to *rule* scores for a large chunk: a positional LLM score is worth more than a heuristic one, which is why the constant case is positional. `scoring.index_absent` / `index_constant` / `index_shuffled` / `index_mismatch` log what the model actually does with the field.
- **LLM window**: `LLM_WINDOW_START_HOUR`/`LLM_WINDOW_END_HOUR` in `/etc/tv-tipps/env` (job6: 3/6). Outside it `_defer_to_llm_window` lets only events starting before the window opens reach the LLM; later ones get a `rule` row (none yet) or keep their stale row. Inside the window the watcher, the 03:30 sweep and the 04:15 catch-all do the rest. `rerate-window` is gated the same way, so in the evening it only re-rates what airs before 03:00.
- **On-demand re-rate**: `POST /api/admin/rerate-window?hours=4[&user=slug]` re-rates the programmes airing now or starting within `hours`, as a background task *inside the app process* — deliberately, so it shares the per-process Ollama semaphore; a second process would let Ollama (-np 1) start a second copy of the model. It marks the rows stale first and re-rates the window, leaving the rest to the 04:15 `daily_rerate_stale` catch-all, so a restart mid-run degrades to "stale until tonight" rather than a lost run. Stale rows are still served by `/api/recommendations` (only the pending-count/ETA queries filter `stale == False`).
- **Determinism**: `_OPTIONS` pins `seed = 42`, so the same batch scores the same way twice (verified). Without it, two runs over identical input averaged 0.20 vs 0.39 — more spread than any prompt change we measured. Changing the prompt or the batch composition still changes the scores; that is expected and is what the stale/re-rate path is for.
- **Context-usage monitoring**: every Ollama call logs `ollama.usage` with `caller`, `prompt_tokens`, `completion_tokens`, `num_ctx`, `ctx_used_pct` (num_ctx mirrors the server-wide `OLLAMA_CONTEXT_LENGTH`; it is never sent as a request option — a differing value would reload the shared model for every client). Per-caller running min/avg/max/sum are exposed at `GET /api/admin/ollama-stats`; reset with `POST /api/admin/ollama-stats/reset`. Callers in use: `recs`, `i18n`. Stats reset on process restart.

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
