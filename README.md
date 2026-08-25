# OctoFlux

A small, fast, OpenAI-compatible LLM gateway. It sits between your
application and multiple OpenAI-compatible providers (Groq, OpenRouter,
NVIDIA NIM, or anything else that speaks `/v1/chat/completions`) and keeps
requests alive by rotating keys, rotating models, and failing over between
providers when something goes wrong — without you writing any retry code in
your application.

Point an OpenAI SDK at OctoFlux and it works without knowing which upstream
provider actually served the request:

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8000/v1", api_key="OctoFlux-dev-key")

response = client.chat.completions.create(
    model="auto",  # or a specific model id, or an alias like "fast"
    messages=[{"role": "user", "content": "Hello"}],
)
```

See `ARCHITECTURE.md` for the full design rationale (why each decision was
made, what was deliberately left out, and what was learned from studying
LiteLLM/Portkey/OpenRouter).

---

## 1. What OctoFlux is

- An async FastAPI service exposing `POST /v1/chat/completions`,
  `POST /v1/responses`, and `GET /v1/models`.
- A **router** that picks provider → model → API key based on priority,
  health, cooldowns, and locally-enforced rate limits.
- A **retry/failover engine** that is *error-aware*: it retries 429s and 5xxs
  against another key/provider, but returns 400s and context-length errors
  straight to the caller instead of hammering every provider with the same
  broken request.
- Entirely **configuration-driven** — adding a provider is a YAML block, not
  a Python class.
- A single process, in-memory by default, with **zero mandatory external
  infrastructure** (no Postgres, no Redis, no Kafka).

## 2. Architecture

```
Client
  → auth (OctoFlux client key)
  → Router            (resolve model → ordered provider/model/key candidates)
  → Scheduler/Retry    (send upstream, classify result, backoff, fail over)
  → OpenAICompatibleProvider (pooled httpx.AsyncClient per provider)
  → Health/Limits/Usage registries (runtime state, separate from config)
  → structured JSON logs (no secrets)
```

```
OctoFlux/
├── app/
│   ├── main.py                 # FastAPI app + lifespan (loads config, builds AppState)
│   ├── api/
│   │   ├── chat.py             # POST /v1/chat/completions (streaming + non-streaming)
│   │   ├── responses.py        # POST /v1/responses (thin mirror, see §13)
│   │   ├── models.py           # GET /v1/models
│   │   └── deps.py             # client auth, request-id
│   ├── core/
│   │   ├── config.py           # YAML load, ${ENV} resolution, fail-fast validation
│   │   ├── router.py           # candidate resolution (priority/health/limits/rotation)
│   │   ├── retry.py            # scheduler: backoff, failover, attempt budget
│   │   ├── errors.py           # HTTP status/body -> ErrorCategory -> RetryDecision
│   │   ├── health.py           # per-provider/per-key health + cooldown state
│   │   ├── limits.py           # in-memory sliding-window RPM/RPD/TPM/TPD/concurrency
│   │   ├── usage.py            # in-memory counters (requests, tokens, latency)
│   │   └── app_state.py        # wires config + registries + scheduler together
│   ├── providers/
│   │   ├── base.py             # ProviderAdapter protocol
│   │   ├── openai_compatible.py# the one generic adapter (config-driven)
│   │   └── registry.py         # one adapter instance per configured provider
│   ├── models/
│   │   ├── provider.py         # Pydantic config schema (Provider/Key/Model/Limits/...)
│   │   └── request.py          # OpenAI-compatible request/response schemas
│   ├── admin/status.py         # /health /admin/status /admin/providers /admin/usage /metrics /admin/reload
│   └── observability/logging.py# structured JSON logging, secret redaction
├── config/
│   ├── OctoFlux.yaml          # runnable example config (Groq + disabled OpenRouter)
│   └── providers.example.yaml  # reference blocks for more providers
├── tests/                      # 67 tests, mocked upstreams via respx (no real network)
├── scripts/benchmark.py        # routing/scheduling overhead benchmark
├── ARCHITECTURE.md             # design rationale (read this first)
├── pyproject.toml
└── .env.example
```

## 3. Installation

Requires Python 3.11+.

```bash
git clone <this repo> OctoFlux && cd OctoFlux
pip install -e ".[dev]"       # or: pip install fastapi "uvicorn[standard]" httpx pydantic pyyaml
cp .env.example .env
# edit .env: set OctoFlux_CLIENT_KEY and at least one provider's API key

