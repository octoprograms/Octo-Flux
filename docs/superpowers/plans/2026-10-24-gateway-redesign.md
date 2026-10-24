# OctoFlux Gateway Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Admin-secured, dashboard-managed OctoFlux with smarter key rotation, self-ranked free OpenRouter models, and a < 80 MB footprint.

**Architecture:** Backend changes stay inside the existing FastAPI layers (`app/core` runtime state, `app/admin` admin API, `app/models` config schema). Config edits go through one whole-config GET/PUT on the raw YAML. The dashboard is static HTML/CSS/JS in `app/admin/static/`, one JS module per page, no build step.

**Tech Stack:** Python ≥ 3.11, FastAPI, httpx, Pydantic v2, PyYAML, SQLite (stdlib), pytest + respx, Playwright MCP for the UI pass; vanilla JS (ES modules) + inline SVG.

**Spec:** `docs/superpowers/specs/2026-10-24-gateway-redesign-design.md`

## Global Constraints

- Work on branch `gateway-redesign` in the Octo-Flux repo; never commit `.env` or `data/`.
- Memory: < 80 MB RSS in the Docker image; no growth between 5 000 and 10 000 benchmark requests.
- No new runtime dependencies except `httptools` (replacing `uvicorn[standard]`). No JS/CSS libraries, no build step.
- Secrets never reach the browser unmasked and are never written from resolved env values.
- Mask format: `"•••• " + last 4 chars` (values ≤ 8 chars: `"••••"`).
- Admin errors use the OpenAI shape: `{"error": {"type": ..., "message": ...}}`.
- Backups: `config/backups/OctoFlux.<UTC %Y%m%dT%H%M%SZ>.yaml`, newest 20 kept.
- Request log keeps the last 200 requests and **no message content**.
- Every task ends with the full suite green: `.venv/Scripts/python -m pytest -q`.

## Review Focus

1. **Renamed key with an unchanged mask** — user renames a key in the dashboard without retyping it; expected: value kept. Unmask matches by last-4 within the same provider when the name changed; ambiguous → 422 "re-enter key". Test in Task 2.
2. **`${VAR}` inside a longer string or with a default** (`"Bearer ${X}"`, `${X:-dev}`) — expected: written back byte-identical. Test in Task 2.
3. **Absurd rate-limit reset headers** (negative, garbage, or days away) — expected: ignored or clamped to 24 h; never a permanent lockout. Test in Task 5.
4. **OpenRouter discovery down** — expected: `auto` still routes via static models and `openrouter/free`; nothing marked stale. Test in Task 8.
5. **Restoring a backup whose `${VAR}` is unset** — expected: 422, live file and running config untouched. Test in Task 3.

---

### Task 0: Branch and commit the earlier fixes

- [ ] **Step 1:** `git switch -c gateway-redesign`
- [ ] **Step 2:** Run `.venv/Scripts/python -m pytest -q` → 104 passed.
- [ ] **Step 3:** Commit `app/api/chat.py app/core/retry.py app/providers/base.py tests/test_streaming_and_api.py` as `fix: count tokens toward limits; route streaming through scheduler; mask streaming errors`, then the spec and this plan as `docs: gateway redesign spec and plan`.

---

### Task 1: Admin auth

**Files:**
- Modify: `app/models/provider.py` (`ServerConfig`), `app/api/deps.py`, `app/admin/status.py`, `config/OctoFlux.yaml`, `.env.example`, `README.md`
- Test: `tests/test_admin_auth.py`

