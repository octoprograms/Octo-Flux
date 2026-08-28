<!-- markdownlint-disable MD033 -->
<div align="center">
  <a href="https://octoprograms.com" target="_blank">
    <img src="assets/OctoFlux.svg" alt="OctoFlux" width="400">
  </a>
</div>
<!-- markdownlint-enable MD033 -->

--- 

**OctoFlux is an OpenAI-compatible LLM gateway with intelligent routing, key rotation, rate limiting, health checks, and provider failover.**

OctoFlux sits between your application and multiple OpenAI-compatible LLM providers. It handles provider selection, API-key rotation, local rate limiting, health tracking, retries, and failover so your application doesn't have to.

Point any OpenAI-compatible SDK at OctoFlux and let the gateway decide where each request should go.

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:8000/v1",
    api_key="OctoFlux-dev-key",
)

response = client.chat.completions.create(
    model="auto",
    messages=[
        {"role": "user", "content": "Hello"}
    ],
)

print(response.choices[0].message.content)
```

OctoFlux works with **Groq, OpenRouter, NVIDIA NIM**, and other providers exposing OpenAI-compatible APIs.

> **Design goal:** keep the application-facing API stable while making upstream providers interchangeable.

See [`ARCHITECTURE.md`](ARCHITECTURE.md) for the design rationale, trade-offs, and non-goals.

---

## Features

* **OpenAI-compatible API**

  * `POST /v1/chat/completions`
  * `POST /v1/responses`
  * `GET /v1/models`
  * `GET /v1/aliases`

* **Intelligent routing**

  * Provider and model priorities
  * Health-aware routing
  * API-key rotation
  * Local rate-limit enforcement
  * Model aliases
  * Automatic routing with `model: "auto"`

* **Error-aware failover**

  * Retries rate limits, timeouts, connection failures, and server errors
  * Rotates keys and providers
  * Avoids retrying invalid requests
  * Avoids retrying context-length failures
  * Bounded retry attempts

* **Configuration-driven providers**

  * Add providers without writing Python code
  * Environment-variable based secrets
  * Per-provider and per-key limits
  * Custom authentication headers

* **Health monitoring**

  * Per-provider health
  * Per-key health
  * Cooldown tracking
  * Background `/models` probes
  * Manual provider/key testing

* **Operational visibility**

  * Structured JSON logging
  * Request IDs
  * Usage counters
  * `/metrics`
  * Admin API
  * Web dashboard
  * Runtime configuration reload

* **Streaming**

  * Pass-through SSE streaming
  * Failover before the first response byte
  * No silent stream duplication

* **Zero mandatory infrastructure**

  * No PostgreSQL
  * No Redis
  * No Kafka
  * Runtime state is in-memory by default

---

## Architecture

```text
                         ┌──────────────────────┐
                         │      Application     │
                         │  OpenAI SDK / HTTP   │
                         └──────────┬───────────┘
                                    │
                                    ▼
                         ┌──────────────────────┐
                         │      OctoFlux        │
                         │                      │
                         │  Authentication      │
                         │        ↓             │
                         │  Model Resolution    │
                         │        ↓             │
                         │  Router              │
                         │        ↓             │
                         │  Limits / Health     │
                         │        ↓             │
                         │  Retry / Failover    │
                         └──────────┬───────────┘
                                    │
                    ┌───────────────┼───────────────┐
                    ▼               ▼               ▼
              ┌──────────┐   ┌──────────┐   ┌──────────┐
              │   Groq   │   │OpenRouter│   │ NVIDIA   │
              │          │   │          │   │   NIM    │
              └──────────┘   └──────────┘   └──────────┘
```

The request path is intentionally deterministic:

```text
Client
  │
  ├── Authenticate
  │
  ├── Resolve model / alias / auto
  │
  ├── Build provider → model → key candidates
  │
  ├── Filter disabled / unhealthy / rate-limited targets
  │
  ├── Sort candidates by priority
  │
  ├── Select API key
  │
  ├── Send upstream request
  │
  ├── Classify response
  │
  └── Retry / fail over when appropriate