# Linux/macOS: creates and initializes .venv on first run
chmod +x run.sh
./run.sh
```

## 4. Configuration

Everything lives in `config/OctoFlux.yaml` (path overridable via
`OctoFlux_CONFIG`). Secrets are never written to YAML directly — reference
environment variables:

```yaml
value: "${GROQ_API_KEY_1}"          # required, startup fails if unset
value: "${GROQ_API_KEY_2:-unset}"   # optional, falls back to "unset"
```

Startup **fails fast** with a specific message on invalid config — duplicate
provider/model/key ids, malformed `base_url`, out-of-range limits, a provider
with no enabled keys or models, an alias pointing at an unknown provider.

## 5. Adding a provider

No source changes needed — copy a block into `providers:`:

```yaml
providers:
  my_new_provider:
    enabled: true
    type: openai_compatible
    base_url: "https://api.example.com/v1"
    priority: 30                      # lower = tried first (all else equal)
    key_selection: round_robin        # round_robin | least_used | priority
    keys:
      - name: primary
        value: "${MY_NEW_PROVIDER_KEY}"
    models:
      - id: some-model-id
        enabled: true
        priority: 10
    limits:
      requests_per_minute: 30
    retry:
      max_attempts: 3
    health:
      failure_threshold: 3
      cooldown_seconds: 30
```

More examples (NVIDIA NIM, OpenRouter, a bare-bones template) are in
`config/providers.example.yaml`. Providers needing non-standard auth can set
`authentication: {type: header, header: "X-Api-Key"}` or add arbitrary
`headers:` — still pure config.

## 6. Adding API keys

Add entries under a provider's `keys:` list. Each key gets independent
runtime health, cooldown, and (optionally) its own `limits:` override — so
you can give one key a tighter budget than the provider default:

```yaml
keys:
  - name: key-tight-budget
    value: "${KEY_1}"
    limits:
      requests_per_minute: 10
  - name: key-normal
    value: "${KEY_2}"
```

## 7. Adding models

Add entries under a provider's `models:` list; `priority` controls fallback
order within that provider (lower tried first). To let a request fall back
across *different* model ids (not just across providers for the *same*
model id), declare an alias:

```yaml
aliases:
  fast:
    - {provider: groq, model: llama-3.1-8b-instant}
    - {provider: groq, model: llama-3.3-70b-versatile}
```

Requesting `model: "fast"` tries the alias targets in the listed order.
Requesting `model: "auto"` tries every enabled model across every enabled
provider, sorted by `(provider.priority, model.priority)`. Requesting an
exact model id only ever fails over across **providers offering that same
id** — OctoFlux never silently substitutes a different model for an exact
request unless you've named it in an alias.

## 8. Configuring limits

Limits are enforced **locally**, before ever calling upstream — useful
because provider limits are often undocumented or account-specific. Set
them at provider level and/or per-key:

```yaml
limits:
  requests_per_second: 1
  requests_per_minute: 30
  requests_per_hour: 1000
  requests_per_day: 1000
  tokens_per_minute: 6000
  tokens_per_hour: 100000
  tokens_per_day: 100000
  concurrent_requests: 5
```

Any field left unset is unbounded. Windows are independent sliding windows
(hitting RPD blocks even with RPM headroom).

## 9. Routing

Deterministic pipeline (see `app/core/router.py`):

1. Resolve `model` — exact id, alias, or `"auto"`.
2. Drop disabled providers/models.
3. Drop providers/keys currently in cooldown.
4. Drop providers/keys whose local limits are exhausted right now.
5. Sort remaining candidates by `(provider.priority, model.priority)`.
6. Order keys within a provider by `key_selection` (`round_robin` default).

`GET /admin/status` shows exactly what's healthy/cooling down right now;
every routing decision is logged with its reason (`app/observability/logging.py`
event `routing_decision`) without ever logging a key value.

`GET /health` also reports the last background `/models` probe for every
enabled provider key. Probes run concurrently at the shortest configured
`health.check_interval_seconds` (default 43,200 seconds / 12 hours), and show provider/key,
working status, latency, and a masked key hint. Terminal logs emit
`provider_health_check` events without exposing key values.

## 10. Failover

The retry engine (`app/core/retry.py`) is **error-aware**, not a blind loop:

| what happened | what OctoFlux does |
|---|---|
| 429 rate limited | cooldown that key, try another key/provider |
| 401/403 | cooldown that key, try another key/provider (not the same key) |
| 5xx / timeout / connection error | short backoff, then try again; soft-marks the provider unhealthy after repeated failures |
| 400 invalid request | **return to caller immediately** — retrying elsewhere won't help |
| context length exceeded | **return to caller immediately** — same request, same outcome everywhere |
| model not found | try another provider that has the same model id |

Bounded by `routing.max_total_attempts` (default 4) and never retries the
exact same `(provider, model, key)` twice for one inbound request.

## 11. Logging

Structured JSON, one line per event, to stdout:

```json
{"ts": 1755000000.1, "event": "upstream_failure", "request_id": "abc123",
 "provider": "groq", "model": "llama-3.3-70b-versatile", "key": "groq-primary",
 "category": "rate_limited", "status_code": 429, "attempt": 1, "retryable": true}