**Interfaces:**
- Produces: `class AdminKeyConfig(BaseModel): name: str; value: str` (non-empty value validator like `KeyConfig`); `ServerConfig.admin_keys: list[AdminKeyConfig] = []`; `require_admin_auth(request, state) -> None` in `app/api/deps.py`, sets `request.state.admin_name: str`; route `GET /admin/health` (body = today's `/health` body).

- [ ] **Step 1: Write failing tests** in `tests/test_admin_auth.py` (fixture config adds `server: {require_auth: true, client_keys: ["client-1"], admin_keys: [{name: noure, value: "admin-1"}]}`):
  - `test_admin_key_opens_admin_routes`: `GET /admin/status` with `Bearer admin-1` → 200.
  - `test_client_key_rejected_on_admin_routes`: same with `Bearer client-1` → 401.
  - `test_admin_key_rejected_on_v1_routes`: `GET /v1/models` with `Bearer admin-1` → 401.
  - `test_admin_disabled_without_admin_keys`: config without `admin_keys` → `GET /admin/status` 403, `json()["error"]["type"] == "admin_disabled"`.
  - `test_public_health_is_minimal`: `GET /health` → `== {"status": "ok"}`.
  - `test_admin_health_has_details`: `GET /admin/health` with admin key → contains `"providers"`.
  - `test_require_auth_false_does_not_open_admin`: `require_auth: false` + admin keys → `GET /admin/status` without header → 401.
- [ ] **Step 2:** Run `pytest tests/test_admin_auth.py -v` → FAIL.
- [ ] **Step 3:** Implement. Compare with `hmac.compare_digest` over every key (no early exit); switch `require_client_auth` to the same helper `_matches_any(token: str, values: list[str]) -> str | None` (returns the matching index/name). Move every `/admin/*` route and `/metrics` in `app/admin/status.py` to `require_admin_auth`. Existing tests that call admin routes with `require_auth: false` fixtures need an admin key header: add `admin_keys: [{name: test, value: test-admin}]` to the `two_provider_config` fixture and a helper `ADMIN = {"Authorization": "Bearer test-admin"}` in `tests/conftest.py`; update callers.
- [ ] **Step 4:** Add to `config/OctoFlux.yaml`: `admin_keys: [{name: owner, value: "${OctoFlux_ADMIN_KEY}"}]`; add `OctoFlux_ADMIN_KEY=` to `.env.example`; README "Admin access" section stating client keys no longer open `/admin`.
- [ ] **Step 5:** Run full suite → PASS. Commit `feat: separate admin keys for admin API`.

---

### Task 2: Config store and `GET/PUT /admin/config`

**Files:**
- Create: `app/core/config_store.py`, `app/admin/config_api.py`
- Modify: `app/admin/status.py` (extract reload), `app/main.py` (include router)
- Test: `tests/test_config_api.py`

**Interfaces:**
- Consumes: `require_admin_auth` (Task 1); `_resolve_env_recursive`, `load_yaml_text`, `_format_validation_error` from `app/core/config.py`.
- Produces (in `app/core/config_store.py`):
  - `config_path() -> Path` (same env lookup as `load_config_from_env`)
  - `read_raw(path: Path) -> tuple[dict, str]` — raw dict + `version` (sha256 hex of file bytes)
  - `mask_secrets(raw: dict) -> dict` — deep copy with literal secrets masked; `${...}` values untouched
  - `unmask_secrets(new: dict, old: dict) -> dict` — raises `ConfigStoreError(422, "unknown_mask", msg)`
  - `validate_raw(raw: dict) -> OctoFluxConfig` — resolve copy, fill provider ids, validate; raises `ConfigStoreError(422, "invalid_config", msg, errors=list)`; raises `ConfigStoreError(422, "would_lock_out", ...)` when `server.admin_keys` is empty
  - `write_with_backup(path: Path, raw: dict) -> str` — backup, prune to 20, write `.tmp`, `os.replace`; returns new version
  - `class ConfigStoreError(Exception): status: int; type: str; message: str; errors: list | None`
- Produces (in `app/admin/status.py`): `async def apply_config(app: FastAPI, new_config: OctoFluxConfig) -> None` — the body of today's `admin_reload` after loading; `admin_reload` calls it.
- Secret locations: `providers.<id>.keys[].value`, `server.client_keys[]`, `server.admin_keys[].value`.

- [ ] **Step 1: Write failing tests** (use `tmp_path` config file + `monkeypatch.setenv("OctoFlux_CONFIG", ...)`, app built via `create_app()` with state from that file):
  - `test_get_masks_literal_secrets`: literal key `"sk-abcdefgh1234"` → `"•••• 1234"`; `"${GROQ_KEY}"` returned verbatim.
  - `test_put_round_trip_keeps_real_secret`: GET, flip a model's `enabled`, PUT → file contains `sk-abcdefgh1234` and the flipped flag.
  - `test_put_preserves_env_refs_verbatim`: values `"Bearer ${X}"` and `"${Y:-dev}"` unchanged in file after PUT. *(Review Focus 2)*
  - `test_put_renamed_key_keeps_value`: rename key `a` → `b` with mask unchanged → file has `b` with original value. *(Review Focus 1)*
  - `test_put_ambiguous_mask_422`: two stored keys ending `1234`, renamed key with mask `"•••• 1234"` → 422 `unknown_mask`.
  - `test_put_stale_version_409`: PUT with old version after the file changed → 409 `config_conflict`, file unchanged.
  - `test_put_invalid_config_422`: `base_url: "ftp://x"` → 422 `invalid_config`, `errors[0]["loc"]` contains `"base_url"`.
  - `test_put_without_admin_keys_422`: → 422 `would_lock_out`.
  - `test_put_writes_backup_and_prunes`: 22 saves → exactly 20 files in `config/backups/`.
  - `test_put_hot_reloads`: after disabling a provider via PUT, `GET /admin/status` shows it disabled without restart.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement `config_store.py`. Unmask rule: for each masked value, first look up the same identity (provider id + key name / admin key name / client key position) in `old`; if not found or its mask differs, search all stored secrets in the same group (same provider's keys / admin keys / client keys) for a unique mask match; else `unknown_mask`. YAML written with `yaml.safe_dump(raw, sort_keys=False, allow_unicode=True)`.
- [ ] **Step 4:** Implement `app/admin/config_api.py`: `GET /admin/config`, `PUT /admin/config` (body `{"version": str, "config": dict}`), serialized by a module-level `asyncio.Lock`; on success log `config_saved` with `admin=request.state.admin_name` and return `{"version": new}`.
- [ ] **Step 5:** Full suite → PASS. Commit `feat: dashboard config API with masking, versioning and backups`.

---

### Task 3: Backups list and restore

**Files:** Modify `app/core/config_store.py`, `app/admin/config_api.py`; Test `tests/test_config_api.py`

**Interfaces:**
- Produces: `list_backups(path: Path) -> list[dict]` (`name`, `created_at` ISO, `size`), newest first; `GET /admin/config/backups`; `POST /admin/config/backups/{name}/restore` → `{"version": str}`.

- [ ] **Step 1: Write failing tests:**
  - `test_list_backups_newest_first`.
  - `test_restore_backup_applies_and_backs_up_current`: restore → file equals backup content; a new backup of the pre-restore file exists.
  - `test_restore_unknown_name_404`: name `../OctoFlux.yaml` → 404.
  - `test_restore_with_unset_env_var_422`: backup references `${MISSING}` → 422, live file bytes and running config unchanged. *(Review Focus 5)*
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement; `name` must be in `list_backups()` names, else 404 `backup_not_found`. Restore = `validate_raw` → `write_with_backup` → `apply_config`.
- [ ] **Step 4:** Full suite → PASS. Commit `feat: list and restore config backups`.

---

### Task 4: Bucketed rate counters

**Files:** Modify `app/core/limits.py`; Test `tests/test_limits.py`

**Interfaces:**
- Produces: `_SlidingWindowCounter(window_seconds: float, limit: int, buckets: int = 60)` with unchanged methods `current_total`, `would_exceed`, `record`, plus `dump() -> dict` (`{"start": float, "counts": list[int]}`, wall-clock based) and `load(data: dict) -> None`.

- [ ] **Step 1: Write failing tests:**
  - `test_counter_memory_constant`: record 100 000 times → `len(counter.counts) == 60`.
  - `test_counter_expires_after_window`: record at t=0, `current_total(now=61)` for a 60 s window → 0.
  - `test_counter_limit_at_edge`: limit 5, 5 records → `would_exceed(1)` True.
  - `test_counter_dump_load_round_trip`.
- [ ] **Step 2:** Run → FAIL (existing deque has no `counts`).
- [ ] **Step 3:** Implement as a ring of 60 ints indexed by `int(now // bucket_seconds) % 60`, clearing buckets skipped since the last write. Use `time.time()` (not monotonic) so dumps survive restarts.
- [ ] **Step 4:** Full suite → PASS. Commit `perf: fixed-size bucketed rate counters`.

---

### Task 5: Per-key limits, total limits, token budgets, upstream headers

**Files:**
- Modify: `app/models/provider.py`, `app/core/limits.py`, `app/core/router.py`, `app/core/retry.py`, `app/core/health.py`
- Create: `app/core/ratelimit_headers.py`
- Test: `tests/test_limits.py`, `tests/test_ratelimit_headers.py`

**Interfaces:**
- Produces:
  - `ProviderConfig.total_limits: LimitsConfig | None = None`
  - `effective_key_limits(provider: ProviderConfig, key: KeyConfig) -> LimitsConfig` in `limits.py` — provider `limits` with the key's non-None fields overriding.
  - `LimitsRegistry.key(...)` now always returns a `LimitState` (never None); `LimitsRegistry.provider(...)` is built from `total_limits` (empty `LimitsConfig()` when None).
  - `estimate_tokens(payload: dict) -> int` in `limits.py`: `ceil(sum(len(content)) / 4) + (max_tokens or max_completion_tokens or 0)`; list-type contents count their `text` parts.
  - `parse_rate_limit_headers(headers: httpx.Headers, now: float) -> RateLimitInfo | None` with `@dataclass RateLimitInfo: remaining_requests: int | None; remaining_tokens: int | None; resets_at: float | None` (wall-clock epoch seconds).
  - `HealthState` gains `remaining_requests`, `remaining_tokens`, `resets_at`, `max_remaining_requests` (largest seen) and `record_rate_limit(info: RateLimitInfo) -> None`, which cools the key down until `resets_at` when a remaining value is 0.
  - `UpstreamSuccess` gains `headers: httpx.Headers | None = None` (set by the adapter for chat and embeddings).
- Router: `Router.resolve(...)` takes `payload` and checks `has_request_budget()` and `has_token_budget(estimate_tokens(payload))` for both the key state and the provider total state; excluded reasons `"... exhausted its local request rate limit"` / `"... exhausted its local token limit"`.

- [ ] **Step 1: Write failing tests:**
  - `test_two_keys_double_capacity`: provider `requests_per_minute: 1`, two keys → two requests succeed, third → 503.
  - `test_total_limits_cap_all_keys`: `total_limits: {requests_per_minute: 1}`, two keys → second request 503.
  - `test_token_budget_skips_key`: `tokens_per_minute: 10`, request with 100-char message → 503 with reason containing `"token limit"`.
  - `test_groq_headers_parse`: `{"x-ratelimit-remaining-requests": "0", "x-ratelimit-reset-requests": "2m59.5s"}` → `remaining_requests == 0`, `resets_at == now + 179.5`.
  - `test_openrouter_headers_parse`: `{"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(int((now+30)*1000))}` → `resets_at ≈ now+30`.
  - `test_zero_remaining_cools_key_until_reset`: success response carrying remaining=0 → next request uses the other key.
  - `test_absurd_reset_headers_clamped`: reset `"-5s"`, `"abc"`, and 3 days → None / ignored / clamped to 86 400 s. *(Review Focus 3)*
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement. Duration grammar: sequence of `<number><unit>` with units `ms|s|m|h`. Call `record_rate_limit` from `Scheduler._execute_operation` for both success and failure results that carry headers. No change to `config/OctoFlux.yaml` (its numbers are already per-key free-tier limits); add a README "Limits" section explaining per-key semantics and `total_limits`.
- [ ] **Step 4:** Full suite → PASS (existing limit tests may assume provider-wide limits; update them to `total_limits` where they meant combined). Commit `feat: per-key limits, token budgets and upstream rate-limit headers`.

---

### Task 6: `smart` key selection

**Files:** Modify `app/models/provider.py`, `app/core/router.py`; Test `tests/test_routing.py`

**Interfaces:**
- Produces: key-selection literal adds `"smart"`; `RoutingConfig.default_key_selection` default `"smart"`; `ProviderConfig.key_selection: ... | None = None` (None → routing default); `Router._headroom(provider_id: str, key: KeyConfig, cfg: LimitsConfig) -> float` in [0, 1].

- [ ] **Step 1: Write failing tests:**
  - `test_smart_prefers_key_with_most_headroom`: key A used 8/10, key B 2/10 → B first.
  - `test_smart_uses_upstream_remaining`: equal local use, A header remaining 1 of max 10, B 9 → B first.
  - `test_smart_ties_round_robin`: fresh keys → consecutive requests alternate.
  - `test_provider_inherits_routing_default`.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement headroom = min over configured windows of `1 - used/limit`, and `remaining/max_remaining` when known; 1.0 when nothing is known. Sort descending, stable over the round-robin rotation.
- [ ] **Step 4:** Full suite → PASS. Commit `feat: smart key selection by remaining headroom`.

---

### Task 7: Persist limit counters; batch usage writes

**Files:** Modify `app/core/limits.py`, `app/core/usage.py`, `app/core/app_state.py`, `app/main.py`; Test `tests/test_limits.py`

**Interfaces:**
- Produces: `LimitsRegistry.dump() -> list[tuple[str, str, str, dict]]` (`scope` = provider|key, provider, key_name, `{window_field: counter.dump()}`) and `LimitsRegistry.load(rows, config: OctoFluxConfig)` (drops unknown providers/keys); `UsageRegistry.save_limits(rows)` / `UsageRegistry.load_limits() -> rows` using table `limit_counters(scope, provider, key_name, payload, PRIMARY KEY(scope, provider, key_name))`; `UsageRegistry.flush()`; `AppState.start_flusher()` / `stop_flusher()` — background task every 60 s calling `usage.flush()` and saving limits; final flush on shutdown.
- Change: `record_attempt` / `record_tokens` stop calling `_persist()` per request (today the whole table is rewritten on every request); `flush()` persists.

- [ ] **Step 1: Write failing tests:**
  - `test_limit_counters_survive_restart`: record 5 requests, flush, build a new `AppState` on the same db → `requests_per_day` used == 5.
  - `test_unknown_keys_dropped_on_load`.
  - `test_usage_not_written_per_request`: patch `_persist` → 0 calls after 10 requests; 1 after `flush()`.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement. Change the signature to `AppState.build(config, usage_db: str | None = None)` where `None` means "read `OctoFlux_USAGE_DB`, default `data/usage.db`" and `app/main.py` passes nothing; the `app_state` fixture in `tests/conftest.py` passes `str(tmp_path / "usage.db")` so tests stop writing the real `data/usage.db`.
- [ ] **Step 4:** Full suite → PASS. Commit `feat: persist limit counters; flush usage every 60s`.

---

### Task 8: Free OpenRouter models — discovery for `auto` chat and filters

**Files:** Modify `app/core/router.py`, `app/models/provider.py`; Test `tests/test_openrouter_auto.py`, `tests/test_dynamic_models.py`

**Interfaces:**
- Produces: `DynamicModelsConfig.pinned: list[str] = []`, `.blocked: list[str] = []`, `.min_context: int = 32768`; `_model_suits_request(model, payload, settings: DynamicModelsConfig) -> str | None` returns the rejection reason (None = accepted); `_NON_CHAT_RE = re.compile(r"safety|guard|embed|moderation|rerank", re.I)`.
- Behaviour: `resolve_dynamic` no longer skips OpenRouter for chat; `_provider_model_pairs` no longer inserts `openrouter/free` up front; after the discovered candidates, `openrouter/free` is appended once per key as the last OpenRouter candidate (`reason="openrouter free router fallback"`).

- [ ] **Step 1: Write failing tests** (respx-mocked discovery list):
  - `test_auto_tries_discovered_before_openrouter_free`: attempt order = discovered model first, `openrouter/free` last.
  - `test_filters_drop_non_chat_and_short_context`: `*-content-safety:free`, `*-embedding:free`, a 16k-context model → not candidates.
  - `test_blocked_model_never_used`.
  - `test_discovery_down_auto_still_routes`: discovery endpoint 500 → `auto` succeeds via a static model; `/admin/free-models` (Task 9) shows no stale flags. *(Review Focus 4)*
  - Update `test_streaming_auto_openrouter_uses_free_router` to the new order.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Full suite → PASS. Commit `feat: route auto chat through ranked free OpenRouter models`.

---

### Task 9: Free-model scoring, pins, stale models, `/admin/free-models`

**Files:**
- Create: `app/core/model_stats.py`
- Modify: `app/core/router.py`, `app/core/retry.py`, `app/admin/status.py`, `app/core/health.py`
- Test: `tests/test_free_models.py`

**Interfaces:**
- Produces (in `model_stats.py`): `class ModelStats` with `record(success: bool, latency_ms: float, now: float)`, `successes_24h(now)`, `failures_24h(now)`, `p50_ms(now) -> float | None`, `last_used: float`; storage = 24 hourly buckets of (successes, failures) + a 32-sample latency ring. `class ModelStatsRegistry` with `get(provider_id, model_id) -> ModelStats`, `evict_idle(now, max_idle=86400) -> int`.
- `score(position: int, stats: ModelStats, now: float) -> float` = `position - 3*min(s,5) + 5*min(f,5) + min(max(0, ceil((p50_s - 5)/5)), 5)`.
- Remove `HealthState.promotion_until` / `is_promoted` / `record_success(promote=...)` usage in favour of the score.
- Stale rule: configured model id ending `:free`, provider has discovery enabled, last discovery succeeded, id not in it → excluded `"no longer listed as free upstream"`.
- `GET /admin/free-models` (admin) → `{"provider": "openrouter", "discovered_at": float|null, "models": [{id, rank, score, position, context_length, tools, successes_24h, failures_24h, p50_ms, pinned, blocked, rejected_reason, stale}]}`.

- [ ] **Step 1: Write failing tests:**
  - `test_failures_push_model_down` / `test_successes_pull_model_up`.
  - `test_slow_model_penalised`: p50 16 s → +3.
  - `test_pinned_first_in_pin_order`.
  - `test_stale_free_model_skipped_after_discovery`.
  - `test_stale_not_applied_before_first_discovery`.
  - `test_idle_stats_evicted_after_24h`.
  - `test_free_models_endpoint_shape`.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement; `Scheduler` records into `ModelStatsRegistry` on every attempt; eviction runs at each discovery refresh.
- [ ] **Step 4:** Full suite → PASS. Commit `feat: score free models on real results; pins, blocks, stale detection`.

---

### Task 10: Lean runtime packaging and memory benchmark

**Files:** Modify `pyproject.toml`, `run.sh`, `run.bat`, `Dockerfile`, `scripts/benchmark.py`, `README.md`

- [ ] **Step 1:** `pyproject.toml`: `uvicorn[standard]>=0.30` → `uvicorn>=0.30`, `httptools>=0.6`; add `watchfiles>=0.21` to `dev`.
- [ ] **Step 2:** `run.sh` / `run.bat`: no `--reload` unless first arg is `--dev`.
- [ ] **Step 3:** `Dockerfile`: `ENV MALLOC_ARENA_MAX=2`; CMD adds `--no-access-log`.
- [ ] **Step 4:** `scripts/benchmark.py --memory`: 10 000 mocked requests through `AppState`, print RSS (via `resource.getrusage` on Linux, `psutil`-free fallback reading `/proc/self/status`; on Windows print "memory mode needs Linux/Docker") at 0 / 5 000 / 10 000; exit 1 if final ≥ 80 MB or growth 5 000→10 000 > 2 MB.
- [ ] **Step 5:** Verify: `pip install -e ".[dev]"`, full suite PASS; `docker build -t octoflux . && docker run --rm --entrypoint python octoflux scripts/benchmark.py --memory` → exit 0 (if Docker is unavailable, record that in the task report — do not claim the target met).
- [ ] **Step 6:** Commit `perf: lean runtime deps, no reload in prod, memory benchmark`.

---

### Task 11: Observability backend for the dashboard

**Files:**
- Create: `app/core/request_log.py`, `app/core/timeseries.py`, `app/admin/insights.py`
- Modify: `app/core/retry.py`, `app/api/chat.py`, `app/core/app_state.py`, `app/main.py`, `app/core/usage.py` (persist time series in `flush`)
- Test: `tests/test_insights.py`

**Interfaces:**
- `request_log.py`: `@dataclass RequestRecord(id, started_at, requested_model, upstream_model, provider, key, status, latency_ms, input_tokens, output_tokens, stream, attempts: list[dict])`; `class RequestLog(maxlen=200)` with `add(rec)`, `list(limit=200) -> list[dict]`, `get(id) -> dict | None`.
- `timeseries.py`: `class MinuteSeries` — 1 440 slots per provider of `(requests, errors, tokens)`; `record(provider, ok, tokens, now)`, `series(provider, minutes=1440, now) -> list[list[int]]`, `dump()/load()`. `class LatencyHistogram` — fixed bucket edges `[50,100,200,400,800,1600,3200,6400,12800,25600,∞]` ms; `observe(provider, ms)`, `percentiles(provider) -> {"p50","p75","p90","p95","p99"}`.
- `insights.py` (admin): `GET /admin/requests`, `GET /admin/requests/{id}` (404 unknown), `GET /admin/timeseries?minutes=1440`, `GET /admin/latency`, `POST /admin/playground` (body = `ChatCompletionRequest`, forced non-stream; returns `{"response": ..., "record": RequestRecord}`).
- `ChatCompletionResult` already carries `trace`; the scheduler caller builds the `RequestRecord` (chat, embeddings, responses, streaming).

- [ ] **Step 1: Write failing tests:**
  - `test_request_log_bounded_200`.
  - `test_request_log_has_no_message_content`: send message `"TOP-SECRET-PROMPT"` → absent from `json.dumps(GET /admin/requests)`.
  - `test_request_detail_includes_attempt_trace` (failover case → 2 attempts).
  - `test_timeseries_counts_per_minute`.
  - `test_latency_percentiles`.
  - `test_playground_returns_record`.
- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement. **Step 4:** Full suite → PASS.
- [ ] **Step 5:** Commit `feat: request log, time series and latency for the dashboard`.

---

### Task 12: Dashboard shell, design system, charts, Overview

Before writing UI, load the `frontend-design:frontend-design` skill and view the reference image `C:\Users\USER\Downloads\original-ccdba8ba0e42c7163f55eee0623a7019.webp`.

**Files:**
- Replace: `app/admin/static/index.html`
- Create: `app/admin/static/styles.css`, `app.js`, `charts.js`, `pages/overview.js`, `fonts/<font>.woff2`
- Modify: `app/admin/ui.py` (mount `/admin/static` with `StaticFiles`; keep `/admin`, `/admin/`, `/admin/dashboard` → `index.html`)

**Interfaces:**
- `app.js` exports `api(path, {method, body}) -> Promise<any>` (adds admin bearer from `sessionStorage["octoflux.admin"]`, on 401/403 shows login), `loadConfig() -> {version, config}`, `saveConfig(version, config) -> version` (on 409 shows "Someone else saved first — reload?"), `toast(msg, kind)`, `register(route, {title, render(el)})`; routes by `location.hash` (`#/overview` default).
- `charts.js` exports `sparkline(values, {dashed?})`, `bars(values, labels)`, `gradientMeter(fraction)`, each returning an SVG string; every chart has a text alternative (`<title>`).
- Design tokens in `:root` of `styles.css`: background warm off-white with SVG-noise grain; card = `rgba(255,255,255,.7)`, `backdrop-filter: blur(12px)`, 1 px border, 16 px radius, soft shadow; accent orange; gradient orange→yellow-green; secondary muted purple-grey; success green, danger red.
- Layout: logo left, centered pill tab bar (Overview · Requests · Providers · Keys · Models · Aliases · Playground · Settings), round icon buttons right; large page title top-left.

- [ ] **Step 1:** Build shell + login + Overview (provider cards with requests today + 24 h sparkline, requests-by-model list, p50 latency, tokens/min, P50/P90/P99 gradient bars) from `/admin/health`, `/admin/usage`, `/admin/timeseries`, `/admin/latency`.
- [ ] **Step 2:** Verify with Playwright against `http://127.0.0.1:8000/admin`: login with the admin key, Overview renders, zero console errors; screenshot at 1440×900 and 768×1024 and compare against the reference image's structure.
- [ ] **Step 3:** Keyboard: tabs reachable and focus-visible; contrast of text on cards ≥ 4.5:1.
- [ ] **Step 4:** Commit `feat(dashboard): Quantix-style shell and overview`.

---

### Task 13: Dashboard read pages — Requests and Playground

**Files:** Create `app/admin/static/pages/requests.js`, `pages/playground.js`

- [ ] **Step 1:** Requests: table of `/admin/requests`, request-ID search box (filters; exact ID opens detail), row opens a drawer with the attempt trace from `/admin/requests/{id}`; refresh every 5 s while visible.
- [ ] **Step 2:** Playground: model select built from `loadConfig()` (`auto`, then alias names, then each provider's enabled model ids), message box, send via `POST /admin/playground`, show reply, provider/key/model and trace.
- [ ] **Step 3:** Playwright: send a playground message → reply shown and the request appears on the Requests page; zero console errors.
- [ ] **Step 4:** Commit `feat(dashboard): requests and playground pages`.

---

### Task 14: Dashboard edit pages — Providers, Keys, Models, Aliases

**Files:** Create `app/admin/static/pages/providers.js`, `pages/keys.js`, `pages/models.js`, `pages/aliases.js`

All four edit the object from `loadConfig()` and save with `saveConfig()`; validation errors (422) are shown next to the field named in `errors[].loc`.

- [ ] **Step 1:** Providers: card per provider (enable, live health, priority, base URL, `limits`, `total_limits`, retry, health); add provider form (id, base URL, first key, first model); remove with confirmation dialog built in-page (no `window.confirm`).
- [ ] **Step 2:** Keys: per provider table (name, masked value or `${VAR}` badge "from environment", status, remaining quota, cooldown, today's usage from `/admin/status` + `/admin/usage`); add key (literal or `${VAR}`), enable/disable, rename, test (`POST /admin/providers/{p}/keys/{k}/test`), delete; rotation mode select (`smart`, `round_robin`, `least_used`, `priority`).
- [ ] **Step 3:** Models: static models table (enable, priority, capabilities); Free models table from `/admin/free-models` with pin/unpin, block/unblock (edits `dynamic_models.pinned/blocked`), stale and rejected reasons shown.
- [ ] **Step 4:** Aliases: list aliases, add/remove alias, add/remove targets (provider + model selects), reorder with up/down.
- [ ] **Step 5:** Playwright: on each page make one change, save, reload page → change persisted and `config/backups/` gained a file; rename a key without retyping → still `working` after save.
- [ ] **Step 6:** Commit `feat(dashboard): manage providers, keys, models and aliases`.

---

### Task 15: Dashboard Settings page

**Files:**
- Create: `app/admin/static/pages/settings.js`
- Modify: `app/admin/config_api.py` (add `POST /admin/client-keys` → generates `secrets.token_urlsafe(24)` prefixed `octo-`, appends to raw `server.client_keys`, saves via the Task 2 path, returns the plain key once)
- Test: `tests/test_config_api.py::test_create_client_key_returned_once` (POST returns key; subsequent `GET /admin/config` shows it masked)

- [ ] **Step 1:** Write the failing test; run → FAIL; implement endpoint; run → PASS.
- [ ] **Step 2:** Settings page: client keys (create → shown once with copy button; revoke), admin keys (add name + value or `${VAR}`; remove — UI blocks removing the last one, server enforces `would_lock_out`), routing defaults (`default_key_selection`, `max_total_attempts`, fallbacks), backups list with restore (in-page confirmation).
- [ ] **Step 3:** Playwright: create client key → use it on `/v1/models` → 200; restore a backup → toast + config reloads.
- [ ] **Step 4:** Commit `feat(dashboard): settings, client keys and backups`.

---

### Task 16: Final verification and docs

- [ ] **Step 1:** Full suite PASS; `scripts/benchmark.py --memory` result recorded (Docker).
- [ ] **Step 2:** Playwright pass over all 8 pages: no console errors, screenshots at 1440×900 / 768×1024 / 390×844.
- [ ] **Step 3:** README: admin keys, dashboard, per-key limits + `total_limits`, `smart` rotation, free-model ranking/pins, `--dev` flag, breaking changes list. Update `ARCHITECTURE.md` routing + limits sections.
- [ ] **Step 4:** Commit `docs: dashboard, admin access, rotation and free models`.