```

Every routing decision is logged with its reason through:

`app/observability/logging.py`

---

## Project Structure

```text
OctoFlux/
├── app/
│   ├── main.py
│   │
│   ├── api/
│   │   ├── chat.py
│   │   ├── responses.py
│   │   ├── models.py
│   │   └── deps.py
│   │
│   ├── core/
│   │   ├── config.py
│   │   ├── router.py
│   │   ├── retry.py
│   │   ├── errors.py
│   │   ├── health.py
│   │   ├── limits.py
│   │   ├── usage.py
│   │   └── app_state.py
│   │
│   ├── providers/
│   │   ├── base.py
│   │   ├── openai_compatible.py
│   │   └── registry.py
│   │
│   ├── models/
│   │   ├── provider.py
│   │   └── request.py
│   │
│   ├── admin/
│   │   └── status.py
│   │
│   └── observability/
│       └── logging.py
│
├── config/
│   ├── OctoFlux.yaml
│   └── providers.example.yaml
│
├── tests/
├── scripts/
│   └── benchmark.py
│
├── Dockerfile
├── docker-compose.yml
├── .dockerignore
├── .env.example
├── ARCHITECTURE.md
└── pyproject.toml
```

---

# Quick Start

## Requirements

* Python **3.11+**
* An API key for at least one OpenAI-compatible provider

## 1. Clone

```bash
git clone https://github.com/octoprograms/Octo-Flux.git OctoFlux
cd OctoFlux
```

## 2. Configure environment

```bash
cp .env.example .env
```

Set your OctoFlux client key and provider credentials:

```env
OctoFlux_CLIENT_KEY=OctoFlux-dev-key
GROQ_API_KEY_1=your-key
```

Provider secrets should **never** be committed to Git or written directly into the YAML configuration.

## 3. Start locally

The repository includes a development helper:

```bash
chmod +x run.sh
./run.sh
```

The script:

1. Creates `.venv` if necessary.
2. Installs the project and development dependencies.
3. Starts Uvicorn with live reload.

The API will be available at:

```text
http://localhost:8000
```

## 4. Test the gateway

```bash
curl \
  -H "Authorization: Bearer $OctoFlux_CLIENT_KEY" \
  http://localhost:8000/v1/models
```

Send a completion request:

```bash
curl \
  -H "Authorization: Bearer $OctoFlux_CLIENT_KEY" \
  -H "Content-Type: application/json" \
  -X POST \
  http://localhost:8000/v1/chat/completions \
  -d '{
    "model": "auto",
    "messages": [
      {
        "role": "user",
        "content": "Hello"
      }
    ]
  }'
```

---

# Docker Deployment

OctoFlux includes a production-oriented Docker image.

The container:

* Uses Python 3.12.
* Runs as the unprivileged `octoflux` user.
* Does not use development reload mode.
* Exposes port `8000`.
* Includes a health check against `/health`.

Create your environment file:

```bash
cp .env.example .env
```

Build and start:

```bash
docker compose up -d --build
```

Check the service:

```bash
docker compose ps
docker compose logs -f octoflux
```

Health check:

```bash
curl http://localhost:8000/health
```

Stop:

```bash
docker compose down
```

### Direct Docker execution

```bash
docker build -t octoflux:latest .

docker run -d \
  --name octoflux \
  --restart unless-stopped \
  --env-file .env \
  -p 8000:8000 \
  octoflux:latest
```

**Never copy `.env` into the Docker image or commit it to the repository.**

---

# Configuration

OctoFlux is configuration-driven.

The default configuration is:

```text
config/OctoFlux.yaml
```

Override the path using:

```env
OctoFlux_CONFIG=/path/to/config.yaml
```

Secrets are resolved from environment variables:

```yaml
keys:
  - name: primary
    value: "${GROQ_API_KEY_1}"

  - name: secondary
    value: "${GROQ_API_KEY_2:-unset}"
```

Required variables cause startup to fail when missing.

Optional variables can specify a fallback using:

```text
${VARIABLE:-default}
```

OctoFlux validates the configuration at startup and fails fast on errors such as:

* Duplicate provider IDs
* Duplicate model IDs
* Duplicate key names
* Invalid `base_url`
* Invalid limits
* Providers without enabled keys
* Providers without enabled models
* Aliases referencing unknown providers or models

---

# Adding a Provider

No Python code is required.

Add a provider to `config/OctoFlux.yaml`:

```yaml
providers:
  my_provider:
    enabled: true
    type: openai_compatible

    base_url: "https://api.example.com/v1"

    priority: 30
    key_selection: round_robin

    keys:
      - name: primary
        value: "${MY_PROVIDER_API_KEY}"

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

