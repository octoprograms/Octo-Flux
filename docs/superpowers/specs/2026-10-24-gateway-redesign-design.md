# OctoFlux gateway redesign — design

Date: 2026-10-24 · Status: approved in conversation, awaiting written-spec review

## Goal

Make OctoFlux get more out of free-tier keys, pick strong free OpenRouter models on its own,
run comfortably on a 512 MB server, and be fully manageable by a small team from a
Quantix-style dashboard.

## Context and constraints

- Users of the dashboard: the owner plus a few trusted teammates (not outside clients).
- Dashboard edits are saved back to `config/OctoFlux.yaml`; the YAML stays the single source of truth.
- Target host: ≤ 512 MB RAM. OctoFlux must stay under **80 MB RSS** in the Docker image.
  Measured today on Windows: worker 37 MB working set / 60 MB private, plus a 20 MB `--reload` watcher.
- Dashboard is plain HTML/CSS/JS served by FastAPI. No framework, no build step.
- Each provider key belongs to a separate upstream account (including all three OpenRouter keys),
  so upstream rate limits are per key.
- Already done before this spec (uncommitted): token counts are recorded after successful
  requests; streaming goes through `Scheduler`; streaming errors no longer echo upstream bodies.

## Build order

1. Admin auth + config API (section 1) — everything else is managed through it.
2. Key rotation (section 2).
3. Free OpenRouter models (section 3).
4. Low RAM (section 4).
5. Dashboard (section 5).

Each step leaves the test suite green and the server runnable.

---

## 1. Admin auth and config API

### Admin auth

- New config field `server.admin_keys: list[{name: str, value: str}]`. `value` may be a literal or `${VAR}`.
- New dependency `require_admin_auth`: `Authorization: Bearer <admin key>`, compared with
  `hmac.compare_digest` against every admin key. Sets `request.state.admin_name`.
- All `/admin/*` API routes and `/metrics` use `require_admin_auth`. Client keys work only on `/v1/*`.
  The static dashboard files themselves stay public (they hold no data).
- If `admin_keys` is empty, admin API routes return **403** `{"error": {"type": "admin_disabled",
  "message": "No admin keys configured (server.admin_keys)."}}`.
- `server.require_auth: false` keeps disabling *client* auth only; it never disables admin auth.
- Public `GET /health` returns only `{"status": "ok"}`. The per-provider/per-key detail moves to
  `GET /admin/health`.
- Every config save logs `config_saved` with `admin=<name>`.
- Client-key comparison in `require_client_auth` also switches to `hmac.compare_digest`.
- README documents the breaking change (client keys no longer open `/admin`).

### Config API

Works on the **raw** YAML (before `${VAR}` resolution), never on the resolved in-memory config,
so secrets from the environment are never written to disk.

`GET /admin/config` →
```json
{ "version": "<sha256 of file bytes>", "config": { ...raw YAML as JSON... } }
```
- Every literal secret (`providers.*.keys[].value`, `server.client_keys[]`, `server.admin_keys[].value`)
  is replaced by a mask `"•••• <last 4>"`. Values that are `${VAR}` references are returned as-is.

`PUT /admin/config` with `{ "version": "...", "config": {...} }`:
1. Read the file; if its hash ≠ `version` → **409** `config_conflict`.
2. Unmask: any secret still equal to its mask is replaced with the stored raw value at the same
   identity (provider id + key name; client key position; admin key name). A mask with no match → **422**.
3. Validate: resolve `${VAR}` on a copy and run `OctoFluxConfig.model_validate`. Errors → **422**
   with Pydantic's error list (field path + message).
4. If the result has no admin keys → **422** `would_lock_out`.
5. Copy the current file to `config/backups/OctoFlux.<UTC timestamp>.yaml`; keep the newest 20.
6. Write to `OctoFlux.yaml.tmp` and `os.replace` it over the original.
7. Hot-reload through the existing reload path (`AppState.replace_config`), which keeps
   health/limits/usage state. Return `{ "version": "<new hash>" }`.

Saves are serialized with an `asyncio.Lock`. YAML is written with `yaml.safe_dump(sort_keys=False)`;
**comments in the file are lost on the first dashboard save** (the backup keeps the original).

`GET /admin/config/backups` lists backups (name, time, size). `POST /admin/config/backups/{name}/restore`
runs the same steps 3–7 using the backup's content (backup name is validated against the listing,
never used as a raw path).

`POST /admin/reload` stays, now admin-only.