```

Key *values*, `Authorization` headers, and prompt/response bodies are never
logged — only key *names* and sizes. Level via `OctoFlux_LOG_LEVEL`
(`DEBUG`/`INFO`/`WARNING`/`ERROR`).

## 12. Usage tracking

In-memory counters, no database required: requests, success/failure,
input/output tokens, average latency, rate-limit events, fallback count —
broken down globally and per provider/model/key. See `GET /admin/usage` or
`GET /metrics`.

## 13. Streaming

`stream: true` proxies the upstream SSE stream chunk-by-chunk (never
buffered in full). Failover only happens **before the first byte** reaches
the client — once streaming has started, a mid-stream failure terminates the
stream rather than restarting against another provider, so output is never
silently duplicated.

`/v1/responses` is a **thin pass-through mirror** of chat completions (same
routing/failover), not a full implementation of OpenAI's Responses API — no
server-side conversation state or built-in tools. Use `/v1/chat/completions`
for full functionality.

## 14. Security

- **Client → OctoFlux**: `Authorization: Bearer <key>` checked against
  `server.client_keys`. Disable with `server.require_auth: false` for local
  dev only.
- **OctoFlux → provider**: each provider's key lives only in config
  (resolved from env) and is attached per-request; it is never returned to
  the client and never appears in logs or `/admin/*` responses (only key
  *names* do).
- Admin endpoints require the same client auth.

## 15. Running locally

```bash
export $(cat .env | xargs)   # or your preferred env loader
uvicorn app.main:app --reload --port 8000
```

```bash
curl -H "Authorization: Bearer $OctoFlux_CLIENT_KEY" http://localhost:8000/v1/models

curl -H "Authorization: Bearer $OctoFlux_CLIENT_KEY" -H "Content-Type: application/json" \
  -X POST http://localhost:8000/v1/chat/completions \
  -d '{"model":"auto","messages":[{"role":"user","content":"hello"}]}'
```

## 16. Running in production

- Run behind a process manager (systemd, supervisord) or container; a single
  `uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1` process is
  the intended deployment unit (rate-limit/health state is in-process and
  not shared across workers — run one worker, or put a load balancer with
  sticky-enough routing in front if you need more throughput than one
  process provides).
- Set `OctoFlux_LOG_LEVEL=INFO` and ship stdout to your log aggregator.
- `POST /admin/reload` reloads `config/OctoFlux.yaml` without restarting
  and without losing live health/cooldown/usage state for targets that still
  exist — use it after editing config on disk.
- No database, cache, or message queue is required to run this in
  production; add a persistent `UsageStore` implementation later only if you
  need usage history to survive restarts (the interface is already isolated
  in `app/core/usage.py` for this).

## Testing

```bash
pip install -e ".[dev]"
pytest -q
```

67 tests, all against mocked upstreams (`respx` intercepts `httpx` — no real
network calls), covering: config validation, env resolution, routing
priority/rotation/cooldown/limits, the full error-classification matrix,
end-to-end failover (provider A 429 → B success; key1 exhausted → key2 fails
→ provider B succeeds; model failure via alias → alternate model; timeout →
failover; invalid-request and context-length **not** failing over),
streaming pre-first-byte failover, and API-level auth enforcement.

## Benchmarking

```bash
python scripts/benchmark.py --requests 500 --concurrency 20
```

Measures OctoFlux's own routing/scheduling overhead against a mocked
upstream (isolates gateway cost from real network/provider latency).

## What's out of scope in v1

See `ARCHITECTURE.md` "Non-goals" — no multi-tenant billing, no
distributed/shared rate-limit state across processes, no config
file-watching (use `/admin/reload`), no full Responses API parity, no
mandatory external infrastructure.