Lower priority values are attempted first.

Additional examples are available in:

```text
config/providers.example.yaml
```

Providers using non-standard authentication can configure custom headers:

```yaml
authentication:
  type: header
  header: "X-Api-Key"

headers:
  X-Custom-Header: "${CUSTOM_VALUE}"
```

---

# API Key Rotation

Keys are configured independently:

```yaml
keys:
  - name: key-tight-budget
    value: "${KEY_1}"
    limits:
      requests_per_minute: 10

  - name: key-normal
    value: "${KEY_2}"
```

Each key maintains independent runtime state:

* Health
* Cooldown
* Usage
* Rate limits
* Selection state

Supported key-selection strategies:

```text
round_robin
least_used
priority
```

---

# Model Routing

Models can be referenced in three ways.

## Exact model

```json
{
  "model": "llama-3.3-70b-versatile"
}
```

OctoFlux only fails over to another provider offering that same model ID.

It will **not silently substitute another model**.

## Alias

Aliases allow explicit model fallback:

```yaml
aliases:
  fast:
    - provider: groq
      model: llama-3.1-8b-instant

    - provider: groq
      model: llama-3.3-70b-versatile
```

Then:

```json
{
  "model": "fast"
}
```

tries the configured targets in order.

Aliases are useful when you intentionally want model substitution.

## Automatic routing

```json
{
  "model": "auto"
}
```

`auto` considers all enabled models across all enabled providers and orders candidates using:

```text
(provider.priority, model.priority)
```

---

# Model Discovery

```http
GET /v1/models
```

Returns configured models and aliases using an OpenAI-compatible response format.

To inspect alias targets:

```http
GET /v1/aliases
```

Example:

```json
{
  "object": "list",
  "data": {
    "fast": [
      {
        "provider": "nvidia_nim",
        "model": "nvidia/nemotron-3.5-lightning-30b-a3b",
        "enabled": true
      }
    ]
  }
}
```

Both endpoints require the configured OctoFlux client key when:

```yaml
server:
  require_auth: true
```

Configured model IDs should match the IDs exposed by the provider's `/models` endpoint.

OctoFlux also handles NVIDIA NIM's publisher-prefixed model IDs when the provider returns the corresponding bare model name.

---

# Local Rate Limiting

Limits are enforced **before an upstream request is sent**.

Supported limits include:

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

Unset limits are unlimited.

Windows operate independently. For example, exhausting the daily request limit blocks requests even if the minute-level limit still has capacity.

Limits can be configured at provider and key level.

---

# Routing Pipeline

Routing is deterministic.

For each request OctoFlux:

1. Resolves the requested model, alias, or `auto`.
2. Removes disabled providers and models.
3. Removes providers and keys in cooldown.
4. Removes targets whose local limits are exhausted.
5. Sorts candidates by provider and model priority.
6. Selects a key using the configured key-selection strategy.
7. Sends the request.
8. Classifies the result.
9. Retries or fails over when appropriate.

This logic is implemented primarily in:

```text
app/core/router.py
app/core/retry.py
app/core/errors.py
```

---

# Failover

OctoFlux does not blindly retry every error.

| Condition              | Behaviour                                     |
| ---------------------- | --------------------------------------------- |
| `429` rate limited     | Cool down the key and try another target      |
| `401/403`              | Cool down the key and avoid retrying that key |
| `5xx`                  | Back off and retry/fail over                  |
| Timeout                | Back off and retry/fail over                  |
| Connection failure     | Retry/fail over                               |
| `400` invalid request  | Return immediately                            |
| Context-length failure | Return immediately                            |
| Model not found        | Try another provider with the same model      |

The retry engine is bounded by:

```yaml
routing:
  max_total_attempts: 4
```

A single inbound request never retries the exact same:

```text
(provider, model, key)
```

combination twice.

This prevents retry storms and avoids repeatedly sending requests that are guaranteed to fail.

---

# Health Checks

Provider and key health is tracked independently.

The background health system periodically probes:

```http
GET /models
```

The default interval is:

