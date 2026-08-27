# OctoFlux — Architecture Decision Record

## Problem

OctoPrograms uses several OpenAI-compatible inference providers (Groq, NVIDIA NIM,
OpenRouter, and others in the future). Each has its own keys, rate limits, and
failure modes. Applications calling these providers directly are fragile: a single
rate-limited key or a single flaky provider breaks the app. OctoFlux sits between
applications and providers as a thin, OpenAI-compatible gateway that picks a
healthy provider/model/key for every request and fails over intelligently when
something goes wrong.

## Goals

- OpenAI-compatible surface (`/v1/chat/completions`, `/v1/models`, streaming),
  plus alias inspection through `/v1/aliases`.
- Provider, model, and key are pure configuration — no source changes to add one.
- Deterministic, explainable routing (provider → model → key) with priority,
  rotation, health, cooldown, and local rate limits.
- Error-aware retry/failover — never retry things that can't succeed elsewhere
  (400, 401 on the same key, context-length errors) and always retry things that
  can (429, 5xx, timeouts, connection errors) against another candidate.
- Zero mandatory external infrastructure — one Python process, in-memory state.
- Every routing decision and failure explainable via structured logs, without
  leaking secrets.

## Non-goals (v1)

- Prompt-level caching, semantic routing, cost optimization by $/token.
- Multi-tenant billing.
- Distributed/multi-process shared rate-limit state (single-process only; SQLite
  usage export is optional, Redis is explicitly out of scope for v1).
- Full OpenAI feature parity (e.g. Assistants API, fine-tuning, embeddings can be
  added later — chat completions and models are the v1 surface. `/v1/responses`
  is a thin pass-through mirror of `/v1/chat/completions` semantics, not a full
  Responses API implementation).
- Config hot-reload via file-watching (a manual `POST /admin/reload` is provided
  instead — simpler and more predictable).

## Findings from researching existing gateways (LiteLLM, Portkey, OpenRouter, etc.)

1. **What they solve well:** unified OpenAI-shaped surface over many backends,
   per-key/provider rate-limit bookkeeping, fallback chains, streaming proxying.
2. **What makes them heavy:** pluggable everything (100+ providers hard-coded as
   Python classes), built-in DB migrations, background workers, dashboards,
   caching layers, budget/spend tracking across teams — most of which a
   single-agency, single-process deployment doesn't need.
3. **Failure modes they handle that we must too:** 429 with/without `Retry-After`,
   5xx bursts, connection resets, partial-stream failures (never silently
   restart a stream that already emitted tokens — that duplicates output for the
   caller), and provider-specific error-body shapes under a shared HTTP status.
4. **What we simplify:** one generic `OpenAICompatibleProvider` adapter driven by
   YAML instead of one Python class per vendor; in-memory sliding-window limits
   instead of a rate-limit microservice; a single deterministic routing pipeline
   instead of a pluggable strategy framework (the *policy* is configurable, the
   *pipeline* is not).
5. **Genuinely necessary:** structured request-id-tagged logging, a hard
   distinction between "safe to retry elsewhere" and "must return to caller",
   and separating **config** (declared shape) from **runtime state** (health,
   counters, cooldowns) so a config reload never wipes live state.

## Architecture

```
Client → FastAPI app → auth (OctoFlux key) → Router → Scheduler/Retry engine
                                                     → OpenAICompatibleProvider (httpx)
                                                     → Error classifier
                                                     → Health/Limits/Usage (runtime state)
                                                     → structured logs
```

Config objects (`app/models/provider.py`) are immutable per load. Runtime state
(`app/core/health.py`, `app/core/limits.py`, `app/core/usage.py`) lives in a
`Registry` keyed by `(provider_id, key_name)` / `(provider_id, model_id)` and is
never touched by config parsing — so a `/admin/reload` rebuilds config while
preserving live health/usage/cooldown state for providers/keys that still exist.

## Request lifecycle

```
validate request → request_id → resolve model (exact/alias/auto)
  → build ordered candidates (provider, model, key)
  → for each candidate (bounded by retry budget):
      check health + cooldown + local limits → skip if blocked
      send upstream request
      classify result
      success → record usage, health-ok, return OpenAI-shaped response
      failure → classify error → record health/cooldown → decide retry vs abort
        retryable   → next candidate (small backoff for same-target 5xx/timeout)
        non-retryable → return error to client immediately
  → all candidates exhausted → return 502 with a summary of what was tried
```

## Routing strategy

Deterministic pipeline, in this order:

1. Resolve requested `model` — exact `provider/model` id, an alias group, or
   `"auto"` (all enabled providers/models, priority order).
2. Filter disabled providers/models.
3. Filter providers/models/keys currently in `cooldown` or `unhealthy`.
4. Filter keys/providers whose local RPM/RPD/TPM/TPD or concurrency limit is
   currently exhausted.
5. Sort remaining candidates by `(provider.priority, model.priority)` ascending
   (lower number = tried first), then by key-selection policy
   (`round_robin` default; `least_used`, `priority` also supported).
6. Scheduler walks the sorted candidate list. It never retries the exact same
   `(provider, model, key)` twice for one client request.