### Tests
Admin vs client key access; 403 when no admin keys; masked secret round-trip keeps the real value;
`${VAR}` preserved; 409 on stale version; 422 on invalid config and on lock-out; backup written and
pruned to 20; restore; `/health` exposes nothing beyond status.

---

## 2. Key rotation

### Limits per key
- A provider's `limits` now apply **to each key** of that provider (matches free-tier reality).
  A key's own `limits` still override field-by-field.
- New optional provider field `total_limits` (same shape as `limits`) caps all keys combined.
- Router check per candidate key: key budget (`limits` merged with key overrides) and, if set,
  provider `total_limits`.

### Upstream rate-limit headers
- After every upstream response (success or failure) parse, case-insensitively:
  `x-ratelimit-remaining-requests`, `x-ratelimit-remaining-tokens`,
  `x-ratelimit-reset-requests`, `x-ratelimit-reset-tokens` (Groq/OpenAI style, durations like `2m59.5s`, `7.66s`, `120ms`),
  and `x-ratelimit-remaining` / `x-ratelimit-reset` (OpenRouter style; reset as epoch milliseconds).
- Stored on the key's health record: `remaining_requests`, `remaining_tokens`, `resets_at`.
- When remaining requests (or tokens) reaches 0, cool the key down until the reset time — before
  it ever returns a 429. Unknown/garbled headers are ignored.

### `smart` key selection (new default)
- Headroom of a key = the smallest of: local remaining fraction for each configured window, and
  upstream remaining fraction when known (remaining ÷ the largest remaining seen for that key in the window).
- Keys are ordered by headroom, highest first; ties broken by round-robin.
- `round_robin`, `least_used`, `priority` remain available. `routing.default_key_selection`
  defaults to `smart`; providers without an explicit `key_selection` use the routing default.

### Token budgets enforced
- Estimate per request: `ceil(total characters of message contents / 4) + (max_tokens or max_completion_tokens or 0)`.
- Router skips keys/providers where `has_token_budget(estimate)` is false
  (excluded reason: "exhausted its local token limit").
- Actual tokens (from the upstream `usage`) are recorded after the request, as today.

### Counters survive restarts
- Limit counters are saved to the existing `usage.db` (new table `limit_counters`) every 60 s and on
  shutdown, loaded at startup, keyed by (provider, key, window). Counters for keys that no longer
  exist are dropped on load.

### Tests
Two keys double capacity; `total_limits` caps combined; remaining=0 header cools the key until reset;
Groq duration and OpenRouter epoch formats parse; `smart` prefers the key with most headroom;
oversize request skips the key; counters restored after rebuild.

---

## 3. Free OpenRouter models

### Selection
- For `auto` + chat, OpenRouter candidates come from discovery (the existing `dynamic_models`
  endpoint) instead of jumping straight to `openrouter/free`. `openrouter/free` is appended as the
  **last** OpenRouter candidate.
- Filters (in addition to the existing ones): both prices 0; not deprecated; text output;
  `context_length ≥ 32768`; id not matching `safety|guard|embed|moderation|rerank`; tools required
  when the request has `tools`; not in `dynamic_models.blocked`.

### Ranking
Score (lower is better), per discovered model:
```
score = openrouter_position
      - 3 * successes_recent          (capped at 5)
      + 5 * failures_recent           (capped at 5; 429/quota counts as failure)
      + latency_penalty               (0 if p50 ≤ 5 s, else +1 per extra 5 s, capped at 5)
```
- "recent" = last 24 h, stored per model in the model health record (bounded, see section 4).
- Models in `dynamic_models.pinned` come first in pin order, regardless of score.
- Ties keep OpenRouter's order. `max_candidates` still limits how many are tried.
- The existing `promoted` flag is replaced by this score.

### Stale configured models
- For providers with discovery enabled, a configured model whose id ends in `:free` and is missing
  from the latest successful discovery is skipped with excluded reason
  "no longer listed as free upstream" and flagged in the admin API. If discovery has never
  succeeded, nothing is skipped.

### Config additions
`dynamic_models.pinned: list[str] = []`, `dynamic_models.blocked: list[str] = []`.

### API
`GET /admin/free-models` → for each discovered model: id, rank, score, OpenRouter position,
context length, tools support, successes/failures (24 h), p50 latency, pinned/blocked, and the reason
it was filtered out if it was.