```text
43,200 seconds
12 hours
```

Checks run concurrently using the shortest configured provider interval.

Health information is available through:

```http
GET /health
GET /admin/status
```

Manual checks are also available:

```http
POST /admin/providers/{provider_id}/keys/{key_name}/test
```

A successful `/models` request means the provider accepted the request for that key.

It does **not** guarantee:

* Every configured model exists.
* Every model supports chat completions.
* The account has sufficient quota.
* The account is not currently rate limited.

The live provider response remains the source of truth.

---

# Admin Dashboard

Open:

```text
http://localhost:8000/admin
```

The dashboard uses an existing OctoFlux client key. There is no separate admin database or password system.

The key is stored in browser `sessionStorage` and sent as:

```http
Authorization: Bearer <key>
```

The dashboard provides:

* Provider health and priority
* Key health and cooldown state
* Masked key hints
* Model availability
* Per-key testing
* Request counters
* Success/failure counters
* Rate-limit counters
* Timeout counters
* Fallback counters
* Configuration reload
* Manual refresh
* Sign-out

### Configuration reload

After modifying `config/OctoFlux.yaml` or its environment variables:

```http
POST /admin/reload
```

Reloading preserves runtime health, cooldown, and usage state for providers and keys that still exist.

If the new configuration is invalid, OctoFlux keeps the currently active configuration instead of switching to a broken state.

> **Security:** the client key grants access to both the gateway and admin API. Deploy the admin interface behind HTTPS and restrict access to trusted operators.

---

# Streaming

Set:

```json
{
  "stream": true
}
```

OctoFlux proxies upstream SSE responses chunk-by-chunk without buffering the entire response.

Failover is only possible **before the first byte reaches the client**.

Once streaming has started, a mid-stream failure terminates the connection instead of restarting the request against another provider.

This prevents silently duplicated model output.

---

# Responses API

OctoFlux exposes:

```http
POST /v1/responses
```

This is currently a thin compatibility layer over the chat-completions routing pipeline.

It provides the same:

* Provider routing
* Key rotation
* Limits
* Health handling
* Retry logic
* Failover

It is **not** a full implementation of OpenAI's Responses API.

It does not currently provide:

* Server-side conversation state
* Built-in tools
* Full Responses API feature parity

For maximum compatibility, use:

```http
POST /v1/chat/completions
```

---

# Observability

OctoFlux emits structured JSON logs to stdout.

Example:

```json
{
  "ts": 1755000000.1,
  "event": "upstream_failure",
  "request_id": "abc123",
  "provider": "groq",
  "model": "llama-3.3-70b-versatile",
  "key": "groq-primary",
  "category": "rate_limited",
  "status_code": 429,
  "attempt": 1,
  "retryable": true
}
```

Sensitive information is never logged.

OctoFlux does **not** log:

* API-key values
* Authorization headers
* Prompt bodies
* Response bodies

Key names and metadata may be logged for operational visibility.

Set the log level with:

```env
OctoFlux_LOG_LEVEL=INFO
```

Supported levels:

```text
DEBUG
INFO
WARNING
ERROR
```

Every routing decision includes its reason through the `routing_decision` event.

---

# Usage Tracking

Runtime usage statistics are kept in memory.

Tracked metrics include:

* Requests
* Successful requests
* Failed requests
* Input tokens
* Output tokens
* Latency
* Rate-limit events
* Fallbacks

Statistics are available globally and by:

* Provider
* Model
* API key

Endpoints:

```http
GET /admin/usage
GET /metrics
```

No database is required.

A persistent `UsageStore` can be introduced later if historical usage needs to survive restarts.

---

# Security

## Client → OctoFlux

Authentication uses:

```http
Authorization: Bearer <client-key>
```

Keys are configured through:

```yaml
server:
  client_keys:
    - "${OctoFlux_CLIENT_KEY}"
```

Authentication can be disabled for local development:

```yaml
server:
  require_auth: false
```

Do **not** disable authentication on an exposed production instance.

## OctoFlux → Provider

Provider credentials:

* Are resolved from environment variables.
* Are attached only to upstream requests.
* Are never returned through the API.
* Are never included in logs.
* Are never exposed through the admin dashboard.

---

# PM2

For Linux development or lightweight deployments, OctoFlux can be managed with PM2.