This is intentionally a plain sort + filter, not a pluggable strategy object
graph — the *filters/limits* are config-driven, the *pipeline* is fixed and
easy to read top-to-bottom in `app/core/router.py`.

## Error classification

`app/core/errors.py` maps upstream HTTP status + response body shape into a
closed set of categories (`authentication_error`, `rate_limited`,
`quota_exceeded`, `model_not_found`, `invalid_request`,
`context_length_exceeded`, `server_error`, `timeout`, `connection_error`,
`service_unavailable`, `overloaded`, `unknown`) independent of which provider
produced them. Each category carries a `RetryDecision`:

| category | retry same key | retry other key | retry other provider | mark unhealthy |
|---|---|---|---|---|
| authentication_error | no | no | yes | key |
| rate_limited | no (cooldown) | yes | yes | no (cooldown only) |
| quota_exceeded | no (long cooldown) | yes | yes | no |
| model_not_found | no | no | yes (diff model) | no |
| invalid_request | no | no | no | no |
| context_length_exceeded | no | no | no (same request) | no |
| server_error / overloaded / service_unavailable | after backoff | yes | yes | provider (soft) |
| timeout / connection_error | after backoff | yes | yes | provider (soft) |

`invalid_request` and `context_length_exceeded` return straight to the client —
the same malformed/oversized request will fail identically on every provider.

## Retry strategy

`app/core/retry.py` implements exponential backoff with full jitter
(`base * 2^attempt + random(0, jitter)`, capped), a **per-request attempt
budget** (`max_total_attempts`, default 4) independent of per-provider
`retry.max_attempts`, and an attempted-set of `(provider, model, key)` so a
candidate is never retried twice for the same inbound request. `Retry-After`
from a 429 response is honored when present and used as the cooldown duration
instead of the configured default.

## Health model

Each provider and each key has a small state machine:
`healthy → (N consecutive failures) → unhealthy/cooldown → (cooldown elapses) →
healthy (optimistically, no active probing — the next real request is the probe)`.
No background polling traffic is generated; cooldown expiry is checked lazily
when a candidate is considered. This avoids "excessive background traffic"
while still self-healing.

The on-demand admin key test and background health monitor call the provider's
`GET /models` endpoint. A successful HTTP response proves provider reachability
and key acceptance, but is not a model completion test. The adapter preserves
the returned model IDs, and `POST /admin/providers/{provider_id}/keys/{key_name}/test`
compares them with the provider's enabled configured model IDs and returns a
per-model `available` boolean. OpenRouter and Groq IDs are compared directly.
NVIDIA NIM returns bare names, so the comparison also strips the configured
publisher prefix (for example, `nvidia/foo` matches `foo`).

Model availability is therefore a snapshot: a model may be listed and still
be unavailable for a completion because of upstream routing, permissions,
quota, or rate limits. The chat scheduler remains the final authority and
classifies actual completion failures such as `model_not_found` and
`rate_limited`.

The dashboard's **Reload configuration** action calls `POST /admin/reload`
after operator confirmation. A successful reload swaps the parsed config and
provider adapters while preserving compatible runtime health, cooldown, and
usage state. If parsing fails, the endpoint returns an error and leaves the
active configuration unchanged.

## Configuration model

YAML, environment-variable interpolation (`${VAR}` / `${VAR:-default}`),
validated eagerly at startup with Pydantic (`app/models/provider.py`,
`app/core/config.py`). Startup fails fast with a specific, actionable message
on duplicate IDs, malformed URLs, or out-of-range limits — never a stack trace.

## Persistence strategy

In-memory only for v1 (dict-backed `Registry`, sliding-window counters).
Persistence is behind a small `UsageStore` interface (`app/core/usage.py`) so a
SQLite-backed implementation can be dropped in later without touching the
router or providers. Not implemented in v1 because nothing in the required
behavior needs it to survive a process restart.

## Security model

Two independent credential domains:

- **Client → OctoFlux:** `Authorization: Bearer <OctoFlux_KEY>` checked in
  `app/api/deps.py`. Multiple client keys supported (`server.client_keys`).
- **OctoFlux → upstream provider:** each provider key lives only in config
  (resolved from env), attached to the outbound request in
  `OpenAICompatibleProvider`, and is referenced everywhere else (logs, admin
  status, error messages) by its configured `name`, never its value.

Admin endpoints (`/admin/*`) require the same client-key auth by default and
never render key values. The static operator dashboard is served at `/admin`
and uses that same client key from the browser session to call the protected
admin endpoints; it does not introduce a second identity or password store.
Deployments should put the dashboard behind HTTPS and restrict access to
trusted operators because the client key authorizes both admin and gateway
requests.

## Performance considerations

- Single shared `httpx.AsyncClient` per provider (connection pooling,
  keep-alive) built once at startup, not per request.
- Hot path does no disk/db I/O; rate-limit counters are in-memory deques with
  O(1) amortized trim.
- Structured logs are single `logger.info(json)` calls, not multi-line
  formatting, and never serialize full prompts/responses — only sizes/ids.
- Streaming responses are forwarded chunk-by-chunk (`httpx` streaming +
  FastAPI `StreamingResponse`), never buffered in full.