### Tests
Discovery feeds `auto` before `openrouter/free`; filters drop safety/embedding/short-context models;
failures push a model down and successes pull it up; pinned first, blocked never; stale `:free`
config model skipped only after a successful discovery.

---

## 4. Low RAM

1. `run.sh` / `run.bat` start without `--reload`; `--dev` turns it on. Dockerfile already runs without it.
2. `_SlidingWindowCounter` becomes a fixed 60-bucket ring per window (bucket = window/60). Memory is
   constant regardless of traffic; `current_total` is a sum over 60 ints. Accuracy: within one bucket
   (1 s for a minute window, 24 min for a day window) — acceptable for local limits.
3. Health/stat records for dynamically discovered models not used for 24 h are evicted
   (checked during discovery refresh).
4. Dependencies: `uvicorn[standard]` → `uvicorn` + `httptools`; `watchfiles` moves to the `dev` extra.
5. Dockerfile: `ENV MALLOC_ARENA_MAX=2`; uvicorn started with `--no-access-log` (OctoFlux logs
   requests itself).
6. `scripts/benchmark.py` gains a memory mode: 10 000 mocked requests, prints RSS at 0 / 5 000 / 10 000.
   Pass: < 80 MB in the Docker image and no growth between 5 000 and 10 000.

### Tests
Bucketed counter: limits enforced at window edges, memory constant after many records; eviction of
idle dynamic models.

---

## 5. Dashboard

### Visual language (from the Quantix reference)
- Warm off-white page background with a faint grain; frosted white cards (light backdrop blur,
  1 px light border, 16 px radius, soft shadow).
- Orange accent; charts use orange → yellow-green gradient bars; secondary series in muted purple-grey;
  success green / failure red for deltas.
- Large page title at top-left; logo top-left; centered pill tab bar; round icon buttons on the right.
- One bundled sans-serif `woff2` (~30 KB) served locally. Hand-written SVG charts, no chart library.
- Light theme only. Responsive for desktop and tablet; usable on phones.

### Pages

| Page | Content |
|---|---|
| Overview | One card per provider (last used, requests today, 24 h sparkline). Requests-by-model list. Latency (p50), tokens/min, P50/P90/P99 with gradient bars. |
| Requests | Last 200 requests (time, requested model, upstream model, provider, key, status, latency, tokens, attempts). Request-ID search. Row opens the attempt trace. |
| Providers | Card per provider: enable, health, priority, base URL, limits, total limits, retry, health settings. Add/remove provider. |
| Keys | Per provider: masked key, status, remaining quota, cooldown, today's usage; add (literal or `${VAR}`), enable/disable, test, delete; rotation mode. |
| Models | Static models (enable, priority, capabilities). Free models: ranking from `/admin/free-models` with pin/block. |
| Aliases | Edit alias targets; reorder with up/down. |
| Playground | Send a chat message through the gateway; show provider, key, model and the attempt trace. |
| Settings | Client keys (create — shown once — and revoke), admin keys, routing defaults, config backups with restore. |

### Backend additions
- Request log: in-memory ring of the last 200 requests (request id, times, models, provider, key name,
  status, latency, tokens, attempt trace). No message content is stored.
- Time series: per-provider per-minute request/error/token counts for 24 h (1 440 slots), saved to
  `usage.db` with the limit counters.
- Latency percentiles from a fixed bucket histogram per provider.
- Endpoints: `GET /admin/requests`, `GET /admin/requests/{id}`, `GET /admin/timeseries`,
  `GET /admin/health`, `GET /admin/free-models`, `POST /admin/playground`
  (runs the scheduler as an internal client; same body as `/v1/chat/completions`, non-streaming),
  plus the config and backup endpoints from section 1.

### Frontend structure
`app/admin/static/`: `index.html`, `styles.css`, `app.js` (hash router, auth, API wrapper, save/conflict
handling), `charts.js` (sparkline, bars, gradient meter), `pages/<page>.js` (one per page),
`fonts/<font>.woff2`. Admin key kept in `sessionStorage`. Every edit page loads `/admin/config`, edits its
part and saves with `PUT /admin/config`; on 409 it shows "Someone else saved first — reload?".
New client keys are generated server-side and shown once.

### Tests
API tests for every new endpoint; request log bounded to 200 and free of message content;
Playwright pass over every page against the local server (loads, no console errors, one save round-trip).

---

## Out of scope
Dark theme; per-admin permissions (all admins are equal); multi-process/shared state; editing YAML
comments; REA integration.