Install:

```bash
npm install -g pm2
```

Start the application directly:

```bash
pm2 start \
  "python -m uvicorn app.main:app --host 0.0.0.0 --port 8000" \
  --name octoflux
```

Inspect:

```bash
pm2 status
pm2 logs octoflux
```

You can also run the helper script:

```bash
pm2 start "bash ./run.sh" --name octoflux
```

The direct Uvicorn command is preferable for production-style PM2 deployments because `run.sh` is intended primarily for development and may enable reload behaviour.

---

# Production Deployment

OctoFlux is designed to run as a **single process** by default.

```bash
uvicorn app.main:app \
  --host 0.0.0.0 \
  --port 8000 \
  --workers 1
```

The single-worker constraint matters because health, rate-limit, and usage state are currently stored in process memory.

Multiple independent workers would therefore have independent runtime state.

For production:

* Run one worker per OctoFlux instance.
* Use Docker, systemd, supervisord, or another process manager.
* Put a load balancer in front if multiple instances are required.
* Configure centralized logging.
* Protect the admin interface.
* Keep provider credentials outside the repository.
* Use HTTPS when exposing the gateway beyond localhost.

No external database, cache, or message queue is required.

---

# Development

Create a virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

Install development dependencies:

```bash
pip install -e ".[dev]"
```

Run the application:

```bash
uvicorn app.main:app \
  --host 0.0.0.0 \
  --port 8000 \
  --reload
```

---

# Testing

Run the test suite:

```bash
pytest -q
```

The test suite uses `respx` to mock upstream HTTP requests.

No real provider network calls are required.

Coverage includes:

* Configuration validation
* Environment-variable resolution
* Provider/model/key routing
* Priority ordering
* Key rotation
* Cooldowns
* Local rate limits
* Error classification
* Provider failover
* Key failover
* Alias-based model fallback
* Timeout handling
* Invalid-request handling
* Context-length handling
* Streaming pre-first-byte failover
* API authentication

---

# Benchmarking

Benchmark routing and scheduling overhead with:

```bash
python scripts/benchmark.py \
  --requests 500 \
  --concurrency 20
```

The benchmark uses a mocked upstream to isolate OctoFlux's internal routing and scheduling overhead from real network and provider latency.

---

# API Overview

| Method | Endpoint                                      | Purpose                            |
| ------ | --------------------------------------------- | ---------------------------------- |
| `POST` | `/v1/chat/completions`                        | OpenAI-compatible chat completions |
| `POST` | `/v1/responses`                               | Responses API compatibility layer  |
| `GET`  | `/v1/models`                                  | List configured models and aliases |
| `GET`  | `/v1/aliases`                                 | Inspect alias targets              |
| `GET`  | `/health`                                     | Gateway and provider health        |
| `GET`  | `/admin`                                      | Admin dashboard                    |
| `GET`  | `/admin/status`                               | Runtime routing state              |
| `GET`  | `/admin/usage`                                | Runtime usage statistics           |
| `GET`  | `/metrics`                                    | Metrics endpoint                   |
| `POST` | `/admin/reload`                               | Reload configuration               |
| `POST` | `/admin/providers/{provider}/keys/{key}/test` | Test a provider key                |

Protected endpoints require the configured OctoFlux client key when authentication is enabled.

---

# Project Status

OctoFlux is intentionally focused on the gateway layer.

The architecture favors:

* Simple deployment
* Deterministic routing
* Provider independence
* Explicit failure handling
* Minimal infrastructure
* Configuration over provider-specific code

---

# Non-Goals

The following are intentionally outside v1:

* Multi-tenant billing
* Distributed rate-limit state
* Shared runtime state across workers
* Automatic configuration file watching
* Full OpenAI Responses API parity
* Mandatory Redis/PostgreSQL/Kafka infrastructure
* Persistent usage history

Configuration changes are applied explicitly through:

```http
POST /admin/reload
```

See [`ARCHITECTURE.md`](ARCHITECTURE.md) for the complete list of design non-goals and architectural trade-offs.

---

# Support the Project

If OctoFlux is useful to you, consider supporting its development:

**[☕ Buy Me a Coffee](https://buymeacoffee.com/octoprograms)**

---

## License

See the repository's license file for licensing terms.
